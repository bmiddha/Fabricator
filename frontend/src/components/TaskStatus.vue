<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { getTasks } from '../api/events'
import { useAuthStore } from '../stores/auth'
import { eventLabel } from '../utils/events'
import { t, te } from '../i18n'

const props = defineProps({
  serverId: { type: String, required: true },
  actions: { type: Array, default: null },
})
const emit = defineEmits(['update', 'unavailable'])
const auth = useAuthStore()
const tasks = ref([])
const loading = ref(true)
const unavailable = ref(false)
const truncated = ref(false)
let timer
let revision = 0
const visible = computed(() => {
  const matching = tasks.value.filter(task => !props.actions || props.actions.includes(task.action))
  return [...matching.filter(task => task.active), ...matching.filter(task => !task.active).slice(0, 3)]
})
function stage(task) {
  const key = `tasks.phases.${task.phase}`
  return te(key) ? t(key) : t('tasks.otherStage', { stage: task.phase.replaceAll('_', ' ') })
}
function operation(task) {
  const key = `tasks.actions.${task.action.replaceAll('.', '_')}`
  return te(key) ? t(key) : eventLabel(task.action)
}
function seconds(task) {
  return Math.floor(Math.max(0, task.elapsed_seconds || 0))
}
function measured(task) {
  return task.active && !task.stale && !unavailable.value
    && ['uploading', 'downloading', 'verifying'].includes(task.phase)
    && Number.isFinite(task.total_bytes) && task.total_bytes > 0
    && Number.isFinite(task.transferred_bytes) && task.transferred_bytes >= 0
}
async function load(id = revision) {
  const serverId = props.serverId
  const authRevision = auth.revision
  if (!props.serverId || !auth.can('server.view', props.serverId)
      || (auth.checked && auth.enabled && !auth.isAuthenticated)) {
    tasks.value = []
    loading.value = false
    emit('update', [], serverId)
    return
  }
  try {
    const data = await getTasks(serverId)
    if (id !== revision || serverId !== props.serverId || authRevision !== auth.revision) return
    if (!Array.isArray(data?.items) || data.items.some(task =>
      typeof task.id !== 'string' || typeof task.action !== 'string'
      || typeof task.phase !== 'string' || typeof task.active !== 'boolean'
      || typeof task.job_id !== 'string'
      || (task.server_id !== serverId && task.related_server !== serverId)
    )) throw new Error('Invalid task status response')
    tasks.value = data.items
    truncated.value = Boolean(data.truncated)
    unavailable.value = false
    emit('update', data.items, serverId)
  } catch {
    if (id !== revision || serverId !== props.serverId || authRevision !== auth.revision) return
    unavailable.value = true
    emit('unavailable', serverId)
  } finally {
    if (id === revision) {
      loading.value = false
      timer = setTimeout(() => load(id), 3000)
    }
  }
}
watch([() => props.serverId, () => auth.revision], () => {
  clearTimeout(timer)
  revision++
  tasks.value = []
  unavailable.value = false
  loading.value = true
  emit('unavailable', props.serverId)
  load()
}, { immediate: true })
onUnmounted(() => { revision++; clearTimeout(timer) })
</script>

<template>
  <section class="task-status" :aria-label="$t('tasks.title')">
    <p v-if="loading" class="task-status__notice" role="status">{{ $t('tasks.loading') }}</p>
    <p v-if="unavailable" class="task-status__notice" role="status">{{ $t('tasks.unavailable') }}</p>
    <ol v-if="visible.length" class="task-status__list">
      <li v-for="task in visible" :key="task.id" class="task-status__row">
        <svg class="task-status__icon" width="16" height="16" viewBox="0 0 16 16"
          fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <circle cx="8" cy="8" r="6" />
          <path v-if="task.active" d="M8 4v4l3 2" />
          <path v-else-if="['done', 'completed'].includes(task.phase)" d="M5 8l2 2 4-4" />
          <path v-else d="M8 4v5m0 2v1" />
        </svg>
        <div class="task-status__detail">
          <div class="task-status__heading">
            <strong>{{ operation(task) }}</strong>
            <span>{{ task.server_name || task.server_id }}</span>
          </div>
          <p class="task-status__phase" role="status" aria-atomic="true">
            {{ stage(task) }}<span v-if="task.stale"> · {{ $t('tasks.stale') }}</span>
          </p>
          <progress v-if="measured(task)" class="task-status__progress"
            :value="Math.min(task.transferred_bytes, task.total_bytes)" :max="task.total_bytes"
            :aria-label="stage(task)" />
        </div>
        <span class="task-status__elapsed">{{ $t('tasks.elapsed', { seconds: seconds(task) }) }}</span>
      </li>
    </ol>
    <p v-if="truncated" class="task-status__notice">{{ $t('tasks.truncated') }}</p>
  </section>
</template>

<style scoped>
.task-status { min-width: 0; }
.task-status__list { display: grid; gap: var(--space-2); list-style: none; margin: 0; padding: 0; }
.task-status__row { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--border-color); border-radius: var(--radius-md); background: var(--bg-secondary); font-size: var(--text-sm); }
.task-status__icon { flex: none; margin-top: 2px; color: var(--text-secondary); }
.task-status__detail { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.task-status__heading { display: flex; gap: var(--space-2); flex-wrap: wrap; color: var(--text-primary); }
.task-status__heading strong { font-weight: 600; }
.task-status__phase, .task-status__elapsed, .task-status__notice { color: var(--text-secondary); font-size: var(--text-xs); }
.task-status__phase { margin: var(--space-1) 0 0; }
.task-status__elapsed { flex: none; white-space: nowrap; }
.task-status__notice { margin: 0 0 var(--space-2); line-height: 1.5; }
.task-status__progress { width: 100%; max-width: 24rem; height: 6px; margin-top: var(--space-2); accent-color: var(--primary); }
@media (max-width: 480px) {
  .task-status__row { flex-wrap: wrap; }
  .task-status__elapsed { margin-left: calc(16px + var(--space-3)); }
}
</style>
