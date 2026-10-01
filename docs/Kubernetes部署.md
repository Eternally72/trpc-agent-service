# Kubernetes 部署

## 定位

2026-09-29 对照 `deploy/kubernetes/` 清单整理。已有 Docker Desktop 三节点部署人工验收记录，见[测试结果](测试结果.md)；本次文档更新没有重新部署集群。Locust 容量测试运行在单台 WSL 主机，不能把其吞吐和延迟标为该集群的实测值。

清单初始为 2 个 Gateway、2 个 Agent Worker、1 个 IM Channel Runtime、1 个 Worker Scaler，以及共享存储和监控组件。资源位于 `trpc-agent-service` Namespace，Pod 由 Deployment/StatefulSet 创建。

正式生产可以复用这些清单，但 PostgreSQL、Redis、对象存储和监控存储应替换为高可用或托管服务，并补充 Ingress TLS、远端镜像仓库、备份策略和跨故障域调度。

```mermaid
flowchart TB
    USER[平台 / 租户管理员] --> SVC[LoadBalancer Service]
    IM[企业微信 / 飞书] <--> CHANNEL[Channel Runtime × 1]
    SVC --> GW[Gateway × 2]
    GW --> SQL[(PostgreSQL + pgvector)]
    CHANNEL --> SQL
    SQL --> WORKER[Agent Worker × N]
    SCALER[Worker Scaler × 1] -->|scale 子资源| WORKER
    WORKER --> MODEL[百炼 Model / Embedding]
    WORKER --> MCP[租户 MCP]
    WORKER --> SQL
    WORKER --> S3[(SeaweedFS S3)]
    WORKER -->|近期 Session 缓存| REDIS[(Redis)]
    SQL --> CHANNEL
    GW -. OTLP .-> OTEL[OTel Collector]
    WORKER -. OTLP .-> OTEL
    CHANNEL -. OTLP .-> OTEL
    OTEL --> PROM[Prometheus]
    OTEL --> TEMPO[Tempo]
    ALLOY[Alloy] --> LOKI[Loki]
    PROM --> GRAFANA[Grafana]
    TEMPO --> GRAFANA
    LOKI --> GRAFANA
```

`Agent Worker × N` 的 N 来自数据库中的期望节点数；初始清单为 2，系统管理端修改后由 Scaler 收敛。Gateway、Worker、Channel 和 Scaler 是独立 Deployment，业务事实都进入共享存储。Worker Pod 当前显式设置 `TRPC_SERVICE_WORKER_CONCURRENCY=1`，即每进程 1 个异步 slot；增加副本数与调整每个 Worker 的 slot 数是不同操作。

本地脚本模式和 Kubernetes 模式会使用相同的 8000、3000、9090 等本机端口，不能直接按默认配置同时运行。本地切到 Kubernetes 前执行 `./stop.sh`。反向切换时要注意：`./deploy/kubernetes/stop.sh` 只把应用副本缩为零，保留数据库、监控和 Service，不保证释放 LoadBalancer 的本机端口。先检查 `kubectl get services -n trpc-agent-service`，处理端口暴露冲突或调整本地端口与连接配置，再启动 Compose 模式。

## 部署

首次部署且需要导入 Compose 的 PostgreSQL 数据时，先停止宿主机进程，再只启动源 PostgreSQL：

```bash
./stop.sh
docker compose up --detach --wait postgres
./deploy/kubernetes/deploy.sh --import-compose-data
docker compose down
```

`--import-compose-data` **仅迁移 PostgreSQL**，不会复制 SeaweedFS 对象、Redis 缓存或本地 Workspace 文件。若旧知识库仍引用 Compose 中的对象文件，需另行迁移对象存储或在 Kubernetes 租户控制台重新上传；不能把数据库导入成功视为知识库文件也已迁移。

后续更新直接执行：

```bash
./deploy/kubernetes/deploy.sh
```

如果只修改了 Kubernetes 清单或运行配置，或者目标应用镜像已由 CI 构建，可以显式复用本机已有镜像，避免重复构建：

```bash
TRPC_K8S_REUSE_IMAGE='trpc-agent-service:your-existing-tag' ./deploy/kubernetes/deploy.sh
```

