#!/bin/bash
# detach.sh <rc file> <name> <command...>: run the command, then record its exit status as evidence.
# common.sh's `detached` starts this with setsid nohup, so the command (a service step's srun) outlives the shell that
# launched it, and its exit status is not lost: a detached step's status says how the service ended.
set -u
umask 077
rcfile=$1 name=$2
shift 2
cd /
"$@"
rc=$?
python3 -I -B "$(dirname "$0")/record.py" rc "$rcfile" "$name" "$rc"
