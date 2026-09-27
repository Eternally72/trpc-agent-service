# GitHub MCP 故障定位与修复（2026-09-27）

## 用户问题与范围

企业微信 Agent 已获 GitHub MCP 授权，却无法查找 `Eternally72/trpc-agent-service`。本轮基于 `208f963` 定位实际运行故障，保留企业微信机器人 ID、密钥、模型策略和 Agent 授权，不对 GitHub 执行写操作。

对应运行配置：企业微信租户“租户1-企业微信”、`wecom-agent`，稳定版本 **7**；GitHub MCP 连接为 `api.githubcopilot.com`。已发布配置包含 **48 个 MCP 工具**，其中 `search_repositories` 可见。9 月 27 日原始请求的 Tool Ledger 记录搜索和身份查询均为 `FAILED / ExceptionGroup`，不是控制台勾选未生效。

## 根因一：异步 HTTP 请求流被错误转换

`PinnedMCPTransport` 为防止 DNS 重绑定，会把目标主机固定为经过校验的 IP，同时保留原 Host 和 TLS SNI。原实现用 `content=request.stream` 构造新请求；HTTPX 会将同时支持同步/异步接口的 `ByteStream` 按同步 iterable 重新包装。实际异步传输入口随后触发：

```text
httpx/_transports/default.py:378
assert isinstance(request.stream, AsyncByteStream)
AssertionError
```

这发生在 MCP 初始化请求发出前。AnyIO 任务组及 SDK 清理把它包装成 `ExceptionGroup`，外层表现为 MCP 会话建立失败或等待超时。

对照实验：不带凭据访问同一 GitHub MCP 地址，普通 HTTPS 约 0.82 秒返回 401；项目固定 IP 传输立即触发断言。网络可达，失败由请求流适配造成。

修复为 **`stream=request.stream`**，直接保留已有流，不再重新编码。原有目标地址校验、Host、TLS SNI、跨域/重定向限制及仅连接失败可重试的规则均保留。

此前测试替换了 `AsyncHTTPTransport.handle_async_request`，没有进入 HTTPX 自身的流断言，因此漏掉这个故障。新增测试只替换更下层的连接池，让空请求、JSON 请求和流式请求都经过真实 HTTPX 传输入口。

## 根因二：GitHub 搜索条件遗漏 fork

连接修复后，真实 MCP 可以发现工具、搜索账号仓库、获取身份并读取指定仓库 README。但不包含 fork 的搜索返回空结果。该目标仓库实际为 fork；下列真实查询能返回目标仓库：

```text
user:Eternally72 trpc-agent-service fork:true
```

GitHub 官方说明默认仓库搜索排除 fork，需使用 `fork:true` 或 `fork:only` 纳入结果，见 [Searching in forks](https://docs.github.com/en/search-github/searching-on-github/searching-in-forks)。

只修复传输时，真实模型连续执行了 7 次 MCP 调用，却错误回答仓库不存在。因此还给**官方 GitHub MCP 的 `search_repositories` 模型可见说明**补充查询示例 `user:OWNER REPOSITORY in:name fork:true`，说明空结果不能证明不存在；已知 owner/repo 时，可用已授权的 `get_file_contents` 核实。仅增加声明文字的复测仍有误判，最终将同一说明放入成功工具响应的独立 `guidance` 字段，让模型在读取结果时再次看到搜索范围限制。

该说明只匹配官方主机及该工具，原始返回仍保留在 `result` 字段，不改写模型实际调用参数，不更改持久化目录、Agent 不可变版本或权限，也不影响其他 MCP。回归特别检查 `fork:false` 参数原样传递。它改善模型工具选择，不能保证任意模型和提问永远得到相同结果。

## 同时修复：SDK 错误日志暴露鉴权头

SDK 在会话建立失败时将连接参数格式化进异常及日志，原参数表示包含 Authorization。新增 `CredentialSafeMCPParameters`，将 headers 从字符串和 repr 表示中排除，同时仍通过原请求传送鉴权头。

回归通过真实 SDK 的会话创建失败路径检查异常、日志和参数 repr 不含测试 Token，并验证鉴权头没有被删除。本地三份历史日志中共 **20 处 GitHub Token 文本**已原位遮蔽，不修改密钥配置。已转发的日志副本或其他历史副本无法靠本地遮蔽撤回，建议轮换曾记录在旧日志中的 GitHub Token；本轮未替用户撤销任何凭据。

## 真实运行验收

- 使用 `start.sh` 重新构建、迁移检查并启动修复后的 API、Worker 和渠道进程，保留 Docker 数据。
- 重新发现 **48 个 GitHub MCP 工具**，清除连接的旧错误状态。
- 使用既有企业微信 Agent 的版本 7、现有真实模型和 MCP 凭据，经持久化队列交给运行中的 Worker。创建独立 `live_check` 测试绑定和新会话，避免向真实 IM 会话投递验收消息。
- 原始措辞“帮我搜索一下 GitHub 上 Eternally72 的 trpc-agent-service 这个项目，告诉我链接和项目用途”已通过真实调用返回正确仓库链接与基于 README 的介绍；Tool Ledger 确认 MCP 调用成功，而非仅检查模型文本。
- 最终版本的原始措辞验收：请求 `mcp-live-e3310f197ba2445fb0e414989e31d18e`，任务 `688cd19f-5e12-43f6-b4ed-07d90d1e3f50`，**30.85 秒、尝试 1 次**。2 次 `search_repositories` 和 1 次 `get_file_contents` 均为 `SUCCEEDED`，其中仓库搜索的原始结果实际包含 `Eternally72/trpc-agent-service`。
- 独立测试用户及新会话复核：请求 `mcp-live-4a453b894b094015a2615a867ff6da1e`，**12.14 秒、尝试 1 次**。模型选择直接调用 `get_file_contents` 核实已知 owner/repo，调用成功并返回正确链接及用途；这次没有执行仓库搜索，不将其记作第二次搜索命中。
- 测试绑定停用、内部回复取消；既有租户、Agent、GitHub 连接及企业微信机器人绑定均保留。
- `/ready` 返回 200，数据库正常、2 个活跃 Worker；前后端保持同源 8000。重启后企业微信认证成功。本轮内部验收覆盖模型、MCP、Worker 及回复持久化，不冒充新的企业微信客户端收发验收。

## 回归结果

- 新增 **8 项**回归：3 种请求流、SDK 敏感参数错误路径、4 种工具说明与权限隔离情形。请求流和日志测试修复前为 **3 failed / 1 passed**；GitHub 说明测试修复前为 **1 failed / 3 passed**，修复后均通过。
- MCP、传输及安全定向回归：**32 passed**。
- 最终 `coverage.sh -q`：**475 passed、2 skipped，52.75 秒，覆盖率 90.12%**。默认跳过的是需单独基础设施脚本执行的两项存储集成测试。
- Flake8、Mypy（126 个源文件）、YAPF、差异空白检查及 `start.sh` 构建启动通过。最终版本包含声明及结果提示两部分，并检查原始工具参数、结果和授权不受影响。

快速重现回归：

```bash
.venv/bin/pytest -q tests/test_mcp_transport_regression.py \
  tests/test_mcp_connections.py tests/test_security_hardening.py --no-cov
```

本轮没有修改第三方依赖源码或通过关闭网络安全检查来规避故障。新增测试保留真实 HTTPX 与 SDK 边界，以防后续适配变化再次出现“模拟测试通过、真实连接失败”。
