<template>
  <div v-if="guide" class="big">
    <div class="big-head">
      <el-icon class="big-ico"><WarningFilled /></el-icon>
      <span class="big-title">{{ guide.title }}</span>
    </div>
    <p class="big-summary">{{ guide.summary }}</p>
    <div v-for="o in guide.options" :key="o.kind" class="big-opt">
      <div class="big-opt-head">
        <span class="big-opt-title">{{ o.title }}</span>
        <el-tag v-if="o.recommended" size="small" type="primary" effect="plain">推荐</el-tag>
        <el-tag v-if="o.requires_permission && !can(o.requires_permission)" size="small" type="warning" effect="plain">
          需管理员权限
        </el-tag>
      </div>
      <p class="big-opt-detail">{{ o.detail }}</p>
      <div class="big-opt-ops">
        <el-button v-if="o.route && canOpen(o)" size="small" type="primary" @click="go(o)">
          {{ o.kind === 'bundled' ? '去安装内置运行时' : '打开浏览器设置' }}
        </el-button>
        <a
          v-for="l in o.links || []"
          :key="l.url"
          class="big-link"
          :href="l.url"
          target="_blank"
          rel="noopener noreferrer"
        >{{ l.name }} ↗</a>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { WarningFilled } from '@element-plus/icons-vue'
import type { BrowserInstallGuide, BrowserInstallOption } from '@/api'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ guide?: BrowserInstallGuide | null }>()
const router = useRouter()
const auth = useAuthStore()

function can(perm?: string) {
  return auth.hasPermission(perm)
}
/** 有权限才给跳转按钮：没权限的用户点了也只会被路由守卫弹回首页 */
function canOpen(o: BrowserInstallOption) {
  return !o.requires_permission || can(o.requires_permission)
}
function go(o: BrowserInstallOption) {
  if (!o.route) return
  router.push({ path: o.route, query: o.query })
}
</script>

<style scoped>
.big {
  border: 1px solid var(--warn-border); background: var(--warn-soft);
  border-radius: 10px; padding: 12px 14px;
}
.big-head { display: flex; align-items: center; gap: 6px; }
.big-ico { color: var(--warn); font-size: 15px; }
.big-title { font-size: 13.5px; font-weight: 600; color: var(--text-title); }
.big-summary { margin: 6px 0 10px; font-size: 12.5px; line-height: 1.7; color: var(--text-body); }

.big-opt {
  border-top: 1px dashed var(--warn-border); padding-top: 10px; margin-top: 10px;
}
.big-opt:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
.big-opt-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.big-opt-title { font-size: 13px; font-weight: 500; color: var(--text-title); }
.big-opt-detail { margin: 4px 0 8px; font-size: 12.5px; line-height: 1.7; color: var(--text-secondary); }
.big-opt-ops { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.big-link { font-size: 12.5px; color: var(--color-primary); text-decoration: none; }
.big-link:hover { text-decoration: underline; }
</style>
