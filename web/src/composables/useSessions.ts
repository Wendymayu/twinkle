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
export interface InstalledSkill { name: string; description: string }
export interface SkillNetSkillItem { name: string; description: string; skill_url: string }
export interface SkillHubSkillItem { name: string; description: string; slug: string; downloads: number; score: number }

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

type NavKey = 'chat' | 'sessions' | 'skills'
const activeNav = ref<NavKey>('chat')
const selectedSessionId = ref<string>('')
const sessionFiles = ref<{ name: string; is_dir: boolean; size: number }[]>([])
const previewFile = ref<string | null>(null)
const previewContent = ref<string>('')
const previewLoading = ref(false)
const historyAsBubbles = ref(true)
const searchResults = ref<(SkillNetSkillItem | SkillHubSkillItem)[]>([])
const installedSkills = ref<InstalledSkill[]>([])
const skillsLoading = ref(false)
const skillsError = ref<string | null>(null)

function setNav(key: NavKey) {
  activeNav.value = key
}

const completedCount = computed(() =>
  todo.value ? todo.value.tasks.filter((t) => t.status === 'completed').length : 0,
)

function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'in_progress') return '◐'
  if (status === 'cancelled') return '✗'
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

async function loadInstalled() {
  try {
    const payload = await client.request('skills.list_local', {})
    installedSkills.value = payload?.skills ?? []
  } catch {
    installedSkills.value = []
  }
}

function clearSearch() { searchResults.value = [] }

async function searchSkills(q: string, force = false, source: 'skillnet' | 'skillhub' = 'skillnet') {
  skillsLoading.value = true
  skillsError.value = null
  try {
    const payload = await client.request('skills.search', { q, force_refresh: force, source }, 60000)
    searchResults.value = payload?.skills ?? []
  } catch (e: any) {
    searchResults.value = []
    skillsError.value = e?.message || '搜索失败'
  } finally {
    skillsLoading.value = false
  }
}

async function installSkill(args: {
  source: 'skillnet' | 'skillhub'; slug?: string; url?: string
}): Promise<{ ok: boolean; skillName?: string; error?: string }> {
  // 后台任务 + 延迟结果。source=skillhub 走 zip 下载,skillnet 走 GitHub raw。失败帧 → request reject。
  try {
    const payload = await client.request('skills.install', { ...args, force: false }, 180000)
    if (payload?.ok) {
      await loadInstalled() // 刷新已安装列表
      return { ok: true, skillName: payload.skill_name }
    }
    return { ok: false, error: payload?.error || '安装失败' }
  } catch (e: any) {
    return { ok: false, error: e?.message || '安装失败' }
  }
}

async function uninstallSkill(name: string): Promise<{ ok: boolean; error?: string }> {
  // 本地瞬时操作(走后台任务通路)。rmtree 不可逆 → 前端 confirm 二次确认。
  try {
    const payload = await client.request('skills.uninstall', { name }, 30000)
    if (payload?.ok) {
      await loadInstalled()
      return { ok: true }
    }
    return { ok: false, error: payload?.error || '卸载失败' }
  } catch (e: any) {
    return { ok: false, error: e?.message || '卸载失败' }
  }
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
    // 连接就绪即拉已装 skill:SkillsView.onMounted 的 loadInstalled 可能在 ws.onopen 前
    // 触发(竞态),那次 send 抛 InvalidStateError → 静默置空且不重试 → 首次进入显示空。
    // onopen 补一枪:共享 installedSkills 更新后,已挂载的 SkillsView 响应式重渲染。
    loadInstalled()
    const saved = client.getSessionId()
    loadSessions()
      .then(() => (saved ? selectSession(saved).catch(() => createSession()) : createSession()))
      .then(() => checkAndRestorePendingApproval())
      .catch(() => { /* session bootstrap failed — user can retry via the + 新对话 button */ })
  })
}

/** After (re)connection, check for pending approvals and restore approval cards
 *  so the user can resume from a breakpoint after closing the browser. */
async function checkAndRestorePendingApproval() {
  try {
    const result = await client.checkPendingApprovals(client.getSessionId())
    const pending = result?.pending ?? []
    for (const p of pending) {
      // Avoid duplicate cards (e.g. network blip without full page reload)
      const exists = messages.value.some(m => m.kind === 'approval' && m.approvalId === p.approval_id)
      if (!exists) {
        messages.value.push({
          role: 'assistant',
          kind: 'approval',
          content: '',
          approvalId: p.approval_id,
          tool: p.tool,
          args: p.args,
          reason: p.reason,
          requestId: p.request_id,
          decided: null,
        })
        inputDisabled.value = true
      }
    }
  } catch {
    // Non-critical — if it fails, the user can still interact normally
  }
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
    searchResults, installedSkills, skillsLoading, skillsError,
    searchSkills, loadInstalled, clearSearch, installSkill, uninstallSkill,
    init, loadSessions, createSession, selectSession, deleteSession, sendQuery,
    loadSessionFiles, readSessionFile, restoreSession,
    webClient: client,
  }
}
