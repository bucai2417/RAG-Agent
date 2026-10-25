import { defineStore } from 'pinia'
import { authApi, type User } from '@/api'

interface AuthState {
  user: User | null
  loading: boolean
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({ user: null, loading: true }),
  getters: {
    /** 权限判定：admin 兜底全权；支持通配 '*' */
    hasPermission: (state) => (code?: string) => {
      if (!code) return true
      const u = state.user
      if (!u) return false
      if (u.is_admin) return true
      const p = u.permissions || []
      return p.includes('*') || p.includes(code)
    },
  },
  actions: {
    async fetchMe() {
      this.loading = true
      try {
        this.user = await authApi.me()
      } catch {
        this.user = null
      } finally {
        this.loading = false
      }
    },
    async login(username: string, password: string, tenantSlug?: string) {
      const r = await authApi.login(username, password, tenantSlug)
      localStorage.setItem('access_token', r.access_token)
      if (r.refresh_token) localStorage.setItem('refresh_token', r.refresh_token)
      await this.fetchMe()
      return r
    },
    logout() {
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
      this.user = null
      this.loading = false
    },
  },
})
