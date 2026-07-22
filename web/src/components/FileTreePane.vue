<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { sessionFiles, previewFile, readSessionFile, selectedSessionId, previewLoading } = useSessions()
</script>

<template>
  <section class="pane tree-pane">
    <div class="pane-head"><span>文件</span></div>
    <ul class="file-list">
      <li v-if="!selectedSessionId" class="empty">先选一个会话</li>
      <li v-else v-for="f in sessionFiles" :key="f.name"
          :class="['file-item', { active: f.name === previewFile, dir: f.is_dir }]"
          @click="!f.is_dir && readSessionFile(selectedSessionId, f.name)">
        <span class="icon">{{ f.is_dir ? '📁' : '📄' }}</span>
        <span class="name">{{ f.name }}</span>
        <span v-if="f.name === previewFile && previewLoading" class="load">…</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.tree-pane { flex: 1 1 0; min-width: 0; }
.pane-head { padding: .7rem .85rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: .9rem; }
.file-list { list-style: none; margin: 0; padding: .35rem; overflow-y: auto; flex: 1; }
.file-item { display: flex; align-items: center; gap: .4rem; padding: .4rem .5rem; border-radius: 8px; cursor: pointer; font-size: .82rem; color: #334155; }
.file-item:hover { background: #f1f5f9; }
.file-item.active { background: #eef2ff; color: #4f46d5; }
.file-item.dir { color: #94a3b8; cursor: default; }
.icon { width: 1.1em; }
.name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.load { color: #94a3b8; }
.empty { padding: 1rem; color: #94a3b8; font-size: .82rem; }
</style>
