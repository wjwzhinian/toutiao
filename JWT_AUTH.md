# JWT 认证与权限配置

项目已使用 HS256 签发短效 Access Token，并通过 HttpOnly Cookie 管理可轮换的 Refresh Token。

## 本地配置

后端 `news-headline-backend/.env` 已配置随机 `JWT_SECRET_KEY`。`.env` 被 Git 忽略，不要把密钥贴到聊天、代码、日志或提交记录中。若需要重新生成：

```powershell
conda activate fast
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

将生成值保存到 `.env` 的 `JWT_SECRET_KEY`。密钥变更会立即使已签发的 Access Token 失效，用户需重新登录。当前本地设置为 HS256、Access Token 30 分钟、Refresh Token 14 天。

## 认证接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/auth/login` | 登录并返回 Bearer Access Token；Refresh Token 写入 HttpOnly Cookie |
| `POST /api/auth/refresh` | 轮换 Refresh Token 并签发新的 Access Token |
| `POST /api/auth/logout` | 撤销当前 Refresh Token 会话并清除 Cookie |
| `GET /api/auth/me` | 返回当前用户资料和有效权限 |

原有 `POST /api/user/login`、`POST /api/user/register` 保留兼容，返回的 `token` 现在是 JWT。客户端应把 Access Token 仅保存在内存中；前端启动时使用 Refresh Cookie 恢复会话，遇到 401 时自动刷新并重试一次。旧版浏览器持久化的 Token 会清除。

Refresh Token 本身是高熵随机值，数据库只保存 SHA-256 摘要。Access Token 除了验证签名和有效期，每次请求还会检查数据库中的会话、账号启用状态和当前角色，因此注销、禁用账号和角色变更即时生效。密码仍由 bcrypt 哈希保存；密码修改会撤销用户已有会话。

## 角色与保护

| 角色 | 主要能力 |
| --- | --- |
| `admin` | 用户角色/状态管理、来源与自动发布策略、采集与 Agent 运营 |
| `editor` | 查看并编辑新闻稿 |
| `reviewer` | 查看审核队列、检索证据、标注审核反馈和查看评估 |
| `viewer` | 个人资料、收藏、历史与 Agent 基础访问 |
| `user` | 兼容历史账号，权限等同 `viewer` |

后端逐接口校验权限；前端隐藏入口只用于界面体验，不构成权限边界。管理员可通过 `PATCH /api/admin/users/{user_id}/role` 分配角色，通过 `PATCH /api/admin/users/{user_id}/active` 禁用或重新启用账号。禁用会撤销其 Refresh 会话，系统不允许停用最后一个管理员。

## 数据库迁移

本地/开发模式启动时会自动给 `user` 表添加 `is_active`，并创建 `auth_refresh_sessions` 表。Docker 初始 SQL 也包含对应结构。现有生产数据库应在上线前运行后端迁移：

```powershell
conda activate fast
cd news-headline-backend
python -m db.migrations
```

## 部署注意

生产环境须设置不少于 32 个字符的随机密钥、`JWT_COOKIE_SECURE=true`、HTTPS 和精确的 `FRONTEND_ORIGINS`。跨站前后端部署时需配置 `JWT_COOKIE_SAMESITE=none`，同时保持 Secure Cookie；刷新和注销接口会检查浏览器 Origin。JWT 密钥仅通过服务器环境变量、Docker Secrets 或密钥管理服务注入。

项目暂只启用 HS256；多服务部署要采用 RS256 时，需增加私钥管理、密钥轮换和公钥验证配置，不能只把算法环境变量改成 RS256。
