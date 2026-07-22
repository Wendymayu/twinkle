<script setup lang="ts">
import ChatPanel from './ChatPanel.vue'
import TodoPanel from './TodoPanel.vue'
</script>

<template>
  <div class="chat-view">
    <ChatPanel />
    <TodoPanel />
  </div>
</template>

<style scoped>
.chat-view { display: flex; flex: 1; min-width: 0; }
@media (max-width: 640px) { .chat-view { flex-direction: column; } }
</style>
