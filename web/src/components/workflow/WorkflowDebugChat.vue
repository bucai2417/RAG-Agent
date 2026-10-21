<template>
  <div class="dbg">
    <!-- 会话记录 -->
    <div ref="listRef" class="dbg-list">
      <div v-if="!turns.length" class="dbg-empty">
        <p>在下方输入消息，像跟客服对话一样调试这个工作流。</p>
        <p class="muted">消息会作为 start 的第一个输入传入，历史会自动累积到 history 变量。</p>
      </div>
      <div v-for="(t, i) in turns" :key="i" class="dbg-turn" :class="t.role">
        <div class="dbg-bubble">
          <MarkdownBody v-if="t.role === 'assistant'" :content="t.content" />
          <span v-else>{{ t.content }}</span>
        </div>
        <div v-if="t.meta" class="dbg-meta">{{ t.meta }}</div>
      </div>
    </div>

    <!-- 输入区 -->
    <div class="dbg-input">
      <el-input
        v-model="draft"
        type="textarea"
        :rows="2"
        resize="none"
        placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
        :disabled="busy"
        @keydown.enter.exact.prevent="send"
      />
      <div class="dbg-actions">
        <el-button size="small" :icon="RefreshLeft" :disabled="busy || !turns.length" @click="clear">清空</el-button>
        <el-button size="small" type="primary" :icon="Position" :loading="busy" @click="send">发送</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Position, RefreshLeft } from '@element-plus/icons-vue'
import MarkdownBody from '@/components/MarkdownBody.vue'
import { errMsg } from '@/api/http'

interface Turn { role: 'user' | 'assistant'; content: string; meta?: string }

const props = defineProps<{
  /** 执行一次对话：返回助手回复 + 可选元信息 */
  send: (text: string) => Promise<{ text: string; meta?: string }>
  busy: boolean
  /** 外部重置（如切换运行/清空） */
  resetKey?: number
}>()

const turns = ref<Turn[]>([])
const draft = ref('')
const listRef = ref<HTMLElement>()

async function scrollToBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function send() {
  const text = draft.value.trim()
  if (!text || props.busy) return
  turns.value.push({ role: 'user', content: text })
  draft.value = ''
  await scrollToBottom()
  try {
    const r = await props.send(text)
    turns.value.push({ role: 'assistant', content: r.text || '（无回复）', meta: r.meta })
  } catch (e) {
    turns.value.push({ role: 'assistant', content: `出错：${errMsg(e)}` })
    ElMessage.error(errMsg(e))
  }
  await scrollToBottom()
}

function clear() {
  turns.value = []
}

watch(() => props.resetKey, () => clear())
</script>

<style scoped>
.dbg { display: flex; flex-direction: column; height: 100%; min-height: 320px; }
.dbg-list { flex: 1; overflow: auto; padding: 4px 2px; min-height: 0; }
.dbg-empty { color: var(--text-secondary); font-size: 13px; line-height: 1.8; padding: 24px 8px; text-align: center; }
.dbg-empty .muted { color: var(--text-muted); font-size: 12px; }
.dbg-turn { display: flex; flex-direction: column; margin-bottom: 12px; }
.dbg-turn.user { align-items: flex-end; }
.dbg-turn.assistant { align-items: flex-start; }
.dbg-bubble {
  max-width: 86%; padding: 8px 12px; border-radius: 10px;
  font-size: 13.5px; line-height: 1.65; word-break: break-word;
}
.dbg-turn.user .dbg-bubble { background: var(--color-primary); color: #fff; }
.dbg-turn.assistant .dbg-bubble { background: var(--bg-subtle); border: 1px solid var(--border-subtle); }
.dbg-meta { font-size: 11px; color: var(--text-muted); margin-top: 4px; }
.dbg-input { border-top: 1px solid var(--border-subtle); padding-top: 10px; margin-top: 6px; }
.dbg-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
