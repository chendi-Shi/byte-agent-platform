# 实测记录

本项目与 [Byte Agent Eval](https://github.com/chendi-Shi/byte-agent-eval) 配套。下列记录来自 2026-10-09 本地 CPU 实验；所有业务输入均为合成资料。完整配置、逐任务得分、成功和失败 trace 见 [评测报告](https://github.com/chendi-Shi/byte-agent-eval/blob/main/examples/model-results.md)。

| 验证 | 实际结果 | 范围 |
|---|---|---|
| 平台测试 | 77 项通过，无跳过 | 官方 MCP SDK 互操作、断连、服务隔离、索引、恢复、跨进程任务租约与取消 |
| 配套评测测试 | 35 项通过 | 独立 oracle、统计、trace 校验、SFT 导出与数值边界 |
| 真实 Qwen3 4B，verified 开发回归 | 2/2 完整验收成功 | 开发失败选出的两个任务，有选择偏差 |
| 真实 Qwen3 4B，verified 留出集 | 7/8，87.5%，套件完整 | 8 个独立服务；描述性 95% Wilson 区间 52.9%–97.8% |
| 真实 BGE-M3 Embedding | 5 个片段，1024 维；RRF 检索完成 | 执行链路 smoke，无检索质量比较 |
| SFT 候选导出 | 两个开发实验合计 8 条 | 实际成功 dev 轨迹，需人工审查；无 holdout、fixture 或失败轨迹 |

Qwen3 模型为 `qwen3:4b-instruct`，digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`，Ollama 0.34.4，Q4_K_M，CPU，seed 42，temperature 0，context 3072，output 384，timeout 300 秒，4 threads，think=false。verified treatment 包括服务 scope、输出/观测校验和原预算内修正，不能解释为纯提示收益。

留出集 `holdout-market-tax` 的回答完成运行、通过格式及观测检查，但独立 oracle 拒绝其状态、原因、建议和不确定性。`holdout-recommend-cache` 在一次输出修正后通过。保留两者的完整轨迹，未根据留出结果继续调参。旧版 8 dev × 3 profiles 实验的最终消融 trial 发生响应超时，套件仍标记未完整完成；其真实结果和未知用量也保留在配套报告。

平台冻结源码 checkpoint 为 `8ffe6eeece82305b85094337aef145c316b11526`，评测 checkpoint 为 `aff57b9d6496a4f3e228fc9e4bb2d0139101e357`。最新发布提交还包含报告与证据。复现命令见 [评测复现说明](https://github.com/chendi-Shi/byte-agent-eval/blob/main/docs/reproduce.md)。

BGE-M3 digest、检索排名和实际耗时见 [Embedding 报告](embedding-smoke.json)，运行脚本为 [embedding_smoke.py](embedding_smoke.py)。平台软件测试记录见 [validation.json](validation.json)。

没有执行 LLM 权重 SFT/RL、Multi-Agent 优化或生产多机规模测试；这些不作为已完成能力宣称。
