# 新闻头条与 Agent 系统

这是一个 Vue 3 + FastAPI 的新闻阅读、个人收藏与历史、新闻采集审核和 AI 新闻问答项目。后端以 MySQL 保存业务数据、Redis 提供缓存和并发控制，并通过服务端配置连接 DeepSeek 的 OpenAI 兼容接口。前端不直接持有模型密钥。

## 当前系统能力

| 子系统 | 已实现能力 |
| --- | --- |
| 新闻阅读 | 按分类分页浏览新闻、查看详情和相关文章；公开查询只返回已发布内容。 |
| 用户与个人数据 | 注册、登录、查看/修改个人资料和密码；登录用户可以维护收藏和浏览历史。 |
| 角色权限 | `user` 与 `admin` 两种角色；后端按权限依赖保护个人数据、Agent 和管理 API。 |
| 新闻 Agent | 使用模型进行新闻相关问答；可以调用受控的新闻检索、详情、摘要、比较、热度和趋势工具，并以 SSE 返回过程和结果。 |
| 新闻采集 | 管理员先测试并启用 RSS/Atom 来源，可配置每个来源的定时采集间隔；新稿先进入草稿并自动去重。 |
| 自动审核发布 | 默认使用影子模式评估自动发布候选，不更改稿件状态；管理员确认来源谱系和分类白名单并完成人工评估后，才可切换为执行模式。 |
| 内容管理 | 管理员搜索和分页查看草稿、已发布和归档内容；可新建、编辑、审核发布、下架为草稿或归档。 |
| 运行检查 | `/health/live` 检查 API 进程；`/health/ready` 检查 MySQL 和 Redis 依赖。 |

## 系统架构

部署差异：单进程开发模式可由 FastAPI 生命周期运行定时流水线；Docker Compose 模式使用 Celery Beat 投递任务、Celery Worker 执行任务，并关闭 API 内置调度，避免重复运行。

~~~mermaid
flowchart LR
  Browser[浏览器 / Vue 3] -->|REST JSON、SSE| API[FastAPI API]
  API --> Router[路由与权限]
  Router --> Service[业务服务 / Agent 编排]
  Service --> CRUD[CRUD 数据访问]
  CRUD --> MySQL[(MySQL)]
  API --> Redis[(Redis 缓存、限流和分布式锁)]
  Service -->|OpenAI 兼容 API| Model[DeepSeek 模型服务]
  Service -->|安全读取 RSS / Atom| Feeds[已审核新闻源]
  Scheduler[定时采集与自动审核] --> Service
  Scheduler --> Redis
~~~

一次普通页面请求从 Vue 页面发起，经 FastAPI 路由验证参数和权限，再由 CRUD 层读写 MySQL；适合缓存的新闻列表、分类和详情由 Redis 加速。Agent 请求经过身份校验、用户级限流和并发锁，再由编排服务调用模型；模型只能选择服务端允许的只读新闻工具。RSS 采集既可由管理员手动触发，也可由 FastAPI 生命周期中的后台调度器按周期运行；调度器只读取数据库中已测试、已启用且开启定时采集的来源。Redis 分布式锁避免多个 API 进程同时处理同一来源或稿件。

## 系统各部分

### 前端：`news-headline-frontend`

前端使用 Vue 3、Vite、Vue Router、Pinia、Vant、Axios 和 Vue I18n。

- `src/main.js` 初始化 Vue、Pinia、路由和国际化；`src/App.vue` 是应用外壳。
- `src/router/index.js` 定义首页、分类、详情、登录、注册、收藏、历史、Agent 对话、个人资料和设置等页面。管理员页面为 `/admin/news`。路由守卫用于改善页面体验，真正的权限检查仍由后端执行。
- `src/views/` 按页面组织交互；`NewsManagement.vue` 提供采集源和内容审核界面，`AIChat.vue` 使用 Fetch 读取 Agent SSE。
- `src/store/` 保存用户、新闻、收藏、历史、主题和语言等前端状态；用户状态当前由 Pinia 持久化到浏览器 `localStorage`。
- `src/config/api.js` 读取构建时变量 `VITE_API_BASE_URL`，开发默认值为 `http://127.0.0.1:8001`。DeepSeek Key 只配置在后端。
- `src/components/` 放置新闻列表项和底部导航等复用组件；`src/i18n/` 放置中英文资源。

