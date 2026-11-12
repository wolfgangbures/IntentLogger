"""Transcript/result correlation and failure classification."""

from intent_logger_units.capture import RunRecord


def test_duplicate_wake_up_is_logged_but_ignored_for_failure_by_default():
    record = RunRecord(
        {"run_id": "duplicate"},
        ignored_error_codes=frozenset({"duplicate_wake_up_detected"}),
    )
    error = {
        "stage": "pipeline",
        "code": "duplicate_wake_up_detected",
        "message": "A second assistant heard the wake word",
    }
    intermediate = record.event("error", error, "error-time")
    summary = record.event("run-end", {}, "end-time")
    assert intermediate["status"] == "pending"
    assert summary["status"] == "ignored"
    assert summary["errors"] == [error]
    assert summary["ignored_errors"] == [error]


def test_duplicate_wake_up_remains_a_failure_when_not_ignored():
    record = RunRecord({"run_id": "duplicate"})
    record.event("error", {"code": "duplicate_wake_up_detected"}, "error-time")
    summary = record.event("run-end", {}, "end-time")
    assert summary["status"] == "failed"
    assert summary["ignored_errors"] == []


def test_ignoring_one_error_does_not_hide_other_pipeline_errors():
    record = RunRecord(
        {},
        errors=[
            {"code": "duplicate_wake_up_detected"},
            {"code": "stt-stream-failed"},
        ],
        ignored_error_codes=frozenset({"duplicate_wake_up_detected"}),
    )
    assert record.snapshot("run-end", "time")["status"] == "failed"


def test_speech_and_structured_result_are_correlated_and_snapshotted():
    record = RunRecord({"run_id": "run-one"})
    transcript = record.event(
        "stt-end", {"stt_output": {"text": "turn on the light"}}, "time-one"
    )
    record.event(
        "intent-start",
        {"intent_input": "turn on the light", "engine": "homeassistant"},
        "time-two",
    )
    output = {"response": {"response_type": "action_done", "data": {}}}
    result = record.event("intent-end", {"intent_output": output}, "time-three")
    assert transcript["stt_text"] == result["text_input"] == "turn on the light"
    assert transcript["intent_output"] is None
    assert result["run_id"] == transcript["run_id"] == "run-one"
    assert result["status"] == "success"
    output["response"]["data"]["changed"] = True
    assert "changed" not in result["intent_output"]["response"]["data"]


def test_unknown_intent_is_failure_not_a_successful_pipeline():
    record = RunRecord({"run_id": "unknown"}, text_input="nonsense")
    record.event(
        "intent-end",
        {
            "intent_output": {
                "response": {
                    "response_type": "error",
                    "data": {"code": "no_intent_match"},
                }
            }
        },
        "time",
    )
    summary = record.event("run-end", {}, "end")
    assert summary["status"] == "failed"
    assert summary["errors"] == [{"stage": "intent", "code": "no_intent_match"}]
    assert summary["stt_text"] is None
    assert summary["text_input"] == "nonsense"


def test_partial_action_failure_is_reported():
    record = RunRecord({})
    result = record.event(
        "intent-end",
        {
            "intent_output": {
                "response": {
                    "response_type": "action_done",
                    "data": {"failed": [{"id": "light.office"}]},
                }
            }
        },
        "time",
    )
    assert result["errors"][0]["code"] == "partial_failure"


def test_stt_failure_retains_error_without_inventing_a_transcript():
    record = RunRecord({"run_id": "no-speech"})
    error = record.event("error", {"code": "stt-no-text-recognized"}, "time")
    assert error["stt_text"] is None
    assert error["intent_output"] is None
    assert error["status"] == "failed"


def test_llm_prose_is_not_heuristically_classified_as_failure():
    record = RunRecord({})
    result = record.event(
        "intent-end",
        {
            "intent_output": {
                "response": {
                    "response_type": "action_done",
                    "speech": {"plain": {"speech": "Sorry, I cannot do that."}},
                }
            }
        },
        "time",
    )
    assert result["status"] == "success"


def test_stt_only_run_is_incomplete_not_an_intent_success():
    record = RunRecord({})
    record.event("stt-end", {"stt_output": {"text": "hello"}}, "time")
    summary = record.event("run-end", {}, "end")
    assert summary["status"] == "incomplete"
    assert summary["stt_text"] == "hello"
