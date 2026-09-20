# codex-session-metrics

这是一个本地 Codex plugin，用 lifecycle hooks 在回合结束后解析 transcript，并把统计保存到 SQLite。插件默认不保存提示词、消息正文、Authorization header 或响应正文。

## 安装与信任

把 `plugins/codex-session-metrics` 放入本地 marketplace 后启用插件。安装后在 Codex 中打开 `/hooks`，检查并信任当前 hook 定义；未信任的非托管 hook 会被跳过。

```bash
python3 /Users/cjx/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  /Users/cjx/projects/codex-plugins/plugins/codex-session-metrics
```

## 查看报告

```bash
python3 plugins/codex-session-metrics/scripts/metrics.py report
python3 plugins/codex-session-metrics/scripts/metrics.py report --json
python3 plugins/codex-session-metrics/scripts/metrics.py requests
```

数据默认写入 `$PLUGIN_DATA`；命令行手工查看时默认写入 `$CODEX_HOME/session-metrics`。可通过 `CODEX_METRICS_DATA` 或 `--data-dir` 指定目录。

## 指标含义

- `ttft_ms`：优先使用 Codex transcript 的 `task_complete.time_to_first_token_ms`。
- `turn_output_tokens_per_second`：该回合输出 token ÷ 整个回合耗时，包含工具等待。
- `generation_tokens_per_second`：只有启用下方 SSE 采集器且响应有完整 usage 时才给出；使用非 reasoning 输出 token ÷ 首个输出 delta 到完成的时长。
- `selected_model_ids`：来自 transcript 的 `turn_context.model`，表示 Codex 选择值。
- `request_model_ids`：只来自采集器实际读取的请求 JSON `model`；不会用 `turn_context.model` 冒充原始请求值。
- `response_model_ids`：只来自采集器实际读取的 SSE `response.model`。
- `model_id_comparisons`：按请求保存 `request_model_id`、`response_model_id`、`response_id` 和 `match`。两端都存在且不同才会出现 `match: false`。
- `model_id_mismatch`：回合内只要有一个完整请求对比不一致就为 `true`；没有完整对比则为 `null`。`true` 只表示标识差异，不直接判定发生了模型替换。
- `cache_hit_rate`：`cached_input_tokens / input_tokens`。没有真实 usage 时为 `null`，不会从字符数估算 token。

Codex transcript 通常能给出 `token_usage_record` 和 `cached_input_tokens`，但不一定包含原始请求体、响应体 model 或首个 SSE delta 时间。因此报告会带 `source` 和 `reason`，把不可证明的字段显示为 `null`。

## 可选 Responses SSE 采集器

采集器只监听 `127.0.0.1`，仅转发 `POST /responses`（或 `/v1/responses`）的 `stream=true` JSON 请求，不支持 WebSocket、压缩请求体或浏览器 Origin。非本机 upstream 必须使用 HTTPS。

```bash
python3 plugins/codex-session-metrics/scripts/proxy.py \
  --upstream https://api.openai.com/v1 --port 8765
```

将调用方的 Responses base URL 指向 `http://127.0.0.1:8765/v1`。为把请求归入某回合，可添加 `X-Codex-Metrics-Session` 和 `X-Codex-Metrics-Turn`；否则请求仍会记录，但报告无法按回合合并。采集器不会把 delta 数量当作 token 数，而使用最终响应 usage。

## 限制

Codex 桌面端的 ChatGPT 登录流量由宿主端管理，插件 hook 没有原始 HTTP 请求/响应体访问权；本插件不会声称从 transcript 推断出请求体和响应体模型。若使用 ChatGPT 登录桌面端，只能得到 transcript 暴露的指标；要测量真实请求/响应模型，需要在你控制的 Responses API provider 或上述本地采集器路径中运行。
