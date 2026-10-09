#!/bin/bash

shopt -s nullglob

# Runs every minute: until there is work, use builtins only (no subshells, no
# external commands, no Python).

# Ensure working directory is the project root (cron starts in /)
if [[ "$0" == */* ]]; then
	cd "${0%/*}" || exit 1
fi
BASE_DIR="${PWD}"
IN_DIR="${BASE_DIR}/data/incoming"
NORMALIZED_DIR="${BASE_DIR}/data/normalized"
FAILED_DIR="${BASE_DIR}/data/failed"
LOG_DIR="${BASE_DIR}/data/logs"

PYTHON_MODULE="engine.process_csv"

LOCKFILE_PATH="${BASE_DIR}/.process.lock"
# Set by the importer while a batch import still needs Firefly's follow-up
RECALCULATE_FLAG="${BASE_DIR}/data/firefly-recalculate.flag"
UPLOAD_SETTLE_SECONDS=30
INCOMPLETE_LINE_GRACE_SECONDS=600

# Nothing to normalize, import or follow up → done
PENDING=("${IN_DIR}"/*.csv "${NORMALIZED_DIR}"/*.csv)
((${#PENDING[@]})) || [[ -e "${RECALCULATE_FLAG}" ]] || exit 0

# data/ is not in git: a fresh deploy has no log folder, and logging would fail
mkdir -p "${LOG_DIR}" "${FAILED_DIR}" || exit 1

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

# Append to the log of the current file, whatever name Python gave it
run_log() {
	local logs=("${LOG_DIR}/${RUN_ID}".log "${LOG_DIR}/${RUN_ID}"-*.log) now
	printf -v now '%(%F %T)T' -1
	echo "${now} $*" >>"${logs[0]:-${LOG_DIR}/${RUN_ID}.log}"
}

for FILE_PATH in "${IN_DIR}"/*.csv; do
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
		[[ -f "${FILE_PATH}" ]] && mv "${FILE_PATH}" "${FAILED_DIR}/${RUN_ID}-crashed-${FILENAME}"
		run_log "${PYTHON_MODULE} crashed before cleanup (exit code 1)."
		;;

	9[2-7])
		# Critical file operation error; Python already alerted. A file left in
		# incoming would be retried (and alerted) every minute, so move it.
		[[ -f "${FILE_PATH}" ]] && mv "${FILE_PATH}" "${FAILED_DIR}/${RUN_ID}-move-failed-${FILENAME}"
		run_log "${PYTHON_MODULE}: critical file operation error (exit code ${EXIT_CODE}), see above."
		;;

	*)
		# Unknown exit code → treat as Python crash; nobody else alerted, so stderr
		[[ -f "${FILE_PATH}" ]] && mv "${FILE_PATH}" "${FAILED_DIR}/${RUN_ID}-crashed-${FILENAME}"
		run_log "${PYTHON_MODULE} exited with unknown code ${EXIT_CODE}."
		echo "${PYTHON_MODULE} exited with unknown code ${EXIT_CODE} on ${FILENAME} (run ${RUN_ID})." >&2
		;;
	esac
done

# Import everything in data/normalized/ into Firefly III. Still under the flock,
# so a long import never overlaps the next cron run. The importer alerts on
# stderr itself.
NORMALIZED=("${NORMALIZED_DIR}"/*.csv)
if ((${#NORMALIZED[@]})) || [[ -e "${RECALCULATE_FLAG}" ]]; then
	python3 -m engine.firefly.import_normalized
fi
