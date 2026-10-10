# 两个项目做了什么、怎样实现

这是一组互相配合的项目：Platform 执行业务任务，Eval 用独立规则检查任务是否真的完成，并提供训练实验。示例业务是研发服务故障诊断：给定服务、时间窗口、运行手册、指标和变更记录，Agent 查证据、判断状态及可能原因、给出需人工审核的处理建议。全部公开业务资料都是合成数据，没有使用 TikTok 内部系统或公司业务数据。

## 1. 让 Agent 真正执行任务

`runtime.py` 实现 ReAct 的“模型决策 → 工具执行 → 把观测交回模型 → 下一步决策”循环。模型通过 `knowledge_search` 查该服务的手册，通过 `service_metrics` 读取指定时间窗口的真实 SQL 聚合，通过 `incident_changes` 读取当前发布、依赖和流量观测。最终产出包括服务、完整窗口错误率、状态、原因、建议、不确定性和实际引用。

运行前绑定源码、模型 digest、任务、工具 schema、数据内容及预算。请求发出前先记录 inflight，模型返回后事务保存工具计划，再执行工具；同一会话用 OS writer 锁避免两个进程重复推进。模型请求发出后丢失响应会标记 uncertain，防止无依据重试。只读工具可安全重放。步骤、工具次数、tokens 和上下文均有限额；未知用量单独记录。

## 2. 接知识、工具与 Skill

`knowledge.py` 将 Markdown 按段落分块，建立 BM25 索引；可以接入实际 Embedding，用余弦排名与 BM25 通过 RRF 合并。引用 id 绑定内容、相对路径和片段位置，文档更新后旧快照会被替换。查询限定服务和来源，防止混入其他服务的证据。原始 Markdown/SQLite 可以通过显式 connector 配置接入，缺数据会报错。

`mcp.py` 使用官方 Python MCP SDK 提供 stdio 服务与客户端，实际测试初始化、工具发现、调用、错误与硬断连。Skill loader 只加载操作者选定的 Markdown 工作流，不让检索内容自行安装或修改 Skill。工具采用白名单、参数校验、参数化 SQL、只读数据连接和输出大小上限。

## 3. 从单 Agent 到四个协作 Agent

`multiagent.py` 增加固定的专业分工：指标分析者只读指标，知识分析者读手册和变更，审查者检查前两者的证据与推理，裁决者综合原始观测和审查意见。四者使用相同模型权重，但拥有独立上下文、工具权限、执行日志和预算。

它们不共享可修改的聊天记录。前两者完成后，协调器把观测和提议封成以 SHA-256 标识的不可变快照，再授予审查者和裁决者只读访问。全局预算约束全部角色的消耗；失败不会悄悄降级成单 Agent 的“成功”。如果角色提出冲突的因果判断，最终结论要保留冲突并请求继续验证。

真实开发实验暴露了两类问题：非法工具 limit 和不合法 JSON。修正方法是由 operator 固定无必要自由选择的窗口、数量与快照目标，并在实际必需观测齐全后约束输出 JSON 结构。结构约束没有提供实际测量、正确原因或引用答案，独立观测验证仍然拒绝伪造证据。各轮成功或失败均保留，不把开发重试当成随机泛化实验。[设计与复现](multiagent.md)

后续又发现指标角色把工具名称当作引用 ID。指标来自结构化观测，不应伪装成知识库引用；V4 将该角色的最终 citations 限制为空，并提供预算内格式修正及回归测试。这个修复有独立源码 checkpoint，见 [来源元数据](../examples/multiagent-source-v4.json)。修复后的真实多角色结果按实测报告记录，不从工程测试通过推断业务已经成功。

V6 进一步处理语义错误：知识角色没有指标权限，并不表示团队没有指标；整个时间窗口的请求数也不能代表近期流量增长。协调器从实际成功工具事件重算共享 `derived_facts`，列出数据可用性、前后样本数、每观测样本的请求数及其比例，审查者与裁决者先读原观测，再读角色提议。这里不假定采样间隔，不能把该数值称为 RPS 或每分钟请求数。

