<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import Panel from '../ui/Panel.vue'
import AppButton from '../ui/AppButton.vue'
import FormField from '../ui/FormField.vue'
import ToggleRow from '../ui/ToggleRow.vue'
import { configureDns, getDns, getServerDns, previewDns, saveDnsRecords, saveServerDns, syncDns } from '../../api/dns'
import { useToast } from '../../composables/useToast'
import { t } from '../../i18n'
import { copyToClipboard } from '../../utils/clipboard'
import { useServerStore } from '../../stores/server'

const props = defineProps({ serverId: { type: String, default: '' } })
const toast = useToast()
const store = useServerStore()
const state = ref(null)
const busy = ref(false)
const error = ref('')
const config = ref({ enabled: false, url: 'http://dns-provider:8888', zone: '', domain: '' })
const binding = ref({ enabled: false, hostname: '', addresses: [], target: '', srv: true, ttl: 300, priority: 0, weight: 0 })
const addressText = ref('')
const editIndex = ref(-1)
const record = ref(blankRecord())
const targetText = ref('')
const planned = ref(null)
const types = ['A', 'AAAA', 'CNAME', 'SRV', 'TXT', 'MX', 'CAA', 'NS']
const changes = computed(() => (planned.value?.groups || []).flatMap(group =>
  Object.entries(group.changes).flatMap(([action, records]) =>
    records.map(record => ({ ...record, action, domain: group.config.domain }))
  )
))
const joinAddress = computed(() => {
  if (!binding.value.enabled) return ''
  const port = store.server?.port || 25565
  return binding.value.hostname + (!binding.value.srv && Number(port) !== 25565 ? `:${port}` : '')
})

function blankRecord() {
  return { dnsName: '', recordType: 'A', recordTTL: 300, targets: [] }
}
async function run(action) {
  busy.value = true
  error.value = ''
  try { await action() } catch (e) {
    error.value = e.message
    toast.error(e.message)
  } finally { busy.value = false }
}
async function load() {
  state.value = await getDns()
  if (state.value.config) config.value = { ...state.value.config }
  if (props.serverId) {
    const saved = await getServerDns(props.serverId)
    binding.value = { ...binding.value, ...saved }
    addressText.value = (saved.addresses || []).join('\n')
  }
}
function changed() { planned.value = null }
async function saveConfig() {
  await configureDns(config.value)
  changed()
  await load()
  toast.success(t('dns.saved'))
}
async function saveRecord() {
  const updated = [...state.value.records]
  const next = { ...record.value, targets: targetText.value.split('\n').filter(line => line.trim()) }
  if (editIndex.value < 0) updated.push(next)
  else updated.splice(editIndex.value, 1, next)
  await saveDnsRecords(updated)
  cancelEdit()
  changed()
  await load()
  toast.success(t('dns.saved'))
}
function edit(item, index) {
  editIndex.value = index
  record.value = { ...item }
  targetText.value = item.targets.join('\n')
}
function cancelEdit() {
  editIndex.value = -1
  record.value = blankRecord()
  targetText.value = ''
}
async function remove(index) {
  const records = state.value.records.filter((_, i) => i !== index)
  await saveDnsRecords(records)
  cancelEdit()
  changed()
  await load()
}
async function saveBinding() {
  await saveServerDns(props.serverId, {
    ...binding.value, addresses: addressText.value.split(/\s+/).filter(Boolean)
  })
  changed()
  await load()
  toast.success(t('dns.saved'))
}
async function preview() { planned.value = await previewDns() }
async function apply() {
  await syncDns()
  planned.value = null
  await load()
  toast.success(t('dns.applied'))
}
async function copyAddress() {
  if (await copyToClipboard(joinAddress.value)) toast.success(t('common.copied'))
  else throw new Error(t('dns.copyFailed'))
}
onMounted(() => run(load))
watch(() => props.serverId, () => { changed(); run(load) })
</script>

