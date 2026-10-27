<template>
  <div class="app-shell">
    <!-- ============ 侧边栏 ============ -->
    <aside class="sider" :class="{ collapsed }">
      <div class="brand" @click="nav('/dashboard')">
        <div class="brand-logo">R</div>
        <span v-show="!collapsed" class="brand-name">企业 AI 中台</span>
      </div>

      <el-scrollbar class="nav-scroll">
        <el-menu
          class="nav-menu"
          :default-active="activeKey"
          :collapse="collapsed"
          :collapse-transition="false"
          unique-opened
          @select="onSelect"
        >
          <template v-for="g in visibleNav" :key="g.key">
            <el-sub-menu v-if="'children' in g" :index="g.key">
              <template #title>
                <el-icon><component :is="g.icon" /></el-icon>
                <span>{{ g.label }}</span>
              </template>
              <el-menu-item
                v-for="c in g.children"
                :key="c.key"
                :index="c.key"
                :title="c.tip || c.label"
              >
                <el-icon><component :is="c.icon" /></el-icon>
                <template #title>{{ c.label }}</template>
              </el-menu-item>
            </el-sub-menu>
            <el-menu-item v-else :index="g.key" :title="g.tip || g.label">
              <el-icon><component :is="g.icon" /></el-icon>
              <template #title>{{ g.label }}</template>
            </el-menu-item>
          </template>
        </el-menu>
      </el-scrollbar>

      <div class="sider-foot">
        <div
          v-if="appVersion && !collapsed"
          class="ver-chip"
          :title="`当前版本 v${appVersion}${deploymentLabel ? ' · ' + deploymentLabel : ''}（点击检查更新）`"
          @click="showAbout = true"
        >
          <span class="ver-dot" :class="{ has: hasUpdate }" />
          <span class="ver-text">v{{ appVersion }}</span>
          <span v-if="hasUpdate" class="ver-new">有新版本</span>
        </div>
        <el-button text class="collapse-btn" @click="collapsed = !collapsed">
          <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
          <span v-show="!collapsed" style="margin-left: 8px">收起侧边栏</span>
        </el-button>
      </div>
    </aside>

    <!-- ============ 主区 ============ -->
    <div class="main">
      <header class="topbar">
        <div class="topbar-left">
          <el-breadcrumb :separator-icon="ArrowRight" class="crumb">
            <el-breadcrumb-item :to="{ path: '/dashboard' }">工作台</el-breadcrumb-item>
            <el-breadcrumb-item v-if="parentLabel">{{ parentLabel }}</el-breadcrumb-item>
            <el-breadcrumb-item v-if="currentLabel && activeKey !== '/dashboard'">
              {{ currentLabel }}
            </el-breadcrumb-item>
          </el-breadcrumb>
        </div>

        <div class="topbar-right">
          <div class="global-search" @click="nav('/chat')">
            <el-icon><Search /></el-icon>
            <span class="gs-text">搜索知识或发起提问</span>
            <span class="gs-kbd">⌘K</span>
          </div>

          <NotificationBell />

          <el-dropdown trigger="click" @command="onUserCmd">
            <div class="user-chip">
              <el-avatar :size="28" :style="avatarStyle">
                {{ (user?.display_name || user?.username || 'U').slice(0, 1).toUpperCase() }}
              </el-avatar>
              <span class="user-name">{{ user?.display_name || user?.username }}</span>
              <el-icon class="user-caret"><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="account">
                  <el-icon><User /></el-icon>我的账号
                </el-dropdown-item>
                <el-dropdown-item command="guide">
                  <el-icon><Reading /></el-icon>使用文档
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <!-- 更新日志滚动条：所有已登录用户可见（独立于下方管理员专属的版本检查） -->
      <ChangelogTicker />

      <main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <ErrorBoundary>
              <component :is="Component" />
            </ErrorBoundary>
          </transition>
        </router-view>
      </main>
    </div>

    <ChatDock v-if="route.path !== '/chat-room'" />

    <AboutUpdateDialog
      v-model="showAbout"
      :current-version="appVersion"
      :deployment="deployment"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight, ArrowDown, Reading } from '@element-plus/icons-vue'
import { NAV, type NavGroup, type NavItem } from '@/config/nav'
import { ROUTE_PERMS } from '@/router/routeMeta'
import { useAuthStore } from '@/stores/auth'
import { systemOpsApi, updateApi } from '@/api'
import NotificationBell from '@/components/NotificationBell.vue'
import ChatDock from '@/components/ChatDock.vue'
import ErrorBoundary from '@/components/ErrorBoundary.vue'
import AboutUpdateDialog from '@/components/AboutUpdateDialog.vue'
import ChangelogTicker from '@/components/ChangelogTicker.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const collapsed = ref(false)
const user = computed(() => auth.user)
const avatarStyle = 'background: linear-gradient(135deg,#2563eb,#3b82f6); font-weight:600'

// ---- 版本 / 检查更新 ----
const appVersion = ref('')
const deployment = ref('')
const hasUpdate = ref(false)
const showAbout = ref(false)
const deploymentLabel = computed(
  () => ({ desktop: '桌面版', docker: 'Docker', source: '源码运行' })[deployment.value] || '',
)

onMounted(() => {
  // 版本号/更新检查都是「锦上添花」的信息，不该挡首屏渲染。
  // 之前这里 await 串行调两个接口，其中一个（更新检查）要打 GitHub，
  // 网络慢时整页像是卡住。现在全部推迟到首屏渲染之后再各自独立发起。
  void loadVersionInfo()
})

let updateTimer: number | null = null

