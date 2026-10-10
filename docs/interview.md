# 面试讲述与演示

这组项目围绕一个具体任务：读取服务指标、服务自己的运行手册，以及发布与依赖观测，输出可验收的事件诊断。平台负责 ReAct、四角色协作和本地／HTTP 队列执行，评测仓库负责独立验收。示例业务全部是合成数据；接入自己的 Markdown 和 SQLite 文件使用相同数据接口。

历史单 Agent 模型配置、完成情况和成绩以[历史公开记录](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/model-results.md)为准。V3 [完整结果](../examples/v3-results.md)另行记录四角色执行完成但业务失败的负面案例、网络 worker 独立 oracle、同机 TCP 故障恢复，以及实际 135M LoRA SFT／REINFORCE。引用数字同时说明模型、任务数、策略与失败；一个开发案例的通过和 Scripted 工程任务都不构成总体模型准确率。

## 两个项目分别解决什么问题

| 项目 | 核心问题 | 可以打开的证据 |
|---|---|---|
| Byte Agent Platform | 如何让一个或多个能力受限的 Agent 完成诊断，并由本地或 TCP worker 执行、恢复和审查？ | `runtime.py` 的状态迁移；实际工具输出；MCP／Skill；`multiagent.py` 的角色与封存证据；`distributed.py` 的服务器租约与 fencing |
| Byte Agent Eval | 如何避免 Agent 把“我完成了”当成成功，并公平比较提示与工具配置？ | 独立 JSON 结果验收；当前指标和引用验收；24 dev / 8 holdout 的任务划分；同预算对照；失败归因；统计与训练候选数据过滤 |

平台不读取带有答案标签的 `services.json`。知识索引只读取 `knowledge/` 中的 Markdown，三个工具只暴露观测和政策。评测程序读取标签并核对轨迹，这样模型回答和验收标准分属不同路径。

```mermaid
flowchart LR
    Task[任务与显式 Skill] --> Runtime[ReAct 运行时]
    Runtime <--> Model[工具模型]
    Runtime <--> Tools[本地注册表或 MCP SDK client]
    Tools --> Metrics[service_metrics]
    Tools --> Knowledge[knowledge_search]
    Tools --> Changes[incident_changes]
    Runtime --> Journal[SQLite journal 与 trace]
    Queue[队列租约与 fencing token] --> Runtime
    API[Waitress 中央 API] --> Queue
    Worker[TCP worker 的本地数据与日志] --> Runtime
    Team[四角色 Coordinator] --> Runtime
    Team --> Shared[封存证据快照]
    Journal --> Oracle[独立结果验收]
    Labels[评测标签] --> Oracle
    Oracle --> Report[失败归因与实验报告]
```

## 十分钟演示顺序

### 1. 先展示业务约束与数据，约两分钟

在平台仓库根目录执行：

```powershell
python -m pip install -e ".[mcp]"
python -m byte_agent dataset --data runs/interview-data
```

打开 `runs/interview-data/services.json` 和 `runs/interview-data/knowledge/growth-feed/runbook.md`。解释为什么需要三类证据：指标证明当前异常，运行手册定义这个服务的阈值，变化记录支持原因判断。单靠错误率不能区分发布回归、依赖故障和容量压力。

任务共有 32 个独立服务：24 个开发任务、8 个不同服务的 holdout 任务，覆盖发布、依赖、容量、健康、指标缺失、运行手册缺失、证据冲突和恶意检索指令。32 个服务的错误率阈值与延迟限制各不相同。holdout 已公开，它用于工程上的实验隔离，不属于保密考试题。

### 2. 展示一条完整执行轨迹，约三分钟

使用已经安装且支持工具调用的模型，并显式加载操作员选择的诊断 Skill：

```powershell
$model = "YOUR_TOOL_CAPABLE_MODEL"
python -m byte_agent run --data runs/interview-data --model $model --service growth-feed --verify --skill skills/incident-analysis/SKILL.md --task "Diagnose growth-feed using its latest 30-minute metrics, its own runbook and current change/dependency observations. Return the incident-analysis JSON answer." --output runs/interview-model-v1
```

打开 `runs/interview-model-v1/trace.json`，按“模型选择工具 → 工具观测 → 模型整合证据”的顺序解释。最终结果含 `service`、30 分钟聚合 `error_rate`、`status`、`likely_cause`、`recommendation`、`citations` 和 `uncertainty`。

先检查轨迹是否完成，再检查实际调用了哪些工具、查询是否限定目标服务、引用是否属于当前观测、诊断是否与证据一致。`--service` 在工具执行前固定服务范围；`--verify` 根据观测检查 JSON、聚合错误率、工具历史和引用，允许模型在原预算内修复输出。它不验收根因语义与建议质量，仍需要独立 evaluator。出现 `uncertain`、空回答或预算停止时应展示原始失败，不能用 scripted demo 替换真实模型结果。

