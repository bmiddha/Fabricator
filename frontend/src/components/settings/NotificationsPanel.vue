<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import Panel from '../ui/Panel.vue'
import AppButton from '../ui/AppButton.vue'
import ConfirmModal from '../modals/ConfirmModal.vue'
import { getNotifications, saveDestination, deleteDestination, testDestination } from '../../api/events'
import { useAuthStore } from '../../stores/auth'
import { eventLabel, eventTarget } from '../../utils/events'
import { formatTimestamp } from '../../utils/format'
import { t } from '../../i18n'

const auth = useAuthStore()
const destinations = ref([])
const deliveries = ref([])
const alerts = ref([])
const editing = ref(false)
const editingId = ref(null)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const removing = ref(null)
const alertKinds = ['server.crashed', 'backup.failed', 'job.failed', 'disk.pressure', 'disk.recovered']
const blank = () => ({ name: '', kind: 'webhook', url: '', enabled: true, alerts: [...alertKinds] })
const form = ref(blank())
let timer
let alive = true
let requestId = 0

async function load() {
  const id = ++requestId
  loading.value = true
  try {
    const data = await getNotifications()
    if (!alive || id !== requestId) return
    destinations.value = data.destinations
    deliveries.value = data.deliveries
    alerts.value = data.alerts
    error.value = ''
  } catch (e) {
    if (alive && id === requestId) error.value = e.message
  } finally {
    if (alive && id === requestId) loading.value = false
  }
}
function edit(destination = null) {
  editingId.value = destination?.id || null
  form.value = destination ? { name: destination.name, kind: destination.kind, url: '', enabled: destination.enabled, alerts: [...destination.alerts] } : blank()
  editing.value = true
  notice.value = ''
}
function cancel() {
  editing.value = false
  form.value = blank() // Remove the entered secret from component state.
}
async function act(action, message) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = message
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
async function save() {
  const data = { ...form.value }
  if (editingId.value && !data.url) delete data.url
  await act(async () => { await saveDestination(data, editingId.value); cancel() }, t('notifications.saved'))
}
async function remove() {
  await act(async () => { await deleteDestination(removing.value.id); removing.value = null }, t('notifications.removed'))
}
onMounted(() => {
  load()
  timer = setInterval(() => {
    if (!loading.value && !busy.value && deliveries.value.some(d => ['pending', 'sending'].includes(d.state))) load()
  }, 5000)
})
onUnmounted(() => { alive = false; clearInterval(timer); form.value = blank() })
</script>

<template>
  <div class="notifications">
    <p class="notifications__intro">{{ $t('notifications.description') }}</p>
    <p v-if="error" role="alert" class="notifications__error">{{ error }}</p>
    <p v-if="notice" role="status" class="notifications__notice">{{ notice }}</p>
    <Panel :title="$t('notifications.destinations')">
      <template #action>
        <AppButton v-if="!editing" :disabled="busy || loading" @click="edit()">{{ $t('notifications.add') }}</AppButton>
      </template>
      <form v-if="editing" class="notifications__form" @submit.prevent="save">
        <h4>{{ $t(editingId ? 'notifications.edit' : 'notifications.add') }}</h4>
        <label for="notification-name">{{ $t('notifications.name') }}
          <input id="notification-name" v-model="form.name" required maxlength="80" :disabled="busy" />
        </label>
        <label for="notification-kind">{{ $t('notifications.kind') }}
          <select id="notification-kind" v-model="form.kind" :disabled="busy">
            <option value="webhook">{{ $t('notifications.webhook') }}</option>
            <option value="discord">Discord</option>
          </select>
        </label>
        <label for="notification-url">{{ $t('notifications.url') }}
          <input id="notification-url" v-model="form.url" type="password" :required="!editingId" autocomplete="new-password" maxlength="2048" :disabled="busy" aria-describedby="notification-url-hint" />
        </label>
        <p id="notification-url-hint" class="notifications__hint">{{ $t(editingId ? 'notifications.keepUrl' : 'notifications.urlHelp') }}</p>
        <fieldset :disabled="busy">
          <legend>{{ $t('notifications.triggers') }}</legend>
          <label v-for="kind in alertKinds" :key="kind" class="notifications__check">
            <input v-model="form.alerts" type="checkbox" :value="kind" />{{ $t(`notifications.kinds.${kind.replaceAll('.', '_')}`) }}
          </label>
        </fieldset>
        <label class="notifications__check"><input v-model="form.enabled" type="checkbox" :disabled="busy" />{{ $t('notifications.enabled') }}</label>
        <div class="notifications__actions">
          <AppButton type="submit" :loading="busy" :disabled="!form.alerts.length">{{ $t('notifications.save') }}</AppButton>
          <AppButton variant="ghost" :disabled="busy" @click="cancel">{{ $t('common.cancel') }}</AppButton>
        </div>
      </form>
      <p v-if="loading && !destinations.length" role="status">{{ $t('activity.loading') }}</p>
      <p v-else-if="!destinations.length && !editing" class="notifications__hint">{{ $t('notifications.empty') }}</p>
      <ul class="notifications__list">
        <li v-for="destination in destinations" :key="destination.id" class="notifications__destination">
          <div>
            <strong>{{ destination.name }}</strong>
            <p class="notifications__hint">{{ destination.kind === 'discord' ? 'Discord' : $t('notifications.webhook') }} · {{ $t(destination.enabled ? 'notifications.enabled' : 'notifications.paused') }}</p>
          </div>
          <div class="notifications__actions">
            <AppButton variant="ghost" :disabled="busy || !destination.enabled" @click="act(() => testDestination(destination.id), $t('notifications.testQueued'))">{{ $t('notifications.test') }}</AppButton>
            <AppButton variant="ghost" :disabled="busy" @click="edit(destination)">{{ $t('notifications.edit') }}</AppButton>
            <AppButton variant="ghost" :disabled="busy" @click="removing = destination">{{ $t('notifications.delete') }}</AppButton>
          </div>
        </li>
      </ul>
    </Panel>

    <Panel :title="$t('notifications.deliveryTitle')">
      <template #action><AppButton variant="ghost" :loading="loading" @click="load">{{ $t('activity.refresh') }}</AppButton></template>
      <p class="notifications__hint">{{ $t('notifications.deliveryHelp') }}</p>
      <p v-if="!deliveries.length" class="notifications__empty">{{ $t('notifications.noDeliveries') }}</p>
      <div v-else class="notifications__table-wrap">
        <table>
          <caption class="notifications__sr">{{ $t('notifications.deliveryTitle') }}</caption>
          <thead><tr><th>{{ $t('notifications.name') }}</th><th>{{ $t('notifications.state') }}</th><th>{{ $t('notifications.attempts') }}</th><th>{{ $t('notifications.lastDetail') }}</th></tr></thead>
          <tbody>
            <tr v-for="delivery in deliveries" :key="delivery.id">
              <td :data-label="$t('notifications.name')"><span>{{ delivery.name || $t('notifications.deleted') }}</span><small>{{ formatTimestamp(delivery.created * 1000) }}</small></td>
              <td :data-label="$t('notifications.state')" :class="{ 'notifications__error': delivery.state === 'failed' }">{{ $t(`notifications.states.${delivery.state}`) }}</td>
              <td :data-label="$t('notifications.attempts')">{{ delivery.attempts }} / 5</td>
              <td :data-label="$t('notifications.lastDetail')"><span v-if="delivery.error">{{ delivery.error }}</span><span v-else>{{ $t('notifications.noError') }}</span>
                <small v-if="delivery.state === 'pending'">{{ $t('notifications.nextAttempt', { time: formatTimestamp(delivery.next_at * 1000) }) }}</small>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </Panel>
    <Panel :title="$t('notifications.recentAlerts')">
      <p v-if="!alerts.length" class="notifications__hint">{{ $t('notifications.noAlerts') }}</p>
      <ul v-else class="notifications__list">
        <li v-for="event in alerts" :key="event.id" class="notifications__destination">
          <div><strong>{{ eventLabel(event.action) }}</strong>
            <p :class="{ 'notifications__error': ['failed', 'interrupted'].includes(event.outcome) }">{{ $t(`activity.outcomes.${event.outcome}`) }}</p>
            <p class="notifications__hint">{{ formatTimestamp(event.at * 1000) }}<span v-if="event.server_id"> · {{ event.server_id }}</span></p>
          </div>
          <router-link v-if="eventTarget(event, auth)" :to="eventTarget(event, auth)">{{ $t('activity.openView') }}</router-link>
        </li>
      </ul>
    </Panel>
    <p class="notifications__hint">{{ $t('notifications.diskHelp') }}</p>
    <ConfirmModal :show="Boolean(removing)" type="danger" :title="$t('notifications.delete')" :message="$t('notifications.deleteConfirm', { name: removing?.name || '' })" :loading="busy" @confirm="remove" @cancel="removing = null" />
  </div>
