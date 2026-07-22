<script setup lang="ts">
import { onMounted } from 'vue'
import { useSessions } from './composables/useSessions'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatPanel from './components/ChatPanel.vue'
import TodoPanel from './components/TodoPanel.vue'

const { init } = useSessions()

onMounted(() => { init() })
</script>

<template>
  <div class="app">
    <SessionSidebar />
    <ChatPanel />
    <TodoPanel />
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body { background: #f8fafc; }
.app { display: flex; height: 100%; max-width: 1280px; margin: 0 auto; }
@media (max-width: 640px) { .app { flex-direction: column; max-width: 100%; } }
</style>
