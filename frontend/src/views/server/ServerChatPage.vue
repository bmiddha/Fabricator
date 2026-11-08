<script setup>
import { useRoute } from 'vue-router'
import ServerChat from '../../components/ServerChat.vue'
const route = useRoute()
</script>

<template>
  <ServerChat :server-id="String(route.params.id)" />
</template>
