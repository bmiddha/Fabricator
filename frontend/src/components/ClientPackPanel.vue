<script setup>
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import Panel from './ui/Panel.vue'
import AppButton from './ui/AppButton.vue'
import SearchResultCard from './ui/SearchResultCard.vue'
import { get, post, put, apiRequest } from '../api/client'
import { getModVersions, searchMods } from '../api/modrinth'
import { useCapabilities } from '../composables/useCapabilities'
import { formatFileSize } from '../utils/format'

const props = defineProps({ serverId: { type: String, required: true }, server: Object })
const can = useCapabilities()
const Preview = defineAsyncComponent(() => import('./modals/ModProjectPreview.vue'))
const expanded = ref(false)
const busy = ref(false)
const error = ref('')
const plan = ref(null)
const config = ref(null)
const dirty = ref(false)
const exports = ref([])
const query = ref('')
const results = ref([])
const searching = ref(false)
const searched = ref(false)
const searchError = ref('')
const preview = ref(null)
const chosen = ref(null)
const versions = ref([])
const versionId = ref('')
const requirement = ref('required')
const upload = ref(null)
const base = computed(() => `/api/servers/${encodeURIComponent(props.serverId)}/client-pack`)
const editable = computed(() => can('clientpacks.manage'))
const readable = computed(() => can('clientpacks.read'))
const canDownload = computed(() => can('clientpacks.download'))
const supported = computed(() => ['fabric', 'quilt', 'forge', 'neoforge'].includes(props.server?.loader))
const canExport = computed(() => can('clientpacks.export') && plan.value && !dirty.value && !plan.value.errors.length)
const pinnedLoader = computed(() => Object.entries(plan.value?.dependencies || {}).find(([key]) => key !== 'minecraft')?.[1] || '')
const pendingSelections = computed(() => (config.value?.selections || []).filter(
  selection => selection.key.startsWith('client:') && !plan.value?.entries.some(entry => entry.key === selection.key)
))
let generation = 0

