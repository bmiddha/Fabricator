<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import BaseModal from './BaseModal.vue'
import AppButton from '../ui/AppButton.vue'
import FormField from '../ui/FormField.vue'
import { get, post, del } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { useServerStore } from '../../stores/server'
import { formatFileSize } from '../../utils/format'
import { createProgressPoller, lostContactMessage, POLL_INTERVAL_MS } from '../../utils/pollProgress'
import { t } from '../../i18n'

const props = defineProps({ show: Boolean })
const emit = defineEmits(['close', 'recovered'])
const auth = useAuthStore()
const store = useServerStore()
const connection = reactive({ type: 's3', profile: 'default', bucket: '', prefix: '', region: '', endpointUrl: '' })
const legacy = reactive({ loader: 'vanilla', version: '', loaderVersion: '', target: 'server.jar', type: 'jar', memory: 4, memoryMin: 1, jvmArgs: '' })
const snapshots = ref(null)
const cursor = ref(null)
const selected = ref('')
const name = ref('')
const manual = ref(false)
const preview = ref(null)
const result = ref(null)
const error = ref('')
const working = ref(false)
const job = ref(null)
const acknowledged = ref(false)
const retainSecrets = ref(false)
const confirmName = ref('')
const uncertain = ref(false)
const loaders = ['vanilla', 'fabric', 'quilt', 'forge', 'neoforge', 'paper', 'purpur', 'pufferfish', 'folia']
const savedKey = 'fabricator-s3-recovery'
let timer
let generation = 0
const allowed = computed(() => !auth.managed && auth.can('servers.import') && auth.can('servers.create'))
const active = computed(() => job.value && !['done', 'failed', 'unknown'].includes(job.value.phase))
const busy = computed(() => working.value || active.value)
const selectedSnapshot = computed(() => snapshots.value?.find(s => s.objectKey === selected.value))
const useLegacy = computed(() => manual.value || selectedSnapshot.value?.legacy)
const ready = computed(() => selected.value && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(name.value) &&
  (!useLegacy.value || (legacy.version && legacy.target &&
    (!['fabric', 'quilt', 'forge', 'neoforge'].includes(legacy.loader) || legacy.loaderVersion))))
const secretReview = computed(() => preview.value?.secrets.suspectedCount || preview.value?.secrets.scanTruncated)
const confirmed = computed(() => acknowledged.value && confirmName.value === preview.value?.name &&
  (!secretReview.value || retainSecrets.value) && !uncertain.value)
const phase = computed(() => t(`s3Recovery.phases.${job.value?.phase || 'queued'}`))
const byteProgress = computed(() => ['copying', 'verifying', 'registering'].includes(job.value?.phase)
  ? job.value?.copied_bytes || 0 : job.value?.transferred_bytes || 0)

