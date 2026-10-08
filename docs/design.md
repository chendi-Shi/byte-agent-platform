# 设计、来源与演进

## GitHub 调研

2026-10-08 通过 GitHub 插件检索并阅读以下仓库：

- [langchain-ai/react-agent](https://github.com/langchain-ai/react-agent)：官方 ReAct model/tools 循环，适合研究主架构。已阅读 README 与 `src/react_agent/graph.py`；读取时 graph.py 的 blob SHA 为 `e41ee1ae8090e7d226e5f5f912bfe0e63b29cbe1`。
- [THUDM/AgentBench](https://github.com/THUDM/AgentBench)：函数调用与多环境验收参考。它的部署包含容器及额外服务；本项目不冒称跑过官方基准。
- [ethz-spylab/agentdojo](https://github.com/ethz-spylab/agentdojo)：将 prompt injection 与正常任务效果分别评估的参考；本项目不冒称实现了它的防御或集成。

代码为独立实现，仅参考公开架构思想，无上游代码或数据复制。采用轻量 Python 运行时是为了让状态迁移、协议与恢复行为能被逐行审查。MIT 许可证仅覆盖本仓库原创内容。

## 执行状态

`running → model inflight → model decision committed → pending tool observations → completed`。

模型请求前落库 `inflight=true`。请求失败或恢复时发现 inflight，转为 `uncertain`，不自动再次请求。模型响应成功后，消息、用量与 pending 调用一起保存。只读工具结果、消息与 pending 删除同时保存。OS 文件锁限制同一 run 的 writer；不同 run 可以独立运行，但实验调度目前串行。

run 身份绑定任务、模型名、endpoint、模型版本、fixture 计划、工具 schema、知识与指标逻辑内容、源码和预算。调用者不得在一个活动 run 中修改输入库。CLI 与评测入口自动记录 Ollama digest、量化及版本；直接使用 Runtime 的自定义模型需由调用者提供 revision。模型版本在请求前查询，若服务在试验期间替换权重仍存在竞态，正式实验应固定服务。

## 检索

Markdown 段落分块，最长 1000 字符；Latin 词与中文 bigram 用于词法索引。id 绑定相对路径、片段位置和文本。重建使用事务替换快照，避免删除的旧文本继续被召回。向量由指定 Ollama Embedding 模型提供，长度与数值检查后保存；检索使用 BM25 + 余弦排名的 RRF。这里只实现一个 Markdown 知识源，没有 GraphRAG。

## 下一步验收标准

1. 固定工具模型和 embedding 模型 digest，在配套 dev suite 调整提示，再冻结配置跑 holdout。记录实际失败，不能用 fixture 差值写模型提升。
2. 扩充到至少 30 个不同服务/知识任务，覆盖未检索到、相互矛盾证据、过期资料与恶意指令；当前任务是微型合成检查集。
3. 引入 LangGraph 适配器时对齐工具 schema 和轨迹，比较同模型、同输入、同预算，避免把框架差异与提示差异混在一起。
4. 引入 PostgreSQL、多租户和队列前，先建立授权矩阵、幂等键、取消语义与 worker 崩溃测试。当前无大规模分布式性能结论。
