<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { getPlayerDirectory, getDirectoryPlayer } from '../api/playerDirectory'
import AppButton from '../components/ui/AppButton.vue'
import { t } from '../i18n'

const route = useRoute()
const auth = useAuthStore()
const data = ref(null)
const detail = ref(null)
const error = ref('')
const loading = ref(false)
const query = ref('')
const relation = ref('all')
const server = ref('')
const offset = ref(0)
let generation = 0
const playerId = computed(() => route.params.playerId)
const total = computed(() => data.value?.total || 0)
const date = value => value ? new Date(value).toLocaleString() : t('playerDirectory.unknown')
const state = value => t(`playerDirectory.${value === true ? 'yes' : value === false ? 'no' : 'unknown'}`)
const namespace = value => t(`playerDirectory.${value === 'minecraft' ? 'minecraft' : value.startsWith('offline:') ? 'offline' : 'unverified'}`)
const count = (player, key) => player.servers.filter(s => s[key] === true).length

async function load() {
  const current = ++generation
  loading.value = true
  error.value = ''
  detail.value = null
  try {
    const result = playerId.value
      ? await getDirectoryPlayer(playerId.value)
      : await getPlayerDirectory({ q: query.value, relation: relation.value, server: server.value, offset: offset.value })
    if (current !== generation) return
    if (playerId.value) detail.value = result
    else data.value = result
  } catch (failure) {
    if (current !== generation) return
    data.value = null
    console.error('Player directory load failed', failure)
    error.value = failure.message || t('playerDirectory.loadFailed')
  } finally {
    if (current === generation) loading.value = false
  }
}
function search() { offset.value = 0; load() }
function page(delta) { offset.value += delta; load() }
watch([playerId, () => auth.revision], () => {
  data.value = null
  query.value = ''
  relation.value = 'all'
  server.value = ''
  offset.value = 0
  load()
}, { immediate: true })
onUnmounted(() => { generation++ })
</script>

<template>
  <div class="directory">
    <header class="directory__header">
      <div>
        <router-link v-if="playerId" :to="{ name: 'PlayersDirectory' }">{{ $t('playerDirectory.back') }}</router-link>
        <h1>{{ detail?.name || $t('playerDirectory.title') }}</h1>
        <p>{{ $t('playerDirectory.intro') }}</p>
      </div>
      <AppButton variant="ghost" :disabled="loading" @click="load">{{ $t('common.refresh') }}</AppButton>
    </header>

    <form v-if="!playerId" class="directory__filters" @submit.prevent="search">
      <label class="directory__search">{{ $t('playerDirectory.search') }}
        <input v-model="query" type="search" maxlength="100" :placeholder="$t('playerDirectory.searchHint')">
      </label>
      <label>{{ $t('playerDirectory.relationship') }}
        <select v-model="relation" @change="search">
          <option v-for="key in ['all', 'joined', 'whitelisted', 'online']" :key="key" :value="key">{{ $t(`playerDirectory.${key}`) }}</option>
        </select>
      </label>
      <label>{{ $t('playerDirectory.server') }}
        <select v-model="server" @change="search">
          <option value="">{{ $t('playerDirectory.allServers') }}</option>
          <option v-for="scope in data?.servers || []" :key="scope.id" :value="scope.id">{{ scope.name }}</option>
        </select>
      </label>
      <AppButton type="submit" :disabled="loading">{{ $t('playerDirectory.find') }}</AppButton>
    </form>

    <p v-if="error" role="alert" class="directory__error">{{ error }}</p>
    <p v-else-if="loading" role="status">{{ $t('playerDirectory.loading') }}</p>
    <template v-else-if="detail">
      <div class="directory__identity">
        <span>{{ namespace(detail.namespace) }}</span>
        <code>{{ detail.uuid }}</code>
        <p>{{ $t('playerDirectory.names') }}: {{ detail.names.join(', ') || $t('playerDirectory.unknown') }}</p>
        <p>{{ $t('playerDirectory.lastSeen') }}: {{ date(detail.lastSeen) }}</p>
      </div>
      <h2>{{ $t('playerDirectory.servers') }}</h2>
      <div class="directory__table">
        <table>
          <thead><tr><th>{{ $t('playerDirectory.server') }}</th><th>{{ $t('playerDirectory.joined') }}</th><th>{{ $t('playerDirectory.whitelisted') }}</th><th>{{ $t('playerDirectory.online') }}</th><th>{{ $t('playerDirectory.lastSeen') }}</th></tr></thead>
          <tbody><tr v-for="scope in detail.servers" :key="scope.id">
            <td><router-link :to="{ name: 'ServerPlayers', params: { id: scope.id } }">{{ scope.name }}</router-link></td>
            <td>{{ state(scope.joined) }}</td><td>{{ state(scope.whitelisted) }}</td><td>{{ state(scope.online) }}</td><td>{{ date(scope.lastSeen) }}</td>
          </tr></tbody>
        </table>
      </div>
    </template>
    <template v-else-if="data">
      <p class="directory__summary">{{ $t('playerDirectory.results', { count: total }) }}</p>
      <p v-if="!data.players.length">{{ $t(data.servers.length ? 'playerDirectory.empty' : 'playerDirectory.noAccess') }}</p>
      <div v-else class="directory__table">
        <table>
          <thead><tr><th>{{ $t('playerDirectory.player') }}</th><th>{{ $t('playerDirectory.joined') }}</th><th>{{ $t('playerDirectory.whitelisted') }}</th><th>{{ $t('playerDirectory.online') }}</th><th>{{ $t('playerDirectory.lastSeen') }}</th></tr></thead>
          <tbody><tr v-for="player in data.players" :key="player.id">
            <td><router-link :to="{ name: 'PlayerDetail', params: { playerId: player.id } }">{{ player.name }}</router-link><small>{{ namespace(player.namespace) }}</small><code>{{ player.uuid }}</code></td>
            <td>{{ count(player, 'joined') }}</td><td>{{ count(player, 'whitelisted') }}</td><td>{{ count(player, 'online') }}</td><td>{{ date(player.lastSeen) }}</td>
          </tr></tbody>
        </table>
      </div>
      <nav v-if="total > data.limit" class="directory__pagination" :aria-label="$t('playerDirectory.pagination')">
        <AppButton variant="ghost" :disabled="offset === 0" @click="page(-data.limit)">{{ $t('playerDirectory.previous') }}</AppButton>
        <span>{{ offset + 1 }}–{{ Math.min(offset + data.limit, total) }} / {{ total }}</span>
        <AppButton variant="ghost" :disabled="offset + data.limit >= total" @click="page(data.limit)">{{ $t('playerDirectory.next') }}</AppButton>
      </nav>
      <details v-if="data.servers.length" class="directory__coverage">
        <summary>{{ $t('playerDirectory.coverage') }}</summary>
        <p v-for="scope in data.servers" :key="scope.id">{{ scope.name }}: {{ $t('playerDirectory.whitelisted') }} — {{ $t(`playerDirectory.${scope.whitelist}`) }}; {{ $t('playerDirectory.online') }} — {{ $t(`playerDirectory.${scope.online}`) }}; {{ $t('playerDirectory.joined') }} — {{ $t(`playerDirectory.${scope.history}`) }}</p>
      </details>
    </template>
    <p class="directory__note">{{ $t('playerDirectory.limits') }}</p>
  </div>
