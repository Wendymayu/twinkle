import { ref, computed } from 'vue'
import { WebClient, type TodoTask } from '../services/webClient'

export interface SessionItem {
  session_id: string
  title: string
  last_message_at: number
  message_count: number
}
export interface ChatMsg {
  role: 'user' | 'assistant' | 'tool'
  content: string
}
interface TodoState { tasks: TodoTask[]; remaining: number; total: number }

const client = new WebClient()
const sessions = ref<SessionItem[]>([])
const currentSessionId = ref<string>('')
const messages = ref<ChatMsg[]>([])
const connected = ref(false)
const busy = ref(false)
const loading = ref(false)
const todo = ref<TodoState | null>(null)

type NavKey = 'chat' | 'sessions'
const activeNav = ref<NavKey>('chat')
const selectedSessionId = ref<string>('')
const sessionFiles = ref<{ name: string; is_dir: boolean; size: number }[]>([])
const previewFile = ref<string | null>(null)
const previewContent = ref<string>('')
const previewLoading = ref(false)
const historyAsBubbles = ref(true)

function setNav(key: NavKey) {
  activeNav.value = key
}

const completedCount = computed(() =>
  todo.value ? todo.value.tasks.filter((t) => t.status === 'completed').length : 0,
)

function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'running') return '◐'
  return '○'
}

function fromHistory(records: any[]): ChatMsg[] {
  // system messages are the todo-guidance prompt — skip in the UI.
  return records
    .filter((r) => r.role !== 'system')
    .map((r) => ({ role: r.role, content: r.content ?? '' }))
}

async function loadSessions() {
  const payload = await client.request('session.list', {})
  sessions.value = payload?.sessions ?? []
}

async function selectSession(id: string) {
  loading.value = true
  client.setSessionId(id)
  currentSessionId.value = id
  try {
    const payload = await client.request('history.get', { session_id: id })
    messages.value = fromHistory(payload?.messages ?? [])
  } finally {
    loading.value = false
  }
}

async function createSession() {
  const id = 'sess_' + crypto.randomUUID()
  client.setSessionId(id)
  currentSessionId.value = id
  messages.value = []
  await client.request('session.create', { session_id: id })
  await loadSessions()
}

async function deleteSession(id: string) {
  await client.request('session.delete', { session_id: id })
  if (id === currentSessionId.value) {
    await createSession()
  }
  await loadSessions()
}

async function loadSessionFiles(sid: string) {
  if (!sid) {
    sessionFiles.value = []
    previewFile.value = null
    previewContent.value = ''
    return
  }
  selectedSessionId.value = sid
  const payload = await client.request('session.files', { session_id: sid })
  sessionFiles.value = payload?.files ?? []
  // auto-select the first file
  const first = sessionFiles.value.find((f) => !f.is_dir)
  if (first) {
    await readSessionFile(sid, first.name)
  } else {
    previewFile.value = null
    previewContent.value = ''
  }
}

async function readSessionFile(sid: string, name: string) {
  if (!sid || !name) return
  previewLoading.value = true
  previewFile.value = name
  try {
    const payload = await client.request('file.read', { session_id: sid, name })
    previewContent.value = payload?.content ?? ''
  } catch {
    previewContent.value = ''
  } finally {
    previewLoading.value = false
  }
}

async function restoreSession(sid: string) {
  await selectSession(sid) // loads chat history + sets currentSessionId
  setNav('chat')
}

function sendQuery(q: string) {
  if (!q.trim() || !connected.value) return
  messages.value.push({ role: 'user', content: q })
  busy.value = true
  client.send('chat.send', { query: q })
}

function init() {
  client.connect(() => {
    connected.value = true
    client.setHandlers(
      (delta, rid) => {
        if (rid !== client.getLastRequestId()) return
        const last = messages.value[messages.value.length - 1]
        if (last && last.role === 'assistant') last.content += delta
        else messages.value.push({ role: 'assistant', content: delta })
      },
      (text, rid) => {
        if (rid !== client.getLastRequestId()) return
        const last = messages.value[messages.value.length - 1]
        if (!last || last.role !== 'assistant') messages.value.push({ role: 'assistant', content: text })
        else if (!last.content) last.content = text
        busy.value = false
        loadSessions() // refresh to pick up a fresh auto-title
      },
      (t) => { todo.value = t },
    )
    const saved = client.getSessionId()
    loadSessions()
      .then(() => (saved ? selectSession(saved).catch(() => createSession()) : createSession()))
      .catch(() => { /* session bootstrap failed — user can retry via the + 新对话 button */ })
  })
}

export function useSessions() {
  return {
    sessions, currentSessionId, messages, connected, busy, loading, todo,
    completedCount, box, fromHistory,
    activeNav, setNav,
    selectedSessionId, sessionFiles, previewFile, previewContent,
    previewLoading, historyAsBubbles,
    init, loadSessions, createSession, selectSession, deleteSession, sendQuery,
    loadSessionFiles, readSessionFile, restoreSession,
  }
}
