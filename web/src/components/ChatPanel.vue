<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { useSessions } from '../composables/useSessions'

const { messages, connected, busy, loading, sendQuery, createSession } = useSessions()
const input = ref('')
const logEl = ref<HTMLUListElement | null>(null)

function scrollDown() {
  nextTick(() => { if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight })
}
function send() {
  const q = input.value.trim()
  if (!q || !connected.value) return
  input.value = ''
  sendQuery(q)
  scrollDown()
}
</script>

<template>
  <div class="chat">
    <ul ref="logEl" class="log">
      <li v-for="(m, i) in messages" :key="i" :class="['row', m.role]">
        <div v-if="m.role === 'tool'" class="tool-line">{{ m.content }}</div>
        <div v-else class="bubble">{{ m.content }}</div>
      </li>
      <li v-if="busy" class="row assistant"><div class="bubble processing">处理中…</div></li>
      <li v-if="loading" class="row assistant"><div class="bubble processing">加载历史…</div></li>
    </ul>
    <footer>
      <input v-model="input" @keyup.enter="send" :disabled="!connected" placeholder="说点什么…" />
      <button class="new-btn" @click="createSession" :disabled="!connected" title="新对话">➕ 新对话</button>
      <button @click="send" :disabled="!connected">发送</button>
    </footer>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; flex: 1; min-width: 0; background: #f8fafc;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #1e293b; }
.log { list-style: none; margin: 0; padding: 1rem; flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: .55rem; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble { max-width: 75%; padding: .55rem .85rem; border-radius: 16px; white-space: pre-wrap; word-break: break-word; line-height: 1.5; box-shadow: 0 1px 2px rgba(15,23,42,.06); }
.row.user .bubble { background: #4f46d5; color: #fff; border-bottom-right-radius: 4px; }
.row.assistant .bubble { background: #fff; color: #1e293b; border: 1px solid #e2e8f0; border-bottom-left-radius: 4px; }
.bubble.processing { color: #94a3b8; font-style: italic; animation: pulse 1.2s ease-in-out infinite; }
.tool-line { font-family: ui-monospace, monospace; font-size: .8rem; color: #94a3b8; padding: .2rem .5rem; }
@keyframes pulse { 0%,100% { opacity: .45; } 50% { opacity: 1; } }
footer { display: flex; gap: .5rem; padding: .8rem 1rem; border-top: 1px solid #e2e8f0; background: #fff; }
input { flex: 1; padding: .6rem .8rem; border: 1px solid #cbd5e1; border-radius: 12px; outline: none; font-size: .95rem; }
input:focus { border-color: #4f46d5; }
button { padding: .6rem 1.2rem; border: 0; border-radius: 12px; background: #4f46d5; color: #fff; font-size: .95rem; cursor: pointer; }
button:hover:not(:disabled) { background: #4338ca; }
button:disabled { background: #cbd5e1; cursor: not-allowed; }
.new-btn {
  padding: .6rem 1rem; border: 0; border-radius: 12px; background: #fff;
  border: 1px solid #cbd5e1; color: #4f46e5; font-size: 1rem; cursor: pointer;
}
.new-btn:hover:not(:disabled) { background: #f1f5f9; }
.new-btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
