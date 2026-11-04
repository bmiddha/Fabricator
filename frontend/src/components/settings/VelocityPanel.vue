<script setup>
import { computed, ref, watch } from 'vue'
import Panel from '../ui/Panel.vue'
import AppButton from '../ui/AppButton.vue'
import FormField from '../ui/FormField.vue'
import ToggleRow from '../ui/ToggleRow.vue'
import { useAuthStore } from '../../stores/auth'
import { useServerStore } from '../../stores/server'
import { useToast } from '../../composables/useToast'
import { getVelocity, saveVelocity, previewForwarding, applyForwarding } from '../../api/velocity'
import { updateServerSettings } from '../../api/servers'
import { t } from '../../i18n'

const auth = useAuthStore()
const store = useServerStore()
const toast = useToast()
const config = ref(null)
const runtime = ref({})
const defaults = ref('')
const hosts = ref([])
const busy = ref(false)
const error = ref('')
const preview = ref(null)
const consent = ref(false)
const selected = ref([])
const id = computed(() => store.currentServerId)
const canRead = computed(() => auth.can('server.settings.read', id.value))
const canWrite = computed(() => canRead.value && auth.can('server.settings.write', id.value))
const stopped = computed(() => !['running', 'starting', 'stopping'].includes(store.serverStatus.status))
const editable = computed(() => canWrite.value && stopped.value && !busy.value)
const backends = computed(() => store.serversList.filter(server =>
  server.id !== id.value && server.loader !== 'velocity' && auth.can('server.settings.read', server.id)
))
const canApply = computed(() => editable.value && selected.value.length > 0
  && selected.value.every(sid => auth.can('server.settings.write', sid)))

async function run(action) {
  const started = id.value
  const revision = auth.revision
  const current = () => started === id.value && revision === auth.revision
  busy.value = true
  error.value = ''
  try { await action(started, current) } catch (e) {
    if (current()) {
      error.value = e.message
      toast.error(e.message)
    }
  } finally { if (current()) busy.value = false }
}
async function load(serverId, current) {
  const value = await getVelocity(serverId)
  if (!current()) return
  config.value = value
  defaults.value = value.try.join('\n')
  hosts.value = Object.entries(value.forcedHosts).map(([hostname, targets]) => ({ hostname, targets: targets.join(', ') }))
  const server = store.server
  const factor = server?.memoryUnit === 'MB' ? 1 / 1024 : 1
  runtime.value = { javaPath: server?.javaPath || '', memory: (server?.memory || 1) * factor,
    memoryMin: (server?.memoryMin || server?.memory || 1) * factor, memoryUnit: 'GB' }
}
async function save(serverId, current) {
  await saveVelocity(serverId, {
    ...config.value,
    try: defaults.value.split('\n').map(s => s.trim()).filter(Boolean),
    forcedHosts: Object.fromEntries(hosts.value.map(h => [h.hostname, h.targets.split(',').map(s => s.trim()).filter(Boolean)]))
  })
  if (!current()) return
  preview.value = null
  await store.loadServer({ silent: true })
  if (!current()) return
  await load(serverId, current)
  if (current()) toast.success(t('velocity.saved'))
}
async function saveRuntime(serverId, current) {
  await updateServerSettings(serverId, runtime.value)
  if (!current()) return
  await store.loadServer({ silent: true })
  if (current()) toast.success(t('velocity.runtimeSaved'))
}
async function plan(serverId, current) {
  const result = await previewForwarding(serverId, { serverIds: selected.value })
  if (current()) { preview.value = result; consent.value = false }
}
async function apply(serverId, current) {
  await applyForwarding(serverId, {
    serverIds: selected.value, previewToken: preview.value.previewToken, confirmOfflineMode: consent.value
  })
  if (current()) { preview.value = null; consent.value = false; toast.success(t('velocity.applied')) }
}
watch([id, () => auth.revision], () => {
  config.value = null
  error.value = ''
  busy.value = false
  preview.value = null
  selected.value = []
  if (canRead.value) run(load)
}, { immediate: true })
watch(selected, () => { preview.value = null; consent.value = false }, { deep: true })
</script>