### 后端：`news-headline-backend`

后端使用 FastAPI、SQLAlchemy 异步 ORM、aiomysql、Redis asyncio 客户端和 Pydantic。

- `main.py` 创建 FastAPI 应用、注册路由、CORS、Trusted Host 和统一异常处理；开发/本地模式会在启动时执行角色与新闻管理结构的幂等迁移。生产模式不会自动改表，并关闭 Swagger、ReDoc 和 OpenAPI 地址。
- `routers/` 是 HTTP 接口层，负责接收请求、依赖注入和调用业务代码；`schemas/` 定义输入验证与字段别名。
- `crud/` 封装用户、新闻、收藏、历史的数据库读写。公开新闻和 Agent 新闻工具只查询 `published` 状态；缓存命中后也会再次检查新闻当前是否已发布。
- `services/news_collection.py` 负责 Feed 下载、RSS/Atom 解析、纯文本清理、去重和草稿入库。
- `services/auto_pipeline.py` 负责按计划采集、审核候选稿和执行确定性发布门槛。AI 只能提供审核建议；任何安全检查失败都会留在人工审核队列。
- `agent/review.py` 提供新闻库证据匹配、重复稿检测、链接静态检查和敏感/广告规则检查，不会请求稿件中的链接。
- `agent/` 包括模型网关、Agent 编排和只读新闻工具。`provider.py` 处理兼容接口、流式输出、超时及有限重试；`service.py` 控制工具调用、保存对话和运行记录；`tools.py` 限定 Agent 的新闻数据访问范围。
- `config/settings.py` 从后端 `.env` 或进程环境读取配置并进行生产环境检查；`config/db_confing.py` 与 `config/cache_conf.py` 提供 MySQL、Redis 客户端。
- `db/database.sql` 是基础数据库和演示数据脚本；`db/agent_schema.sql` 创建 Agent 会话、消息、运行和工具调用表；`db/migrations.py` 为已存在数据库增加角色及新闻审核/采集结构。
- `tests/` 覆盖密钥和权限、模型 Provider、Agent 服务、新闻管理与 Feed 采集。

### 数据库与缓存

MySQL 是业务数据的持久化来源。主要表包括：

| 表 | 职责 |
| --- | --- |
| `user`、`user_token` | 用户身份、角色与登录令牌摘要。 |
| `news_category`、`news`、`related_news` | 分类、新闻正文/状态/浏览量和相关文章关系。 |
| `news_feed_source` | RSS/Atom 来源与管理员登记的来源类型、稿源分组；来源谱系变更会重置自动发布授权。 |
| `news_auto_publish_categories` | 管理员明确允许自动发布的分类及启用时间。 |
| `news_auto_review_logs` | 自动审核结果、按条主张证据、规则命中、影子候选和撤回记录。 |
| `news_review_feedback` | 编辑对建议的采纳、驳回或修改，稿件发布判断、证据引用准确性与理由；为误放率、误拦率和引用准确率提供评估样本。 |
| `favorite`、`history` | 用户收藏和浏览历史。 |
| `agent_sessions`、`agent_messages`、`agent_runs`、`agent_tool_calls` | Agent 用户会话、消息、执行状态和工具调用记录。 |

Redis 目前承担新闻读取缓存、Agent 用户级请求计数/并发锁以及自动采集和审核的分布式互斥锁。MySQL 保存来源配置、发布策略和审核审计记录。定时器随启用自动流程的 FastAPI 进程运行；停止所有后端进程会暂停定时采集，重新启动后继续按上次采集时间判断是否到期。

### 权限与数据边界

公开接口包括根路径服务状态、健康检查、新闻分类/列表/详情、注册和登录。收藏、历史、个人信息和 Agent API 需要 `Authorization: Bearer <token>`。普通用户具备个人资料、收藏、历史和 Agent 使用权限；管理员额外具备 `news:manage` 和 `users:manage_roles`。服务端每次按数据库用户角色检查权限，不依赖前端页面是否显示。

