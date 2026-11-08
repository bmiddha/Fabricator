<script setup>
import { provide, ref } from 'vue'
import { useRouter } from 'vue-router'
import ToastContainer from './components/common/ToastContainer.vue'
import ServerCreateModal from './components/modals/ServerCreateModal.vue'
import ImportServerModal from './components/modals/ImportServerModal.vue'
import S3RecoveryModal from './components/modals/S3RecoveryModal.vue'
import { useToast } from './composables/useToast'
import { useAuthStore } from './stores/auth'
import { t } from './i18n'

const router = useRouter()
const toast = useToast()
const auth = useAuthStore()

// Single writable ref provided down to consumers. Both AppSidebar's
// "No servers yet" chip / ServerSwitcher "Add server" item and Servers.vue's
// empty-state CTA flip this open. Hoisted to App.vue so the modal is
// available regardless of which layout is active.
const showCreateModal = ref(false)
provide('showCreateModal', showCreateModal)
const showImportModal = ref(false)
const showS3Recovery = ref(false)

const openImport = () => {
  showCreateModal.value = false
  showImportModal.value = true
}

const handleImportedServer = (server) => {
  if (server?.id) router.push({ name: 'ServerOverview', params: { id: server.id } })
}

const handleCreateServer = (createdServer) => {
  if (!createdServer) return
  const modpackTitle = createdServer.modpackSelection?.title
  const modpackInstallError = createdServer.modpackInstallError
  if (modpackInstallError) {
    toast.warning(
      t('createServer.createdModpackFailed', { name: createdServer.name, error: modpackInstallError }),
      t('createServer.modpackFailedTitle')
    )
  } else {
    const message = modpackTitle
      ? t('createServer.createdWithModpack', { name: createdServer.name, modpack: modpackTitle })
      : t('createServer.created', { name: createdServer.name })
    toast.success(message, t('createServer.createdTitle'))
  }

  // Always navigate to the new server's overview — works from "/" (where
  // the empty state was the only thing visible) and from any other server's
  // page (a switch to the new context).
  if (createdServer.id) {
    router.push({ name: 'ServerOverview', params: { id: createdServer.id } })
  }
}
</script>

<template>
  <router-view />
  <template v-if="!auth.managed && (auth.isAuthenticated || !auth.enabled)">
    <ServerCreateModal
      v-if="auth.can('servers.create')"
      :show="showCreateModal"
      @close="showCreateModal = false"
      @create="handleCreateServer"
      @import="openImport"
    />
    <ImportServerModal
      v-if="auth.can('servers.import')"
      :show="showImportModal"
      @close="showImportModal = false"
      @imported="handleImportedServer"
      @s3="showImportModal = false; showS3Recovery = true"
    />
    <S3RecoveryModal
      v-if="auth.can('servers.import') && auth.can('servers.create')"
      :show="showS3Recovery"
      @close="showS3Recovery = false"
      @recovered="handleImportedServer"
    />
  </template>
  <ToastContainer />
</template>
