<script setup lang="ts">
import { computed } from 'vue'
import { useSessions } from '../composables/useSessions'

const { previewFile, previewContent, previewLoading, historyAsBubbles, fromHistory } = useSessions()

const isHistory = computed(() => previewFile.value === 'history.json')
const formattedJson = computed(() => {
  try { return JSON.stringify(JSON.parse(previewContent.value), null, 2) } catch { return previewContent.value }
})
const bubbles = computed(() => {
  if (!isHistory.value) return []
  return fromHistory(
    previewContent.value
      .split('\n')
      .filter((l) => l.trim())
      .map((l) => { try { return JSON.parse(l) } catch { return null } })
      .filter((r): r is Record<string, unknown> => r !== null),
  )
})
</script>

<template>
  <section class="pane preview-pane">
    <div class="pane-head">
      <span>{{ previewFile || '预览' }}</span>
      <label v-if="isHistory" class="toggle">
        <input type="checkbox" v-model="historyAsBubbles" />
        聊天气泡
      </label>
    </div>
    <div class="preview-body">
      <div v-if="previewLoading" class="state">加载中…</div>
      <div v-else-if="!previewFile" class="state">选一个文件查看</div>
      <div v-else-if="isHistory && historyAsBubbles" class="bubbles">
        <div v-for="(m, i) in bubbles" :key="i" :class="['row', m.role]">
          <div v-if="m.role === 'tool'" class="tool-line">{{ m.content }}</div>
          <div v-else class="bubble">{{ m.content }}</div>
        </div>
      </div>
      <pre v-else class="json">{{ formattedJson }}</pre>
    </div>
  </section>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.preview-pane { flex: 3 1 0; min-width: 0; }
.pane-head { display: flex; justify-content: space-between; align-items: center; padding: .7rem .85rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: .9rem; }
.toggle { font-size: .78rem; font-weight: 400; color: #64748b; display: flex; align-items: center; gap: .3rem; }
.preview-body { flex: 1; overflow: auto; padding: 1rem; }
.state { color: #94a3b8; }
.json { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: .8rem; color: #1e293b; }
.bubbles { display: flex; flex-direction: column; gap: .55rem; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble { max-width: 80%; padding: .5rem .8rem; border-radius: 14px; white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
.row.user .bubble { background: #4f46d5; color: #fff; border-bottom-right-radius: 4px; }
.row.assistant .bubble { background: #f1f5f9; color: #1e293b; border-bottom-left-radius: 4px; }
.tool-line { font-family: ui-monospace, monospace; font-size: .78rem; color: #94a3b8; padding: .15rem .4rem; }
</style>
