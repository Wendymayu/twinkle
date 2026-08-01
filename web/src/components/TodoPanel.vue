<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
import type { TodoTask } from '../services/webClient'

const { todo, completedCount, box } = useSessions()

function grouped(tasks: TodoTask[]) {
  const inProgress = tasks.filter(t => t.status === 'in_progress')
  const pending = tasks.filter(t => t.status === 'pending')
  const completed = tasks.filter(t => t.status === 'completed')
  const cancelled = tasks.filter(t => t.status === 'cancelled')
  return { inProgress, pending, completed, cancelled }
}
</script>

<template>
  <aside class="todo-panel">
    <div class="todo-head">
      <span>Todo</span>
      <span class="todo-count" v-if="todo">{{ completedCount }}/{{ todo.total }}</span>
    </div>
    <div v-if="todo && todo.tasks.length" class="todo-list">
      <!-- In Progress -->
      <div v-if="grouped(todo.tasks).inProgress.length" class="todo-group">
        <div class="todo-group-label"><span class="dot in-progress"></span>进行中</div>
        <div v-for="t in grouped(todo.tasks).inProgress" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
          <span class="todo-deps" v-if="t.blocked_by.length">依赖: {{ t.blocked_by.join(', ').slice(0, 30) }}</span>
          <span class="todo-owner" v-if="t.owner">@{{ t.owner }}</span>
        </div>
      </div>
      <!-- Pending -->
      <div v-if="grouped(todo.tasks).pending.length" class="todo-group">
        <div class="todo-group-label"><span class="dot pending"></span>待处理</div>
        <div v-for="t in grouped(todo.tasks).pending" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
          <span class="todo-deps" v-if="t.blocked_by.length">依赖: {{ t.blocked_by.join(', ').slice(0, 30) }}</span>
          <span class="todo-owner" v-if="t.owner">@{{ t.owner }}</span>
        </div>
      </div>
      <!-- Completed -->
      <div v-if="grouped(todo.tasks).completed.length" class="todo-group">
        <div class="todo-group-label"><span class="dot completed"></span>已完成</div>
        <div v-for="t in grouped(todo.tasks).completed" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
          <span class="todo-result" v-if="t.result">{{ t.result }}</span>
        </div>
      </div>
      <!-- Cancelled -->
      <div v-if="grouped(todo.tasks).cancelled.length" class="todo-group">
        <div class="todo-group-label"><span class="dot cancelled"></span>已取消</div>
        <div v-for="t in grouped(todo.tasks).cancelled" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
        </div>
      </div>
    </div>
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
.todo-group { margin-bottom: .75rem; }
.todo-group-label { display: flex; align-items: center; gap: .4rem; font-size: .75rem; font-weight: 600; color: #64748b; padding: .25rem .25rem .15rem; text-transform: uppercase; letter-spacing: .05em; }
.dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.dot.in-progress { background: #6366f1; animation: pulse 1.5s ease-in-out infinite; }
.dot.pending { background: #94a3b8; }
.dot.completed { background: #10b981; }
.dot.cancelled { background: #ef4444; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
.todo-item { display: flex; flex-wrap: wrap; align-items: baseline; gap: .25rem; padding: .3rem .25rem; font-size: .85rem; }
.todo-item.completed .todo-subject { text-decoration: line-through; color: #94a3b8; }
.todo-item.cancelled .todo-subject { text-decoration: line-through; color: #ef4444; }
.todo-item.in-progress .todo-subject { color: #4f46d5; font-weight: 500; }
.todo-box { width: 1.1em; text-align: center; color: #4f46d5; flex-shrink: 0; }
.todo-item.completed .todo-box { color: #10b981; }
.todo-item.cancelled .todo-box { color: #ef4444; }
.todo-subject { flex: 1; min-width: 0; }
.todo-deps { font-size: .7rem; color: #f59e0b; background: #fef3c7; padding: 0 .35rem; border-radius: 3px; }
.todo-owner { font-size: .7rem; color: #6366f1; }
.todo-result { color: #64748b; font-size: .75rem; }
.todo-empty { padding: 1rem; color: #94a3b8; font-size: .85rem; }
</style>
