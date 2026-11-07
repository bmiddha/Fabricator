<script setup>
import { onBeforeUnmount, ref } from 'vue'
import AppButton from '../components/ui/AppButton.vue'
import { t } from '../i18n'

let token = window.location.hash.slice(1)
window.history.replaceState(null, '', window.location.pathname)
const busy = ref(false)
const error = ref('')
const started = ref(false)
let channel
let timer
const frames = []
onBeforeUnmount(() => { token = ''; channel?.port1.close(); clearTimeout(timer); frames.forEach(frame => frame.remove()) })

async function download() {
  if (busy.value) return
  if (!/^[a-f0-9]{64}\.[A-Za-z0-9_-]{43}$/.test(token)) {
    error.value = t('worldExports.unavailable')
    return
  }
  if (!window.isSecureContext || !navigator.serviceWorker) {
    error.value = t('worldExports.browserRequired')
    return
  }
  busy.value = true
  error.value = ''
  started.value = false
  try {
    await navigator.serviceWorker.register('/world-download-worker.js', { scope: '/world-' })
    if (!navigator.serviceWorker.controller) {
      await new Promise((resolve, reject) => {
        function ready() {
          clearTimeout(timeout)
          resolve()
        }
        const timeout = setTimeout(() => {
          navigator.serviceWorker.removeEventListener('controllerchange', ready)
          reject(new Error(t('worldExports.downloadFailed')))
        }, 15000)
        navigator.serviceWorker.addEventListener('controllerchange', ready, { once: true })
      })
    }
    if (!token) return
    const path = `/world-download-stream/${crypto.randomUUID()}`
    channel?.port1.close()
    clearTimeout(timer)
    channel = new MessageChannel()
    timer = setTimeout(() => { error.value = t('worldExports.downloadFailed'); busy.value = false }, 30000)
    channel.port1.onmessage = ({ data }) => {
      if (data.ready) {
        const frame = document.createElement('iframe')
        frame.hidden = true
        frame.referrerPolicy = 'no-referrer'
        frame.src = path
        document.body.append(frame)
        frames.push(frame)
      } else {
        clearTimeout(timer)
        busy.value = false
        if (data.started) started.value = true
        else error.value = data.error === 404 ? t('worldExports.unavailable')
          : data.error === 429 || data.error === 503 || data.error === 'busy'
            ? t('worldExports.limited') : t('worldExports.downloadFailed')
      }
    }
    navigator.serviceWorker.controller.postMessage({ path, token }, [channel.port2])
  } catch (e) {
    busy.value = false
    error.value = e.message
  }
}
</script>

<template>
  <main class="world-share">
    <section aria-labelledby="world-share-title">
      <img src="/favicon.svg" alt="" width="48" height="48" />
      <h1 id="world-share-title">{{ $t('worldExports.publicTitle') }}</h1>
      <p>{{ $t('worldExports.publicHelp') }}</p>
      <p>{{ $t('worldExports.publicCompatibility') }}</p>
      <p v-if="error" role="alert" class="world-share__error">{{ error }}</p>
      <p v-if="busy" role="status">{{ $t('worldExports.publicWorking') }}</p>
      <p v-if="started" role="status">{{ $t('worldExports.publicStarted') }}</p>
      <AppButton :disabled="busy" :loading="busy" @click="download">{{ $t('worldExports.download') }}</AppButton>
    </section>
  </main>
</template>

<style scoped>
.world-share { min-height: 100dvh; display: grid; place-items: center; padding: var(--space-5); }
.world-share section { width: 100%; max-width: 480px; display: grid; gap: var(--space-4); }
.world-share h1 { font-size: var(--text-xl); color: var(--text-primary); }
.world-share p { line-height: var(--leading-normal); }
.world-share__error { color: var(--danger); }
</style>
