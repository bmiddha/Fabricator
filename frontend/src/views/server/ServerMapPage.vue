<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppButton from '../../components/ui/AppButton.vue'
import Panel from '../../components/ui/Panel.vue'
import { get, post, put } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { t } from '../../i18n'

const route = useRoute()
const auth = useAuthStore()
const state = ref(null)
const viewer = ref('')
const error = ref('')
const busy = ref(false)
const consent = ref(false)
const configured = ref(false)
const canManage = computed(() => auth.can('map.manage', route.params.id))
const canInstall = computed(() => canManage.value && auth.can('mods.install', route.params.id))
let generation = 0
let poll = null
let expires = 0

async function refresh(force = false) {
  const current = generation
  const base = `/api/servers/${encodeURIComponent(route.params.id)}/map`
  try {
    const result = await get(base)
    if (current !== generation) return
    state.value = result
    error.value = ''
    if (!result.ready) {
      viewer.value = ''
      return
    }
    if (force || !viewer.value || Date.now() >= expires) {
      const session = await post(`${base}/session`)
      if (current !== generation) return
      viewer.value = session.url
      expires = Date.now() + (session.expires_in - 30) * 1000
    }
  } catch (failure) {
    if (current !== generation) return
    error.value = failure.message
    // Never leave last-known player positions visible after a transport failure.
    viewer.value = ''
  }
}

async function act(action, data = {}) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  const current = generation
  const base = `/api/servers/${encodeURIComponent(route.params.id)}/map`
  try {
    if (action === 'enabled') await put(`${base}/enabled`, data)
    else await post(`${base}/${action}`, data)
    if (current !== generation) return
    if (action === 'setup') configured.value = true
    await refresh()
  } catch (failure) {
    if (current === generation) error.value = failure.message
  } finally {
    if (current === generation) busy.value = false
  }
}

watch(() => route.params.id, () => {
  generation++
  clearInterval(poll)
  state.value = null
  viewer.value = ''
  error.value = ''
  consent.value = false
  configured.value = false
  busy.value = false
  refresh()
  poll = setInterval(() => { if (!busy.value) refresh() }, 5000)
}, { immediate: true })

onUnmounted(() => { generation++; clearInterval(poll) })
</script>

<template>
  <div class="map-page">
    <header class="map-page__header">
      <div>
        <h1>{{ t('maps.title') }}</h1>
        <p>{{ t('maps.private') }}</p>
      </div>
      <AppButton variant="ghost" :loading="busy" @click="refresh(true)">{{ t('maps.refresh') }}</AppButton>
    </header>
    <p v-if="error" class="map-page__error" role="alert">{{ error }}</p>
    <p v-if="!state && !error" role="status">{{ t('maps.loading') }}</p>
    <template v-if="state">
      <div v-if="viewer" class="map-page__viewer">
        <p id="map-controls-help">{{ t('maps.controls') }}</p>
        <iframe
          :src="viewer"
          :title="t('maps.viewerTitle')"
          sandbox="allow-scripts"
          referrerpolicy="no-referrer"
          aria-describedby="map-controls-help"
        />
      </div>
      <Panel v-else :title="t('maps.readiness')">
        <div class="map-page__stack" role="status">
          <p>{{ state.message || (state.enabled ? t('maps.waiting') : t('maps.disabled')) }}</p>
          <p v-if="configured">{{ t('maps.restart') }}</p>
          <p v-if="!canManage">{{ t('maps.askOperator') }}</p>
        </div>
      </Panel>
      <details v-if="canManage" :open="!state.enabled" class="map-page__setup">
        <summary>{{ t('maps.setup') }}</summary>
        <div class="map-page__stack">
          <p>{{ state.preview.target }}</p>
          <p v-if="!state.preview.compatible" class="map-page__error">{{ t('maps.unsupported') }}</p>
          <p>{{ t('maps.setupHelp') }}</p>
          <div class="map-page__table">
            <table>
              <thead><tr><th scope="col">{{ t('maps.dependency') }}</th><th scope="col">{{ t('maps.required') }}</th><th scope="col">{{ t('maps.installed') }}</th></tr></thead>
              <tbody>
                <tr v-for="dep in state.preview.dependencies" :key="dep.id">
                  <th scope="row">{{ dep.id }}</th>
                  <td>{{ dep.required }}</td>
                  <td>{{ dep.installed || t('maps.missing') }}{{ dep.installed && !dep.compatible ? ` (${t('maps.incompatible')})` : '' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p>{{ t('maps.adapter') }}</p>
          <div v-if="!state.configured" class="map-page__stack">
            <AppButton v-if="canInstall && state.preview.compatible && !state.preview.dependencies.find(d => d.id === 'bluemap').installed"
              :loading="busy" @click="act('install-bluemap')">{{ t('maps.installBlueMap') }}</AppButton>
            <label class="map-page__consent">
              <input v-model="consent" type="checkbox">
              <span>{{ t('maps.consent') }} <a href="https://www.minecraft.net/eula" target="_blank" rel="noopener noreferrer">{{ t('maps.eula') }}</a></span>
            </label>
            <AppButton :disabled="!consent || !state.preview.ready_to_configure" :loading="busy"
              @click="act('setup', { accept_resource_download: consent })">{{ t('maps.configure') }}</AppButton>
          </div>
          <AppButton v-if="state.enabled" variant="ghost" :loading="busy"
            @click="act('enabled', { enabled: false })">{{ t('maps.disable') }}</AppButton>
          <AppButton v-else-if="state.configured" variant="ghost" :loading="busy"
            @click="act('enabled', { enabled: true })">{{ t('maps.enable') }}</AppButton>
        </div>
      </details>
      <p class="map-page__privacy">{{ t('maps.privacy') }}</p>
    </template>
  </div>
</template>

<style scoped>
.map-page, .map-page__stack { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.map-page__header { display: flex; justify-content: space-between; gap: var(--space-4); align-items: start; }
.map-page h1 { font-size: var(--text-xl); color: var(--text-primary); margin-bottom: var(--space-2); }
.map-page p { line-height: 1.6; max-width: 75ch; }
.map-page__header p, .map-page__privacy { color: var(--text-secondary); font-size: var(--text-sm); }
.map-page__error { color: var(--danger); }
.map-page__viewer { display: flex; flex-direction: column; gap: var(--space-2); }
.map-page iframe { width: 100%; height: max(65vh, 420px); border: 1px solid var(--border-color); border-radius: var(--radius-md); background: var(--bg-secondary); }
.map-page__setup { border-top: 1px solid var(--border-color); padding-top: var(--space-4); }
.map-page summary { cursor: pointer; color: var(--text-primary); padding: var(--space-2) 0; }
.map-page summary:focus-visible, .map-page a:focus-visible, .map-page input:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.map-page__table { overflow-x: auto; }
.map-page table { border-collapse: collapse; width: 100%; font-size: var(--text-sm); text-align: left; }
.map-page th, .map-page td { padding: var(--space-3); border-bottom: 1px solid var(--border-color); overflow-wrap: anywhere; }
.map-page__consent { display: flex; align-items: start; gap: var(--space-3); line-height: 1.6; max-width: 75ch; }
.map-page input { margin-top: 5px; flex-shrink: 0; }
.map-page a { color: var(--text-primary); text-decoration: underline; }
.map-page__stack > .app-btn { align-self: start; }
@media (max-width: 600px) {
  .map-page__header { flex-wrap: wrap; }
  .map-page iframe { height: 65vh; min-height: 360px; }
}
</style>
