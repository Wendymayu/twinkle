<script setup lang="ts">
import { onMounted } from 'vue'
import { useSessions } from './composables/useSessions'
import ChatView from './components/ChatView.vue'
import SessionsView from './components/SessionsView.vue'

const { init, activeNav, setNav, connected } = useSessions()

onMounted(() => { init() })
</script>

<template>
  <div class="app">
    <header class="top-bar">
      <div class="brand">✨ Twinkle</div>
      <span class="status" :class="{ ok: connected }">{{ connected ? '已连接' : '未连接' }}</span>
    </header>
    <div class="body">
      <nav class="sidebar">
        <button :class="{ active: activeNav === 'chat' }" @click="setNav('chat')">💬 聊天</button>
        <button :class="{ active: activeNav === 'sessions' }" @click="setNav('sessions')">🗂 会话</button>
      </nav>
      <main class="content">
        <ChatView v-if="activeNav === 'chat'" />
        <SessionsView v-else />
      </main>
    </div>
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body { background: #f8fafc; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.app { display: flex; flex-direction: column; height: 100%; }
.top-bar {
  display: flex; align-items: center; gap: .75rem;
  padding: .65rem 1rem; background: #fff;
  border-bottom: 1px solid #e2e8f0;
}
.brand { font-size: 1.05rem; font-weight: 700; color: #1e293b; flex: 0 0 auto; }
.status { font-size: .7rem; color: #94a3b8; flex: 1; text-align: center; }
.status.ok { color: #22c55e; }
.body { flex: 1; display: flex; min-height: 0; }
.sidebar {
  width: 120px; flex: 0 0 120px; border-right: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column; padding: .6rem .5rem; gap: .35rem;
}
.sidebar button {
  border: 0; background: transparent; border-radius: 8px; padding: .6rem .4rem;
  cursor: pointer; font-size: .88rem; color: #475569; display: flex;
  flex-direction: column; align-items: center; gap: .2rem;
}
.sidebar button:hover { background: #f1f5f9; }
.sidebar button.active { background: #eef2ff; color: #4f46d5; font-weight: 600; }
.content { flex: 1; min-width: 0; display: flex; }
</style>
