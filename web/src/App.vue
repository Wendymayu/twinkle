<script setup lang="ts">
import { onMounted } from 'vue'
import { useSessions } from './composables/useSessions'
import LeftNav from './components/LeftNav.vue'
import ChatView from './components/ChatView.vue'
import SessionsView from './components/SessionsView.vue'

const { init, activeNav } = useSessions()

onMounted(() => { init() })
</script>

<template>
  <div class="app">
    <LeftNav />
    <main class="content">
      <ChatView v-if="activeNav === 'chat'" />
      <SessionsView v-else />
    </main>
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body { background: #f8fafc; }
.app { display: flex; height: 100%; }
.content { flex: 1; min-width: 0; display: flex; }
</style>
