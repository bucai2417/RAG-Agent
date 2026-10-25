<template>
  <!--
    上下文占用环：出现在输入框工具栏右侧，与其它 Agent 产品一致。
    - 圆环按 used/limit 填充，接近阈值变黄、达阈值变红。
    - hover 浮层给出实际数字（窗口 / 阈值 / 已用 / 本轮实发 / 摘要 / 折叠条数），
      而不是含糊的百分比；窗口与阈值同一口径，换模型会自动变。
    - 无会话（草稿）时只显示空心环，提示"发第一条后开始统计"。
  -->
  <el-tooltip placement="top" :show-after="120" effect="light" popper-class="ctx-tip" :disabled="!showTooltip">
    <template #content>
      <div class="ctx-pop">
        <div class="ctx-head">
          <span class="ctx-title">上下文占用</span>
          <span class="ctx-pct" :class="toneClass">{{ stats ? `${stats.percent}%` : '—' }}</span>
        </div>
        <div class="ctx-bar">
          <div class="ctx-bar-fill" :class="toneClass" :style="{ width: `${barWidth}%` }" />
        </div>
        <div class="ctx-rows">
          <div class="ctx-row">
            <span>已占用（压力）</span>
            <b>{{ fmt(used) }} / {{ fmt(limit) }} tokens</b>
          </div>
          <div class="ctx-row">
            <span>剩余可用</span>
            <b>{{ fmt(remain) }} tokens</b>
          </div>
          <div v-if="loaded < used" class="ctx-row is-hint">
            <span>本轮实发</span>
            <b>{{ fmt(loaded) }} tokens · {{ stats?.loaded_messages ?? 0 }} 条</b>
          </div>
        </div>
        <div class="ctx-divider" />
        <div class="ctx-rows">
          <div class="ctx-row">
            <span>压缩阈值</span>
            <b>{{ fmt(limit) }} tokens</b>
          </div>
          <div v-if="stats?.window_tokens" class="ctx-row">
            <span>模型窗口</span>
            <b>{{ fmt(stats.window_tokens) }} tokens</b>
          </div>
          <div v-if="stats?.model_name" class="ctx-row">
            <span>模型</span>
            <b class="ctx-model">{{ stats.model_name }}</b>
          </div>
          <div v-if="stats?.output_reserve_tokens" class="ctx-row">
            <span>输出预留</span>
            <b>{{ fmt(stats.output_reserve_tokens) }} tokens</b>
          </div>
        </div>
        <div class="ctx-divider" />
        <div class="ctx-rows">
          <div v-if="stats?.available" class="ctx-row">
            <span>消息条数</span>
            <b>{{ stats.message_count }} 条</b>
          </div>
          <div v-if="stats?.archived_messages" class="ctx-row">
            <span>已折叠进摘要</span>
            <b>{{ stats.archived_messages }} 条</b>
          </div>
          <div v-if="stats?.compressed" class="ctx-row">
            <span>摘要占用</span>
            <b>{{ fmt(stats.summary_tokens) }} tokens</b>
          </div>
          <div class="ctx-row">
            <span>压缩后保留原文</span>
            <!-- keep_recent 是下限、预算才是上限：两个数字都给，避免读成"固定保留 6 条" -->
            <b>
              <template v-if="stats?.keep_target_tokens">
                预算内自动取 · 目标 {{ fmt(stats.keep_target_tokens) }} tokens
              </template>
              <template v-else>预算内自动取 · 至少 {{ stats?.keep_recent ?? 6 }} 条</template>
            </b>
          </div>
        </div>
        <div class="ctx-state" :class="toneClass">{{ stateText }}</div>
      </div>
    </template>
    <button type="button" class="ctx-ring" :class="[toneClass, { idle: !hasConv }]" @click="$emit('refresh')">
      <svg class="ctx-svg" viewBox="0 0 32 32" aria-hidden="true">
        <circle class="ctx-track" cx="16" cy="16" r="13" />
        <circle
          class="ctx-fill"
          cx="16" cy="16" r="13"
          :stroke-dasharray="`${dash} ${circumference}`"
          :transform="`rotate(-90 16 16)`"
        />
      </svg>
      <span v-if="compressing" class="ctx-dot" />
    </button>
  </el-tooltip>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ContextStats } from '@/api'

