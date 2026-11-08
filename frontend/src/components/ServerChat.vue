<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { get, post } from '../api/client'
import { useAuthStore } from '../stores/auth'
import AppButton from './ui/AppButton.vue'
import PlayerAvatar from './players/PlayerAvatar.vue'
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
const conversation = computed(() => (state.value?.items || []).map((item, index, items) => {
  return {
    ...item,
    continued: continuesGroup(items[index - 1], item),
    groupEnd: !continuesGroup(item, items[index + 1]),
    own: item.kind === 'fabricator' && item.sender === state.value?.sender,
  }
}))
let generation = 0
let timer
let controller
let liveCursor = 0

function continuesGroup(previous, item) {
  if (!previous || !item || !['player', 'fabricator'].includes(item.kind)
    || previous.kind !== item.kind || previous.sender !== item.sender) return false
  const before = new Date(previous.ts)
  const after = new Date(item.ts)
  const gap = after - before
  return gap >= 0 && gap < 5 * 60 * 1000 && before.toDateString() === after.toDateString()
}
function time(ts) {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function fullTime(ts) {
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString([], {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    second: '2-digit', fractionalSecondDigits: 3, timeZoneName: 'short',
  })
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
      <h2>{{ $t('chat.title') }}</h2>
      <span v-if="state" class="server-chat__state" :class="{ 'is-ready': state.ready && state.supported }">
        <span aria-hidden="true" class="server-chat__status-dot"></span>
        {{ $t(!state.supported ? 'chat.unsupportedLabel' : state.ready ? 'chat.live' : 'chat.offline') }}
      </span>
    </header>
    <div class="server-chat__viewport">
      <div class="server-chat__notices">
        <p v-if="!canRead" class="server-chat__notice">{{ $t('chat.noRead') }}</p>
        <p v-else-if="error" class="server-chat__error" role="alert">{{ error }}</p>
        <template v-else>
          <p v-if="loading" class="server-chat__notice" role="status">{{ $t('chat.loading') }}</p>
          <p v-else-if="!state?.supported" class="server-chat__notice">{{ $t('chat.unsupported') }}</p>
          <p v-else-if="!state.ready" class="server-chat__notice">{{ $t('chat.stopped') }}</p>
          <p v-if="state?.captureErrors" class="server-chat__error" role="alert">{{ $t('chat.captureError') }}</p>
          <p v-if="historyError" class="server-chat__error" role="alert">{{ historyError }}</p>
          <p v-if="sendError" class="server-chat__error" role="alert">{{ sendError }}</p>
        </template>
      </div>
      <div v-if="canRead && !error" ref="transcript" class="server-chat__transcript" role="log" :aria-label="$t('chat.messages')"
        aria-live="polite" aria-relevant="additions" tabindex="0" @scroll="trackScroll">
        <AppButton v-if="state?.nextBefore" variant="ghost" :loading="olderBusy" @click="loadOlder">
          {{ $t('chat.older') }}
        </AppButton>
        <p v-if="state?.supported && !state.items.length" class="server-chat__empty">{{ $t('chat.empty') }}</p>
        <ol v-if="conversation.length" class="server-chat__messages">
          <li v-for="item in conversation" :key="item.id" class="server-chat__message"
            :class="{ 'is-continued': item.continued, 'is-own': item.own }">
            <div class="server-chat__avatar">
              <template v-if="!item.continued">
                <PlayerAvatar v-if="item.kind === 'player'" :name="item.sender" :size="32"
                  :offline-mode="! /^[A-Za-z0-9_]{1,16}$/.test(item.sender)" />
                <span v-else class="server-chat__portal-avatar" aria-hidden="true">F</span>
              </template>
            </div>
            <div class="server-chat__message-body">
              <strong class="server-chat__author" :class="{ 'server-chat__sr-only': item.continued }">
                {{ item.kind === 'fabricator' ? `Fabricator · ${item.sender}` : item.sender }}
              </strong>
              <details class="server-chat__message-details">
                <summary class="server-chat__bubble" tabindex="0" :title="fullTime(item.ts)"
                  :aria-describedby="`server-chat-time-${props.serverId}-${item.id}`">{{ item.message }}</summary>
                <time :id="`server-chat-time-${props.serverId}-${item.id}`" class="server-chat__exact-time"
                  :datetime="item.ts" :aria-label="fullTime(item.ts)">{{ item.ts }}</time>
              </details>
              <div v-if="item.groupEnd || item.status" class="server-chat__meta">
                <time v-if="item.groupEnd" :datetime="item.ts" :title="fullTime(item.ts)">{{ time(item.ts) }}</time>
                <span v-if="item.status">{{ $t(item.status === 'submitted' ? 'chat.submitted'
                  : item.status === 'failed' ? 'chat.failed' : 'chat.unknownOutcome') }}</span>
              </div>
            </div>
          </li>
        </ol>
      </div>
      <AppButton v-if="canRead && !error && !follow" variant="ghost" class="server-chat__latest"
        @click="follow = true; scrollToLatest()">
        {{ $t('chat.latest') }}
      </AppButton>
    </div>
    <form v-if="canRead && canSend" class="server-chat__composer" @submit.prevent="send">
      <label for="server-chat-message" class="server-chat__sr-only">{{ $t('chat.message') }}</label>
      <div class="server-chat__input-row">
        <input id="server-chat-message" v-model="message" type="text" maxlength="512" autocomplete="off"
          :disabled="!allowed" :placeholder="$t('chat.placeholder')" aria-describedby="server-chat-identity" />
        <button type="submit" class="server-chat__send" :disabled="!allowed || !message.trim()"
          :aria-label="$t('chat.send')" :aria-busy="sending">
          <span v-if="sending" class="server-chat__send-spinner" aria-hidden="true"></span>
          <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path d="m21 3-7 18-4-7-7-4 18-7Z" stroke-linejoin="round" />
            <path d="m10 14 11-11" stroke-linecap="round" />
          </svg>
        </button>
      </div>
      <p id="server-chat-identity" class="server-chat__identity">
        {{ state?.sender ? $t('chat.portalIdentity', { name: state.sender }) : $t('chat.portal') }}
      </p>
    </form>
    <p v-else-if="canRead" class="server-chat__read-only">{{ $t('chat.readOnly') }}</p>
  </section>
</template>

<style scoped>
.server-chat { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0;
  background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-md); }
.server-chat__header { flex-shrink: 0; display: flex; justify-content: space-between; align-items: center;
  gap: var(--space-4); padding: var(--space-4) var(--space-5); border-bottom: 1px solid var(--border-color); }
.server-chat h2 { color: var(--text-primary); font-size: var(--text-md); }
.server-chat__state { display: flex; align-items: center; gap: var(--space-2);
  font-size: var(--text-sm); color: var(--text-secondary); white-space: nowrap; }
.server-chat__status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); }
.server-chat__state.is-ready .server-chat__status-dot { background: var(--success); }
.server-chat__viewport { position: relative; flex: 1; display: flex; flex-direction: column; min-height: 0; }
.server-chat__notices { flex-shrink: 0; padding-inline: var(--space-5); }
.server-chat__notices p { margin-top: var(--space-3); font-size: var(--text-sm); line-height: var(--leading-normal); }
.server-chat__notice { color: var(--text-secondary); }
.server-chat__transcript { flex: 1; display: flex; flex-direction: column; min-height: 0;
  overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; overflow-anchor: none;
  padding: var(--space-5) var(--space-5) var(--space-8); }
