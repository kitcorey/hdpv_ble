"""Services for the Hunter Douglas PowerView (BLE) integration."""

import asyncio
from typing import TYPE_CHECKING, Final, cast

import voluptuous as vol

from homeassistant.components.cover import (
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    CoverEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform

from .api import CLOSED_POSITION, OPEN_POSITION, ShadeMove
from .const import ATTR_CONNECT_TIMEOUT, DOMAIN, LOGGER, SERVICE_GROUP_MOVE

if TYPE_CHECKING:
    # Type-only: cover.py imports the package __init__, which imports this.
    from .cover import PowerViewCoverBase

# Covers the slowest connect measured on a busy proxy (~7.3 s) with headroom.
DEFAULT_CONNECT_TIMEOUT: Final[float] = 10

GROUP_MOVE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): vol.All(cv.entity_ids, vol.Length(min=1)),
        vol.Required(ATTR_POSITION): vol.All(
            vol.Coerce(int), vol.Range(min=CLOSED_POSITION, max=OPEN_POSITION)
        ),
        vol.Optional(ATTR_CONNECT_TIMEOUT, default=DEFAULT_CONNECT_TIMEOUT): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=60)
        ),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration-wide services."""

    async def _async_group_move(call: ServiceCall) -> ServiceResponse:
        return await _group_move(
            _resolve_covers(hass, call.data[ATTR_ENTITY_ID]),
            call.data[ATTR_POSITION],
            call.data[ATTR_CONNECT_TIMEOUT],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GROUP_MOVE,
        _async_group_move,
        schema=GROUP_MOVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


@callback
def _resolve_covers(
    hass: HomeAssistant, entity_ids: list[str]
) -> list["PowerViewCoverBase"]:
    """Map entity IDs to this integration's cover entities, one per shade."""
    covers: dict[str, PowerViewCoverBase] = {}
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if platform.domain == COVER_DOMAIN:
            covers.update(cast("dict[str, PowerViewCoverBase]", platform.entities))

    entities: list[PowerViewCoverBase] = []
    shades: dict[int, str] = {}
    for entity_id in dict.fromkeys(entity_ids):
        if (entity := covers.get(entity_id)) is None:
            raise ServiceValidationError(f"{entity_id} is not a {DOMAIN} cover")
        if not entity.supported_features & CoverEntityFeature.SET_POSITION:
            raise ServiceValidationError(f"{entity_id} cannot be set to a position")
        # One command per shade: two entities of the same shade (both TDBU
        # rails, or Duolite fabrics) would each send a whole move.
        if (other := shades.get(id(entity.coordinator))) is not None:
            raise ServiceValidationError(
                f"{entity_id} and {other} belong to the same shade; "
                "include only one of them"
            )
        shades[id(entity.coordinator)] = entity_id
        entities.append(entity)
    return entities


async def _async_connect(entity: "PowerViewCoverBase") -> bool:
    """Bring a shade's link up, returning whether it came up."""
    try:
        await entity.coordinator.api.connect()
    except Exception as err:  # noqa: BLE001 - becomes late/failed, never raised
        LOGGER.debug("%s: group_move connect failed: %s", entity.name, err)
        return False
    return True


async def _group_move(
    entities: list["PowerViewCoverBase"], position: int, connect_timeout: float
) -> ServiceResponse:
    """Connect to every shade, then send all the moves at once.

    A shade that is not connected by ``connect_timeout`` is sent its move as
    soon as its own connect finishes, or through the ordinary send path (which
    connects again) if that connect failed. The call returns once every shade
    has an outcome.
    """
    result: dict[str, list[str]] = {
        "moved": [],
        "late": [],
        "failed": [],
        "skipped": [],
    }

    planned: dict[PowerViewCoverBase, tuple[ShadeMove, str]] = {}
    for entity in entities:
        if (plan := entity._plan_move(position)) is None:  # noqa: SLF001
            result["skipped"].append(entity.entity_id)
        else:
            planned[entity] = plan
    if not planned:
        return cast("ServiceResponse", result)

    connects: dict[PowerViewCoverBase, asyncio.Task[bool]] = {
        entity: asyncio.create_task(_async_connect(entity)) for entity in planned
    }
    done, _ = await asyncio.wait(connects.values(), timeout=connect_timeout)
    in_step = {
        entity
        for entity, task in connects.items()
        if task in done and task.result() and entity.coordinator.api.is_connected
    }
    LOGGER.debug(
        "group_move: %i of %i shades connected, sending", len(in_step), len(planned)
    )

    async def _send(entity: "PowerViewCoverBase") -> bool:
        if entity not in in_step:
            # Outcome deliberately ignored: a failed connect is retried by the
            # send itself, which connects on its own.
            await connects[entity]
        return await entity._async_send_planned(*planned[entity])  # noqa: SLF001

    order = list(planned)
    sent = await asyncio.gather(*(_send(entity) for entity in order))
    for entity, ok in zip(order, sent, strict=True):
        if not ok:
            outcome = "failed"
        elif entity in in_step:
            outcome = "moved"
        else:
            outcome = "late"
        result[outcome].append(entity.entity_id)

    if result["failed"]:
        LOGGER.warning("group_move: %s did not move", ", ".join(result["failed"]))
    return cast("ServiceResponse", result)
