# 后端说明

后端位于此目录，是新闻头条系统的 HTTP API、业务处理和数据访问服务。项目使用 FastAPI、SQLAlchemy 异步 ORM、MySQL、Redis 和 DeepSeek OpenAI 兼容接口。根目录 [README](../README.md) 介绍整体系统；部署、配置和安全步骤分别见 [DEPLOYMENT.md](../DEPLOYMENT.md) 与 [SECURITY.md](../SECURITY.md)。

## 代码分层

一次 API 请求通常按以下方向处理：

~~~text
HTTP Router -> 权限/Schema 校验 -> CRUD 或 Service -> SQLAlchemy/Redis/模型网关
~~~

| 路径 | 职责 |
| --- | --- |
| `main.py` | 应用创建、路由注册、生命周期、Trusted Host、CORS、健康检查和异常处理。 |
| `routers/` | 新闻、用户、收藏、历史、Agent 和管理员 HTTP 接口。 |
| `schemas/` | 请求字段验证、长度/枚举限制和 API 字段别名。 |
| `utils/auth.py`、`utils/permissions.py` | Bearer Token 身份读取和后端角色权限检查。 |
| `crud/` | 用户、新闻、收藏、历史数据库读写。公开新闻 CRUD 只返回已发布记录。 |
| `services/news_collection.py` | RSS/Atom 获取、内容清理、去重和草稿入库。 |
| `agent/provider.py` | 通过 OpenAI 兼容接口请求模型，处理 SSE 文本、超时和有限重试。 |
| `agent/service.py` | Agent 会话准备、工具调用编排、SSE 输出和执行记录持久化。 |
| `agent/tools.py` | 供模型调用的新闻只读查询工具。 |
| `cache/news_cache.py` | 新闻分类、列表、详情等读缓存和失效处理。 |
| `config/settings.py` | 从后端目录 `.env` 或进程环境读取配置；对生产配置进行 fail-closed 校验。 |
| `config/db_confing.py`、`config/cache_conf.py` | MySQL 异步 Session 和 Redis 客户端。 |
| `db/database.sql` | 新库基础表、索引及演示分类/新闻数据。 |
| `db/agent_schema.sql` | Agent 会话、消息、运行和工具调用表。 |
| `db/migrations.py` | 幂等添加用户角色、Feed 来源、新闻状态及审核去重字段。 |
| `tests/` | 安全、Provider、Agent、新闻管理和采集回归测试。 |

配置应通过环境变量设置，不要把数据库密码、Redis 密码或模型密钥写入 Python 文件。

## HTTP API

### Production Agent operations added

- `GET /api/admin/evidence/search?claim=...&limit=5` retrieves evidence only from published local news. Results include a source, publication time, URL, verbatim excerpt, relevance score, and publisher group. It never requests a model-supplied external URL.
- `GET /api/admin/feeds/{source_id}/quality` and `POST .../quality/recalculate` expose an explainable source score based on registered provenance, feed availability, collection duplicate rate, and editor feedback. A source must have at least three feedback samples, score at least 80, and no current feed error to receive the `high` tier required by the automatic-publication gate.
- `GET /api/admin/orchestration/jobs` exposes collection job states, retry times, attempts, and safe error codes. Transient failures use exponential backoff, capped at one hour and five attempts; permanent errors stop for administrator action.
- `GET /api/admin/events` and `GET /api/admin/events/alerts` expose title-similarity event groups and update/correction alerts. `GET /api/admin/events/{event_id}` compares grouped stories, while `POST /api/admin/events/{event_id}/revision-suggestion` generates a read-only revision suggestion. `POST /api/admin/events/alerts/{alert_id}/resolve` records the editor's review. The event service never edits or withdraws a story.
- `POST /api/admin/operations/command` accepts a small allow-list of admin commands. Read operations return database results; pause, manual-review, and re-review operations create the same pending confirmation actions used by the chat Agent.

The conversational Agent also has a read-only `search_evidence` tool. Its administrator tools can inspect collection failures and publication counts, or propose pausing a category, switching a category to manual review, and rechecking blocked reviews. Write tools never run until an administrator confirms the proposed action.

For an existing deployment, run `python -m db.migrations` before using these routes. The migration is idempotent and creates the collection, score, and event tables; fresh databases get the same definitions from `db/agent_schema.sql` plus the source retry columns in `db/database.sql`.

所有用户认证接口使用：

~~~http
Authorization: Bearer <token>
~~~

Token 登录后返回，服务端按摘要保存并校验有效期。注册和登录本身不要求认证；下列路由表中的“需登录”接口需要有效 Token。

### 新闻与账户

| 方法 | 路径 | 认证 | 功能 |
| --- | --- | --- | --- |
| GET | `/api/news/categories` | 公开 | 新闻分类。 |
| GET | `/api/news/list?categoryId=1&page=1&pageSize=10` | 公开 | 按分类分页读取已发布新闻，单页最多 100 条。 |
| GET | `/api/news/detail?id=1` | 公开 | 已发布新闻详情、浏览量和相关文章。 |
| POST | `/api/user/register` | 公开 | 注册并返回登录信息。 |
| POST | `/api/user/login` | 公开 | 登录并返回用户信息和 Token。 |
| GET | `/api/user/info` | 需登录 | 当前用户资料。 |
| PUT | `/api/user/update` | 需登录 | 更新个人资料。 |
| PUT | `/api/user/password` | 需登录 | 修改密码；修改后活动 Token 会被撤销。 |
| GET | `/api/user/permissions` | 需登录 | 当前用户角色和权限。 |

