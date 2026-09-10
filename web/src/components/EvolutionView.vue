<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useSessions } from '../composables/useSessions'

const {
  installedSkills, evolveRecords, evolvePending, evolveSelectedSkill,
  evolveRecordsLoading, evolvePendingLoading, evolveActionLoading, evolveError,
  loadInstalled, loadEvolvePending, loadEvolveRecords,
  approveEvolve, rejectEvolve, simplifyEvolve, selectEvolveSkill,
} = useSessions()

interface PendingRow {
  skill: string
  id: string
  source: string
  section: string
  summary: string
}
// pending(按 skill 分组)展平成行
const pendingRows = computed<PendingRow[]>(() => {
  const rows: PendingRow[] = []
  for (const [skill, recs] of Object.entries(evolvePending.value)) {
    for (const r of recs) rows.push({ skill, id: r.id, source: r.source, section: r.section, summary: r.summary })
  }
  return rows
})
const pendingCount = computed(() => pendingRows.value.length)
const pendingSkills = computed(() => Object.keys(evolvePending.value))

onMounted(async () => {
  loadEvolvePending()
  if (!installedSkills.value.length) await loadInstalled()
  if (!evolveSelectedSkill.value && installedSkills.value.length) {
    selectEvolveSkill(installedSkills.value[0].name)
  }
})

function onPick(e: Event) {
  const name = (e.target as HTMLSelectElement).value
  if (name) selectEvolveSkill(name)
}
function approveOne(row: PendingRow) { approveEvolve(row.skill, [row.id]) }
function rejectOne(row: PendingRow) { rejectEvolve(row.skill, [row.id]) }
function approveAll(skill: string) { approveEvolve(skill, null) }
function rejectAll(skill: string) { rejectEvolve(skill, null) }
</script>

<template>
  <div class="evolve-view">
    <section class="picker">
      <h3>🧬 进化</h3>
      <select :value="evolveSelectedSkill" @change="onPick" :disabled="!installedSkills.length">
        <option value="" disabled>选择 skill…</option>
        <option v-for="s in installedSkills" :key="s.name" :value="s.name">{{ s.name }}</option>
      </select>
      <button class="ghost" @click="loadEvolveRecords(evolveSelectedSkill)"
              :disabled="!evolveSelectedSkill || evolveRecordsLoading">
        {{ evolveRecordsLoading ? '…' : '刷新记录' }}
      </button>
    </section>

    <section class="pending">
      <h3>📥 待批 ({{ pendingCount }})
        <span class="warn">⚠ 待批存于内存,服务重启丢失</span>
      </h3>
      <ul>
        <li v-for="row in pendingRows" :key="row.skill + row.id">
          <div class="meta">
            <strong>{{ row.skill }}</strong> · <span>{{ row.id }}</span> ·
            <span>{{ row.source }}</span> · <span>{{ row.summary }}</span>
          </div>
          <div class="actions">
            <button class="ok" :disabled="evolveActionLoading" @click="approveOne(row)">✓ 批准</button>
            <button class="ghost" :disabled="evolveActionLoading" @click="rejectOne(row)">✗ 拒绝</button>
          </div>
        </li>
        <li v-if="!pendingCount && !evolvePendingLoading" class="empty">无待批记录</li>
      </ul>
      <div class="bulk" v-for="skill in pendingSkills" :key="'bulk-' + skill">
        <span>{{ skill }} 批量:</span>
        <button class="ok" :disabled="evolveActionLoading" @click="approveAll(skill)">全部批准</button>
        <button class="ghost" :disabled="evolveActionLoading" @click="rejectAll(skill)">全部拒绝</button>
      </div>
    </section>

    <section class="records">
      <h3>📊 经验记录: {{ evolveSelectedSkill || '—' }} ({{ evolveRecords.length }})</h3>
      <ul>
        <li v-for="r in evolveRecords" :key="r.id">
          <div class="meta">
            <strong>{{ r.id }}</strong> · <span class="score">{{ r.score.toFixed(2) }}</span> ·
            <span>{{ r.section }}</span> · <span>used {{ r.used }} / +{{ r.positive }}</span>
          </div>
          <span class="summary">{{ r.summary }}</span>
        </li>
        <li v-if="!evolveRecords.length && !evolveRecordsLoading" class="empty">该 skill 暂无经验记录</li>
      </ul>
    </section>

    <section class="actions-bar">
      <button :disabled="!evolveSelectedSkill || evolveActionLoading"
              @click="simplifyEvolve(evolveSelectedSkill)">
        {{ evolveActionLoading ? '处理中…' : '🧹 蒸馏清理 ' + (evolveSelectedSkill || '') }}
      </button>
    </section>

    <p v-if="evolveError" class="error">{{ evolveError }}</p>
  </div>
</template>

<style scoped>
.evolve-view {
  flex: 1; display: flex; flex-direction: column; gap: 1rem;
  padding: 1rem; min-height: 0; overflow: auto;
}
.picker { display: flex; align-items: center; gap: .6rem; }
.picker h3 { margin: 0; font-size: .95rem; color: #1e293b; }
select {
  padding: .45rem .6rem; border: 1px solid #cbd5e1;
  border-radius: 8px; font-size: .85rem; background: #fff;
}
h3 { margin: 0 0 .5rem; font-size: .9rem; color: #1e293b; }
.warn { font-size: .72rem; color: #b45309; margin-left: .5rem; font-weight: 400; }
ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
li {
  display: flex; align-items: center; justify-content: space-between; gap: .5rem;
  padding: .5rem .6rem; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
}
li .meta { min-width: 0; }
li strong { font-size: .85rem; color: #1e293b; }
li span { font-size: .78rem; color: #64748b; }
li .summary { font-size: .78rem; color: #475569; }
.score { color: #2563eb; font-weight: 600; }
.empty { color: #94a3b8; font-size: .8rem; justify-content: center; }
.actions { display: flex; gap: .3rem; }
.bulk { display: flex; align-items: center; gap: .4rem; margin-top: .4rem; font-size: .8rem; color: #475569; }
button {
  border: 0; background: #4f46d5; color: #fff; border-radius: 8px;
  padding: .45rem .8rem; cursor: pointer; font-size: .8rem;
}
button:disabled { opacity: .5; cursor: not-allowed; }
button.ghost { background: #e2e8f0; color: #475569; }
button.ok { background: #16a34a; }
.actions-bar { margin-top: .2rem; }
.error { color: #dc2626; font-size: .78rem; margin: .3rem 0; }
</style>
