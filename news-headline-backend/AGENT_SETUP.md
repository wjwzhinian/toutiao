# 新闻 Agent 配置与运行说明

Agent 位于后端服务中，为登录用户提供基于项目新闻库的问答。前端只调用本项目的 Agent API；DeepSeek 密钥、模型调用和工具权限均由后端控制。它不是可执行任意代码的通用 Agent。

整体架构见项目根目录 [README.md](../README.md)，后端接口列表见本目录 [README.md](README.md)。

## 能力和数据边界

Agent 使用 DeepSeek 的 OpenAI 兼容聊天接口，并通过服务端工具访问新闻数据库。当前工具能力包括：

- 按关键词检索已发布新闻，并返回标题、摘要和引用信息。
- 查询指定新闻详情。
- 基于已发布新闻生成有来源依据的总结。
- 按新闻 ID 对比新闻。
- 查询本地新闻库中的高浏览量内容。
- 按时间范围和分类统计新闻发布量。

Agent 工具只读状态为 `published` 的新闻；不会返回草稿或归档内容，不执行模型生成的 SQL，不写入新闻/用户业务表，也不访问任意网页。热门和趋势基于项目数据库，不表示实时互联网排名或全网趋势。Agent 的会话和执行数据属于独立的 Agent 持久化表。

## 配置模型服务

首次配置时在本目录将 `.env.example` 复制为 `.env`，然后填写有效值。已有 `.env` 时不要覆盖：

~~~env
AI_API_BASE_URL=https://api.deepseek.com
AI_API_KEY=replace-with-a-secret
AI_MODEL=deepseek-flash
AI_PROVIDER_NAME=deepseek
AI_TIMEOUT_SECONDS=45
AI_MAX_RETRIES=2
~~~

`AI_API_KEY` 必须是有效密钥；示例占位值不可用于真实调用。不要把密钥贴到前端、提交到 Git、放入 `VITE_*` 变量或写进日志。`config/settings.py` 会从后端 `.env` 和进程环境读取配置，并隐藏关键字段的对象表示。

| 变量 | 用途 | 默认/范围 |
| --- | --- | --- |
| `AI_API_BASE_URL` | OpenAI 兼容接口根地址 | DeepSeek 默认地址；生产必须 HTTPS。 |
| `AI_API_KEY` | 模型服务凭证 | 必填；生产环境拒绝空值和占位密钥。 |
| `AI_MODEL` | 服务端请求模型名称 | `deepseek-flash`。 |
| `AI_PROVIDER_NAME` | Provider 标识 | `deepseek`。 |
| `AI_TIMEOUT_SECONDS` | 模型请求超时 | 默认 45 秒，允许 1–300。 |
| `AI_MAX_RETRIES` | 可重试失败的最大重试次数 | 默认 2，允许 0–5。 |
| `AGENT_RATE_LIMIT` | 每用户时间窗内最大聊天请求数 | 默认每 60 秒 10 次。 |
| `AGENT_RATE_WINDOW_SECONDS` | 限流时间窗 | 默认 60 秒。 |
| `AGENT_LOCK_TTL_SECONDS` | 用户并发生成锁最长时间 | 默认 240 秒，允许 30–86400。 |

模型请求对超时、连接错误、HTTP 429 和服务端错误进行有限退避重试；普通客户端错误不会无限重试。运行失败会写入安全错误码和工具运行记录，日志不应包含 API Key。

## 数据库准备

Agent 持久化表定义在 `db/agent_schema.sql`，包括：

| 表 | 用途 |
| --- | --- |
| `agent_sessions` | 用户拥有的对话会话。 |
| `agent_messages` | 用户消息和 Assistant 回复。 |
| `agent_runs` | 每次 Agent 执行的状态、错误码和耗时等信息。 |
| `agent_tool_calls` | 每次允许的工具调用及其运行结果元数据。 |

基础数据库首次创建时，先导入 `db/database.sql`，再导入 `db/agent_schema.sql`。如果现有数据库缺少 Agent 表，在后端目录执行：

~~~powershell
cmd /c "mysql -u root -p news_app < db\agent_schema.sql"
~~~

也可以使用 MySQL Workbench 执行该 SQL 文件。开发模式启动时的自动迁移只处理用户角色和新闻管理结构，不会创建 Agent 表。升级既有数据库时，先运行 `python -m db.migrations`，并在需要时单独补齐 Agent 表。生产数据库由数据库管理员按 [SECURITY.md](../SECURITY.md) 的流程迁移。

## HTTP 接口与 SSE

所有 Agent 接口要求用户已登录且具有 `agent:use` 权限。会话 ID 使用 UUID；服务器通过用户 ID 限定会话所有权。

### 发送消息

`POST /api/agent/chat`

~~~json
{
  "message": "总结最近的科技新闻，并附上来源",
  "session_id": null
}
~~~

`message` 必须是非空字符串，最多 2000 字符。新会话将返回会话事件；续聊时传入该用户已有会话的 UUID。

响应媒体类型为 `text/event-stream`。前端应逐行解析 SSE 的 `event:` 和 `data:` 字段，不要尝试把响应解析为一个 JSON 文档。事件类型包括：

| 事件 | 含义 |
| --- | --- |
| `session` | 新建或继续的会话信息。 |
| `tool_started` | Agent 开始调用新闻工具。 |
| `tool_finished` | 新闻工具已返回结果。 |
| `token` | Assistant 流式文本片段。 |
| `citations` | 回答引用的新闻来源。 |
| `done` | 本轮正常结束。 |
| `error` | 本轮失败及可展示的安全错误信息。 |

### 会话历史

- `GET /api/agent/sessions?limit=20` 列出当前用户会话，limit 为 1–50。
- `GET /api/agent/sessions/{session_id}/messages` 返回该用户拥有的指定会话消息；其他用户的 ID 不会授权读取。

## 限流、并发与可用性

Redis 用于按用户做固定时间窗计数和单用户生成互斥锁。默认每用户每 60 秒最多 10 次聊天请求；同一用户正在生成时再次提交会收到 HTTP 409，超出速率会收到 HTTP 429。Redis 不可用时 Agent 请求失败关闭并返回 HTTP 503。新闻阅读和用户资料接口不依赖模型 Key，但 `/health/ready` 要求 MySQL 与 Redis 都可连接。

Provider 使用超时和有限重试减轻短暂网络故障；前端仍应展示连接中、接收流、完成和错误状态。服务端日志和数据库错误字段使用安全错误类型/代码，不能用原始异常文本代替 API 响应。

## 本地验证

在本目录安装后端依赖并运行回归测试：

~~~powershell
python -m unittest discover -s tests -v
~~~

相关用例位于 `tests/test_agent_service.py`、`tests/test_provider.py`、`tests/test_security.py`。启动后可通过 Swagger 查看 Agent 路由，或者使用前端 `/aichat` 页面验证 SSE 和会话历史。

## 当前限制

Agent 的知识范围是本地已发布新闻数据；不使用实时搜索，不读取未审核草稿，也没有外部工具插件、组织级隔离或后台评测/人工标注工作台。模型回答质量取决于新闻库内容、来源和所配置模型。
