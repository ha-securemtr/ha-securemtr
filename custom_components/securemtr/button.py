"""Button entities for Secure Meters."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
import logging

from aiohttp import ClientWebSocketResponse
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from typing import Final, NamedTuple

from . import (
    SecuremtrController,
    SecuremtrRuntimeData,
    async_execute_controller_command,
    coerce_end_time,
    consumption_metrics,
)
from .beanbag import BeanbagBackend, BeanbagError, BeanbagSession, WeeklyProgram
from .entity import SecuremtrRuntimeEntityMixin, async_get_ready_controller
from .runtime_helpers import async_read_zone_program

_LOGGER = logging.getLogger(__name__)


class BoostButtonMetadata(NamedTuple):
    """Describe the metadata associated with a timed boost button."""

    duration_minutes: int
    translation_key: str


_DAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


BOOST_BUTTON_METADATA: Final[tuple[BoostButtonMetadata, ...]] = (
    BoostButtonMetadata(30, "boost_30_minutes"),
    BoostButtonMetadata(60, "boost_60_minutes"),
    BoostButtonMetadata(120, "boost_120_minutes"),
)

_BOOST_BUTTON_METADATA_MAP: Final[dict[int, BoostButtonMetadata]] = {
    metadata.duration_minutes: metadata for metadata in BOOST_BUTTON_METADATA
}

CUSTOM_BOOST_TRANSLATION_KEY: Final[str] = "boost_custom_minutes"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Secure Meters button entities for a config entry."""

    runtime, controller = await async_get_ready_controller(hass, entry)

    timed_boost_buttons = [
        SecuremtrTimedBoostButton(runtime, controller, entry, metadata.duration_minutes)
        for metadata in BOOST_BUTTON_METADATA
    ]
    async_add_entities(
        [
            *timed_boost_buttons,
            SecuremtrCancelBoostButton(runtime, controller, entry),
            SecuremtrConsumptionMetricsButton(runtime, controller, entry),
            SecuremtrLogWeeklyScheduleButton(runtime, controller, entry),
        ]
    )


class SecuremtrConsumptionMetricsButton(SecuremtrRuntimeEntityMixin, ButtonEntity):
    """Trigger a manual refresh of Secure Meters consumption metrics."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the consumption metrics button for the controller."""

        super().__init__(runtime, controller, entry)
        slug = self._identifier_slug()
        self._attr_unique_id = f"{slug}_refresh_consumption"
        self._attr_translation_key = "refresh_consumption_metrics"

    async def async_press(self) -> None:
        """Trigger an on-demand refresh of consumption metrics."""

        hass = self.hass
        if hass is None:
            raise HomeAssistantError("Home Assistant instance is not available")

        await consumption_metrics(hass, self._entry)


class SecuremtrLogWeeklyScheduleButton(SecuremtrRuntimeEntityMixin, ButtonEntity):
    """Read and log the configured weekly schedules."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the schedule logging button."""

        super().__init__(runtime, controller, entry)
        self._attr_unique_id = f"{self._identifier_slug()}_log_schedule"
        self._attr_translation_key = "log_weekly_schedules"

    async def async_press(self) -> None:
        """Fetch the weekly programs for both zones and emit them to the log."""

        runtime = self._runtime
        entry = self._entry

        async def _read_programs(
            backend: BeanbagBackend,
            session: BeanbagSession,
            websocket: ClientWebSocketResponse,
            controller: SecuremtrController,
        ) -> tuple[WeeklyProgram | None, WeeklyProgram | None]:
            controller_label = (
                controller.name
                or controller.serial_number
                or controller.identifier
                or controller.gateway_id
            )
            primary = await async_read_zone_program(
                backend,
                session,
                websocket,
                gateway_id=controller.gateway_id,
                zone="primary",
                entry_identifier=controller_label,
            )
            boost = await async_read_zone_program(
                backend,
                session,
                websocket,
                gateway_id=controller.gateway_id,
                zone="boost",
                entry_identifier=controller_label,
            )
            return primary, boost

        primary_program, boost_program = await async_execute_controller_command(
            runtime,
            entry,
            _read_programs,
            log_context="Failed to read Secure Meters weekly schedule",
        )

        if primary_program is None or boost_program is None:
            _LOGGER.error("Failed to read Secure Meters weekly schedule")
            raise HomeAssistantError("Failed to read Secure Meters weekly schedule")

        controller = runtime.controller
        if controller is None:  # pragma: no cover - defensive guard
            raise HomeAssistantError("Secure Meters controller is not connected")

        controller_label = (
            controller.name
            or controller.serial_number
            or controller.identifier
            or controller.gateway_id
        )

        primary_summary = self._format_program_summary(primary_program)
        boost_summary = self._format_program_summary(boost_program)

        _LOGGER.info(
            "Secure Meters weekly schedule for %s primary zone: %s",
            controller_label,
            primary_summary,
        )
        _LOGGER.info(
            "Secure Meters weekly schedule for %s boost zone: %s",
            controller_label,
            boost_summary,
        )

    @staticmethod
    def _format_program_summary(
        program: WeeklyProgram,
    ) -> dict[str, dict[str, list[str]]]:
        """Convert a weekly program into a human-readable dictionary."""

        summary: dict[str, dict[str, list[str]]] = {}
        for day_name, day_program in zip(_DAY_NAMES, program, strict=False):
            summary[day_name] = {
                "on": SecuremtrLogWeeklyScheduleButton._format_transitions(
                    day_program.on_minutes
                ),
                "off": SecuremtrLogWeeklyScheduleButton._format_transitions(
                    day_program.off_minutes
                ),
            }
        return summary

    @staticmethod
    def _format_transitions(
        minutes: Iterable[int | None],
    ) -> list[str]:
        """Translate minute offsets into HH:MM strings."""

        formatted: list[str] = []
        for minute in minutes:
            if minute is None:
                continue
            hours, remainder = divmod(minute, 60)
            formatted.append(f"{hours:02d}:{remainder:02d}")
        return formatted