async function operation(fn) {
  busy.value = true
  error.value = ''
  try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function refresh() {
  const current = ++generation
  // Both endpoints share the non-blocking per-server lock.
  const value = readable.value ? await get(base.value) : null
  const artifacts = canDownload.value ? await get(`${base.value}/exports`) : { exports: [] }
  if (current !== generation) return
  plan.value = value
  config.value = value ? structuredClone(value.config) : null
  exports.value = artifacts.exports
  dirty.value = false
}
async function toggle() {
  expanded.value = !expanded.value
  if (expanded.value) await operation(refresh)
}
async function save() {
  await operation(async () => {
    await put(base.value, config.value)
    await refresh()
  })
}
function setSelection(key, value, pinnedVersion = '') {
  const selections = config.value.selections
  const existing = selections.find(entry => entry.key === key)
  if (existing) Object.assign(existing, { requirement: value, ...(pinnedVersion ? { version_id: pinnedVersion } : {}) })
  else selections.push({ key, requirement: value, version_id: pinnedVersion })
  dirty.value = true
}
function selectionValue(entry) {
  return config.value.selections.find(selection => selection.key === entry.key)?.requirement || entry.requirement
}
function removeClient(key) {
  config.value.selections = config.value.selections.filter(entry => entry.key !== key)
  dirty.value = true
}
async function search() {
  if (searching.value) return
  searching.value = true
  searchError.value = ''
  results.value = []
  searched.value = false
  try {
    const response = await searchMods({
      query: query.value, version: props.server.version, loader: props.server.loader, limit: 12,
    })
    results.value = response.hits || []
    searched.value = true
  } catch (e) { searchError.value = e.message } finally { searching.value = false }
}
async function choose(hit) {
  await operation(async () => {
    chosen.value = hit
    versionId.value = ''
    versions.value = await getModVersions(hit.project_id, {
      game_versions: props.server.version, loaders: props.server.loader,
    })
    if (versions.value.length) versionId.value = versions.value[0].id
  })
}
function addClient() {
  setSelection(`client:${chosen.value.project_id}`, requirement.value, versionId.value)
  chosen.value = null
}
async function build() {
  await operation(async () => {
    const artifact = await post(`${base.value}/exports`, { fingerprint: plan.value.fingerprint })
    // Export permissions need not imply artifact-list or download permissions.
    if (canDownload.value) exports.value = (await get(`${base.value}/exports`)).exports
    else exports.value = [artifact, ...exports.value]
  })
}
async function importMetadata(event) {
  const file = event.target.files?.[0]
  if (!file) return
  await operation(async () => {
    await apiRequest(`${base.value}/import`, {
      method: 'POST', headers: { 'Content-Type': 'application/x-modrinth-modpack+zip' }, body: file,
    })
    await refresh()
  })
  event.target.value = ''
}
watch(() => props.serverId, () => {
  ++generation
  expanded.value = false
  plan.value = null
  config.value = null
  exports.value = []
  dirty.value = false
  results.value = []
  chosen.value = null
  preview.value = null
  error.value = ''
})
</script>

<template>
  <Panel :title="$t('clientPack.title')">
    <div class="client-pack">
      <div class="client-pack__intro">
        <p>{{ $t('clientPack.intro') }}</p>
        <AppButton variant="ghost" :disabled="busy" :aria-expanded="expanded" @click="toggle">
          {{ expanded ? $t('clientPack.hide') : $t('clientPack.open') }}
        </AppButton>
      </div>
      <p v-if="!supported">{{ $t('clientPack.unsupported') }}</p>
      <p v-if="error" role="alert" class="client-pack__error">{{ error }}</p>
      <p v-if="busy" role="status">{{ $t('clientPack.working') }}</p>
      <template v-if="expanded">
        <template v-if="config">
          <fieldset :disabled="!editable || busy" class="client-pack__fields" @input="dirty = true">
            <label>{{ $t('clientPack.name') }}<input v-model="config.name" maxlength="120" /></label>
            <label>{{ $t('clientPack.loaderVersion') }}<input v-model="config.loader_version" :placeholder="pinnedLoader" maxlength="80" /></label>
          </fieldset>
          <p>{{ $t('clientPack.loaderHelp') }}</p>
          <div class="client-pack__actions">
            <AppButton v-if="editable" :disabled="busy || !dirty" @click="save">{{ $t('clientPack.save') }}</AppButton>
            <AppButton :disabled="busy || dirty" variant="ghost" @click="operation(refresh)">{{ $t('clientPack.refresh') }}</AppButton>
            <AppButton v-if="can('clientpacks.export')" :disabled="busy || !canExport" @click="build">{{ $t('clientPack.export') }}</AppButton>
            <span v-if="dirty" role="status">{{ $t('clientPack.unsaved') }}</span>
          </div>
          <template v-if="plan">
            <p>{{ $t('clientPack.target', { minecraft: plan.dependencies?.minecraft || server?.version, loader: server?.loader, version: pinnedLoader || '?' }) }}</p>
            <p role="status">{{ $t('clientPack.counts', { mods: plan.file_count, overrides: plan.override_count }) }}</p>
            <div v-if="plan.errors.length" class="client-pack__review" role="alert">
              <h4>{{ $t('clientPack.blocked') }}</h4>
              <ul><li v-for="message in plan.errors" :key="message">{{ message }}</li></ul>
            </div>
            <div v-if="plan.warnings.length" class="client-pack__review">
              <h4>{{ $t('clientPack.review') }}</h4>
              <ul><li v-for="message in plan.warnings" :key="message">{{ message }}</li></ul>
            </div>
            <details v-if="plan.excluded_imported_overrides.length">
              <summary>{{ $t('clientPack.excludedOverrides') }}</summary>
              <ul><li v-for="path in plan.excluded_imported_overrides" :key="path">{{ path }}</li></ul>
            </details>
            <h4>{{ $t('clientPack.contents') }}</h4>
            <p>{{ $t('clientPack.requirementHelp') }}</p>
            <div v-if="!plan.entries.length && !pendingSelections.length">{{ $t('clientPack.empty') }}</div>
            <div v-for="entry in plan.entries" :key="entry.key" class="client-pack__entry">
              <div><strong>{{ entry.title }}</strong><p>{{ entry.version || entry.path }} &middot; {{ $t(`clientPack.origins.${entry.origin}`) }}</p></div>
              <div class="client-pack__actions">
                <AppButton v-if="entry.project_id" variant="ghost" size="sm" @click="preview = { project_id: entry.project_id, title: entry.title }">{{ $t('modPreview.details') }}</AppButton>
                <label class="client-pack__requirement">{{ $t('clientPack.requirement') }}
                  <select :value="selectionValue(entry)" :disabled="!editable || busy" @change="setSelection(entry.key, $event.target.value, entry.key.startsWith('client:') ? entry.version_id : '')">
                    <option value="required">{{ $t('clientPack.required') }}</option>
                    <option value="optional">{{ $t('clientPack.optional') }}</option>
                    <option value="excluded">{{ $t('clientPack.excluded') }}</option>
                  </select>
                </label>
                <AppButton v-if="editable && entry.origin === 'client'" variant="ghost" size="sm" :disabled="busy" @click="removeClient(entry.key)">{{ $t('clientPack.remove') }}</AppButton>
              </div>
            </div>
            <div v-for="selection in pendingSelections" :key="selection.key" class="client-pack__entry">
              <span>{{ selection.key }} &middot; {{ selection.version_id }} &middot; {{ $t(`clientPack.${selection.requirement}`) }}</span>
              <AppButton v-if="editable" variant="ghost" size="sm" :disabled="busy" @click="removeClient(selection.key)">{{ $t('clientPack.remove') }}</AppButton>
            </div>
            <details v-if="editable && supported" class="client-pack__section">
              <summary>{{ $t('clientPack.add') }}</summary>
              <p>{{ $t('clientPack.clientOnlyHelp') }}</p>
              <form class="client-pack__actions" @submit.prevent="search">
                <label>{{ $t('clientPack.search') }}<input v-model="query" type="search" /></label>
                <AppButton type="submit" :disabled="searching || busy">{{ $t('clientPack.search') }}</AppButton>
              </form>
              <p v-if="searching" role="status">{{ $t('clientPack.searching') }}</p>
              <p v-if="searchError" role="alert">{{ searchError }}</p>
              <p v-if="searched && !results.length">{{ $t('clientPack.noResults') }}</p>
              <div class="client-pack__results">
                <SearchResultCard v-for="hit in results" :key="hit.project_id" :title="hit.title" :description="hit.description" :icon-url="hit.icon_url">
                  <AppButton variant="ghost" size="sm" @click="preview = hit">{{ $t('modPreview.details') }}</AppButton>
                  <AppButton size="sm" :disabled="busy" @click="choose(hit)">{{ $t('clientPack.choose') }}</AppButton>
                </SearchResultCard>
              </div>
              <div v-if="chosen" class="client-pack__fields">
                <h4>{{ chosen.title }}</h4>
                <label>{{ $t('clientPack.pinnedVersion') }}
                  <select v-model="versionId"><option v-for="version in versions" :key="version.id" :value="version.id">{{ version.version_number }} ({{ version.id }})</option></select>
                </label>
                <label>{{ $t('clientPack.requirement') }}<select v-model="requirement"><option value="required">{{ $t('clientPack.required') }}</option><option value="optional">{{ $t('clientPack.optional') }}</option></select></label>
                <p v-if="!versions.length">{{ $t('clientPack.noVersion') }}</p>
                <AppButton :disabled="!versionId || busy" @click="addClient">{{ $t('clientPack.addSelection') }}</AppButton>
              </div>
            </details>
            <details class="client-pack__section">
              <summary>{{ $t('clientPack.overrides') }}</summary>
              <p>{{ $t('clientPack.overridesHelp') }}</p>
              <label v-for="path in plan.override_candidates" :key="path" class="client-pack__check">
                <input v-model="config.overrides" type="checkbox" :value="path" :disabled="!editable || busy" @change="dirty = true" />{{ path }}
              </label>
              <p v-if="!plan.override_candidates.length">{{ $t('clientPack.noOverrides') }}</p>
            </details>
            <div v-if="editable" class="client-pack__section">
              <p>{{ $t('clientPack.importHelp') }}</p>
              <input ref="upload" type="file" accept=".mrpack" hidden @change="importMetadata" />
              <AppButton variant="ghost" :disabled="busy || dirty" @click="upload.click()">{{ $t('clientPack.import') }}</AppButton>
            </div>
          </template>
        </template>
        <div class="client-pack__section">
          <h4>{{ $t('clientPack.downloads') }}</h4>
          <p>{{ $t('clientPack.downloadHelp') }}</p>
          <p v-if="!exports.length">{{ $t('clientPack.noExports') }}</p>
          <div v-for="artifact in exports" :key="artifact.id" class="client-pack__entry">
            <div>
              <strong>{{ artifact.filename }}</strong>
              <p>{{ new Date(artifact.created_at * 1000).toLocaleString() }} &middot; {{ formatFileSize(artifact.size) }}</p>
              <p v-if="plan && artifact.fingerprint !== plan.fingerprint" class="client-pack__stale">{{ $t('clientPack.outdated') }}</p>
              <p v-else-if="!plan">{{ $t('clientPack.snapshot') }}</p>
            </div>
            <a v-if="canDownload" :href="`${base}/exports/${artifact.id}/download`" download>{{ $t('clientPack.download') }}</a>
          </div>
        </div>
      </template>
    </div>
  </Panel>
  <Preview v-if="preview" :show="true" :project-id="preview.project_id" :title="preview.title" @close="preview = null" />
</template>

<style scoped>
.client-pack { display: grid; gap: var(--space-4); font-size: var(--text-sm); line-height: var(--leading-normal); }
.client-pack p { max-width: 75ch; }
.client-pack__intro, .client-pack__actions, .client-pack__entry { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
.client-pack__actions { justify-content: flex-start; }
.client-pack__fields { border: 0; display: flex; align-items: end; gap: var(--space-4); flex-wrap: wrap; }
.client-pack label { display: grid; gap: var(--space-2); }
.client-pack input:not([type=checkbox]), .client-pack select { min-width: 0; max-width: 100%; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-color); border-radius: var(--radius-sm); }
.client-pack__entry { border-bottom: 1px solid var(--border-color); padding-block: var(--space-3); overflow-wrap: anywhere; }
.client-pack__entry > div:first-child { flex: 1; min-width: 160px; }
.client-pack__entry strong, .client-pack h4 { color: var(--text-primary); }
.client-pack__entry p { margin-top: var(--space-1); }
.client-pack__section { border-top: 1px solid var(--border-color); padding-top: var(--space-4); }
.client-pack__section > * + * { margin-top: var(--space-3); }
.client-pack__check { display: flex !important; align-items: center; overflow-wrap: anywhere; }
.client-pack__review { padding: var(--space-3); background: var(--bg-tertiary); border-radius: var(--radius-sm); }
.client-pack ul { padding-left: var(--space-5); overflow-wrap: anywhere; }
.client-pack__error { color: var(--danger); }
.client-pack__stale { color: var(--warning); }
.client-pack__results { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: var(--space-3); }
.client-pack summary { cursor: pointer; color: var(--text-primary); }
.client-pack a { color: var(--info); }
.client-pack :is(input, select, summary, a):focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
@media (max-width: 600px) {
  .client-pack__fields { display: grid; grid-template-columns: minmax(0, 1fr); }
  .client-pack__entry { align-items: flex-start; }
  .client-pack__entry > .client-pack__actions { width: 100%; }
}
</style>
