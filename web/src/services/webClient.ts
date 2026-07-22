// Minimal WebSocket client: sends {type:req,id,method,params}, correlates
// streamed chat.delta / chat.final events by request_id, surfaces
// todo.update events, and resolves session/history RPCs via a `request()`
// promise that awaits the matching `result` event.

export type DeltaHandler = (delta: string, requestId: string) => void
export type FinalHandler = (text: string, requestId: string) => void
export type TodoUpdateHandler = (
  todo: { tasks: TodoTask[]; remaining: number; total: number },
  requestId: string,
) => void

export interface TodoTask {
  idx: number
  title: string
  status: 'waiting' | 'running' | 'completed'
  result: string
}

const SESSION_KEY = 'twinkle.sessionId'

export class WebClient {
  private ws: WebSocket | null = null
  private onDelta: DeltaHandler = () => {}
  private onFinal: FinalHandler = () => {}
  private onTodoUpdate: TodoUpdateHandler = () => {}
  private seq = 0
  private sessionId = ''
  private lastRequestId = ''
  private pending = new Map<string, (payload: any) => void>()

  connect(onReady: () => void): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    this.ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws.onopen = () => {
      // sticky session id: reuse the one from localStorage so a page reload
      // reattaches to the same session (backend cache or cold-start hydration).
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
    if (frame.type === 'res') return // immediate ack — nothing to surface
    if (frame.type === 'event') {
      const rid = frame.request_id
      const content = frame.payload?.content ?? ''
      if (frame.event === 'chat.delta') this.onDelta(content, rid)
      else if (frame.event === 'chat.final') this.onFinal(content, rid)
      else if (frame.event === 'todo.update') this.onTodoUpdate(frame.payload ?? { tasks: [], remaining: 0, total: 0 }, rid)
      else if (frame.event === 'result') {
        const resolve = this.pending.get(rid)
        if (resolve) {
          this.pending.delete(rid)
          resolve(frame.payload)
        }
      }
    }
  }

  setHandlers(onDelta: DeltaHandler, onFinal: FinalHandler, onTodoUpdate: TodoUpdateHandler): void {
    this.onDelta = onDelta
    this.onFinal = onFinal
    this.onTodoUpdate = onTodoUpdate
  }

  send(method: string, params: Record<string, any>): string {
    const id = 'req_' + Date.now().toString(36) + '_' + (this.seq++).toString(36)
    this.lastRequestId = id
    const fullParams = { ...params, session_id: this.sessionId }
    this.ws?.send(JSON.stringify({ type: 'req', id, method, params: fullParams }))
    return id
  }

  /** Fire an RPC (session.* / history.get) and resolve with the `result` payload. */
  request(method: string, params: Record<string, any> = {}): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = this.send(method, params)
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`timeout waiting for result: ${method}`))
      }, 15000)
      this.pending.set(id, (payload: any) => {
        clearTimeout(timer)
        if (payload?.error) reject(new Error(payload.error))
        else resolve(payload)
      })
    })
  }
}