.server-chat__messages { flex-shrink: 0; margin-top: auto; list-style: none; display: flex;
  flex-direction: column; gap: var(--space-1); }
.server-chat__transcript > :deep(.app-btn) { flex-shrink: 0; align-self: center; margin-bottom: var(--space-4); }
.server-chat__message { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: var(--space-3); }
.server-chat__message + .server-chat__message:not(.is-continued) { margin-top: var(--space-4); }
.server-chat__avatar { width: 32px; min-height: 32px; }
.server-chat__avatar :deep(img) { image-rendering: pixelated; }
.server-chat__portal-avatar { display: flex; align-items: center; justify-content: center; width: 32px; height: 32px;
  border-radius: var(--radius-sm); background: var(--bg-tertiary); color: var(--primary); font-size: var(--text-sm); font-weight: 700; }
.server-chat__message-body { min-width: 0; max-width: min(85%, 65ch); justify-self: start;
  display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-1); }
.server-chat__author { color: var(--text-secondary); font-size: var(--text-sm); overflow-wrap: anywhere; }
.server-chat__bubble { padding: var(--space-2) var(--space-3); border-radius: var(--radius-md);
  background: var(--bg-tertiary); color: var(--text-primary); line-height: var(--leading-normal);
  overflow-wrap: anywhere; cursor: pointer; list-style: none; }
