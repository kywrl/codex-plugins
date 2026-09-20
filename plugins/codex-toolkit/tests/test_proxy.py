import json
import sys
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from proxy import Capture, SSEParser, take_records, remember, serve
from metrics import proxy_records


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


def test_proxy_records_are_in_memory_only():
    server = serve("http://127.0.0.1:9000", 0)
    try:
        capture = Capture("requested", "s1", "t1")
        capture.event({"type": "response.created", "response": {"id": "r1", "model": "served"}})
        remember(server, capture)
        records = take_records(server, "s1", "t1")
        assert len(records) == 1
        assert records[0]["response_model_id"] == "served"
        # 领取之后删除；网络转发 finally 再执行也不会恢复已消费的记录。
        remember(server, capture)
        assert take_records(server, "s1", "t1") == []
    finally:
        server.server_close()


def test_hook_consumes_only_its_turn_over_http():
    server = serve("http://127.0.0.1:9000", 0)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        for session, turn in [("s1", "t1"), ("s1", "t2"), ("s2", "t1")]:
            remember(server, Capture("requested", session, turn))
        endpoint = f"http://127.0.0.1:{server.server_port}"
        records, error = proxy_records(endpoint, "s1", "t1")
        assert error is None
        assert [(r["session_id"], r["turn_id"]) for r in records] == [("s1", "t1")]
        assert proxy_records(endpoint, "s1", "t1") == ([], None)
        assert len(take_records(server, "s1", "t2")) == 1
        assert len(take_records(server, "s2", "t1")) == 1
        remember(server, Capture("unassociated"))
        assert not server.records
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
