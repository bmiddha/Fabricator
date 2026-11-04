#!/bin/sh
set -eu

case "${PDNS_AUTH_API_KEY:-}" in
  ""|*[!a-zA-Z0-9_+=/.-]*)
    echo "POWERDNS_API_KEY must be nonempty and contain only letters, digits, or _+=/.-" >&2
    exit 1
    ;;
esac

exec /usr/bin/tini -- /usr/local/sbin/pdns_server-startup "$@"