function saved(value) {
  sessionStorage.setItem(savedKey, JSON.stringify(value))
}
function stopPolling() {
  clearTimeout(timer)
  timer = undefined
}
async function poll(id, kind) {
  stopPolling()
  const current = generation
  const tick = createProgressPoller(() => get(`/api/s3-recovery/jobs/${encodeURIComponent(id)}`))
  const run = async () => {
    const response = await tick()
    if (current !== generation || !props.show) return
    if (response.status === 'giveup') {
      job.value = { phase: 'unknown' }
      error.value = `${lostContactMessage()} ${t('s3Recovery.restartHint')}`
      return
    }
    if (response.status === 'ok') {
      job.value = response.progress
      if (job.value.phase === 'failed') {
        error.value = job.value.error
        if (job.value.published) {
          result.value = job.value.result
          sessionStorage.removeItem(savedKey)
          return
        }
        if (preview.value) saved({ previewId: preview.value.previewId })
        else sessionStorage.removeItem(savedKey)
        return
      }
      if (job.value.phase === 'done') {
        if (kind === 's3-preview') {
          preview.value = job.value.result
          saved({ previewId: preview.value.previewId })
        } else {
          result.value = job.value.result
          sessionStorage.removeItem(savedKey)
          const loaded = await store.loadServers()
          if (!loaded.ok) error.value = loaded.error?.message || t('servers.loadFailed')
        }
        return
      }
    }
    timer = setTimeout(run, POLL_INTERVAL_MS)
  }
  await run()
}
async function discover(more = false) {
  working.value = true
  error.value = ''
  try {
    const response = await post('/api/s3-recovery/discover', { destination: connection, cursor: more ? cursor.value : null })
    snapshots.value = more ? [...snapshots.value, ...response.snapshots] : response.snapshots
    cursor.value = response.cursor
    if (!more) selected.value = ''
  } catch (e) {
    error.value = e.message
  } finally {
    working.value = false
  }
}
async function inspect() {
  working.value = true
  error.value = ''
  try {
    const data = { destination: connection, objectKey: selected.value, name: name.value }
    if (useLegacy.value) data.legacy = {
      loader: legacy.loader, version: legacy.version, loaderVersion: legacy.loaderVersion,
      memory: legacy.memory, memoryMin: legacy.memoryMin, memoryUnit: 'GB', jvmArgs: legacy.jvmArgs,
      launch: { type: legacy.type, [legacy.type === 'jar' ? 'jar' : 'args_file']: legacy.target,
        jvm_args: [], program_args: ['nogui'] },
    }
    const response = await post('/api/s3-recovery/previews', data)
    job.value = { phase: 'queued' }
    saved({ jobId: response.jobId, kind: 's3-preview' })
    await poll(response.jobId, 's3-preview')
  } catch (e) {
    error.value = e.message
  } finally {
    working.value = false
  }
}
async function recover() {
  working.value = true
  error.value = ''
  try {
    const response = await post('/api/s3-recovery/imports', {
      previewId: preview.value.previewId, confirmName: confirmName.value,
      acknowledgeRisks: acknowledged.value, retainEmbeddedSecrets: retainSecrets.value,
    })
    job.value = { phase: 'queued' }
    saved({ jobId: response.jobId, kind: 's3-import', previewId: preview.value.previewId })
    await poll(response.jobId, 's3-import')
  } catch (e) {
    if (!e.status || e.status >= 500) {
      uncertain.value = true
      job.value = { phase: 'unknown' }
      saved({ previewId: preview.value.previewId, uncertain: true })
      error.value = `${e.message} ${t('s3Recovery.restartHint')}`
    } else {
      error.value = e.message
    }
  } finally {
    working.value = false
  }
}
async function reset() {
  working.value = true
  error.value = ''
  try {
    if (preview.value) {
      try {
        await del(`/api/s3-recovery/previews/${preview.value.previewId}`)
      } catch (e) {
        if (e.status !== 404) throw e
        error.value = `${e.message} ${t('s3Recovery.restartHint')}`
      }
    }
    preview.value = null
    result.value = null
    job.value = null
    acknowledged.value = false
    retainSecrets.value = false
    confirmName.value = ''
    uncertain.value = false
    sessionStorage.removeItem(savedKey)
  } catch (e) {
    error.value = e.message
  } finally {
    working.value = false
  }
}
watch(() => props.show, async show => {
  generation++
  stopPolling()
  if (!show || !allowed.value) return
  error.value = ''
  acknowledged.value = false
  retainSecrets.value = false
  confirmName.value = ''
  const value = sessionStorage.getItem(savedKey)
  if (!value) return
  working.value = true
  try {
    const stored = JSON.parse(value)
    uncertain.value = stored.uncertain === true
    if (stored.previewId) preview.value = await get(`/api/s3-recovery/previews/${stored.previewId}`)
    if (stored.jobId) await poll(stored.jobId, stored.kind)
  } catch (e) {
    error.value = `${e.message} ${t('s3Recovery.restartHint')}`
    sessionStorage.removeItem(savedKey)
    job.value = { phase: 'unknown' }
  } finally {
    working.value = false
  }
}, { immediate: true })
function openInstance() {
  emit('recovered', { id: result.value.serverId, name: result.value.name })
  emit('close')
}
onBeforeUnmount(stopPolling)
</script>