.server-chat__bubble::-webkit-details-marker { display: none; }
.server-chat__bubble:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.server-chat__message-details { max-width: 100%; }
.server-chat__exact-time { display: block; margin-top: var(--space-1); color: var(--text-secondary);
  font-size: var(--text-xs); overflow-wrap: anywhere; }
.server-chat__message.is-own .server-chat__exact-time { text-align: right; }
.server-chat__meta { display: flex; flex-wrap: wrap; gap: var(--space-2); color: var(--text-muted); font-size: var(--text-xs); }
.server-chat__message.is-own { grid-template-columns: minmax(0, 1fr) 32px; }
.server-chat__message.is-own .server-chat__avatar { grid-column: 2; grid-row: 1; }
.server-chat__message.is-own .server-chat__message-body { grid-column: 1; grid-row: 1; justify-self: end; align-items: flex-end; }
.server-chat__message.is-own .server-chat__bubble { background: color-mix(in srgb, var(--primary) 12%, var(--bg-tertiary)); }
.server-chat__empty { margin: auto; color: var(--text-muted); font-size: var(--text-sm); }
.server-chat__composer { flex-shrink: 0; padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--border-color); }
.server-chat__input-row { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-1);
  background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: var(--radius-md); }
.server-chat__input-row input { flex: 1; min-width: 0; height: 44px; padding-inline: var(--space-3);
  border: 0; border-radius: 0; background: transparent; }
.server-chat__input-row input::placeholder { color: var(--text-muted); }
.server-chat__input-row input:focus { outline: none; border-color: transparent; }
.server-chat__input-row:focus-within { outline: 2px solid var(--primary); outline-offset: 2px; }
.server-chat__send { flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 44px; height: 44px;
  border: 0; border-radius: var(--radius-sm); background: var(--primary); color: var(--text-on-primary); cursor: pointer; }
.server-chat__send:hover:not(:disabled) { background: var(--primary-dark); }
.server-chat__send:disabled { opacity: 0.5; cursor: not-allowed; }
.server-chat__send svg { width: 20px; height: 20px; }
.server-chat__send-spinner { width: 20px; height: 20px; border: 2px solid currentColor;
  border-right-color: transparent; border-radius: 50%; animation: chat-spin 0.8s linear infinite; }
.server-chat__identity { margin-top: var(--space-2); color: var(--text-muted); font-size: var(--text-xs); overflow-wrap: anywhere; }
.server-chat__read-only { flex-shrink: 0; padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--border-color); color: var(--text-muted); font-size: var(--text-sm); }
.server-chat__error { color: var(--danger); }
.server-chat__latest { position: absolute; bottom: var(--space-2); left: 50%; transform: translateX(-50%);
  box-shadow: var(--shadow-md); }
.server-chat__transcript:focus-visible, .server-chat__send:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.server-chat__sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
@keyframes chat-spin { to { transform: rotate(1turn); } }
@media (max-width: 600px) {
  .server-chat__header { padding: var(--space-3); }
  .server-chat__transcript { padding: var(--space-3); }
  .server-chat__composer { padding: var(--space-3); }
  .server-chat__notices { padding-inline: var(--space-3); }
  .server-chat__message-body { max-width: 100%; }
  .server-chat__input-row input { font-size: var(--text-md); }
}
@media (prefers-reduced-motion: reduce) {
  .server-chat__send-spinner { animation: none; }
}
</style>
