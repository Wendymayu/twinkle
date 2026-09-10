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
  // approval-card 字段——仅在 kind === 'approval' 时有意义
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
export interface EvolveRecord {
  id: string
  source: string
  score: number
  section: string
  summary: string
  used: number
  positive: number
}
export interface EvolvePendingItem {
  id: string
  source: string
  section: string
  summary: string
}
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
// approval.ask 等待用户决策期间为 true——禁用聊天输入
const inputDisabled = ref(false)

type NavKey = 'chat' | 'sessions' | 'skills' | 'evolution'
type AgentMode = 'normal' | 'team'
const activeNav = ref<NavKey>('chat')
const agentMode = ref<AgentMode>('normal')
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
const evolveRecords = ref<EvolveRecord[]>([])
const evolvePending = ref<Record<string, EvolvePendingItem[]>>({})
const evolveSelectedSkill = ref<string>('')
const evolveRecordsLoading = ref(false)
const evolvePendingLoading = ref(false)
const evolveActionLoading = ref(false)
const evolveError = ref<string | null>(null)

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
  // system 消息是 todo 指导 prompt——在 UI 中跳过。
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
  // 自动选中第一个文件
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
  await selectSession(sid) // 加载聊天历史 + 设置 currentSessionId
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

async function loadEvolveRecords(name: string) {
  if (!name) { evolveRecords.value = []; return }
  evolveRecordsLoading.value = true
  evolveError.value = null
  try {
    const payload = await client.request('skills.evolve_list', { name })
    evolveRecords.value = payload?.records ?? []
  } catch (e: any) {
    evolveRecords.value = []
    evolveError.value = e?.message || '加载经验记录失败'
  } finally {
    evolveRecordsLoading.value = false
  }
}

async function loadEvolvePending() {
  evolvePendingLoading.value = true
  evolveError.value = null
  try {
    const payload = await client.request('skills.evolve_pending', {})
    evolvePending.value = payload?.pending ?? {}
  } catch (e: any) {
    evolvePending.value = {}
    evolveError.value = e?.message || '加载待批失败'
  } finally {
    evolvePendingLoading.value = false
  }
}

/** approve/reject/simplify 成功后刷新 inbox(+选中 skill 的 records)。 */
async function refreshEvolve() {
  await Promise.all([loadEvolvePending(), loadEvolveRecords(evolveSelectedSkill.value)])
}

async function approveEvolve(name: string, ids: string[] | null) {
  evolveActionLoading.value = true
  evolveError.value = null
  try {
    await client.request('skills.evolve_approve', { name, record_ids: ids }, 60000)
    await refreshEvolve()
  } catch (e: any) {
    evolveError.value = e?.message || '批准失败'
  } finally {
    evolveActionLoading.value = false
  }
}

async function rejectEvolve(name: string, ids: string[] | null) {
  evolveActionLoading.value = true
  evolveError.value = null
  try {
    await client.request('skills.evolve_reject', { name, record_ids: ids }, 60000)
    await refreshEvolve()
  } catch (e: any) {
    evolveError.value = e?.message || '拒绝失败'
  } finally {
    evolveActionLoading.value = false
  }
}

async function simplifyEvolve(name: string) {
  if (!name) return
  evolveActionLoading.value = true
  evolveError.value = null
  try {
    await client.request('skills.evolve_simplify', { name }, 180000)
    await loadEvolveRecords(name)
  } catch (e: any) {
    evolveError.value = e?.message || '蒸馏失败'
  } finally {
    evolveActionLoading.value = false
  }
}

function selectEvolveSkill(name: string) {
  evolveSelectedSkill.value = name
  loadEvolveRecords(name)
}

function sendQuery(q: string) {
  if (!q.trim() || !connected.value) return
  messages.value.push({ role: 'user', content: q })
  busy.value = true
  client.send('chat.send', { query: q, mode: agentMode.value })
}

function init() {
  client.connect(() => {
    connected.value = true
    client.setHandlers(
      (delta, rid) => {
        if (rid !== client.getLastRequestId()) return
        const last = messages.value[messages.value.length - 1]
        // 不要把恢复的 delta 追加到 approval card 上——开一个新气泡
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
        inputDisabled.value = false // 防御性清理：以防仍有 approval 在等待
        loadSessions() // 刷新以获取新的自动标题
      },
      (t) => { todo.value = t },
      (payload, rid) => {
        // approval.ask：payload={approval_id,tool,args,tool_call_id,reason}，
        // rid 是原始 chat.send 的 request_id——存下来，使 card 在响应时
        // 能把它作为 original_request_id 传回。
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
        inputDisabled.value = true // approval 等待期间禁用输入
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
      .catch(() => { /* session bootstrap 失败——用户可通过 + 新对话 按钮重试 */ })
  })
}

/** （重）连接后，检查待处理 approval 并恢复 approval card，
 *  使用户关闭浏览器后可从断点继续。 */
async function checkAndRestorePendingApproval() {
  try {
    const result = await client.checkPendingApprovals(client.getSessionId())
    const pending = result?.pending ?? []
    for (const p of pending) {
      // 避免重复 card（如网络抖动但未整页刷新）
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
    // 非关键——失败时用户仍可正常交互
  }
}

/** 将 approval card 标记为已决策，使其动作按钮换成结果
 * 标签。原地修改 message——因 messages 是 deep ref 而保持响应式。 */
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
    activeNav, setNav, agentMode,
    selectedSessionId, sessionFiles, previewFile, previewContent,
    previewLoading, historyAsBubbles,
    searchResults, installedSkills, skillsLoading, skillsError,
    searchSkills, loadInstalled, clearSearch, installSkill, uninstallSkill,
    evolveRecords, evolvePending, evolveSelectedSkill,
    evolveRecordsLoading, evolvePendingLoading, evolveActionLoading, evolveError,
    loadEvolveRecords, loadEvolvePending, approveEvolve, rejectEvolve,
    simplifyEvolve, selectEvolveSkill,
    init, loadSessions, createSession, selectSession, deleteSession, sendQuery,
    loadSessionFiles, readSessionFile, restoreSession,
    webClient: client,
  }
}
