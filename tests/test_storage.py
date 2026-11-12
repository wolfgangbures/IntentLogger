"""Actual disk writes, rotation, and retention boundaries."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from intent_logger_units.storage import LogStore


def test_jsonl_escapes_newlines_and_roundtrips_unicode(tmp_path):
    store = LogStore(tmp_path, 30, 1, 3)
    store.prepare()
    store.append({"text": "hello\nGr\u00fc\u00dfe", "run_id": "one"})
    lines = next(tmp_path.glob("*.jsonl")).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "hello\nGr\u00fc\u00dfe"


def test_rotation_keeps_newest_segments_under_both_limits(tmp_path):
    store = LogStore(tmp_path, 30, 1, 2)
    store.prepare()
    for index in range(4):
        store.append({"text": "x" * 600_000, "index": index})
    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == 2
    assert all(path.stat().st_size <= 1024 * 1024 for path in files)
    assert [json.loads(path.read_text())["index"] for path in files] == [2, 3]


def test_expiration_uses_creation_not_recent_append_time(tmp_path):
    old = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y%m%dT%H%M%S%f")
    expired = tmp_path / f"intent_logger-{old}-{'a' * 32}.jsonl"
    expired.write_text("recently touched")
    unrelated = tmp_path / "other.jsonl"
    unrelated.write_text("keep")
    store = LogStore(tmp_path, 30, 1, 2)
    store.prepare()
    assert not expired.exists()
    assert unrelated.exists()
    store.append({"text": "fresh"})
    assert len(list(tmp_path.glob("intent_logger-*.jsonl"))) == 1


def test_oversized_record_raises_instead_of_exceeding_limit(tmp_path):
    store = LogStore(tmp_path, 30, 1, 2)
    store.prepare()
    with pytest.raises(ValueError, match="exceeds"):
        store.append({"text": "x" * 1_048_576})
    assert not list(tmp_path.iterdir())
