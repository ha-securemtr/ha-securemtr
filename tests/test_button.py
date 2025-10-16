from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.securemtr import DOMAIN, SecuremtrController, SecuremtrRuntimeData
from custom_components.securemtr.button import (
    SecuremtrConsumptionMetricsButton,
    async_setup_entry,
)
from homeassistant.exceptions import HomeAssistantError


@dataclass(slots=True)
class DummyEntry:
    """Provide the minimal attributes required by the button platform."""

    entry_id: str


class DummyBackend:
    """Stub backend for button tests."""

    async def read_device_metadata(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        """Unused backend stub."""


def _create_runtime() -> SecuremtrRuntimeData:
    """Create runtime data with a ready controller."""

    runtime = SecuremtrRuntimeData(backend=DummyBackend())
    runtime.session = SimpleNamespace()
    runtime.websocket = SimpleNamespace()
    runtime.controller = SecuremtrController(
        identifier="controller-1",
        name="E7+ Water Heater (SN: serial-1)",
        gateway_id="gateway-1",
        serial_number="serial-1",
        firmware_version="1.0.0",
        model="E7+",
    )
    runtime.controller_ready.set()
    return runtime


@pytest.mark.asyncio
async def test_button_setup_and_press(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the button triggers the consumption metrics helper."""

    runtime = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SecuremtrConsumptionMetricsButton] = []

    async def fake_consumption_metrics(hass_obj, entry_obj):
        fake_consumption_metrics.calls.append((hass_obj, entry_obj))

    fake_consumption_metrics.calls = []

    monkeypatch.setattr(
        "custom_components.securemtr.button.consumption_metrics",
        fake_consumption_metrics,
    )

    def _add_entities(new_entities: list[SecuremtrConsumptionMetricsButton]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert len(entities) == 1
    button = entities[0]
    assert button.unique_id == "serial_1_consumption_metrics"
    assert button.name == "Get Consumption Metrics"
    assert button.available
    assert button.device_info["identifiers"] == {(DOMAIN, "serial-1")}

    button.hass = hass
    await button.async_press()

    assert fake_consumption_metrics.calls == [(hass, entry)]

    runtime.websocket = None
    assert not button.available


@pytest.mark.asyncio
async def test_button_setup_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure setup raises when controller metadata does not arrive."""

    runtime = _create_runtime()
    runtime.controller_ready.clear()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    async def fake_wait_for(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr("custom_components.securemtr.button.asyncio.wait_for", fake_wait_for)

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_button_setup_missing_controller() -> None:
    """Ensure setup fails when controller metadata was not populated."""

    runtime = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)
