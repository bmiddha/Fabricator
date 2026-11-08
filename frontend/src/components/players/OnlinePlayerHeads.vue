<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useAuthStore } from '../../stores/auth'
import { getOnlinePreview } from '../../api/playerDirectory'
import PlayerAvatar from './PlayerAvatar.vue'
import { t } from '../../i18n'

const props = defineProps({
  serverId: { type: String, default: null },
  status: { type: String, default: 'unknown' },
  loader: { type: String, default: '' },
})
const auth = useAuthStore()
const allowed = computed(() => props.serverId && props.loader !== 'velocity'
  && auth.can('server.view', props.serverId) && auth.can('players.read', props.serverId))
const snapshot = ref(null)
const loading = ref(false)
const error = ref('')
const players = computed(() => snapshot.value?.players.slice(0, 10) || [])
const remaining = computed(() => Math.max(0, (snapshot.value?.total || 0) - players.value.length))
const target = computed(() => ({ name: 'ServerPlayers', params: { id: props.serverId } }))
const publicUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const usesPublicAvatar = player => !snapshot.value?.offlineMode
  && player.namespace === 'minecraft' && publicUuid.test(player.uuid || '')
let timer = null
let generation = 0

async function load() {
  if (!allowed.value || props.status !== 'running') return
  if (timer) clearTimeout(timer)
  const current = ++generation
  loading.value = !snapshot.value
  error.value = ''
  try {
    const result = await getOnlinePreview(props.serverId)
    if (!Array.isArray(result.players) || !Number.isInteger(result.total) || result.total < 0) {
      throw new Error(t('onlineHeads.loadFailed'))
    }
    if (current === generation) snapshot.value = result
  } catch (failure) {
    if (current !== generation) return
    console.error('Online player preview failed', failure)
    snapshot.value = null
    error.value = failure.message || t('onlineHeads.loadFailed')
  } finally {
    if (current === generation) {
      loading.value = false
      timer = setTimeout(load, 5000)
    }
  }
}
watch([allowed, () => props.serverId, () => props.status, () => props.loader, () => auth.revision], () => {
  generation++
  if (timer) clearTimeout(timer)
  timer = null
  snapshot.value = null
  loading.value = false
  error.value = ''
  load()
}, { immediate: true })
onUnmounted(() => {
  generation++
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <section v-if="allowed" class="online-heads" :aria-label="$t('onlineHeads.title')">
    <header class="online-heads__header">
      <h2>{{ $t('onlineHeads.title') }}</h2>
      <router-link :to="target">{{ $t('onlineHeads.allPlayers') }}</router-link>
    </header>
    <p v-if="error" class="online-heads__error" role="alert">{{ error }} <button type="button" @click="load">{{ $t('common.retry') }}</button></p>
    <p v-else-if="props.status === 'stopped' || snapshot?.status === 'offline'">{{ $t('onlineHeads.offline') }}</p>
    <p v-else-if="loading" role="status">{{ $t('onlineHeads.loading') }}</p>
    <p v-else-if="!snapshot || snapshot.status === 'unavailable'">{{ $t('onlineHeads.unavailable') }}</p>
    <template v-else>
      <ul v-if="players.length" class="online-heads__list">
        <li v-for="player in players" :key="player.id || player.name">
          <router-link :to="player.id ? { name: 'PlayerDetail', params: { playerId: player.id } } : target" :title="player.name">
            <PlayerAvatar :name="player.name" :uuid="usesPublicAvatar(player) ? player.uuid : null" :size="32"
              :offline-mode="!usesPublicAvatar(player)" />
            <span>{{ player.name }}</span>
          </router-link>
        </li>
      </ul>
      <p v-else>{{ $t('onlineHeads.empty') }}</p>
      <router-link v-if="remaining" class="online-heads__remaining" :to="target">{{ $t('onlineHeads.remaining', { count: remaining }) }}</router-link>
      <p class="online-heads__note">{{ $t('onlineHeads.partial') }}</p>
    </template>
  </section>
</template>

<style scoped>
.online-heads { min-width: 0; padding: var(--space-3) 0; }
.online-heads__header { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
h2 { font-size: var(--text-sm); color: var(--text-primary); font-weight: 600; }
a, button { color: var(--text-secondary); font-size: var(--text-sm); text-underline-offset: 3px; }
a:hover, button:hover { color: var(--primary); }
a:focus-visible, button:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; }
.online-heads__list { display: flex; flex-wrap: wrap; list-style: none; gap: var(--space-3) var(--space-5); padding: 0; margin: 0; }
li a { display: flex; align-items: center; gap: var(--space-2); text-decoration: none; max-width: 180px; }
li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.online-heads__remaining { display: inline-block; margin-top: var(--space-3); }
p { font-size: var(--text-sm); line-height: var(--leading-normal); }
.online-heads__note { margin-top: var(--space-3); color: var(--text-muted); }
.online-heads__error { color: var(--danger); }
button { background: transparent; border: 0; text-decoration: underline; cursor: pointer; margin-left: var(--space-2); }
</style>