<template>
  <div class="dns-panels" :aria-busy="busy">
    <p v-if="error" class="dns-error" role="alert">{{ error }}</p>
    <p v-if="!state && busy" role="status">{{ $t('common.loading') }}</p>
    <AppButton v-if="!state && !busy" @click="run(load)">{{ $t('common.retry') }}</AppButton>
    <template v-if="state">
      <Panel v-if="!serverId" :title="$t('dns.connection')">
        <p class="dns-intro">{{ $t('dns.connectionHint') }}</p>
        <form class="dns-form" @submit.prevent="run(saveConfig)">
          <ToggleRow v-model="config.enabled" :label="$t('dns.enabled')" :hint="$t('dns.enabledHint')" />
          <FormField :label="$t('dns.webhookUrl')" :hint="$t('dns.webhookHint')" v-slot="{ id, describedBy }">
            <input :id="id" v-model="config.url" type="url" required :aria-describedby="describedBy">
          </FormField>
          <div class="dns-columns">
            <FormField :label="$t('dns.zone')" v-slot="{ id }">
              <input :id="id" v-model="config.zone" placeholder="example.com" required autocapitalize="none" spellcheck="false">
            </FormField>
            <FormField :label="$t('dns.domain')" :hint="$t('dns.domainHint')" v-slot="{ id, describedBy }">
              <input :id="id" v-model="config.domain" placeholder="mc.example.com" :aria-describedby="describedBy" autocapitalize="none" spellcheck="false">
            </FormField>
          </div>
          <div><AppButton type="submit" :loading="busy">{{ $t('common.save') }}</AppButton></div>
        </form>
      </Panel>

      <Panel v-if="serverId" :title="$t('dns.publicAddress')">
        <p class="dns-intro">{{ $t('dns.bindingHint') }}</p>
        <p v-if="!state.config">
          <router-link :to="{ name: 'GlobalSettings', params: { section: 'dns' } }">{{ $t('dns.configureFirst') }}</router-link>
        </p>
        <form v-else class="dns-form" @submit.prevent="run(saveBinding)">
          <ToggleRow v-model="binding.enabled" :label="$t('dns.publish')" />
          <template v-if="binding.enabled">
            <FormField :label="$t('dns.hostname')" v-slot="{ id }">
              <input :id="id" v-model="binding.hostname" :placeholder="`survival.${state.config.domain}`" required autocapitalize="none" spellcheck="false">
            </FormField>
            <FormField :label="$t('dns.addresses')" :hint="$t('dns.addressHint')" v-slot="{ id, describedBy }">
              <textarea :id="id" v-model="addressText" rows="2" placeholder="192.0.2.10" :aria-describedby="describedBy" spellcheck="false" />
            </FormField>
            <FormField :label="$t('dns.target')" :hint="$t('dns.targetHint')" v-slot="{ id, describedBy }">
              <input :id="id" v-model="binding.target" placeholder="host.example.net" :aria-describedby="describedBy" autocapitalize="none" spellcheck="false">
            </FormField>
            <ToggleRow v-model="binding.srv" :label="$t('dns.srv')" :hint="$t('dns.srvHint')" />
            <div class="dns-columns">
              <FormField :label="$t('dns.ttl')" v-slot="{ id }"><input :id="id" v-model.number="binding.ttl" type="number" min="60" max="2147483647" required></FormField>
              <FormField :label="$t('dns.priority')" v-slot="{ id }"><input :id="id" v-model.number="binding.priority" type="number" min="0" max="65535" required></FormField>
              <FormField :label="$t('dns.weight')" v-slot="{ id }"><input :id="id" v-model.number="binding.weight" type="number" min="0" max="65535" required></FormField>
            </div>
            <div v-if="joinAddress" class="dns-actions"><strong>{{ $t('dns.joinAddress') }}:</strong> <code>{{ joinAddress }}</code><AppButton variant="ghost" size="sm" @click="run(copyAddress)">{{ $t('common.copy') }}</AppButton></div>
          </template>
          <div><AppButton type="submit" :loading="busy">{{ $t('common.save') }}</AppButton></div>
        </form>
      </Panel>

      <Panel v-if="!serverId && state.config" :title="$t('dns.records')">
        <p class="dns-intro">{{ $t('dns.recordsHint') }}</p>
        <p v-if="!state.records.length" class="dns-intro">{{ $t('dns.noRecords') }}</p>
        <div v-else class="dns-table-wrap" tabindex="0" :aria-label="$t('dns.records')">
          <table>
            <thead><tr><th>{{ $t('dns.name') }}</th><th>{{ $t('dns.type') }}</th><th>{{ $t('dns.targets') }}</th><th>{{ $t('dns.ttl') }}</th><th>{{ $t('dns.actions') }}</th></tr></thead>
            <tbody><tr v-for="(item, index) in state.records" :key="`${item.dnsName}/${item.recordType}`">
              <td><code>{{ item.dnsName }}</code></td><td>{{ item.recordType }}</td>
              <td><div v-for="target in item.targets" :key="target"><code>{{ target }}</code></div></td><td>{{ item.recordTTL }}</td>
              <td><div class="dns-actions"><AppButton variant="ghost" size="sm" :disabled="busy" @click="edit(item, index)">{{ $t('dns.edit') }}</AppButton><AppButton variant="danger" size="sm" :disabled="busy" @click="run(() => remove(index))">{{ $t('common.delete') }}</AppButton></div></td>
            </tr></tbody>
          </table>
        </div>
        <form class="dns-form dns-editor" @submit.prevent="run(saveRecord)">
          <h3>{{ editIndex < 0 ? $t('dns.addRecord') : $t('dns.editRecord') }}</h3>
          <div class="dns-columns">
            <FormField :label="$t('dns.name')" v-slot="{ id }"><input :id="id" v-model="record.dnsName" :placeholder="`play.${state.config.domain}`" required autocapitalize="none" spellcheck="false"></FormField>
            <FormField :label="$t('dns.type')" v-slot="{ id }"><select :id="id" v-model="record.recordType"><option v-for="type in types" :key="type">{{ type }}</option></select></FormField>
            <FormField :label="$t('dns.ttl')" v-slot="{ id }"><input :id="id" v-model.number="record.recordTTL" type="number" min="60" max="2147483647" required></FormField>
          </div>
          <FormField :label="$t('dns.targets')" :hint="$t('dns.targetsHint')" v-slot="{ id, describedBy }">
            <textarea :id="id" v-model="targetText" rows="3" required :aria-describedby="describedBy" spellcheck="false" />
          </FormField>
          <div class="dns-actions"><AppButton type="submit" :loading="busy">{{ $t('common.save') }}</AppButton><AppButton v-if="editIndex >= 0" variant="ghost" @click="cancelEdit">{{ $t('common.cancel') }}</AppButton></div>
        </form>
      </Panel>

      <Panel :title="$t('dns.synchronization')">
        <div class="dns-form">
          <p role="status"><strong>{{ $t(`dns.states.${state.status.state}`) }}</strong><span v-if="state.status.syncedAt"> · {{ new Date(state.status.syncedAt).toLocaleString() }}</span></p>
          <p v-if="state.status.error" class="dns-error" role="alert">{{ state.status.error }}</p>
          <p class="dns-intro">{{ $t('dns.syncHint') }}</p>
          <div class="dns-actions">
            <AppButton variant="ghost" :disabled="!state.config" :loading="busy" @click="run(preview)">{{ $t('dns.preview') }}</AppButton>
            <AppButton v-if="planned" :loading="busy" @click="run(apply)">{{ $t('dns.apply') }}</AppButton>
          </div>
          <template v-if="planned">
            <p v-if="!changes.length">{{ $t('dns.noChanges') }}</p>
            <div v-else class="dns-table-wrap" tabindex="0" :aria-label="$t('dns.preview')">
              <table>
                <thead><tr><th>{{ $t('dns.action') }}</th><th>{{ $t('dns.name') }}</th><th>{{ $t('dns.type') }}</th><th>{{ $t('dns.targets') }}</th></tr></thead>
                <tbody><tr v-for="(item, i) in changes" :key="i"><td>{{ item.action }}</td><td><code>{{ item.dnsName }}</code></td><td>{{ item.recordType }}</td><td><div v-for="target in item.targets" :key="target"><code>{{ target }}</code></div></td></tr></tbody>
              </table>
            </div>
          </template>
        </div>
      </Panel>
    </template>
  </div>
</template>

<style scoped>
.dns-panels, .dns-form { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.dns-intro { color: var(--text-secondary); font-size: var(--text-sm); line-height: 1.6; max-width: 72ch; }
.dns-intro + form { margin-top: var(--space-4); }
.dns-columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: var(--space-4); }
.dns-actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.dns-error { color: var(--danger); overflow-wrap: anywhere; }
.dns-table-wrap { max-width: 100%; overflow-x: auto; margin-block: var(--space-4); }
table { border-collapse: collapse; width: 100%; font-size: var(--text-sm); text-align: left; }
th, td { padding: var(--space-3); border-bottom: 1px solid var(--border-color); vertical-align: top; }
th { color: var(--text-primary); font-weight: 500; }
code { font-family: var(--font-mono); font-size: var(--text-sm); overflow-wrap: anywhere; }
.dns-editor { margin-top: var(--space-6); }
h3 { font-size: var(--text-md); color: var(--text-primary); }
textarea { resize: vertical; font-family: var(--font-mono); }
input, textarea, select { width: 100%; min-width: 0; }
@media (max-width: 600px) { .dns-columns { grid-template-columns: 1fr; } }
</style>