<template>
  <BaseModal :show="show && allowed" :title="$t('s3Recovery.title')" size="large" :close-on-escape="!busy" @close="emit('close')">
    <p class="recovery-muted">{{ $t('s3Recovery.intro') }}</p>
    <p v-if="error" class="recovery-error" role="alert">{{ error }}</p>

    <div v-if="busy" class="recovery-status" role="status" aria-live="polite">
      <p>{{ phase }}</p>
      <template v-if="job?.total_bytes">
        <progress :value="byteProgress" :max="job.total_bytes" :aria-label="phase"></progress>
        <p>{{ formatFileSize(byteProgress) }} / {{ formatFileSize(job.total_bytes) }}</p>
      </template>
      <p class="recovery-muted">{{ $t('s3Recovery.stagingHint') }}</p>
    </div>
    <div v-else-if="result" class="recovery-result">
      <h3>{{ $t('s3Recovery.done', { name: result.name }) }}</h3>
      <p>{{ $t('s3Recovery.stopped') }}</p>
      <code>{{ result.serverId }}</code>
      <p v-if="!auth.can('server.view', result.serverId)" class="recovery-muted">{{ $t('s3Recovery.grants') }}</p>
    </div>
    <div v-else-if="preview" class="recovery-preview">
      <h3>{{ preview.name }}</h3>
      <dl class="recovery-facts">
        <dt>{{ $t('createServer.minecraftVersion') }}</dt><dd>{{ preview.version }} / {{ preview.loader }} {{ preview.loaderVersion }}</dd>
        <dt>{{ $t('s3Recovery.source') }}</dt><dd>{{ preview.sourceServerId }} / {{ preview.snapshotId }}</dd>
        <dt>{{ $t('s3Recovery.size') }}</dt><dd>{{ formatFileSize(preview.archiveBytes) }} / {{ formatFileSize(preview.expandedBytes) }}</dd>
        <dt>{{ $t('s3Recovery.worlds') }}</dt><dd>{{ preview.worlds.join(', ') || $t('s3Recovery.noWorlds') }}</dd>
        <dt>{{ $t('s3Recovery.java') }}</dt><dd>{{ preview.java.required_java || $t('s3Recovery.javaUnknown') }}</dd>
        <dt>SHA-256</dt><dd><code>{{ preview.sha256 }}</code></dd>
      </dl>
      <p class="recovery-muted">{{ $t('s3Recovery.removed', { count: preview.removedAdministrativeFiles }) }}</p>
      <details open>
        <summary>{{ $t('s3Recovery.risks') }}</summary>
        <ul><li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li></ul>
      </details>
      <details v-if="secretReview" open>
        <summary>{{ $t('s3Recovery.secrets', { count: preview.secrets.suspectedCount }) }}</summary>
        <p class="recovery-muted">{{ $t('s3Recovery.secretHint') }}</p>
        <p v-if="preview.secrets.scanTruncated">{{ $t('s3Recovery.scanTruncated') }}</p>
        <ul><li v-for="path in preview.secrets.suspectedFiles" :key="path"><code>{{ path }}</code></li></ul>
      </details>
      <label class="recovery-check"><input v-model="acknowledged" type="checkbox"><span>{{ $t('s3Recovery.acknowledge') }}</span></label>
      <label v-if="secretReview" class="recovery-check"><input v-model="retainSecrets" type="checkbox"><span>{{ $t('s3Recovery.retainSecrets') }}</span></label>
      <FormField :label="$t('s3Recovery.confirmName', { name: preview.name })">
        <template #default="{ id }"><input :id="id" v-model="confirmName" type="text" autocomplete="off"></template>
      </FormField>
    </div>
    <form v-else class="recovery-form" @submit.prevent="discover()">
      <fieldset :disabled="busy">
        <legend>{{ $t('s3Recovery.connection') }}</legend>
        <div class="recovery-row">
          <FormField :label="$t('s3Recovery.profile')" :hint="$t('s3Recovery.profileHint')">
            <template #default="{ id, describedBy }"><input :id="id" v-model="connection.profile" :aria-describedby="describedBy" type="text" required @input="snapshots = null; selected = ''"></template>
          </FormField>
          <FormField :label="$t('s3Recovery.bucket')">
            <template #default="{ id }"><input :id="id" v-model="connection.bucket" type="text" required @input="snapshots = null; selected = ''"></template>
          </FormField>
        </div>
        <FormField :label="$t('s3Recovery.prefix')">
          <template #default="{ id }"><input :id="id" v-model="connection.prefix" type="text" @input="snapshots = null; selected = ''"></template>
        </FormField>
        <details>
          <summary>{{ $t('s3Recovery.advanced') }}</summary>
          <div class="recovery-row">
            <FormField :label="$t('s3Recovery.region')"><template #default="{ id }"><input :id="id" v-model="connection.region" type="text" @input="snapshots = null; selected = ''"></template></FormField>
            <FormField :label="$t('s3Recovery.endpoint')"><template #default="{ id }"><input :id="id" v-model="connection.endpointUrl" type="url" @input="snapshots = null; selected = ''"></template></FormField>
          </div>
        </details>
        <AppButton type="submit" variant="primary" size="md" :disabled="!connection.bucket">{{ $t('s3Recovery.discover') }}</AppButton>
      </fieldset>
      <fieldset v-if="snapshots">
        <legend>{{ $t('s3Recovery.snapshots') }}</legend>
        <p v-if="!snapshots.length" class="recovery-muted">{{ $t('s3Recovery.empty') }}</p>
        <label v-for="snapshot in snapshots" :key="snapshot.objectKey" class="recovery-snapshot">
          <input v-model="selected" type="radio" :value="snapshot.objectKey">
          <span><strong>{{ snapshot.sourceServerId }} / {{ snapshot.snapshotId }}</strong><br>
            {{ snapshot.createdAt }} &middot; {{ formatFileSize(snapshot.sizeBytes) }}<br>
            {{ snapshot.legacy ? $t('s3Recovery.legacy') : `${snapshot.version} / ${snapshot.loader}` }}
          </span>
        </label>
        <AppButton v-if="cursor" variant="ghost" size="md" @click="discover(true)">{{ $t('s3Recovery.more') }}</AppButton>
      </fieldset>
      <template v-if="selected">
        <FormField :label="$t('s3Recovery.name')" :hint="$t('s3Recovery.nameHint')">
          <template #default="{ id, describedBy }"><input :id="id" v-model="name" type="text" :aria-describedby="describedBy" required></template>
        </FormField>
        <label class="recovery-check"><input :checked="useLegacy" type="checkbox" :disabled="selectedSnapshot?.legacy" @change="manual = $event.target.checked"><span>{{ $t('s3Recovery.manual') }}</span></label>
        <fieldset v-if="useLegacy">
          <legend>{{ $t('s3Recovery.legacyInputs') }}</legend>
          <p class="recovery-muted">{{ $t('s3Recovery.legacyHint') }}</p>
          <div class="recovery-row">
            <FormField :label="$t('properties.identity.modLoader')"><template #default="{ id }"><select :id="id" v-model="legacy.loader"><option v-for="loader in loaders" :key="loader">{{ loader }}</option></select></template></FormField>
            <FormField :label="$t('createServer.minecraftVersion')"><template #default="{ id }"><input :id="id" v-model="legacy.version" type="text" required></template></FormField>
            <FormField :label="$t('s3Recovery.loaderBuild')"><template #default="{ id }"><input :id="id" v-model="legacy.loaderVersion" type="text"></template></FormField>
          </div>
          <div class="recovery-row">
            <FormField :label="$t('s3Recovery.launchType')"><template #default="{ id }"><select :id="id" v-model="legacy.type"><option value="jar">jar</option><option value="args_file">args_file</option></select></template></FormField>
            <FormField :label="$t('s3Recovery.launchTarget')"><template #default="{ id }"><input :id="id" v-model="legacy.target" type="text" required></template></FormField>
          </div>
          <div class="recovery-row">
            <FormField :label="$t('s3Recovery.memory')"><template #default="{ id }"><input :id="id" v-model.number="legacy.memory" type="number" min="1" max="1024"></template></FormField>
            <FormField :label="$t('s3Recovery.memoryMin')"><template #default="{ id }"><input :id="id" v-model.number="legacy.memoryMin" type="number" min="1" :max="legacy.memory"></template></FormField>
          </div>
          <FormField :label="$t('s3Recovery.jvmFlags')"><template #default="{ id }"><input :id="id" v-model="legacy.jvmArgs" type="text"></template></FormField>
        </fieldset>
      </template>
    </form>
    <template #footer>
      <AppButton variant="ghost" size="md" @click="emit('close')">{{ busy ? $t('s3Recovery.background') : $t('common.close') }}</AppButton>
      <template v-if="!busy && result">
        <AppButton v-if="auth.can('server.view', result.serverId)" variant="primary" size="md" @click="openInstance">{{ $t('importServer.open') }}</AppButton>
      </template>
      <template v-else-if="!busy && preview">
        <AppButton variant="ghost" size="md" @click="reset">{{ $t('s3Recovery.discard') }}</AppButton>
        <AppButton variant="primary" size="md" :disabled="!confirmed" @click="recover">{{ $t('s3Recovery.confirm') }}</AppButton>
      </template>
      <AppButton v-else-if="!busy && !result" variant="primary" size="md" :disabled="!ready" @click="inspect">{{ $t('s3Recovery.inspect') }}</AppButton>
    </template>
  </BaseModal>
