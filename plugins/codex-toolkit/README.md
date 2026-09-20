# codex-toolkit

这是一个本地 Codex plugin。每轮结束时，插件读取当前 transcript，计算会话指标，并把摘要直接显示在本轮 Codex 输出末尾。插件不会写入 SQLite、文件或其他持久化存储；进程结束后，统计数据即丢弃。

## 安装与信任

把 `plugins/codex-toolkit` 放入本地 marketplace 后启用插件。安装后在 Codex 中打开 `/hooks`，检查并信任当前 hook 定义；未信任的非托管 hook 会被跳过。

```bash
python3 /Users/cjx/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  /Users/cjx/projects/codex-plugins/plugins/codex-toolkit
```

`Stop` hook 使用同步命令，先计算摘要，再请求一次只输出统计的助手续写，因此统计会出现在本轮回复的末尾。这个续写会额外消耗一次模型调用，并且统计只覆盖追加摘要之前的任务；`stop_hook_active` 会阻止它再次触发。`Interrupt` hook 不能继续生成正文，因此只通过界面提示显示已完成的部分统计。输出保持简短，避免触发 Codex 对超长 hook 输出的溢出处理。

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
