# Worker Pool 使用期望状态扩缩容

状态：已采纳并实现。2026-09-29 按当前本地 Supervisor 和 Kubernetes Scaler 核对。

## 决策

系统管理员只在控制面修改 `agent-worker` 的期望节点数，不由 Web 请求直接创建或终止进程。独立协调器负责把实际容量收敛到目标数量：本地模式管理独立 Worker 子进程，Kubernetes 模式只修改 `agent-worker` Deployment 的 scale 子资源。

缩容统一先排空：目标外 Worker 停止领取新任务，完成或释放已领取任务后退出。管理请求携带 generation，防止两个过期页面互相覆盖。任务租约和 fencing token 仍是最终正确性边界，协调器不能绕过数据库状态。

## 结果

- 管理 API 不需要宿主机进程权限或通用 Kubernetes 管理权限。
- 本地与 Kubernetes 使用同一种控制面语义。
- 手工 `kubectl scale` 或 HPA 不能与当前期望状态协调器长期同时控制副本数；接入 HPA 时必须明确移交控制权。
- 管理员不能强杀指定节点；故障节点由心跳过期、租约接管和运维流程处理。

## 节点与执行槽

期望数量控制 Worker 进程或 Kubernetes 副本，不是并发线程数。每个 Worker 通过多个异步 slot 处理不同会话；同一会话仍按 FIFO 执行。当前本地默认每进程 1 个 slot，Kubernetes Worker 清单也显式设置为 1。

扩容不实现租户公平调度。2026-09-28 的竞争测试表明增加 slot 可缓解短任务排队，但持续过载仍然影响其他租户。后续引入租户配额或公平调度需要另行设计，不能把节点扩容当作资源隔离。

实现：[扩缩容协调](../../trpc_service/agent/scaling.py)、[Worker](../../trpc_service/agent/worker.py)。数据：[测试结果](../测试结果.md)。
