# V3 实际结果与证据

以下数字从完整的公开原始报告、日志、checkpoint 和评测记录读取；先核对完成状态、非 fixture 标志及 SHA256，再生成本文。历史 V2 单 Agent、Embedding、提示／消融结果保留在 model-results.md。

候选 commit 的 GitHub CI：平台在 Windows、Ubuntu 各 140 项通过且无跳过；评测核心在两系统各运行 51 项，其中 44 项通过、7 项因训练依赖跳过。独立 training-contracts job 实际通过 9 项训练环境测试和 7 项策略测试；后者逐个覆盖核心跳过项，合并为 51 个不同测试，重复测试不累加。这些是候选 CI 结果；最终 main CI 另行核验，本机失败记录保留。[完整测试回执](validation-v3.json) 从实际 CI 日志核对数量、耗时、跳过项覆盖和成功终态，并记录规范化日志 SHA-256；模型效果按下列实验单独验收。

## 四角色真实模型

实际模型 `qwen3:4b-instruct`，digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`。metrics → knowledge → reviewer → arbiter 四个角色均完成，fixture=false；执行、动态 Schema、精确引用和封存证据检查通过。新合成开发场景 `demo-upload-v3` 的六字段执行后检查为 3/6，整体业务验收**未通过**。3/6 是一个任务的字段检查数量，不能解释成任务准确率 50%。耗时 511.455 秒。`completed` 表示执行协议完成，不能代替语义成功。这是已知开发案例的负面或正面结果记录，不等同于总体准确率，也不证明优于单 Agent。

| 字段 | 实际输出 | 独立开发检查 |
|---|---|---|
| service | `demo-upload-v3` | 通过 |
| error_rate | `0.023` | 通过 |
| status | `incident` | 通过 |
| likely_cause | `insufficient_evidence` | 未通过 |
| recommendation | `collect_evidence` | 未通过 |
| uncertainty | `insufficient_data` | 未通过 |

| 角色 | 状态 | 模型步 | 工具调用 | 已知 tokens | 未知用量 |
|---|---|---:|---:|---:|---|
| metrics | completed | 2 | 1 | 1198 | 0 |
| knowledge | completed | 2 | 2 | 1789 | 0 |
| reviewer | completed | 2 | 1 | 2536 | 0 |
| arbiter | completed | 3 | 1 | 5565 | 0 |

[报告](experiments/multiagent-v3-compact/report.json) · [实际模型与角色配置](experiments/multiagent-v3-compact/experiment.json) · [团队原始 trace](experiments/multiagent-v3-compact/team/team-trace.json)。

V7 知识角色错误声称指标未超阈值，审查者用原始指标纠正了这一点。裁决者先输出近期错误率和无流量增长支持的容量归因，被确定性验证器拒绝；随后修正了整窗错误率，却选择证据不足、继续收集和数据缺失，仍未达到该案例的业务契约。没有继续针对这一已知案例调整提示追求正确答案，验收标签也未改变。

七次开发运行全部保留，包括执行失败、完成但诊断错误的记录。这些有开发选择偏差的重试不能当作随机抽样准确率或多 Agent 优于单 Agent 的比较结果。

| 保留的开发运行 | 执行状态 | 相同开发决策检查 |
|---|---|---|
| [multiagent-real-v3](experiments/multiagent-real-v3/report.json) | role_failed | 未通过 |
| [multiagent-real-v3-narrow](experiments/multiagent-real-v3-narrow/report.json) | role_failed | 未通过 |
| [multiagent-v3-structured](experiments/multiagent-v3-structured/report.json) | role_failed | 未通过 |
| [multiagent-v3-grounded-schema](experiments/multiagent-v3-grounded-schema/report.json) | completed | 未通过 |
| [multiagent-v3-provisional](experiments/multiagent-v3-provisional/report.json) | completed | 未通过 |
| [multiagent-v3-observed](experiments/multiagent-v3-observed/report.json) | role_failed | 未通过 |
| [multiagent-v3-compact](experiments/multiagent-v3-compact/report.json) | completed | 未通过 |

当前运行源码与不可变 GitHub checkpoint 的四文件审计见 [acceptance-source-v7.json](acceptance-source-v7.json)；训练仍绑定其历史源码归档，后续运行时修复不会改写原训练指纹。

## Waitress 与真实 TCP Agent worker

工程实验：一个独立 Waitress 服务端与三个独立 TCP worker 完成 24/24 条 Scripted ReAct Runtime／三工具／验证器任务；分配为 `worker-0=8, worker-1=9, worker-2=7`，耗时 113.839 秒。终止领取任务的进程后第 2 次 claim 完成恢复；旧 token 的迟到提交被拒绝，最终结果无重复。

真实模型实验：`qwen3:4b-instruct`（digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`），独立 Waitress 服务端和独立 Ollama Agent worker 在 TCP 队列执行开发案例 `creator-upload`，Skill、verify 与执行后独立 oracle 全部通过（fixture=false）。模型 2 步、工具 3 次、输出验证 1 次，已知 tokens 4067，未知用量 `0`，耗时 331.411 秒。worker 只收到 prompt 与 service，标签 manifest 留在父进程评分路径；一个选定开发案例不构成总体准确率。

[24 条工程任务与故障恢复](experiments/distributed-v3-waitress/report.json) · [真实 worker 报告](experiments/distributed-v3-real-complete/report.json) · [独立 oracle](experiments/distributed-v3-real-complete/oracle.json) · [模型、Skill 与源码配置](experiments/distributed-v3-real-complete/experiment.json)。

