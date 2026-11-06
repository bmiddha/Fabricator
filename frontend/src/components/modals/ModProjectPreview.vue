<script setup>
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import AppButton from '../ui/AppButton.vue'
import { getProjectDetails } from '../../api/modrinth'
import { createProjectDetailsCache } from '../../utils/modProjectPreview'
import { createModrinthMarkdownRenderer, safeProjectUrl } from '../../utils/modrinthMarkdown'
import { t } from '../../i18n'

const props = defineProps({
  show: Boolean,
  projectId: { type: String, default: null },
  title: { type: String, default: '' },
  lockScroll: { type: Boolean, default: true },
})
const emit = defineEmits(['close'])
const titleId = useId()
const dialog = ref(null)
const project = ref(null)
const loading = ref(false)
const error = ref('')
let request = 0
let controller
let previousFocus
let previousOverflow
const renderMarkdown = createModrinthMarkdownRenderer(window)
const body = computed(() => renderMarkdown(project.value?.body))
const hasBody = computed(() => typeof project.value?.body === 'string' && !!project.value.body.trim())
const links = computed(() => {
  const p = project.value
  if (!p) return []
  return [
    ['modPreview.modrinth', `https://modrinth.com/project/${encodeURIComponent(p.id || props.projectId)}`],
    ['modPreview.source', p.source_url],
    ['modPreview.issues', p.issues_url],
    ['modPreview.wiki', p.wiki_url],
    ['modPreview.community', p.discord_url],
  ].map(([label, url]) => ({ label, url: safeProjectUrl(url) })).filter((link) => link.url)
})

async function load() {
  const current = ++request
  controller?.abort()
  project.value = null
  error.value = ''
  loading.value = false
  if (!props.show || !props.projectId) return
  controller = new AbortController()
  loading.value = true
  try {
    const result = await loadProject(props.projectId, { signal: controller.signal })
    if (current === request) project.value = result
  } catch (e) {
    if (current === request && e.name !== 'AbortError') error.value = e.message || t('modPreview.failed')
  } finally {
    if (current === request) loading.value = false
  }
}

function restore() {
  if (previousOverflow !== undefined) document.body.style.overflow = previousOverflow
  previousOverflow = undefined
  if (previousFocus?.isConnected) previousFocus.focus()
  previousFocus = null
}

