import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ROUTE_PERMS } from './routeMeta'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/LoginPage.vue'), meta: { public: true } },
    { path: '/register', name: 'register', component: () => import('@/pages/RegisterPage.vue'), meta: { public: true } },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      redirect: '/dashboard',
      children: [
        { path: 'dashboard', name: 'dashboard', component: () => import('@/pages/DashboardPage.vue') },
        { path: 'guide', name: 'guide', component: () => import('@/pages/DocsPage.vue') },
        { path: 'chat', name: 'chat', component: () => import('@/pages/ChatPage.vue') },
        { path: 'kb', name: 'kb', component: () => import('@/pages/KBListPage.vue') },
        { path: 'kb/:id', name: 'kb-detail', component: () => import('@/pages/kb/KBDetailPage.vue') },
        { path: 'chat-room', name: 'chat-room', component: () => import('@/pages/ChatRoomPage.vue') },
        { path: 'service-tickets', name: 'service-tickets', component: () => import('@/pages/service/ServiceTicketPage.vue') },
        { path: 'records', name: 'records', component: () => import('@/pages/record/RecordPage.vue') },
        { path: 'agents', name: 'agents', component: () => import('@/pages/AgentListPage.vue') },
        { path: 'agents/:id/chat', name: 'agent-chat', component: () => import('@/pages/agents/AgentChatPage.vue') },
        { path: 'agents/:id/edit', name: 'agent-edit', component: () => import('@/pages/AgentEditPage.vue') },
        { path: 'agents/:id/workflow', name: 'agent-workflow', component: () => import('@/pages/agents/WorkflowEditPage.vue') },
        { path: 'skills', name: 'skills', component: () => import('@/pages/skills/SkillPage.vue') },
        { path: 'scheduled', name: 'scheduled', component: () => import('@/pages/scheduled/ScheduledTasksPage.vue') },
        { path: 'channels', name: 'channels', component: () => import('@/pages/ChannelListPage.vue') },
        { path: 'admin/channel-users', name: 'channel-users', component: () => import('@/pages/admin/ChannelUsersPage.vue') },
        { path: 'retrieval', name: 'retrieval', component: () => import('@/pages/kb/RetrievalDebugPage.vue') },
        { path: 'eval', name: 'eval', component: () => import('@/pages/kb/EvalPage.vue') },
        { path: 'todo', name: 'todo', component: () => import('@/pages/todo/TodoPage.vue') },
        { path: 'mail', name: 'mail', component: () => import('@/pages/mail/MailPage.vue') },
        { path: 'approvals', name: 'approvals', component: () => import('@/pages/approvals/ApprovalPage.vue') },
        { path: 'account', name: 'account', component: () => import('@/pages/account/AccountPage.vue') },
        { path: 'admin/users', name: 'admin-users', component: () => import('@/pages/admin/UserPage.vue') },
        { path: 'admin/roles', name: 'admin-roles', component: () => import('@/pages/admin/RolePage.vue') },
        { path: 'admin/depts', name: 'admin-depts', component: () => import('@/pages/admin/DeptPage.vue') },
        { path: 'admin/groups', name: 'admin-groups', component: () => import('@/pages/admin/GroupPage.vue') },
        { path: 'admin/settings', name: 'admin-settings', component: () => import('@/pages/admin/SettingsPage.vue') },
        { path: 'admin/browser', name: 'admin-browser', component: () => import('@/pages/admin/BrowserPage.vue') },
        { path: 'admin/system-ops', name: 'admin-system-ops', component: () => import('@/pages/admin/SystemOpsPage.vue') },
        { path: 'admin/models', name: 'admin-models', component: () => import('@/pages/admin/ModelPage.vue') },
        { path: 'admin/persona-templates', name: 'admin-persona-templates', component: () => import('@/pages/admin/PersonaTemplatePage.vue') },
        { path: 'admin/notify-channels', name: 'admin-notify', component: () => import('@/pages/admin/NotifyChannelPage.vue') },
        { path: 'admin/event-subscriptions', name: 'admin-events', component: () => import('@/pages/admin/EventSubscriptionPage.vue') },
        { path: 'admin/email-sources', name: 'admin-email', component: () => import('@/pages/admin/EmailSourcePage.vue') },
        { path: 'admin/audit', name: 'admin-audit', component: () => import('@/pages/admin/AuditPage.vue') },
        { path: 'admin/logs', name: 'admin-logs', component: () => import('@/pages/admin/LogPage.vue') },
        { path: 'admin/usage', name: 'admin-usage', component: () => import('@/pages/admin/UsagePage.vue') },
        { path: 'admin/security', name: 'admin-security', component: () => import('@/pages/admin/SecurityPage.vue') },
        { path: 'admin/files', name: 'admin-files', component: () => import('@/pages/admin/FilePage.vue') },
        { path: 'admin/tools', name: 'admin-tools', component: () => import('@/pages/admin/ToolPage.vue') },
        { path: 'admin/mcp', name: 'admin-mcp', component: () => import('@/pages/admin/McpPage.vue') },
        { path: 'admin/api-keys', name: 'admin-apikeys', component: () => import('@/pages/admin/ApiKeyPage.vue') },
        { path: 'admin/sso', name: 'admin-sso', component: () => import('@/pages/admin/SsoPage.vue') },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const token = localStorage.getItem('access_token')

  if (to.meta.public) {
    return token ? { path: '/dashboard' } : true
  }
  if (!token) return { path: '/login', query: { redirect: to.fullPath } }

  if (!auth.user) await auth.fetchMe()
  if (!auth.user) return { path: '/login' }

  const perm = ROUTE_PERMS[to.path]
  if (perm && !auth.hasPermission(perm)) return { path: '/dashboard' }
  return true
})

export default router
