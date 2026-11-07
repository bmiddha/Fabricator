<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Panel from './ui/Panel.vue'
import AppButton from './ui/AppButton.vue'
import MetricTrend from './diagnostics/MetricTrend.vue'
import { get, post, put } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { reportUrl } from '../utils/diagnostics'
import { t } from '../i18n'

const props = defineProps({ serverId: { type: String, required: true } })
const auth = useAuthStore()
const canManage = computed(() => auth.can('diagnostics.manage', props.serverId))
const base = computed(() => `/api/servers/${encodeURIComponent(props.serverId)}/diagnostics`)
const data = ref(null)
const window = ref('1h')
const error = ref('')
const loading = ref(false)
const busy = ref(false)
const consent = ref(false)
const duration = ref(60)
const end = ref(Date.now() / 1000)
const start = computed(() => end.value - ({ '1h': 3600, '6h': 21600, '24h': 86400 }[window.value]))
const active = computed(() => data.value?.captures.some(capture => ['requested', 'running'].includes(capture.status)))
const families = [
  [{ key: 'tps', unit: 'TPS' }, { key: 'mspt', unit: 'ms/tick' }],
  [{ key: 'rssBytes', unit: 'GiB' }, { key: 'heapUsedBytes', unit: 'GiB' }, { key: 'gcTimeMs', unit: 'ms' }, { key: 'gcCollections', unit: 'count' }],
  [{ key: 'volumeUsedBytes', unit: 'GiB' }, { key: 'cpu', unit: '%' }],
]
const selected = ref(['tps', 'rssBytes', 'volumeUsedBytes'])
const selections = computed(() => families.map((family, index) => family.find(metric => metric.key === selected.value[index])))
let timer
let alive = true
let requestId = 0

async function refresh() {
  const id = ++requestId
  loading.value = true
  try {
    const result = await get(base.value, { window: window.value })
    if (!alive || id !== requestId) return
    data.value = result
    end.value = Date.now() / 1000
    error.value = ''
  } catch (e) {
    if (alive && id === requestId) error.value = e.message
  } finally {
    if (alive && id === requestId) loading.value = false
  }
}
async function operation(fn) {
  busy.value = true
  error.value = ''
  try { await fn(); await refresh() } catch (e) { error.value = e.message } finally { busy.value = false }
}
function setSampling(event) {
  const enabled = event.target.checked
  // Keep the control tied to persisted state if saving fails.
  event.target.checked = data.value.jvmSampling
  return operation(() => put(`${base.value}/sampling`, { jvmSampling: enabled }))
}
async function capture() {
  if (!consent.value) return
  await operation(async () => {
    await post(`${base.value}/captures`, { durationSeconds: duration.value, acceptUpload: true })
    consent.value = false
  })
}
onMounted(async () => {
  await refresh()
  if (alive) timer = setInterval(refresh, 15000)
})
onUnmounted(() => { alive = false; requestId++; clearInterval(timer) })
</script>

