<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSessions } from '../composables/useSessions'

const { installedSkills, searchResults, skillsLoading, skillsError,
        loadInstalled, searchSkills, installSkill } = useSessions()

const query = ref('')
// skill_url currently being installed (disables its button + shows 安装中…)
const installing = ref<string | null>(null)
const toast = ref<string | null>(null)

onMounted(() => { loadInstalled() })

async function onSearch(force = false) {
  await searchSkills(query.value.trim(), force)
}

async function onInstall(url: string) {
  installing.value = url
  toast.value = null
  try {
    const r = await installSkill(url)
    toast.value = r.ok ? `✓ 已安装：${r.skillName}` : `✗ ${r.error}`
  } finally {
    installing.value = null
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
        </li>
        <li v-if="!installedSkills.length" class="empty">暂无已安装 skill</li>
      </ul>
    </section>

    <section class="search">
      <h3>从 SkillNet 搜索</h3>
      <div class="search-bar">
        <input v-model="query" placeholder="关键词…" @keyup.enter="onSearch()" />
        <button :disabled="skillsLoading" @click="onSearch()">
          {{ skillsLoading ? '搜索中…' : '搜索' }}
        </button>
        <button :disabled="skillsLoading" class="ghost"
                @click="onSearch(true)" title="强制刷新目录(跳过缓存)">刷新</button>
      </div>
      <p v-if="skillsError" class="error">{{ skillsError }}</p>
      <ul class="results">
        <li v-for="s in searchResults" :key="s.skill_url">
          <div class="meta"><strong>{{ s.name }}</strong> — <span>{{ s.description }}</span></div>
          <button :disabled="installing === s.skill_url" @click="onInstall(s.skill_url)">
            {{ installing === s.skill_url ? '安装中…' : '安装' }}
          </button>
        </li>
        <li v-if="!searchResults.length && !skillsLoading && !skillsError" class="empty">
          输入关键词搜索 SkillNet 中的 skill
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