const props = defineProps<{
  stats: ContextStats | null
  /** 当前是否挂着某个会话（草稿态无统计意义） */
  hasConv?: boolean
  /** 压缩任务在后台跑（环上打脉冲点） */
  compressing?: boolean
  loading?: boolean
}>()

defineEmits<{ (e: 'refresh'): void }>()

const R = 13
const circumference = 2 * Math.PI * R

const used = computed(() => props.stats?.used_tokens ?? 0)
/** 本轮真正会发出去的量；后端未提供时退回 used */
const loaded = computed(() => props.stats?.loaded_tokens ?? used.value)
const limit = computed(() => props.stats?.limit_tokens || 0)
const remain = computed(() => Math.max(0, limit.value - used.value))
const compressing = computed(() => !!props.compressing || !!props.stats?.compressing)

/** 百分比：四舍五入后夹在 0..100，草稿/无数据时为 0 */
const pct = computed(() => {
  if (!props.hasConv || !limit.value) return 0
  return Math.max(0, Math.min(100, props.stats?.percent ?? (used.value / limit.value) * 100))
})
const barWidth = computed(() => Math.max(pct.value, props.hasConv ? 2 : 0))
const dash = computed(() => (pct.value / 100) * circumference)

/** 分档：<70% 常规蓝；70-90% 提醒黄；>=90% 危险红 */
const toneClass = computed(() => {
  if (!props.hasConv) return 'is-idle'
  if (pct.value >= 90) return 'is-danger'
  if (pct.value >= 70) return 'is-warn'
  return 'is-normal'
})

const showTooltip = computed(() => props.hasConv)

const stateText = computed(() => {
  if (!props.hasConv) return '发送第一条消息后开始统计上下文占用'
  if (compressing.value) return '正在压缩早期对话…'
  if (!props.stats?.available) return '暂无统计'
  const clipped = loaded.value < used.value
  const window = props.stats.window_tokens
  if (pct.value >= 100) {
    return clipped
      ? '已达压缩阈值：本轮已按预算裁剪历史，新一轮将把早期对话折叠进摘要'
      : '已达压缩阈值，新一轮将自动压缩早期对话'
  }
  if (props.stats.compressed) {
    const base = window
      ? `已压缩历史；窗口 ${fmt(window)}，阈值 ${fmt(limit.value)}，上下文稳定在阈值以内`
      : '已压缩历史；上下文稳定在阈值以内'
    return clipped ? `${base}（本轮按预算取了最近 ${props.stats.loaded_messages ?? 0} 条）` : base
  }
  if (pct.value >= 90) return '接近压缩阈值，很快会自动压缩历史'
  if (clipped) return `上下文正常；本轮按预算取了最近 ${props.stats.loaded_messages ?? 0} 条原文`
  return '上下文正常'
})

