<template>
  <div v-if="reasoning" class="reasoning">
    <!-- 折叠态：一行摘要。用户关心的是「它在想什么」，不是全程推理文本 -->
    <button type="button" class="rs-head" @click="open = !open">
      <el-icon class="rs-ic" :class="{ spinning: streaming }"><Sunny /></el-icon>
      <span class="rs-title">{{ summaryText }}</span>
      <el-icon class="rs-caret" :class="{ open }"><ArrowDown /></el-icon>
    </button>

    <Transition name="rs-expand">
      <div v-show="open" class="rs-body">
        <div class="rs-scroll">{{ reasoning }}</div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  reasoning?: string
  streaming?: boolean
  /** 流式结束后的耗时（毫秒），有值则摘要显示「思考了 N 秒」 */
  durationMs?: number
}>()

const open = ref(false)
let ended = false

/**
 * 摘要文案：
 * - 流式中：显示「正在思考…」，正文默认展开让人看到进展
 * - 结束后：优先用耗时（最接近主流做法），没有耗时则用字符数
 */
const summaryText = computed(() => {
  if (props.streaming) return '正在思考…'
  if (props.durationMs && props.durationMs > 0) {
    const s = Math.max(1, Math.round(props.durationMs / 1000))
    return `思考了 ${s} 秒`
  }
  const n = (props.reasoning || '').length
  return n > 0 ? `思考过程（${n} 字）` : '思考过程'
})

// 流式中默认展开（让人看到进展），结束后自动收起成一行摘要
watch(
  () => props.streaming,
  (s) => {
    if (s) {
      ended = false
      open.value = true
    } else if (!ended) {
      open.value = false
      ended = true
    }
  },
)
</script>

<style scoped>
.reasoning { margin-bottom: 8px; }

.rs-head {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px 4px 8px; border: 1px solid var(--border-subtle);
  border-radius: 999px; background: var(--bg-subtle);
  cursor: pointer; font: inherit; font-size: 12.5px; color: var(--text-secondary);
  transition: background 0.15s, border-color 0.15s;
}
.rs-head:hover { background: var(--bg-surface); border-color: var(--border); }

.rs-ic { color: #d97706; font-size: 13px; }
.rs-ic.spinning { animation: rs-spin 2.4s linear infinite; }
@keyframes rs-spin { to { transform: rotate(360deg); } }

.rs-title { line-height: 1.2; }

.rs-caret {
  font-size: 10px; opacity: 0.7;
  transition: transform 0.2s;
}
.rs-caret.open { transform: rotate(180deg); }

.rs-body {
  margin-top: 6px;
  border-left: 2px solid var(--border-subtle);
  padding-left: 10px;
}
.rs-scroll {
  font-size: 12.5px; line-height: 1.7; color: var(--text-secondary);
  white-space: pre-wrap; max-height: 260px; overflow-y: auto;
}

.rs-expand-enter-active, .rs-expand-leave-active { transition: opacity 0.15s; }
.rs-expand-enter-from, .rs-expand-leave-to { opacity: 0; }
</style>
