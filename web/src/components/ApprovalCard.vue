<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
import type { ApprovalDecision } from '../services/webClient'

const props = defineProps<{
  approvalId: string
  tool: string
  args: any
  reason?: string
  requestId: string
  decided: ApprovalDecision | null
}>()

const { webClient, inputDisabled, markApprovalDecided } = useSessions()

async function decide(d: ApprovalDecision) {
  // respond() does NOT touch lastRequestId — the resumed chat.delta/final
  // arrive on the original request_id and stay associated with it.
  try {
    await webClient.respond(props.approvalId, d, props.requestId)
  } catch (e) {
    // approval already expired/cancelled (e.g. session switched) — still flip
    // the card to decided so the user can't keep clicking.
    console.error('approval.respond failed', e)
  }
  markApprovalDecided(props.approvalId, d)
  inputDisabled.value = false
}
</script>

<template>
  <div class="approval-card">
    <div class="approval-head">需要审批：工具 <code>{{ tool }}</code></div>
    <pre class="approval-args">{{ JSON.stringify(args, null, 2) }}</pre>
    <div class="approval-reason" v-if="reason">{{ reason }}</div>
    <div class="approval-actions" v-if="!decided">
      <button @click="decide('allow')">放行一次</button>
      <button @click="decide('allow_always')">永久放行</button>
      <button class="deny" @click="decide('deny')">拒绝</button>
    </div>
    <div class="approval-result" v-else>已{{ decided === 'deny' ? '拒绝' : '放行' }}</div>
  </div>
</template>

<style scoped>
.approval-card {
  max-width: 75%;
  padding: .6rem .85rem;
  border: 1px solid #f59e0b;
  background: #fffbeb;
  border-radius: 16px;
  border-bottom-left-radius: 4px;
  font-size: .9rem;
  box-shadow: 0 1px 2px rgba(15, 23, 42, .06);
}
.approval-head { font-weight: 600; color: #92400e; }
.approval-head code {
  font-family: ui-monospace, monospace;
  background: #fef3c7;
  padding: .1rem .35rem;
  border-radius: 6px;
}
.approval-args {
  margin: .5rem 0;
  padding: .5rem;
  background: #fff;
  border: 1px solid #fde68a;
  border-radius: 8px;
  font-family: ui-monospace, monospace;
  font-size: .78rem;
  color: #475569;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 12rem;
  overflow: auto;
}
.approval-reason { color: #64748b; font-size: .82rem; margin-bottom: .4rem; }
.approval-actions { display: flex; gap: .5rem; flex-wrap: wrap; }
.approval-actions button {
  padding: .4rem .9rem;
  border: 0;
  border-radius: 10px;
  background: #4f46d5;
  color: #fff;
  font-size: .85rem;
  cursor: pointer;
}
.approval-actions button:hover { background: #4338ca; }
.approval-actions button.deny { background: #dc2626; }
.approval-actions button.deny:hover { background: #b91c1c; }
.approval-result { color: #16a34a; font-weight: 600; }
</style>
