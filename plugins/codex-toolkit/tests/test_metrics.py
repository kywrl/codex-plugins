import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metrics import MAX_FOOTER_BYTES, analyze, compare_model_ids, display, enrich, hook_output, read_jsonl


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


def test_enrich_adds_per_request_comparisons_and_warning():
    report = analyze([], "s1", "t1")
    result = enrich(report, [{
        "id": "a", "response_id": "r1", "request_model_id": "gpt-a",
        "response_model_id": "gpt-served", "status": "completed",
    }])
    assert result["model_id_mismatch"]["value"] is True
    assert result["model_id_comparisons"][0]["match"] is False
    assert any("标识差异" in warning for warning in result["warnings"])


def test_display_renders_boolean_without_numeric_formatting():
    report = analyze([], "s1", "t1")
    report["model_id_mismatch"]["value"] = False
    assert "模型标识不一致：否" in display(report)


@pytest.mark.parametrize("stop_hook_active", [False, True])
def test_stop_hook_stdout_displays_summary_without_continuation_or_writes(tmp_path, stop_hook_active):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        line("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line("2026-01-01T00:00:00Z", "turn_context", {"turn_id": "t1", "model": "selected-model"}),
        line("2026-01-01T00:00:01Z", "token_usage_record", {"thread_id": "s1", "turn_id": "t1", "turn_token_usage": {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 20}}),
        line("2026-01-01T00:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 2000, "time_to_first_token_ms": 250}),
    ]
    transcript.write_text("\n".join(map(json.dumps, rows)) + "\n")
    before = transcript.read_bytes()
    # 运行真正的 CLI，拒绝文件写入、数据库连接和网络连接。
    runner = '''
import os, runpy, sys
script = sys.argv[1]
def forbid_writes(event, args):
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        raise RuntimeError("file write forbidden")
    if event in ("os.mkdir", "os.remove", "os.rename", "sqlite3.connect"):
        raise RuntimeError("persistence forbidden")
    if event == "socket.connect":
        raise RuntimeError("network forbidden")
sys.addaudithook(forbid_writes)
sys.argv = [script, "hook"]
runpy.run_path(script, run_name="__main__")
'''
    event = {"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1", "transcript_path": str(transcript), "stop_hook_active": stop_hook_active}
    env = dict(os.environ)
    env.pop("CODEX_METRICS_PROXY", None)
    process = subprocess.run([sys.executable, "-B", "-c", runner, str(ROOT / "scripts/metrics.py")], input=json.dumps(event), text=True, capture_output=True, cwd=tmp_path, env=env, check=True)
    output = json.loads(process.stdout)
    assert set(output) == {"systemMessage"}
    assert "250.00 ms" in output["systemMessage"]
    assert "25.00%" in output["systemMessage"]
    assert "10.00 token/s" in output["systemMessage"]
    assert "selected-model" in output["systemMessage"]
    assert process.stderr == ""
    assert list(tmp_path.iterdir()) == [transcript]
    assert transcript.read_bytes() == before


def test_interrupt_displays_notice_without_continuation(monkeypatch):
    monkeypatch.delenv("CODEX_METRICS_PROXY", raising=False)
    output = hook_output({"hook_event_name": "Interrupt", "session_id": "s1", "turn_id": "t1"})
    assert set(output) == {"systemMessage"}
    assert "不可用" in output["systemMessage"]


def test_long_footer_is_bounded_to_avoid_hook_output_spill():
    report = analyze([], "s1", "t1")
    report["warnings"] = ["warning-" + "x" * 500 for _ in range(100)]
    footer = display(report)
    assert len(footer.encode("utf-8")) <= MAX_FOOTER_BYTES
    assert "截断" in footer
