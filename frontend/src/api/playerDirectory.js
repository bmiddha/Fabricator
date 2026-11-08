import { get } from './client'

export function getPlayerDirectory(filters) {
  return get(`/api/players?${new URLSearchParams(filters)}`)
}

export function getDirectoryPlayer(id) {
  return get(`/api/players/${encodeURIComponent(id)}`)
}

export function getOnlinePreview(serverId) {
  return get(`/api/servers/${encodeURIComponent(serverId)}/players/preview`)
}