</template>

<style scoped>
.recovery-form, .recovery-preview, .recovery-result { display: flex; flex-direction: column; gap: var(--space-4); }
.recovery-muted { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.6; }
.recovery-error { color: var(--danger); overflow-wrap: anywhere; }
.recovery-row { display: flex; flex-wrap: wrap; gap: var(--space-3); }
.recovery-row > * { flex: 1 1 180px; min-width: 0; }
fieldset { border: 1px solid var(--border-color); padding: var(--space-4); margin: 0; min-width: 0; display: flex; flex-direction: column; gap: var(--space-3); }
legend { font-weight: 600; padding: 0 var(--space-2); }
.recovery-check, .recovery-snapshot { display: flex; align-items: flex-start; gap: var(--space-3); line-height: 1.6; }
.recovery-check input, .recovery-snapshot input { flex-shrink: 0; margin-top: 5px; }
.recovery-snapshot { padding-block: var(--space-2); border-bottom: 1px solid var(--border-color); overflow-wrap: anywhere; }
.recovery-facts { display: grid; grid-template-columns: minmax(100px, 1fr) minmax(0, 2fr); gap: var(--space-2) var(--space-4); margin: 0; }
.recovery-facts dt { color: var(--text-muted); }
.recovery-facts dd { margin: 0; overflow-wrap: anywhere; }
code { overflow-wrap: anywhere; }
details { line-height: 1.6; }
summary { cursor: pointer; font-weight: 600; }
ul { padding-left: var(--space-5); }
li + li { margin-top: var(--space-2); }
progress { width: 100%; accent-color: var(--primary); }
@media (max-width: 480px) { .recovery-facts { grid-template-columns: 1fr; } .recovery-facts dd { margin-bottom: var(--space-2); } }
</style>
