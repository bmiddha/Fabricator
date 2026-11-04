/**
 * playit.gg Tunnel Store (setup syntax).
 *
 * Single source of truth for the playit agent. There is exactly ONE poller
 * across the whole app: callers ref-count it via subscribe() (returns an
 * unsubscribe), so the interval runs while ≥1 consumer is active and stops the
 * moment the last one unmounts.
 *
 * Response shape (matches backend.playit.routes / agent.get_status):
 *   { status, claim_url, error_reason, binary_trust, tunnels, tunnels_known }
 *
 * `status` is DAEMON/ACCOUNT level:
 *   stopped | provisioning | claiming | starting | running | error | unsupported
 *
 * Per-server state is derived: match a tunnel's local_port to a server's port
 * via tunnelForPort(port). `tunnels_known` is false until the first successful
 * rundata of this run, so the UI can tell "no tunnel for this port" apart from
 * "can't reach the playit API right now".
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from './auth'
import { getPlayitStatus, startPlayit, stopPlayit, resetPlayit } from '../api/playit'
import { t } from '../i18n'

const POLL_MS = 3000

export const usePlayitStore = defineStore('playit', () => {
  // ---------- State ----------
  const status         = ref('stopped')
  const tunnels        = ref([])
  const tunnelsKnown   = ref(false)
  const claimUrl       = ref(null)
  const errorReason    = ref(null)
  // verified | unverified | system | missing — see backend binary_trust().
  const binaryTrust    = ref('verified')

  // Internal — ref-counted poller and a debounce so two consumers mounting in
  // the same tick don't trigger two parallel fetches.
  const _consumers     = ref(0)
  const _pollHandle    = ref(null)
  const _inFlightFetch = ref(null)
  let generation = 0

  function clearState() {
    generation++
    if (_pollHandle.value) clearInterval(_pollHandle.value)
    _pollHandle.value = null
    _consumers.value = 0
    _inFlightFetch.value = null
    _applySnapshot(null)
  }

  // ---------- Getters (daemon-level) ----------
  // `isActive` is the on/off truth for the agent toggle: the daemon is live or
  // being brought up.
  const isActive     = computed(() =>
    ['provisioning', 'claiming', 'starting', 'running'].includes(status.value)
  )
  const isStopped      = computed(() => status.value === 'stopped')
  const isProvisioning = computed(() => status.value === 'provisioning')
  const isClaiming     = computed(() => status.value === 'claiming')
  const isStarting     = computed(() => status.value === 'starting')
  const isRunning      = computed(() => status.value === 'running')
  const isError        = computed(() => status.value === 'error')
  const isUnsupported  = computed(() => status.value === 'unsupported')

  // Trust getters. `missing` is intentionally silent: the agent's own error
  // state already explains an absent binary, and warning twice reads as two
  // separate problems.
  const binaryUnverified = computed(() => binaryTrust.value === 'unverified')
  const binarySystem     = computed(() => binaryTrust.value === 'system')

  /**
   * The tunnel forwarding to `port`, or null. local_port arrives as a JSON
   * number (backend int()-parsed), so a strict === against Number(port) holds.
   * @returns {{local_port:number, address:string|null, disabled_reason:string|null, name:string|null, tunnel_type:string|null}|null}
   */
  function tunnelForPort(port) {
    const p = Number(port)
    if (!Number.isFinite(p)) return null
    return tunnels.value.find(t => t.local_port === p) || null
  }

  // ---------- Internals ----------
  function _applySnapshot(data) {
    status.value         = data?.status ?? 'stopped'
    tunnels.value        = Array.isArray(data?.tunnels) ? data.tunnels : []
    tunnelsKnown.value   = data?.tunnels_known === true
    claimUrl.value       = data?.claim_url    ?? null
    errorReason.value    = data?.error_reason ?? null
    // Defaults to 'verified' so an absent field never flashes a warning.
    binaryTrust.value    = data?.binary_trust ?? 'verified'
  }

  // ---------- Actions ----------
  async function fetch() {
    if (!useAuthStore().can('tunnels.read')) return
    if (_inFlightFetch.value) return _inFlightFetch.value
    const started = generation
    const promise = (async () => {
      try {
        const data = await getPlayitStatus()
        if (started === generation) _applySnapshot(data)
      } catch {
        // Network hiccup — keep stale state. Don't flash an absent card.
      } finally {
        if (started === generation) _inFlightFetch.value = null
      }
    })()
    _inFlightFetch.value = promise
    return promise
  }

  async function mutate(action) {
    const started = generation
    try {
      const data = await action()
      if (started === generation) _applySnapshot(data)
    } catch (err) {
      if (started !== generation) return
      status.value = 'error'
      errorReason.value = err?.message || t('errors.networkShort')
    }
  }

  const start = () => mutate(startPlayit)
  const stop = () => mutate(stopPlayit)
  const reset = () => mutate(resetPlayit)

  /**
   * Subscribe to polling. Returns an unsubscribe function — every component
   * that subscribes must also unsubscribe (typically in onUnmounted) or the
   * poller leaks past the consumer's lifetime.
   */
  function subscribe() {
    const started = generation
    _consumers.value += 1
    if (_consumers.value === 1) {
      // Fire an immediate fetch so the new consumer doesn't have to wait
      // a full POLL_MS for first paint.
      fetch()
      _pollHandle.value = setInterval(fetch, POLL_MS)
    }
    let released = false
    return function unsubscribe() {
      if (released || started !== generation) return
      released = true
      _consumers.value -= 1
      if (_consumers.value <= 0) {
        _consumers.value = 0
        if (_pollHandle.value) {
          clearInterval(_pollHandle.value)
          _pollHandle.value = null
        }
      }
    }
  }

  return {
    // state
    status, tunnels, tunnelsKnown, claimUrl, errorReason, binaryTrust,
    // getters
    isActive, isStopped, isProvisioning, isClaiming, isStarting, isRunning,
    isError, isUnsupported, binaryUnverified, binarySystem,
    tunnelForPort,
    // actions
    fetch, start, stop, reset, subscribe, clearState,
  }
})
