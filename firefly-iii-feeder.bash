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

	# One timestamp/logfile per csv
	RUN_TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
	LOGFILE_PATH="${LOG_DIR}/${RUN_TIMESTAMP}-${FILENAME%.csv}.log"

	echo "$(date '+%F %T') Processing file ${FILENAME}... " >>"${LOGFILE_PATH}"

	python3 -m "${PYTHON_MODULE}" "${FILE_PATH}" "${RUN_TIMESTAMP}" "${LOGFILE_PATH}"
	EXIT_CODE="${?}"

	case "${EXIT_CODE}" in
	0 | 65 | 75 | 99)
		# Python handled everything → bash does absolutely nothing
		;;

	1)
		# Python crashed before cleanup (traceback on stderr) → bash must move the file
		[[ -f "${FILE_PATH}" ]] && mv "${FILE_PATH}" "${FAILED_DIR}/${RUN_TIMESTAMP}-${FILENAME%.csv}-failed.csv"
		echo "$(date '+%F %T') ${PYTHON_MODULE} crashed before cleanup (exit code 1)." >>"${LOGFILE_PATH}"
		;;

	9[2-7])
		# Critical file operation error; Python already alerted. A file left in
		# incoming would be retried (and alerted) every minute, so move it.
		[[ -f "${FILE_PATH}" ]] && mv "${FILE_PATH}" "${FAILED_DIR}/${RUN_TIMESTAMP}-${FILENAME%.csv}-failed.csv"
		echo "$(date '+%F %T') ${PYTHON_MODULE}: critical file operation error (exit code ${EXIT_CODE}), see above." >>"${LOGFILE_PATH}"
		;;

	*)
		# Unknown exit code → treat as Python crash; nobody else alerted, so stderr
		[[ -f "${FILE_PATH}" ]] && mv "${FILE_PATH}" "${FAILED_DIR}/${RUN_TIMESTAMP}-${FILENAME%.csv}-failed.csv"
		echo "$(date '+%F %T') ${PYTHON_MODULE} exited with unknown code ${EXIT_CODE}." | tee -a "${LOGFILE_PATH}" >&2
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