watch(() => [props.show, props.projectId], load, { immediate: true })
function syncDialog(show) {
  if (show) {
    previousFocus = document.activeElement
    if (props.lockScroll) {
      previousOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    dialog.value.showModal()
  } else {
    dialog.value?.close()
    restore()
  }
}
watch(() => props.show, syncDialog, { flush: 'post' })
onMounted(() => { if (props.show) syncDialog(true) })
onBeforeUnmount(() => {
  ++request
  controller?.abort()
  dialog.value?.close()
  restore()
})
</script>

<script>
// Shared between entry points, not populated until a preview is requested.
const loadProject = createProjectDetailsCache(getProjectDetails)
</script>

<template>
  <dialog
    ref="dialog"
    class="mod-preview"
    :aria-labelledby="titleId"
    @cancel.prevent="emit('close')"
    @keydown.esc.stop
    @click="(event) => { if (event.target === dialog) emit('close') }"
  >
    <div class="mod-preview__surface">
      <header class="mod-preview__header">
        <h2 :id="titleId">{{ project?.title || title || $t('modPreview.title') }}</h2>
        <AppButton variant="ghost" :aria-label="$t('common.close')" @click="emit('close')">{{ $t('common.close') }}</AppButton>
      </header>
      <div class="mod-preview__content" :aria-busy="loading">
        <p v-if="!projectId" role="status">{{ $t('modPreview.unidentified') }}</p>
        <p v-else-if="loading" role="status">{{ $t('modPreview.loading') }}</p>
        <div v-else-if="error" role="alert">
          <p>{{ $t('modPreview.failed') }}</p>
          <p>{{ error }}</p>
          <AppButton variant="ghost" @click="load">{{ $t('modPreview.retry') }}</AppButton>
        </div>
        <template v-else-if="project">
          <p v-if="project.description" class="mod-preview__summary">{{ project.description }}</p>
          <nav class="mod-preview__links" :aria-label="$t('modPreview.links')">
            <a v-for="link in links" :key="link.label" :href="link.url" target="_blank" rel="noopener noreferrer">{{ $t(link.label) }}</a>
          </nav>
          <p v-if="project.license?.name" class="mod-preview__license">{{ $t('modPreview.license', { name: project.license.name }) }}</p>
          <h3>{{ $t('modPreview.about') }}</h3>
          <!-- Only the strict allowlisted renderer above may provide this HTML. -->
          <div v-if="hasBody" class="mod-preview__markdown" v-html="body"></div>
          <p v-else>{{ $t('modPreview.empty') }}</p>
        </template>
      </div>
    </div>
  </dialog>
</template>

<style scoped>
.mod-preview {
  padding: 0;
  margin: auto;
  width: min(900px, calc(100% - var(--space-5) * 2));
  max-height: 90dvh;
  color: var(--text-primary);
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overscroll-behavior: contain;
}
.mod-preview::backdrop { background: rgba(0, 0, 0, 0.7); }
.mod-preview__surface { display: flex; flex-direction: column; max-height: 88dvh; }
.mod-preview__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--border-color);
}
.mod-preview__header h2 { margin: 0; font-size: var(--text-md); overflow-wrap: anywhere; }
.mod-preview__content {
  padding: var(--space-5);
  overflow: auto;
  font-size: var(--text-sm);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.mod-preview__summary { margin-top: 0; }
.mod-preview__content > * + * { margin-top: var(--space-4); }
.mod-preview__links { display: flex; flex-wrap: wrap; gap: var(--space-4); }
.mod-preview__content :deep(a) { color: var(--primary); text-decoration: underline; }
.mod-preview__content :deep(a:focus-visible) { outline: 2px solid var(--primary); outline-offset: 3px; }
.mod-preview__license { color: var(--text-secondary); }
.mod-preview__markdown { max-width: 75ch; }
.mod-preview__markdown :deep(p), .mod-preview__markdown :deep(ul),
.mod-preview__markdown :deep(ol), .mod-preview__markdown :deep(pre),
.mod-preview__markdown :deep(table), .mod-preview__markdown :deep(blockquote) { margin-block: var(--space-3); }
.mod-preview__markdown :deep(ul), .mod-preview__markdown :deep(ol) { padding-inline-start: var(--space-5); }
.mod-preview__markdown :deep(h1), .mod-preview__markdown :deep(h2),
.mod-preview__markdown :deep(h3), .mod-preview__markdown :deep(h4),
.mod-preview__markdown :deep(h5), .mod-preview__markdown :deep(h6) { margin-top: var(--space-5); margin-bottom: var(--space-2); }
.mod-preview__markdown :deep(hr) { border: 0; border-top: 1px solid var(--border-color); margin-block: var(--space-5); }
.mod-preview__markdown :deep(img) { max-width: 100%; height: auto; }
.mod-preview__markdown :deep(pre) { overflow-x: auto; padding: var(--space-3); background: var(--bg-tertiary); }
.mod-preview__markdown :deep(table) { display: block; overflow-x: auto; border-collapse: collapse; }
.mod-preview__markdown :deep(th), .mod-preview__markdown :deep(td) { border: 1px solid var(--border-color); padding: var(--space-2); }
.mod-preview__markdown :deep(h1), .mod-preview__markdown :deep(h2) { font-size: var(--text-lg); }
.mod-preview__markdown :deep(h3), .mod-preview__markdown :deep(h4) { font-size: var(--text-md); }
.mod-preview__markdown :deep(blockquote) { margin-inline: var(--space-4); color: var(--text-secondary); }
@media (max-width: 768px) {
  .mod-preview { width: 100%; max-width: 100%; margin: auto 0 0; border-radius: var(--radius-lg) var(--radius-lg) 0 0; }
  .mod-preview__header, .mod-preview__content { padding: var(--space-4); }
}
</style>