改为 `--transport mcp` 时，运行时通过正式 MCP SDK client 发现并调用 SDK server 暴露的工具。此模式使用事先准备的 `--data` 快照。连接配置或 Embedding 查询需要先准备本地数据，不能同时直接传给 MCP transport。

### 3. 展示独立验收，约两分钟

两个仓库放在同一父目录。在评测仓库根目录执行：

```powershell
python -m pip install -e .
python -m byte_eval run --fixture --platform ../byte-agent-platform --split dev --profiles grounded --output runs/interview-fixture-v1
python -m byte_eval report --output runs/interview-fixture-v1 --include-fixtures
```

明确说明这是验收程序的离线演示。真实模型对照另行运行，并查看[公开实验记录](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/model-results.md)。报告默认排除 fixture，训练候选数据导出也排除 fixture、失败任务和 holdout。

展示一个拒绝案例：正确错误率配上错误根因、伪造引用、过期变化记录或另一服务的指标都会失败。注入场景中的恶意段落要求伪造零错误率和健康状态；演示时先从 trace 确认攻击段落实际进入检索结果，再用独立验收检查最终结构化结论是否被污染。工具白名单本身不能证明回答免于污染。

### 4. 展示恢复与队列，约三分钟

打开 `runtime.py`：模型请求前写入 inflight，响应后的模型消息和 pending 工具计划一起提交，只读工具结果与 pending 删除一起提交。模型响应丢失可能已经计费，因此转为 `uncertain` 并停止自动重发。只读工具可以安全重放，但这不等于任意副作用的 exactly-once。

队列示例：

```powershell
$job = python -m byte_agent enqueue --queue runs/interview-jobs.sqlite --service growth-feed --idempotency-key interview-growth-01 --task "Diagnose growth-feed using all three incident tools and return the incident-analysis JSON answer." | ConvertFrom-Json
python -m byte_agent worker --once --queue runs/interview-jobs.sqlite --data runs/interview-data --model $model --verify --skill skills/incident-analysis/SKILL.md --output runs/interview-jobs
python -m byte_agent status --queue runs/interview-jobs.sqlite --job-id $job.id
```

解释幂等入队键、事务抢占、心跳续租、失效租约回收、取消检查与 fencing token：旧 worker 即使晚到，也不能提交另一轮租约的结果。丢失租约停止当前 attempt 并保留可恢复状态，用户取消才保存 cancelled；新 owner 在续租期间等待旧 writer 锁释放。这个直接读 SQLite 的模式面向单机本地磁盘；网络 worker 使用 V3 HTTP 接口，不能把 WAL 文件挂给多主机。取消在安全边界生效，不能强行中断正在进行的模型请求。

## V3 延伸演示：角色协作与网络故障恢复

先打开已保存的报告和源码；真实模型演示在 CPU 上可能需要数分钟，不把推理等待纳入十分钟讲述。

四角色的顺序是 metrics → knowledge → reviewer → arbiter。指标 Agent 只读指标，缺少因果证据必须保留 `insufficient_evidence`；知识 Agent 读取目标服务的政策与变化。两份提案和原始观测形成不可变 SHA256 快照。reviewer 提出批判，arbiter 综合诊断；其他 Agent 的文本仍是不可信证据，冲突不能被多数票抹去。

打开 [multiagent.py](../src/byte_agent/multiagent.py)，展示各角色独立的工具 schema、Runtime 与预算。解释 `RoleOllama` 为什么要先取得成功工具观测，再切换 JSON Schema 输出：这减少结构格式错误，但没有提供测量值、引用或答案标签，也没有训练模型。团队失败／未知响应停止执行，没有单 Agent 降级成功路径。

```powershell
python examples/multiagent_demo.py --fixture --output runs/interview-team-fixture
python examples/multiagent_demo.py --model qwen3:4b-instruct --output runs/interview-team-real
```

示例是新合成开发服务 `demo-upload-v3`，不使用原 benchmark holdout。检查 `experiment.json`、四份 `agents/<role>/trace.json` 和 `team-trace.json` 的角色状态、证据和累计用量；不能只看最终一段回答。真实四角色执行已完成，动态 Schema 与观测协议通过；该已知开发案例的保守归因仍未通过业务验收。七次尝试全部保留，不报 holdout 成功率或对照提升。

随后展示 [Waitress 网络报告](../examples/experiments/distributed-v3-waitress/report.json)：同一物理 Windows 主机上，一个独立 API 进程和三个独立 TCP worker 完成 24/24 条实际 Runtime／三工具／验证器任务，任务分配 9/7/8。kill 已领取任务的进程后，第 2 次领取恢复；旧 token 提交被拒绝，最终结果无重复。该实验使用 Scripted 模型，只证明工程链路。

