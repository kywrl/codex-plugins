#!/usr/bin/env python3
"""可选 Responses HTTP/SSE 采集代理，只监听 127.0.0.1；不支持 WebSocket。"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
import time
import uuid
from urllib.parse import urlsplit

from metrics import connect, data_dir, divide, number, usage

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


def associate(db, headers):
    """显式关联头优先；native session_id 是待真实环境验证的兼容适配。"""
    session = headers.get("X-Codex-Metrics-Session") or headers.get("session_id")
    turn = headers.get("X-Codex-Metrics-Turn")
    if not turn and session:
        row = db.execute("SELECT turn FROM starts WHERE session=? ORDER BY started DESC LIMIT 1", (session,)).fetchone()
        if row:
            turn = row[0]
    return session, turn


def persist(root, capture):
    record = capture.record
    db = connect(root)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO requests VALUES(?,?,?,?)", (record["id"], record["session_id"], record["turn_id"], json.dumps(record, ensure_ascii=False)))
    finally:
        db.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 不记录带凭据的 URL、headers 或正文。

    def do_GET(self):
        self.send_error(405, "Only POST /responses is supported; disable WebSocket transport")

    def do_POST(self):
        if self.headers.get("Origin"):
            self.send_error(403, "Browser origins are not accepted")
            return
        if self.path not in ("/responses", "/v1/responses"):
            self.send_error(404, "Only Responses create is supported")
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
            if not isinstance(request, dict) or not isinstance(request.get("model"), str):
                raise ValueError()
            if request.get("stream") is not True:
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send_error(400, "A model and stream=true are required")
            return
        db = connect(self.server.data_root)
        try:
            session, turn = associate(db, self.headers)
        finally:
            db.close()
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
                        # 先写指标，再把结束事件交给 Codex，缩小 Stop 读取竞争窗口。
                        persist(self.server.data_root, capture)
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
            persist(self.server.data_root, capture)


def serve(root, upstream, port):
    target = urlsplit(upstream)
    if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password or target.query or target.fragment:
        raise ValueError("upstream 必须是不含凭据和 query 的 HTTP(S) base URL")
    if target.scheme == "http" and target.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("非本机 upstream 必须使用 HTTPS")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.data_root, server.upstream = root, target
    return server


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="例如 https://api.openai.com/v1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    server = serve(data_dir(args.data_dir), args.upstream, args.port)
    print(f"Responses 采集器：http://127.0.0.1:{server.server_port}/v1；Ctrl-C 停止", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
