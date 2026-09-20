#!/usr/bin/env python3
"""可选 Responses HTTP/SSE 采集代理，只监听 127.0.0.1；不支持 WebSocket。"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import sys
from threading import Lock
import time
import uuid
from urllib.parse import urlsplit

# 直接运行代理也不会因导入 metrics 而生成 __pycache__。
sys.dont_write_bytecode = True
from metrics import divide, usage

MAX_BODY = 32 * 1024 * 1024
MAX_EVENT = 8 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
OUTPUT_DELTAS = {"response.output_text.delta", "response.refusal.delta",
                 "response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}
REASONING_DELTAS = {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}


class Capture:
    def __init__(self, model, session=None, turn=None):
        self.started = time.perf_counter()
        self.first_output = None
        self.published = False
        self.record = {
            "id": str(uuid.uuid4()), "session_id": session, "turn_id": turn,
            "request_model_id": model, "response_model_id": None,
            "status": "streaming", "ttft_ms": None, "first_text_ms": None,
            "request_duration_ms": None, "generation_duration_ms": None,
            "output_tokens": None, "non_reasoning_output_tokens": None,
            "generation_tokens_per_second": None,
            "cache_hit_rate": None, "usage": None,
            "request_model_source": "request.body.model",
            "response_model_source": "response.model",
        }

    def event(self, event, elapsed_ms=None):
        if not isinstance(event, dict):
            return
        elapsed = elapsed_ms if elapsed_ms is not None else (time.perf_counter() - self.started) * 1000
        kind = event.get("type")
        if kind in OUTPUT_DELTAS | REASONING_DELTAS and isinstance(event.get("delta"), str) and event["delta"]:
            if self.record["ttft_ms"] is None:
                self.record["ttft_ms"] = elapsed
            if kind in OUTPUT_DELTAS and self.first_output is None:
                self.first_output = elapsed
            if kind in ("response.output_text.delta", "response.refusal.delta") and self.record["first_text_ms"] is None:
                self.record["first_text_ms"] = elapsed
        response = event.get("response") or {}
        if not isinstance(response, dict):
            response = {}
        response_model = response.get("model") if isinstance(response.get("model"), str) else event.get("model")
        if isinstance(response_model, str):
            self.record["response_model_id"] = response_model
        if isinstance(response.get("id"), str):
            self.record["response_id"] = response["id"]
        if kind in ("response.completed", "response.failed", "response.incomplete", "error"):
            self.record["status"] = {"response.completed": "completed", "response.incomplete": "incomplete"}.get(kind, "failed")
            self.record["request_duration_ms"] = elapsed
            tokens = usage(response.get("usage"))
            self.record["usage"] = tokens
            self.record["output_tokens"] = tokens["output_tokens"]
            self.record["cache_hit_rate"] = divide(tokens["cached_input_tokens"], tokens["input_tokens"])
            total, reasoning = tokens["output_tokens"], tokens["reasoning_output_tokens"]
            visible = total - reasoning if total is not None and reasoning is not None and total >= reasoning else None
            self.record["non_reasoning_output_tokens"] = visible
            duration = elapsed - self.first_output if self.first_output is not None else None
            if duration is not None and duration > 0:
                self.record["generation_duration_ms"] = duration
                self.record["generation_tokens_per_second"] = divide(visible, duration / 1000)

    def finish(self):
        if self.record["status"] == "streaming":
            self.record["status"] = "disconnected"
            self.record["request_duration_ms"] = (time.perf_counter() - self.started) * 1000


class SSEParser:
    """跨网络块解析 SSE；保持原始字节原样转发，不把 delta 数量当 token 数。"""
    def __init__(self, capture):
        self.capture = capture
        self.buffer = b""
        self.lines = []
        self.size = 0

    def feed(self, chunk):
        self.buffer += chunk
        if len(self.buffer) + self.size > MAX_EVENT:
            raise ValueError("SSE event too large")
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if self.lines:
                    data = b"\n".join(self.lines)
                    if data != b"[DONE]":
                        try:
                            self.capture.event(json.loads(data))
                        except (ValueError, UnicodeError):
                            self.capture.record["parse_error"] = True
                self.lines, self.size = [], 0
            elif line.startswith(b"data:"):
                line = line[5:]
                if line.startswith(b" "):
                    line = line[1:]
                self.lines.append(line)
                self.size += len(line)

    def finish(self):
        """处理 EOF 前没有空行的最后一个 event。"""
        if self.lines:
            data = b"\n".join(self.lines)
            if data != b"[DONE]":
                try:
                    self.capture.event(json.loads(data))
                except (ValueError, UnicodeError):
                    self.capture.record["parse_error"] = True


def associate(headers):
    """只从请求头取得回合关联信息；采集器不保存回合状态。"""
    session = headers.get("X-Codex-Metrics-Session") or headers.get("session_id")
    turn = headers.get("X-Codex-Metrics-Turn")
    return session, turn


def remember(server, capture):
    """每个请求发布一次；hook 领取后不会被 finally 再次放回内存。"""
    record = dict(capture.record)
    if not record.get("session_id") or not record.get("turn_id"):
        return
    request_id = record["id"]
    with server.records_lock:
        if capture.published:
            # 结束事件或 HTTP 状态可能在首次发布后才确定；已被 hook 领取的记录不重新放回。
            if request_id in server.records:
                server.records[request_id] = record
            return
        capture.published = True
        server.records[request_id] = record
        while len(server.records) > server.max_records:
            server.records.popitem(last=False)


def take_records(server, session, turn):
    """只领取并移除指定回合记录，其他会话保持隔离。"""
    with server.records_lock:
        ids = [key for key, record in server.records.items() if record.get("session_id") == session and record.get("turn_id") == turn]
        return [server.records.pop(key) for key in ids]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 不记录带凭据的 URL、headers 或正文。

    def do_GET(self):
        self.send_error(405, "Use POST /responses or POST /metrics")

    def send_metrics(self, request):
        session, turn = request.get("session_id"), request.get("turn_id")
        if not isinstance(session, str) or not session or not isinstance(turn, str) or not turn:
            self.send_error(400, "session_id and turn_id are required")
            return
        body = json.dumps({"requests": take_records(self.server, session, turn)}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_POST(self):
        if self.headers.get("Origin"):
            self.send_error(403, "Browser origins are not accepted")
            return
        if self.path not in ("/responses", "/v1/responses", "/metrics"):
            self.send_error(404, "Unsupported endpoint")
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
            self.send_error(415, "Disable request compression; Content-Length JSON is required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            self.send_error(413, "Invalid request body length")
            return
        self.connection.settimeout(300)
        body = self.rfile.read(length)
        try:
            request = json.loads(body)
            if not isinstance(request, dict):
                raise ValueError()
            if self.path != "/metrics" and (not isinstance(request.get("model"), str) or request.get("stream") is not True):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send_error(400, "A model and stream=true are required")
            return
        if self.path == "/metrics":
            self.send_metrics(request)
            return
        session, turn = associate(self.headers)
        capture = Capture(request["model"], session, turn)
        target = self.server.upstream
        factory = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = factory(target.hostname, target.port, timeout=300)
        response_started = False
        try:
            excluded = HOP_HEADERS | {h.strip().lower() for h in self.headers.get("Connection", "").split(",")}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded and not k.lower().startswith("x-codex-metrics-")}
            headers["Accept-Encoding"] = "identity"
            conn.request("POST", target.path.rstrip("/") + "/responses", body=body, headers=headers)
            response = conn.getresponse()
            capture.record["http_status"] = response.status
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("Compressed responses are unsupported")
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            response_started = True
            parser = SSEParser(capture)
            is_sse = "text/event-stream" in response.getheader("Content-Type", "")
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                if is_sse:
                    try:
                        parser.feed(chunk)
                    except ValueError:
                        capture.record["parse_error"] = True
                        is_sse = False
                    if capture.record["status"] != "streaming":
                        # 先更新内存记录，再把结束事件交给 Codex，缩小 Stop 读取竞争窗口。
                        remember(self.server, capture)
                self.wfile.write(chunk)
                self.wfile.flush()
            if is_sse:
                parser.finish()
            if response.status >= 400:
                capture.record["status"] = "http_error"
        except (OSError, ValueError, http.client.HTTPException) as error:
            capture.record["transport_error"] = type(error).__name__
            if not response_started:
                self.send_error(502, "Upstream transport failed")
        finally:
            conn.close()
            capture.finish()
            remember(self.server, capture)


def serve(upstream, port):
    target = urlsplit(upstream)
    if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password or target.query or target.fragment:
        raise ValueError("upstream 必须是不含凭据和 query 的 HTTP(S) base URL")
    if target.scheme == "http" and target.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("非本机 upstream 必须使用 HTTPS")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.upstream = target
    server.records = OrderedDict()
    server.records_lock = Lock()
    server.max_records = 1000
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="例如 https://api.openai.com/v1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = serve(args.upstream, args.port)
    print(f"Responses 采集器：http://127.0.0.1:{server.server_port}/v1；数据只保存在内存，Ctrl-C 后丢弃", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
