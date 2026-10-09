# TikTok ByteIntern Agent 岗位对齐

两个项目提供 Agent 架构、业务诊断应用、工具与知识接入、Agentic Eval 和可靠性设计的可查看证据。当前业务样本是合成服务事件，真实模型数字以[评测仓库公开实验记录](https://github.com/chendi-Shi/byte-agent-eval/tree/main/examples)为准。项目不能证明毕业时间、学历、算法面试水平或个人独立掌握程度。

## 岗位职责

| JD 条目 | 当前可以验证的内容 | 证据入口 | 实际边界 |
|---|---|---|---|
| 1. Agent 架构与基建，ReAct 框架 | 模型选择动作、工具 observation、循环停止；模型请求与工具计划的事务状态；预算与配置身份 | [runtime.py](../src/byte_agent/runtime.py)、[model.py](../src/byte_agent/model.py)、`trace.json` | 单 Agent 运行时；没有 Multi Agent 编排或 LangGraph 集成 |
| 2. Agent 应用，研发提效、问答、数据分析与决策 | 读取服务指标、运行手册、发布与依赖变化，形成结构化事件诊断；文件与 SQLite 接入可配置 | [domain.py](../src/byte_agent/domain.py)、[connectors.py](../src/byte_agent/connectors.py)、[数据契约](../examples/corpus/README.md) | 当前场景为合成业务；支持接入已有本地数据，不等于已经连接生产系统或产生业务收益 |
| 3. 工具、知识库和记忆基建 | 服务／文档精确过滤、版本化分块与引用、BM25＋可选向量 RRF；统一工具 schema；正式 MCP SDK server/client；显式 Skill loader | [knowledge.py](../src/byte_agent/knowledge.py)、[tools.py](../src/byte_agent/tools.py)、[mcp.py](../src/byte_agent/mcp.py)、[mcp_client.py](../src/byte_agent/mcp_client.py)、[skills.py](../src/byte_agent/skills.py) | 执行记忆用于恢复与审计；没有长期用户记忆、GraphRAG 或多来源在线知识同步 |
| 4. Agentic Eval、SFT、Agentic RL | 32 场景，24 dev／8 holdout；结果与当前证据验收；同预算提示对照和工具消融；失败归因、区间与任务聚类统计；训练候选数据过滤 | [评测仓库](https://github.com/chendi-Shi/byte-agent-eval)、[scoring.py](https://github.com/chendi-Shi/byte-agent-eval/blob/main/src/byte_eval/scoring.py)、[experiment.py](https://github.com/chendi-Shi/byte-agent-eval/blob/main/src/byte_eval/experiment.py) | 没有完成 LLM SFT 或 Agentic RL 权重训练；候选数据准备、提示调整、模型模板修复均不属于训练成果 |
| 5. 安全性、可用性与扩展性 | 参数化 SQL、schema 校验、只读工具白名单、输出限制；不可信检索内容；指纹与状态恢复；幂等入队、租约、心跳、取消与 fencing token | [tools.py](../src/byte_agent/tools.py)、[runtime.py](../src/byte_agent/runtime.py)、[jobs.py](../src/byte_agent/jobs.py)、[测试目录](../tests) | 单机 SQLite 队列；未实现多租户鉴权、大规模压测、跨主机任务调度或生产可用性 SLA |

## 任职要求

| JD 要求 | 这组项目能提供的证据 | 仍需要个人补充的证明 |
|---|---|---|
| 2027 届本科及以上，相关专业优先 | 无法由仓库验证 | 学历、专业、预计毕业时间和可实习周期 |
| 数据结构与算法、编码习惯，掌握 Go／Python／Java | Python 包结构、数据校验、BM25 排序与 RRF、事务状态机、任务队列，以及关键失败边界测试 | 能独立实现和解释；算法题与复杂度分析；Python 常用框架经验 |
| LLM 原理、Prompt Engineering、RAG／Graph、Embedding，LangChain 优先 | 系统提示与可信 Skill、不可信证据边界、可选 Embedding、混合检索、真实模型轨迹与策略实验接口 | Transformer／工具调用原理；检索质量实验；LangChain 或 Graph 的实践不能由当前仓库代替 |
| ReAct／Multi Agent、MCP／Skill；评测框架或 RL 经验优先 | ReAct 的实际执行循环，MCP SDK 服务与客户端，显式 Skill loader，独立 Agent 评测与轨迹数据准备 | Multi Agent 与 RL 实践仍缺；需要准确解释自己实现和验证过的范围 |
| 分布式、数据库、消息中间件；责任心、自驱力与成长能力 | SQLite 状态持久化、只读数据连接、幂等键、租约、fencing、可审查失败记录与复现实验 | PostgreSQL／Redis／消息中间件、跨主机部署与性能验证；实际团队协作和独立排障经历 |

这些加分项不需要全部实现才适合申请实习。面试价值取决于能否讲清代码与证据、现场复现一条任务、解释失败、识别能力边界，并对下一步改进提出合理验收标准。

## 验证清单与讲述重点

1. 用服务自己的运行手册解释阈值，而不是统一套用固定错误率；同时展示当前指标与变化记录。
2. 展示一次真实模型工具循环及原始 trace。区分模型跑通、任务验收通过和模型效果提升三个结论。
3. 解释错误服务、缺失证据、冲突因果报告、过期变化记录和虚假健康指令如何导致不同失败。
4. 解释模型响应丢失为什么停止重发，只读工具为什么可以重放，旧 worker 为什么被 fencing token 拒绝。
5. 用公开报告说明结果分母、配置、真实失败和未完成任务；fixture 只用于工程验收。
6. 清楚说明训练候选数据怎样排除 fixture、失败和 holdout；完成 LLM SFT/RL 需要额外的训练与权重对照证据。

简历措辞和演示命令见[面试讲述与演示](interview.md)。
