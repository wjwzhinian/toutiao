# 本地开发与部署

本文说明两种运行方式：直接在 Windows/PyCharm 启动前后端并连接本机 MySQL/Redis，或用 Docker Compose 启动一套本地容器。Compose 配置面向本地开发验证，不是生产部署模板。

## 方式 A：本机 MySQL/Redis + 前后端进程

### 1. 准备数据库

启动本机 MySQL 8.x 和 Redis，确认端口、数据库名和账号。常见本地端口分别是 MySQL 3306、Redis 6379；项目环境变量也可配置其他地址。

首次创建数据库时，在后端目录导入基础数据，再导入 Agent 表。可从 PowerShell 调用 cmd 进行 MySQL 文件输入重定向：

~~~powershell
cd news-headline-backend
cmd /c "mysql -u root -p < db\database.sql"
cmd /c "mysql -u root -p news_app < db\agent_schema.sql"
~~~

也可以在 MySQL Workbench 先执行 db/database.sql，再对 news_app 执行 db/agent_schema.sql。基础脚本包含演示新闻，不包含默认用户账号；部署后请先注册并核验运营账号，再按 SECURITY.md 中的步骤授予管理员角色。

如果数据库已存在，首次切换到当前项目版本时，在后端目录完成配置后运行幂等升级：

~~~powershell
python -m db.migrations
~~~

该升级会补充用户角色和新闻采集审核结构。它不会创建基础业务数据库，也不会创建 Agent 表；若 Agent 表缺少，另外导入 db/agent_schema.sql。生产迁移步骤见 SECURITY.md。

### 2. 配置并启动后端

在后端目录首次创建本地环境文件：

~~~powershell
cd news-headline-backend
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
~~~

将以下配置改为本机实际值：

- DATABASE_URL：数据库连接。建议为应用建立专用 MySQL 账号；密码中的 URL 保留字符应进行 URL 编码。
- REDIS_URL：本机 Redis 地址，常见值为 redis://127.0.0.1:6379/0。
- AI_API_KEY：只有要使用 Agent 时需要有效模型 Key。密钥只留在后端 .env。
- APP_ENV=local、HOST=127.0.0.1、PORT=8001 适合本机开发。

安装依赖并从 news-headline-backend 目录启动：

~~~powershell
conda activate fast
python -m pip install -r requirements-dev.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8001 --reload
~~~

在 PyCharm 中新建 Python Run Configuration：

1. Interpreter 选择装有 requirements.txt 依赖的虚拟环境或 Conda 环境。
2. Working directory 指向项目的 news-headline-backend 目录。
3. Run kind 选择 Module name，填写 uvicorn。
4. Parameters 填 main:app --host 127.0.0.1 --port 8001。需要自动重载时再加 --reload。
5. 环境变量可以留在后端 .env，也可以配置到 Run Configuration 的 Environment variables。

后端从所在目录读取 .env，并不会因为打开了项目根目录而自动寻找错误位置。若出现模块找不到，确认解释器安装了依赖；若端口无法绑定，确认 8001 没有其他后端进程占用。

### 3. 启动前端

打开新的终端，从前端目录执行：

~~~powershell
cd news-headline-frontend
conda activate fast
npm ci
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1
~~~

在浏览器打开 Vite 输出的地址，默认通常为 http://127.0.0.1:5173。如果后端使用其他端口，同时调整 VITE_API_BASE_URL 和后端 FRONTEND_ORIGINS。模型 Key 不应传给前端。

## 方式 B：Docker Compose 本地栈

在项目根目录准备 Compose 专用 .env：

~~~powershell
if (!(Test-Path .env)) { Copy-Item .env.docker.example .env }
notepad .env
~~~

把 MYSQL_ROOT_PASSWORD、NEWS_DB_PASSWORD 换成不同的本地密码，把 AI_API_KEY 换成有效的 DeepSeek Key。数据库密码在 Compose URL 中使用，建议只使用 URL 安全字符。

启动并查看容器：

~~~powershell
docker compose up --build
~~~

需要后台运行时使用 docker compose up --build -d，再通过 docker compose ps 和 docker compose logs -f backend 查看状态。

| 服务 | 本机地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5173 |
| API | http://127.0.0.1:8001 |
| Swagger | http://127.0.0.1:8001/docs |
| Readiness | http://127.0.0.1:8001/health/ready |
| Compose MySQL | 127.0.0.1:3307 |
| Compose Redis | 127.0.0.1:6380 |
| Compose Qdrant | 127.0.0.1:6333 |

Compose 另行启动自己的 MySQL 和 Redis 容器，避免与常见本机 3306/6379 端口冲突；容器内的服务通过 Compose 网络互连。若要连接已安装在本机的数据库，请使用方式 A。初始化 SQL 只会在 MySQL 数据卷首次创建时导入；后续启动会保留已有数据。

停止服务使用 docker compose down，它会保留数据卷。docker compose down -v 会永久删除 Compose MySQL 和 Redis 数据，只在确实要清空本地数据时使用。