class SecuremtrTimedBoostButton(SecuremtrRuntimeEntityMixin, ButtonEntity):
    """Trigger a timed boost run for a fixed duration."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
        duration_minutes: int,
    ) -> None:
        """Initialise the timed boost button for the requested duration."""

        super().__init__(runtime, controller, entry)
        self._duration = duration_minutes
        self._attr_unique_id = f"{self._identifier_slug()}_boost_{duration_minutes}"
        metadata = _BOOST_BUTTON_METADATA_MAP.get(duration_minutes)
        if metadata is None:
            self._attr_translation_key = CUSTOM_BOOST_TRANSLATION_KEY
            self._attr_translation_placeholders = {"duration": str(duration_minutes)}
        else:
            self._attr_translation_key = metadata.translation_key

    async def async_press(self) -> None:
        """Send the timed boost start command for the configured duration."""

        duration = self._duration

        await self._async_mutate(
            operation=lambda backend,
            session,
            websocket,
            controller: backend.start_timed_boost(
                session,
                websocket,
                controller.gateway_id,
                duration_minutes=duration,
            ),
            mutation=lambda data: self._apply_timed_boost_start(data, duration),
            log_context="Failed to start Secure Meters timed boost",
            exception_types=(BeanbagError, ValueError),
        )

    @staticmethod
    def _apply_timed_boost_start(
        runtime: SecuremtrRuntimeData,
        duration: int,
    ) -> None:
        """Update runtime state for a newly started timed boost."""

        runtime.timed_boost_active = True
        now_local = dt_util.now()
        end_local = now_local + timedelta(minutes=duration)
        runtime.timed_boost_end_minute = end_local.hour * 60 + end_local.minute
        runtime.timed_boost_end_time = coerce_end_time(runtime.timed_boost_end_minute)


class SecuremtrCancelBoostButton(SecuremtrRuntimeEntityMixin, ButtonEntity):
    """Cancel an active timed boost run."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the timed boost cancellation button."""

        super().__init__(runtime, controller, entry)
        self._attr_unique_id = f"{self._identifier_slug()}_boost_cancel"
        self._attr_translation_key = "cancel_boost"

    @property
    def available(self) -> bool:
        """Only expose the button while a timed boost is active."""

        return super().available and self._runtime.timed_boost_active is True

    async def async_press(self) -> None:
        """Send the timed boost stop command."""

        runtime = self._runtime

        if runtime.timed_boost_active is not True:
            raise HomeAssistantError("Timed boost is not currently active")

        await self._async_mutate(
            operation=lambda backend,
            session,
            websocket,
            controller: backend.stop_timed_boost(
                session,
                websocket,
                controller.gateway_id,
            ),
            mutation=self._apply_timed_boost_stop,
            log_context="Failed to cancel Secure Meters timed boost",
        )

    @staticmethod
    def _apply_timed_boost_stop(runtime: SecuremtrRuntimeData) -> None:
        """Update runtime state after cancelling a timed boost."""

        runtime.timed_boost_active = False
        runtime.timed_boost_end_minute = None
        runtime.timed_boost_end_time = None


__all__ = [
    "BOOST_BUTTON_METADATA",
    "SecuremtrCancelBoostButton",
    "SecuremtrConsumptionMetricsButton",
    "SecuremtrLogWeeklyScheduleButton",
    "SecuremtrTimedBoostButton",
]
