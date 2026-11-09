<script setup>
import { computed, onMounted, onUnmounted, watch } from 'vue'
import AppButton from '../../components/ui/AppButton.vue'
import BackupStatsStrip from '../../components/backups/BackupStatsStrip.vue'
import TaskStatus from '../../components/TaskStatus.vue'
import SnapshotsTable from '../../components/backups/SnapshotsTable.vue'
import ManageConfigsPanel from '../../components/backups/ManageConfigsPanel.vue'
import RestoreModeModal from '../../components/modals/RestoreModeModal.vue'
import DeleteBackupConfigModal from '../../components/modals/DeleteBackupConfigModal.vue'
import QuickBackupModal from '../../components/modals/QuickBackupModal.vue'
import ImportWorldModal from '../../components/modals/ImportWorldModal.vue'
import WorldExportsPanel from '../../components/WorldExportsPanel.vue'
import ConfirmModal from '../../components/modals/ConfirmModal.vue'
import { useBackupsStore } from '../../stores/backups'
import { useAuthStore } from '../../stores/auth'
import { useServerStore } from '../../stores/server'
import { useCapabilities } from '../../composables/useCapabilities'

const store = useBackupsStore()
const auth = useAuthStore()
const serverStore = useServerStore()
const can = useCapabilities()

// `label` is an i18n key.
const TYPE_FILTERS = [
  { value: 'all',     label: 'backups.filters.all'     },
  { value: 'backup',  label: 'backups.filters.backup'  },
  { value: 'safety',  label: 'backups.filters.safety'  },
  { value: 'restore', label: 'backups.filters.restore' },
  { value: 'import',  label: 'backups.filters.import'  }
]

const configsById = computed(() => {
  const map = {}
  for (const c of store.configs) map[c.id] = c
  return map
})

const filtered = computed(() => store.filteredSnapshots)

const busySnapshotId = computed(() => {
  if (store.activeJob?.active && store.activeJob?.kind === 'restore') return store.activeJob.snapshotId
  return null
})

function onRestoreRequest(snapshot) {
  store.requestRestoreSnapshot(snapshot)
}

function onSnapshotDeleteRequest(snapshot) {
  store.requestDeleteSnapshot(snapshot)
}

function onConfigDeleteRequest(config) {
  store.requestDeleteConfig(config)
}

const configBeingDeleted = computed(() => store.configToDelete)
const snapshotsForDeleteModal = computed(() => {
  const id = configBeingDeleted.value?.id
  if (!id) return []
  return store.snapshotsByConfigId.get(id) || []
})

watch(
  () => serverStore.currentServerId,
  (id, oldId) => {
    if (!id || id === oldId) return
    if (auth.managed) return
    store.resetState()
    store.loadAll()
  }
)

onMounted(() => {
  if (auth.managed) return
  store.resetState()
  store.loadAll()
})

onUnmounted(() => {
  store.stopJobPolling()
})
</script>

