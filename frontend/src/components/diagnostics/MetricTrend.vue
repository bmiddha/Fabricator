<script setup>
import { computed } from 'vue'
import { metricValue, trend } from '../../utils/diagnostics'
import { t } from '../../i18n'

const props = defineProps({
  samples: { type: Array, required: true },
  metric: { type: String, required: true },
  unit: { type: String, required: true },
  label: { type: String, required: true },
  start: { type: Number, required: true },
  end: { type: Number, required: true },
})
const data = computed(() => trend(props.samples, props.metric, props.start, props.end))
const value = number => metricValue(number, props.unit) ?? t('diagnostics.unavailable')
const time = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const coordinates = points => points.map(point => `${point.x},${point.y}`).join(' ')
</script>

<template>
  <figure class="metric-trend">
    <figcaption>{{ label }} <strong>{{ value(data.latest) }} <span v-if="data.latest != null">{{ unit }}</span></strong></figcaption>
    <svg v-if="data.paths.length" viewBox="0 0 704 154" preserveAspectRatio="none" role="img" :aria-label="t('diagnostics.chartLabel', { label, min: value(data.min), max: value(data.max), unit })">
      <path d="M48 138H688" class="metric-trend__axis" />
      <template v-for="(points, index) in data.paths" :key="index">
        <polyline :points="coordinates(points)" class="metric-trend__line" />
        <circle v-if="points.length === 1" :cx="points[0].x" :cy="points[0].y" r="2" class="metric-trend__point" />
      </template>
    </svg>
    <div v-if="data.paths.length" class="metric-trend__times"><span>{{ time(start) }}</span><span>{{ time(end) }}</span></div>
    <p v-else class="metric-trend__empty">{{ t('diagnostics.noMeasurement') }}</p>
    <p v-if="data.paths.length" class="metric-trend__range">{{ t('diagnostics.range', { min: value(data.min), max: value(data.max), unit }) }}</p>
  </figure>
</template>

<style scoped>
.metric-trend { min-width: 0; }
figcaption { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--space-2); color: var(--text-secondary); font-size: var(--text-sm); }
strong { color: var(--text-primary); font-variant-numeric: tabular-nums; }
svg { display: block; width: 100%; height: 126px; margin-top: var(--space-3); overflow: visible; }
.metric-trend__times { display: flex; justify-content: space-between; color: var(--text-muted); font-size: var(--text-xs); }
.metric-trend__axis { stroke: var(--text-disabled); fill: none; }
.metric-trend__line { stroke: var(--info); stroke-width: 2; fill: none; vector-effect: non-scaling-stroke; }
.metric-trend__point { fill: var(--info); }
.metric-trend__empty { padding: var(--space-8) 0; color: var(--text-muted); font-size: var(--text-sm); }
.metric-trend__range { color: var(--text-muted); font-size: var(--text-xs); margin-top: var(--space-2); }
</style>