<template>
  <div class="velocity-panels" :aria-busy="busy">
    <p v-if="error" role="alert" class="velocity-error">{{ error }}</p>
    <p v-if="!canRead">{{ $t('access.settingsReadOnly') }}</p>
    <p v-else-if="!config && busy" role="status">{{ $t('common.loading') }}</p>
    <AppButton v-else-if="!config" @click="run(load)">{{ $t('common.retry') }}</AppButton>
    <template v-if="config">
      <p v-if="!editable">{{ $t(canWrite ? 'properties.guard' : 'access.settingsReadOnly') }}</p>
      <Panel :title="$t('velocity.title')">
        <p>{{ $t('velocity.intro') }}</p>
        <form @submit.prevent="editable && run(save)">
          <fieldset :disabled="!editable" class="velocity-fields">
            <FormField :label="$t('velocity.listen')" v-slot="{ id }"><input :id="id" v-model="config.listen.host" required></FormField>
            <FormField :label="$t('velocity.port')" v-slot="{ id }"><input :id="id" v-model.number="config.listen.port" type="number" min="1" max="65535" required></FormField>
            <ToggleRow v-model="config.onlineMode" :label="$t('createServer.onlineMode')" :disabled="!editable" />
            <ToggleRow :model-value="config.forwardingMode === 'modern'" :label="$t('velocity.forwarding')" :disabled="!editable" @update:model-value="config.forwardingMode = $event ? 'modern' : 'none'" />
            <h3>{{ $t('velocity.backends') }}</h3>
            <div v-for="(backend, i) in config.backends" :key="i" class="velocity-row">
              <FormField :label="$t('velocity.backendName')" v-slot="{ id }"><input :id="id" v-model="backend.name" required></FormField>
              <FormField :label="$t('velocity.server')" v-slot="{ id }">
                <select :id="id" v-model="backend.serverId" required>
                  <option v-for="server in backends" :key="server.id" :value="server.id">{{ server.name }}</option>
                </select>
              </FormField>
              <AppButton variant="ghost" @click="config.backends.splice(i, 1)">{{ $t('velocity.remove') }}</AppButton>
            </div>
            <AppButton variant="ghost" @click="config.backends.push({ name: '', serverId: '' })">{{ $t('velocity.addBackend') }}</AppButton>
            <FormField :label="$t('velocity.defaults')" :hint="$t('velocity.defaultsHint')" v-slot="{ id, describedBy }"><textarea :id="id" v-model="defaults" rows="3" :aria-describedby="describedBy" /></FormField>
            <h3>{{ $t('velocity.hosts') }}</h3>
            <p>{{ $t('velocity.hostHint') }}</p>
            <div v-for="(host, i) in hosts" :key="i" class="velocity-row">
              <FormField :label="$t('velocity.hostname')" v-slot="{ id }"><input :id="id" v-model="host.hostname" required></FormField>
              <FormField :label="$t('velocity.hostTargets')" v-slot="{ id }"><input :id="id" v-model="host.targets" required></FormField>
              <AppButton variant="ghost" @click="hosts.splice(i, 1)">{{ $t('velocity.remove') }}</AppButton>
            </div>
            <AppButton variant="ghost" @click="hosts.push({ hostname: '', targets: '' })">{{ $t('velocity.addHost') }}</AppButton>
            <div><AppButton type="submit" :loading="busy">{{ $t('velocity.save') }}</AppButton></div>
          </fieldset>
        </form>
      </Panel>
      <Panel :title="$t('velocity.forwarding')">
        <p>{{ $t('velocity.forwardingHint') }}</p>
        <div class="velocity-fields">
          <label v-for="backend in config.backends" :key="backend.serverId">
            <input v-model="selected" type="checkbox" :value="backend.serverId" :disabled="busy || !stopped || !backends.some(s => s.id === backend.serverId)">
            {{ backend.name }}
          </label>
          <div><AppButton variant="ghost" :disabled="busy || !stopped || !selected.length" @click="run(plan)">{{ $t('velocity.preview') }}</AppButton></div>
          <template v-if="preview">
            <p v-for="warning in preview.warnings" :key="warning">{{ warning }}</p>
            <div v-for="backend in preview.backends" :key="backend.serverId">
              <strong>{{ backend.name }}</strong>
              <p>{{ backend.files.join(', ') }}</p>
              <pre>{{ JSON.stringify(backend.changes, null, 2) }}</pre>
            </div>
            <label><input v-model="consent" type="checkbox" :disabled="!canApply"> {{ $t('velocity.consent') }}</label>
            <div><AppButton :disabled="!canApply || !consent" :loading="busy" @click="run(apply)">{{ $t('velocity.apply') }}</AppButton></div>
          </template>
        </div>
      </Panel>
      <Panel v-if="!auth.managed" :title="$t('velocity.runtime')">
        <form @submit.prevent="editable && run(saveRuntime)">
          <fieldset :disabled="!editable" class="velocity-fields">
            <FormField :label="$t('velocity.javaPath')" v-slot="{ id }"><input :id="id" v-model.trim="runtime.javaPath"></FormField>
            <FormField :label="$t('velocity.memoryMin')" v-slot="{ id }"><input :id="id" v-model.number="runtime.memoryMin" type="number" min="0.5" :max="runtime.memory" step="0.5" required></FormField>
            <FormField :label="$t('velocity.memory')" v-slot="{ id }"><input :id="id" v-model.number="runtime.memory" type="number" :min="runtime.memoryMin" max="64" step="0.5" required></FormField>
            <div><AppButton type="submit" :loading="busy">{{ $t('velocity.saveRuntime') }}</AppButton></div>
          </fieldset>
        </form>
      </Panel>
    </template>
    <Panel v-if="!auth.managed && auth.can('server.delete', id)" :title="$t('properties.danger.title')">
      <p>{{ $t('properties.danger.text') }}</p>
      <AppButton variant="danger" :loading="store.deletingServer" @click="store.openDeleteServerModal">{{ $t('properties.danger.deleteServer') }}</AppButton>
    </Panel>
  </div>
</template>

<style scoped>
.velocity-panels, .velocity-fields { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.velocity-fields { border: 0; padding: 0; margin: var(--space-4) 0 0; }
.velocity-row { display: flex; gap: var(--space-3); flex-wrap: wrap; align-items: end; }
.velocity-row > :not(button) { flex: 1 1 180px; min-width: 0; }
.velocity-error { color: var(--danger); }
p, pre { overflow-wrap: anywhere; max-width: 72ch; }
pre { white-space: pre-wrap; font-family: var(--font-mono); }
input:not([type="checkbox"]), select, textarea { width: 100%; min-width: 0; }
textarea { resize: vertical; }
</style>
