#!/bin/bash
# Recalculate all Firefly III running balances in the Firefly app container.
#
# Runs as root through a passwordless sudo rule for the cron user, so it takes
# no arguments: that user cannot change what runs. Install it root-owned in a
# folder the cron user cannot write (see AGENTS.md, "Root helper").
#
# The container is found by name, so it survives renames (other server, new
# Firefly release): exactly one running "firefly" container that is not one of
# the helper services, else it refuses.

set -euo pipefail

if (($# > 0)); then
	echo "usage: ${0##*/} (no arguments)" >&2
	exit 64
fi

mapfile -t CONTAINERS < <(
	/usr/bin/docker ps --format '{{.Names}}' |
		grep -i 'firefly' |
		grep -v -i -E 'importer|cron|postgres|redis|mysql|mariadb|db' ||
		true
)

if ((${#CONTAINERS[@]} != 1)); then
	echo "Expected exactly one Firefly III app container, found ${#CONTAINERS[@]}: ${CONTAINERS[*]:-none}" >&2
	exit 2
fi

exec /usr/bin/docker exec "${CONTAINERS[0]}" php artisan firefly-iii:refresh-running-balance --force
