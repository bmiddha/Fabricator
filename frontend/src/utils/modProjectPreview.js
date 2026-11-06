/** Only recorded or content-hash-resolved identities may identify installed files. */
export function installedProjectId(mod) {
  for (const ref of [mod?.modrinth, mod?.modrinthGuess]) {
    const key = ref?.projectId || ref?.slug
    if (typeof key === 'string' && key.trim()) return key
  }
  return null
}

/** Successful details are bounded and short-lived; failures are always retryable. */
export function createProjectDetailsCache(fetchDetails, { ttl = 5 * 60_000, limit = 32, now = Date.now } = {}) {
  const cache = new Map()
  return async (id, options = {}) => {
    const cached = cache.get(id)
    if (cached && cached.expires > now()) return cached.project
    const project = await fetchDetails(id, options)
    if (!options.signal?.aborted) {
      cache.delete(id)
      cache.set(id, { project, expires: now() + ttl })
      while (cache.size > limit) cache.delete(cache.keys().next().value)
    }
    return project
  }
}
