# codex-plugins

Codex 插件集合，目前包含 [codex-toolkit](plugins/codex-toolkit/README.md)。

## codex-toolkit

在每轮结束或中断时，由脚本解析当前会话的 transcript，将统计结果作为 Hook 提示直接显示，不增加模型调用，也不写入数据库或报告文件。

- 统计首 token 时长、回合平均 token 速度、token 用量和缓存命中率。
- 区分 `turn_context.model`、请求体 `model` 与响应体 `response.model`。
- 可选 Responses SSE 采集器提供请求级指标，数据只在内存中暂存，读取后删除。
- 缺失指标显示为不可用；模型标识不一致不直接代表发生了模型替换。

安装方式、指标定义和采集器配置见 [插件文档](plugins/codex-toolkit/README.md)。

## 开发

运行时需要 Python 3.9 或更高版本，插件脚本只依赖标准库。测试需要安装 `pytest`：

```bash
python3 -m pip install pytest
python3 -B -m pytest -q -p no:cacheprovider plugins/codex-toolkit/tests
```
