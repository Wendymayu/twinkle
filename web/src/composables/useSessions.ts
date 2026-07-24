import { ref, computed } from 'vue'
import { WebClient, type TodoTask, type ApprovalDecision } from '../services/webClient'

export interface SessionItem {
  session_id: string
  title: string
  last_message_at: number
  message_count: number
}
export interface ChatMsg {
  role: 'user' | 'assistant' | 'tool'
  content: string
  // approval-card fields — only meaningful when kind === 'approval'
  kind?: 'approval'
  approvalId?: string
  tool?: string
  args?: any
  reason?: string
  requestId?: string
  decided?: ApprovalDecision | null
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
// true while an approval.ask is awaiting a user decision — disables the chat input
const inputDisabled = ref(false)

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
        // don't append resumed deltas onto an approval card — start a fresh bubble
        if (last && last.role === 'assistant' && last.kind !== 'approval') last.content += delta
        else messages.value.push({ role: 'assistant', content: delta })
      },
      (text, rid) => {
        if (rid !== client.getLastRequestId()) return
        const last = messages.value[messages.value.length - 1]
        if (!last || last.role !== 'assistant' || last.kind === 'approval')
          messages.value.push({ role: 'assistant', content: text })
        else if (!last.content) last.content = text
        busy.value = false
        inputDisabled.value = false // defensive: clear in case an approval was still pending
        loadSessions() // refresh to pick up a fresh auto-title
      },
      (t) => { todo.value = t },
      (payload, rid) => {
        // approval.ask: payload={approval_id,tool,args,tool_call_id,reason},
        // rid is the ORIGINAL chat.send request_id — store it so the card can
        // pass it back as original_request_id when responding.
        messages.value.push({
          role: 'assistant',
          kind: 'approval',
          content: '',
          approvalId: payload.approval_id,
          tool: payload.tool,
          args: payload.args,
          reason: payload.reason,
          requestId: rid,
          decided: null,
        })
        inputDisabled.value = true // disable input while an approval is pending
      },
    )
    const saved = client.getSessionId()
    loadSessions()
      .then(() => (saved ? selectSession(saved).catch(() => createSession()) : createSession()))
      .catch(() => { /* session bootstrap failed — user can retry via the + 新对话 button */ })
  })
}

/** Mark an approval card as decided so its action buttons swap for a result
 * label. Mutates the message in-place — reactive because messages is a deep ref. */
function markApprovalDecided(approvalId: string, decision: ApprovalDecision) {
  for (const m of messages.value) {
    if (m.kind === 'approval' && m.approvalId === approvalId) {
      m.decided = decision
      break
    }
  }
}

export function useSessions() {
  return {
    sessions, currentSessionId, messages, connected, busy, loading, todo,
    inputDisabled, markApprovalDecided,
    completedCount, box, fromHistory,
    activeNav, setNav,
    selectedSessionId, sessionFiles, previewFile, previewContent,
    previewLoading, historyAsBubbles,
    init, loadSessions, createSession, selectSession, deleteSession, sendQuery,
    loadSessionFiles, readSessionFile, restoreSession,
    webClient: client,
  }
}
