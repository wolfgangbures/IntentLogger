"""Narrow, reversible compatibility hooks for Home Assistant 2026.11."""

import asyncio
import inspect
import logging
from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime
from functools import wraps
from types import ModuleType
from typing import Any

from .capture import RunRecord

_LOGGER = logging.getLogger(__name__)
_CURRENT_RUN: ContextVar[RunRecord | None] = ContextVar(
    "intent_logger_run", default=None
)


class PipelineObserver:
    """Observe pipeline runs without changing inputs, outputs, or exceptions."""

    def __init__(
        self,
        hass: Any,
        run_class: type,
        intent_module: ModuleType,
        pipeline_ids: list[str],
        emit: Callable[[dict[str, Any]], None],
        failed: Callable[[dict[str, Any]], None],
    ) -> None:
        """Prepare per-instance hooks without installing them yet."""
        self.hass = hass
        self.run_class = run_class
        self.intent_module = intent_module
        self.pipeline_ids = set(pipeline_ids)
        self.emit = emit
        self.failed = failed
        self._runs: dict[str, RunRecord] = {}
        self._enabled = False
        self._originals: list[tuple[Any, str, Callable, Callable]] = []

    def install(self) -> None:
        """Check upstream signatures before changing any method."""
        targets = (
            (self.run_class, "process_event", ("self", "event")),
            (self.run_class, "async_execute", ("self", "pipeline_input")),
            (self.intent_module, "async_handle", ("hass", "platform", "intent_type")),
        )
        for owner, name, prefix in targets:
            method = getattr(owner, name, None)
            if (
                not callable(method)
                or tuple(inspect.signature(method).parameters)[: len(prefix)] != prefix
            ):
                raise RuntimeError(f"Unsupported Home Assistant API: {name}")
            if getattr(method, "_intent_logger_hook", False):
                raise RuntimeError("Intent Logger hooks are already installed")

        original_event = self.run_class.process_event
        original_execute = self.run_class.async_execute
        original_intent = self.intent_module.async_handle
        intent_signature = inspect.signature(original_intent)

        @wraps(original_event)
        def process_event(run: Any, event: Any) -> None:
            # Observing first also preserves transcripts if a client disconnects.
            if (
                self._enabled
                and run.hass is self.hass
                and (record := self._runs.get(run.id)) is not None
            ):
                self._observe(record, event)
            original_event(run, event)

        @wraps(original_execute)
        async def async_execute(run: Any, pipeline_input: Any, **kwargs: Any) -> Any:
            if (
                not self._enabled
                or run.hass is not self.hass
                or (self.pipeline_ids and run.pipeline.id not in self.pipeline_ids)
            ):
                return await original_execute(run, pipeline_input, **kwargs)
            record = RunRecord(
                {
                    "run_id": run.id,
                    "pipeline_id": run.pipeline.id,
                    "pipeline_name": run.pipeline.name,
                    "language": run.language,
                    "device_id": pipeline_input.device_id,
                    "satellite_id": pipeline_input.satellite_id,
                    "agent_id": run.pipeline.conversation_engine,
                    "source": "speech"
                    if pipeline_input.stt_stream is not None
                    else "text",
                },
                text_input=pipeline_input.intent_input,
            )
            self._runs[run.id] = record
            token = _CURRENT_RUN.set(record)
            try:
                return await original_execute(run, pipeline_input, **kwargs)
            except asyncio.CancelledError:
                record.errors.append({"stage": "pipeline", "code": "cancelled"})
                raise
            except Exception as err:
                record.errors.append(
                    {
                        "stage": "pipeline",
                        "code": "unhandled_exception",
                        "exception_type": type(err).__name__,
                    }
                )
                raise
            finally:
                _CURRENT_RUN.reset(token)
                self._runs.pop(run.id, None)
                if self._enabled and not record.finished:
                    record.finished = True
                    try:
                        self._deliver(
                            record.snapshot("run-end", datetime.now(UTC).isoformat())
                        )
                    except Exception:
                        _LOGGER.exception("Unable to finalize an Intent Logger record")

        @wraps(original_intent)
        async def async_handle(*args: Any, **kwargs: Any) -> Any:
            record = _CURRENT_RUN.get() if self._enabled else None
            if record is None or record.finished:
                return await original_intent(*args, **kwargs)
            details = None
            try:
                bound = intent_signature.bind(*args, **kwargs)
                if bound.arguments["hass"] is self.hass:
                    details = {
                        "name": bound.arguments["intent_type"],
                        "slots": deepcopy(bound.arguments.get("slots") or {}),
                    }
                    record.intents.append(details)
            except Exception:
                _LOGGER.exception("Unable to capture intent name or slots")
            if details is None:
                return await original_intent(*args, **kwargs)
            try:
                result = await original_intent(*args, **kwargs)
            except self.intent_module.IntentError as err:
                details["error"] = {"type": type(err).__name__, "message": str(err)}
                raise
            return result

        self._enabled = True
        for owner, name, replacement in (
            (self.run_class, "process_event", process_event),
            (self.run_class, "async_execute", async_execute),
            (self.intent_module, "async_handle", async_handle),
        ):
            original = getattr(owner, name)
            replacement._intent_logger_hook = True
            self._originals.append((owner, name, original, replacement))
            setattr(owner, name, replacement)

    def uninstall(self) -> None:
        """Restore only our own hooks; never overwrite another integration."""
        self._enabled = False
        for owner, name, original, replacement in reversed(self._originals):
            if getattr(owner, name) is replacement:
                setattr(owner, name, original)
            else:
                _LOGGER.warning(
                    "Another integration replaced %s; our chained hook is disabled",
                    name,
                )
        self._originals.clear()
        self._runs.clear()

    def _observe(self, record: RunRecord, event: Any) -> None:
        # Finalize in async_execute's finally, after any exception or cancellation.
        if str(event.type) == "run-end":
            return
        try:
            snapshot = record.event(str(event.type), event.data or {}, event.timestamp)
            if snapshot is not None:
                self._deliver(snapshot)
        except Exception:
            # This boundary must not turn a logging defect into an Assist failure.
            _LOGGER.exception("Unable to capture an Assist pipeline event")

    def _deliver(self, snapshot: dict[str, Any]) -> None:
        try:
            self.emit(snapshot)
        except Exception:
            _LOGGER.exception("Unable to deliver an Intent Logger record")
        if snapshot["event_type"] == "run-end" and snapshot["errors"]:
            try:
                self.failed(snapshot)
            except Exception:
                _LOGGER.exception("Unable to fire an Intent Logger failure event")
