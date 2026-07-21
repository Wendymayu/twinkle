// Minimal WebSocket client: sends {type:req,id,method,params}, correlates
// streamed chat.delta / chat.final events by request_id, and surfaces
// todo.update events (structured todo snapshots) for the side panel.

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

export class WebClient {
  private ws: WebSocket | null = null
  private onDelta: DeltaHandler = () => {}
  private onFinal: FinalHandler = () => {}
  private onTodoUpdate: TodoUpdateHandler = () => {}
  private seq = 0
  private sessionId = ''

  connect(onReady: () => void): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    this.ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws.onopen = () => {
      // mint a fresh conversation id per connection (browser-driven session,
      // matches roadmap Phase 1: gateway stays a dumb relay)
      this.sessionId = 'sess_' + crypto.randomUUID()
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

  private handle(frame: any): void {
    if (frame.type === 'event' && frame.event === 'connection.ack') return
    if (frame.type === 'res') {
      // immediate ack — nothing to surface in Phase 0
      return
    }
    if (frame.type === 'event') {
      const rid = frame.request_id
      const content = frame.payload?.content ?? ''
      if (frame.event === 'chat.delta') this.onDelta(content, rid)
      else if (frame.event === 'chat.final') this.onFinal(content, rid)
      else if (frame.event === 'todo.update') this.onTodoUpdate(frame.payload ?? { tasks: [], remaining: 0, total: 0 }, rid)
    }
  }

  setHandlers(onDelta: DeltaHandler, onFinal: FinalHandler, onTodoUpdate: TodoUpdateHandler): void {
    this.onDelta = onDelta
    this.onFinal = onFinal
    this.onTodoUpdate = onTodoUpdate
  }

  send(method: string, params: Record<string, any>): string {
    const id = 'req_' + Date.now().toString(36) + '_' + (this.seq++).toString(36)
    const fullParams = { ...params, session_id: this.sessionId }
    this.ws?.send(JSON.stringify({ type: 'req', id, method, params: fullParams }))
    return id
  }
}
