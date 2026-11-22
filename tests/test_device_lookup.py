"""Tests that every device-registry lookup is scoped to the shade's config entry."""

# The lookups under test live in private helpers.
# ruff: noqa: SLF001

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.hunterdouglas_powerview_ble import _migrate_legacy_identity
from custom_components.hunterdouglas_powerview_ble.const import DOMAIN
from custom_components.hunterdouglas_powerview_ble.coordinator import PVCoordinator
from custom_components.hunterdouglas_powerview_ble.diagnostics import _entities
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er

ENTRY_ID = "entry-mine"
OTHER_ENTRY_ID = "entry-other"
ADDRESS = "E4:F4:9A:A1:8B:F8"
NEW_ADDRESS = "E4:F4:9A:A1:8B:F9"
SHADE_ID = "ROL:88E2"


class _DeviceRegistry:
    """Hold devices by (identifier, config entry), as the real registry does."""

    def __init__(self) -> None:
        self.devices: dict[tuple[tuple[str, str], str], Any] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def add(self, identifier: tuple[str, str], entry_id: str) -> Any:
        device = SimpleNamespace(id=f"dev-{entry_id}-{identifier[1]}")
        self.devices[(identifier, entry_id)] = device
        return device

    def async_get_device_by_identifier(
        self, identifier: tuple[str, str], config_entry_id: str
    ) -> Any:
        return self.devices.get((identifier, config_entry_id))

    def async_update_device(self, device_id: str, **changes: Any) -> None:
        self.updates.append((device_id, changes))


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> _DeviceRegistry:
    """Install an empty fake device registry."""
    reg = _DeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda hass: reg)
    return reg


def _coordinator() -> PVCoordinator:
    """Build a coordinator without the Bluetooth machinery behind it."""
    coord = PVCoordinator.__new__(PVCoordinator)
    coord.hass = MagicMock()
    coord.api = MagicMock()
    coord.address = ADDRESS
    coord.entry_id = ENTRY_ID
    coord._shade_id = SHADE_ID
    coord._friendly_name = SHADE_ID
    coord.dev_details = {"sw_rev": "1.2", "hw_rev": "3", "serial_nr": "SN"}
    return coord


def test_device_info_updates_only_this_entrys_device(
    registry: _DeviceRegistry,
) -> None:
    """Firmware details land on this entry's device, not a namesake's."""
    other = registry.add((DOMAIN, SHADE_ID), OTHER_ENTRY_ID)
    coord = _coordinator()

    coord._push_device_registry_update()
    assert registry.updates == []  # only the other entry holds the name

    mine = registry.add((DOMAIN, SHADE_ID), ENTRY_ID)
    coord._push_device_registry_update()
    assert [dev for dev, _ in registry.updates] == [mine.id]
    assert other.id not in {dev for dev, _ in registry.updates}


def test_retarget_moves_this_entrys_device(
    registry: _DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moved shade re-addresses its own device and leaves others alone."""
    monkeypatch.setattr(
        PassiveBluetoothDataUpdateCoordinator, "_async_stop", lambda self: None
    )
    monkeypatch.setattr(PVCoordinator, "_async_start", lambda self: None)
    registry.add((DOMAIN, SHADE_ID), OTHER_ENTRY_ID)
    mine = registry.add((DOMAIN, SHADE_ID), ENTRY_ID)
    coord = _coordinator()

    moved: Any = SimpleNamespace(address=NEW_ADDRESS)
    coord.async_retarget(moved)
    assert [dev for dev, _ in registry.updates] == [mine.id]
    assert (dr.CONNECTION_BLUETOOTH, NEW_ADDRESS) in registry.updates[0][1][
        "new_connections"
    ]


def test_migration_looks_only_in_this_entry(
    registry: _DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another entry's devices neither block nor feed this entry's migration."""
    monkeypatch.setattr(er, "async_get", lambda hass: MagicMock())
    monkeypatch.setattr(er, "async_entries_for_device", lambda *a, **kw: [])
    # The other entry has already migrated this name and still has the address.
    registry.add((DOMAIN, SHADE_ID), OTHER_ENTRY_ID)
    registry.add((DOMAIN, ADDRESS), OTHER_ENTRY_ID)

    _migrate_legacy_identity(MagicMock(), ENTRY_ID, ADDRESS, SHADE_ID)
    assert registry.updates == []  # nothing of ours to move

    legacy = registry.add((DOMAIN, ADDRESS), ENTRY_ID)
    _migrate_legacy_identity(MagicMock(), ENTRY_ID, ADDRESS, SHADE_ID)
    assert [dev for dev, _ in registry.updates] == [legacy.id]
    assert (DOMAIN, SHADE_ID) in registry.updates[0][1]["new_identifiers"]


def test_diagnostics_lists_this_entrys_entities(
    registry: _DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics read the entities of this entry's device."""
    queried: list[str] = []

    def entries_for_device(reg: Any, device_id: str, **kw: Any) -> list[Any]:
        queried.append(device_id)
        return []

    monkeypatch.setattr(er, "async_get", lambda hass: MagicMock())
    monkeypatch.setattr(er, "async_entries_for_device", entries_for_device)
    registry.add((DOMAIN, SHADE_ID), OTHER_ENTRY_ID)
    coord: Any = SimpleNamespace(shade_id=SHADE_ID, entry_id=ENTRY_ID)

    assert _entities(MagicMock(), coord) == []
    assert queried == []  # no device of ours, so no entity query at all

    mine = registry.add((DOMAIN, SHADE_ID), ENTRY_ID)
    _entities(MagicMock(), coord)
    assert queried == [mine.id]
