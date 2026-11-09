import { get, post, put, del } from './client'

export function getActivity(serverId, before, alerts = false) {
  const endpoint = serverId
    ? `/api/servers/${encodeURIComponent(serverId)}/${alerts ? 'alerts' : 'activity'}`
    : '/api/activity'
  return get(endpoint, { ...(before ? { before } : {}), ...(!serverId && alerts ? { alerts: '1' } : {}) })
}
export const getNotifications = () => get('/api/notifications')
export const getTasks = serverId => get(`/api/servers/${encodeURIComponent(serverId)}/tasks`)
export const saveDestination = (data, id) => id
  ? put(`/api/notifications/destinations/${encodeURIComponent(id)}`, data)
  : post('/api/notifications/destinations', data)
export const deleteDestination = id => del(`/api/notifications/destinations/${encodeURIComponent(id)}`)
export const testDestination = id => post(`/api/notifications/destinations/${encodeURIComponent(id)}/test`)
