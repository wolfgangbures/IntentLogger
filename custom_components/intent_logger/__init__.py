"""Log Assist transcripts and intent results locally."""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from homeassistant.components.assist_pipeline import PipelineRun
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import intent

from .const import (
    CONF_FAILURE_EVENTS,
    CONF_IGNORE_DUPLICATE_WAKE_UPS,
    CONF_MAX_FILE_MB,
    CONF_MAX_FILES,
    CONF_PIPELINE_IDS,
    CONF_RETENTION_DAYS,
    DEFAULTS,
    DOMAIN,
    EVENT_FAILED,
)
from .hooks import PipelineObserver
from .storage import LogStore

_LOGGER = logging.getLogger(__name__)
_QUEUE_SIZE = 1000
type IntentLoggerEntry = ConfigEntry[Runtime]


class Runtime:
    """Serialize disk work off the event loop with a bounded backlog."""

    def __init__(self, hass: HomeAssistant, settings: Mapping[str, Any]) -> None:
        """Configure the writer and observer."""
        self.hass = hass
        self.store = LogStore(
            Path(hass.config.path(DOMAIN)),
            settings[CONF_RETENTION_DAYS],
            settings[CONF_MAX_FILE_MB],
            settings[CONF_MAX_FILES],
        )
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(_QUEUE_SIZE)
        self.worker: asyncio.Task[None] | None = None
        self.failure_events = settings[CONF_FAILURE_EVENTS]
        self.observer = PipelineObserver(
            hass,
            PipelineRun,
            intent,
            settings[CONF_PIPELINE_IDS],
            self.enqueue,
            self.fire_failure,
            ignore_duplicate_wake_ups=settings[CONF_IGNORE_DUPLICATE_WAKE_UPS],
        )
        self._stopping = False

    @callback
    def enqueue(self, record: dict[str, Any]) -> None:
        """Enqueue a snapshot without blocking the pipeline."""
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            _LOGGER.error(
                "Intent Logger backlog is full; dropping record for run %s",
                record["run_id"],
            )

    @callback
    def fire_failure(self, record: dict[str, Any]) -> None:
        """Expose a structured failure to local automations."""
        if self.failure_events:
            self.hass.bus.async_fire(EVENT_FAILED, record)

    async def write_records(self) -> None:
        """Write and expire logs on a single executor job at a time."""
        while True:
            try:
                record = await asyncio.wait_for(self.queue.get(), timeout=3600)
            except TimeoutError:
                try:
                    await self.hass.async_add_executor_job(self.store.cleanup)
                except OSError:
                    _LOGGER.exception("Unable to expire Intent Logger logs")
                continue
            try:
                if record is None:
                    return
                await self.hass.async_add_executor_job(self.store.append, record)
            except OSError, ValueError, TypeError:
                _LOGGER.exception("Unable to write Intent Logger record")
            finally:
                self.queue.task_done()

    async def stop(self, event: Event | None = None) -> None:
        """Stop capture and flush queued records before unload or shutdown."""
        if self._stopping:
            if self.worker is not None:
                await self.worker
            return
        self._stopping = True
        self.observer.uninstall()
        if self.worker is not None:
            await self.queue.put(None)
            await self.worker


async def async_setup_entry(hass: HomeAssistant, entry: IntentLoggerEntry) -> bool:
    """Enable pipeline observation for this Home Assistant instance."""
    settings = {**DEFAULTS, **entry.data, **entry.options}
    runtime = Runtime(hass, settings)
    try:
        await hass.async_add_executor_job(runtime.store.prepare)
    except OSError as err:
        raise ConfigEntryNotReady(
            "Cannot prepare the Intent Logger log directory"
        ) from err
    try:
        runtime.observer.install()
    except RuntimeError as err:
        raise ConfigEntryError(str(err)) from err
    runtime.worker = hass.async_create_background_task(
        runtime.write_records(), "Intent Logger writer"
    )
    entry.runtime_data = runtime
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, runtime.stop)
    )
    _LOGGER.warning(
        "Intent Logger is capturing Assist text to %s using internal pipeline APIs",
        runtime.store.directory,
    )
    return True


async def _async_update_options(hass: HomeAssistant, entry: IntentLoggerEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IntentLoggerEntry) -> bool:
    """Stop logging and restore Home Assistant's original methods."""
    await entry.runtime_data.stop()
    return True
