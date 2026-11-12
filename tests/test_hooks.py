"""Exercise the verified upstream hook signatures, including concurrent runs."""

import asyncio
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest
from intent_logger_units.hooks import PipelineObserver


@dataclass
class Event:
    type: str
    data: dict | None = None
    timestamp: str = "timestamp"


@pytest.fixture
def rig():
    hass = object()
    intent_module = ModuleType("fake_intent")

    class IntentError(Exception):
        pass

    async def async_handle(
        hass, platform, intent_type, slots=None, text_input=None, **kwargs
    ):
        await asyncio.sleep(0)
        if intent_type == "fail":
            raise IntentError("cannot handle")
        return "intent-result"

    intent_module.async_handle = async_handle
    intent_module.IntentError = IntentError

    class Run:
        def __init__(self, run_id, pipeline_id="pipeline", owner=hass):
            self.hass = owner
            self.id = run_id
            self.pipeline = SimpleNamespace(
                id=pipeline_id, name="Test", conversation_engine="homeassistant"
            )
            self.language = "en"
            self.seen = []

        def process_event(self, event):
            self.seen.append(event.type)

        async def async_execute(self, pipeline_input, *, validate=False):
            try:
                self.process_event(Event("run-start", {"conversation_id": self.id}))
                if pipeline_input.stt_stream is not None:
                    self.process_event(
                        Event("stt-end", {"stt_output": {"text": self.id}})
                    )
                self.process_event(
                    Event("intent-start", {"intent_input": self.id, "engine": "local"})
                )
                if self.id == "cancel":
                    raise asyncio.CancelledError
                if self.id == "explode":
                    raise RuntimeError("upstream failure")
                try:
                    await intent_module.async_handle(
                        self.hass,
                        "conversation",
                        "fail" if self.id == "fail" else "HassTurnOn",
                        {"name": {"value": self.id}},
                    )
                    response = {"response_type": "action_done", "data": {}}
                except IntentError:
                    response = {
                        "response_type": "error",
                        "data": {"code": "failed_to_handle"},
                    }
                self.process_event(
                    Event("intent-end", {"intent_output": {"response": response}})
                )
                return "upstream-result"
            finally:
                self.process_event(Event("run-end"))

    emitted, failed = [], []
    observer = PipelineObserver(
        hass, Run, intent_module, [], emitted.append, failed.append
    )
    originals = (Run.process_event, Run.async_execute, intent_module.async_handle)
    observer.install()
    yield SimpleNamespace(
        hass=hass,
        Run=Run,
        intent=intent_module,
        observer=observer,
        emitted=emitted,
        failed=failed,
        originals=originals,
    )
    observer.uninstall()


def pipeline_input(speech=True):
    return SimpleNamespace(
        device_id="device",
        satellite_id="assist_satellite.test",
        stt_stream=object() if speech else None,
        intent_input=None if speech else "typed",
    )


async def test_concurrent_runs_keep_transcripts_intents_and_results_separate(rig):
    results = await asyncio.gather(
        rig.Run("one").async_execute(pipeline_input(), validate=True),
        rig.Run("two").async_execute(pipeline_input()),
    )
    assert results == ["upstream-result", "upstream-result"]
    summaries = [r for r in rig.emitted if r["event_type"] == "run-end"]
    assert len(summaries) == 2
    for record in summaries:
        assert record["run_id"] == record["stt_text"] == record["text_input"]
        assert record["intents"] == [
            {"name": "HassTurnOn", "slots": {"name": {"value": record["run_id"]}}}
        ]
        assert record["status"] == "success"
    assert not rig.failed


async def test_failure_fires_once_and_preserves_input_and_intent_error(rig):
    run = rig.Run("fail")
    assert await run.async_execute(pipeline_input(False)) == "upstream-result"
    assert len(rig.failed) == 1
    assert rig.failed[0]["text_input"] == "fail"
    assert rig.failed[0]["source"] == "text"
    assert rig.failed[0]["intents"][0]["error"]["type"] == "IntentError"
    assert run.seen[-1] == "run-end"


@pytest.mark.parametrize(
    ("name", "exception", "code"),
    [
        ("cancel", asyncio.CancelledError, "cancelled"),
        ("explode", RuntimeError, "unhandled_exception"),
    ],
)
async def test_exceptions_propagate_but_transcripts_are_finalized(
    rig, name, exception, code
):
    with pytest.raises(exception):
        await rig.Run(name).async_execute(pipeline_input())
    assert len(rig.failed) == 1
    assert rig.failed[0]["errors"][-1]["code"] == code
    assert rig.failed[0]["stt_text"] == name
    assert not rig.observer._runs


async def test_filter_and_other_hass_instances_do_not_capture(rig):
    rig.observer.pipeline_ids = {"selected"}
    for run in (rig.Run("excluded"), rig.Run("other", "selected", object())):
        assert await run.async_execute(pipeline_input()) == "upstream-result"
    assert rig.emitted == rig.failed == []


async def test_observer_failure_never_changes_upstream_result(rig, caplog):
    def broken(record):
        raise OSError("disk unavailable")

    rig.observer.emit = broken
    assert await rig.Run("one").async_execute(pipeline_input()) == "upstream-result"
    assert "Unable to deliver" in caplog.text


async def test_unload_restores_original_methods(rig):
    rig.observer.uninstall()
    assert (
        rig.Run.process_event,
        rig.Run.async_execute,
        rig.intent.async_handle,
    ) == rig.originals
    await rig.Run("one").async_execute(pipeline_input())
    assert not rig.emitted


def test_signature_incompatibility_fails_before_installing(rig):
    rig.observer.uninstall()
    rig.Run.process_event = lambda self, changed: None
    with pytest.raises(RuntimeError, match="process_event"):
        rig.observer.install()
    assert rig.Run.async_execute is rig.originals[1]


async def test_non_pipeline_intent_calls_are_not_captured(rig):
    assert (
        await rig.intent.async_handle(rig.hass, "conversation", "HassTurnOn")
        == "intent-result"
    )
    assert not rig.emitted
