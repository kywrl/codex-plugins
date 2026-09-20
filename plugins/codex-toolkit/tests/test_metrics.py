import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metrics import analyze, compare_model_ids, connect, enrich, read_jsonl


def line(ts, typ, payload):
    return {"timestamp": ts, "type": typ, "payload": payload}


def test_token_usage_record_and_ttft_are_selected(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        line("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line("2026-01-01T00:00:00Z", "turn_context", {"turn_id": "t1", "model": "gpt-test"}),
        {"timestamp": "2026-01-01T00:00:01Z", "type": "token_usage_record", "payload": {
            "session_id": "s1", "thread_id": "s1", "turn_id": "t1", "response_id": "r1",
            "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20},
            "turn_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20, "total_tokens": 120},
        }},
        line("2026-01-01T00:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 2000, "time_to_first_token_ms": 250}),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    report = analyze(read_jsonl(path), "s1", "t1")
    assert report["ttft_ms"]["value"] == 250
    assert report["tokens"]["output_tokens"] == 20
    assert report["cache_hit_rate"]["value"] == 0.8
    assert report["selected_model_ids"] == ["gpt-test"]


def test_old_cumulative_usage_without_baseline_is_null():
    rows = [
        line("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line("2026-01-01T00:00:01Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}}}),
        line("2026-01-01T00:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 1000}),
    ]
    report = analyze(rows, "s1", "t1")
    assert report["tokens"]["input_tokens"] is None
    assert any("基线" in warning for warning in report["warnings"])


def test_model_ids_are_compared_per_request_without_inference():
    comparisons, mismatch = compare_model_ids([
        {"id": "a", "response_id": "ra", "request_model_id": "gpt-a", "response_model_id": "gpt-a"},
        {"id": "b", "response_id": "rb", "request_model_id": "gpt-b", "response_model_id": "gpt-served"},
        {"id": "c", "response_id": "rc", "request_model_id": "gpt-c", "response_model_id": None},
    ])
    assert [item["match"] for item in comparisons] == [True, False, None]
    assert mismatch is True


def test_missing_model_side_is_unknown_not_mismatch():
    _, mismatch = compare_model_ids([{"id": "a", "request_model_id": "gpt-a"}])
    assert mismatch is None


def test_enrich_adds_per_request_comparisons_and_warning(tmp_path):
    db = connect(tmp_path)
    report = analyze([], "s1", "t1")
    with db:
        db.execute(
            "INSERT INTO requests VALUES(?,?,?,?)",
            ("a", "s1", "t1", json.dumps({
                "id": "a", "response_id": "r1", "request_model_id": "gpt-a",
                "response_model_id": "gpt-served", "status": "completed",
            })),
        )
    result = enrich(db, report)
    db.close()
    assert result["model_id_mismatch"]["value"] is True
    assert result["model_id_comparisons"][0]["match"] is False
    assert any("标识差异" in warning for warning in result["warnings"])