Agent 会话归属于认证用户；查询历史会话时同样校验用户所有权。Agent 工具仅读取已发布新闻，不执行模型生成的 SQL、不编辑内容，也不抓取任意 URL。所谓“热门”和“趋势”是项目数据库内新闻的浏览量/发布量统计，不代表实时互联网趋势。

## 主要业务流程

### 采集与审核

1. 管理员登记来源后先运行 RSS 测试；新来源始终以停用状态创建，测试成功后才可启用。
2. 管理员为来源设置 5–1440 分钟的采集间隔并开启定时采集。应用默认每 60 秒检查一次到期来源；每轮最多处理 5 个到期来源和 10 篇待审核稿件。
3. 服务器仅请求 HTTP(S) 80/443，拒绝本机和私有网络目标、不跟随重定向，并限制 15 秒请求时间和 2 MiB 响应体。RSS/Atom 条目会清洗为纯文本，再按来源条目 ID 和内容哈希去重。
4. 所有采集条目先写入 `draft`。仅管理员标记为可信的来源、且分类已加入自动发布白名单，并且稿件是在这两项授权后采集的，才进入自动发布审核候选。
5. 自动发布候选必须通过来源谱系、稿源可信组、关键主张多源独立证据、安全规则和人工标注可评估的门槛。`AUTO_PUBLISH_MODE=shadow` 只写影子审核记录；只有明确配置 `enforce` 才能执行自动发布。
6. 管理员在“自动审核记录”查看自动发布、人工复核和审核异常的结果与拦截原因。自动发布可撤回到归档；管理员修改自动发布稿件或将其下架时，新闻会退出公开状态并记录撤回信息。

### Agent 问答

1. 前端发送用户消息和可选会话 ID，并携带 Bearer Token。
2. 后端验证 `agent:use`，按用户隔离会话，使用 Redis 限制每个用户默认每 60 秒最多 10 次请求，并限制同一用户同一时刻只有一个生成任务。
3. 模型在服务端允许的只读新闻工具中选择操作；工具结果带新闻来源信息，最终回答通过 SSE 事件发送。
4. 会话消息、执行结果和工具调用写入 Agent 表；临时模型故障按有限次数重试，并记录安全错误码。

## API 模块概览

完整接口可在开发环境访问 `http://127.0.0.1:8001/docs`。

| 模块 | 主要路径 | 访问要求 |
| --- | --- | --- |
| 健康状态 | `GET /`、`GET /health/live`、`GET /health/ready` | 公开 |
| 新闻阅读 | `GET /api/news/categories`、`GET /api/news/list`、`GET /api/news/detail?id=...` | 公开，只返回已发布新闻 |
| 用户 | `POST /api/user/register`、`POST /api/user/login`、`GET /api/user/info`、`PUT /api/user/update`、`PUT /api/user/password`、`GET /api/user/permissions` | 注册/登录公开，其余需登录 |
| 收藏 | `/api/favorite/check`、`add`、`remove`、`list`、`clear` | 需登录 |
| 浏览历史 | `/api/history/add`、`list`、`delete/{news_id}`、`clear` | 需登录 |
| Agent | `POST /api/agent/chat`、`GET /api/agent/sessions`、`GET /api/agent/sessions/{session_id}/messages` | 需登录及 `agent:use` |
| 内容与采集管理 | `/api/admin/feeds`、`/api/admin/feeds/{id}/collect`、`/api/admin/content` | 管理员 `news:manage` |
| 自动发布策略与审计 | `/api/admin/auto-publish/categories`、`/api/admin/auto-publish/logs`、`/api/admin/auto-publish/evaluation`、`/api/admin/auto-publish/logs/{id}/feedback`、`/api/admin/auto-publish/logs/{id}/withdraw` | 管理员 `news:manage` |
| 角色管理 | `GET /api/admin/access`、`PATCH /api/admin/users/{user_id}/role` | 管理员 `users:manage_roles` |

新闻列表使用 `categoryId`、`page`、`pageSize`；管理内容支持 `status`、`q`、`page`、`pageSize` 筛选。多数 JSON API 使用 `code/message/data` 包装，分页对象另外带总数和是否有下一页。Agent 对话是 `text/event-stream`，不是普通 JSON 响应。

## 本地开发

