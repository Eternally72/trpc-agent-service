# Kubernetes 验收记录

日期：2026-10-16。代码以 `89d213d` 为起点，包含本轮未提交的部署和运行时修订。命令从项目根目录执行；日常部署说明见 [Kubernetes 部署](Kubernetes部署.md)。

## 环境与范围

Docker Desktop 的 `docker-desktop` context，Kubernetes v1.36.1。三个 Ready 节点为 `desktop-control-plane`、`desktop-worker`、`desktop-worker2`，均运行在同一台 Docker Desktop 主机上。命名空间为 `trpc-agent-service`，数据存放在独立于 Compose 的 PostgreSQL、Redis、SeaweedFS PVC 中，未导入原机器人配置。

应用角色为 2 个 Gateway、2 个 Worker、1 个 Channel Runtime 和 1 个 Scaler。每个 Worker 有 1 个异步执行槽，副本目标保存在数据库中。数据库迁移 head 为 `20260930_0029`。

本轮验证部署、配置继承、扩缩容、重启和业务功能，没有重新压测。三节点是调度环境，不能据此声称跨主机容灾；真实模型脚本核对持久化回复，没有向外部 IM 发送消息。

## 发现的问题与修订

| 问题 | 原因 | 修订 |
| --- | --- | --- |
| 本地模型配置没有进入 Pod | 部署直接使用 Kubernetes 模板中的模型名 | 从 `.env` 读取 LLM 和 Embedding 配置，用类型校验后生成 ConfigMap；不复制宿主机存储地址 |
| 部署可能重置 Worker 数量 | 清单中的固定 `replicas` 与 Scaler 同时控制副本数 | 清单移除该字段，升级前仅清理旧 last-applied 注解的字段所有权，保留实时副本数 |
| 停止后无法恢复 Worker | 零副本的 Scale 响应为 `spec: {}`，旧解析逻辑要求必须有整数 `replicas` | 按 Scale API 的缺省值处理为 0，再恢复数据库目标；保留非法值校验 |
| Worker 为 0，部署仍报告成功 | 零副本 Deployment 也能完成 rollout；Gateway `/ready` 只要求数据库可用 | 增加容量检查，同时核对数据库活跃 Worker、Deployment 代次和更新/就绪/可用副本数，超时失败 |
| Scaler 长时间运行后可能持有过期 Token | HTTP 客户端只读取一次挂载令牌 | 每次对账重读 ServiceAccount Token；回归测试模拟文件轮换 |
| 非 HTTP 角色启动后立即显示就绪 | 原清单没有对应就绪探针 | Worker、Channel、Scaler 通过数据库心跳写入健康记录，探针检查角色、PID 启动标识、状态和新鲜度 |
| 停止时仍可能残留正在排空的 Pod | 只观察 Deployment，没有等待 Pod 删除 | 先等待 Scaler 退出，再停止其他角色，并为 Worker 保留覆盖终止宽限期的等待时间 |
| 凭据传递与挂载范围过宽 | 密钥通过命令参数传入，存储组件挂载整组平台密钥 | 密钥经临时 0600 文件创建 Secret；PostgreSQL 和 Grafana 各自只挂载所需密码 |
| 更新后不便回滚镜像 | 部署默认清理旧镜像 | 清理改为显式开关，保留 Pod、ReplicaSet、Job 引用的镜像 |

