import { t, te } from '../i18n'

export function eventLabel(action) {
  const key = `activity.actions.${action.replaceAll('.', '_')}`
  return te(key) ? t(key) : action
}

export function eventTarget(event, auth) {
  const id = event.server_id
  if (!id || !auth.can('server.view', id)) return null
  if (event.alert === 'server.crashed' && auth.can('server.logs.read', id)) {
    return { name: 'ServerConsole', params: { id } }
  }
  if (event.action.startsWith('backups.') || event.action.startsWith('worlds.')) {
    if (auth.can('backups.list', id)) return { name: 'ServerBackups', params: { id } }
  }
  if (event.action === 'schedules.run' && auth.can('schedules.read', id)) {
    return { name: 'ServerSchedules', params: { id } }
  }
  return auth.can('activity.read', id)
    ? { name: 'ServerActivity', params: { id }, query: { event: event.id } } : null
}
