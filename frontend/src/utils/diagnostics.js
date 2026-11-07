export function reportUrl(value) {
  return typeof value === 'string' && /^https:\/\/spark\.lucko\.me\/[A-Za-z0-9]{5,100}$/.test(value) ? value : null
}

export function trend(samples, key, start, end) {
  const points = samples.filter(sample => Number.isFinite(sample[key]))
  if (!points.length) return { paths: [], min: null, max: null, latest: null }
  const values = points.map(sample => sample[key])
  const min = Math.min(...values)
  const max = Math.max(...values)
  const padding = Math.max((max - min) * 0.1, max * 0.01, 0.01)
  const low = Math.max(0, min - padding)
  const high = max + padding
  const paths = []
  let segment = []
  let previous = null
  for (const sample of samples) {
    const value = sample[key]
    if (!Number.isFinite(value) || (previous && (sample.ts - previous.ts > 75 || sample.run !== previous.run))) {
      if (segment.length) paths.push(segment)
      segment = []
    }
    if (Number.isFinite(value)) {
      segment.push({
        x: 48 + ((sample.ts - start) / Math.max(1, end - start)) * 640,
        y: 138 - ((value - low) / (high - low)) * 122,
      })
    }
    previous = sample
  }
  if (segment.length) paths.push(segment)
  const last = samples.at(-1)
  return { paths, min, max, latest: last && end - last.ts <= 75 ? last[key] ?? null : null }
}

export function metricValue(value, unit) {
  if (!Number.isFinite(value)) return null
  if (unit === 'GiB') return (value / 1024 ** 3).toFixed(2)
  return value.toLocaleString(undefined, { maximumFractionDigits: unit === 'count' ? 0 : 1 })
}
