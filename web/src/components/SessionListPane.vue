<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { sessions, selectedSessionId, loadSessionFiles, deleteSession, restoreSession, connected } = useSessions()

function relTime(ts: number): string {
  if (!ts) return ''
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return '刚刚'
  if (s < 3600) return Math.floor(s / 60) + '分钟前'
  if (s < 86400) return Math.floor(s / 3600) + '小时前'
  return Math.floor(s / 86400) + '天前'
}
</script>

<template>
  <section class="pane list-pane">
    <div class="pane-head">
      <span>历史会话</span>
      <button class="restore-btn"
        :disabled="!selectedSessionId || !connected"
        @click="selectedSessionId && restoreSession(selectedSessionId)">↩ 恢复</button>
    </div>
    <ul class="sess-list">
      <li v-for="s in sessions" :key="s.session_id"
          :class="['sess-item', { active: s.session_id === selectedSessionId }]"
          @click="loadSessionFiles(s.session_id)">
        <div class="sess-main">
          <div class="sess-title">{{ s.title || '(无标题)' }}</div>
          <div class="sess-meta">{{ relTime(s.last_message_at) }} · {{ s.message_count }}条</div>
        </div>
        <span class="sess-del" @click.stop="deleteSession(s.session_id)">✕</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.list-pane { flex: 1 1 0; min-width: 0; }
.pane-head { display: flex; justify-content: space-between; align-items: center; padding: .7rem .85rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: .9rem; color: #1e293b; }
.restore-btn { border: 0; border-radius: 8px; background: #4f46e5; color: #fff; padding: .3rem .6rem; font-size: .78rem; cursor: pointer; }
.restore-btn:disabled { background: #cbd5e1; cursor: not-allowed; }
.sess-list { list-style: none; margin: 0; padding: .35rem; overflow-y: auto; flex: 1; }
.sess-item { display: flex; align-items: center; gap: .4rem; padding: .5rem; border-radius: 8px; cursor: pointer; }
.sess-item:hover { background: #f1f5f9; }
.sess-item.active { background: #eef2ff; }
.sess-main { flex: 1; min-width: 0; }
.sess-title { font-size: .85rem; color: #1e293b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sess-meta { font-size: .72rem; color: #94a3b8; margin-top: .1rem; }
.sess-del { color: #cbd5e1; flex-shrink: 0; }
.sess-del:hover { color: #ef4444; }
</style>
