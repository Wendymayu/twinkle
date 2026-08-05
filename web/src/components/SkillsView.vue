<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSessions } from '../composables/useSessions'

const { installedSkills, searchResults, skillsLoading, skillsError,
        loadInstalled, searchSkills, installSkill, uninstallSkill, clearSearch } = useSessions()

const query = ref('')
const source = ref<'skillnet' | 'skillhub'>('skillhub')
const installing = ref<string | null>(null)
const uninstalling = ref<string | null>(null)
const toast = ref<string | null>(null)

onMounted(() => { loadInstalled() })

function installKey(s: any): string {
  return source.value === 'skillhub' ? `skillhub:${s.slug}` : `skillnet:${s.skill_url}`
}

function switchSource(s: 'skillnet' | 'skillhub') {
  if (source.value === s) return
  source.value = s
  clearSearch()  // 切来源清掉旧结果(字段不同,避免渲染错位)
}

async function onSearch(force = false) {
  await searchSkills(query.value.trim(), force, source.value)
}

async function onInstall(s: any) {
  const key = installKey(s)
  installing.value = key
  toast.value = null
  try {
    const args: { source: 'skillnet' | 'skillhub'; slug?: string; url?: string } =
      source.value === 'skillhub'
        ? { source: 'skillhub', slug: s.slug }
        : { source: 'skillnet', url: s.skill_url }
    const r = await installSkill(args)
    toast.value = r.ok ? `✓ 已安装：${r.skillName}` : `✗ ${r.error}`
  } finally {
    installing.value = null
  }
}

async function onUninstall(name: string) {
  if (!confirm(`确定卸载 skill「${name}」？此操作不可撤销。`)) return
  uninstalling.value = name
  toast.value = null
  try {
    const r = await uninstallSkill(name)
    toast.value = r.ok ? `✓ 已卸载：${name}` : `✗ ${r.error}`
  } finally {
    uninstalling.value = null
  }
}
</script>

<template>
  <div class="skills-view">
    <section class="installed">
      <h3>已安装 ({{ installedSkills.length }})</h3>
      <ul>
        <li v-for="s in installedSkills" :key="s.name">
          <div class="meta"><strong>{{ s.name }}</strong> — <span>{{ s.description }}</span></div>
          <button class="ghost uninstall" :disabled="uninstalling === s.name" @click="onUninstall(s.name)">
            {{ uninstalling === s.name ? '…' : '卸载' }}
          </button>
        </li>
        <li v-if="!installedSkills.length" class="empty">暂无已安装 skill</li>
      </ul>
    </section>

    <section class="search">
      <div class="source-toggle">
        <button :class="{ active: source === 'skillhub' }" @click="switchSource('skillhub')">SkillHub</button>
        <button :class="{ active: source === 'skillnet' }" @click="switchSource('skillnet')">SkillNet</button>
      </div>
      <h3>从 {{ source === 'skillhub' ? 'SkillHub' : 'SkillNet' }} 搜索</h3>
      <div class="search-bar">
        <input v-model="query" placeholder="关键词…" @keyup.enter="onSearch()" />
        <button :disabled="skillsLoading" @click="onSearch()">
          {{ skillsLoading ? '搜索中…' : '搜索' }}
        </button>
        <button :disabled="skillsLoading" class="ghost"
                @click="onSearch(true)" title="强制刷新(跳过缓存)">刷新</button>
      </div>
      <p v-if="skillsError" class="error">{{ skillsError }}</p>
      <ul class="results">
        <li v-for="s in searchResults" :key="installKey(s)">
          <div class="meta">
            <strong>{{ s.name }}</strong> — <span>{{ s.description }}</span>
            <span v-if="source === 'skillhub'" class="stats">↓ {{ (s as any).downloads }} · score {{ (s as any).score }}</span>
          </div>
          <button :disabled="installing === installKey(s)" @click="onInstall(s)">
            {{ installing === installKey(s) ? '安装中…' : '安装' }}
          </button>
        </li>
        <li v-if="!searchResults.length && !skillsLoading && !skillsError" class="empty">
          输入关键词搜索 {{ source === 'skillhub' ? 'SkillHub' : 'SkillNet' }} 中的 skill
        </li>
      </ul>
      <p v-if="toast" class="toast">{{ toast }}</p>
    </section>
  </div>
</template>

<style scoped>
.skills-view {
  flex: 1; display: flex; flex-direction: column; gap: 1rem;
  padding: 1rem; min-height: 0; overflow: auto;
}
h3 { margin: 0 0 .5rem; font-size: .9rem; color: #1e293b; }
ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
li {
  display: flex; align-items: center; justify-content: space-between; gap: .5rem;
  padding: .5rem .6rem; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
}
li .meta { min-width: 0; }
li strong { font-size: .85rem; color: #1e293b; }
li span { font-size: .78rem; color: #64748b; }
.empty { color: #94a3b8; font-size: .8rem; justify-content: center; }
.source-toggle { display: flex; gap: .3rem; margin-bottom: .3rem; }
.source-toggle button {
  border: 1px solid #cbd5e1; background: #fff; color: #475569;
  border-radius: 8px; padding: .35rem .7rem; cursor: pointer; font-size: .8rem;
}
.source-toggle button.active { background: #4f46d5; color: #fff; border-color: #4f46d5; }
.stats { margin-left: .4rem; font-size: .72rem; color: #2563eb; }
.search-bar { display: flex; gap: .4rem; }
input {
  flex: 1; padding: .45rem .6rem; border: 1px solid #cbd5e1;
  border-radius: 8px; font-size: .85rem;
}
button {
  border: 0; background: #4f46d5; color: #fff; border-radius: 8px;
  padding: .45rem .8rem; cursor: pointer; font-size: .8rem;
}
button:disabled { opacity: .5; cursor: not-allowed; }
button.ghost { background: #e2e8f0; color: #475569; }
.error { color: #dc2626; font-size: .78rem; margin: .3rem 0; }
.toast { margin: .4rem 0; font-size: .8rem; color: #475569; }
</style>
