<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import BaseModal from './modals/BaseModal.vue'
import AppButton from './ui/AppButton.vue'
import { useAuthStore } from '../stores/auth'
import { useServerStore } from '../stores/server'
import { get, post } from '../api/client'
import { createProgressPoller, lostContactMessage, POLL_INTERVAL_MS } from '../utils/pollProgress'
import { formatFileSize } from '../utils/format'
import { t } from '../i18n'

const auth = useAuthStore()
const store = useServerStore()
const kind = ref('')
const error = ref('')
const working = ref(false)
const job = ref(null)
const preview = ref(null)
const groups = ref([])
const acknowledged = ref(false)
const replacement = ref('')
const form = reactive({
  sourceId: '', destinationId: '', worldId: '', name: '',
  includeWorlds: '', includePlayerData: '', restart: '',
})
let timer
let generation = 0
const permittedFork = (id) => !auth.managed && auth.can('servers.create') &&
  ['server.fork', 'server.settings.read', 'files.read'].every(p => auth.can(p, id))
const canFork = computed(() => permittedFork(store.currentServerId))
const canCopy = computed(() => !auth.managed && auth.can('worlds.read', store.currentServerId))
const sources = computed(() => store.serversList.filter(s => s.loader !== 'velocity' &&
  (kind.value === 'fork' ? permittedFork(s.id) : auth.can('worlds.read', s.id))))
const destinations = computed(() => store.serversList.filter(s => s.id !== form.sourceId &&
  s.loader !== 'velocity' && ['worlds.import', 'backups.create'].every(p => auth.can(p, s.id))))
const active = computed(() => job.value && !['done', 'failed', 'unknown'].includes(job.value.phase))
const busy = computed(() => working.value || active.value)
const ready = computed(() => form.sourceId && form.includePlayerData !== '' && (
  kind.value === 'fork'
    ? /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(form.name) && form.includeWorlds !== ''
    : form.worldId && form.destinationId && form.restart !== ''
))
const confirmed = computed(() => acknowledged.value &&
  (!preview.value?.replaces || replacement.value === preview.value.destinationWorld))
const payload = () => ({
  ...form, includeWorlds: form.includeWorlds === 'yes', includePlayerData: form.includePlayerData === 'yes',
  restart: form.restart === 'yes',
})
const jobKey = () => `fabricator-transfer:${store.currentServerId}`
const phaseLabel = computed(() => t(`transfers.phases.${job.value?.phase || 'queued'}`))

function stopPolling() {
  clearTimeout(timer)
  timer = undefined
}
async function pollJob(id) {
  stopPolling()
  const poll = createProgressPoller(() => get(`/api/transfer-jobs/${encodeURIComponent(id)}`))
  const current = generation
  const tick = async () => {
    const result = await poll()
    if (current !== generation) return
    if (result.status === 'giveup') {
      error.value = lostContactMessage()
      job.value = { phase: 'unknown' }
      return
    }
    if (result.status === 'ok') {
      job.value = result.progress
      if (job.value.phase === 'failed') {
        error.value = job.value.error
        sessionStorage.removeItem(jobKey())
        return
      }
      if (job.value.phase === 'done') {
        sessionStorage.removeItem(jobKey())
        const loaded = await store.loadServers()
        if (!loaded.ok) error.value = loaded.error?.message || t('servers.loadFailed')
        return
      }
    }
    timer = setTimeout(tick, POLL_INTERVAL_MS)
  }
  await tick()
}

async function loadWorlds() {
  if (kind.value !== 'world-copy' || !form.sourceId) return
  const source = form.sourceId
  const current = generation
  groups.value = []
  working.value = true
  try {
    const result = await get(`/api/servers/${encodeURIComponent(source)}/worlds`)
    if (current !== generation || source !== form.sourceId) return
    groups.value = result.worlds
    if (!groups.value.length) error.value = t('transfers.noWorlds')
  } catch (e) {
    if (current === generation) error.value = e.message
  } finally {
    if (current === generation) working.value = false
  }
}

async function open(mode) {
  generation++
  kind.value = ''
  error.value = ''
  preview.value = null
  job.value = null
  acknowledged.value = false
  replacement.value = ''
  Object.assign(form, {
    sourceId: store.currentServerId, destinationId: '', worldId: '', name: '',
    includeWorlds: '', includePlayerData: '', restart: '',
  })
  kind.value = mode
  const saved = sessionStorage.getItem(jobKey())
  if (saved) {
    await pollJob(saved)
    if (active.value || job.value?.phase === 'done') return
  }
  await loadWorlds()
}

function close() {
  if (busy.value) return
  generation++
  stopPolling()
  kind.value = ''
}

watch(() => ({ ...form }), (next, old) => {
  preview.value = null
  acknowledged.value = false
  replacement.value = ''
  error.value = ''
  if (next.sourceId !== old.sourceId && kind.value === 'world-copy' && !active.value) {
    form.worldId = ''
    loadWorlds()
  }
}, { flush: 'sync' })

