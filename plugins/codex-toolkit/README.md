# codex-toolkit

这是一个本地 Codex plugin。每轮结束时，插件读取当前 transcript，由脚本计算并输出统计摘要，交给 Codex 显示为 Hook 提示，不产生额外模型调用。插件不会写入 SQLite、文件或其他持久化存储；进程结束后，统计数据即丢弃。

## 使用

### 从仓库市场安装

仓库根目录已经包含 `.agents/plugins/marketplace.json`，市场名为 `codex-plugins`，其中的插件路径是相对于仓库根目录的 `./plugins/codex-toolkit`。推荐直接从 GitHub 添加市场，不需要先克隆仓库：

```bash
codex plugin marketplace add kywrl/codex-plugins
codex plugin add codex-toolkit@codex-plugins
```

也可以使用完整 Git URL，或固定到 `main` 分支：

```bash
codex plugin marketplace add https://github.com/kywrl/codex-plugins.git --ref main
```

本地路径仅适用于开发和测试：

```bash
codex plugin marketplace add /path/to/codex-plugins
```

确认市场和插件已经被发现：

```bash
codex plugin marketplace list
codex plugin list --available --marketplace codex-plugins
```

也可以在 Codex CLI 中运行 `/plugins`，选择 `codex-plugins` 市场并安装 `codex-toolkit`。在 ChatGPT 桌面端的 Codex 中，重启应用后打开 Plugins 目录，选择该仓库市场再安装插件。

安装后在 Codex 中打开 `/hooks`，检查并信任当前 hook 定义；安装插件不会自动信任非托管 hook，未信任的 hook 会被跳过。启用插件后请新建会话，让宿主加载最新的插件副本。