裁决者只在真实手册明确要求流量增长、指标完整且变更列表未达截断上限时，检查容量判断是否有近期请求增长及当前有效流量引用支持。缺支持时反馈实际数值并让模型重新判断，不提供正确原因，不读取 gold。未知手册或不完整数据不会被补成证据。这项修复、模型元数据读取与对应测试的独立来源见 [V6 checkpoint](../examples/acceptance-source-v6.json)；它不替代独立业务验收。

V6 实测中，审查者把 `service_metrics` 工具名当作未观测的引用 ID，反复修复后耗尽预算。V7 将实际成功工具观测中的引用 ID 列在不可变快照中，只有取齐所需证据后才将这些 ID 绑定到输出 Schema；模型仍需选择相关引用、判断原因和建议。Schema 不写入验收标签、正确原因或测量答案。审查意见为“不同意”时允许重新判断，只有实际专家假设冲突才强制保留冲突。该变更见 [V7 checkpoint](../examples/acceptance-source-v7.json)，之前失败的原始记录继续保留。

新增真实 Multi-Agent 与网络 worker 运行的配置、状态、通过和失败记录统一见 [V3 实测记录](../examples/v3-results.md)，以该报告内的实际证据为准。

V7 的实际结果是：四个角色全部 completed，动态 Schema、精确引用、共享快照和观测检查通过；但该开发案例的业务验收失败。服务、整窗错误率和 incident 状态三项正确，最终却选择了 `insufficient_evidence / collect_evidence / insufficient_data`，没有达到该案例要求的原因与建议。运行用了 9 个模型步骤、5 次工具调用、11,088 个已知 tokens，耗时约 511.455 秒。七次开发尝试全部保留，没有继续针对同一案例调提示追求通过。[V7 原始报告](../examples/experiments/multiagent-v3-compact/report.json) 将 `runtime_completed=true` 与 `semantic_success=false` 分列。

## 4. 从本地队列到网络 worker

`jobs.py` 使用事务、幂等键、租约与 fencing token 管理本机任务。`distributed.py` 在中央服务提供经认证的 HTTP API，远端 worker 通过 TCP claim、续租和提交结果。只有服务端打开它自己的 SQLite，worker 不共享队列数据库文件；每个 worker 拥有本地知识快照和 Agent journal。

租约全部按服务端时钟判断。进程中断后任务可过期重领，新 attempt 获取新的 token；旧 owner 的提交会被拒绝。网络请求丢失响应时不盲目重放修改。真实 Agent worker 接入同一 Runtime、Ollama、Skill、服务权限和结果检查。部署使用 Waitress，提供 Compose 及私网 HTTPS 的跨主机说明。

已记录的同机网络实验使用一个 Waitress 服务进程和三个独立 worker，24 个 Scripted 工程任务全部完成；终止已领取任务的进程后恢复到第 2 次领取，旧 token 提交被拒绝。Scripted 验证工程行为，不提供模型效果分数。跨主机、容器与真实模型执行分别记录，不能从部署配置推断它们已经实测。[部署与故障语义](distributed.md)

真实 Qwen 网络 worker 另行完成 `creator-upload` 开发任务，3 次工具调用、1 次输出验证，执行结束后的独立 oracle 全部通过，记录 4,067 个已知 tokens、331.411 秒。其运行和评分数据分开，worker 不接收标签 manifest；一个选定开发案例不能代表总体准确率。[真实 TCP 报告](../examples/experiments/distributed-v3-real-complete/report.json)

