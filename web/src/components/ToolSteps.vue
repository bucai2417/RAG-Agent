<template>
  <div v-if="groups.length" class="tsteps">
    <div v-for="(g, gi) in groups" :key="gi" class="ts-group">
      <!-- 折叠态：一行「图标 + 动作 + 次数」 -->
      <button type="button" class="ts-head" @click="toggle(gi)">
        <el-icon class="ts-ic"><component :is="g.icon" /></el-icon>
        <span class="ts-label">{{ g.label }}</span>
        <span v-if="g.items.length > 1" class="ts-count">× {{ g.items.length }}</span>
        <el-tag v-if="g.failed" size="small" type="danger" effect="plain">失败 {{ g.failed }}</el-tag>
        <el-tag v-else-if="g.running" size="small" type="info" effect="plain">执行中</el-tag>
        <el-icon class="ts-caret" :class="{ open: isOpen(gi) }"><ArrowDown /></el-icon>
      </button>

      <!-- 展开态：每条的参数与结果 -->
      <div v-show="isOpen(gi)" class="ts-body">
        <div v-for="(s, si) in g.items" :key="si" class="ts-item">
          <div class="ts-item-head">
            <code class="ts-tool">{{ s.call.name }}</code>
            <el-tag v-if="!s.result" size="small" type="info">执行中</el-tag>
            <el-tag v-else-if="s.result.is_error" size="small" type="danger">失败</el-tag>
            <el-tag v-else size="small" type="success">完成</el-tag>
            <span v-if="s.summary" class="ts-summary">{{ s.summary }}</span>
          </div>
          <details class="ts-detail">
            <summary>参数与结果</summary>
            <div class="ts-label">参数</div>
            <pre class="ts-pre">{{ s.call.arguments }}</pre>
            <template v-if="s.result">
              <div class="ts-label">结果</div>
              <pre class="ts-pre ts-result">{{ truncate(s.result.content) }}</pre>
              <!-- 本机没装浏览器：纯文本指引点不动，这里给可跳转的安装卡片 -->
              <BrowserMissingGuide
                v-if="s.result.data?.browser_missing"
                :guide="s.result.data.install_guide"
                class="ts-guide"
              />
            </template>
          </details>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue'
import {
  Search, Document, Tools, Connection, DataAnalysis, Picture, Edit,
  Download, Upload, ChatDotRound, MagicStick,
} from '@element-plus/icons-vue'
import BrowserMissingGuide from '@/components/BrowserMissingGuide.vue'

interface Step {
  call: { id: string; name: string; arguments: string }
  result?: { id: string; name: string; content: string; is_error: boolean; data?: any }
  /** 可选：一行的动作摘要，如「搜索：订单查询」 */
  summary?: string
}

const props = defineProps<{ steps: Step[] }>()

/**
 * 工具名 → 展示类别。
 * 按关键词归类，未命中归到「工具调用」。
 * 同类连续操作会合并成一行「搜索 ×3」，而不是三条独立卡片。
 */
const CATEGORY_RULES: { test: (n: string) => boolean; label: string; icon: any }[] = [
  { test: (n) => /search|find|lookup|recall|retriev/i.test(n), label: '搜索', icon: Search },
  { test: (n) => /http|api|request|fetch|webhook|curl/i.test(n), label: '网络请求', icon: Connection },
  { test: (n) => /browser|navigate|crawl|scrape|cdp|page/i.test(n), label: '浏览网页', icon: Picture },
  { test: (n) => /file|read_doc|write_doc|docx|xlsx|pptx|office/i.test(n), label: '文件操作', icon: Document },
  { test: (n) => /table|excel|csv|analyz|aggregate/i.test(n), label: '数据处理', icon: DataAnalysis },
  { test: (n) => /edit|patch|modify/i.test(n), label: '编辑', icon: Edit },
  { test: (n) => /export|download/i.test(n), label: '导出', icon: Download },
  { test: (n) => /upload|import|ingest/i.test(n), label: '导入', icon: Upload },
  { test: (n) => /chat|message|notify|send|push/i.test(n), label: '消息', icon: ChatDotRound },
  { test: (n) => /agent|skill|workflow/i.test(n), label: '智能体', icon: MagicStick },
]