`Stop` hook 使用同步命令，向标准输出返回 `{"continue": true, "systemMessage": "脚本生成的统计摘要"}`；`Interrupt` 返回 `{"systemMessage": "脚本生成的统计摘要"}`，两者均以退出码 `0` 结束。Stop 显式返回 `continue: true` 是为了兼容 Codex Desktop 的 hook 结果转发；插件不返回 `decision: "block"`、续写提示或模型上下文。摘要在回合结束时作为独立 Hook 提示显示，具体位置和样式由 Codex 宿主决定，不改写助手正文。协议依据：[OpenAI Docs — Hooks](https://learn.chatgpt.com/docs/hooks#common-output-fields)。

插件按默认约定从 `hooks/hooks.json` 加载 hook；hook 命令通过 `scripts/run_metrics_hook.sh` 查找 `python3`，以兼容从桌面应用启动时与终端不同的 `PATH`。修改 hook 定义后需要重新信任该 hook，并新建会话。

统计截至 hook 执行时；输出始终只有一行：

```text
[CodexToolkit] 首字：n s ｜输出速度：n tok/s | 响应模型：xxx
```

默认情况下，首字读取 `task_complete.time_to_first_token_ms`，输出速度使用本轮 `turn_token_usage.output_tokens / (duration_ms - time_to_first_token_ms)`，响应模型读取当前回合的 `turn_context.model`。这样普通 Codex Desktop 会话不需要额外代理即可显示三项指标。若启用下方 Responses SSE 采集器，首字、输出速度和响应模型会分别由真实流式时间、响应 usage 和响应体 `response.model` 覆盖。只有 transcript 与采集器均未提供所需字段时才显示“不可用”。

### 可选 Responses SSE 采集器

采集器只监听 `127.0.0.1`，仅转发 `POST /responses`（或 `/v1/responses`）的 `stream=true` JSON 请求，不支持 WebSocket、压缩请求体或浏览器 Origin。非本机 upstream 必须使用 HTTPS。采集记录只保存在采集器进程内存中，最多保留最近 1000 条，进程退出后丢弃。

启动采集器：

```bash
python3 plugins/codex-toolkit/scripts/proxy.py \
  --upstream https://api.openai.com/v1 --port 8765
```

让调用方的 Responses base URL 指向 `http://127.0.0.1:8765/v1`，并为每个请求添加 `X-Codex-Metrics-Session` 和 `X-Codex-Metrics-Turn`。启动 Codex 时设置：

```bash
export CODEX_METRICS_PROXY=http://127.0.0.1:8765
```

回合结束时，hook 从采集器的本机内存端点一次性领取当前 session/turn 的请求记录并把三项指标合并到摘要；领取后记录立即丢弃。不设置该变量时，三项指标均从 transcript 读取。对于包含工具调用的长回合，transcript 的输出速度会包含工具等待时间；采集器提供的流式速度更精确。

### 手工分析

手工分析只读取指定 transcript 并把完整 JSON 打到标准输出，不创建任何文件：

```bash
python3 plugins/codex-toolkit/scripts/metrics.py analyze \
  /path/to/rollout.jsonl --session SESSION_ID --turn TURN_ID
```

如果要合并采集器中的请求记录，追加 `--proxy`：

```bash
python3 plugins/codex-toolkit/scripts/metrics.py analyze \
  /path/to/rollout.jsonl --session SESSION_ID --turn TURN_ID \
  --proxy http://127.0.0.1:8765
```

Codex 桌面端的 ChatGPT 登录流量由宿主端管理，插件 hook 没有原始 HTTP 请求/响应体访问权；因此未启用采集器时显示的是 `turn_context.model`，不是服务端响应体中的模型 ID。要测量真实流式速度和 `response.model`，需要在你控制的 Responses API provider 或上述本地采集器路径中运行。

## 开发

### 目录结构

- `.codex-plugin/plugin.json`：插件元数据和 UI 描述。
- `hooks/hooks.json`：`Stop`、`Interrupt` 生命周期 hook 定义。
- `scripts/run_metrics_hook.sh`：为桌面应用解析可用的 `python3` 后启动 hook。
- `scripts/metrics.py`：读取 transcript 中的三项回合指标、合并采集器数据并生成单行 hook 输出。
- `scripts/proxy.py`：可选的本地 Responses SSE 采集器，仅在进程内保存三项指标所需记录。
- `tests/test_metrics.py`：首字、输出速度和单行 hook 输出测试。
- `tests/test_proxy.py`：SSE 解析、响应模型、内存记录和领取隔离测试。

### 本地校验

如果本机安装了 `plugin-creator` skill，可在仓库根目录运行结构校验：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/validate_plugin.py" \
  plugins/codex-toolkit
```

运行插件测试：

```bash
python3 -m pytest plugins/codex-toolkit/tests
```

### 更新已安装的本地插件

修改仓库中的代码后，重新安装插件以刷新本地副本：

```bash
codex plugin add codex-toolkit@codex-plugins
```

如果 Codex 仍使用旧的缓存副本，使用仓库中的 `plugin-creator` skill 更新本地 cachebuster，然后再次安装：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py" \
  plugins/codex-toolkit
codex plugin add codex-toolkit@codex-plugins
```

更新后请新建会话，并重新检查 `/hooks` 中的信任状态。

也可以直接检查两个命令行入口：

```bash
python3 -m py_compile plugins/codex-toolkit/scripts/metrics.py \
  plugins/codex-toolkit/scripts/proxy.py
python3 plugins/codex-toolkit/scripts/metrics.py --help
python3 plugins/codex-toolkit/scripts/proxy.py --help
```

### 实现约束

- hook 只读取宿主传入的 transcript 和环境变量，不调用模型，不写文件、数据库或其他持久化存储。
- 采集器只监听回环地址，按 `session_id` 和 `turn_id` 隔离记录；hook 领取后立即删除记录。
- transcript 模型只代表 `turn_context.model`；真实响应模型 ID 只能来自 SSE 响应，不能据此推断模型替换。
- usage 缺失时指标显示为 `null`；不能用字符数或 delta 数量估算 token。
- 修改摘要字段或 hook 协议时，应同步更新测试和本 README 的指标说明。
