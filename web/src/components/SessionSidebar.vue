<script setup lang="ts">
import { useSessions } from '../composables/useSessions'

const { sessions, currentSessionId, createSession, selectSession, deleteSession } = useSessions()

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
  <aside class="sidebar">
    <button class="new-btn" @click="createSession">+ 新对话</button>
    <ul class="sess-list">
      <li
        v-for="s in sessions"
        :key="s.session_id"
        :class="['sess-item', { active: s.session_id === currentSessionId }]"
        @click="selectSession(s.session_id)"
      >
        <span class="sess-title">{{ s.title || '(无标题)' }}</span>
        <span class="sess-time">{{ relTime(s.last_message_at) }}</span>
        <span class="sess-del" @click.stop="deleteSession(s.session_id)">✕</span>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.sidebar { width: 240px; flex: 0 0 240px; border-right: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column; padding: .6rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
@media (max-width: 640px) { .sidebar { width: 100%; flex: 0 0 auto; max-height: 30%; } }
.new-btn { padding: .55rem; border: 0; border-radius: 10px; background: #4f46d5; color: #fff; cursor: pointer; font-size: .9rem; margin-bottom: .5rem; }
.new-btn:hover { background: #4338ca; }
.sess-list { list-style: none; margin: 0; padding: 0; overflow-y: auto; flex: 1; }
.sess-item { display: flex; align-items: center; gap: .4rem; padding: .5rem .5rem; border-radius: 8px; cursor: pointer; font-size: .85rem; }
.sess-item:hover { background: #f1f5f9; }
.sess-item.active { background: #eef2ff; }
.sess-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #1e293b; }
.sess-time { color: #94a3b8; font-size: .72rem; flex-shrink: 0; }
.sess-del { color: #cbd5e1; flex-shrink: 0; }
.sess-del:hover { color: #ef4444; }
</style>
