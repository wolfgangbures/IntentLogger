"""Correlate the JSON-compatible portions of Assist pipeline events."""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunRecord:
    """One pipeline execution, independent of other simultaneous runs."""

    metadata: dict[str, Any]
    stt_text: str | None = None
    text_input: str | None = None
    intent_output: dict[str, Any] | None = None
    intents: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    ignored_error_codes: frozenset[str] = field(default_factory=frozenset)

    def event(
        self, event_type: str, data: dict[str, Any], timestamp: str
    ) -> dict[str, Any] | None:
        """Return a snapshot for a transcript, result, error, or final summary."""
        if event_type == "run-start":
            self.metadata["conversation_id"] = data.get("conversation_id")
        elif event_type == "stt-end":
            self.stt_text = data["stt_output"]["text"]
        elif event_type == "intent-start":
            self.text_input = data["intent_input"]
            self.metadata["conversation_id"] = data.get("conversation_id")
            self.metadata["agent_id"] = data.get("engine")
        elif event_type == "intent-end":
            self.intent_output = deepcopy(data["intent_output"])
            self.metadata["processed_locally"] = data.get("processed_locally")
            response = self.intent_output.get("response", {})
            if response.get("response_type") == "error":
                self.errors.append({"stage": "intent", **response.get("data", {})})
            elif response.get("data", {}).get("failed"):
                self.errors.append(
                    {
                        "stage": "intent",
                        "code": "partial_failure",
                        "failed": response["data"]["failed"],
                    }
                )
        elif event_type == "error":
            self.errors.append({"stage": "pipeline", **deepcopy(data)})
        elif event_type == "run-end":
            self.finished = True
        else:
            return None

        if event_type not in {"stt-end", "intent-end", "error", "run-end"}:
            return None
        return self.snapshot(event_type, timestamp)

    def snapshot(self, event_type: str, timestamp: str) -> dict[str, Any]:
        """Copy state so later events cannot mutate queued records."""
        ignored_errors = [
            error
            for error in self.errors
            if error.get("code") in self.ignored_error_codes
        ]
        if len(ignored_errors) < len(self.errors):
            status = "failed"
        elif self.intent_output is not None:
            status = "success"
        elif self.finished and ignored_errors:
            status = "ignored"
        elif self.finished:
            status = "incomplete"
        else:
            status = "pending"
        return deepcopy(
            {
                "schema_version": 2,
                **self.metadata,
                "timestamp": timestamp,
                "event_type": event_type,
                "stt_text": self.stt_text,
                "text_input": self.text_input,
                "intent_output": self.intent_output,
                "intents": self.intents,
                "errors": self.errors,
                "ignored_errors": ignored_errors,
                "status": status,
            }
        )