需要 Python、Node.js/npm、MySQL 8.x 和 Redis。完整的 PowerShell 步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

后端在独立终端中从 `news-headline-backend` 目录运行，配置保存在 `news-headline-backend/.env`：

~~~powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8001 --reload
~~~

前端在另一终端从 `news-headline-frontend` 运行：

~~~powershell
npm ci
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1
~~~

在浏览器打开 Vite 输出的本地地址。Agent 还需要在后端 `.env` 配置有效的模型密钥；新闻阅读、账户等其他 API 不依赖模型服务。MySQL 和 Redis 必须先启动，且数据库账号、库名和端口须与后端 `.env` 一致。

## 初始化与迁移

新建本地数据库时，先导入 `news-headline-backend/db/database.sql`，再导入 `news-headline-backend/db/agent_schema.sql`。前者创建基础表并导入演示分类/新闻，后者创建 Agent 表。已有库升级时，在后端环境配置完成后运行 `python -m db.migrations`；缺少 Agent 表时另行执行 `db/agent_schema.sql`。开发模式启动会自动执行角色和新闻管理迁移，但不会替你创建整个基础数据库，也不会创建 Agent 表。

生产数据库迁移步骤、权限提升和安全配置见 [SECURITY.md](SECURITY.md)。本地 Docker Compose 启动方式见 [DEPLOYMENT.md](DEPLOYMENT.md)。
- [JWT 认证与角色权限配置](JWT_AUTH.md)

## 检查与测试

~~~powershell
cd news-headline-backend
python -m unittest discover -s tests -v
~~~

前端提供 `npm run dev`、`npm run build` 和 `npm run preview` 脚本；当前没有独立的前端单元测试脚本。生产部署前应在目标环境分别验证后端测试、前端构建、数据库迁移和 `/health/ready`。

## 文档索引

- [Agent 采集与内容审核](AGENT_OPERATIONS.md)
- [后端模块与 API 说明](news-headline-backend/README.md)
- [前端模块说明](news-headline-frontend/README.md)
- [Agent 配置与流程](news-headline-backend/AGENT_SETUP.md)
- [本地开发和 Docker 部署](DEPLOYMENT.md)

## 企业级能力实现状态（2026-10）

当前版本已经实现：

| 能力 | 实现与验证方式 |
| --- | --- |
| 独立调度 Worker | `news-headline-backend/celery_app.py` 提供 Celery Worker/Beat 任务入口，复用 Redis 锁、来源级指数退避和审核流水线；Compose 默认关闭 API 内置调度。 |
| JWT 与 RBAC | 接入 HS256 Access Token、HttpOnly Refresh Cookie 轮换、admin/editor/reviewer/viewer 权限、账号禁用、服务端权限检查和认证审计；详见 `JWT_AUTH.md`。 |
| Agent 可靠性 | 模型超时、连接错误和临时 HTTP 错误退避重试；工具调用、运行状态和延迟落库，SSE `done` 增加结构化摘要。 |
| 回归评测 | `news-headline-backend/tests/evaluations/agent_cases.json` 覆盖重试状态和权限边界，`pytest -q` 不访问真实模型。 |
| Prometheus 指标 | `/metrics` 输出请求、Agent 运行、工具调用、证据数和 Redis 汇总的流水线运行/处理数量；Compose 增加 Prometheus 服务与持久化数据卷。 |
| 一键启动 | Compose 包含 MySQL、Redis、API、Celery Worker/Beat、Prometheus 和前端，Redis 锁保证多进程幂等。 |

仍需准备或部署后才能完整实现的项目：

2. **Grafana**：Prometheus 和持久化已接入；Grafana 仪表盘、告警阈值和邮件/企业微信/钉钉通知渠道仍需部署配置。
3. **全文/混合检索**：MySQL 8 ngram FULLTEXT 与 `LIKE` 关键词召回并用，再按标题、摘要、正文、相关度及来源多样性排序；无 ngram 索引时安全回退到 `LIKE`。向量检索仍需向量模型、向量库及索引重建计划。
4. **CI/CD 发布**：仓库内已加入 `.github/workflows/ci.yml`，可运行后端测试、编译检查和前端构建；自动部署还需要 Runner、镜像仓库、部署目标、环境密钥、迁移审批和回滚策略。

