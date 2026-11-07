<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Panel from './ui/Panel.vue'
import AppButton from './ui/AppButton.vue'
import { get, post, del } from '../api/client'
import { uploadModpackArchive } from '../api/modrinth'
import { useAuthStore } from '../stores/auth'

const props = defineProps({ serverId: { type: String, required: true }, server: { type: Object, required: true } })
const auth = useAuthStore()
const plans = ref([])
const plan = ref(null)
const project = ref(props.server.modpack?.projectId || '')
const upload = ref(null)
const source = ref('project')
const name = ref('')
const error = ref('')
const working = ref(false)
const acknowledge = ref(false)
const execute = ref(false)
const confirmName = ref('')
const replaceFiles = ref([])
const expanded = ref(false)
let timer
let alive = true
const base = computed(() => `/api/servers/${encodeURIComponent(props.serverId)}/modpack-updates`)
const active = computed(() => ['preparing', 'staging', 'validating', 'promoting'].includes(plan.value?.status))
const busy = computed(() => working.value || active.value)
const allowed = (permissions, id = props.serverId) => permissions.every(p => auth.can(p, id))
const canStage = computed(() => !auth.managed && allowed(['server.fork', 'servers.create']))
const canValidate = computed(() => plan.value?.stagingId && allowed(
  ['server.view', 'server.settings.read', 'files.read', 'server.start', 'server.stop'], plan.value.stagingId))
const canPromote = computed(() => canValidate.value && allowed(['modpacks.promote', 'backups.create']))
const blocked = computed(() => Boolean(plan.value?.loaderChanges?.length))
const ready = computed(() => !busy.value && (source.value === 'project' ? project.value.trim() : upload.value))

async function operation(fn) {
  if (working.value) return
  clearTimeout(timer)
  working.value = true
  error.value = ''
  try { await fn() } catch (e) { if (alive) error.value = e.message } finally {
    working.value = false
    if (!error.value) schedule()
  }
}
function schedule() {
  clearTimeout(timer)
  if (alive && active.value) timer = setTimeout(() => operation(refresh), 1500)
}
async function refresh() {
  const result = await get(base.value)
  if (!alive) return
  plans.value = result.plans
  plan.value = result.plans.find(p => p.id === plan.value?.id) || result.plans[0] || null
}
function choose(id) {
  plan.value = plans.value.find(p => p.id === id) || null
  acknowledge.value = false
  execute.value = false
  confirmName.value = ''
  replaceFiles.value = []
  schedule()
}
async function uploadPack(event) {
  const file = event.target.files?.[0]
  if (!file) return
  await operation(async () => { upload.value = await uploadModpackArchive(file) })
}
async function prepare() {
  await operation(async () => {
    const result = await post(base.value, {
      source: source.value, project_id: project.value.trim(), upload_id: upload.value?.upload_id,
      replaceFiles: replaceFiles.value,
    })
    if (!alive) return
    plan.value = result
    plans.value.unshift(result)
    acknowledge.value = false
    execute.value = false
    confirmName.value = ''
  })
}
async function run(action) {
  await operation(async () => {
    const result = await post(`${base.value}/${plan.value.id}/${action}`, {
      reviewToken: plan.value.reviewToken, name: name.value,
      acknowledgeRisks: acknowledge.value, executePackCode: execute.value, confirmName: confirmName.value,
    })
    if (!alive) return
    plan.value = result
  })
}
async function remove() {
  await operation(async () => {
    await del(`${base.value}/${plan.value.id}`)
    plan.value = null
    await refresh()
  })
}
onMounted(() => { if (!auth.managed) operation(refresh) })
onBeforeUnmount(() => { alive = false; clearTimeout(timer) })
</script>