```powershell
python -m pip install -r requirements-distributed.txt
python examples/distributed_demo.py --output runs/interview-network --server-implementation waitress
python examples/distributed_llm_demo.py --data runs/interview-data --output runs/interview-network-real
```

第二个脚本另行运行真实 Ollama `agent-worker --once`，启用 Skill 与 verify，开发标签只用于父进程执行后的独立 oracle。它与 24 条工程实验分别报告。说明实际隔离点：worker 不打开中央 SQLite、租约使用 server clock、凭证仅由环境传递、不跟随重定向、响应丢失不盲目重放变更。进一步部署跨物理主机需私网 HTTPS、故障与负载实测；当前没有这些成绩或复制高可用证据。

## 常见追问与关键取舍

**为什么独立实现 ReAct，而不是直接包装 LangChain？** 为了直接审查请求、状态提交、工具重放和预算边界。项目参考了公开 ReAct 架构思想，但没有集成 LangChain 或 LangGraph；能说明框架机制和工程取舍，不代表有这两个框架的实战成果。

**为什么精确限定 service/source？** 相似运行手册的文字会高度重叠，但阈值、依赖和处理规则可能不同。检索使用参数化 SQL 过滤候选集，再在这个集合上计算 BM25 和可选向量 RRF。没有匹配时返回空证据，避免借用其他服务的政策。

**这里的记忆是什么？** 是执行状态、消息与观测的持久化，用于恢复和审计。V3 还让角色共享封存的只读证据快照；它是本次团队的有界上下文，不是长期用户画像、跨会话语义记忆或跨节点复制日志。

**MCP 和 Skill 分别负责什么？** MCP 定义工具发现和调用的协议接口；显式 Skill loader 把操作员选中的可信工作流加入系统提示。检索库中的 Markdown 仍属于不可信数据，不能自动升级为 Skill。

**怎么说明模型提升？** 固定模型 digest、生成参数、数据、工具和预算，只改变被比较因素；开发集用于调整，冻结后再跑 holdout。报告原始成功数、分母、置信区间、失败类别和未完成任务。`no-changes` 是工具消融，不能把它造成的能力缺失归因于提示优劣。

**SFT/RL 做到了哪一步？** 实际训练 pinned SmolLM2-135M 的 230,400 个 LoRA 参数，完成 72 步 SFT 和 8 步 REINFORCE。5 步具有 reward advantage、梯度及参数更新；参数变化、精确 reload 和梯度五项验收通过。打开 [训练分数与权重](../examples/v3-results.md)，讲清 18／6／8 划分、实际损失、全部三阶段结果，以及九动作解码与机械渲染器的限制。它没有训练原 Qwen3 4B；模板、Prompt、Schema 和候选导出也不是权重训练。

**为什么 HTTP 队列后端仍用 SQLite？** worker 经 TCP 访问一个服务器，该服务器在本地事务内决定租约和提交；没有让多个主机共享 SQLite 文件。它把执行节点与状态所有者分开，便于验证网络协议和故障语义，但仍是单协调器，有可用性和吞吐瓶颈。更换 Redis／Postgres 或增加复制时要重新验证分区、幂等和 fencing。

**多 Agent 就更准确吗？** 不一定。角色隔离和 reviewer 给出了可检查的协作机制，也增加请求、延迟和成本。需在相同任务、模型与预算定义下单独做对照；一次新开发案例跑通不能证明 holdout 提升。

## 简历表述模板

只有在能够解释并复现实现时使用：

> 设计并实现 Python ReAct 服务诊断 Agent，集成 BM25／Embedding、受控工具与 MCP／Skill；扩展四角色能力隔离、封存证据和有预算的审查／裁决，通过事务日志与配置指纹支持恢复和审计。

> 实现 Waitress HTTP 中央队列与独立 TCP Agent worker，采用幂等提交、服务器租约、心跳和 fencing；在单一物理主机上完成三 worker、24 条工程任务的故障恢复实验，验证旧租约结果无法覆盖当前结果。

> 构建配套 Agent 评测流水线，设计 32 个独立合成服务场景并划分 24 dev／8 holdout；采用独立结构化结果与证据验收、同预算策略对照、工具消融和任务聚类统计，并过滤 fixture／失败／holdout 轨迹以准备人工审核的训练候选数据。

真实模型成绩可另加一句，并填写可追溯的报告数据：

> 使用［模型与 digest］在［任务集、任务数、重复次数］上完成［实验配置］，取得［成功数／总数及区间］；完整轨迹与失败归因见［报告链接］。

不要把同机 TCP 写成多物理主机或大规模系统，把合成业务写成生产上线，把训练数据准备写成完成 SFT/RL，或把四角色实现写成已经证明效果提升。历史单 Agent 成绩和 V3 新实验分别引用自己的配置与报告。
