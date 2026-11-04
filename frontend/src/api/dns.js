import { get, post, put } from './client'

export const getDns = () => get('/api/dns')
export const configureDns = config => put('/api/dns/config', config)
export const saveDnsRecords = records => put('/api/dns/records', records)
export const previewDns = () => post('/api/dns/plan')
export const syncDns = () => post('/api/dns/sync')
export const getServerDns = id => get(`/api/servers/${encodeURIComponent(id)}/dns`)
export const saveServerDns = (id, binding) => put(`/api/servers/${encodeURIComponent(id)}/dns`, binding)