<template>
  <Panel :title="t('diagnostics.title')">
    <template #action>
      <AppButton variant="ghost" size="sm" :loading="loading" @click="refresh">{{ t('diagnostics.refresh') }}</AppButton>
    </template>
    <div class="diagnostics">
      <p class="diagnostics__intro">{{ t('diagnostics.intro') }}</p>
      <p v-if="error" role="alert" class="diagnostics__error">{{ error }}</p>
      <p v-if="!data && loading" role="status">{{ t('diagnostics.loading') }}</p>
      <template v-if="data">
        <p v-if="data.samplingError" role="alert" class="diagnostics__error">{{ data.samplingError }}</p>
        <p v-if="!data.samplingEnabled" role="status">{{ t('diagnostics.disabled') }}</p>
        <div class="diagnostics__toolbar">
          <label>{{ t('diagnostics.window') }}
            <select v-model="window" @change="refresh">
              <option value="1h">{{ t('diagnostics.hour') }}</option>
              <option value="6h">{{ t('diagnostics.sixHours') }}</option>
              <option value="24h">{{ t('diagnostics.day') }}</option>
            </select>
          </label>
          <span>{{ t('diagnostics.retention') }}</span>
        </div>
        <p v-if="!data.samples.length" role="status">{{ t('diagnostics.empty') }}</p>
        <div class="diagnostics__charts">
          <div v-for="(family, index) in families" :key="index" class="diagnostics__chart">
            <label>
              <span class="diagnostics__sr-only">{{ t('diagnostics.metric') }}</span>
              <select v-model="selected[index]">
                <option v-for="metric in family" :key="metric.key" :value="metric.key">{{ t(`diagnostics.metrics.${metric.key}`) }}</option>
              </select>
            </label>
            <MetricTrend :samples="data.samples" :metric="selections[index].key" :unit="selections[index].unit" :label="t(`diagnostics.metrics.${selections[index].key}`)" :start="start" :end="end" />
          </div>
        </div>
        <p class="diagnostics__note">{{ t('diagnostics.units') }}</p>
        <p class="diagnostics__note">{{ t('diagnostics.diskScope') }}</p>
        <label v-if="canManage" class="diagnostics__check">
          <input type="checkbox" :checked="data.jvmSampling" :disabled="busy" @change="setSampling">
          {{ t('diagnostics.jvmSampling') }}
        </label>
        <p class="diagnostics__note">{{ t('diagnostics.jvmHelp') }}</p>
        <p v-if="data.samples.at(-1)?.jvmUnavailable" class="diagnostics__note">{{ t(`diagnostics.reasons.${data.samples.at(-1).jvmUnavailable}`) }}</p>
        <section class="diagnostics__profiling" :aria-label="t('diagnostics.spark')">
          <h4>{{ t('diagnostics.spark') }}</h4>
          <p>{{ t('diagnostics.sparkHelp') }}</p>
          <p v-if="!data.capability.supported">{{ t('diagnostics.unsupported') }}</p>
          <p v-else-if="!data.captureReady" role="status">{{ t('diagnostics.notReady') }}</p>
          <a :href="data.capability.docs" target="_blank" rel="noopener noreferrer">{{ t('diagnostics.docs') }}</a>
          <template v-if="canManage && data.capability.supported">
            <label class="diagnostics__check">
              <input v-model="consent" type="checkbox" :disabled="busy || active">
              {{ t('diagnostics.consent') }}
            </label>
            <div class="diagnostics__toolbar">
              <label>{{ t('diagnostics.duration') }}
                <select v-model="duration" :disabled="busy || active">
                  <option :value="60">{{ t('diagnostics.seconds', { count: 60 }) }}</option>
                  <option :value="120">{{ t('diagnostics.seconds', { count: 120 }) }}</option>
                  <option :value="300">{{ t('diagnostics.seconds', { count: 300 }) }}</option>
                </select>
              </label>
              <AppButton :disabled="!consent || active || !data.captureReady" :loading="busy" @click="capture">{{ t('diagnostics.startCapture') }}</AppButton>
            </div>
          </template>
          <p v-if="!canManage" class="diagnostics__note">{{ t('diagnostics.readOnly') }}</p>
          <p v-if="!data.captures.length" class="diagnostics__note">{{ t('diagnostics.noCaptures') }}</p>
          <ol v-else class="diagnostics__captures" aria-live="polite">
            <li v-for="entry in data.captures" :key="entry.id">
              <div><time :datetime="new Date(entry.created * 1000).toISOString()">{{ new Date(entry.created * 1000).toLocaleString() }}</time> · {{ t('diagnostics.seconds', { count: entry.durationSeconds }) }} · <strong>{{ t(`diagnostics.status.${entry.status}`) }}</strong></div>
              <p>{{ entry.message }}</p>
              <a v-if="reportUrl(entry.reportUrl)" :href="reportUrl(entry.reportUrl)" target="_blank" rel="noopener noreferrer">{{ t('diagnostics.openReport') }}</a>
            </li>
          </ol>
        </section>
      </template>
    </div>
  </Panel>
</template>

<style scoped>
.diagnostics { display: grid; gap: var(--space-4); font-size: var(--text-sm); line-height: var(--leading-normal); }
.diagnostics__intro, .diagnostics__note { max-width: 75ch; color: var(--text-muted); }
.diagnostics__toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-4); }
.diagnostics__toolbar > span { color: var(--text-muted); }
label { display: inline-flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
select { max-width: 100%; min-height: 32px; border: 1px solid var(--border-color); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); background: var(--bg-tertiary); color: var(--text-primary); font: inherit; }
select:focus-visible, input:focus-visible, a:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.diagnostics__charts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-6); }
.diagnostics__chart { min-width: 0; display: grid; align-content: start; gap: var(--space-3); }
.diagnostics__check { display: flex; flex-wrap: nowrap; align-items: flex-start; max-width: 75ch; }
input { accent-color: var(--primary); flex-shrink: 0; margin-top: 3px; }
.diagnostics__profiling { display: grid; gap: var(--space-3); border-top: 1px solid var(--border-color); padding-top: var(--space-5); }
h4 { font-size: var(--text-md); color: var(--text-primary); }
a { color: var(--info); width: fit-content; }
.diagnostics__captures { list-style: none; display: grid; gap: var(--space-4); padding: 0; }
.diagnostics__captures li { display: grid; gap: var(--space-1); overflow-wrap: anywhere; }
.diagnostics__captures p { max-width: 75ch; color: var(--text-muted); }
.diagnostics__error { color: var(--danger); }
.diagnostics__sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
@media (max-width: 1100px) { .diagnostics__charts { grid-template-columns: 1fr; } }
</style>