Qdrant 默认随 Compose 启动，但 `QDRANT_ENABLED=false`，因此未配置 Embedding 服务时原有检索仍走 MySQL。需要向量检索时，在项目 `.env` 中设置 `QDRANT_ENABLED=true`，并配置 `EMBEDDING_API_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION` 和 `EMBEDDING_VERSION`。Embedding 凭据必须来自支持 OpenAI 兼容 `/embeddings` 接口的服务；现有聊天模型密钥不保证可用于向量生成。配置后执行 `docker compose up -d --build backend worker beat`，Worker 会自动按批次补建已发布新闻索引。

后台管理员可用 `GET /api/admin/vector-index` 查看索引状态，用 `POST /api/admin/vector-index/rebuild` 触发全量重建。更换 Embedding 模型或维度时，更新版本标识；维度变化时还需使用新的 `QDRANT_COLLECTION`，重建完成并验证后再清理旧集合。修改或撤回新闻后，对账 Worker 会更新或删除向量；Qdrant 暂时不可用时，证据检索回退到 MySQL，并在 `/health/ready` 的 `qdrant` 检查中显示降级。

## 上线前配置

部署到公网前应按 SECURITY.md 完成数据库迁移和管理员账号准备，并至少做到：

- 使用 HTTPS 反向代理和有效证书；生产时设置 APP_ENV=production。
- 将 ALLOWED_HOSTS 配成真实 API 主机名，将 FRONTEND_ORIGINS 配成真实前端 HTTPS Origin；不要使用通配符。
- 为应用创建最小权限数据库账号，不使用 MySQL root。
- 通过平台 Secret Store 注入数据库、Redis 和模型凭据；不要把 .env 放入镜像、仓库或构建日志。
- 先备份数据库并手动执行迁移，再部署新版本；生产应用启动不会自动改表。
- 使用独立的生产级数据库、Redis、备份和监控方案。当前 Compose 文件只用于本地开发验证。

生产环境会关闭 Swagger、ReDoc 和 OpenAPI 地址。/health/live 仅确认 API 进程存活，/health/ready 会检查 MySQL 和 Redis。

## 自动采集与审核发布的部署检查

自动流程由 FastAPI 应用生命周期启动，依赖同一套 MySQL、Redis 和可用的模型 API。后端 `.env` 或 Compose 根目录 `.env` 至少配置：

```env
AUTO_PIPELINE_ENABLED=true
AUTO_PIPELINE_INTERVAL_SECONDS=60
AUTO_PUBLISH_MODE=shadow
AI_API_KEY=replace-with-your-deepseek-key
```

`AUTO_PIPELINE_INTERVAL_SECONDS` 可设为 15 到 3600 秒，默认 60 秒。将 `AUTO_PIPELINE_ENABLED` 设为 `false` 会暂停定时采集和自动审核；管理员仍可手动测试、采集和审核。生产环境启用前先备份数据库并按前文完成数据库初始化和迁移。生产模式不会在应用启动时自动修改数据库结构。

`AUTO_PUBLISH_MODE` 默认必须保持为 `shadow`：通过所有规则的稿件只记为影子候选并保持草稿。来源谱系、稿源分组、关键主张独立证据和人工评估达标后，管理员才可显式设置为 `enforce` 并重启服务。自动发布门槛、反馈指标和回滚说明见 [NEWS_REVIEW_GOVERNANCE.md](NEWS_REVIEW_GOVERNANCE.md)。

管理员在“新闻采集与内容管理”页按以下顺序授权：

1. 新增 RSS/Atom 来源后先点“测试”。测试成功后再启用来源；更改来源地址后需要重新测试。
2. 为来源单独开启定时采集并设置 5 到 1440 分钟的间隔。采集到的稿件先保存为草稿。
3. 只有经过人工评估的来源才开启“可信来源”，并在“自动发布分类白名单”中启用允许自动发布的分类。
4. 在“自动审核记录”查看自动发布、转人工复核和审核异常的理由；自动发布的稿件可由管理员撤回。

自动发布还要求已登记的官方原始来源或独立媒体、稿源分组、内容长度、来源 URL、逐条站内证据及关键主张的多源独立支持，并且没有重复稿、异常链接、确定性敏感内容或广告风险。模型输出只提供审核建议；关键风险由规则引擎决定。`AUTO_PUBLISH_MODE=shadow` 时即使全部规则通过也只记录候选并保持草稿，需管理员明确切到 `enforce` 才会执行发布。

当前定时器随每个后端 API 进程运行，Redis 分布式锁用于避免多进程重复处理同一来源或稿件。所有后端实例必须连接同一个可用 Redis。部署或排查时可查看后端日志中的 `Automatic news pipeline started`、`Automatic news pipeline tick completed` 和 `News auto-published` 记录；访问 `/health/ready` 可确认 MySQL 与 Redis 连通。高吞吐部署应将调度器迁移到独立 Worker，并保留相同的 Redis 锁和发布规则。

在 Conda `fast` 环境运行回归检查（后端和前端命令分别在对应目录执行）：

```powershell
cd news-headline-backend
conda run -n fast python -m pytest -q -p no:cacheprovider tests
cd ..\news-headline-frontend
conda run -n fast npm run build
```