async function review() {
  working.value = true
  error.value = ''
  const current = generation
  const selected = JSON.stringify(payload())
  try {
    const result = await post(`/api/servers/${encodeURIComponent(form.sourceId)}/transfers/${kind.value}/preview`, payload())
    if (current === generation && selected === JSON.stringify(payload())) preview.value = result
  } catch (e) {
    if (current === generation) error.value = e.message
  } finally {
    if (current === generation) working.value = false
  }
}

async function submit() {
  if (!preview.value || !confirmed.value || busy.value) return
  working.value = true
  error.value = ''
  try {
    const result = await post(`/api/servers/${encodeURIComponent(form.sourceId)}/transfers/${kind.value}`, {
      ...payload(), previewToken: preview.value.previewToken, acknowledgeRisks: acknowledged.value,
      confirmReplace: replacement.value,
    })
    sessionStorage.setItem(jobKey(), result.jobId)
    job.value = { phase: 'queued' }
    await pollJob(result.jobId)
  } catch (e) {
    error.value = e.message
    if (!e.status || e.status >= 500) job.value = { phase: 'unknown' }
  } finally {
    working.value = false
  }
}
onBeforeUnmount(() => { generation++; stopPolling() })
</script>

<template>
  <section v-if="!auth.managed && store.server?.loader !== 'velocity'" class="transfers">
    <div>
      <h2>{{ $t('transfers.title') }}</h2>
      <p>{{ $t('transfers.subtitle') }}</p>
    </div>
    <div class="transfers__actions">
      <AppButton v-if="canFork" variant="ghost" @click="open('fork')">{{ $t('transfers.fork') }}</AppButton>
      <AppButton v-if="canCopy" variant="ghost" @click="open('world-copy')">{{ $t('transfers.copy') }}</AppButton>
      <p v-if="!canFork && !canCopy">{{ $t('transfers.readOnly') }}</p>
    </div>
    <BaseModal :show="Boolean(kind)" :title="$t(kind === 'fork' ? 'transfers.fork' : 'transfers.copy')"
      :close-on-escape="!busy" @close="close">
      <div class="transfer-form" role="group" :aria-label="$t('transfers.title')">
        <p class="transfer-form__notice">{{ $t('transfers.stoppedOnly') }}</p>
        <fieldset v-if="!job" :disabled="busy" class="transfer-form__fields">
          <label for="transfer-source">{{ $t('transfers.source') }}</label>
          <select id="transfer-source" v-model="form.sourceId">
            <option v-for="s in sources" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
          <template v-if="kind === 'fork'">
            <label for="transfer-name">{{ $t('transfers.name') }}</label>
            <input id="transfer-name" v-model.trim="form.name" maxlength="80" autocomplete="off"
              :placeholder="$t('transfers.nameExample')" />
            <p>{{ $t('transfers.nameHelp') }}</p>
            <label for="transfer-worlds">{{ $t('transfers.worlds') }}</label>
            <select id="transfer-worlds" v-model="form.includeWorlds">
              <option value="" disabled>{{ $t('transfers.choose') }}</option>
              <option value="yes">{{ $t('transfers.includeWorlds') }}</option>
              <option value="no">{{ $t('transfers.newWorld') }}</option>
            </select>
          </template>
          <template v-else>
            <label for="transfer-world">{{ $t('transfers.sourceWorld') }}</label>
            <select id="transfer-world" v-model="form.worldId">
              <option value="" disabled>{{ $t('transfers.choose') }}</option>
              <option v-for="world in groups" :key="world.id" :value="world.id">{{ world.name }}</option>
            </select>
            <label for="transfer-destination">{{ $t('transfers.destination') }}</label>
            <select id="transfer-destination" v-model="form.destinationId">
              <option value="" disabled>{{ $t('transfers.choose') }}</option>
              <option v-for="s in destinations" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
            <p v-if="!destinations.length">{{ $t('transfers.noDestinations') }}</p>
            <label for="transfer-restart">{{ $t('transfers.afterCopy') }}</label>
            <select id="transfer-restart" v-model="form.restart">
              <option value="" disabled>{{ $t('transfers.choose') }}</option>
              <option value="no">{{ $t('transfers.leaveStopped') }}</option>
              <option v-if="auth.can('server.start', form.destinationId)" value="yes">{{ $t('transfers.startDestination') }}</option>
            </select>
          </template>
          <label for="transfer-players">{{ $t('transfers.players') }}</label>
          <select id="transfer-players" v-model="form.includePlayerData">
            <option value="" disabled>{{ $t('transfers.choose') }}</option>
            <option value="no">{{ $t('transfers.freshPlayers') }}</option>
            <option value="yes">{{ $t('transfers.keepPlayers') }}</option>
          </select>
          <p>{{ $t('transfers.playersHelp') }}</p>
          <p v-if="kind === 'fork'">{{ $t('transfers.freshAccess') }}</p>
          <p v-else>{{ $t('transfers.keepAccess') }}</p>
        </fieldset>

        <section v-if="preview && !job" class="transfer-form__review" :aria-label="$t('transfers.review')">
          <h3>{{ $t('transfers.review') }}</h3>
          <p>{{ $t('transfers.size', { size: formatFileSize(preview.bytes) }) }}</p>
          <p v-if="kind === 'fork'">{{ $t('transfers.forkScope') }}</p>
          <p v-else>{{ $t('transfers.copyScope', { name: preview.destinationWorld }) }}</p>
          <dl v-for="world in preview.worlds" :key="world.id">
            <dt>{{ world.name }} · {{ world.version }} · DataVersion {{ world.dataVersion }}</dt>
            <dd>{{ world.layout }} · {{ world.directories.join(', ') }}</dd>
            <dd v-if="world.dimensions.length">{{ world.dimensions.join(', ') }}</dd>
          </dl>
          <ul><li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li></ul>
          <label v-if="preview.replaces" for="transfer-replacement">
            {{ $t('transfers.replaceConfirm', { name: preview.destinationWorld }) }}
          </label>
          <input v-if="preview.replaces" id="transfer-replacement" v-model="replacement" :disabled="busy" autocomplete="off" />
          <label class="transfer-form__check">
            <input v-model="acknowledged" type="checkbox" :disabled="busy" />
            <span>{{ $t('transfers.acknowledge') }}</span>
          </label>
        </section>
        <div v-if="job" role="status" aria-live="polite" class="transfer-form__progress">
          <strong>{{ phaseLabel }}</strong>
          <progress v-if="active" :value="job.phase === 'copying' ? job.copied_bytes : undefined"
            :max="job.total_bytes || 1" :aria-label="phaseLabel"></progress>
          <p v-if="job.phase === 'copying'">{{ formatFileSize(job.copied_bytes || 0) }} / {{ formatFileSize(job.total_bytes || 0) }}</p>
          <p v-if="job.phase === 'done'">{{ $t('transfers.success') }}</p>
          <p v-if="job.result?.name">{{ job.result.name }} · {{ $t('transfers.gamePort') }} {{ job.result.port }}</p>
          <p v-if="job.result?.safetySnapshotId">{{ $t('transfers.safetyId') }} {{ job.result.safetySnapshotId }}</p>
          <p v-if="job.published && job.phase === 'failed'">{{ $t('transfers.publishedFailure') }}</p>
        </div>
        <p v-if="error" role="alert" class="transfer-form__error">{{ error }}</p>
      </div>
      <template #footer>
        <AppButton variant="ghost" :disabled="busy" @click="close">{{ $t('common.close') }}</AppButton>
        <AppButton v-if="!job && !preview" :disabled="!ready || busy" :loading="working" @click="review">{{ $t('transfers.review') }}</AppButton>
        <AppButton v-else-if="!job" :disabled="!confirmed || busy" :loading="working" @click="submit">
          {{ $t(kind === 'fork' ? 'transfers.fork' : preview.replaces ? 'transfers.replace' : 'transfers.copy') }}
        </AppButton>
      </template>
    </BaseModal>
  </section>
