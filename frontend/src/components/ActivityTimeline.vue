<script setup>
import { onUnmounted, ref, watch } from 'vue'
import Panel from './ui/Panel.vue'
import AppButton from './ui/AppButton.vue'
import { getActivity } from '../api/events'
import { useAuthStore } from '../stores/auth'
import { eventLabel, eventTarget } from '../utils/events'
import { formatTimestamp } from '../utils/format'

const props = defineProps({ serverId: { type: String, default: '' } })
const auth = useAuthStore()
const items = ref([])
const next = ref(null)
const busy = ref(false)
const error = ref('')
const alertsOnly = ref(false)
let requestId = 0

async function load(more = false) {
  const id = ++requestId
  busy.value = true
  error.value = ''
  if (!more) { items.value = []; next.value = null }
  try {
    const data = await getActivity(props.serverId, more ? next.value : null, alertsOnly.value)
    if (id !== requestId) return
    items.value = more ? [...items.value, ...data.items] : data.items
    next.value = data.next
  } catch (e) {
    if (id === requestId) error.value = e.message
  } finally {
    if (id === requestId) busy.value = false
  }
}
watch([() => props.serverId, alertsOnly], () => load(), { immediate: true })
onUnmounted(() => { requestId++ })
</script>

<template>
  <div class="activity">
    <p class="activity__intro">{{ $t('activity.description') }}</p>
    <div class="activity__toolbar">
      <label class="activity__filter"><input v-model="alertsOnly" type="checkbox" />{{ $t('activity.alertsOnly') }}</label>
      <AppButton variant="ghost" :loading="busy" @click="load()">{{ $t('activity.refresh') }}</AppButton>
    </div>
    <p v-if="error" role="alert" class="activity__error">{{ error }}</p>
    <Panel :title="$t('activity.title')" :padded="false">
      <p v-if="busy && !items.length" class="activity__empty" role="status">{{ $t('activity.loading') }}</p>
      <p v-else-if="!items.length && !error" class="activity__empty">{{ $t('activity.empty') }}</p>
      <ol v-else class="activity__list" :aria-label="$t('activity.title')">
        <li v-for="event in items" :id="`event-${event.id}`" :key="event.id" class="activity__event">
          <div class="activity__when">
            <time :datetime="new Date(event.at * 1000).toISOString()">{{ formatTimestamp(event.at * 1000) }}</time>
            <span class="activity__outcome" :class="{ 'activity__error': ['failed', 'interrupted'].includes(event.outcome) }">
              {{ $t(`activity.outcomes.${event.outcome}`) }}
            </span>
          </div>
          <div class="activity__detail">
            <strong>{{ eventLabel(event.action) }}</strong>
            <p>{{ $t('activity.by', { actor: event.actor.id }) }}
              <span class="activity__kind">({{ $t(`activity.actors.${event.actor.kind}`) }})</span>
            </p>
            <p v-if="!serverId && event.server_id" class="activity__scope">{{ $t('activity.server', { id: event.server_id }) }}</p>
            <p v-if="event.ref" class="activity__scope">{{ $t('activity.job', { id: event.ref }) }}</p>
          </div>
          <router-link v-if="eventTarget(event, auth)" :to="eventTarget(event, auth)" class="activity__link">{{ $t('activity.openView') }}</router-link>
        </li>
      </ol>
    </Panel>
    <AppButton v-if="next" variant="ghost" :loading="busy" @click="load(true)">{{ $t('activity.loadOlder') }}</AppButton>
    <p class="activity__note">{{ $t('activity.retention') }}</p>
  </div>
</template>

<style scoped>
.activity { display: grid; gap: var(--space-4); min-width: 0; }
.activity__intro, .activity__note { color: var(--text-muted); line-height: 1.6; max-width: 75ch; font-size: var(--text-sm); }
.activity__toolbar, .activity__filter { display: flex; align-items: center; gap: var(--space-3); }
.activity__toolbar { justify-content: space-between; }
.activity__filter { font-size: var(--text-sm); cursor: pointer; }
.activity__list { list-style: none; }
.activity__event { display: grid; grid-template-columns: minmax(150px, 190px) 1fr auto; gap: var(--space-4); padding: var(--space-4); border-bottom: 1px solid var(--border-color); font-size: var(--text-sm); overflow-wrap: anywhere; }
.activity__event:last-child { border-bottom: 0; }
.activity__when, .activity__detail { display: grid; gap: var(--space-2); align-content: start; }
.activity__when, .activity__kind, .activity__scope { color: var(--text-muted); }
.activity__detail strong { color: var(--text-primary); font-weight: 500; }
.activity__outcome { color: var(--text-secondary); }
.activity__error { color: var(--danger); font-size: var(--text-sm); }
.activity__link { color: var(--text-primary); text-underline-offset: 3px; align-self: start; }
.activity__link:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.activity__empty { padding: var(--space-8) var(--space-4); color: var(--text-muted); font-size: var(--text-sm); }
@media (max-width: 768px) {
  .activity__event { grid-template-columns: 1fr; gap: var(--space-3); }
  .activity__when { display: flex; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
}
</style>
