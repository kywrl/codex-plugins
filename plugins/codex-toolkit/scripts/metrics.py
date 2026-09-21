#!/usr/bin/env python3
"""生成 Codex Toolkit 的三项回合统计；不记录提示词、消息正文或凭据。"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
from datetime import datetime
from pathlib import Path
import sys
from urllib.parse import urlsplit


def number(value):
    """返回非负有限数；其余值视为不可用。"""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return number(value)


def divide(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def read_jsonl(path):
    """只读取完整且合法的 JSONL 行。"""
    with Path(path).open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value
            except (ValueError, UnicodeError):
                continue


def empty_report():
    return {
        "first_char_seconds": None,
        "output_tokens_per_second": None,
        "response_model": None,
    }


def analyze(records, session_id, turn_id):
    """从 transcript 取得首字时间；输出速度和响应模型来自 SSE 采集器。"""
    started = None
    first_message = None
    native_first_char = None

    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = record.get("type")
        event = payload.get("type") if kind == "event_msg" else None
        at = timestamp(record.get("timestamp"))

        if event == "task_started" and payload.get("turn_id") == turn_id and at is not None:
            started = at if started is None else min(started, at)
        elif event == "task_complete" and payload.get("turn_id") == turn_id:
            native_first_char = number(payload.get("time_to_first_token_ms"))
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant" and at is not None:
            content = payload.get("content")
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "output_text" and item.get("text")
                for item in content
            ):
                first_message = at if first_message is None else min(first_message, at)

    first_char_seconds = native_first_char / 1000 if native_first_char is not None else None
    if first_char_seconds is None and started is not None and first_message is not None and first_message >= started:
        first_char_seconds = first_message - started

    report = empty_report()
    report["first_char_seconds"] = first_char_seconds
    return report


def proxy_records(endpoint, session, turn, timeout=0.25):
    """一次性领取本轮内存记录；领取后采集器删除记录，不提供历史查询。"""
    if not endpoint:
        return [], None
    connection = None
    try:
        target = urlsplit(endpoint)
        if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1") or target.username or target.password or target.query or target.fragment or target.path.rstrip("/") not in ("", "/v1"):
            return [], "CODEX_METRICS_PROXY 必须是无凭据、无 query 的本机 HTTP base URL"
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=timeout)
        connection.request("POST", "/metrics", json.dumps({"session_id": session, "turn_id": turn}), {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return [], f"采集器返回 HTTP {response.status}"
        payload = json.loads(response.read(2 * 1024 * 1024))
        records = payload.get("requests") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return [], "采集器返回的数据格式无效"
        return [item for item in records if isinstance(item, dict) and item.get("session_id") == session and item.get("turn_id") == turn], None
    except (http.client.HTTPException, OSError, ValueError, UnicodeError):
        return [], "无法读取 Responses 采集器"
    finally:
        if connection is not None:
            connection.close()


def enrich(report, requests):
    """合并采集器提供的首字、输出速度和 response.model。"""
    requests = [request for request in requests if isinstance(request, dict)]
    first_char = next((number(request.get("ttft_ms")) for request in requests if number(request.get("ttft_ms")) is not None), None)
    if first_char is not None:
        report["first_char_seconds"] = first_char / 1000

    output_tokens = [number(request.get("output_tokens")) for request in requests]
    durations = [number(request.get("generation_duration_ms")) for request in requests]
    if output_tokens and durations and len(output_tokens) == len(durations) and all(value is not None for value in output_tokens + durations):
        total_duration = sum(durations)
        if total_duration > 0:
            report["output_tokens_per_second"] = divide(sum(output_tokens), total_duration / 1000)

    for request in requests:
        model = request.get("response_model_id")
        if isinstance(model, str) and model:
            report["response_model"] = model
            break
    return report


def collect(session, turn, transcript, proxy=None):
    records = read_jsonl(transcript) if transcript and Path(transcript).is_file() else []
    report = analyze(records, session, turn)
    requests, _ = proxy_records(proxy, session, turn)
    return enrich(report, requests)


def handle_hook(event):
    name = event.get("hook_event_name")
    session = event.get("session_id")
    turn = event.get("turn_id")
    if name not in ("Stop", "Interrupt"):
        return None
    if not isinstance(session, str) or not session:
        raise ValueError("缺少 session_id")
    if not isinstance(turn, str) or not turn:
        raise ValueError("缺少 turn_id")
    return collect(session, turn, event.get("transcript_path"), proxy=os.getenv("CODEX_METRICS_PROXY"))


def _one_line(value):
    return str(value).replace("\r", " ").replace("\n", " ")


def display(report):
    first_char = report.get("first_char_seconds")
    output_speed = report.get("output_tokens_per_second")
    response_model = report.get("response_model")
    first_text = f"{first_char:.2f} s" if isinstance(first_char, (int, float)) else "不可用"
    speed_text = f"{output_speed:.2f} tok/s" if isinstance(output_speed, (int, float)) else "不可用"
    model_text = _one_line(response_model) if isinstance(response_model, str) and response_model else "不可用"
    return f"[CodexToolkit] 首字：{first_text} ｜输出速度：{speed_text} | 响应模型：{model_text}"


def hook_output(event):
    report = handle_hook(event)
    if report is None:
        return {}
    if event.get("hook_event_name") == "Stop":
        return {"continue": True, "systemMessage": display(report)}
    return {"systemMessage": display(report)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    imp = sub.add_parser("analyze", help="只分析指定 transcript 的一个回合")
    imp.add_argument("transcript")
    imp.add_argument("--session", required=True)
    imp.add_argument("--turn", required=True)
    imp.add_argument("--proxy", help="本机 Responses 采集器 URL；只读取其内存数据")
    args = parser.parse_args()
    if args.command == "hook":
        event = None
        try:
            event = json.load(sys.stdin)
            output = hook_output(event)
        except Exception as error:
            print(f"codex-toolkit: 统计失败 ({type(error).__name__})", file=sys.stderr)
            fallback = display(empty_report())
            if isinstance(event, dict) and event.get("hook_event_name") == "Stop":
                output = {"continue": True, "systemMessage": fallback}
            else:
                output = {"systemMessage": fallback}
        print(json.dumps(output, ensure_ascii=False))
        return
    if args.command == "analyze":
        report = analyze(read_jsonl(args.transcript), args.session, args.turn)
        requests, _ = proxy_records(args.proxy, args.session, args.turn)
        print(json.dumps(enrich(report, requests), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
