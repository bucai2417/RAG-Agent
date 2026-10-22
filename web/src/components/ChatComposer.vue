<template>
  <div class="cc-wrap">
    <div class="cc-card" :class="{ focused, streaming }">
      <textarea
        ref="taRef"
        class="cc-input"
        :value="modelValue"
        :placeholder="placeholder"
        :rows="1"
        :style="{ maxHeight: `${maxHeight}px` }"
        spellcheck="false"
        @input="onInput"
        @keydown="onKeydown"
        @focus="focused = true"
        @blur="focused = false"
        @compositionstart="composing = true"
        @compositionend="composing = false"
      />
      <div class="cc-toolbar">
        <div class="cc-left">
          <slot name="left" />
        </div>
        <div class="cc-right">
          <!-- 上下文占用环：放在工具栏最右（紧邻发送键），与其它 Agent 产品的位置一致 -->
          <slot name="context" />
          <slot name="right" />
          <el-tooltip v-if="streaming" content="停止生成" placement="top">
            <button type="button" class="cc-send is-stop" @click="emit('stop')">
              <el-icon><VideoPause /></el-icon>
            </button>
          </el-tooltip>
          <el-tooltip v-else :content="canSend ? '发送（Enter）' : '先输入内容'" placement="top">
            <button
              type="button"
              class="cc-send"
              :disabled="!canSend"
              @click="emit('send')"
            >
              <el-icon><Top /></el-icon>
            </button>
          </el-tooltip>
        </div>
      </div>
    </div>
    <div class="cc-hint">
      <template v-if="streaming">正在生成，可点右侧按钮停止</template>
      <template v-else>Enter 发送 · Shift + Enter 换行 · 可直接拖入文件</template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { Top, VideoPause } from '@element-plus/icons-vue'

/**
 * 通用对话输入框：圆角卡片 + 内嵌底部工具栏。
 *
 * 为什么用原生 textarea 而不是 el-input：
 * 卡片内需要「无边框、透明背景、自适应高度、与工具栏同一容器」的输入区，
 * 靠 :deep 覆写 el-textarea 的边框/padding 反而更零碎，且 autosize 与
 * max-height 滚动条难以精确控制。
 *
 * 回车策略：Enter 发送、Shift+Enter 换行；输入法组词中（isComposing）的
 * Enter 只用于选词，绝不能触发发送。
 *
 * 三个插槽：left（附件/知识库等工具）、context（上下文占用环）、right（参数等）。
 * context 单列是因为它要固定在发送键左侧，不受 right 槽位内容增减影响。
 */
const props = withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
    streaming?: boolean
    /** 无正文但有附件时也允许发送，由父组件决定 */
    canSend?: boolean
    maxHeight?: number
  }>(),
  {
    placeholder: '输入问题…',
    streaming: false,
    canSend: true,
    maxHeight: 200,
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  (e: 'send'): void
  (e: 'stop'): void
}>()

const taRef = ref<HTMLTextAreaElement>()
const focused = ref(false)
const composing = ref(false)

const canSend = computed(() => props.canSend && !props.streaming)

function onInput(e: Event) {
  emit('update:modelValue', (e.target as HTMLTextAreaElement).value)
  resize()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  if (e.shiftKey) return          // 换行
  if (e.isComposing || composing.value) return  // 输入法选词
  e.preventDefault()
  if (canSend.value) emit('send')
}

/** 随内容长高，超过 maxHeight 后内部滚动 */
function resize() {
  const el = taRef.value
  if (!el) return
  el.style.height = 'auto'
  const h = Math.min(el.scrollHeight, props.maxHeight)
  el.style.height = `${h}px`
  el.style.overflowY = el.scrollHeight > props.maxHeight ? 'auto' : 'hidden'
}

watch(() => props.modelValue, (v) => {
  // 被父组件清空（发送后）时立即收起，不必等下次输入
  if (!v) nextTick(() => { if (taRef.value) taRef.value.style.height = 'auto' })
  nextTick(resize)
})
watch(() => props.maxHeight, () => nextTick(resize))
onMounted(resize)

function focus() {
  taRef.value?.focus()
}
defineExpose({ focus })
</script>

<style scoped>
.cc-wrap { display: flex; flex-direction: column; gap: 6px; width: 100%; }

.cc-card {
  display: flex; flex-direction: column; gap: 6px;
  padding: 10px 10px 8px 14px;
  border: 1px solid var(--border-strong);
  border-radius: 22px;
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.cc-card:hover { border-color: #b6c3d6; }
.cc-card.focused {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
}
.cc-card.streaming { border-color: var(--color-primary-border); }

.cc-input {
  width: 100%; border: none; outline: none; resize: none;
  background: transparent; color: var(--text-body);
  font: inherit; font-size: 14.5px; line-height: 1.65;
  padding: 4px 4px 0 0; margin: 0;
  overflow-y: hidden;
}
.cc-input::placeholder { color: var(--text-muted); }

.cc-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; min-height: 32px;
}
.cc-left, .cc-right { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cc-left { flex-wrap: wrap; }

/* 圆形发送键：位于框内右下，视觉上收在卡片里 */
.cc-send {
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; flex-shrink: 0;
  border: none; border-radius: 50%; cursor: pointer;
  background: var(--color-primary); color: #fff;
  font-size: 15px;
  transition: background 0.15s ease, transform 0.12s ease, opacity 0.15s ease;
}
.cc-send:hover:not(:disabled) { background: var(--color-primary-hover); }
.cc-send:active:not(:disabled) { transform: scale(0.94); }
.cc-send:disabled {
  background: var(--bg-muted); color: var(--text-muted); cursor: not-allowed;
}
.cc-send.is-stop { background: var(--error); }
.cc-send.is-stop:hover { background: #dc2626; }

.cc-hint {
  text-align: right; padding-right: 6px;
  font-size: 11.5px; color: var(--text-muted); user-select: none;
}
</style>
