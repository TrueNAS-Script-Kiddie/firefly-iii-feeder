#!/bin/bash

# Start over (AGENTS.md, hard rule "Rebuild from scratch"): wipe what the feeder put into Firefly,
# move data/ aside and put every archived original back into data/incoming/. The cron job does the
# rest, oldest arrival first. By hand, as the owner of this folder (the cron user):
#   sudo -H -u <cron user> bash <app-ds>/firefly-iii-feeder/start-over.bash

shopt -s nullglob

if [[ "$0" == */* ]]; then
	cd "${0%/*}" || exit 1
fi
BASE_DIR="${PWD}"
DATA_DIR="${BASE_DIR}/data"
PREVIOUS_DATA_DIR="${BASE_DIR}/data-before-start-over"
ARCHIVE_DIR="${BASE_DIR}/archive"
APP_ENV="${BASE_DIR}/config/app.env"
LOCKFILE_PATH="${BASE_DIR}/.process.lock"
# As in firefly-iii-feeder.bash: while it exists, every cron run pushes the archive again
ARCHIVE_PUSH_FLAG="${DATA_DIR}/archive-push-pending.flag"

stop() {
	printf 'STOPPED: %s\n' "$1" >&2
	shift
	(($#)) && printf '%s\n' "$@" >&2
	exit 1
}

# A file root creates in data/ or archive/ breaks the cron job, which runs as the folder's owner
if ((EUID != $(stat -c %u "${BASE_DIR}"))); then
	stop "run this as the owner of ${BASE_DIR}:" \
		"  sudo -H -u $(stat -c %U "${BASE_DIR}") bash ${BASE_DIR}/start-over.bash"
fi

# The cron job's lock: a running run finishes first, and none starts until this script ends
exec 9>"${LOCKFILE_PATH}" || exit 1
if ! flock -n 9; then
	echo "Waiting for the running cron run to finish..."
	flock 9 || exit 1
fi

# Read as engine/core/runtime.py load_env does: KEY=VALUE, whitespace around both ignored
FIREFLY_URL="" FIREFLY_TOKEN=""
if [[ -f "${APP_ENV}" ]]; then
	while IFS= read -r line || [[ -n "${line}" ]]; do
		if [[ "${line}" =~ ^[[:space:]]*(FIREFLY_URL|FIREFLY_TOKEN)[[:space:]]*=[[:space:]]*(.*[^[:space:]]) ]]; then
			printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
		fi
	done <"${APP_ENV}"
fi
[[ -n "${FIREFLY_URL}" && -n "${FIREFLY_TOKEN}" ]] || stop "FIREFLY_URL or FIREFLY_TOKEN missing in ${APP_ENV}."
[[ -d "${ARCHIVE_DIR}/.git" ]] || stop "${ARCHIVE_DIR} is not under git: set up its repo first (plan 00 step 2)."

git=(git -c core.quotepath=off -C "${ARCHIVE_DIR}")
# Only what the archive repo holds or the commit below adds: leftovers its .gitignore keeps out
# (Thumbs.db, Office's ~$ lock files) stay behind
mapfile -d '' -t archived < <("${git[@]}" ls-files -z --cached --others --exclude-standard -- originals unprocessed)
wait $! || stop "git ls-files failed in ${ARCHIVE_DIR}; nothing changed."
ORIGINALS=() UNPROCESSED=()
for path in "${archived[@]}"; do
	# Tracked but deleted by hand: the commit below removes it
	[[ -f "${ARCHIVE_DIR}/${path}" ]] || continue
	case "${path}" in
	originals/*) ORIGINALS+=("${ARCHIVE_DIR}/${path}") ;;
	*) UNPROCESSED+=("${ARCHIVE_DIR}/${path}") ;;
	esac
done
# Wiping with nothing to load back would leave Firefly empty
((${#ORIGINALS[@]})) || stop "${ARCHIVE_DIR}/originals/ holds no files: nothing to load back."

cat <<EOF
Start over:
  1. wipe every transaction in Firefly at ${FIREFLY_URL}, and the expense and
     revenue accounts they created (asset accounts, rules and categories stay)
  2. move data/ to data-before-start-over/ (the one there now is deleted)
  3. load back ${#ORIGINALS[@]} files from archive/originals/ and ${#UNPROCESSED[@]} from archive/unprocessed/
EOF
read -r -p 'Type WIPE to continue: ' answer
[[ "${answer}" == WIPE ]] || stop "nothing changed."

# Pending changes in the archive (a hand fix, §3.8 of plan 00) are part of what gets loaded
"${git[@]}" add -A || stop "git add failed in ${ARCHIVE_DIR}; nothing changed."
changes=$("${git[@]}" diff --cached --name-status -M) || stop "git diff failed in ${ARCHIVE_DIR}; nothing changed."
if [[ -n "${changes}" ]]; then
	printf -v now '%(%Y%m%d-%H%M%S)T' -1
	"${git[@]}" commit -q -F - <<<"Before start-over ${now}"$'\n\n'"${changes}" ||
		stop "git commit failed in ${ARCHIVE_DIR}; nothing changed."
	printf 'Committed in the archive:\n%s\n' "${changes}"
fi

# Pushed here, not left to the push flag: that flag lives in data/, which moves aside below, and a
# failure shows on screen now. A failed push only puts the flag in the new data/, so the cron job
# retries silently; the commits are safe locally.
push_failed=0
if ! push_output=$("${git[@]}" push -q origin HEAD 2>&1); then
	push_failed=1
	printf 'Archive push failed; the cron job pushes again every minute:\n%s\n' "${push_output}" >&2
fi

firefly_delete() {
	# The token goes in through stdin, so it never shows in the process list
	curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 -X DELETE -H @- -H 'Accept: application/json' \
		"${FIREFLY_URL%/}/api/v1/data/$1" <<<"Authorization: Bearer ${FIREFLY_TOKEN}"
}
wipe_failed() {
	stop "Firefly answered ${code} to ${1} (401/403: token in ${APP_ENV}; 000: unreachable)." \
		"Firefly may be partly wiped; data/ is untouched. Fix it, then run start-over.bash again."
}
# destroy stops partway with a 504 after about a minute: repeat it until 204
for objects in transactions expense_accounts revenue_accounts; do
	printf 'Wiping %s:' "${objects}"
	while
		code=$(firefly_delete "destroy?objects=${objects}")
		printf ' %s' "${code}"
		[[ "${code}" == 504 ]]
	do :; done
	echo
	[[ "${code}" == 204 ]] || wipe_failed "destroy ${objects}"
done
# Firefly's duplicate check includes deleted transactions: without a purge the reload adds nothing
code=$(firefly_delete purge)
echo "Purging: ${code}"
[[ "${code}" == 204 ]] || wipe_failed purge

after_wipe="Firefly is wiped. Fix it, then run start-over.bash again."
rm -rf "${PREVIOUS_DATA_DIR}" || stop "could not delete ${PREVIOUS_DATA_DIR}." "${after_wipe}"
if [[ -d "${DATA_DIR}" ]]; then
	mv "${DATA_DIR}" "${PREVIOUS_DATA_DIR}" || stop "could not move ${DATA_DIR} aside." "${after_wipe}"
fi
mkdir -p "${DATA_DIR}/incoming" || stop "could not create ${DATA_DIR}/incoming." "${after_wipe}"
# Files that arrived but were not processed yet are not in the archive: they go back in
waiting=("${PREVIOUS_DATA_DIR}"/incoming/*)
if ((${#waiting[@]})); then
	mv -- "${waiting[@]}" "${DATA_DIR}/incoming/" || stop "could not move back ${PREVIOUS_DATA_DIR}/incoming/*." "${after_wipe}"
fi
((push_failed)) && printf '%s\n' "${push_output}" >"${ARCHIVE_PUSH_FLAG}"

# Flat: names start with the arrival run id, so incoming/ sorts in arrival order. A file that is in
# both folders under one name (a reload copy that crashed) is the same export: originals/ wins.
if ((${#UNPROCESSED[@]})); then
	cp -- "${UNPROCESSED[@]}" "${DATA_DIR}/incoming/" || stop "could not copy archive/unprocessed/." "${after_wipe}"
fi
cp -- "${ORIGINALS[@]}" "${DATA_DIR}/incoming/" || stop "could not copy archive/originals/." "${after_wipe}"

waiting=("${DATA_DIR}"/incoming/*)
cat <<EOF
Done: ${#waiting[@]} files in data/incoming/. The cron job loads them from the next minute on
(about an hour for 15,000 rows); alerts come by mail. The previous data/ is in data-before-start-over/.
EOF