两个网络实验均只有同一物理 Windows 主机上的独立进程，经过真实 TCP。未执行多个物理主机、复制高可用或生产规模压测。SQLite 只由协调器在本地持有；执行副作用为 at-least-once，fencing 只约束当前 owner 的队列结果提交。

另行执行的 [GitHub Ubuntu 容器 CI](https://github.com/chendi-Shi/byte-agent-platform/actions/runs/37918991758) 已在候选提交 `a14ea74845dd3e2a3467955bd2d1fd7e6e5bdff2` 构建镜像，启动一个队列和两个 demo worker 容器，完成 12/12 条显式合成数值任务。原始分配记录为全部 12 条由同一个 worker 完成，不能声称公平分配或并行吞吐提升。该检查为单个 Ubuntu runner 上的真实容器集成，fixture=true、real_llm=false，不属于 LLM 准确率或多物理机验证。Windows 本机 Docker 后端启动失败的原始记录仍保留；候选分支 CI 与最终 main CI 分别核验。[原始容器报告](experiments/container-ci-v3/report.json) · [commit／run／artifact 来源](experiments/container-ci-v3/provenance.json)。

## 实际 LoRA SFT 与 REINFORCE

实际训练模型为 `HuggingFaceTB/SmolLM2-135M-Instruct`，revision `12fd25f77366fa6b3b4b768ec3050bf629380bac`；LoRA rank 4，可训练参数 230,400。SFT 72 步（batch 4，lr 0.001），REINFORCE 8 步（group 4，lr 0.0001，entropy 0）。

SFT loss 首步／末步／均值为 `3.44924/0.0019207/0.815568`；RL loss 为 `0.0610409/0/-0.186929`。5/8 个 RL 步骤具有非零 reward advantage、梯度并实际改变参数。SFT 与 RL 参数变化、两阶段扰动后精确加载和 reward 梯度五项检查全部通过。


| 阶段 | 分组 | 完整契约通过（含渲染器） | 决策正确 | 三工具齐全 | 平均 reward |
|---|---|---:|---:|---:|---:|
| base | 开发验证 | 0/6 | 0/6 | 0/6 | -0.633 |
| base | 公开留出 | 0/8 | 0/8 | 0/8 | -0.450 |
| sft | 开发验证 | 3/6 | 3/6 | 6/6 | 0.800 |
| sft | 公开留出 | 3/8 | 3/8 | 8/8 | 0.675 |
| reinforce | 开发验证 | 2/6 | 2/6 | 6/6 | 0.633 |
| reinforce | 公开留出 | 3/8 | 3/8 | 8/8 | 0.675 |


这项评测使用原始词表中九个动作 token、操作者绑定的工具参数及机械数值／引用／JSON 渲染器。18 个服务用于训练，6 个用于开发验证，8 个公开留出只用于评测；72 条监督记录标记为 synthetic-supervised。它衡量 135M 受约束工具策略加渲染器，不能与自由生成答案的 Qwen3 4B 分数直接相减。表中零提升或下降也保留；小样本单次运行不证明生产泛化。

| checkpoint | 参数 SHA256 | adapter 文件 SHA256 | 权重与说明 |
|---|---|---|---|
| initial | `5ccfe6b7912f75a631897f281127d6029c0f736cb022434b27c454e62b222c72` | `5a50515d4764f29b65e6baf0ae55d4f3b6856b9d9b523953878f3febe42274ec` | [adapter](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/checkpoints/initial/adapter_model.safetensors) · [元数据](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/checkpoints/initial/weights.json) |
| sft | `f744026542e6676fac01dae20087246bc1b78530386ead9a5b37c8858052c4c9` | `2d4fd5e972e04ca7b1c0279bd0877095985fd0b208635665eda75cb4c7c6f2fd` | [adapter](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/checkpoints/sft/adapter_model.safetensors) · [元数据](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/checkpoints/sft/weights.json) |
| reinforce | `a4fd3641e75d2f4b4188289794431be654ec37e4cd2d13e57ae83fb7c84ac6e2` | `80e0d44a3be4b7ada64024ba8b57c11a3acb09a4159894a50cf0d132f63927b0` | [adapter](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/checkpoints/reinforce/adapter_model.safetensors) · [元数据](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/checkpoints/reinforce/weights.json) |

公开 reinforce adapter 随后在全新 consumer 进程中重新加载：文件／元数据 SHA256 与加载后参数 hash 均匹配，optimizer_steps=0，评测前后参数 hash 不变。14 个任务的动作、决策和全部 oracle checks 与原结果一致，包括原失败；这是复现一致性，不是 14/14 业务通过。耗时和浮点概率没有宣称逐位一致。[consumer summary](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/adapter-consumer-v3/summary.json) · [14 条 consumer 评测及 trace](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/adapter-consumer-v3/reinforce-evaluation.json)。

26 个冻结训练源码 SHA256 已绑定并核对不可变 Git commit 与实际 blob：[源码绑定](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/v3-source-binding.json)。


配置 hash：`5023bce6122e40a42e25679a501003c69b07b19ff533e7d39f47084cc038aecb`。

[summary](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/summary.json) · [manifest](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/manifest.json) · [实际训练日志](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/training-log.json) · [全部训练报告](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/experiments/training-v3/report.md)。
