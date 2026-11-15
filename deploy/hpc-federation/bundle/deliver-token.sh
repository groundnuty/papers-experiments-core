#!/bin/bash
# deliver-token.sh: in a step of the PROVIDER's job, read the registration token from stdin and store it
# as rootfs/root/.hpcop/registration-token (0600), where onepanel reads it (tokenProvisionMethod
# fromFile). stdin is the payload, so this is the right-hand side of a pipe from
# mint-registration-token.sh; the token never touches a command line, an environment or shared storage.
# An empty or malformed token (a failed mint upstream) is refused, and so is a second delivery.
# Usage: deliver-token.sh [--workdir W]
source "$(dirname "$0")/lib.sh"
component op
[[ -z ${1:-} || $1 == --workdir ]] || die "usage: deliver-token.sh [--workdir W]"
token=
IFS= read -r -t 600 token || [[ -n $token ]] || die "no token arrived on stdin"
[[ $token =~ ^[A-Za-z0-9_-]{32,4096}$ ]] || die "the token on stdin is empty or malformed (did the mint fail?)"
W=$(wait_workdir op 900 "${2:-}"); S=$(secrets_dir "$W")
end=$((SECONDS + 900))
until [[ -e $W/rootfs.complete && -d $S ]]; do (( SECONDS < end )) || die "node-init.sh never finished in $W"; sleep 5; done
[[ ! -e $S/registration-token ]] || die "a registration token was already delivered for this launch"
put_secret "$S/registration-token" "$token"
unset token
ok "registration token delivered to $W"
