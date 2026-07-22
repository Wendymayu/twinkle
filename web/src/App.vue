<script setup lang="ts">
import { ref, onMounted, nextTick, computed } from 'vue'
import { WebClient, type TodoTask } from './services/webClient'

interface Msg { role: 'user' | 'assistant'; content: string }

const msgs = ref<Msg[]>([])
const input = ref('')
const connected = ref(false)
const busy = ref(false)
const logEl = ref<HTMLUListElement | null>(null)

const client = new WebClient()
let currentId: string | null = null

interface TodoState { tasks: TodoTask[]; remaining: number; total: number }
const todo = ref<TodoState | null>(null)

const completedCount = computed(() =>
  todo.value ? todo.value.tasks.filter((t) => t.status === 'completed').length : 0,
)

function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'running') return '◐'
  return '○'
}

onMounted(() => {
  client.connect(() => { connected.value = true })
  client.setHandlers(
    (delta, rid) => {
      if (rid !== currentId) return
      const last = msgs.value[msgs.value.length - 1]
      if (last && last.role === 'assistant') last.content += delta
      else msgs.value.push({ role: 'assistant', content: delta })
      scrollDown()
    },
    (text, rid) => {
      if (rid !== currentId) return
      const last = msgs.value[msgs.value.length - 1]
      if (!last || last.role !== 'assistant') msgs.value.push({ role: 'assistant', content: text })
      else if (!last.content) last.content = text
      busy.value = false
      scrollDown()
    },
    (t) => { todo.value = t },
  )
})

function scrollDown() {
  nextTick(() => { if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight })
}

function send() {
  const q = input.value.trim()
  if (!q || !connected.value) return
  msgs.value.push({ role: 'user', content: q })
  input.value = ''
  currentId = client.send('chat.send', { query: q })
  busy.value = true
  scrollDown()
}
</script>

<template>
  <div class="app">
    <div class="chat">
      <header>
        <span class="title">✨ Twinkle</span>
        <span class="status" :class="{ on: connected }">{{ connected ? '已连接' : '连接中…' }}</span>
      </header>
      <ul ref="logEl" class="log">
        <li v-for="(m, i) in msgs" :key="i" :class="['row', m.role]">
          <div class="bubble">{{ m.content }}</div>
        </li>
        <li v-if="busy" class="row assistant">
          <div class="bubble processing">处理中…</div>
        </li>
      </ul>
      <footer>
        <input
          v-model="input"
          @keyup.enter="send"
          :disabled="!connected"
          placeholder="说点什么…"
        />
        <button @click="send" :disabled="!connected">发送</button>
      </footer>
    </div>
    <aside class="todo-panel">
      <div class="todo-head">
        <span>Todo</span>
        <span class="todo-count" v-if="todo">{{ completedCount }}/{{ todo.total }}</span>
      </div>
      <ul v-if="todo && todo.tasks.length" class="todo-list">
        <li v-for="t in todo.tasks" :key="t.idx" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-idx">{{ t.idx }}.</span>
          <span class="todo-title">{{ t.title }}</span>
          <span class="todo-result" v-if="t.result">{{ t.result }}</span>
        </li>
      </ul>
      <p v-else class="todo-empty">暂无任务</p>
    </aside>
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body { background: #f8fafc; }
.app {
  display: flex;
  height: 100%;
  max-width: 1040px;
  margin: 0 auto;
}
.chat {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: #1e293b;
  background: #f8fafc;
}
.todo-panel {
  width: 280px;
  flex: 0 0 280px;
  border-left: 1px solid #e2e8f0;
  background: #fff;
  display: flex;
  flex-direction: column;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (max-width: 640px) {
  .app { flex-direction: column; max-width: 100%; }
  .todo-panel { width: 100%; flex: 0 0 auto; border-left: 0; border-top: 1px solid #e2e8f0; max-height: 40%; }
}
.todo-head {
  display: flex;
  justify-content: space-between;
  padding: .9rem 1rem;
  border-bottom: 1px solid #e2e8f0;
  font-weight: 600;
}
.todo-count { color: #6366f1; }
.todo-list { list-style: none; margin: 0; padding: .5rem; overflow-y: auto; flex: 1; }
.todo-item { display: flex; align-items: baseline; gap: .35rem; padding: .35rem .25rem; font-size: .9rem; }
.todo-item.completed .todo-title { text-decoration: line-through; color: #94a3b8; }
.todo-box { width: 1.1em; text-align: center; color: #4f46e5; }
.todo-item.completed .todo-box { color: #10b981; }
.todo-result { color: #64748b; font-size: .8rem; }
.todo-empty { padding: 1rem; color: #94a3b8; font-size: .85rem; }
header {
  display: flex;
  align-items: baseline;
  gap: .6rem;
  padding: .9rem 1rem;
  border-bottom: 1px solid #e2e8f0;
  background: #fff;
}
.title { font-weight: 700; font-size: 1.05rem; }
.status { margin-left: auto; font-size: .8rem; color: #ef4444; }
.status.on { color: #10b981; }

.log {
  list-style: none;
  margin: 0;
  padding: 1rem;
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: .55rem;
}
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble {
  max-width: 75%;
  padding: .55rem .85rem;
  border-radius: 16px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
  box-shadow: 0 1px 2px rgba(15, 23, 42, .06);
}
.row.user .bubble {
  background: #4f46e5;
  color: #fff;
  border-bottom-right-radius: 4px;
}
.row.assistant .bubble {
  background: #fff;
  color: #1e293b;
  border: 1px solid #e2e8f0;
  border-bottom-left-radius: 4px;
}
.bubble.processing {
  color: #94a3b8;
  font-style: italic;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: .45; }
  50% { opacity: 1; }
}

footer {
  display: flex;
  gap: .5rem;
  padding: .8rem 1rem;
  border-top: 1px solid #e2e8f0;
  background: #fff;
}
input {
  flex: 1;
  padding: .6rem .8rem;
  border: 1px solid #cbd5e1;
  border-radius: 12px;
  outline: none;
  font-size: .95rem;
}
input:focus { border-color: #4f46e5; }
button {
  padding: .6rem 1.2rem;
  border: 0;
  border-radius: 12px;
  background: #4f46e5;
  color: #fff;
  font-size: .95rem;
  cursor: pointer;
}
button:hover:not(:disabled) { background: #4338ca; }
button:disabled { background: #cbd5e1; cursor: not-allowed; }
</style>
