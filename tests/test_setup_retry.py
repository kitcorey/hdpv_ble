"""Tests for retrying shades that no connectable proxy could reach."""

import asyncio
from collections.abc import Coroutine
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import custom_components.hunterdouglas_powerview_ble as integration
from custom_components.hunterdouglas_powerview_ble import (
    HubShades,
    _async_setup_shade,
    _UnreachableShades,
)
from custom_components.hunterdouglas_powerview_ble.api import V2_RECORD_LEN
from custom_components.hunterdouglas_powerview_ble.const import (
    MFCT_ID,
    RETRY_MIN_INTERVAL,
    SKIP_WARN_AFTER,
)

ADDRESS = "E4:F4:9A:A1:8B:F8"
DIAGNOSIS = "no connectable scanner has seen it"


class _Bluetooth:
    """Stand in for Home Assistant's Bluetooth manager, one shade at a time."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.connectable = False
        self.watches: list[Any] = []
        self.cancelled = 0
        self.clock = 1000.0
        self.tasks: list[Coroutine[Any, Any, None]] = []
        self.hass = MagicMock()
        self.hass.async_create_task.side_effect = self.tasks.append
        bt = integration.bluetooth
        monkeypatch.setattr(integration, "monotonic", lambda: self.clock)
        monkeypatch.setattr(
            integration,
            "async_ble_device_from_address",
            lambda hass, address, connectable: (
                SimpleNamespace(address=address) if self.connectable else None
            ),
        )
        monkeypatch.setattr(bt, "async_register_advertisement_callback", self._watch)
        monkeypatch.setattr(
            bt, "async_last_service_info", lambda hass, address, connectable: None
        )
        monkeypatch.setattr(
            bt,
            "async_address_reachability_diagnostics",
            lambda hass, address, intent: DIAGNOSIS,
        )

    def _watch(self, hass: object, cb: Any, address: str) -> Any:
        self.watches.append(cb)

        def cancel() -> None:
            self.cancelled += 1

        return cancel

    def advert(self) -> None:
        """Deliver one packet to every watch, as each proxy relaying it would."""
        for cb in list(self.watches):
            cb(_advert())

    def run_tasks(self) -> None:
        tasks, self.tasks[:] = list(self.tasks), []
        for task in tasks:
            asyncio.run(task)


def _advert(record_len: int = V2_RECORD_LEN) -> Any:
    return SimpleNamespace(
        address=ADDRESS,
        name="ROL:88E2",
        manufacturer_data={MFCT_ID: bytes(record_len)},
    )


def _shades(bt: _Bluetooth) -> tuple[_UnreachableShades, list[str]]:
    """Build a watch whose retry adopts the shade once there is a path."""
    retried: list[str] = []

    async def retry(service_info: Any) -> None:
        retried.append(service_info.address)
        if bt.connectable:
            unreachable.adopted(service_info.address)
        else:
            unreachable.skipped(service_info.address)

    unreachable = _UnreachableShades(bt.hass, retry)
    return unreachable, retried


def test_unreachable_skip_starts_one_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup with no connectable path watches the address, once."""
    bt = _Bluetooth(monkeypatch)
    unreachable, _ = _shades(bt)
    entry: Any = SimpleNamespace(runtime_data={})
    hub = HubShades({}, {})
    for _ in range(2):
        asyncio.run(_async_setup_shade(bt.hass, entry, _advert(), hub, unreachable))
    assert len(bt.watches) == 1
    assert entry.runtime_data == {}


def test_not_a_shade_is_not_watched(monkeypatch: pytest.MonkeyPatch) -> None:
    """An advert without a V2 record never starts a watch (issue #42)."""
    bt = _Bluetooth(monkeypatch)
    unreachable, _ = _shades(bt)
    entry: Any = SimpleNamespace(runtime_data={})
    asyncio.run(
        _async_setup_shade(
            bt.hass, entry, _advert(record_len=3), HubShades({}, {}), unreachable
        )
    )
    assert bt.watches == []


def test_unchanged_advert_retries_once_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix: a skipped shade is set up from a repeat advert, no move needed."""
    bt = _Bluetooth(monkeypatch)
    unreachable, retried = _shades(bt)
    unreachable.skipped(ADDRESS)

    bt.advert()
    bt.advert()
    assert bt.tasks == []  # no connectable path yet, so nothing to try

    bt.connectable = True  # e.g. the proxy reconnects
    bt.advert()
    bt.run_tasks()
    assert retried == [ADDRESS]
    assert bt.cancelled == 1


def test_burst_of_packets_schedules_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packets from several proxies at once don't queue duplicate setups."""
    bt = _Bluetooth(monkeypatch)
    unreachable, _ = _shades(bt)
    unreachable.skipped(ADDRESS)
    bt.connectable = True
    for _ in range(5):
        bt.advert()
    assert len(bt.tasks) == 1
    bt.run_tasks()


def test_failed_retry_waits_before_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry that doesn't adopt the shade isn't repeated on every packet."""
    bt = _Bluetooth(monkeypatch)

    async def retry(service_info: Any) -> None:
        """Find a path but still not set up, as with no V2 record yet."""

    unreachable = _UnreachableShades(bt.hass, retry)
    unreachable.skipped(ADDRESS)
    bt.connectable = True

    bt.advert()
    bt.run_tasks()
    bt.advert()
    assert bt.tasks == []

    bt.clock += RETRY_MIN_INTERVAL
    bt.advert()
    assert len(bt.tasks) == 1
    bt.run_tasks()


def test_stuck_shade_warns_once_with_diagnosis(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Five minutes unreachable gives one warning that says why."""
    bt = _Bluetooth(monkeypatch)
    unreachable, _ = _shades(bt)
    unreachable.skipped(ADDRESS)

    with caplog.at_level(logging.WARNING):
        bt.advert()
        assert caplog.records == []
        bt.clock += SKIP_WARN_AFTER
        bt.advert()
        bt.advert()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert DIAGNOSIS in warnings[0].getMessage()


def test_unload_cancels_open_watches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unloading drops every watch still registered."""
    bt = _Bluetooth(monkeypatch)
    unreachable, _ = _shades(bt)
    unreachable.skipped(ADDRESS)
    unreachable.skipped("C6:02:A9:65:BE:64")
    unreachable.cancel_all()
    assert bt.cancelled == 2
