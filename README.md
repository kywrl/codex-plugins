# codex-plugins

Codex 插件集合，目前包含 [codex-toolkit](plugins/codex-toolkit/README.md)。

## codex-toolkit

在每轮结束或中断时，由脚本解析当前会话的 transcript，将统计结果作为 Hook 提示直接显示，不增加模型调用，也不写入数据库或报告文件。

- 每轮只显示首字耗时、输出速度和响应模型三项指标，默认直接读取 Codex transcript。
- 可选 Responses SSE 采集器可用真实流式时间和响应体 `response.model` 覆盖 transcript 回退值；数据只在内存中暂存，读取后删除。

安装方式、指标定义和采集器配置见 [插件文档](plugins/codex-toolkit/README.md)。

## 从插件市场安装

仓库自带 `.agents/plugins/marketplace.json`，可以直接从 GitHub 添加市场，不需要先克隆仓库：

```bash
codex plugin marketplace add kywrl/codex-plugins
codex plugin add codex-toolkit@codex-plugins
```

也可以使用完整 Git URL，或固定到指定分支：

```bash
codex plugin marketplace add https://github.com/kywrl/codex-plugins.git --ref main
```

本地开发时才使用本地路径：

```bash
codex plugin marketplace add /path/to/codex-plugins
```

检查市场和插件：

```bash
codex plugin marketplace list
codex plugin list --available --marketplace codex-plugins
```

安装后在 `/hooks` 中检查并信任 hook，并新建会话。修改代码后重新执行 `codex plugin add codex-toolkit@codex-plugins` 刷新插件副本；如果仍命中旧缓存，请先更新插件的 cachebuster。

## 开发

运行时需要 Python 3.9 或更高版本，插件脚本只依赖标准库。测试需要安装 `pytest`：

```bash
python3 -m pip install pytest
python3 -B -m pytest -q -p no:cacheprovider plugins/codex-toolkit/tests
```
