#!/usr/bin/env python3
"""本地会话统计；不记录提示词、消息正文或凭据。Python 3.9+。"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "cache_write_input_tokens", "total_tokens")


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return number(value)


def metric(value=None, source=None, reason=None):
    return {"value": value, "source": source, "reason": reason}


def divide(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {k: number(raw.get(k)) for k in TOKEN_KEYS}
    details = raw.get("input_tokens_details") or {}
    if result["cached_input_tokens"] is None:
        result["cached_input_tokens"] = number(details.get("cached_tokens"))
    details = raw.get("output_tokens_details") or {}
    if result["reasoning_output_tokens"] is None:
        result["reasoning_output_tokens"] = number(details.get("reasoning_tokens"))
    if result["total_tokens"] is None and result["input_tokens"] is not None and result["output_tokens"] is not None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def sum_usage(entries):
    return {k: sum(x[k] for x in entries) if entries and all(x[k] is not None for x in entries) else None for k in TOKEN_KEYS}


def read_jsonl(path):
    """完整行才参与统计；损坏行计入告警，不从消息正文递归提取指标。"""
    with Path(path).open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                yield None
                continue
            try:
                value = json.loads(line)
                yield value if isinstance(value, dict) else None
            except (ValueError, UnicodeError):
                yield None


def analyze(records, session_id, turn_id, hook_started=None, hook_ended=None):
    active = False
    started = hook_started
    ended = None
    duration_ms = None
    ttft = None
    first_message = None
    selected = []
    per_response = {}
    last_turn_usage = None
    previous_total = None
    legacy_deltas = []
    warnings = set()
    status = "observed_stop"
    observed = False
    for record in records:
        if record is None:
            warnings.add("transcript 存在损坏或尚未写完的行")
            continue
        p = record.get("payload")
        if not isinstance(p, dict):
            continue
        kind = record.get("type")
        event = p.get("type") if kind == "event_msg" else None
        at = timestamp(record.get("timestamp"))
        if event == "task_started" or kind == "turn_context":
            if p.get("turn_id"):
                active = p["turn_id"] == turn_id
            if active:
                observed = True
                if event == "task_started" and at is not None:
                    started = at if started is None else min(started, at)
                if kind == "turn_context" and isinstance(p.get("model"), str) and p["model"] not in selected:
                    selected.append(p["model"])
        if kind == "token_usage_record" and p.get("turn_id") == turn_id and p.get("thread_id", session_id) == session_id:
            observed = True
            if isinstance(p.get("usage"), dict) and p.get("response_id"):
                per_response[p["response_id"]] = usage(p["usage"])
            if isinstance(p.get("turn_token_usage"), dict):
                last_turn_usage = usage(p["turn_token_usage"])
        if event == "token_count":
            info = p.get("info") or {}
            raw = info.get("total_token_usage")
            if isinstance(raw, dict):
                total = usage(raw)
                if active:
                    # 只计算累计值的变化；重复的限额更新不会再次累计 last_token_usage。
                    if previous_total is None:
                        warnings.add("旧版累计用量缺少回合前基线，token 用量不可确认")
                    elif total != previous_total:
                        delta = {k: total[k] - previous_total[k] if total[k] is not None and previous_total[k] is not None and total[k] >= previous_total[k] else None for k in TOKEN_KEYS}
                        if any(total[k] is not None and previous_total[k] is not None and total[k] < previous_total[k] for k in TOKEN_KEYS):
                            warnings.add("累计用量发生重置，旧版统计可能不完整")
                        legacy_deltas.append(delta)
                previous_total = total
        if active and kind == "response_item" and p.get("type") == "message" and p.get("role") == "assistant" and at is not None:
            if any(c.get("type") == "output_text" and c.get("text") for c in p.get("content", []) if isinstance(c, dict)):
                first_message = at if first_message is None else min(first_message, at)
        if event in ("task_complete", "turn_aborted") and p.get("turn_id") == turn_id:
            observed = True
            ended = at
            duration_ms = number(p.get("duration_ms"))
            ttft = number(p.get("time_to_first_token_ms"))
            status = "interrupted" if event == "turn_aborted" else "failed" if p.get("error") else "completed"
    native = list(per_response.values())
    if last_turn_usage is not None:
        tokens, token_source = last_turn_usage, "transcript.token_usage_record.turn_token_usage"
    elif native:
        tokens, token_source = sum_usage(native), "transcript.token_usage_record.usage"
    elif legacy_deltas and not any("基线" in w or "重置" in w for w in warnings):
        tokens, token_source = sum_usage(legacy_deltas), "transcript.token_count.cumulative_delta"
    else:
        tokens, token_source = usage({}), None
    if ended is None:
        ended = hook_ended
    if duration_ms is None and started is not None and ended is not None and ended >= started:
        duration_ms = (ended - started) * 1000
    report = {
        "schema_version": 1, "session_id": session_id, "turn_id": turn_id,
        "status": status, "observed_in_transcript": observed,
        "started_at": started, "ended_at": ended,
        "duration_ms": metric(duration_ms, "transcript_or_hook_elapsed", "缺少回合时间"),
        "ttft_ms": metric(ttft, "transcript.task_complete.time_to_first_token_ms", "当前日志没有原生首 token 时长；Stop 时日志也可能尚未落盘"),
        "first_completed_message_ms": metric((first_message - started) * 1000 if first_message is not None and started is not None and first_message >= started else None, "transcript.response_item.timestamp", "缺少消息完成时间；该字段不是 TTFT"),
        "tokens": tokens, "tokens_source": token_source,
        "cache_hit_rate": metric(divide(tokens["cached_input_tokens"], tokens["input_tokens"]), token_source, "缺少缓存/输入用量，或输入为零"),
        "turn_output_tokens_per_second": metric(divide(tokens["output_tokens"], duration_ms / 1000 if duration_ms is not None else None), "output_tokens / turn_duration_seconds", "缺少用量或回合耗时"),
        "generation_tokens_per_second": metric(reason="需要请求级流式采集；回合耗时包含工具执行和等待"),
        "selected_model_ids": selected,
        "request_model_ids": metric(reason="原始请求体未暴露；请启用 Responses 采集器"),
        "response_model_ids": metric(reason="transcript 未暴露响应体 model 字段"),
        "model_id_comparisons": [],
        "model_id_mismatch": metric(reason="没有同时包含请求体和响应体模型 ID 的请求"),
        "request_count": len(native) if native else None,
        "requests": [], "warnings": sorted(warnings),
    }
    return report


def data_dir(explicit=None):
    base = explicit or os.getenv("CODEX_METRICS_DATA") or os.getenv("PLUGIN_DATA")
    return Path(base).expanduser() if base else Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "session-metrics"


def connect(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root / "metrics.sqlite3", timeout=1)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS starts (session TEXT, turn TEXT, started REAL, transcript TEXT,
          PRIMARY KEY(session, turn));
        CREATE TABLE IF NOT EXISTS reports (session TEXT, turn TEXT, updated REAL, report TEXT,
          PRIMARY KEY(session, turn));
        CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, session TEXT, turn TEXT, record TEXT);
    """)
    return db


