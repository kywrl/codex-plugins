import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metrics import analyze, display, enrich, hook_output, read_jsonl


def line(ts, typ, payload):
    return {"timestamp": ts, "type": typ, "payload": payload}


def test_analyze_keeps_only_the_first_char_metric(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        line("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line("2026-01-01T00:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "t1", "time_to_first_token_ms": 250}),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    report = analyze(read_jsonl(path), "s1", "t1")
    assert set(report) == {"first_char_seconds", "output_tokens_per_second", "response_model"}
    assert report["first_char_seconds"] == 0.25
    assert report["output_tokens_per_second"] is None
    assert report["response_model"] is None


def test_analyze_falls_back_to_first_assistant_message_timestamp():
    report = analyze([
        line("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line("2026-01-01T00:00:01Z", "response_item", {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}],
        }),
    ], "s1", "t1")
    assert report["first_char_seconds"] == 1


def test_enrich_uses_response_model_and_output_speed():
    report = analyze([], "s1", "t1")
    result = enrich(report, [{
        "ttft_ms": 250,
        "generation_duration_ms": 1750,
        "output_tokens": 20,
        "response_model_id": "served-model",
    }])
    assert result["first_char_seconds"] == 0.25
    assert result["output_tokens_per_second"] == 20 / 1.75
    assert result["response_model"] == "served-model"


def test_display_is_exactly_one_line_with_three_metrics():
    output = display({
        "first_char_seconds": 0.25,
        "output_tokens_per_second": 11.428571,
        "response_model": "served-model",
    })
    assert output == "[CodexToolkit] 首字：0.25 s ｜输出速度：11.43 tok/s | 响应模型：served-model"
    assert "\n" not in output


def test_display_marks_missing_metrics_without_extra_output():
    output = display(analyze([], "s1", "t1"))
    assert output == "[CodexToolkit] 首字：不可用 ｜输出速度：不可用 | 响应模型：不可用"
    assert "\n" not in output


def test_stop_and_interrupt_keep_hook_protocol():
    stop = hook_output({"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1"})
    assert set(stop) == {"continue", "systemMessage"}
    assert stop["continue"] is True
    assert "\n" not in stop["systemMessage"]

    interrupt = hook_output({"hook_event_name": "Interrupt", "session_id": "s1", "turn_id": "t1"})
    assert set(interrupt) == {"systemMessage"}
    assert "\n" not in interrupt["systemMessage"]