function categorize(name: string) {
  for (const r of CATEGORY_RULES) {
    if (r.test(name)) return { label: r.label, icon: r.icon }
  }
  return { label: '工具调用', icon: Tools }
}

/** 连续同类合并；不连续的同类型各自成组，保持时间顺序 */
const groups = computed(() => {
  const out: {
    label: string; icon: any; items: Step[]; failed: number; running: boolean
  }[] = []
  let lastLabel: string | null = null

  for (const s of props.steps) {
    const c = categorize(s.call?.name || '')
    if (lastLabel === c.label && out.length) {
      out[out.length - 1].items.push(s)
    } else {
      out.push({ label: c.label, icon: c.icon, items: [s], failed: 0, running: false })
      lastLabel = c.label
    }
  }
  for (const g of out) {
    g.failed = g.items.filter((x) => x.result?.is_error).length
    g.running = g.items.some((x) => !x.result)
  }
  return out
})

// 用户手动切换过的组；未切换时按「失败/执行中默认展开，其余折叠」
const overrides = reactive<Record<number, boolean>>({})

function isOpen(i: number): boolean {
  if (i in overrides) return overrides[i]
  const g = groups.value[i]
  if (!g) return false
  return g.failed > 0 || g.running
}

function toggle(i: number) {
  overrides[i] = !isOpen(i)
}

function truncate(s: string, n = 4000) {
  const t = s || ''
  return t.length > n ? `${t.slice(0, n)}\n…（已截断，共 ${t.length} 字）` : t
}
</script>

<style scoped>
.tsteps { margin-bottom: 8px; display: flex; flex-direction: column; gap: 4px; }

.ts-group { display: flex; flex-direction: column; }

.ts-head {
  display: inline-flex; align-items: center; gap: 7px; align-self: flex-start;
  padding: 4px 10px 4px 8px; border: 1px solid var(--border-subtle);
  border-radius: 999px; background: var(--bg-subtle);
  cursor: pointer; font: inherit; font-size: 12.5px; color: var(--text-secondary);
  transition: background 0.15s, border-color 0.15s;
}
.ts-head:hover { background: var(--bg-surface); border-color: var(--border); }

.ts-ic { font-size: 13px; color: var(--color-primary); }
.ts-label { line-height: 1.2; }
.ts-count {
  font-size: 11px; color: var(--text-muted);
  background: var(--bg-surface); border-radius: 8px; padding: 0 5px;
}
.ts-caret { font-size: 10px; opacity: 0.7; transition: transform 0.2s; }
.ts-caret.open { transform: rotate(180deg); }

.ts-body {
  margin: 6px 0 4px 6px; padding-left: 10px;
  border-left: 2px solid var(--border-subtle);
  display: flex; flex-direction: column; gap: 8px;
}

.ts-item { font-size: 12px; }
.ts-item-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ts-tool { font-family: monospace; font-size: 12px; color: var(--text-title); }
.ts-summary { color: var(--text-secondary); }

.ts-detail { margin-top: 4px; }
.ts-detail summary {
  cursor: pointer; font-size: 11.5px; color: var(--text-muted); user-select: none;
}
.ts-label { color: var(--text-secondary); margin: 6px 0 2px; font-size: 11.5px; }
.ts-pre {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  background: var(--bg-subtle); padding: 8px 10px; border-radius: 6px;
  font-family: 'SFMono-Regular', Consolas, monospace; font-size: 11.5px;
  max-height: 220px; overflow: auto;
}
.ts-result { color: var(--text-body); }
.ts-guide { margin-top: 8px; }
</style>