</template>

<style scoped>
.directory { padding: var(--app-content-padding); max-width: 1300px; width: 100%; margin: 0 auto; }
.directory__header { display: flex; justify-content: space-between; align-items: start; gap: var(--space-4); margin-bottom: var(--space-8); }
h1 { color: var(--text-primary); font-size: 26px; margin: var(--space-2) 0; overflow-wrap: anywhere; }
h2 { font-size: 18px; margin: var(--space-8) 0 var(--space-4); }
p { line-height: var(--leading-normal); }
.directory__filters { display: flex; align-items: end; flex-wrap: wrap; gap: var(--space-4); margin-bottom: var(--space-6); }
label { display: grid; gap: var(--space-2); font-size: var(--text-sm); }
.directory__search { flex: 1; min-width: 200px; }
input, select { min-height: 38px; width: 100%; }
input::placeholder { color: var(--text-muted); opacity: 1; }
.directory__summary { margin-bottom: var(--space-3); }
.directory__table { overflow-x: auto; border: 1px solid var(--border-color); border-radius: var(--radius-md); }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: var(--text-sm); }
th, td { padding: var(--space-4); border-bottom: 1px solid var(--border-color); vertical-align: top; }
th { background: var(--bg-secondary); font-weight: 500; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--bg-secondary); }
td:first-child { min-width: 230px; }
a { color: var(--text-primary); text-decoration: underline; text-underline-offset: 3px; }
a:hover { color: var(--primary); }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; }
small, td code { display: block; margin-top: var(--space-1); }
code { font-size: var(--text-xs); color: var(--text-secondary); overflow-wrap: anywhere; }
.directory__identity { display: grid; gap: var(--space-3); }
.directory__pagination { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); margin-top: var(--space-4); }
.directory__coverage { margin-top: var(--space-6); }
summary { cursor: pointer; margin-bottom: var(--space-3); }
.directory__note { max-width: 75ch; margin-top: var(--space-6); font-size: var(--text-sm); }
.directory__error { color: var(--danger); }
@media (max-width: 600px) {
  .directory__filters label { flex: 1 1 100%; }
  .directory__header { gap: var(--space-2); }
  h1 { font-size: 22px; }
  th, td { padding: var(--space-3); }
}
</style>