</template>

<style scoped>
.transfers { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-4); padding-block: var(--space-4); border-block: 1px solid var(--border-color); }
.transfers h2, .transfer-form h3 { font-size: var(--text-md); color: var(--text-primary); }
.transfers p, .transfer-form p, .transfer-form li, .transfer-form dd { font-size: var(--text-sm); line-height: var(--leading-normal); color: var(--text-muted); overflow-wrap: anywhere; }
.transfers__actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.transfer-form { display: grid; gap: var(--space-4); }
.transfer-form__fields { display: grid; gap: var(--space-2); border: 0; min-width: 0; }
.transfer-form label, .transfer-form dt { color: var(--text-secondary); font-size: var(--text-sm); }
.transfer-form input:not([type="checkbox"]), .transfer-form select { width: 100%; padding: var(--space-3); background: var(--bg-primary); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: var(--radius-sm); font: inherit; }
.transfer-form input:focus-visible, .transfer-form select:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.transfer-form__notice { padding: var(--space-3); background: var(--bg-tertiary); border-radius: var(--radius-sm); }
.transfer-form__review { display: grid; gap: var(--space-3); }
.transfer-form ul { padding-inline-start: var(--space-5); display: grid; gap: var(--space-2); }
.transfer-form__check { display: flex; align-items: start; gap: var(--space-2); }
.transfer-form__check input { margin-top: 3px; flex-shrink: 0; }
.transfer-form__progress { display: grid; gap: var(--space-2); }
.transfer-form progress { width: 100%; accent-color: var(--primary); }
.transfer-form .transfer-form__error { color: var(--danger); }
.transfer-form fieldset:disabled { opacity: 0.65; }
</style>
