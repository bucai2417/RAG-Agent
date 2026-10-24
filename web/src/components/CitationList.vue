<template>
  <div v-if="items.length" class="cites">
    <div class="cites-label">
      {{ label }}
      <span v-if="hasUnused" class="cites-hint">（未在回答中标注编号的资料仅供参考）</span>
    </div>
    <div class="cite-tags">
      <el-tag
        v-for="c in shown"
        :key="c.key"
        :type="c.used ? (c.source_uri ? 'warning' : 'primary') : 'info'"
        :effect="c.used ? 'light' : 'plain'"
        class="cite-tag"
        :class="{ 'cite-unused': !c.used }"
        @click="c.source_uri ? windowOpen(c.source_uri) : $emit('open', c.raw)"
      >
        [{{ c.index }}] {{ c.doc_title || '文档' }}{{ c.page ? ` · 第${c.page}页` : '' }}{{ c.source_uri ? ' ↗' : '' }}
      </el-tag>
    </div>
    <!--
      未标注的资料默认折叠：检索到 ≠ 用到了。展开列表把它们平铺在回答下面时，
      用户看到「吕不韦」的回答下挂着「后续开发方向」「t · 第1页」，
      只会认为系统在乱标引用。默认收起，需要排查召回质量时再点开。
    -->
    <button v-if="unused.length && !expanded" type="button" class="cite-more" @click="expanded = true">
      另有 {{ unused.length }} 条检索结果未在回答中使用，展开
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * 引用来源展示。
 *
 * 为什么要区分「已引用」与「仅检索到」：
 * 检索命中了资料，不代表答案用了它——模型完全可能只是扫了一眼就自己作答。
 * 早先一律显示「引用来源」，用户会去正文里找 [1][2] 却找不到，
 * 反而怀疑系统在乱标引用（这也是"明明没触发知识库却显示引用了"的观感来源之一）。
 *
 * 现在：答案里真的标注过的编号用实色；未标注的**默认收起**，只留一行
 * 「另有 N 条检索结果未在回答中使用」。
 * 为什么不再平铺：检索噪声是常态（向量召回没有依据判据，弱命中也会进 top_k），
 * 把无关文档挂在回答下面，用户不会去分辨"已引用"和"仅检索到"，
 * 只会觉得系统乱标引用。列表仍然保留——召回不准是要排查的问题，
 * 展开就能看到——但不再占据回答的视觉空间。
 */
import { computed, ref } from 'vue'
import type { Citation } from '@/api'

const props = defineProps<{
  citations: Citation[]
  /** 答案里真正标注过的编号（1 基）。为空/未传 = 全部视为「仅检索到」 */
  usedIds?: number[]
  /** 无已引用条目标题文案 */
  title?: string
}>()

defineEmits<{ (e: 'open', c: Citation): void }>()

const used = computed(() => new Set(props.usedIds || []))
/** 未标注的资料是否已展开（默认收起，见上方说明） */
const expanded = ref(false)

const items = computed(() =>
  props.citations.map((c, i) => ({
    key: `${c.chunk_id || 0}-${i}`,
    index: i + 1,
    raw: c,
    doc_title: c.doc_title,
    page: c.page,
    source_uri: c.source_uri,
    used: used.value.size > 0 && used.value.has(i + 1),
  })),
)

const unused = computed(() => items.value.filter((c) => !c.used))
/** 平铺显示的条目：已引用的始终显示，未引用的仅在展开后显示 */
const shown = computed(() => items.value.filter((c) => c.used || expanded.value))

const hasUsed = computed(() => items.value.some((c) => c.used))
/** 有「已引用」时按引用文案；全都是未标注时换个说法，避免误导 */
const label = computed(() => {
  if (hasUsed.value) return props.title || '引用来源（点击查看）：'
  return '检索到的相关资料（回答未标注引用）：'
})
const hasUnused = computed(() => hasUsed.value && unused.value.length > 0)

function windowOpen(u: string) {
  window.open(u, '_blank')
}
</script>

<style scoped>
.cites { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border-light, #ebeef5); }
.cites-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.cites-hint { color: var(--text-placeholder, #a8abb2); }
.cite-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.cite-tag { cursor: pointer; }
/* 未在回答中标注的：降低视觉权重，避免被当成「已引用」 */
.cite-unused { opacity: 0.75; }
.cite-more {
  margin-top: 6px; padding: 0; border: 0; background: none; cursor: pointer;
  font-size: 12px; color: var(--text-placeholder, #a8abb2);
}
.cite-more:hover { color: var(--text-secondary); text-decoration: underline; }
</style>
