<script setup>
import { onBeforeUnmount, ref } from 'vue'
import AppButton from '../components/ui/AppButton.vue'
import { t } from '../i18n'

// Fragments never travel in HTTP requests. Remove it before any fetch or UI.
let token = window.location.hash.slice(1)
window.history.replaceState(null, '', window.location.pathname)
const busy = ref(false)
const error = ref('')
const downloaded = ref(false)
const controller = new AbortController()
onBeforeUnmount(() => { controller.abort(); token = '' })

async function download() {
  if (busy.value) return
  if (!token) { error.value = t('clientPack.publicUnavailable'); return }
  busy.value = true
  error.value = ''
  try {
    const response = await fetch('/api/client-pack-shares/download', {
      method: 'POST', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
      signal: controller.signal,
    })
    if (!response.ok) {
      if (response.status === 404) throw new Error(t('clientPack.publicUnavailable'))
      if (response.status === 429) throw new Error(t('clientPack.publicLimited', { seconds: response.headers.get('Retry-After') || '60' }))
      throw new Error(t('clientPack.publicFailed'))
    }
    const url = URL.createObjectURL(await response.blob())
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'client-pack.mrpack'
    anchor.referrerPolicy = 'no-referrer'
    document.body.append(anchor)
    anchor.click()
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    downloaded.value = true
  } catch (e) {
    if (e.name !== 'AbortError') error.value = e.message
  } finally { busy.value = false }
}
</script>

<template>
  <main class="pack-share">
    <section class="pack-share__content" aria-labelledby="share-title">
      <img src="/favicon.svg" alt="" width="48" height="48" />
      <h1 id="share-title">{{ $t('clientPack.publicTitle') }}</h1>
      <p>{{ $t('clientPack.publicHelp') }}</p>
      <p v-if="error" role="alert" class="pack-share__error">{{ error }}</p>
      <p v-if="busy" role="status">{{ $t('clientPack.publicWorking') }}</p>
      <p v-if="downloaded" role="status">{{ $t('clientPack.publicDone') }}</p>
      <AppButton :disabled="busy" :loading="busy" @click="download">{{ $t('clientPack.download') }}</AppButton>
    </section>
  </main>
</template>

<style scoped>
.pack-share { min-height: 100dvh; display: grid; place-items: center; padding: var(--space-5); }
.pack-share__content { width: 100%; max-width: 440px; display: grid; gap: var(--space-4); }
.pack-share h1 { font-size: var(--text-xl); color: var(--text-primary); }
.pack-share p { line-height: var(--leading-normal); }
.pack-share__error { color: var(--danger); }
</style>