<template>
  <Panel v-if="!auth.managed" :title="$t('packUpdates.title')">
    <div class="pack-updates">
      <p>{{ $t('packUpdates.intro') }}</p>
      <AppButton variant="ghost" :aria-expanded="expanded" @click="expanded = !expanded">
        {{ $t(expanded ? 'packUpdates.collapse' : 'packUpdates.expand') }}
      </AppButton>
      <p v-if="error" role="alert" class="pack-updates__error">{{ error }}</p>
      <template v-if="expanded">
        <p>{{ $t('packUpdates.stopped') }}</p>
        <fieldset :disabled="busy" class="pack-updates__form">
          <label for="update-source">{{ $t('packUpdates.source') }}</label>
          <select id="update-source" v-model="source" @change="replaceFiles = []">
            <option value="project">{{ $t('packUpdates.project') }}</option>
            <option value="upload">{{ $t('packUpdates.upload') }}</option>
          </select>
          <template v-if="source === 'project'">
            <label for="update-project">{{ $t('packUpdates.projectId') }}</label>
            <input id="update-project" v-model="project" type="text" @input="replaceFiles = []" />
          </template>
          <template v-else>
            <label for="update-upload">{{ $t('packUpdates.archive') }}</label>
            <input id="update-upload" type="file" accept=".mrpack" @change="replaceFiles = []; uploadPack($event)" />
            <span v-if="upload">{{ upload.name || upload.filename }}</span>
          </template>
          <AppButton :disabled="!ready" @click="prepare">{{ $t('packUpdates.prepare') }}</AppButton>
        </fieldset>
        <label v-if="plans.length" class="pack-updates__field">
          {{ $t('packUpdates.saved') }}
          <select :value="plan?.id" :disabled="busy" @change="choose($event.target.value)">
            <option v-for="item in plans" :key="item.id" :value="item.id">
              {{ item.summary?.name || item.id.slice(0, 8) }} — {{ $t(`packUpdates.states.${item.status}`) }}
            </option>
          </select>
        </label>
        <template v-if="plan">
          <p role="status">{{ $t(`packUpdates.states.${plan.status}`) }}</p>
          <p v-if="plan.error" role="alert" class="pack-updates__error">{{ plan.error }}</p>
          <p v-if="plan.productionChanged" role="alert" class="pack-updates__warning">{{ $t('packUpdates.publishedFailure') }}</p>
          <p v-if="plan.recoveryRequired" role="alert" class="pack-updates__warning">{{ $t('packUpdates.recoveryRequired') }}</p>
          <p v-if="plan.summary">{{ plan.summary.name }} · {{ plan.summary.version }}</p>
          <p v-if="blocked" class="pack-updates__warning">{{ $t('packUpdates.runtimeBlocked') }}</p>
          <ul v-if="blocked">
            <li v-for="change in plan.loaderChanges" :key="change.field">
              {{ change.field }}: {{ change.current || $t('packUpdates.unknown') }} → {{ change.proposed || $t('packUpdates.unknown') }}
            </li>
          </ul>
          <div v-if="plan.changes" class="pack-updates__changes">
            <h3>{{ $t('packUpdates.files') }}</h3>
            <p v-if="!plan.changes.length">{{ $t('packUpdates.noChanges') }}</p>
            <article v-for="change in plan.changes" :key="change.path" class="pack-updates__change">
              <span>{{ $t(`packUpdates.actions.${change.action}`) }} · {{ change.path }}</span>
              <label v-if="change.conflict && plan.status === 'review'">
                <input v-model="replaceFiles" type="checkbox" :value="change.path" :disabled="busy" />
                {{ $t('packUpdates.replaceLocal') }}
              </label>
              <details v-if="change.diff">
                <summary>{{ $t('packUpdates.diff') }}</summary>
                <pre>{{ change.diff }}</pre>
              </details>
              <p v-else-if="change.category === 'config'">{{ $t('packUpdates.binary') }}</p>
            </article>
            <AppButton v-if="replaceFiles.length && plan.status === 'review'" :disabled="!ready" variant="ghost" @click="prepare">
              {{ $t('packUpdates.rebuild') }}
            </AppButton>
          </div>
          <details v-if="plan.preservedFiles?.length">
            <summary>{{ $t('packUpdates.preserved', { count: plan.preservedFiles.length }) }}</summary>
            <ul><li v-for="path in plan.preservedFiles" :key="path">{{ path }}</li></ul>
          </details>
          <details v-if="plan.dependencyReview?.length" open>
            <summary>{{ $t('packUpdates.dependencies') }}</summary>
            <ul><li v-for="item in plan.dependencyReview" :key="item">{{ item }}</li></ul>
          </details>
          <p v-if="plan.clientImpact">
            {{ $t('packUpdates.clientImpact', { before: plan.clientImpact.previousFiles, after: plan.clientImpact.proposedFiles }) }}
          </p>
          <details>
            <summary>{{ $t('packUpdates.risks') }}</summary>
            <ul><li v-for="warning in plan.warnings" :key="warning">{{ warning }}</li></ul>
          </details>
          <p v-if="plan.stagingId">{{ $t('packUpdates.stagingId') }} {{ plan.stagingId }}</p>
          <fieldset v-if="plan.status === 'review' && !blocked" :disabled="busy" class="pack-updates__form">
            <label for="update-staging-name">{{ $t('packUpdates.stagingName') }}</label>
            <input id="update-staging-name" v-model="name" type="text" maxlength="80" />
            <label class="pack-updates__check">
              <input v-model="acknowledge" type="checkbox" />
              {{ $t('packUpdates.acknowledge') }}
            </label>
            <AppButton :disabled="!canStage || !acknowledge || !name.trim() || replaceFiles.length > 0" @click="run('stage')">
              {{ $t('packUpdates.stage') }}
            </AppButton>
          </fieldset>
          <fieldset v-if="plan.status === 'staged'" :disabled="busy" class="pack-updates__form">
            <label class="pack-updates__check">
              <input v-model="execute" type="checkbox" />
              {{ $t('packUpdates.execute') }}
            </label>
            <AppButton :disabled="!canValidate || !execute" @click="run('validate')">{{ $t('packUpdates.validate') }}</AppButton>
            <p v-if="!canValidate">{{ $t('packUpdates.stagingPermission') }}</p>
          </fieldset>
          <fieldset v-if="plan.status === 'validated'" :disabled="busy" class="pack-updates__form">
            <label for="update-approve">{{ $t('packUpdates.approve', { name: server.name }) }}</label>
            <input id="update-approve" v-model="confirmName" type="text" autocomplete="off" />
            <AppButton variant="danger" :disabled="!canPromote || confirmName !== server.name" @click="run('promote')">
              {{ $t('packUpdates.promote') }}
            </AppButton>
          </fieldset>
          <p v-if="plan.safetySnapshotId">{{ $t('packUpdates.backup') }} {{ plan.safetySnapshotId }}</p>
          <div class="pack-updates__buttons">
            <AppButton variant="ghost" :disabled="working" @click="operation(refresh)">{{ $t('packUpdates.refresh') }}</AppButton>
            <AppButton variant="ghost" :disabled="busy" @click="remove">{{ $t('packUpdates.delete') }}</AppButton>
          </div>
          <p>{{ $t('packUpdates.retained') }}</p>
        </template>
      </template>
    </div>
  </Panel>
