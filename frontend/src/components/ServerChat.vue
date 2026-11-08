<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { get, post } from '../api/client'
import { useAuthStore } from '../stores/auth'
import AppButton from './ui/AppButton.vue'
import { t } from '../i18n'

const props = defineProps({ serverId: { type: String, required: true } })
const auth = useAuthStore()
const canRead = computed(() => auth.can('chat.read', props.serverId))
const canSend = computed(() => auth.can('chat.send', props.serverId))
const state = ref(null)
const message = ref('')
const loading = ref(true)
const sending = ref(false)
const error = ref('')
const sendError = ref('')
const transcript = ref(null)
const follow = ref(true)
const retryUntil = ref(0)
const now = ref(Date.now())
const olderBusy = ref(false)
const historyError = ref('')
const allowed = computed(() => canSend.value && state.value?.canSend && !loading.value
  && !error.value && !sending.value && now.value >= retryUntil.value)
let generation = 0
let timer
let controller
let liveCursor = 0

function time(ts) {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function trackScroll() {
  const el = transcript.value
  follow.value = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 48
}
async function scrollToLatest() {
  await nextTick()
  if (follow.value && transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
}
function mergeItems(items, result) {
  if (result.latestId === 0 && result.earliestId === 0 && !result.items.length) return []
  const byId = new Map(items.map(item => [item.id, item]))
  for (const item of result.items) byId.set(item.id, item)
  for (const outcome of result.outcomes || []) {
    if (byId.has(outcome.id)) byId.set(outcome.id, { ...byId.get(outcome.id), status: outcome.status })
  }
  const oldest = Date.now() - (result.retentionSeconds || 604800) * 1000
  return [...byId.values()].filter(item => Date.parse(item.ts) > oldest
    && (!result.earliestId || item.id >= result.earliestId))
    .sort((a, b) => a.id - b.id).slice(-(result.maxItems || 10000))
}
async function refresh(current = generation) {
  if (!props.serverId || !canRead.value || current !== generation) return
  const url = `/api/servers/${encodeURIComponent(props.serverId)}/chat`
  controller = new AbortController()
  try {
    const after = liveCursor
    const result = await get(url, after ? { after, limit: 200 } : {}, { signal: controller.signal })
    if (current !== generation) return
    const nextBefore = after && state.value ? state.value.nextBefore : result.nextBefore
    state.value = { ...result, items: mergeItems(state.value?.items || [], result), nextBefore }
    if (result.items.length) liveCursor = result.items.at(-1).id
    else if (!after) liveCursor = result.latestId || 0
    error.value = ''
    await scrollToLatest()
  } catch (e) {
    if (current !== generation || e.name === 'AbortError') return
    state.value = null
    liveCursor = 0
    error.value = e.message
  } finally {
    if (current === generation) {
      loading.value = false
      now.value = Date.now()
      timer = setTimeout(() => refresh(current), state.value?.hasMoreAfter ? 0 : 2500)
    }
  }
}
async function loadOlder() {
  if (!state.value?.nextBefore || olderBusy.value) return
  const current = generation
  olderBusy.value = true
  follow.value = false
  historyError.value = ''
  const el = transcript.value
  const height = el?.scrollHeight || 0
  const top = el?.scrollTop || 0
  try {
    const result = await get(`/api/servers/${encodeURIComponent(props.serverId)}/chat`,
      { before: state.value.nextBefore, limit: 200 })
    if (current !== generation || !state.value) return
    state.value.items = mergeItems(state.value.items, result)
    state.value.nextBefore = result.nextBefore
    await nextTick()
    if (el) el.scrollTop = top + el.scrollHeight - height
  } catch (e) {
    if (current !== generation) return
    historyError.value = e.message
    if (e.status === 401 || e.status === 403) { state.value = null; liveCursor = 0; error.value = e.message }
  } finally {
    if (current === generation) olderBusy.value = false
  }
}
async function send() {
  if (!allowed.value) return
  if (!message.value.trim() || [...message.value].length > 256 || /[\p{C}\u2028\u2029\u00a7]/u.test(message.value)) {
    sendError.value = t('chat.invalid')
    return
  }
  const current = generation
  sending.value = true
  sendError.value = ''
  try {
    const result = await post(`/api/servers/${encodeURIComponent(props.serverId)}/chat`, { message: message.value })
    if (current !== generation) return
    message.value = ''
    if (state.value && !state.value.items.some(item => item.id === result.item.id)) {
      state.value.items = mergeItems(state.value.items, { ...state.value, items: [result.item] })
    }
    await scrollToLatest()
  } catch (e) {
    if (current !== generation) return
    sendError.value = e.message
    if (e.status === 401 || e.status === 403) { state.value = null; liveCursor = 0 }
    if (e.status === 409 || e.status === 503) {
      if (state.value) state.value.canSend = false
    }
    if (e.status === 429) retryUntil.value = Date.now() + (e.data?.retryAfter || 60) * 1000
  } finally {
    if (current === generation) sending.value = false
  }
}
watch(() => [props.serverId, auth.revision, canRead.value], () => {
  generation++
  clearTimeout(timer)
  controller?.abort()
  state.value = null
  message.value = ''
  error.value = ''
  sendError.value = ''
  retryUntil.value = 0
  liveCursor = 0
  olderBusy.value = false
  historyError.value = ''
  loading.value = true
  sending.value = false
  follow.value = true
  refresh()
}, { immediate: true })
onUnmounted(() => { generation++; clearTimeout(timer); controller?.abort() })
</script>

<template>
  <section class="server-chat" :aria-label="$t('chat.title')">
    <header class="server-chat__header">
      <div>
        <h2>{{ $t('chat.title') }}</h2>
        <p>{{ $t('chat.intro') }}</p>
      </div>
      <span v-if="state" class="server-chat__state">
        {{ $t(!state.supported ? 'chat.unsupportedLabel' : state.ready ? 'chat.live' : 'chat.offline') }}
      </span>
    </header>
    <p v-if="!canRead" class="server-chat__notice">{{ $t('chat.noRead') }}</p>
    <p v-else-if="error" class="server-chat__error" role="alert">{{ error }}</p>
    <template v-else>
      <p v-if="loading" class="server-chat__notice" role="status">{{ $t('chat.loading') }}</p>
      <p v-else-if="!state?.supported" class="server-chat__notice">{{ $t('chat.unsupported') }}</p>
      <p v-else-if="!state.ready" class="server-chat__notice">{{ $t('chat.stopped') }}</p>
      <p v-if="state?.captureErrors" class="server-chat__error" role="alert">{{ $t('chat.captureError') }}</p>
      <p v-if="historyError" class="server-chat__error" role="alert">{{ historyError }}</p>
      <div ref="transcript" class="server-chat__transcript" role="log" :aria-label="$t('chat.messages')"
        aria-live="polite" aria-relevant="additions" tabindex="0" @scroll="trackScroll">
        <AppButton v-if="state?.nextBefore" variant="ghost" :loading="olderBusy" @click="loadOlder">
          {{ $t('chat.older') }}
        </AppButton>
        <p v-if="state?.supported && !state.items.length" class="server-chat__empty">{{ $t('chat.empty') }}</p>
        <ol v-if="state?.items.length" class="server-chat__messages">
          <li v-for="item in state.items" :key="item.id" class="server-chat__message">
            <div class="server-chat__byline">
              <strong>{{ item.kind === 'fabricator' ? `Fabricator · ${item.sender}` : item.sender }}</strong>
              <time :datetime="item.ts">{{ time(item.ts) }}</time>
              <span v-if="item.status">{{ $t(item.status === 'submitted' ? 'chat.submitted'
                : item.status === 'failed' ? 'chat.failed' : 'chat.unknownOutcome') }}</span>
            </div>
            <p>{{ item.message }}</p>
          </li>
        </ol>
      </div>
      <AppButton v-if="!follow" variant="ghost" class="server-chat__latest" @click="follow = true; scrollToLatest()">
        {{ $t('chat.latest') }}
      </AppButton>
    </template>
    <form v-if="canRead && canSend" class="server-chat__composer" @submit.prevent="send">
      <label for="server-chat-message">{{ $t('chat.message') }}</label>
      <div class="server-chat__input-row">
        <input id="server-chat-message" v-model="message" type="text" maxlength="512" autocomplete="off"
          :disabled="!allowed" :placeholder="$t('chat.placeholder')" aria-describedby="server-chat-identity" />
        <AppButton type="submit" :loading="sending" :disabled="!allowed || !message.trim()">{{ $t('chat.send') }}</AppButton>
      </div>
      <p id="server-chat-identity">{{ state?.sender ? $t('chat.identity', { name: state.sender }) : $t('chat.signIn') }}</p>
      <p v-if="sendError" class="server-chat__error" role="alert">{{ sendError }}</p>
    </form>
    <p v-else-if="canRead" class="server-chat__notice">{{ $t('chat.readOnly') }}</p>
    <p class="server-chat__privacy">{{ $t('chat.privacy') }}</p>
  </section>
</template>

<style scoped>
.server-chat { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.server-chat__header { display: flex; justify-content: space-between; align-items: start; gap: var(--space-4); }
.server-chat h2 { color: var(--text-primary); font-size: var(--text-lg); margin-bottom: var(--space-2); }
.server-chat__header p, .server-chat__notice, .server-chat__privacy,
.server-chat__composer > p { font-size: var(--text-sm); color: var(--text-secondary); line-height: var(--leading-normal); }
.server-chat__state { font-size: var(--text-sm); color: var(--text-secondary); white-space: nowrap; }
.server-chat__transcript { overflow-y: auto; min-height: 240px; max-height: 52vh; padding: var(--space-4);
  background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-md); }
.server-chat__messages { list-style: none; display: flex; flex-direction: column; gap: var(--space-5); }
.server-chat__transcript > :deep(.app-btn) { margin-bottom: var(--space-4); }
.server-chat__byline { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-3);
  font-size: var(--text-sm); color: var(--text-secondary); }
.server-chat__byline strong { color: var(--text-primary); overflow-wrap: anywhere; }
.server-chat__message p { margin-top: var(--space-1); color: var(--text-primary); line-height: var(--leading-normal); overflow-wrap: anywhere; }
.server-chat__empty { color: var(--text-secondary); line-height: var(--leading-normal); max-width: 65ch; }
.server-chat__composer { display: flex; flex-direction: column; gap: var(--space-2); }
.server-chat__composer label { font-size: var(--text-sm); color: var(--text-primary); }
.server-chat__input-row { display: flex; gap: var(--space-2); }
.server-chat__input-row input { flex: 1; min-width: 0; min-height: 40px; }
.server-chat__input-row input::placeholder { color: var(--text-muted); }
.server-chat__input-row :deep(.app-btn) { min-height: 40px; }
.server-chat__error { color: var(--danger) !important; font-size: var(--text-sm); }
.server-chat__latest { align-self: center; }
.server-chat__transcript:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (max-width: 600px) {
  .server-chat__header { flex-direction: column; gap: var(--space-2); }
  .server-chat__transcript { padding: var(--space-3); }
  .server-chat__input-row input { font-size: var(--text-md); }
}
</style>
