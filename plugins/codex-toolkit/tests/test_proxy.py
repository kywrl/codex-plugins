import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from proxy import Capture, SSEParser


def test_sse_capture_uses_response_model_and_final_usage():
    capture = Capture("request-model", "s", "t")
    parser = SSEParser(capture)
    events = [
        {"type": "response.created", "response": {"id": "r1", "model": "actual-model"}},
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.completed", "response": {
            "id": "r1", "model": "actual-model",
            "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 25},
                       "output_tokens": 12, "output_tokens_details": {"reasoning_tokens": 2}},
        }},
    ]
    wire = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    for offset in range(0, len(wire), 5):
        parser.feed(wire[offset:offset + 5])
    parser.finish()
    assert capture.record["request_model_id"] == "request-model"
    assert capture.record["response_model_id"] == "actual-model"
    assert capture.record["cache_hit_rate"] == 0.25
    assert capture.record["non_reasoning_output_tokens"] == 10
    assert capture.record["status"] == "completed"


def test_capture_keeps_request_and_response_model_sources():
    capture = Capture("requested-model", "s", "t")
    capture.event({"type": "response.created", "response": {"id": "r2", "model": "served-model"}})
    assert capture.record["request_model_id"] == "requested-model"
    assert capture.record["response_model_id"] == "served-model"
    assert capture.record["request_model_source"] == "request.body.model"
    assert capture.record["response_model_source"] == "response.model"