def compare_model_ids(requests):
    """逐请求比较两端 ID；缺一端时保持 unknown，不把缺失误报为替换。"""
    comparisons = []
    for request in requests:
        request_model = request.get("request_model_id")
        response_model = request.get("response_model_id")
        if not isinstance(request_model, str) or not isinstance(response_model, str):
            match = None
        else:
            match = request_model == response_model
        comparisons.append({
            "request_id": request.get("id"),
            "response_id": request.get("response_id"),
            "request_model_id": request_model,
            "response_model_id": response_model,
            "match": match,
        })
    complete = [item for item in comparisons if item["match"] is not None]
    mismatch = any(item["match"] is False for item in complete) if complete else None
    return comparisons, mismatch


def enrich(db, report):
    requests = [json.loads(row[0]) for row in db.execute("SELECT record FROM requests WHERE session=? AND turn=? ORDER BY id", (report["session_id"], report["turn_id"]))]
    report["requests"] = requests
    comparisons, mismatch = compare_model_ids(requests)
    report["model_id_comparisons"] = comparisons
    report["model_id_mismatch"] = metric(
        mismatch,
        "responses_proxy.request_model_id == responses_proxy.response_model_id",
        "没有同时包含请求体和响应体模型 ID 的请求" if mismatch is None else None,
    )
    for field in ("request_model_ids", "response_model_ids"):
        key = "request_model_id" if field == "request_model_ids" else "response_model_id"
        values = sorted({r[key] for r in requests if isinstance(r.get(key), str)})
        if values:
            report[field] = metric(values, "responses_proxy." + key)
    complete = [r for r in requests if r.get("status") == "completed"]
    if complete:
        speeds = [r for r in complete if r.get("non_reasoning_output_tokens") is not None and (r.get("generation_duration_ms") or 0) > 0]
        # 全部成功请求均具备时间和用量时才给出加权吞吐量。
        if len(speeds) == len(complete):
            report["generation_tokens_per_second"] = metric(divide(sum(r["non_reasoning_output_tokens"] for r in speeds), sum(r["generation_duration_ms"] for r in speeds) / 1000), "sum(non_reasoning_output_tokens) / sum(first_output_delta_to_completed_seconds)")
    if requests:
        report["captured_request_count"] = len(requests)
        warnings = ["流式采集只代表已捕获请求；响应 model 为服务端声明，不能证明底层权重身份"]
        if mismatch is True:
            warnings.append("请求体 model 与同请求 response.model 不一致；这只是标识差异，不直接判定发生了模型替换")
        report["warnings"] = sorted(set(report["warnings"] + warnings))
    return report


def save_report(db, report):
    with db:
        db.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?,?)", (report["session_id"], report["turn_id"], time.time(), json.dumps(report, ensure_ascii=False)))


