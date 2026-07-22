<script setup lang="ts">
import { computed } from 'vue'
import { useSessions } from '../composables/useSessions'

const { todo, completedCount, box } = useSessions()
</script>

<template>
  <aside class="todo-panel">
    <div class="todo-head">
      <span>Todo</span>
      <span class="todo-count" v-if="todo">{{ completedCount }}/{{ todo.total }}</span>
    </div>
    <ul v-if="todo && todo.tasks.length" class="todo-list">
      <li v-for="t in todo.tasks" :key="t.idx" :class="['todo-item', t.status]">
        <span class="todo-box">{{ box(t.status) }}</span>
        <span class="todo-idx">{{ t.idx }}.</span>
        <span class="todo-title">{{ t.title }}</span>
        <span class="todo-result" v-if="t.result">{{ t.result }}</span>
      </li>
    </ul>
    <p v-else class="todo-empty">暂无任务</p>
  </aside>
</template>

<style scoped>
.todo-panel {
  width: 280px; flex: 0 0 280px; border-left: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (max-width: 640px) {
  .todo-panel { width: 100%; flex: 0 0 auto; border-left: 0; border-top: 1px solid #e2e8f0; max-height: 40%; }
}
.todo-head { display: flex; justify-content: space-between; padding: .9rem 1rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; }
.todo-count { color: #6366f1; }
.todo-list { list-style: none; margin: 0; padding: .5rem; overflow-y: auto; flex: 1; }
.todo-item { display: flex; align-items: baseline; gap: .35rem; padding: .35rem .25rem; font-size: .9rem; }
.todo-item.completed .todo-title { text-decoration: line-through; color: #94a3b8; }
.todo-box { width: 1.1em; text-align: center; color: #4f46d5; }
.todo-item.completed .todo-box { color: #10b981; }
.todo-result { color: #64748b; font-size: .8rem; }
.todo-empty { padding: 1rem; color: #94a3b8; font-size: .85rem; }
</style>