<template>
  <div v-if="!auth.managed" class="backups-page">
    <!-- Top toolbar -->
    <header class="backups-page__toolbar">
      <div class="backups-page__title-block">
        <h2 class="backups-page__title">{{ $t('nav.backups') }}</h2>
        <p class="backups-page__sub">{{ $t('backups.subtitle') }}</p>
      </div>
      <div class="backups-page__top-actions">
        <AppButton
          v-if="can('backups.list') && store.snapshots.some(s => s.destination?.type === 's3')"
          variant="ghost"
          size="md"
          :loading="store.snapshotsLoading"
          :disabled="store.snapshotsLoading || store.activeJob?.active"
          @click="store.loadSnapshots(true)"
        >{{ $t('backups.checkRemote') }}</AppButton>
        <AppButton v-if="can('backups.config.read') || can('backups.config.write')" variant="ghost" size="md" @click="store.openCreateConfig()">{{ $t('backups.schedules') }}</AppButton>
        <AppButton
          v-if="can('worlds.import')"
          variant="ghost"
          size="md"
          :disabled="store.taskBusy || store.importUploading"
          @click="store.openImportWorldModal()"
        >
          {{ $t('backups.importWorld') }}
        </AppButton>
        <AppButton
          v-if="can('backups.create')"
          variant="primary"
          size="md"
          :disabled="store.taskBusy"
          @click="store.openQuickBackupModal()"
        >
          {{ $t('backups.quickBackup') }}
        </AppButton>
      </div>
    </header>

    <WorldExportsPanel v-if="can('worlds.export') || can('worlds.download')"
      :server-id="serverStore.currentServerId" :server="serverStore.server" />

    <!-- Stats strip -->
    <BackupStatsStrip v-if="can('backups.list')" :summary="store.summary" :loading="store.summaryLoading" />

    <!-- Active job pill -->
    <TaskStatus :server-id="String(serverStore.currentServerId || '')"
      :actions="['backups.create', 'worlds.restore', 'worlds.import']"
      @update="store.syncTasks" @unavailable="store.markTasksUnavailable" />

    <!-- Retained files banner (after non-purge delete) -->
    <div v-if="store.retainedFilesBanner" class="backups-page__retained">
      <div class="backups-page__retained-header">
        <strong>{{ $t('backups.retainedTitle') }}</strong>
        <button
          type="button"
          class="backups-page__retained-dismiss"
          @click="store.dismissRetainedFilesBanner()"
        >
          {{ $t('common.dismiss') }}
        </button>
      </div>
      <i18n-t
        keypath="backups.retainedText"
        tag="p"
        class="backups-page__retained-text"
        :plural="store.retainedFilesBanner.retainedFiles"
      >
        <template #name><code>{{ store.retainedFilesBanner.configName }}</code></template>
      </i18n-t>
      <ul class="backups-page__retained-paths">
        <li v-for="(path, i) in store.retainedFilesBanner.retainedPaths" :key="i">
          <code>{{ path }}</code>
        </li>
      </ul>
    </div>

    <!-- Filters + search -->
    <div class="backups-page__filters">
      <div class="backups-page__type-pills" role="tablist" :aria-label="$t('backups.filterByType')">
        <button
          v-for="opt in TYPE_FILTERS"
          :key="opt.value"
          type="button"
          class="backups-page__pill"
          :class="{ 'is-active': store.typeFilter === opt.value }"
          @click="store.typeFilter = opt.value"
        >
          {{ $t(opt.label) }}
        </button>
      </div>

      <select
        v-model="store.configFilter"
        class="backups-page__config-select"
        :aria-label="$t('backups.filterByConfig')"
      >
        <option value="all">{{ $t('backups.allConfigs') }}</option>
        <option value="__manual__">{{ $t('backups.manualNoConfig') }}</option>
        <option v-for="c in store.sortedConfigs" :key="c.id" :value="c.id">{{ c.name }}</option>
      </select>

      <div class="backups-page__search">
        <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true">
          <circle cx="6" cy="6" r="4"/>
          <path d="M9 9l3 3"/>
        </svg>
        <input
          v-model="store.search"
          type="search"
          :placeholder="$t('backups.searchPlaceholder')"
          :aria-label="$t('backups.searchLabel')"
        />
      </div>
    </div>

    <SnapshotsTable
      v-if="can('backups.list')"
      :snapshots="filtered"
      :configs-by-id="configsById"
      :server-id="serverStore.currentServerId"
      :busy-snapshot-id="busySnapshotId"
      :task-busy="store.taskBusy"
      :loading="store.snapshotsLoading"
      @restore="onRestoreRequest"
      @delete="onSnapshotDeleteRequest"
    />

    <!-- Manage configs slide-in -->
    <ManageConfigsPanel
      :show="store.showManagePanel"
      @close="store.closeManagePanel()"
      @request-delete-config="onConfigDeleteRequest"
    />

    <!-- Restore-mode modal (page-level — page is the long-running view) -->
    <RestoreModeModal
      :show="store.showRestoreModal"
      :snapshot="store.snapshotToRestore"
      :active-job="store.activeJob"
      @confirm="store.confirmRestore"
      @cancel="store.cancelRestore()"
    />

    <!-- Delete-config modal (explicit purge checkbox, unchecked by default) -->
    <DeleteBackupConfigModal
      :can-purge="can('backups.delete')"
      :show="store.showDeleteConfigModal"
      :config="store.configToDelete"
      :config-snapshots="snapshotsForDeleteModal"
      :loading="store.deletingConfig"
      @confirm="store.confirmDeleteConfig"
      @cancel="store.cancelDeleteConfig()"
    />

    <!-- Quick backup modal (config-free one-off) -->
    <QuickBackupModal
      :show="store.showQuickBackupModal"
      :active-job="store.activeJob"
      :default-storage-path="store.defaultStoragePath"
      @confirm="store.runQuickBackup"
      @cancel="store.closeQuickBackupModal()"
    />

    <!-- Import world modal (upload an archive to replace the active world) -->
    <ImportWorldModal
      :show="store.showImportWorldModal"
      :uploading="store.importUploading"
      :upload-pct="store.importUploadPct"
      :active-job="store.activeJob"
      @confirm="store.importWorld"
      @cancel="store.closeImportWorldModal()"
      @cancel-upload="store.cancelImportUpload()"
    />

    <!-- Snapshot delete confirmation (simple) -->
    <ConfirmModal
      :show="store.showDeleteSnapshotModal"
      :title="$t('backups.deleteSnapshot.title')"
      :message="store.snapshotToDelete ? $t('backups.deleteSnapshot.message', { name: store.snapshotToDelete.fileName || store.snapshotToDelete.id }) : ''"
      :description="$t('backups.deleteSnapshot.description')"
      type="danger"
      :confirm-text="$t('common.delete')"
      :loading="store.deletingSnapshot"
      @confirm="store.confirmDeleteSnapshot()"
      @cancel="store.cancelDeleteSnapshot()"
    />
  </div>
