<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { activeNav, setNav } = useSessions()
</script>

<template>
  <nav class="left-nav">
    <button :class="{ active: activeNav === 'chat' }" @click="setNav('chat')">💬 聊天</button>
    <button :class="{ active: activeNav === 'sessions' }" @click="setNav('sessions')">🗂 会话</button>
  </nav>
</template>

<style scoped>
.left-nav {
  width: 72px; flex: 0 0 72px; border-right: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column; gap: .25rem; padding: .5rem .35rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
button {
  border: 0; background: transparent; border-radius: 8px; padding: .5rem .25rem;
  cursor: pointer; font-size: .72rem; color: #475569; display: flex;
  flex-direction: column; align-items: center; gap: .15rem;
}
button:hover { background: #f1f5f9; }
button.active { background: #eef2ff; color: #4f46d5; font-weight: 600; }
</style>
