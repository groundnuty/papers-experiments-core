#!/bin/bash
# exec.sh: run one command inside a running service's sandbox, from a control step on its node (hpcfed.py exec).
# It refuses unless the process service.pid records runs. The command runs in a new container on the same rootfs with
# run_service's flags (--containall --no-home --pwd /root) and a per-call --workdir under the work directory, removed
# afterwards. Not --writable: the running service holds the sandbox writable. The new container has its own PID
# namespace but the node's network, so /etc/hpcop-erlang-env gives it the job's epmd port and /root/.erlang.cookie the
# cookie, and Erlang's distribution reaches the running node over loopback. stdin is /dev/null; stdout and stderr
# pass through; the exit status is the command's.
# Usage: exec.sh <op|oz> --workdir W -- <argv...>
source "$(dirname "$0")/lib.sh"
component "${1:-}"; shift
[[ ${1:-} == --workdir && -n ${2:-} && ${3:-} == -- ]] || die "usage: exec.sh <op|oz> --workdir W -- <argv...>"
WD=$2; shift 3
(( $# )) || die "no command given after --"
W=$(find_workdir "$COMP" "$WD")
home_in "$W"   # apptainer's configuration and cache in the work directory, never the account's ~
recorded_service_runs "$W" || die "no running service of this launch in $W: nothing to exec into"
command -v apptainer >/dev/null || die "apptainer not on PATH"
[[ -e $W/rootfs.complete && -d $W/rootfs ]] || die "$W has no complete rootfs"
D=$(mktemp -d "$W/exec.XXXXXX")
cleanup() { chmod -R u+rwX -- "$D" 2>/dev/null || true; rm -rf --one-file-system -- "$D"; }
trap cleanup EXIT
trap 'exit 143' TERM INT
rc=0
apptainer exec --containall --no-home --pwd /root --workdir "$D" "$W/rootfs" /usr/bin/env HOME=/root "$@" < /dev/null || rc=$?
exit "$rc"
