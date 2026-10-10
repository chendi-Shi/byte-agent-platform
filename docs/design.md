# 设计、来源与执行边界

平台把服务诊断分成三层：模型决定下一步，受控工具返回观测，确定性程序检查执行与输出契约。配套评测仓库再独立验收业务结论。示例资料是合成数据，真实模型配置、结果和失败记录见[公开实验记录](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/model-results.md)。

## 开源研究与许可来源

2026-10-08 通过 GitHub 插件检索并阅读了以下设计来源：

- [langchain-ai/react-agent](https://github.com/langchain-ai/react-agent)：参考 model → tools → model 的 ReAct 路由。阅读了 README 和 `src/react_agent/graph.py`；当时该文件的 blob SHA 为 `e41ee1ae8090e7d226e5f5f912bfe0e63b29cbe1`。
- [THUDM/AgentBench](https://github.com/THUDM/AgentBench)：参考函数调用环境与任务结果验收的分离。没有运行其官方多环境基准或引用其成绩。
- [ethz-spylab/agentdojo](https://github.com/ethz-spylab/agentdojo)：参考将正常任务效果与 prompt injection 攻击分别观察的思路。没有移植其任务集、攻击集或防御实现。

平台与评测代码是独立实现，没有复制这些仓库的代码或数据；外部项目的许可证以各自仓库为准。本仓库的 [MIT 许可证](../LICENSE)覆盖本仓库原创内容，不能替代外部依赖的许可证。正式 MCP 互操作通过可选的官方 Python SDK 完成，其依赖声明见 [pyproject.toml](../pyproject.toml)。

选择轻量 Python 运行时，是为了直接检查状态迁移、工具执行、请求丢失和恢复语义。V3 增加四角色 Multi-Agent 与 HTTP 中央队列，复用已有 Runtime、工具和验证器；没有 LangChain／LangGraph 适配器或 GraphRAG。

## 数据与诊断任务

`domain.create_dataset(root)` 生成 32 个独立虚构服务，其中 24 个开发任务、8 个不同服务的 holdout 任务；两组服务名称和观测分布分离。每个服务有自己的错误率预算、延迟限制、业务描述与依赖，只有一个场景刻意缺少手册。场景覆盖发布回归、依赖故障、容量压力、健康服务、指标缺失、手册缺失、相互矛盾的原因报告和恶意检索指令。

运行手册描述判断规则，当前发布与依赖事实仅在 `changes` 表中。这样模型需要结合指标、政策和变化观测完成多步判断，而不能从某段手册直接读出场景答案。`services.json` 含 evaluator 的标签，工具与知识索引不读取它。公开 holdout 用于实验流程隔离，不是保密测试集。

最终答案使用固定 JSON 字段：`service`、30 分钟聚合 `error_rate`、`status`、`likely_cause`、`recommendation`、`citations` 和 `uncertainty`。原因与建议使用有限分类，便于验收；`causality_unproven` 保留“证据支持假设，但相关性没有证明因果”的区别。分类定义见 [domain.py](../src/byte_agent/domain.py)。

`FileSQLiteConnector` 接入操作者指定的现有 UTF-8 Markdown 与 SQLite 文件。配置只有 `knowledge_root` 和 `metrics_database`，相对路径以配置文件所在目录为基准。先验证目录、表结构和基础计数约束，再把 Markdown 写入单独的知识索引；源 SQLite 用 `mode=ro` 打开。缺失或不兼容输入报错，不自动补入合成数据。详细 schema 见[数据契约](../examples/corpus/README.md)。

## 知识检索与工具抽象

Markdown 按段落分块，每块最多 1000 字符；Latin 词项和中文 bigram 用于词法索引。片段 id 绑定相对路径、段落位置、片段偏移和文本。重新索引以事务替换快照，已删除资料不会继续被召回。`service` 可由 frontmatter、子目录名称或平铺文件名获得。

检索先用参数化 SQL 对 `service`／`source` 精确过滤候选集合，再做 BM25 排名。可选 Ollama Embedding 在入库时生成向量、查询时生成 query 向量，使用余弦排名和 Reciprocal Rank Fusion 合并两类排名。向量数量、维度和有限数值有基础校验；更换 Embedding 模型必须重建并使用相同模型查询。没有过滤匹配时返回空集合，不能退回另一服务的政策。

统一注册表暴露三个只读工具：

| 工具 | 观测内容 | 设计约束 |
|---|---|---|
| `service_metrics` | 完整窗口、baseline／recent 错误率、请求量、最大 p95 与最新 minute | 服务名参数绑定；minute 范围限定；缺失值保留 null |
| `knowledge_search` | 运行手册片段、来源与引用 id | 精确服务／文档过滤；文本标记为不可信证据 |
| `incident_changes` | 发布、依赖、流量和遥测变化；状态、时间、单条证据 id | 只读预定义查询；当前事实与 stale 条目明确区分 |

`window_minutes` 按目标服务最新 minute 计算闭区间 `[latest - window + 1, latest]`，避免缺分钟时把很早的样本凑入窗口。baseline／recent 仍按返回样本的前后两半汇总；完整连续分钟序列与原实现保持相同输出。minute 是共享的分钟标识符；真实遥测是否对应当前墙钟时间，仍需调用者建立可信时间基准。

工具校验必要参数、类型、枚举、长度和结果大小。没有任意 SQL、Shell、部署或消息发送工具。`--service` 进一步由操作者固定目标服务：三个诊断工具的 service 变为必填单值枚举，错误服务在 handler 访问数据之前被拒绝；失败尝试仍记录在轨迹里。

## Durable ReAct 与可选答案检查

执行主线为：

```text
running → model inflight → model decision committed
        → pending tool observations → proposed answer
        → completed / validation feedback / terminal failure
```

模型请求前保存 `inflight=true`。模型消息、用量与 pending 调用一起提交；每次只读工具的结果、消息与 pending 删除一起提交。进程在工具结果提交前退出，工具可重放。模型响应丢失可能已经计费，因此保存为 `uncertain` 并停止自动重发；恢复发现 inflight 时采用相同策略。

一个 run 使用 OS 文件锁限制单写者。`RuntimeBusy` 表示当前尚有 writer，不代表 Agent 已执行失败。run 身份绑定任务、模型名与 endpoint、模型 digest 与生成设置、系统提示、工具 schema／revision、知识和指标内容、源码、预算，以及可选 validator revision。身份不一致拒绝恢复。直接使用自定义模型或 validator 的调用者需要提供稳定 revision；运行期间不得修改源数据或替换服务权重。

`--verify` 给 run／worker 启用可选 `answer_validator`：检查固定 JSON 字段、枚举、目标服务、成功的三工具历史、完整 30 分钟聚合错误率和实际返回的目标服务证据 id。validator 只读取观测与操作者选择的 service，不读取 benchmark 标签，不生成答案或隐藏的根因提示。

当前 `--verify` 的知识引用契约要求 `service/runbook.md` 等按 service 分目录的布局；普通 `--service` 支持 connector 的平铺文件与 frontmatter 服务标记，生成的合成案例均采用目录布局。

可修复错误生成确定性反馈，交回模型继续执行，仍使用原有步数、调用数和 token 预算；每次检查保存 validation event。已经执行的错误服务或失败工具历史不能被后续回答抹掉，在严格模式下直接结束为 `validation_failed`。validator 自身异常结束为 `validation_error`。模型、工具与验证反馈的所有重试仍受统一预算约束。

这个 runtime verifier 检查输出契约和观测使用，不判断根因语义、建议是否合理或某条变化是否真正造成事故。配套 evaluator 使用独立标签进一步验收 status、cause、recommendation、uncertainty、当前变化记录与相应引用。因此开启 `--verify` 的 `completed` 仍不能替代业务成功率验收。

## 四角色编排与结构化输出

[multiagent.py](../src/byte_agent/multiagent.py) 实现 metrics → knowledge → reviewer → arbiter 的顺序编排。每个角色有独立模型适配器、提示、能力集合和 durable Runtime；权重可以共享。metrics 只能读取 30 分钟指标，knowledge 只能检索目标服务手册并读取变化记录。操作者固定窗口、检索数量与变化条数，模型不能覆盖这些参数。metrics 缺少政策和变化观测，因此其因果提案必须是 `insufficient_evidence`。

两位专家的成功工具观测与提案组成不可变 JSON 快照，按 SHA256 保存。reviewer 通过 `read_shared_evidence` 审查这份快照；arbiter 读取包含 reviewer 意见的新快照。快照 id 由操作者绑定到工具，无需模型重建 hash。角色输出和检索内容继续被视为不可信证据。恢复时检查已封存文件是否仍匹配 digest；缺失或篡改结束为 `evidence_error`。

`RoleOllama` 分为两个阶段：先保留原生工具调用；只有必需工具返回成功且服务／窗口／快照校验匹配后，才关闭该阶段的工具 schema，并向 Ollama 提供角色最终 JSON Schema。schema SHA、结构化阶段条件和每次请求的 `response_mode` 被记录。约束解码只限制字段、类型和枚举，不填写实际错误率、引用或根因标签。它既不是模型训练，也不能替代语义评测。

角色 validator 检查必需工具、能力边界、目标服务、观测引用及各角色的结构化契约。arbiter 根据专家的实际 observation 验证最终诊断；声明的提案冲突或 reviewer 的 `disagree` 必须保留 `conflicting_evidence` 与 `verify_changes`。它不通过多数投票抹去冲突，也不把多个 Agent 的同意视作因果证明。

Coordinator 保存团队状态、已完成角色、角色预算和快照身份，并持有独立 OS 锁。团队与角色都有步数、调用、token、上下文和时间边界；请求 timeout 受剩余团队时间限制。失败、未知 provider 响应、取消、超时或预算耗尽停止团队，不退化成单 Agent 并声明成功。当前角色顺序执行，未提供角色并行或跨节点团队日志复制。

[multiagent_demo.py](../examples/multiagent_demo.py) 使用独立的新合成开发场景 `demo-upload-v3`，不读取已有 benchmark 的 holdout 标签。Scripted 路径用于工程验证；[真实四角色报告入口](../examples/experiments/multiagent-v3-compact/report.json) 四角色执行协议已完成；该开发案例语义检查 3/6、整体未通过。全部七次开发尝试保留，不宣称模型效果提升。

## MCP 与显式 Skill

正式 stdio server 使用官方 MCP Python SDK，完成生命周期协商、工具发现、调用和标准错误返回。异步 client 处理发现与分页，同步桥在独立线程内保持 SDK task group 的上下文，让同步 Runtime 通过 Future 调用远端工具。只有调用者明确允许的工具会接入注册表；server 的 read-only annotation 本身不构成授权。

SDK 传输异常归一化为 `MCPToolError`，作为 Runtime 的工具错误记录；正常结果优先读取 `structuredContent`。远端工具 revision 需要包含实际数据快照身份，只有服务启动命令和 schema 不能证明数据相同。CLI 的 `--transport mcp` 使用已准备的 `--data` 快照；该路径不能同时直接指定 connector 或 Embedding 查询模型。旧 JSON-lines 子集保留离线兼容用途。

显式 Skill loader 读取操作者选中的 Markdown，校验 name／description frontmatter、大小和正文，再把 name 与 instructions 加入系统提示。原始文件 SHA 用于检查文件标识；执行身份实际绑定解析后的 name 和 instructions，不绑定全部 frontmatter 或原始字节。检索文本不会安装或发现 Skill，也不能变成系统指令。

## Lease 队列与取消语义

SQLite 本地队列通过 `BEGIN IMMEDIATE` 原子 claim，通过持久化幂等键与任务／payload 指纹防止同键提交不同任务。每个 attempt 有新的 fencing token、租约截止时间和尝试计数；后台心跳为阻塞模型请求续租。只有当前、未过期的 token 可以完成或失败任务。

用户取消和丢失租约采用不同处理。用户取消在 Runtime 安全边界保存 `cancelled`；丢失 owner 或租约失效抛出 `LostLease`，停止旧 attempt，保留可恢复 journal，不把任务永久改成用户取消。替代 worker 若遇到旧 writer 的 `RuntimeBusy`，在维持租约的同时等待锁释放，避免短暂锁争用立即耗尽尝试上限。已经发出的模型请求不能强行打断；旧 token 不能覆盖新 owner 的结果。

队列 `completed` 表示 callback 已交付结果，需要继续检查 `result.agent_status` 与 `requires_review`。`uncertain` 的模型 run 不会因为 queue 重领而自动再请求。这个直接读 SQLite 的队列只用于同一主机的本地磁盘；旧 API 保留，跨主机 worker 通过下面的网络接口接入。

## HTTP 中央队列与远端 Agent worker

[distributed.py](../src/byte_agent/distributed.py) 把 JobQueue 的事务和租约判断留在中央 API。只有 API 进程打开本地 SQLite；HTTPJobQueue 通过 TCP 发送受限 JSON 请求。claim、heartbeat、check、complete、fail 与 cancel 使用服务器时间和当前 fencing token，worker 的墙钟不参与所有权判断。

默认只绑定 loopback，必须由环境注入 32–512 字符 bearer secret。请求有大小、JSON 和字段白名单约束；服务不执行上传的 Python 或 shell。客户端拒绝带 URL 凭证的 origin，不跟随重定向，不自动重发结果未知的变更请求。一个共享 token 授予操作者能力，没有租户隔离。跨主机部署需由操作者配置私网防火墙和 HTTPS 代理；Waitress 本身不提供 TLS。

HTTPWorker 领取任务后在后台续租。`AgentJobHandler` 用每个节点自己的知识／指标快照、模型 endpoint、Skill 和独立 job 目录运行实际 Runtime；payload 仅允许 service，不能选择模型、代码或文件路径。服务范围冲突被拒绝。队列结果记录 Agent 状态和需审核标记，完成 callback 仍不等于业务诊断正确。

网络分区或响应丢失可能使一次队列变更的提交结果未知；客户端保持这个区别。不同节点恢复时可以从头重做只读观测，但不能自动恢复另一节点的本地 journal。需要跨节点持久化 trace 时应另建 artifact store。任务执行为 at-least-once；fencing 只保护队列结果，任意外部副作用还需要目标系统的幂等协议。

部署使用可选 Waitress 3.0.2；stdlib WSGI server 仅用于本地示例。[Compose](../compose.yml) 已通过配置校验。Windows 本机 Docker Desktop 后端启动失败的记录保留；后续 [Ubuntu 候选 CI](https://github.com/chendi-Shi/byte-agent-platform/actions/runs/37918991758) 实际构建镜像并运行队列与两个 demo worker 容器，完成 12/12 条数值夹具；这项容器集成不调用 LLM。真实 Waitress 与三个独立 TCP worker 的工程实验完成 24/24 条实际 Runtime／工具／验证器任务，任务分配 9/7/8；kill 后第 2 次领取恢复，旧 token 提交拒绝，最终结果无重复。模型为 Scripted，不构成 LLM 准确率。

见 [网络工程报告](../examples/experiments/distributed-v3-waitress/report.json) 与 [部署说明](distributed.md)。[真实 Ollama 网络 worker 报告入口](../examples/experiments/distributed-v3-real-complete/report.json) 已完成并通过独立 oracle；其脚本把 `services.json` 留给执行结束后的父进程评分，不传给 worker。

## 验证范围与后续工作

工程验收覆盖数据与引用边界、知识更新、预算、模型响应丢失、durable pending 恢复、MCP SDK 互操作、租约与取消、分钟窗口、角色能力与快照边界，以及 HTTP 凭证／重定向／故障恢复。HTTP 新增 12 项测试已通过。具体执行记录以 CI、测试输出和[真实模型实验报告](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/model-results.md)为准；旧单 Agent 的 2/2 开发回归和 7/8 留出成绩保留其原配置，不转写为 V3 的模型成绩。

当前是有限合成服务诊断任务集、四角色编排与单协调器网络执行。实测只有一个物理 Windows 主机，没有真实多物理主机部署、复制高可用、生产上线、大规模性能／SLA、多租户授权或长期用户记忆证据。训练候选导出、模板修复和结构化解码不属于权重训练；配套仓库另行完成实际 135M LoRA SFT／REINFORCE；[V3 结果](../examples/v3-results.md)展示权重、损失与三阶段受约束策略评测，不能作为原 Qwen 或生产系统提升证据。

后续扩展应先确定可验证标准：固定模型与预算后比较单／多 Agent 的效果和成本；固定数据后测检索质量；引入更多真实数据源时验证权限与新鲜度；引入 PostgreSQL／Redis、消息中间件或复制协调器时验证多主机故障、取消、幂等与负载。部署配方、工程实验和模型／训练结果分别保留自己的证据。
