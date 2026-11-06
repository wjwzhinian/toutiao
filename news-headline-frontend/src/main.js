import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import pinia from './store'

// 导入Vant组件库
import { 
  Button, 
  NavBar, 
  Tabbar, 
  TabbarItem, 
  Tab, 
  Tabs, 
  List, 
  PullRefresh, 
  Cell, 
  CellGroup,
  Grid,
  GridItem,
  Empty,
  Form,
  Field,
  Image,
  Toast,
  Icon,
  Popup
} from 'vant'

// 导入Vant样式
import 'vant/lib/index.css'

// 导入全局样式
import './style.css'

// 引入国际化
import { setupI18n } from './i18n'
import { installHttpAuth } from './utils/httpAuth'
import { useUserStore } from './store/user'

const app = createApp(App)

// 设置i18n
const i18n = setupI18n()
app.use(i18n)

// 注册Vant组件
app.use(Button)
app.use(NavBar)
app.use(Tabbar)
app.use(TabbarItem)
app.use(Tab)
app.use(Tabs)
app.use(List)
app.use(PullRefresh)
app.use(Cell)
app.use(CellGroup)
app.use(Grid)
app.use(GridItem)
app.use(Empty)
app.use(Form)
app.use(Field)
app.use(Image)
app.use(Toast)
app.use(Icon)
app.use(Popup)

// 删除旧版持久化的 Bearer Token，避免它继续留在浏览器存储中。
localStorage.removeItem('user-store')
localStorage.removeItem('user')

// 使用路由和状态管理
app.use(pinia)
import { useThemeStore } from './store/theme'
installHttpAuth()

// 启动时用 HttpOnly Refresh Cookie 恢复内存中的短效 Access Token。
async function bootstrap() {
  const userStore = useUserStore(pinia)
  await userStore.restoreSession()
  useThemeStore(pinia).initTheme()
  app.use(router)
  await router.isReady()
  app.mount('#app')
}

bootstrap()
