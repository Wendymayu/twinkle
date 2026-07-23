<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { activeNav, setNav } = useSessions()
</script>

<template>
  <nav class="left-nav">
    <div class="brand">✨ Twinkle</div>
    <button :class="{ active: activeNav === 'chat' }" @click="setNav('chat')">💬 聊天</button>
    <button :class="{ active: activeNav === 'sessions' }" @click="setNav('sessions')">🗂 会话</button>
  </nav>
</template>

<style scoped>
.left-nav {
  width: 72px; flex: 0 0 72px; border-right: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column; align-items: center;
  padding: .75rem .35rem .5rem; gap: .5rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
.brand {
  font-size: .72rem; font-weight: 700; color: #1e293b;
  margin-bottom: .5rem; text-align: center; line-height: 1.2;
}
button {
  border: 0; background: transparent; border-radius: 8px; padding: .5rem .25rem;
  cursor: pointer; font-size: .72rem; color: #475569; display: flex;
  flex-direction: column; align-items: center; gap: .15rem; width: 100%;
}
button:hover { background: #f1f5f9; }
button.active { background: #eef2ff; color: #4f46d5; font-weight: 600; }
</style>
