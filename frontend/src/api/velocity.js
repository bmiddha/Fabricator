import { get, post, put } from './client'

const base = id => `/api/servers/${encodeURIComponent(id)}/velocity`
export const getVelocity = id => get(`${base(id)}/config`)
export const saveVelocity = (id, config) => put(`${base(id)}/config`, config)
export const previewForwarding = (id, data) => post(`${base(id)}/forwarding/preview`, data)
export const applyForwarding = (id, data) => post(`${base(id)}/forwarding/apply`, data)
