"""Button entities for Secure Meters."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
import logging

from aiohttp import ClientWebSocketResponse
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import (
    CONNECTION_MODE_LOCAL_BLE,
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    async_get_local_ble_worker,
    async_refresh_entry_state,
    async_execute_controller_command,
    coerce_end_time,
)
from .beanbag import BeanbagBackend, BeanbagError, BeanbagSession, WeeklyProgram
from .entity import (
    SecuremtrCommandMixin,
    SecuremtrRuntimeEntityMixin,
    async_get_ready_controller,
    controller_display_label,
)
from .runtime_helpers import async_read_zone_programs

_LOGGER = logging.getLogger(__name__)

_DAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


BOOST_BUTTON_TRANSLATION_KEYS: dict[int, str] = {
    30: "boost_30_minutes",
    60: "boost_60_minutes",
    120: "boost_120_minutes",
}

DEFAULT_BOOST_TRANSLATION_KEY = "boost_custom_minutes"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Secure Meters button entities for a config entry."""

    entry_label = getattr(entry, "title", None) or getattr(entry, "entry_id", DOMAIN)

    try:
        runtime, controller = await async_get_ready_controller(hass, entry)
    except HomeAssistantError as error:
        _LOGGER.warning(
            "Skipping SecureMTR button setup for %s: %s",
            entry_label,
            error,
        )
        if isinstance(error.__cause__, TimeoutError):
            raise ConfigEntryNotReady(
                f"SecureMTR controller for {entry_label} is not ready"
            ) from error
        return

    async_add_entities(
        [
            SecuremtrTimedBoostButton(runtime, controller, entry, 30),
            SecuremtrTimedBoostButton(runtime, controller, entry, 60),
            SecuremtrTimedBoostButton(runtime, controller, entry, 120),
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
        self._set_slug_identifiers("refresh_consumption")
        self._attr_translation_key = "refresh_consumption_metrics"

    async def async_press(self) -> None:
        """Trigger an on-demand refresh of consumption metrics."""

        hass = self.hass
        if hass is None:
            raise HomeAssistantError("Home Assistant instance is not available")

        await async_refresh_entry_state(hass, self._entry)

        if self._runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
            _LOGGER.info("Triggered local BLE runtime refresh")


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
        self._set_slug_identifiers("log_schedule")
        self._attr_translation_key = "log_weekly_schedules"

    async def async_press(self) -> None:
        """Fetch the weekly programs for both zones and emit them to the log."""

        runtime = self._runtime
        entry = self._entry

        if runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
            hass = self.hass
            if hass is None:
                raise HomeAssistantError("Home Assistant instance is not available")

            from .local_ble_commissioning import (  # noqa: PLC0415
                LocalBleCommissioningError,
                LocalBlePriority,
            )

            try:
                worker = await async_get_local_ble_worker(hass, entry, runtime)
                programs, canonicals = await worker.async_read_local_weekly_programs(
                    priority=LocalBlePriority.USER_READ,
                    coalesce_key=None,
                    zone_bois=runtime.schedule_zone_bois,
                )
            except LocalBleCommissioningError as error:
                _LOGGER.error("Failed to read Secure Meters weekly schedule: %s", error)
                raise HomeAssistantError(
                    "Failed to read Secure Meters weekly schedule"
                ) from error
        else:

            async def _read_programs(
                backend: BeanbagBackend,
                session: BeanbagSession,
                websocket: ClientWebSocketResponse,
                controller: SecuremtrController,
            ) -> tuple[
                dict[str, WeeklyProgram | None],
                dict[str, list[tuple[int, int]] | None],
            ]:
                controller_label = controller_display_label(controller)
                return await async_read_zone_programs(
                    backend,
                    session,
                    websocket,
                    gateway_id=controller.gateway_id,
                    entry_identifier=controller_label,
                )

            programs, canonicals = await async_execute_controller_command(
                runtime,
                entry,
                _read_programs,
                log_context="Failed to read Secure Meters weekly schedule",
            )

        controller = runtime.controller
        if controller is None:  # pragma: no cover - defensive guard
            raise HomeAssistantError("Secure Meters controller is not connected")

        controller_label = controller_display_label(controller)

        primary_program = programs.get("primary")
        boost_program = programs.get("boost")
        missing_zones = [
            zone_key
            for zone_key, program in (
                ("primary", primary_program),
                ("boost", boost_program),
            )
            if program is None
        ]
        if missing_zones:
            _LOGGER.error(
                "Failed to read Secure Meters weekly schedule for %s: missing zones %s",
                controller_label,
                ", ".join(missing_zones),
            )
            raise HomeAssistantError("Failed to read Secure Meters weekly schedule")

        assert primary_program is not None and boost_program is not None

        primary_canonical = canonicals.get("primary") if canonicals else None
        boost_canonical = canonicals.get("boost") if canonicals else None

        _LOGGER.debug(
            "Canonical weekly schedule for %s primary zone: %s",
            controller_label,
            primary_canonical,
        )
        _LOGGER.debug(
            "Canonical weekly schedule for %s boost zone: %s",
            controller_label,
            boost_canonical,
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


class SecuremtrTimedBoostButton(SecuremtrCommandMixin, ButtonEntity):
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
        self._set_slug_identifiers(f"boost_{duration_minutes}")
        translation_key = BOOST_BUTTON_TRANSLATION_KEYS.get(
            duration_minutes, DEFAULT_BOOST_TRANSLATION_KEY
        )
        self._attr_translation_key = translation_key
        if translation_key == DEFAULT_BOOST_TRANSLATION_KEY:
            self._attr_translation_placeholders = {"duration": str(duration_minutes)}

    async def async_press(self) -> None:
        """Send the timed boost start command for the configured duration."""

        duration = self._duration

        await self._async_controller_command(
            "start_timed_boost",
            duration_minutes=duration,
            runtime_update=lambda data: self._apply_timed_boost_start(data, duration),
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


class SecuremtrCancelBoostButton(SecuremtrCommandMixin, ButtonEntity):
    """Cancel an active timed boost run."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the timed boost cancellation button."""

        super().__init__(runtime, controller, entry)
        self._set_slug_identifiers("boost_cancel")
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

        await self._async_controller_command(
            "stop_timed_boost",
            runtime_update=self._apply_timed_boost_stop,
            log_context="Failed to cancel Secure Meters timed boost",
        )

    @staticmethod
    def _apply_timed_boost_stop(runtime: SecuremtrRuntimeData) -> None:
        """Update runtime state after cancelling a timed boost."""

        runtime.timed_boost_active = False
        runtime.timed_boost_end_minute = None
        runtime.timed_boost_end_time = None


__all__ = [
    "SecuremtrCancelBoostButton",
    "SecuremtrConsumptionMetricsButton",
    "SecuremtrLogWeeklyScheduleButton",
    "SecuremtrTimedBoostButton",
]
