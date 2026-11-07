<script setup>
import { useRoute } from 'vue-router'
import ActivityTimeline from '../../components/ActivityTimeline.vue'
const route = useRoute()
</script>
<template>
  <ActivityTimeline :server-id="String(route.params.id)" />
</template>
