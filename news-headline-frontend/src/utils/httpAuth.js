import axios from 'axios'
import pinia from '../store'
import { useUserStore } from '../store/user'
import { apiConfig } from '../config/api'

let refreshPromise = null

function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = axios.post(`${apiConfig.baseURL}/api/auth/refresh`, null, {
      withCredentials: true,
      __skipAuthRefresh: true,
    }).then((response) => {
      const userStore = useUserStore(pinia)
      userStore.applyTokenResponse(response.data?.data)
      return userStore.token
    }).catch((refreshError) => {
      useUserStore(pinia).clearSession()
      throw refreshError
    }).finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

// 原生 fetch（包括 Agent SSE）也使用同一套刷新和单次重试逻辑。
export async function authenticatedFetch(input, init = {}) {
  const userStore = useUserStore(pinia)
  const send = (token) => {
    const headers = new Headers(init.headers || {})
    if (token) headers.set('Authorization', `Bearer ${token}`)
    return fetch(input, { ...init, headers, credentials: 'include' })
  }

  const firstResponse = await send(userStore.token)
  if (firstResponse.status !== 401) return firstResponse
  const token = await refreshAccessToken()
  return send(token)
}

// 全局 Axios 拦截器让旧页面的 Bearer 请求自动使用内存中的 Access Token。
export function installHttpAuth() {
  axios.defaults.withCredentials = false

  axios.interceptors.request.use((config) => {
    const userStore = useUserStore(pinia)
    const apiOrigin = new URL(apiConfig.baseURL).origin
    const targetOrigin = new URL(config.url || '', config.baseURL || apiConfig.baseURL).origin
    const authRoute = /\/api\/auth\/(login|refresh|logout)/.test(config.url || '')
    if (targetOrigin === apiOrigin && authRoute) config.withCredentials = true
    if (userStore.token && targetOrigin === apiOrigin && !authRoute) {
      config.headers = config.headers || {}
      config.headers.Authorization = `Bearer ${userStore.token}`
    }
    return config
  })

  axios.interceptors.response.use(
    (response) => response,
    async (error) => {
      const request = error.config
      const apiOrigin = new URL(apiConfig.baseURL).origin
      const targetOrigin = request
        ? new URL(request.url || '', request.baseURL || apiConfig.baseURL).origin
        : ''
      if (
        error.response?.status !== 401 ||
        !request ||
        targetOrigin !== apiOrigin ||
        request.__skipAuthRefresh ||
        request.__authRetry ||
        /\/api\/auth\/(login|refresh|logout)|\/api\/user\/(login|register)/.test(request.url || '')
      ) {
        return Promise.reject(error)
      }

      request.__authRetry = true
      const token = await refreshAccessToken()
      request.headers = request.headers || {}
      request.headers.Authorization = `Bearer ${token}`
      return axios(request)
    },
  )
}