应用代码发生变化时仍应使用默认命令构建新镜像；复用模式会先验证镜像存在，再执行迁移和滚动发布。

脚本会使用项目 `Dockerfile` 构建 Python 3.12 应用镜像、同步平台 Secret、创建 ConfigMap、启动存储和监控、执行 Alembic Job，并滚动部署应用角色。每次重新部署都会重启依赖 ConfigMap 的可观测组件，并将 PostgreSQL、Grafana 持久化密码与最新 Secret 同步，避免 Pod 继续使用旧配置或旧密码。发布成功后，只清理未被当前 Namespace 中任何 Pod 引用的旧 `trpc-agent-service` 镜像。真实密钥只从已忽略的 `.env` 和 `.secrets/` 读取，不写入部署清单。首次以 Kubernetes 启动时会生成并持久保留租户 SecretStore 主密钥。

新建或更新的 IM Binding 由租户管理员在 `/tenant` 中配置，密钥以数据库密文保存。部署脚本**不会**把旧的本地文件型 IM SecretRef 挂载进 Pod；如导入的旧 Binding 仍引用这类文件，部署后需在租户控制台重新保存密钥，使其转为数据库密文，否则对应 IM 长连接无法使用。

模型和 Embedding 示例来自 `app.env`，不等于本地控制台的所有配置都已迁移。部署前核对 Profile 引用的凭据在 Pod 内是否可用，Embedding 模型是否与知识库记录一致。当前向量列固定为 1024 维；仅更换模型名不会重建旧索引。

清单提供 Redis、PostgreSQL/pgvector、SeaweedFS 和监控组件。Skill、MCP 客户端随应用镜像运行，不为每个租户创建独立 Pod。部署成功后仍需验证模型额度、机器人权限、MCP 出站访问和知识文件可读性。

## 验证

```bash
kubectl get pods -n trpc-agent-service -o wide
kubectl get services -n trpc-agent-service
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

- 系统管理端：`http://localhost:8000/admin`
- 租户管理端：`http://localhost:8000/tenant`
- Grafana：`http://localhost:3000`
- Prometheus：`http://localhost:9090`
- Bootstrap Token 和 Grafana 密码继续读取 `.secrets/` 下的原文件；Grafana 用户名固定为 `admin`。

执行节点统一在系统管理端的“运行节点”页面调整。页面只写入期望数量，`worker-scaler` ServiceAccount 仅有读取和修改 `agent-worker` scale 子资源的权限，不能操作其他 Deployment。

紧急运维仍可临时执行：

```bash
kubectl scale deployment/agent-worker -n trpc-agent-service --replicas=3
```

Worker Scaler 会在下一次对账时恢复管理端保存的期望数量，因此不能长期同时使用手工 `kubectl scale`、HPA 和管理端期望值。后续接入 HPA 时，应将容量控制权切换给 HPA，而不是让两个控制器同时修改副本数。

Kubernetes 缩容时先向 Pod 发送 `SIGTERM`。Worker 立即进入 `draining` 并停止领取任务，`terminationGracePeriodSeconds` 为现有任务保留完成窗口；最终状态继续由任务租约和 fencing token 保护。

## 停止与清理

仅停止应用并保留全部数据：

```bash
./deploy/kubernetes/stop.sh
```

删除 Namespace、组件和全部 Kubernetes PVC 测试数据：

```bash
./deploy/kubernetes/remove.sh --confirm-delete-data
```

本地 StorageClass 使用节点本地卷，不能据此承诺跨节点存储容灾。Worker 的 `/data/workspaces` 使用 Pod 独立 `emptyDir`。目录 ID 还包含当前执行 fence，接管使用新目录，不继承旧尝试的临时文件；改成 RWX 共享卷也不会改变这个语义。需要保留的结果必须写入 Artifact 或数据库。

Session、知识文档状态、审批、审计和任务事实都保存在共享后端，Worker 不需要 Sticky Session。Channel Runtime 目前只有一个副本，本地存储和 IM 连接所有权也没有组成跨故障域高可用方案。生产部署还需配置 TLS、安全 Cookie、数据库和对象存储容灾，以及实际告警接收。