Windows 本机 Docker 启动失败的记录继续保留。后续 [GitHub Ubuntu 容器 CI](https://github.com/chendi-Shi/byte-agent-platform/actions/runs/37918991758) 在候选提交 `a14ea74845dd3e2a3467955bd2d1fd7e6e5bdff2` 构建镜像、运行一个队列和两个 demo worker 容器，完成 12/12 条数值夹具任务。原始记录显示 12 条都由同一个 worker 完成，不能声称公平分配或并行提速。它证明同一 runner 上的真实容器集成，未调用 LLM，也不是多物理机压测；最终 main CI 另行核验。[原始报告与分配](../examples/experiments/container-ci-v3/report.json) 和 [来源回执](../examples/experiments/container-ci-v3/provenance.json) 已保留。

## 5. 怎样判断结果正确，并实际训练

[Byte Agent Eval](https://github.com/chendi-Shi/byte-agent-eval) 的 oracle 独立读取验收标签。Agent 不接收这些标签；评测结束后才检查结构化决策、三类工具观测、时间窗口、目标服务、精确引用和业务语义。平台的输出 validator 检查格式与观测一致性，不能代替业务 oracle。

先比较相同模型、预算、工具和种子的提示；再做移除 changes 工具的消融。移除必需因果工具会机械违反完整验收，所以报告另列决策指标。后来增加服务 scope 与验证反馈，是另一种 treatment，不把跨配置成功率相减伪称为纯提示收益。每条实验保存配置、源码/模型指纹、trace 校验和、真实 tokens 与耗时，失败和中断也公开。

训练部分用固定的 SmolLM2-135M-Instruct，实际更新 attention 的 LoRA 权重：SFT 学习开发集的监督动作；REINFORCE 让模型采样多步工具行为，按实际执行和独立终局评分计算奖励，再反向更新模型。训练、验证和公开测试服务分开；三个阶段使用相同的评测任务。保存初始 adapter 作为未训练对照；SFT 与 RL 阶段分别要求参数 hash 改变并能在扰动后从文件完整恢复，RL 还要求真实非零奖励梯度。

这是 CPU 上可复现的受限工具动作策略：服务和合法参数由操作者固定，模型选择工具与诊断，程序只机械填充实际数值和引用。它不等同于 Qwen3 4B 的自由 JSON 任务表现。训练报告、adapter 和逐步日志以实际运行结果为准。[评测与训练详解](https://github.com/chendi-Shi/byte-agent-eval/blob/main/docs/project-walkthrough.zh-CN.md)

完整训练实际更新 230,400 个 LoRA 参数，完成 72 次 SFT optimizer update 和 8 次 RL group update，每组采样 4 条 episode；5 组具有有效奖励梯度，另 3 组 advantage 为零。18 个开发训练服务、6 个开发验证服务与 8 个公开留出服务在运行前冻结隔离。

| 阶段 | 验证任务完整成功 | 公开留出任务完整成功 | 三类工具观测齐全 |
|---|---:|---:|---:|
| Base | 0/6 | 0/8 | 0/14 |
| SFT | 3/6 | 3/8 | 14/14 |
| SFT + REINFORCE | 2/6 | 3/8 | 14/14 |

SFT 后工具覆盖达 14/14，但完整诊断仍会错误。这次 RL 没有提升留出表现，验证集还发生退化，失败仍保留。原 Qwen verified 公开留出 7/8 是另一个自由 JSON 模型实验，不能与这个 135M 受约束策略的 3/8 相减归因。详细证据见 [训练报告](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/report.md)。

公开 initial、sft、reinforce 三个 adapter 以及配置、权重哈希和日志；SFT 与 RL 的权重改变、真实奖励梯度、扰动后重新加载恢复等五项校验全部通过。随后独立加载 RL 文件，不进行优化器更新，14/14 任务与原来的动作、决策及 oracle 结果匹配，参数哈希保持不变。**这是复现匹配 14/14；业务完整成功是 5/14（验证 2/6、留出 3/8）**，其中原来的失败也被复现。[消费者报告](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/adapter-consumer-v3/report.md) 记录这项检查。按开发验证选择演示策略时保留 SFT（3/6）优于本轮 RL（2/6），RL 作为研究对照，不根据公开留出再调参。

训练仍绑定历史 platform `10957dfa69c7a2fd33cf12e30066ace883927f9a` 和 eval `24717de469c2e75223f2c1fa3b853a35a8e98fb2`。两个仓库的 `examples/training-source-v3/src` 共归档 26 个原始源码文件，供严格复现使用；后来的 Multi-Agent 与模型元数据读取修复没有改写训练来源。下载固定基础模型、校验 receipt、设置 archive 路径和消费公开 adapter 的命令见 [训练复现说明](https://github.com/chendi-Shi/byte-agent-eval/blob/main/docs/training.md)。

## 6. 怎样在面试中演示

先在平台根目录安装核心项目，用不需要模型的工程演示检查数据流：

```bash
python -m pip install -e .
byte-agent demo --output runs/walkthrough-demo
```

它使用 Scripted provider，适合检查工具、journal 和 trace。要看真实模型协作，先按 [模型准备步骤](../README.md#安装与演示) 下载 `qwen3:4b`，并用 `prepare-model` 创建本地 `qwen3:4b-instruct` 标签；后者不是公开下载标签。Ollama 服务启动后运行：

```bash
python examples/multiagent_demo.py --model qwen3:4b-instruct --context 8192 --output-tokens 512 --provider-timeout 900 --wall-seconds 5400 --output runs/walkthrough-multiagent
```

需要演示网络队列恢复，可另开工程目录：

```bash
python -m pip install -r requirements-distributed.txt
python examples/distributed_demo.py --jobs 24 --server-implementation waitress --output runs/walkthrough-network
```

该命令启动同机独立 API 与 worker 进程，使用 Scripted 工程任务；读取报告中的恢复 attempt 和 stale fencing token 拒绝，再到 [分布式说明](distributed.md) 查看真实模型 worker 与跨主机部署入口。部署配置和软件演示不替代真实模型、容器或多主机实测。

面试演示时，沿一条真实任务展示模型动作、工具参数、观测、最终引用与业务 oracle；再展示失败并解释系统为何拒绝；随后展示 worker 失联后的租约恢复；最后在 eval 加载公开 SFT/RL adapter，比较相同任务的前后结果及参数校验。对应记录见 [V3 实测报告](../examples/v3-results.md)，早期单 Agent 与 Embedding 记录见 [历史实测报告](../examples/model-results.md)。

## 7. 怎样对应岗位 JD

| JD 方向 | 本项目做了什么 | 另一个仓库怎样衔接 |
|---|---|---|
| Agent 架构与基建 | ReAct Runtime、四角色独立上下文、共享只读证据快照和预算 | 独立 oracle 判断最终任务 |
| Agent 应用落地 | 研发故障诊断，从指标、手册和变化到处理建议 | 合成业务案例和失败分析 |
| 工具与知识系统 | 参数化 SQL、BM25/可选 Embedding、MCP、显式可信 Skill | 工具完整性、引用和来源检查 |
| Agentic Eval/SFT/RL | 保存真实执行轨迹和候选导出输入 | 对照、消融、实际 LoRA 训练和奖励优化 |
| 安全、可用性、扩展性 | 白名单、输出上限、durable journal、uncertain、HTTP worker、租约/fencing | 校验原始证据、配置和源码指纹 |

可以用“执行 → 验收 → 发现失败 → 修复或训练 → 同任务再评测”解释端到端流程。当前数据是公开合成业务，队列和训练实测主要在单机 CPU 完成；这些记录展示工程与方法能力，生产集群吞吐及 SLA 需要进一步测试。

重点讲清楚三个区别：执行结束和业务正确不是同一件事，结构化输出和模型训练不是同一件事，网络 worker 原型和生产规模验证不是同一件事。这样能把架构、工具与知识、评测、训练和可靠性串成一个可检查的工程项目。