### 收藏与浏览历史

| 方法 | 路径 | 权限 | 功能 |
| --- | --- | --- | --- |
| GET | `/api/favorite/check?newsId=...` | `favorites:read` | 查询当前用户收藏状态。 |
| POST | `/api/favorite/add` | `favorites:write` | 添加收藏。 |
| DELETE | `/api/favorite/remove?newsId=...` | `favorites:write` | 删除一条收藏。 |
| GET | `/api/favorite/list?page=1&pageSize=20` | `favorites:read` | 收藏列表。 |
| DELETE | `/api/favorite/clear` | `favorites:write` | 清空收藏。 |
| POST | `/api/history/add` | `history:write` | 添加或更新浏览记录。 |
| GET | `/api/history/list?page=1&pageSize=20` | `history:read` | 当前用户历史记录。 |
| DELETE | `/api/history/delete/{news_id}` | `history:write` | 删除单条历史。 |
| DELETE | `/api/history/clear` | `history:write` | 清空历史。 |

### Agent

| 方法 | 路径 | 权限 | 功能 |
| --- | --- | --- | --- |
| POST | `/api/agent/chat` | `agent:use` | 建立或继续对话，响应为 SSE。消息最多 2000 字符。 |
| GET | `/api/agent/sessions?limit=20` | `agent:use` | 当前用户的会话列表，limit 范围 1–50。 |
| GET | `/api/agent/sessions/{session_id}/messages` | `agent:use` | 读取当前用户拥有的会话消息。 |

聊天请求示例：

~~~json
{
  "message": "总结最近的科技新闻",
  "session_id": null
}
~~~

SSE 事件包含 `session`、`tool_started`、`tool_finished`、`token`、`citations`、`done` 和 `error`。Agent 可按关键词检索、读取文章详情、生成有新闻依据的摘要、比较指定新闻、查询本地库热门新闻和按日期/分类统计新闻量。工具只读 `published` 记录，不执行模型生成的 SQL，也不提供实时互联网搜索。

### 管理端

管理接口均在 `/api/admin` 下：

| 方法 | 路径 | 权限 | 功能 |
| --- | --- | --- | --- |
| GET | `/access` | `users:manage_roles` | 查看管理员权限信息。 |
| PATCH | `/users/{user_id}/role` | `users:manage_roles` | 设置用户角色；最后一个管理员不能被移除。 |
| GET/POST | `/feeds` | `news:manage` | 查看或添加 RSS/Atom 来源。 |
| PATCH/DELETE | `/feeds/{source_id}` | `news:manage` | 修改/删除来源或启停来源。 |
| POST | `/feeds/{source_id}/collect` | `news:manage` | 手动触发单个来源采集。 |
| GET | `/content?status=&q=&page=&pageSize=` | `news:manage` | 搜索/筛选并分页管理新闻。status 可为 draft、published、archived。 |
| GET/POST | `/content/{news_id}` / `/content` | `news:manage` | 读取或新建新闻草稿。 |
| PATCH | `/content/{news_id}` | `news:manage` | 编辑新闻。编辑已发布内容会退回草稿。 |
| PATCH | `/content/{news_id}/status` | `news:manage` | 设置为草稿、已发布或归档。 |

采集源仅接受 HTTP(S) URL；实际连接时还会校验目标地址、拒绝私有网段、禁止重定向、限制 15 秒和 2 MiB。Feed 采集不是全文爬虫，也不会自动定时运行。

### 健康检查与响应

- `GET /` 返回服务名和基本运行状态。
- `GET /health/live` 表示 API 进程有响应。
- `GET /health/ready` 同时检查 MySQL 和 Redis；任一失败时返回 HTTP 503。
- 多数 JSON API 使用 `{"code": 200, "message": "success", "data": ...}` 格式；Agent Chat 使用 `text/event-stream`。
- 非生产模式提供 Swagger `/docs`、ReDoc `/redoc` 和 OpenAPI `/openapi.json`；生产模式关闭这三项。

## 数据与迁移

新建空数据库时，先建立 `news_app` 并按部署说明导入 `db/database.sql` 和 `db/agent_schema.sql`。`database.sql` 包含演示数据，生产初始化前应审核并清理演示记录。

既有数据库升级：

~~~powershell
cd news-headline-backend
python -m db.migrations
~~~

开发/本地模式启动时自动运行用户角色和新闻管理结构迁移；这个流程假设基础数据库已存在。它不会创建 Agent 表。若 Agent 表缺失，应单独导入 `db/agent_schema.sql`。生产环境不会在应用启动时改表，数据库管理员应先备份并手动运行迁移。

## 本地启动

1. 启动 MySQL 和 Redis，创建数据库和具备最少所需权限的应用账号。
2. 首次配置时复制 `.env.example` 为 `.env`；已有本地 `.env` 时不要覆盖。将 DATABASE_URL、REDIS_URL 和 AI_API_KEY 改为本机实际配置。
3. 安装 Python 依赖并启动：

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8001 --reload
~~~

从本目录运行 Uvicorn 很重要：`config/settings.py` 会加载此目录的 `.env`。完整数据库初始化和 Docker Compose 步骤见项目根目录 [DEPLOYMENT.md](../DEPLOYMENT.md)。

## 测试

~~~powershell
python -m unittest discover -s tests -v
~~~

测试范围包括密钥与配置校验、权限、模型重试/错误处理、Agent 编排、新闻审核状态和 Feed 解析/安全校验。