async function loadVersionInfo() {
  if (!auth.user) await auth.fetchMe()
  // 版本/更新接口需 system:manage 权限，非管理员不发请求（否则每次加载都刷 401）
  if (!auth.hasPermission('system:manage')) return
  try {
    const info = await systemOpsApi.info()
    appVersion.value = info.version
    deployment.value = info.deployment
  } catch {
    /* 接口不可用：不显示版本 */
  }
  // 更新检查会访问 GitHub（可能 1~2s，网络差时更久），再往后推一拍，
  // 避免与页面首屏数据请求抢带宽/连接。
  updateTimer = window.setTimeout(() => {
    updateApi
      .check()
      .then((r) => (hasUpdate.value = !!r.update_available))
      .catch(() => {
        /* 后台静默检查失败，不打扰用户 */
      })
  }, 3000)
}

onBeforeUnmount(() => {
  if (updateTimer !== null) {
    window.clearTimeout(updateTimer)
    updateTimer = null
  }
})

const permOf = (i: NavItem) => ROUTE_PERMS[i.key] || i.perm
const canSee = (i: NavItem) => auth.hasPermission(permOf(i) as string | undefined)

const visibleNav = computed(() => {
  const out: (NavItem | NavGroup)[] = []
  for (const g of NAV) {
    if ('children' in g) {
      const kids = g.children.filter(canSee)
      if (kids.length) out.push({ ...g, children: kids })
    } else if (canSee(g)) {
      out.push(g)
    }
  }
  return out
})

const allLeaves = computed(() =>
  NAV.flatMap((g) => ('children' in g ? g.children : [g])) as NavItem[],
)
const activeKey = computed(() => {
  const p = route.path
  const hit = allLeaves.value
    .filter((i) => p === i.key || p.startsWith(i.key + '/'))
    .sort((a, b) => b.key.length - a.key.length)[0]
  return hit?.key || '/dashboard'
})
const currentLabel = computed(() => allLeaves.value.find((i) => i.key === activeKey.value)?.label)
const parentLabel = computed(() => {
  const grp = NAV.find((g) => 'children' in g && g.children.some((c) => c.key === activeKey.value)) as
    | NavGroup
    | undefined
  return grp?.label
})

function onSelect(index: string) {
  if (index.startsWith('/')) router.push(index)
}
function nav(p: string) {
  router.push(p)
}
function onUserCmd(cmd: string) {
  if (cmd === 'logout') {
    auth.logout()
    router.push('/login')
  } else if (cmd === 'account') {
    router.push('/account')
  } else if (cmd === 'guide') {
    router.push('/guide')
  }
}</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--bg-canvas);
}

/* ---- 侧边栏 ---- */
.sider {
  width: var(--sider-w);
  flex-shrink: 0;
  background: var(--bg-surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease;
}
.sider.collapsed { width: var(--sider-w-collapsed); }

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--header-h);
  padding: 0 16px;
  cursor: pointer;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-subtle);
}
.brand-logo {
  width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0;
  background: linear-gradient(135deg, #2563eb, #3b82f6);
  color: #fff; display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 16px; letter-spacing: -0.03em;
  box-shadow: 0 2px 6px -1px rgba(37, 99, 235, 0.35);
}
.brand-name { font-size: 15px; font-weight: 600; color: var(--text-title); letter-spacing: -0.015em; white-space: nowrap; }

.nav-scroll { flex: 1; min-height: 0; }
.sider :deep(.el-scrollbar__view) { padding-bottom: 8px; }

.sider-foot {
  flex-shrink: 0;
  border-top: 1px solid var(--border-subtle);
  padding: 6px 8px;
}
.collapse-btn {
  width: 100%;
  height: 36px;
  justify-content: center;
  color: var(--text-secondary);
  font-size: 13px;
}
.collapse-btn:hover { background: var(--bg-muted); color: var(--text-title); }

/* 版本 chip（点击打开「关于/更新」） */
.ver-chip {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 9px 7px;
  cursor: pointer;
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  user-select: none;
}
.ver-chip:hover { background: var(--bg-muted); color: var(--text-title); }
.ver-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  flex-shrink: 0;
}
.ver-dot.has { background: #f59e0b; box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.16); }
.ver-text { font-variant-numeric: tabular-nums; }
.ver-new {
  margin-left: auto;
  color: #f59e0b;
  font-size: 11px;
  font-weight: 600;
}

/* ---- 主区 ---- */
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }

.topbar {
  height: var(--header-h);
  flex-shrink: 0;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  gap: 16px;
}
.topbar-left { min-width: 0; }
.crumb :deep(.el-breadcrumb__inner) { color: var(--text-secondary); font-weight: 500; font-size: 13px; }
.crumb :deep(.el-breadcrumb__item:last-child .el-breadcrumb__inner) { color: var(--text-title); }

.topbar-right { display: flex; align-items: center; gap: 12px; }

.global-search {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-radius: 9999px;
  border: 1px solid var(--border); background: var(--bg-subtle);
  color: var(--text-muted); font-size: 13px; cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.global-search:hover { border-color: var(--border-strong); background: #fff; }
.gs-text { white-space: nowrap; }
.gs-kbd {
  font-size: 11px; border: 1px solid var(--border); border-radius: 4px;
  padding: 0 5px; line-height: 17px; color: var(--text-muted); background: #fff;
}

.user-chip {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 4px 8px; border-radius: 9999px; outline: none;
}
.user-chip:hover { background: var(--bg-muted); }
.user-name { font-size: 13px; font-weight: 500; color: var(--text-title); white-space: nowrap; }
.user-caret { color: var(--text-muted); font-size: 12px; }

.content { flex: 1; min-height: 0; overflow: auto; padding: 24px; }

@media (max-width: 900px) {
  .global-search { display: none; }
  .user-name { display: none; }
}
</style>
