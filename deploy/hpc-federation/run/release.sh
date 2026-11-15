#!/bin/bash
# release.sh (login node): release the allocation by its job id, then record the queue as it is. Never `scancel -u`:
# another study's job may belong to the same user. Evidence (release/): scancel.rc.json; squeue-final.json, the raw
# `squeue -u $USER -h -o '%i %j'` (ids and job names only, no node names), which the summary counts: all jobs listed,
# and those whose name starts with hpcop- (ours), which must be 0; sacct.txt, the allocation's elapsed time.
# Usage: E=<evidence dir> bash release.sh
source "$(dirname "$0")/common.sh"
ev release
scancel "$HPC_JOB"
rec rc "$E/release/scancel.rc.json" scancel "$?"
end=$((SECONDS + 120))
while (( SECONDS < end )) && squeue -j "$HPC_JOB" -h 2> /dev/null | grep -q .; do sleep 2; done
out=$(squeue -u "$HPC_USER" -h -o '%i %j')
rc=$?
printf '%s\n' "$out" | rec lines "$E/release/squeue-final.json" squeue_final "$rc"
say "jobs of this user after the release: $(printf '%s\n' "$out" | grep -c .)"
sacct -j "$HPC_JOB" -n -P -o JobID,JobName,State,Elapsed > "$E/release/sacct.txt" 2>&1
cut -c1-120 "$E/release/sacct.txt"
