# 前端说明

此目录是新闻头条系统的 Vue 3 单页应用，由 Vite 开发和构建。应用负责展示新闻、用户资料、收藏/历史、Agent 对话和管理员新闻管理页面；所有业务数据和权限判断由后端 API 提供。

整体模块说明见项目根目录 [README.md](../README.md)，本地运行和 Docker 方式见 [DEPLOYMENT.md](../DEPLOYMENT.md)。

## 技术和目录

| 路径 | 作用 |
| --- | --- |
| `src/main.js`、`src/App.vue` | 创建 Vue 应用、挂载路由/Pinia/语言支持和应用外壳。 |
| `src/router/index.js` | Vue Router 页面定义及管理员页面的前端路由守卫。 |
| `src/views/` | 首页、分类、新闻详情、登录注册、个人中心、收藏、历史、AI 对话、设置和新闻管理页面。 |
| `src/components/` | 新闻卡片、导航等可复用 UI。 |
| `src/store/` | Pinia 状态：用户、新闻、收藏、历史、主题和语言。 |
| `src/config/api.js` | API 基础地址配置。 |
| `src/i18n/` | 中英文界面资源。 |
| `src/style.css` | 全局样式。 |

主要页面路由：

| 路径 | 页面 |
| --- | --- |
| `/home` | 新闻首页和分类浏览入口。 |
| `/category` | 新闻分类。 |
| `/news/detail/:id` | 新闻详情及相关文章。 |
| `/login`、`/register` | 登录与注册。 |
| `/favorite`、`/history` | 个人收藏和浏览历史。 |
| `/aichat` | 新闻 Agent 对话。 |
| `/my`、`/profile`、`/settings` | 个人中心、资料和设置。 |
| `/admin/news` | 管理员新闻采集与内容审核。 |

## 前端状态与后端通信

普通 API 请求使用 Axios；AI 对话通过 Fetch 读取 `text/event-stream`。登录状态、用户资料和 Token 目前通过 Pinia 持久化插件存入浏览器 `localStorage`。因此，用户退出登录时应调用界面退出操作；浏览器本地数据也应按可信设备管理。

`src/config/api.js` 读取 `VITE_API_BASE_URL`，未设置时默认为 `http://127.0.0.1:8001`。Vite 在构建时将该地址写入静态文件，部署时必须指向浏览器能够访问的后端地址。模型密钥绝不能放入任何 `VITE_*` 变量，因为这类变量会进入前端构建产物。

管理员路由的前端守卫用于未登录跳转和界面体验；即使用户手动构造请求，后端仍通过角色权限依赖强制验证访问权限。

## 本地开发

需要 Node.js 和 npm。PowerShell 从本目录运行：

~~~powershell
npm ci
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1
~~~

打开 Vite 在终端显示的本地地址，通常是 `http://127.0.0.1:5173`。后端必须先运行；若更换后端端口，需要同步修改 `VITE_API_BASE_URL`。后端 CORS 配置 `FRONTEND_ORIGINS` 也要允许当前前端源。

前端可执行：

~~~powershell
npm run build
npm run preview
~~~

`npm run build` 生成 `dist/` 静态文件；`npm run preview` 用于本地预览构建产物。`package.json` 当前没有前端单元测试脚本。

## 容器构建

Dockerfile 使用 Node 构建静态资源，再由 Nginx 提供服务。构建参数 `VITE_API_BASE_URL` 会烘焙进静态资源。项目根目录的 Docker Compose 会把前端映射到本机 5173 端口；浏览器访问的 API 地址默认指向本机 8001 端口，而容器之间的后端连接使用 Compose 网络。

生产部署应在构建前指定正确的公开 API URL，并在后端设置对应的 HTTPS CORS Origin 和允许的 Host。