</template>

<style scoped>
.notifications { display: grid; gap: var(--space-5); min-width: 0; }
.notifications__intro, .notifications__hint { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.6; max-width: 75ch; }
.notifications__error { color: var(--danger); font-size: var(--text-sm); }
.notifications__notice { color: var(--success); font-size: var(--text-sm); }
.notifications__form { display: grid; gap: var(--space-4); max-width: 620px; padding-bottom: var(--space-5); }
.notifications__form label:not(.notifications__check) { display: grid; gap: var(--space-2); font-size: var(--text-sm); }
.notifications__form input:not([type=checkbox]), .notifications__form select { width: 100%; }
.notifications__form fieldset { display: flex; flex-wrap: wrap; gap: var(--space-3) var(--space-5); border: 0; }
.notifications__form legend { padding-bottom: var(--space-3); font-size: var(--text-sm); }
.notifications__check { display: flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm); }
.notifications__actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.notifications__list { list-style: none; }
.notifications__destination { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: var(--space-4); padding: var(--space-4) 0; border-bottom: 1px solid var(--border-color); font-size: var(--text-sm); overflow-wrap: anywhere; }
.notifications__destination:last-child { border-bottom: 0; padding-bottom: 0; }
.notifications__destination strong { font-weight: 500; color: var(--text-primary); }
.notifications__destination a { color: var(--text-primary); text-underline-offset: 3px; }
.notifications__destination a:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.notifications__table-wrap { overflow-x: auto; margin-top: var(--space-4); }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: var(--text-sm); }
th, td { padding: var(--space-3); border-bottom: 1px solid var(--border-color); vertical-align: top; }
th { font-weight: 500; color: var(--text-muted); }
td { overflow-wrap: anywhere; min-width: 100px; }
td small { display: block; color: var(--text-muted); line-height: 1.6; margin-top: var(--space-1); }
.notifications__empty { padding: var(--space-5) 0; color: var(--text-muted); font-size: var(--text-sm); }
.notifications__sr { position: absolute; width: 1px; height: 1px; clip-path: inset(50%); overflow: hidden; white-space: nowrap; }
@media (max-width: 768px) {
  thead { display: none; }
  tbody, tr { display: block; }
  tr { padding: var(--space-3) 0; border-bottom: 1px solid var(--border-color); }
  td { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: var(--space-2); min-width: 0; border: 0; padding: var(--space-2) 0; }
  td::before { content: attr(data-label); color: var(--text-muted); }
  td small { grid-column: 2; margin-top: 0; }
}
</style>
