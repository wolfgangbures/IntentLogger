"""API-level tests with real Core classes; no live STT or LLM service needed."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

if sys.platform == "win32":
    pytest.skip("Home Assistant runtime requires Linux", allow_module_level=True)

pytest.importorskip("homeassistant")

from homeassistant.components.assist_pipeline import (  # noqa: E402
    AudioSettings,
    Pipeline,
    PipelineEvent,
    PipelineEventType,
    PipelineRun,
    PipelineStage,
)
from homeassistant.components.assist_pipeline.const import DATA_CONFIG  # noqa: E402
from homeassistant.components.assist_pipeline.run import PipelineInput  # noqa: E402
from homeassistant.components.assist_pipeline.runtime import (  # noqa: E402
    KEY_ASSIST_PIPELINE,
    PipelineData,
)
from homeassistant.components.conversation import ConversationResult  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady  # noqa: E402
from homeassistant.helpers import intent  # noqa: E402

from custom_components.intent_logger import (  # noqa: E402
    Runtime,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.intent_logger.config_flow import (  # noqa: E402
    IntentLoggerConfigFlow,
    IntentLoggerOptionsFlow,
    _schema,
)
from custom_components.intent_logger.const import DEFAULTS, EVENT_FAILED  # noqa: E402


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.data[DATA_CONFIG] = {}
    instance.data[KEY_ASSIST_PIPELINE] = PipelineData(Mock(data={}))
    yield instance
    await instance.async_block_till_done()
    await instance.async_stop()


def make_run(hass, callback, speech):
    pipeline = Pipeline(
        conversation_engine="homeassistant",
        conversation_language="en",
        language="en",
        name="API test",
        stt_engine=None,
        stt_language=None,
        tts_engine=None,
        tts_language=None,
        tts_voice=None,
        wake_word_entity=None,
        wake_word_id=None,
        id="pipeline-one",
    )
    run = PipelineRun(
        hass=hass,
        context=Context(),
        pipeline=pipeline,
        start_stage=PipelineStage.STT if speech else PipelineStage.INTENT,
        end_stage=PipelineStage.INTENT,
        event_callback=callback,
        audio_settings=AudioSettings(is_vad_enabled=False),
    )
    return PipelineInput(
        run=run,
        session=SimpleNamespace(conversation_id="conversation-one"),
        intent_input=None if speech else "turn on the light",
        stt_stream=Mock() if speech else None,
    )


class Processor:
    """Replace only the external speech/agent work, not the Core run controller."""

    response_audio = None
    supports_streaming_response = False

    def __init__(self, run, failing=False):
        self.run = run
        self.failing = failing

    async def async_execute(self, request):
        if request.stt_stream is not None:
            self.run.process_event(
                PipelineEvent(
                    PipelineEventType.STT_END,
                    {"stt_output": {"text": "turn on the light"}},
                )
            )
        self.run.process_event(
            PipelineEvent(
                PipelineEventType.INTENT_START,
                {"engine": "homeassistant", "intent_input": "turn on the light"},
            )
        )
        if self.failing:
            response = intent.IntentResponse(language="en")
            response.async_set_error(
                intent.IntentResponseErrorCode.NO_INTENT_MATCH, "No matching intent"
            )
        else:
            response = await intent.async_handle(
                self.run.hass,
                "conversation",
                "LoggerTest",
                slots={"name": {"value": "light"}},
                language="en",
            )
        self.run.process_event(
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {
                    "processed_locally": True,
                    "intent_output": ConversationResult(
                        response=response, conversation_id="conversation-one"
                    ).as_dict(),
                },
            )
        )

    def cleanup(self):
        pass


class Handler(intent.IntentHandler):
    intent_type = "LoggerTest"

    async def async_handle(self, intent_obj):
        response = intent_obj.create_response()
        response.async_set_speech("Done")
        return response


@pytest.mark.parametrize("speech", [True, False])
@pytest.mark.parametrize("failing", [True, False])
async def test_real_core_run_serializes_and_preserves_client_events(
    hass, speech, failing
):
    runtime = Runtime(hass, DEFAULTS)
    await hass.async_add_executor_job(runtime.store.prepare)
    runtime.observer.install()
    runtime.worker = asyncio.create_task(runtime.write_records())
    events, failures = [], []
    unsubscribe = hass.bus.async_listen(
        EVENT_FAILED, lambda event: failures.append(event.data)
    )
    intent.async_register(hass, Handler())
    try:
        pipeline_input = make_run(hass, events.append, speech)
        pipeline_input.run._processor = Processor(pipeline_input.run, failing)
        await pipeline_input.execute()
        await hass.async_block_till_done()
    finally:
        await runtime.stop()
        unsubscribe()
    records = [
        json.loads(line)
        for path in runtime.store.directory.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    summary = records[-1]
    assert summary["event_type"] == "run-end"
    assert summary["status"] == ("failed" if failing else "success")
    assert summary["stt_text"] == ("turn on the light" if speech else None)
    assert summary["text_input"] == "turn on the light"
    assert [event.type for event in events][-1] == PipelineEventType.RUN_END
    assert len(failures) == int(failing)
    if not failing:
        assert summary["intents"][0]["name"] == "LoggerTest"
    assert not hass.data[KEY_ASSIST_PIPELINE].pipeline_runs._pipeline_runs[
        "pipeline-one"
    ]


async def test_writer_reports_disk_failure_and_flushes_remaining_records(hass, caplog):
    runtime = Runtime(hass, DEFAULTS)
    runtime.worker = asyncio.create_task(runtime.write_records())
    with patch.object(
        runtime.store, "append", side_effect=[OSError("disk full"), None]
    ):
        runtime.enqueue({"run_id": "one"})
        runtime.enqueue({"run_id": "two"})
        await runtime.stop()
    assert runtime.queue.empty()
    assert "Unable to write Intent Logger record" in caplog.text


async def test_queue_overflow_and_failure_event_option(hass, caplog):
    runtime = Runtime(hass, {**DEFAULTS, "failure_events": False})
    runtime.queue = asyncio.Queue(1)
    runtime.enqueue({"run_id": "one"})
    runtime.enqueue({"run_id": "two"})
    assert "backlog is full" in caplog.text
    events = []
    unsubscribe = hass.bus.async_listen(EVENT_FAILED, events.append)
    try:
        runtime.fire_failure({"run_id": "one"})
        await hass.async_block_till_done()
        assert not events
    finally:
        unsubscribe()


async def test_setup_and_unload_restore_hooks_and_flush(hass):
    entry = Mock(data={}, options={})
    original = PipelineRun.process_event
    assert await async_setup_entry(hass, entry)
    assert PipelineRun.process_event is not original
    entry.runtime_data.enqueue({"run_id": "queued"})
    assert await async_unload_entry(hass, entry)
    assert PipelineRun.process_event is original
    assert entry.runtime_data.worker.done()


@pytest.mark.parametrize(
    ("target", "error", "expected"),
    [
        ("prepare", OSError("permission"), ConfigEntryNotReady),
        ("install", RuntimeError("unsupported"), ConfigEntryError),
    ],
)
async def test_setup_errors_surface_without_starting_worker(
    hass, target, error, expected
):
    owner = (
        "custom_components.intent_logger.LogStore.prepare"
        if target == "prepare"
        else "custom_components.intent_logger.PipelineObserver.install"
    )
    with patch(owner, side_effect=error), pytest.raises(expected):
        await async_setup_entry(hass, Mock(data={}, options={}))


async def test_config_schema_enforces_bounds_and_flow_options(hass):
    schema = _schema(hass, DEFAULTS)
    assert schema({}) == DEFAULTS
    with pytest.raises(Exception, match="365"):
        schema({**DEFAULTS, "retention_days": 366})
    flow = IntentLoggerConfigFlow()
    flow.hass = hass
    with (
        patch.object(flow, "async_set_unique_id"),
        patch.object(flow, "_abort_if_unique_id_configured"),
    ):
        result = await flow.async_step_user(DEFAULTS)
    assert result["data"] == DEFAULTS
    assert isinstance(flow.async_get_options_flow(Mock()), IntentLoggerOptionsFlow)
