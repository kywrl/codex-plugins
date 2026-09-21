# codex-toolkit

这是一个本地 Codex plugin。每轮结束时，插件读取当前 transcript，由脚本计算并输出统计摘要，交给 Codex 显示为 Hook 提示，不产生额外模型调用。插件不会写入 SQLite、文件或其他持久化存储；进程结束后，统计数据即丢弃。

## 安装与信任

把 `plugins/codex-toolkit` 放入本地 marketplace 后启用插件。安装后在 Codex 中打开 `/hooks`，检查并信任当前 hook 定义；未信任的非托管 hook 会被跳过。

若本机安装了 `plugin-creator` skill，可在仓库根目录运行结构校验：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/validate_plugin.py" \
  plugins/codex-toolkit
```

`Stop` 和 `Interrupt` hook 使用同步命令，向标准输出返回 `{"systemMessage": "脚本生成的统计摘要"}`，并以退出码 `0` 结束。插件不返回 `decision: "block"`、续写提示或模型上下文。摘要在回合结束时作为独立 Hook 提示显示，具体位置和样式由 Codex 宿主决定，不改写助手正文。协议依据：[OpenAI Docs — Hooks](https://learn.chatgpt.com/docs/hooks#common-output-fields)。

统计截至 hook 执行时。若此时 transcript 尚未提供 `task_complete`，原生首 token 时长会显示为不可用，不额外调用模型补齐。摘要有长度上限，超出时截断，不另存完整报告。

## 指标

- `ttft_ms`：优先使用 Codex transcript 的 `task_complete.time_to_first_token_ms`。
- `turn_output_tokens_per_second`：该回合输出 token ÷ 整个回合耗时，包含工具等待。
- `generation_tokens_per_second`：启用下方 SSE 采集器且响应有完整 usage 时给出；使用非 reasoning 输出 token ÷ 首个输出 delta 到完成的时长。
- `selected_model_ids`：来自 transcript 的 `turn_context.model`，表示 Codex 选择值。
- `request_model_ids`：只来自采集器实际读取的请求 JSON `model`；不会用 `turn_context.model` 冒充原始请求值。
- `response_model_ids`：只来自采集器实际读取的 SSE `response.model`。
- `model_id_comparisons`：按请求比较请求体模型和响应体模型；缺少任一端时保持未知。
- `model_id_mismatch`：有完整请求对比且出现不一致时为 `true`；这只是标识差异，不直接判定发生了模型替换。
- `cache_hit_rate`：`cached_input_tokens / input_tokens`。没有真实 usage 时为 `null`，不会从字符数估算 token。

## 可选 Responses SSE 采集器

采集器只监听 `127.0.0.1`，仅转发 `POST /responses`（或 `/v1/responses`）的 `stream=true` JSON 请求，不支持 WebSocket、压缩请求体或浏览器 Origin。非本机 upstream 必须使用 HTTPS。采集记录只保存在采集器进程内存中，最多保留最近 1000 条，进程退出后丢弃。

```bash
python3 plugins/codex-toolkit/scripts/proxy.py \
  --upstream https://api.openai.com/v1 --port 8765
```

让调用方的 Responses base URL 指向 `http://127.0.0.1:8765/v1`，并为每个请求添加 `X-Codex-Metrics-Session` 和 `X-Codex-Metrics-Turn`。启动 Codex 时设置：

```bash
export CODEX_METRICS_PROXY=http://127.0.0.1:8765
```

回合结束时，hook 从采集器的本机内存端点一次性领取当前 session/turn 的请求记录并把模型信息合并到摘要；领取后记录立即丢弃。不设置该变量时，只显示 transcript 能提供的指标。

## 手工分析

手工分析只读取指定 transcript 并把完整 JSON 打到标准输出，不创建任何文件：

```bash
python3 plugins/codex-toolkit/scripts/metrics.py analyze \
  /path/to/rollout.jsonl --session SESSION_ID --turn TURN_ID
```

Codex 桌面端的 ChatGPT 登录流量由宿主端管理，插件 hook 没有原始 HTTP 请求/响应体访问权；要测量真实请求/响应模型，需要在你控制的 Responses API provider 或上述本地采集器路径中运行。