def collect(db, session, turn, transcript, ended=None, wait=False):
    start = db.execute("SELECT started, transcript FROM starts WHERE session=? AND turn=?", (session, turn)).fetchone()
    transcript = transcript or (start[1] if start else None)
    deadline = time.monotonic() + (2 if wait else 0)
    while True:
        records = read_jsonl(transcript) if transcript and Path(transcript).is_file() else []
        report = analyze(records, session, turn, start[0] if start else None, ended)
        if not transcript or not Path(transcript).is_file():
            report["warnings"].append("transcript 不存在或未提供")
        if report["status"] != "observed_stop" or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    save_report(db, enrich(db, report))
    return report


def latest_turn_id(transcript):
    """从 transcript 找到最近启动的主回合，兼容插件中途才启用的会话。"""
    latest = None
    if not transcript or not Path(transcript).is_file():
        return latest
    for record in read_jsonl(transcript):
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") or {}
        if record.get("type") == "event_msg" and payload.get("type") == "task_started" and isinstance(payload.get("turn_id"), str):
            latest = payload["turn_id"]
    return latest


def handle_hook(event, root):
    name = event.get("hook_event_name")
    session = event.get("session_id")
    turn = event.get("turn_id")
    if not isinstance(session, str) or not session:
        raise ValueError("缺少 session_id")
    db = connect(root)
    try:
        if name == "UserPromptSubmit":
            if not isinstance(turn, str) or not turn:
                raise ValueError("缺少 turn_id")
            with db:
                db.execute("INSERT OR IGNORE INTO starts VALUES(?,?,?,?)", (session, turn, time.time(), event.get("transcript_path")))
        elif name in ("Stop", "Interrupt"):
            if not isinstance(turn, str) or not turn:
                raise ValueError("缺少 turn_id")
            collect(db, session, turn, event.get("transcript_path"), time.time(), wait=name == "Stop")
        elif name == "SessionEnd":
            # 修复 Stop 之后才落盘的 task_complete；只补采最近一轮。
            transcript = event.get("transcript_path")
            row = db.execute("SELECT turn, transcript FROM starts WHERE session=? ORDER BY started DESC LIMIT 1", (session,)).fetchone()
            if row:
                collect(db, session, row[0], transcript or row[1])
            else:
                inferred = latest_turn_id(transcript)
                if inferred:
                    collect(db, session, inferred, transcript)
    finally:
        db.close()


def display(report):
    def val(key, unit="", factor=1):
        value = report[key]["value"]
        if value is None:
            return "不可用"
        return f"{value * factor:.2f}{unit}" if isinstance(value, (int, float)) else ", ".join(value)
    return "\n".join([
        f"会话：{report['session_id']} / 回合：{report['turn_id']} ({report['status']})",
        f"首 token：{val('ttft_ms', ' ms')} · 回合平均：{val('turn_output_tokens_per_second', ' token/s')}",
        f"流式生成吞吐：{val('generation_tokens_per_second', ' token/s')} · 缓存命中：{val('cache_hit_rate', '%', 100)}",
        f"选中模型：{', '.join(report['selected_model_ids']) or '不可用'}",
        f"请求体模型：{val('request_model_ids')} · 响应体模型：{val('response_model_ids')}",
        f"模型标识不一致：{val('model_id_mismatch')}",
        f"用量：{json.dumps(report['tokens'], ensure_ascii=False)}",
        *[f"提示：{w}" for w in report["warnings"]],
    ])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    imp = sub.add_parser("analyze", help="只分析指定 transcript 的一个回合")
    imp.add_argument("transcript")
    imp.add_argument("--session", required=True)
    imp.add_argument("--turn", required=True)
    show = sub.add_parser("report", help="查看最近一轮，或按 session/turn 筛选")
    show.add_argument("--session")
    show.add_argument("--turn")
    show.add_argument("--json", action="store_true")
    sub.add_parser("requests", help="查看采集器请求，包括未能归属回合的请求")
    args = parser.parse_args()
    root = data_dir(args.data_dir)
    if args.command == "hook":
        try:
            handle_hook(json.load(sys.stdin), root)
        except Exception as error:
            # 统计失败不影响用户回合；不输出可能包含消息/路径的异常正文。
            print(f"codex-toolkit: 统计失败 ({type(error).__name__})", file=sys.stderr)
            print(json.dumps({"systemMessage": "会话统计失败，请检查插件数据目录和 transcript 格式。"}, ensure_ascii=False))
        else:
            print("{}")
        return
    if args.command == "analyze":
        report = analyze(read_jsonl(args.transcript), args.session, args.turn)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    db = connect(root)
    if args.command == "requests":
        rows = [json.loads(r[0]) for r in db.execute("SELECT record FROM requests ORDER BY rowid DESC LIMIT 100")]
        db.close()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    clauses, values = [], []
    for field in ("session", "turn"):
        value = getattr(args, field)
        if value:
            clauses.append(field + "=?")
            values.append(value)
    row = db.execute("SELECT report FROM reports" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY updated DESC LIMIT 1", values).fetchone()
    if not row:
        print("暂无统计。安装并信任 hooks 后，完成一轮新会话。")
        db.close()
        return
    report = enrich(db, json.loads(row[0]))
    db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else display(report))


if __name__ == "__main__":
    main()
