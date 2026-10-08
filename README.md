# Byte Agent Platform

面向研发问答与服务诊断的 Python ReAct Agent 工程项目。使用同一工具注册表驱动本地 Agent 与 MCP 服务，检索运行手册、查询合成服务指标，生成带证据引用的诊断回答。配套评测仓库：[byte-agent-eval](https://github.com/chendi-Shi/byte-agent-eval)。

**状态：可运行的工程原型。离线示例、故障测试与本地 Embedding 索引已验证；qwen3:4b 的真实 Agent 调用出现连续超时，尚无成功模型评测。** 所有示例资料与指标均为合成数据。实测记录见配套仓库的 [本地模型检查](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/local-model-smoke.md)。

## 快速运行

Python 3.11+；核心没有第三方运行依赖。安装后在项目根目录执行：

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m byte_agent demo
```

不安装也可设置 `PYTHONPATH=src`。PowerShell 用 `$env:PYTHONPATH='src'`。

Demo 使用显式标记的 Scripted 模型，验证工具、检索、日志和引用流程。`runs/demo/trace.json` 保存完整消息、工具参数、证据、错误、耗时和用量。再次运行相同配置会读取完成状态；新实验请更换 `--output`。

本地 Ollama 的真实模型调用：

```bash
python -m byte_agent ingest
python -m byte_agent run --model YOUR_TOOL_CAPABLE_MODEL --output runs/model-01
```

首次运行 `demo` 会创建合成 `runs/data/metrics.sqlite`。`run` 使用这份指标与已建立的索引，不会创建真实业务连接。模型必须支持工具调用；使用已经安装的 Ollama 模型，不会自动下载。默认 endpoint 为 `http://127.0.0.1:11434`；可通过 `--base-url` 指定。

可选混合检索：先 `ingest --embedding-model YOUR_EMBEDDING_MODEL`，再在 `run` 中使用相同 `--embedding-model`。BM25 词项分数与余弦排名通过 Reciprocal Rank Fusion 合并。未配置 Embedding 时使用词法检索，不宣称语义检索效果。索引更换模型后须重新构建。

## 从开源设计到具体改进

阅读了官方 [LangGraph ReAct 模板](https://github.com/langchain-ai/react-agent/blob/main/src/react_agent/graph.py) 的 model→tools→model 路由。这里采用独立实现，未复制上游代码，也不依赖 LangGraph。这样可以直接检查执行语义并在离线环境验证。技术来源、取舍与后续工作见 [docs/design.md](docs/design.md)。

| 关注点 | 本项目实现 | 验证 |
|---|---|---|
| ReAct 架构 | 模型选择动作、工具返回 observation、循环到最终回答 | 工具循环、步数与调用数预算测试 |
| 工具抽象 | schema 校验、只读参数化查询、结果大小限制 | 注入式 SQL 字符串不扩大查询范围 |
| RAG / Embedding | 版本化 Markdown 分块、BM25、可选向量 RRF、引用 id | 旧文档删除、向量维度和来源检查 |
| 执行记忆 | SQLite 事务日志与持久化 pending 计划 | 模型决策落库后的模拟崩溃恢复 |
| MCP / Skill | stdio tools 服务、可复用 incident-analysis 工作流 | MCP 初始化与 tools/call 子进程测试 |
| 可用性 | 单写者 OS 锁、配置与源码指纹、预算停止 | 失败请求不自动重试、配置不一致拒绝恢复 |

## MCP 使用

在支持 MCP 的客户端中配置（路径与 Python 可执行文件换成你的实际值）：

```json
{
  "mcpServers": {
    "byte-agent-tools": {
      "command": "python",
      "args": ["-m", "byte_agent", "mcp", "--data", "/absolute/path/to/byte-agent-platform/runs/data"]
    }
  }
}
```

需先安装包并执行 `demo` 或准备索引和指标库。服务仅提供 `initialize`、`ping`、`tools/list`、`tools/call` 的 MCP stdio 子集；不支持 resources、远程 HTTP、动态工具变更或 MCP client 功能。[工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) 固定为 2025-11-25；客户端需支持协商到该版本。

## 可靠性边界

- 工具仅提供本地检索与预定义指标查询，没有任意 SQL、命令执行、部署或消息发送能力。
- 只读工具在提交结果之前崩溃可重放；不声称任意副作用 exactly-once。模拟崩溃是落库后抛异常，不等同于断电测试。
- 模型响应丢失时状态为 `uncertain`，防止无意重复请求和计费。该 run 不自动恢复；检查轨迹后另开目录。
- 检索内容标记为不可信证据，系统提示限制指令权威。这个提示不能证明防住所有 prompt injection；工具白名单是更明确的能力边界。
- 用量记录 provider 的输入/输出 token。未知用量单独计数；token 阈值在响应后检查，最多可能超出一个请求。没有精确预付费预算保证。
- 工具与索引适用于小型本地项目；没有实现多租户鉴权、消息队列、分布式任务调度、长期用户记忆或跨 Agent 协作。
- Skill 文件供支持技能的 host 复用；CLI 不实现自动 Skill 发现。向量检索的真实质量和外部 MCP 客户端兼容性尚需实测。

## JD 对齐与面试讲述

可讲清三项原创工程决策：为什么模型请求失败与只读工具失败采用不同恢复策略；如何防止知识库更新后使用旧 run；为什么只把检索证据作为数据而非指令。面试时先展示 `demo` 与 `trace.json`，再用配套仓库展示独立验收和失败原因。请只描述实际做过、理解并能复现的内容。
