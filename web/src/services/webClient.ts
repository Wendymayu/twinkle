// 精简 WebSocket client：发送 {type:req,id,method,params}，按 request_id 关联
// 流式 chat.delta / chat.final 事件，上报 todo.update 事件，并通过 `request()`
// promise 解析 session/history RPC——该 promise 等待匹配的 `result` 事件。

export type DeltaHandler = (delta: string, requestId: string) => void
export type FinalHandler = (text: string, requestId: string) => void
export type TodoUpdateHandler = (
  todo: { tasks: TodoTask[]; remaining: number; total: number },
  requestId: string,
) => void
export type ApprovalDecision = 'allow' | 'allow_always' | 'deny'
export interface ApprovalAskPayload {
  approval_id: string
  tool: string
  args: any
  tool_call_id?: string
  reason?: string
}
export type ApprovalAskHandler = (payload: ApprovalAskPayload, requestId: string) => void

export interface TodoTask {
  id: string
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled'
  result: string
  blocked_by: string[]
  owner: string
  metadata: Record<string, unknown>
  created_at: number
  updated_at: number
}

const SESSION_KEY = 'twinkle.sessionId'

export class WebClient {
  private ws: WebSocket | null = null
  private onDelta: DeltaHandler = () => {}
  private onFinal: FinalHandler = () => {}
  private onTodoUpdate: TodoUpdateHandler = () => {}
  private onApprovalAsk: ApprovalAskHandler = () => {}
  private seq = 0
  private sessionId = ''
  private lastRequestId = ''
  private pending = new Map<string, (payload: any) => void>()

  connect(onReady: () => void): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    this.ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws.onopen = () => {
      // sticky session id：复用 localStorage 中的值，使页面刷新后
      // 重新挂接到同一 session（后端缓存或冷启动补水）。
      const saved = localStorage.getItem(SESSION_KEY)
      this.sessionId = saved && saved.startsWith('sess_') ? saved : 'sess_' + crypto.randomUUID()
      localStorage.setItem(SESSION_KEY, this.sessionId)
      onReady()
    }
    this.ws.onmessage = (ev) => {
      try {
        this.handle(JSON.parse(ev.data))
      } catch (e) {
        console.error('bad frame', e)
      }
    }
  }

  getSessionId(): string {
    return this.sessionId
  }

  setSessionId(id: string): void {
    this.sessionId = id
    localStorage.setItem(SESSION_KEY, id)
  }

  getLastRequestId(): string {
    return this.lastRequestId
  }

  private handle(frame: any): void {
    if (frame.type === 'event' && frame.event === 'connection.ack') return
    if (frame.type === 'res') return // 即时 ack——无需上报
    if (frame.type === 'event') {
      const rid = frame.request_id
      const content = frame.payload?.content ?? ''
      if (frame.event === 'chat.delta') this.onDelta(content, rid)
      else if (frame.event === 'chat.final') this.onFinal(content, rid)
      else if (frame.event === 'todo.update') this.onTodoUpdate(frame.payload ?? { tasks: [], remaining: 0, total: 0 }, rid)
      else if (frame.event === 'approval.ask') this.onApprovalAsk(frame.payload ?? {}, rid)
      else if (frame.event === 'result') {
        const resolve = this.pending.get(rid)
        if (resolve) {
          this.pending.delete(rid)
          resolve(frame.payload)
        }
      }
    }
  }

  setHandlers(
    onDelta: DeltaHandler,
    onFinal: FinalHandler,
    onTodoUpdate: TodoUpdateHandler,
    onApprovalAsk?: ApprovalAskHandler,
  ): void {
    this.onDelta = onDelta
    this.onFinal = onFinal
    this.onTodoUpdate = onTodoUpdate
    this.onApprovalAsk = onApprovalAsk ?? (() => {})
  }

  send(method: string, params: Record<string, any>): string {
    const id = 'req_' + Date.now().toString(36) + '_' + (this.seq++).toString(36)
    this.lastRequestId = id
    const fullParams = { ...params, session_id: this.sessionId }
    this.ws?.send(JSON.stringify({ type: 'req', id, method, params: fullParams }))
    return id
  }

  /** 发起 RPC（session.* / history.get）并以 `result` payload 解析。 */
  request(method: string, params: Record<string, any> = {}, timeoutMs: number = 15000): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = this.send(method, params)
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`timeout waiting for result: ${method}`))
      }, timeoutMs)
      this.pending.set(id, (payload: any) => {
        clearTimeout(timer)
        if (payload?.error) reject(new Error(payload.error))
        else resolve(payload)
      })
    })
  }

  /** 发送 approval 响应而不污染 lastRequestId。恢复后的
   * chat.delta / chat.final 帧携带原始 request_id R；若此方法把
   * lastRequestId 更新为自己的 id（R2），这些帧会被 delta/final
   * handler 中 rid !== getLastRequestId() 守卫丢弃。因此绕过 send()，自建 id，
   * 并注册以 R2 为键的 pending resolver——gateway 在 R2 上返回 e2a.result ack。 */
  respond(
    approvalId: string,
    decision: ApprovalDecision,
    originalRequestId: string,
  ): Promise<any> {
    const id = 'apr_' + Date.now().toString(36) + '_' + (this.seq++).toString(36)
    const params = {
      approval_id: approvalId,
      decision,
      original_request_id: originalRequestId,
      session_id: this.sessionId,
    }
    this.ws?.send(JSON.stringify({ type: 'req', id, method: 'approval.respond', params }))
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error('timeout waiting for result: approval.respond'))
      }, 15000)
      this.pending.set(id, (payload: any) => {
        clearTimeout(timer)
        if (payload?.error) reject(new Error(payload.error))
        else resolve(payload)
      })
    })
  }

  /** 检查当前 session 的待处理 approval（用于重连后）。 */
  async checkPendingApprovals(sessionId: string): Promise<any> {
    return this.request('approval.check_pending', { session_id: sessionId })
  }
}
