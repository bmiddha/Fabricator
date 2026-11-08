import { get } from './client'

export function getPlayerDirectory(filters) {
  return get(`/api/players?${new URLSearchParams(filters)}`)
}

export function getDirectoryPlayer(id) {
  return get(`/api/players/${encodeURIComponent(id)}`)
}
