// Route requirements describe entry to a surface, not every action within it.
export const ROUTE_PERMISSIONS = {
  ServerOverview: ['server.view'],
  ServerConsole: ['server.logs.read', 'server.console.execute'],
  ServerPlayers: ['players.read', 'players.kick', 'players.bans.manage', 'players.ops.manage', 'players.whitelist.manage', 'players.known.delete'],
  ServerMods: ['mods.read', 'mods.install', 'modpacks.install'],
  ServerFiles: ['files.list', 'files.read', 'files.write', 'files.upload', 'files.delete', 'files.create_directory'],
  ServerBackups: ['backups.list', 'backups.create', 'backups.config.read', 'backups.config.write', 'worlds.import'],
  ServerSchedules: ['schedules.read', 'schedules.create'],
  ServerPlayit: ['tunnels.read', 'tunnels.manage'],
  ServerSettings: ['server.settings.read', 'server.settings.write', 'server.icon.write', 'server.delete', 'server.upgrade']
}

export function canVisit(auth, name, serverId) {
  const permissions = ROUTE_PERMISSIONS[name]
  return !permissions || permissions.some(permission => auth.can(permission, serverId))
}

export function safeReturn(raw) {
  if (typeof raw !== 'string') return '/'
  let decoded
  try {
    decoded = decodeURIComponent(raw)
  } catch {
    return '/'
  }
  return /^\/(?!\/)/.test(raw) && !decoded.startsWith('//')
    && !/[\\\u0000-\u0020\u007f]/.test(decoded)
    && !/^\/(?:api|login|setup)/.test(decoded) ? raw : '/'
}