function fmt(n: number): string {
  if (!n) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`
  return String(Math.round(n))
}
</script>

<style scoped>
.ctx-ring {
  position: relative;
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; flex-shrink: 0;
  padding: 0; border: none; border-radius: 50%;
  background: transparent; cursor: pointer;
  transition: background 0.15s ease;
}
.ctx-ring:hover { background: var(--bg-muted); }
.ctx-ring.idle { cursor: default; }
.ctx-ring.idle:hover { background: transparent; }

.ctx-svg { width: 22px; height: 22px; display: block; }
.ctx-track {
  fill: none; stroke: var(--border); stroke-width: 3.2;
}
.ctx-fill {
  fill: none; stroke: var(--color-primary);
  stroke-width: 3.2; stroke-linecap: round;
  transition: stroke-dasharray 0.45s ease, stroke 0.25s ease;
}
.ctx-ring.is-warn .ctx-fill { stroke: var(--warn); }
.ctx-ring.is-danger .ctx-fill { stroke: var(--error); }
.ctx-ring.is-idle .ctx-fill { stroke: transparent; }

/* 压缩中：右下角脉冲点，暗示有后台任务 */
.ctx-dot {
  position: absolute; right: 2px; bottom: 2px;
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--warn);
  animation: ctx-pulse 1.1s ease-in-out infinite;
}
@keyframes ctx-pulse {
  0%, 100% { opacity: 0.35; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.15); }
}
@media (prefers-reduced-motion: reduce) {
  .ctx-dot { animation: none; opacity: 0.9; }
  .ctx-fill { transition: none; }
}
</style>

<style>
/* 浮层不设 scoped：el-tooltip 的内容挂到 body 上。
   显式声明浅色底：effect=light 只改文字色，底色仍走 --el-bg-color-overlay，
   若全局变量被改成深色主题就会透出深底黑字。这里把底色/边框/文字全部钉死。 */
.ctx-tip.el-popper {
  padding: 0 !important;
  border-radius: 10px !important;
  --el-bg-color-overlay: #ffffff;
  --el-text-color-primary: #0f172a;
  background: #ffffff !important;
  border: 1px solid var(--border) !important;
  color: var(--text-body) !important;
  box-shadow: var(--shadow-floating) !important;
}
.ctx-tip.el-popper .el-popper__arrow::before {
  background: #ffffff !important;
  border-color: var(--border) !important;
}
.ctx-tip .ctx-pop { padding: 10px 12px; min-width: 252px; }
.ctx-tip .ctx-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  margin-bottom: 7px;
}
.ctx-tip .ctx-title { font-size: 12.5px; font-weight: 600; color: var(--text-title); }
.ctx-tip .ctx-pct { font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.ctx-tip .ctx-pct.is-normal { color: var(--color-primary); }
.ctx-tip .ctx-pct.is-warn { color: var(--warn); }
.ctx-tip .ctx-pct.is-danger { color: var(--error); }
.ctx-tip .ctx-pct.is-idle { color: var(--text-muted); }

.ctx-tip .ctx-bar {
  height: 4px; border-radius: 9999px; background: var(--bg-muted);
  overflow: hidden; margin-bottom: 9px;
}
.ctx-tip .ctx-bar-fill {
  height: 100%; border-radius: 9999px; background: var(--color-primary);
  transition: width 0.35s ease;
}
.ctx-tip .ctx-bar-fill.is-warn { background: var(--warn); }
.ctx-tip .ctx-bar-fill.is-danger { background: var(--error); }
.ctx-tip .ctx-bar-fill.is-idle { background: var(--border); }

.ctx-tip .ctx-rows { display: flex; flex-direction: column; gap: 3px; }
.ctx-tip .ctx-divider {
  height: 1px; background: var(--border-subtle); margin: 7px 0;
}
.ctx-tip .ctx-row {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  font-size: 11.5px; color: var(--text-secondary);
}
.ctx-tip .ctx-row b {
  color: var(--text-title); font-weight: 500; font-variant-numeric: tabular-nums;
}
/* 「本轮实发」只在预算裁剪时出现，是补充信息：压低一档，别和主数字抢注意力 */
.ctx-tip .ctx-row.is-hint b { color: var(--text-secondary); font-weight: 400; }
.ctx-tip .ctx-row .ctx-model {
  max-width: 148px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ctx-tip .ctx-state {
  margin-top: 8px; padding-top: 7px; border-top: 1px solid var(--border-subtle);
  font-size: 11px; line-height: 1.55; color: var(--text-secondary);
}
.ctx-tip .ctx-state.is-warn { color: #b45309; }
.ctx-tip .ctx-state.is-danger { color: #b91c1c; }
.ctx-tip .ctx-state.is-idle { color: var(--text-muted); }
</style>
