"""Tests for the group_move service."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hunterdouglas_powerview_ble import services
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.exceptions import ServiceValidationError


class _Shade:
    """Stand in for a loaded cover entity that moves when asked."""

    supported_features = CoverEntityFeature.SET_POSITION

    def __init__(self, entity_id: str, coordinator: object | None = None) -> None:
        self.entity_id = self.name = entity_id
        self.coordinator = coordinator or _coordinator()
        self._async_send_planned = AsyncMock(return_value=True)

    def _plan_move(self, position: int) -> tuple[str, str]:
        return "move", f"to {position}"


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(api=SimpleNamespace(connect=AsyncMock(), is_connected=True))


def _group_move(
    monkeypatch: pytest.MonkeyPatch, loaded: list[_Shade], data: dict[str, Any]
) -> Any:
    """Register the services against the given loaded covers and call group_move."""
    platform = SimpleNamespace(
        domain="cover", entities={shade.entity_id: shade for shade in loaded}
    )
    monkeypatch.setattr(
        services.entity_platform, "async_get_platforms", lambda hass, domain: [platform]
    )
    hass = MagicMock()
    services.async_setup_services(hass)
    handler = hass.services.async_register.call_args.args[2]
    call = SimpleNamespace(data=services.GROUP_MOVE_SCHEMA(data))
    return asyncio.run(handler(call))


def test_unloaded_shade_fails_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shade with no loaded entity is reported failed; the rest still move."""
    shade_1 = _Shade("cover.shade_1")
    result = _group_move(
        monkeypatch,
        [shade_1],
        {"entity_id": ["cover.shade_1", "cover.shade_2"], "position": 100},
    )
    assert result == {
        "moved": ["cover.shade_1"],
        "late": [],
        "failed": ["cover.shade_2"],
        "skipped": [],
    }
    shade_1._async_send_planned.assert_awaited_once()  # noqa: SLF001


def test_no_shade_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing loaded, every shade is reported failed instead of raising."""
    result = _group_move(
        monkeypatch, [], {"entity_id": ["cover.shade_1"], "position": 100}
    )
    assert result["failed"] == ["cover.shade_1"]


def test_two_entities_of_one_shade_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller mistakes still reject the whole call."""
    coordinator = _coordinator()
    with pytest.raises(ServiceValidationError):
        _group_move(
            monkeypatch,
            [_Shade("cover.top", coordinator), _Shade("cover.bottom", coordinator)],
            {"entity_id": ["cover.top", "cover.bottom"], "position": 50},
        )
