import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

export function useCapabilities() {
  const auth = useAuthStore()
  const route = useRoute()
  return (permission) => auth.can(permission, route.params.id)
}