</template>

<style scoped>
.pack-updates { display: grid; gap: var(--space-4); line-height: var(--leading-normal); }
.pack-updates p { max-width: 75ch; }
.pack-updates__form { border: 0; display: grid; gap: var(--space-3); min-width: 0; }
.pack-updates__field { display: grid; gap: var(--space-2); }
.pack-updates input:not([type="checkbox"]), .pack-updates select { width: 100%; max-width: 36rem; min-width: 0; }
.pack-updates__check { display: flex; align-items: flex-start; gap: var(--space-2); max-width: 75ch; }
.pack-updates__check input { margin-top: 5px; flex-shrink: 0; }
.pack-updates__changes { margin-top: var(--space-4); display: grid; gap: var(--space-3); }
.pack-updates__change { display: grid; gap: var(--space-2); padding-block: var(--space-3); border-bottom: 1px solid var(--border-color); overflow-wrap: anywhere; }
.pack-updates h3 { font-size: var(--text-md); color: var(--text-primary); }
.pack-updates ul { padding-left: var(--space-5); overflow-wrap: anywhere; }
.pack-updates summary { cursor: pointer; color: var(--text-primary); }
.pack-updates pre { max-width: 100%; overflow: auto; padding: var(--space-3); background: var(--bg-primary); font-size: var(--text-sm); }
.pack-updates__error { color: var(--danger); }
.pack-updates__warning { color: var(--warning); }
.pack-updates__buttons { display: flex; gap: var(--space-3); flex-wrap: wrap; }
.pack-updates :deep(button) { justify-self: start; white-space: normal; text-align: left; }
</style>
