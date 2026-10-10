#!/bin/bash

shopt -s nullglob extglob

# Runs every minute: until there is work, use builtins only (no subshells, no
# external commands, no Python).
# Every *.csv glob below is !(~\$*).csv: Excel leaves a lock file "~$<name>.csv" next to a CSV
# opened through the share. It is no export, and one left behind would end every idle run.

# Ensure working directory is the project root (cron starts in /)
if [[ "$0" == */* ]]; then
	cd "${0%/*}" || exit 1
fi
BASE_DIR="${PWD}"
IN_DIR="${BASE_DIR}/data/incoming"
NORMALIZED_DIR="${BASE_DIR}/data/normalized"
LOG_DIR="${BASE_DIR}/data/logs"
ARCHIVE_DIR="${BASE_DIR}/archive"
# Originals Python could not archive itself (it archives every other one)
UNPROCESSED_DIR="${ARCHIVE_DIR}/unprocessed"
# A name starting with a run id is a copy from the archive: it keeps that name
ARRIVAL_RUN_ID='^[0-9]{8}-[0-9]{6}-[0-9]{3}-'

PYTHON_MODULE="engine.process_csv"

LOCKFILE_PATH="${BASE_DIR}/.process.lock"
# Set by the importer while a batch import still needs Firefly's follow-up
RECALCULATE_FLAG="${BASE_DIR}/data/firefly-recalculate.flag"
# Set while archive commits wait for a push (Forgejo unreachable); every run retries
ARCHIVE_PUSH_FLAG="${BASE_DIR}/data/archive-push-pending.flag"
UPLOAD_SETTLE_SECONDS=30
INCOMPLETE_LINE_GRACE_SECONDS=600

# Nothing to normalize, import, follow up or push → done
PENDING=("${IN_DIR}"/!(~\$*).csv "${NORMALIZED_DIR}"/!(~\$*).csv)
((${#PENDING[@]})) || [[ -e "${RECALCULATE_FLAG}" ]] || [[ -e "${ARCHIVE_PUSH_FLAG}" ]] || exit 0

# data/ and archive/ are not in git: on a fresh deploy logging and the fallback move would fail
mkdir -p "${LOG_DIR}" "${UNPROCESSED_DIR}" || exit 1

# Ensure Python can import the engine/ package (cron has no PYTHONPATH)
export PYTHONPATH="${BASE_DIR}"

# Prevent double runs. The kernel releases the flock when this process ends,
# so a crash or reboot cannot leave a stale lock behind.
exec 9>"${LOCKFILE_PATH}"
flock -n 9 || exit 0

# Every output of a bank file starts with "<run start>-<file number in this run>", so names are
# unique by construction. A run starting in the same second as an earlier one (a manual run
# right after a cron run) would reuse those numbers: leave the work to the next minute.
printf -v RUN_START '%(%Y%m%d-%H%M%S)T' -1
TAKEN=("${LOG_DIR}/${RUN_START}-"*)
((${#TAKEN[@]})) && exit 0
FILE_NUMBER=0

# Append to the log of the current file, whatever name Python gave it ("<run>.log" until then)
run_log() {
	local logs=("${LOG_DIR}/${RUN_ID}"-*.log) now
	printf -v now '%(%F %T)T' -1
	echo "${now} $*" >>"${logs[0]:-${LOG_DIR}/${RUN_ID}.log}"
}

# Original Python left in incoming/ → archive/unprocessed/ as "<run>-<name>", or under its own
# name when that starts with a run id; a file of that name there is the same export: replaced.
# Content already there under any name: the copy is dropped, so every export is archived once.
archive_unprocessed() {
	local name="${FILENAME}" archived
	[[ -f "${FILE_PATH}" ]] || return 0
	for archived in "${UNPROCESSED_DIR}"/*; do
		if cmp -s "${FILE_PATH}" "${archived}"; then
			rm -f "${FILE_PATH}" && run_log "Original: ${archived} (already archived, copy dropped)"
			return 0
		fi
	done
	[[ "${name}" =~ ${ARRIVAL_RUN_ID} ]] || name="${RUN_ID}-${name}"
	mv -f "${FILE_PATH}" "${UNPROCESSED_DIR}/${name}" && run_log "Original: ${UNPROCESSED_DIR}/${name}"
}

# Commit everything new in archive/ (this run's originals, hand fixes made through the share) and
# push it. Git output is kept and shown only on failure: the cron job mails anything printed.
archive_to_git() {
	local git=(git -c core.quotepath=off -C "${ARCHIVE_DIR}") output changes kind path new_path summary body
	local -A count=()
	if [[ ! -d "${ARCHIVE_DIR}/.git" ]]; then
		printf 'ARCHIVE NOT UNDER GIT: %s\nOriginals are archived, but not committed or pushed. Set up its git repo first.\n' \
			"${ARCHIVE_DIR}/.git is missing" >&2
		return
	fi
	if ! output=$("${git[@]}" add -A 2>&1) || ! changes=$("${git[@]}" diff --cached --name-status -M 2>&1); then
		printf 'ARCHIVE NOT COMMITTED: git add or diff failed in %s\n%s\n%s\n' "${ARCHIVE_DIR}" "${output}" "${changes}" >&2
		return
	fi

	if [[ -n "${changes}" ]]; then
		while IFS=$'\t' read -r kind path new_path; do
			case "${kind}" in
			A) kind=added ;;
			R*) kind=moved path="${path} -> ${new_path}" ;;
			D) kind=removed ;;
			*) kind=changed ;;
			esac
			count[${kind}]=$((${count[${kind}]:-0} + 1))
			body+=$'\n'"${kind} ${path}"
		done <<<"${changes}"
		for kind in added moved changed removed; do
			[[ -n "${count[${kind}]}" ]] && summary+=", ${count[${kind}]} ${kind}"
		done
		if ! output=$("${git[@]}" commit -q -F - 2>&1 <<<"Run ${RUN_START}: ${summary#, }"$'\n'"${body}"); then
			printf 'ARCHIVE NOT COMMITTED: git commit failed in %s\n%s\n' "${ARCHIVE_DIR}" "${output}" >&2
			return
		fi
	elif [[ ! -e "${ARCHIVE_PUSH_FLAG}" ]]; then
		return
	fi

	# Unpushed commits stay local; the flag makes every next run push again, and alerts only once
	if output=$("${git[@]}" push -q origin HEAD 2>&1); then
		[[ -e "${ARCHIVE_PUSH_FLAG}" ]] && rm -f "${ARCHIVE_PUSH_FLAG}"
		return 0
	fi
	[[ -e "${ARCHIVE_PUSH_FLAG}" ]] ||
		printf 'ARCHIVE PUSH FAILED: %s\n%s\n\nThe commits stay in %s; every cron minute pushes again. No further alerts until a push succeeds.\n' \
			"${ARCHIVE_DIR}" "${output}" "${ARCHIVE_DIR}" >&2
	printf '%s\n' "${output}" >"${ARCHIVE_PUSH_FLAG}"
}

for FILE_PATH in "${IN_DIR}"/!(~\$*).csv; do
	FILENAME="${FILE_PATH##*/}"

	# Avoid processing files still being uploaded; the next cron run retries.
	# ctime, not mtime: Explorer/cp -p preserve an old mtime, but every write and
	# every timestamp change bumps ctime, and nothing can set it back.
	printf -v NOW '%(%s)T' -1
	UNCHANGED_SECONDS=$((NOW - $(stat -c %Z "${FILE_PATH}")))
	if ((UNCHANGED_SECONDS < UPLOAD_SETTLE_SECONDS)); then
		continue
	fi
	# A copy that stopped mid-way usually ends mid-line; bank exports end with a
	# newline. After a while, process anyway so a truly broken file gets reported.
	if [[ -n "$(tail -c 1 "${FILE_PATH}")" ]] && ((UNCHANGED_SECONDS < INCOMPLETE_LINE_GRACE_SECONDS)); then
		continue
	fi

	# Python names the outputs once it knows bank, account and period; until then the
	# log is "<run>.log", and the original's name is on its first line
	FILE_NUMBER=$((FILE_NUMBER + 1))
	printf -v RUN_ID '%s-%03d' "${RUN_START}" "${FILE_NUMBER}"
	run_log "Processing file ${FILENAME}..."

	python3 -m "${PYTHON_MODULE}" "${FILE_PATH}" "${RUN_ID}" "${LOG_DIR}/${RUN_ID}.log"
	EXIT_CODE="${?}"

	case "${EXIT_CODE}" in
	0 | 65 | 75 | 99)
		# Python handled everything → bash does absolutely nothing
		;;

	1)
		# Python crashed before cleanup (traceback on stderr) → bash must move the file
		archive_unprocessed
		run_log "${PYTHON_MODULE} crashed before cleanup (exit code 1)."
		;;

	9[2-7])
		# Critical file operation error; Python already alerted. A file left in
		# incoming would be retried (and alerted) every minute, so move it.
		archive_unprocessed
		run_log "${PYTHON_MODULE}: critical file operation error (exit code ${EXIT_CODE}), see above."
		;;

	*)
		# Unknown exit code → treat as Python crash; nobody else alerted, so stderr
		archive_unprocessed
		run_log "${PYTHON_MODULE} exited with unknown code ${EXIT_CODE}."
		echo "${PYTHON_MODULE} exited with unknown code ${EXIT_CODE} on ${FILENAME} (run ${RUN_ID})." >&2
		;;
	esac
done

# Import everything in data/normalized/ into Firefly III. Still under the flock,
# so a long import never overlaps the next cron run. The importer alerts on
# stderr itself.
NORMALIZED=("${NORMALIZED_DIR}"/!(~\$*).csv)
if ((${#NORMALIZED[@]})) || [[ -e "${RECALCULATE_FLAG}" ]]; then
	python3 -m engine.firefly.import_normalized
fi

# Still under the flock: start-over.bash commits the archive too
archive_to_git
