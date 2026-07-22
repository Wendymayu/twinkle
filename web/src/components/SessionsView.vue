<script setup lang="ts">
import SessionListPane from './SessionListPane.vue'
import FileTreePane from './FileTreePane.vue'
import FilePreviewPane from './FilePreviewPane.vue'
import { useSessions } from '../composables/useSessions'
const { sessions, loadSessions } = useSessions()
// ensure the list is fresh when entering the page
loadSessions()
</script>

<template>
  <div class="sessions-view">
    <SessionListPane />
    <FileTreePane />
    <FilePreviewPane />
  </div>
</template>

<style scoped>
.sessions-view {
  flex: 1; display: grid; grid-template-columns: 1fr 1fr 3fr; gap: .75rem;
  padding: .75rem; min-height: 0; min-width: 0;
}
@media (max-width: 900px) { .sessions-view { grid-template-columns: 1fr; grid-auto-rows: auto; } }
</style>
