<script setup>
import { computed, ref, watch } from 'vue'
import Panel from './ui/Panel.vue'
import AppButton from './ui/AppButton.vue'
import ConfirmModal from './modals/ConfirmModal.vue'
import { get, post, del } from '../api/client'
import { useCapabilities } from '../composables/useCapabilities'
import { formatFileSize } from '../utils/format'
import { copyToClipboard } from '../utils/clipboard'
import { t } from '../i18n'

const props = defineProps({ serverId: String, server: Object })
const can = useCapabilities()
const base = computed(() => `/api/servers/${encodeURIComponent(props.serverId)}`)
const stopped = computed(() => props.server?.status === 'stopped')
const shareable = computed(() => can('worlds.share') && can('worlds.download'))
const expanded = ref(false)
const busy = ref(false)
const error = ref('')
const groups = ref([])
const selected = ref('')
const artifacts = ref([])
const links = ref([])
const shareTarget = ref('')
const ttl = ref(86400)
const acknowledged = ref(false)
const newLink = ref(null)
const copied = ref(false)
const confirmation = ref(null)
let generation = 0

async function operation(fn) {
  busy.value = true
  error.value = ''
  try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function refresh() {
  const current = generation
  const url = base.value
  const archiveResult = can('worlds.download') ? await get(`${url}/world-exports`) : { exports: [] }
  if (current !== generation) return
  const shareResult = shareable.value ? await get(`${url}/world-export-shares`) : { shares: [] }
  if (current !== generation) return
  artifacts.value = archiveResult.exports
  links.value = shareResult.shares
  if (!artifacts.value.some(a => a.id === shareTarget.value)) shareTarget.value = ''
  groups.value = []
  const worldResult = can('worlds.read') && can('worlds.export') && stopped.value
    ? await get(`${url}/worlds`) : { worlds: [] }
  if (current !== generation) return
  groups.value = worldResult.worlds
  if (!groups.value.some(g => g.id === selected.value)) selected.value = ''
}
async function toggle() {
  expanded.value = !expanded.value
  newLink.value = null
  if (expanded.value) await operation(refresh)
}
async function build() {
  const current = generation
  await operation(async () => {
    const result = await post(`${base.value}/world-exports`, { world_id: selected.value })
    if (current !== generation) return
    if (can('worlds.download')) await refresh()
    else artifacts.value = [result, ...artifacts.value]
  })
}
async function share() {
  const current = generation
  await operation(async () => {
    const link = await post(`${base.value}/world-exports/${shareTarget.value}/shares`, {
      expiry_seconds: ttl.value, acknowledge_privacy: acknowledged.value,
    })
    if (current !== generation) return
    newLink.value = { ...link, url: new URL(link.url, window.location.origin).href }
    copied.value = false
    acknowledged.value = false
    await refresh()
  })
}
async function copy() {
  if (await copyToClipboard(newLink.value.url)) copied.value = true
  else error.value = t('worldExports.copyFailed')
}
async function confirm() {
  await operation(async () => {
    const target = confirmation.value
    if (target.kind === 'delete') {
      await del(`${base.value}/world-exports/${target.id}`, { confirm_shared: true })
      if (newLink.value?.artifact_id === target.id) newLink.value = null
    } else {
      await del(`${base.value}/world-export-shares/${target.id}`)
      if (newLink.value?.id === target.id) newLink.value = null
    }
    confirmation.value = null
    await refresh()
  })
}
watch(shareTarget, () => { acknowledged.value = false; newLink.value = null })
watch(() => props.serverId, () => {
  generation++
  expanded.value = false
  groups.value = []
  selected.value = ''
  artifacts.value = []
  links.value = []
  shareTarget.value = ''
  newLink.value = null
  confirmation.value = null
  error.value = ''
})
</script>

<template>
  <Panel :title="$t('worldExports.title')">
    <div class="world-exports">
      <div class="world-exports__actions">
        <p>{{ $t('worldExports.intro') }}</p>
        <AppButton variant="ghost" :disabled="busy" :aria-expanded="expanded" @click="toggle">
          {{ $t(expanded ? 'worldExports.hide' : 'worldExports.open') }}
        </AppButton>
      </div>
      <p v-if="error" role="alert" class="world-exports__error">{{ error }}</p>
      <p v-if="busy" role="status">{{ $t('worldExports.working') }}</p>
      <template v-if="expanded">
        <p>{{ $t('worldExports.stopped') }}</p>
        <p>{{ $t('worldExports.contents') }}</p>
        <form v-if="can('worlds.read') && can('worlds.export')" class="world-exports__actions" @submit.prevent="build">
          <label for="export-world">{{ $t('worldExports.group') }}
            <select id="export-world" v-model="selected" :disabled="busy || !stopped">
              <option value="">{{ $t('worldExports.choose') }}</option>
              <option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}</option>
            </select>
          </label>
          <AppButton type="submit" :disabled="busy || !stopped || !selected">{{ $t('worldExports.build') }}</AppButton>
        </form>
        <p v-if="stopped && can('worlds.export') && !groups.length">{{ $t('worldExports.noWorlds') }}</p>
        <p v-for="group in groups.filter(g => g.id === selected)" :key="group.id">
          {{ group.layout }} · {{ group.version }} · {{ group.directories.join(', ') }}
          <span v-if="group.dimensions.length"> · {{ group.dimensions.join(', ') }}</span>
        </p>
        <div class="world-exports__actions">
          <h3>{{ $t('worldExports.archives') }}</h3>
          <AppButton variant="ghost" :disabled="busy" @click="operation(refresh)">{{ $t('worldExports.refresh') }}</AppButton>
        </div>
        <p>{{ $t('worldExports.retention') }}</p>
        <p v-if="!artifacts.length">{{ $t('worldExports.empty') }}</p>
        <div v-for="artifact in artifacts" :key="artifact.id" class="world-exports__archive">
          <div>
            <strong>{{ artifact.world.name }}</strong>
            <p>{{ formatFileSize(artifact.size) }} · {{ new Date(artifact.created_at * 1000).toLocaleString() }}</p>
            <p>{{ $t('worldExports.hash') }} <code>{{ artifact.sha256 }}</code></p>
            <p v-if="artifact.active_shares">{{ $t('worldExports.pinned', { count: artifact.active_shares, until: new Date(artifact.pinned_until * 1000).toLocaleString() }) }}</p>
          </div>
          <div class="world-exports__actions">
            <a v-if="can('worlds.download')" :href="`${base}/world-exports/${artifact.id}/download`" download>{{ $t('worldExports.download') }}</a>
            <AppButton v-if="shareable" variant="ghost" :disabled="busy" @click="shareTarget = artifact.id">{{ $t('worldExports.share') }}</AppButton>
            <AppButton v-if="shareable" variant="ghost" :disabled="busy" @click="confirmation = { kind: 'delete', id: artifact.id }">{{ $t('worldExports.delete') }}</AppButton>
          </div>
        </div>
        <form v-if="shareTarget && shareable" class="world-exports__review" @submit.prevent="share">
          <h3>{{ $t('worldExports.review') }}</h3>
          <p>{{ $t('worldExports.privacy') }}</p>
          <p>{{ $t('worldExports.publicRouting') }}</p>
          <label for="world-share-expiry">{{ $t('worldExports.expiry') }}
            <select id="world-share-expiry" v-model.number="ttl" :disabled="busy">
              <option :value="3600">{{ $t('worldExports.hour') }}</option>
              <option :value="86400">{{ $t('worldExports.day') }}</option>
              <option :value="604800">{{ $t('worldExports.week') }}</option>
            </select>
          </label>
          <label class="world-exports__check">
            <input v-model="acknowledged" type="checkbox" :disabled="busy" />
            {{ $t('worldExports.acknowledge') }}
          </label>
          <AppButton type="submit" :disabled="busy || !acknowledged">{{ $t('worldExports.createShare') }}</AppButton>
        </form>
        <div v-if="newLink" class="world-exports__review">
          <p>{{ $t('worldExports.linkOnce') }}</p>
          <label for="world-share-link">{{ $t('worldExports.link') }}
            <input id="world-share-link" :value="newLink.url" readonly />
          </label>
          <AppButton :disabled="busy" @click="copy">{{ $t(copied ? 'worldExports.copied' : 'worldExports.copy') }}</AppButton>
        </div>
        <template v-if="shareable && links.length">
          <h3>{{ $t('worldExports.links') }}</h3>
          <p>{{ $t('worldExports.recall') }}</p>
          <div v-for="link in links" :key="link.id" class="world-exports__archive">
            <p>{{ link.artifact_id.slice(0, 12) }} · {{ $t('worldExports.expires') }} {{ new Date(link.expires_at * 1000).toLocaleString() }}</p>
            <AppButton variant="ghost" :disabled="busy" @click="confirmation = { kind: 'revoke', id: link.id }">{{ $t('worldExports.revoke') }}</AppButton>
          </div>
        </template>
      </template>
    </div>
    <ConfirmModal :show="!!confirmation" type="warning" :loading="busy"
      :title="$t(confirmation?.kind === 'delete' ? 'worldExports.delete' : 'worldExports.revoke')"
      :message="$t('worldExports.confirmRemoval')" :description="$t('worldExports.recall')"
      @cancel="confirmation = null" @confirm="confirm">
      <template #extra><p v-if="error" role="alert" class="world-exports__error">{{ error }}</p></template>
    </ConfirmModal>
  </Panel>
</template>

<style scoped>
.world-exports { display: grid; gap: var(--space-4); font-size: var(--text-sm); line-height: var(--leading-normal); }
.world-exports p { max-width: 75ch; }
.world-exports__actions, .world-exports__archive { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.world-exports__archive { justify-content: space-between; padding-block: var(--space-3); border-bottom: 1px solid var(--border-color); }
.world-exports__archive > div:first-child { flex: 1; min-width: min(240px, 100%); }
.world-exports__review { display: grid; gap: var(--space-3); padding: var(--space-4); background: var(--bg-tertiary); border-radius: var(--radius-sm); }
.world-exports label { display: grid; gap: var(--space-2); }
.world-exports input:not([type=checkbox]), .world-exports select { min-width: 0; width: 100%; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-color); border-radius: var(--radius-sm); }
.world-exports__check { display: flex !important; align-items: start; }
.world-exports code { overflow-wrap: anywhere; }
.world-exports__error { color: var(--danger); }
.world-exports a { color: var(--info); }
.world-exports :is(input, select, a):focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
@media (max-width: 640px) {
  .world-exports__actions > label { width: 100%; }
  .world-exports__archive { align-items: start; }
}
</style>
