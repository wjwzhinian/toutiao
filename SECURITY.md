# 安全配置与权限说明

本文记录当前代码中的安全边界，以及生产部署必须完成的配置。密钥和数据库凭据应保存在本地忽略的 .env 或部署平台 Secret Store，不应进入代码、前端构建变量、镜像层、提交记录或日志。

## 密钥和运行配置

- 后端从 news-headline-backend/.env 或进程环境读取 DATABASE_URL、REDIS_URL、AI_API_KEY、允许的 Host/Origin 和运行限制。
- Docker Compose 使用项目根目录的 .env，模板是 .env.docker.example；后端进程运行模板是 news-headline-backend/.env.example。
- .gitignore 排除 .env 和 .env.*，仅放行示例文件。提交前仍应检查 git status，避免将真实凭据复制进 Markdown、HTTP 示例或构建日志。
- Settings 对数据库、Redis 和模型密钥字段做隐藏表示；生产校验拒绝空/占位模型密钥、通配 Host、非 HTTPS 前端/模型来源、root 数据库账号、SQL 回显和明显的示例数据库凭据。
- 如果服务密钥曾经被提交或公开，应在对应服务端撤销并换发；仅从当前文件删除并不能清除 Git 历史。

## 用户身份和角色

用户通过 Bearer Token 调用受保护 API。Token 为随机不透明值，成功登录时返回给客户端；数据库只保存 SHA-256 摘要，旧版明文 Token 在后续成功校验时会升级为摘要。Token 默认 7 天后过期，密码修改会撤销活动 Token。密码使用 bcrypt 哈希，不保存明文。

| 角色 | 权限 |
| --- | --- |
| user | 个人资料读写、收藏读写、浏览历史读写、Agent 使用。 |
| admin | 包含 user 全部权限，另有 news:manage 和 users:manage_roles。 |

后端通过每个接口的权限依赖执行授权；Vue 路由守卫只是页面层体验，不能替代 API 授权。个人收藏、历史和 Agent 会话按用户 ID 隔离。管理员角色管理接口禁止移除最后一个管理员。

若升级旧库，迁移会为现有账户增加 role 字段并赋为 user，不会猜测管理员账号。数据库管理员应验证并重置指定运营账号的密码，然后通过受控数据库会话提升角色：

~~~sql
UPDATE `user` SET `role` = 'admin' WHERE `username` = 'your-verified-admin-account';
~~~

完成后使用该账号登录，并通过 GET /api/admin/access 核对权限。后续角色变更优先使用 PATCH /api/admin/users/{user_id}/role。

## 数据库结构迁移

开发/本地模式启动时会自动执行角色与新闻管理表结构的幂等迁移。生产应用启动不会自动 ALTER 表。生产升级前应：

1. 备份并验证数据库恢复方案。
2. 使用具有 ALTER/CREATE 权限的数据库迁移账号运行迁移。
3. 确认 Agent 表已通过 db/agent_schema.sql 初始化。
4. 再部署应用；应用运行账号不应拥有不必要的 DDL 权限。

从 news-headline-backend 目录运行：

~~~powershell
python -m db.migrations
~~~

此命令会为旧结构增加 user.role、news.status、Feed 来源关联、来源 ID、内容哈希、审核字段和相关索引/约束，并创建 news_feed_source。它假设基础业务表已经存在，也不会创建 Agent 会话表。缺少 Agent 表时，应另行执行 db/agent_schema.sql。

## 新闻公开范围和采集

公开新闻、收藏/历史中关联的新闻、Agent 工具查询都只显示 published 状态。新采集内容进入 draft，编辑已发布新闻会退回草稿；归档和草稿内容不对普通读者公开。缓存命中后会重新确认新闻仍处于已发布状态。

Feed 采集会请求管理员登记的 RSS/Atom URL，因此服务端限制：

- 只接受 HTTP(S) 80/443 端口，拒绝嵌入 URL 的用户名/密码。
- DNS 解析后拒绝本机、私有和非公网 IP，禁止跟随重定向。
- 禁用 HTTP 客户端环境代理，连接超时 5 秒、总请求超时 15 秒、响应体最多 2 MiB。
- 采集由管理员手动触发，Redis 锁避免同一来源重复并发执行。

部署者仍需登记有权使用的来源，并在发布前审核新闻准确性和来源授权。当前实现不自动调度采集，也不是通用网站爬虫。

## Agent 安全边界

Agent 仅接受已认证且具有 agent:use 权限的用户；会话 ID 由服务端按用户所有权校验。工具只读已发布新闻，模型不能提交原始 SQL、改变内容状态或访问任意外部 URL。Redis 按用户限流，默认 60 秒最多 10 次，并限制同一用户并发生成。

DeepSeek 请求使用后端配置的 AI_API_KEY。生产时必须使用 HTTPS Provider 地址；不要将模型原始错误或凭据返回给前端。Agent 的热门和趋势只统计本地数据库，不应对外描述为实时互联网数据。

## 生产运行边界

部署到公网时：

- 设置 APP_ENV=production；交互式 API 文档会被关闭。
- ALLOWED_HOSTS 只包含 API 实际 Host；FRONTEND_ORIGINS 只列出真实的 HTTPS 前端源。
- 使用 TLS 终止代理、受限数据库账号、受保护的 Redis 网络、Secret Store、备份和日志监控。
- 限制管理 API 的网络和账号访问；定期复核管理员成员及模型服务额度。
- 浏览器目前将用户 Token 保存在 localStorage。应使用 HTTPS、保持内容安全和 XSS 防护；若企业策略要求 HttpOnly Cookie，需要另行实现对应认证流程。

当前 Agent 有执行记录和工具调用表，部分管理操作写入服务端日志；项目尚无独立的不可变管理员审计归档、SSO/MFA、组织租户隔离、后台任务队列或高可用部署方案。上线范围应据此进行评估。

## 回归测试

~~~powershell
cd news-headline-backend
python -m unittest discover -s tests -v
~~~

相关测试覆盖安全配置校验、角色权限、令牌处理、Provider 错误和重试、Agent 执行以及采集 URL 安全校验。