</template>

<style scoped>
.backups-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-width: 0;
  max-width: 100%;
}

.backups-page__toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.backups-page__title-block {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.backups-page__title {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.3px;
}

.backups-page__sub {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.backups-page__top-actions {
  display: flex;
  gap: var(--space-2);
}

.backups-page__retained {
  border: 1px solid var(--warning-dark);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  background: rgba(245, 158, 11, 0.07);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.backups-page__retained-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--warning);
  font-size: var(--text-sm);
}

.backups-page__retained-dismiss {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-muted);
  font: inherit;
  font-size: var(--text-xs);
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.backups-page__retained-dismiss:hover {
  border-color: var(--text-muted);
  color: var(--text-primary);
}

.backups-page__retained-text {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.backups-page__retained-text code {
  font-family: var(--font-mono);
  color: var(--text-primary);
}

.backups-page__retained-paths {
  margin: 0;
  padding: 0 0 0 var(--space-4);
  list-style: disc;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.backups-page__retained-paths code {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.backups-page__filters {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.backups-page__type-pills {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-pill);
}

.backups-page__pill {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--text-xs);
  padding: 4px var(--space-3);
  border-radius: var(--radius-pill);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.backups-page__pill:hover {
  color: var(--text-primary);
}

.backups-page__pill.is-active {
  background: var(--bg-tertiary);
  color: var(--text-primary);
}

.backups-page__config-select {
  font-size: var(--text-xs);
  padding: 4px var(--space-2);
  height: 28px;
  max-width: 220px;
}

.backups-page__search {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
  min-width: 200px;
  max-width: 360px;
  padding: 4px var(--space-3);
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-pill);
  color: var(--text-muted);
}

.backups-page__search:focus-within {
  border-color: var(--primary);
}

.backups-page__search input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  padding: 2px 0;
  font-size: var(--text-xs);
  color: var(--text-primary);
}
@media (max-width: 768px) {
  /* The toolbar already wraps; this just stops the two top actions from
     stacking into a narrow column once they land on their own row. */
  .backups-page__top-actions {
    flex: 1;
  }

  .backups-page__top-actions > * {
    flex: 1;
  }
}
</style>