零副本问题在真实停止/启动过程中复现。最小测试 `test_kubernetes_capacity_recovers_zero_replicas` 在修复前对空 `spec` 报 `ValueError`，修复后通过。Scale 的 `replicas` 缺省值为 0，可查 [Kubernetes API 定义](https://github.com/kubernetes/api/blob/master/autoscaling/v1/types.go)。

健康探针只证明进程登记与心跳仍可用，不证明 Scaler 每轮对账或外部模型成功。部署时的容量等待与真实业务检查分别验证这两部分，不能相互替代。

## 代码与清单检查

| 检查 | 结果 |
| --- | --- |
| 默认 `pytest -q tests` | 492 passed、7 skipped，44.62 秒 |
| 默认跳过范围 | 5 项需要显式开启的真实 Kubernetes 检查；2 项由 `storage_check.sh` 管理的外部存储集成 |
| Kubernetes 及相关运行时定向回归 | 35 项通过，包含在 492 项中，不重复累加 |
| Flake8 | 本轮修改的源码、部署 Python 脚本和测试通过 |
| mypy | 132 个源码文件通过 |
| Shell 语法 | `deploy.sh`、`stop.sh`、`remove.sh` 通过 `bash -n` |
| 清单校验 | 应用、存储、监控、迁移清单通过真实 API Server 的 `--dry-run=server` |

没有重新计算覆盖率。构建曾遇到 Windows Docker 凭据助手通信超时，临时使用空 Docker 配置拉取公开镜像后完成构建，没有改动全局 Docker 凭据。

## 真实集群检查

`TRPC_TEST_KUBERNETES=1 .venv/bin/pytest tests/integration/test_kubernetes_cluster.py -v --tb=short`：**5 项通过，40.54 秒**。

| 场景 | 核对内容与结果 |
| --- | --- |
| 角色、迁移和控制台 | 三节点 Ready，全部 Deployment 达到目标可用副本；Worker/Channel/Scaler 探针通过，迁移 head 正确，健康接口和两个控制台返回 200 |
| 扩缩容 | 管理 API 将目标从 2 调为 3，再降为 1，最后恢复为 2；每步等待数据库活跃节点与 Deployment 就绪副本一致 |
| Scaler 权限 | 允许修改 `agent-worker/scale`，拒绝修改 `gateway/scale` |
| Pod 重建 | 删除一个 Worker Pod，观察新 UID 替代旧 Pod，容量恢复为 2 |
| 监控 | Grafana 数据库健康，Prometheus 所有活跃抓取目标为 up，Loki 与 Tempo 的 ready 接口返回 200 |

最初一轮在滚动发布尚未结束时提前启动，角色检查捕获到了 0 个可用 Worker，其余 4 项通过。等部署脚本完成容量检查后整组重跑，得到上面的 5 项通过结果；没有删除或放宽失败断言。

此前还将期望节点数设为 3 后重新部署，确认发布没有把它重置为 2。新代码首次发布时，原环境正处于零 Worker 状态；Scaler 自动恢复为数据库目标，无需手工修改副本数。

## 完整停止与恢复

执行 `./deploy/kubernetes/stop.sh`，四种应用 Deployment 均为 `0/0`，脚本等到所有对应 Pod 删除后返回成功；数据库、监控与 PVC 保留。随后复用镜像 `trpc-agent-service:0.1.0-20261016152237` 运行部署脚本，没有手工补充 `kubectl scale`。

重启后的容量检查输出 `Worker capacity ready: 2/2`，四个角色分别恢复为 Gateway `2/2`、Worker `2/2`、Channel `1/1`、Scaler `1/1`。`/ready` 返回 `database: ok`、`worker_nodes: 2`。这次验证覆盖了最初无法从零副本恢复的原始场景。

## 真实模型与 RAG

使用 `tests/kubernetes_live_check.py`，在 Gateway Pod 中创建专用租户，向 PostgreSQL 任务队列入队，由实际 Worker 执行。每条请求重复入队并在完成后重放，脚本断言任务 ID 不变、执行尝试为 1、用量事实只有 1 条，持久化回复包含预期答案。

- 生成模型：`qwen3.7-flash-2026-07-15`。
- 向量模型：`qwen3.7-text-embedding-flash`，配置维度 1024。
- 6 个业务请求全部完成，Usage Ledger 合计 **5,246 个输入 tokens、231 个输出 tokens，共 5,477 tokens**。这些是应用账本的 LLM 用量，不含 Embedding 计费，也未独立对账供应商账单。

| 业务请求 | 结果 | 从入队到观察完成 |
| --- | --- | ---: |
| 基础对话 | 返回“真实模型连接成功” | 1.53 秒 |
| 写入记忆 | 记住验收代号 `蓝鹭7529` | 2.03 秒 |
| 后续轮次召回 | 返回同一代号 | 1.52 秒 |
| 计算工具 | `calculate` 成功，`(173 × 29) + 41 = 5058` | 2.03 秒 |
| RAG 初次检索 | `knowledge.search` 成功，回答 `松石-8362` 和负责人，附来源引用 | 3.03 秒 |
| 文档替换后检索 | 新会话只回答 `琥珀-4917`，未出现旧口令 `8362` | 2.52 秒 |

另行核对真实 Embedding 入库成功、文档版本升至 2，以及删除后文档列表为空。以上是小样本功能验收时间，不是 P95、吞吐或首字延迟。6 个业务请求也不等于 6 次模型 HTTP 调用：工具调用可能引起多轮推理。

此前旧向量模型返回 `AllocationQuota.FreeTierOnly`，当时仅完成了 RAG 之前的步骤。更换为本轮指定模型后，完整链路重新运行通过；没有把此前额度失败记录为通过。两个模型名称已写入本地 `.env` 并由部署脚本同步到集群，API Key 没有写入文档或清单。

## 实际存储的独立对照

`tests/kubernetes_storage_check.py` 使用真实 PostgreSQL、pgvector 和 SeaweedFS，仅将 Embedding 替换为固定 1024 维向量。以下 **6 个场景全部通过**，用于把存储正确性与供应商额度分开验证：

1. 同一 Artifact 重复入库，保持同一文档 ID。
2. 两租户分别上传 `alpha-100` 与 `beta-200`，检索只返回本租户内容。
3. A 租户文档更新到 `alpha-300`，旧版本不再被检索到。
4. 经实际管理 API 停用后不可检索，恢复后重新可见。
5. 删除 A 的文档后，A 无结果，B 的文档仍可检索。
6. 彻底删除测试租户后，文档、向量记录为 0，已上传对象无法再读取。

这组固定向量测试验证过滤、版本和清理逻辑，不衡量语义召回质量；上面的真实 RAG 测试单独验证供应商 Embedding 与模型调用。

## 复现与记录

测试代码在 `tests/`，部署脚本在 `deploy/kubernetes/`，均可随仓库交付，不依赖 `dev/`。执行方法见 [部署文档的检查命令](Kubernetes部署.md#重跑本轮检查)。本机原始输出存放于已忽略的 `.run/kubernetes/`；这里保留测试条件和汇总结论，不把临时日志或真实凭据提交到仓库。

真实模型与存储脚本创建专用租户，结束时调用彻底删除接口清理租户、测试模型配置与凭据引用。历史审计和用量按平台规则保留。