Compose 运行前请在项目根目录 `.env` 设置 `MYSQL_ROOT_PASSWORD`、`NEWS_DB_PASSWORD` 和 `AI_API_KEY`。开发环境可直接 `docker compose up --build`；若只运行 API，请保持后端 `.env` 的 `AUTO_PIPELINE_ENABLED=true` 使用内置调度，或将其关闭后单独运行 `celery -A celery_app.celery_app worker` 与 `celery -A celery_app.celery_app beat`。
- [密钥、权限和数据库安全](SECURITY.md)

## Qdrant 语义与混合证据检索

项目使用 MySQL 保存新闻原文和业务状态，Qdrant 保存新闻语义向量。新闻只有在 `published` 状态下才会进入向量索引；Agent 检索时先用 MySQL 全文/关键词和 Qdrant 语义召回合并候选，再回到 MySQL 校验新闻仍已发布并读取原文、来源和发布时间。Qdrant 或 Embedding API 故障时，证据检索会退回 MySQL。

采集和发布 API 不同步调用 Embedding。Celery Beat 每分钟投递一次索引对账任务，Worker 每批最多处理 `VECTOR_INDEX_BATCH_SIZE` 篇新闻。`news_vector_index_state` 保存在 MySQL，记录待处理、成功、失败、模型版本和重试时间；新闻编辑、状态变化或删除会由对账任务发现并重建/清理，失败任务按退避时间重试。管理员可查看 `GET /api/admin/vector-index`，并通过 `POST /api/admin/vector-index/rebuild` 请求全量重建，这两个接口要求 `feeds:manage` 权限。

在项目根目录 `.env` 中启用并配置独立的 Embedding 服务。聊天 API 密钥不一定有 Embedding 权限，因此必须填写 Embedding 服务专用地址、密钥、模型及实际输出维度：

~~~env
QDRANT_ENABLED=true
QDRANT_URL=http://127.0.0.1:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=news_evidence_v1
EMBEDDING_API_BASE_URL=https://你的Embedding服务/v1
EMBEDDING_API_KEY=你的Embedding密钥
EMBEDDING_MODEL=服务支持的Embedding模型名
EMBEDDING_DIMENSION=1536
EMBEDDING_VERSION=模型与预处理版本标识
VECTOR_API_TIMEOUT_SECONDS=30
VECTOR_INDEX_BATCH_SIZE=50
VECTOR_SEARCH_LIMIT=50
~~~

本地可用 `docker compose up -d qdrant` 启动向量库；Compose 已为 Qdrant 配置独立持久化卷。启用语义检索前，先在 Embedding 服务商处确认模型名称、向量维度和 API 兼容格式。首次启用后，Worker 会按批次为历史已发布新闻补建向量。

本机使用 Conda `fast` 环境运行后端时，先在后端 `.env` 配置上述变量，再分别打开两个终端启动 Celery Worker 和 Beat（Windows 使用 `--pool=solo`）：

~~~powershell
conda activate fast
celery -A celery_app.celery_app worker --loglevel=INFO --pool=solo
~~~

~~~powershell
conda activate fast
celery -A celery_app.celery_app beat --loglevel=INFO
~~~

更换 Embedding 模型或维度时，使用新的 `EMBEDDING_VERSION`；维度变化时还必须使用新的 `QDRANT_COLLECTION`，然后触发全量重建。重建完成前，语义候选会逐步补齐，MySQL 关键词检索仍可使用。确认新集合检索正常后，再手动清理旧集合。新闻正文和业务状态始终以 MySQL 为准，Qdrant 不保存用户信息。

## 当前范围说明

现有实现提供 RSS/Atom 手动及定时采集、基于站内已发布新闻的逐条事实证据建议、来源谱系与稿源去重、确定性风险分流、人工反馈指标和默认影子模式。运行说明见 [NEWS_REVIEW_GOVERNANCE.md](NEWS_REVIEW_GOVERNANCE.md)。外部全网检索与发布后的更正监测不在当前实现范围内。

## 新闻审核治理


JWT 与 RBAC 已实现；配置和迁移说明见 [JWT_AUTH.md](JWT_AUTH.md)。
