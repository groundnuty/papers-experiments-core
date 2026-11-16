#!/bin/bash
# node-init.sh: prepare one service on this compute node, from inside its job (zone.sbatch and
# provider.sbatch call it; it needs no input but the tarball):
#   1. refuse to run unless /proc hides other users' processes (hidepid): a few image residuals put a
#      secret on a command line for a moment (README, "Not fixable in our scripts");
#   2. remove what an earlier job of ours left in the node's /tmp (a SIGKILL can stop its cleanup);
#      a port check (the start repeats it), then a new private work directory:
#      mktemp -d "$TMPDIR/hpcop-<op|oz>.XXXXXX", mode 0700, refusing to run otherwise, owned (owner.pid)
#      by the batch script that runs the job (HPCOP_OWNER_PID);
#   3. the tarball: ours, not writable by others, and the sha256 its .provenance records, hashed and
#      unpacked from the same open file (any tar error is fatal);
#   4. prep.sh / prep-zone.sh again (the overlays live in the persistence directory the tar leaves out);
#   5. per-launch secrets, generated here and kept in 0600 files under rootfs/root/.hpcop (0700):
#      the onepanel emergency passphrase (also the zone admin's password), a random Erlang cookie written
#      to every service's $HOME/.erlang.cookie, and a random Couchbase administrator in an onepanel
#      overlay (its password: 24 alphanumerics, the most couchbase-cli 4.5.1 accepts);
#   6. check-defaults.sh (files): fails if any image default is still in place.
# Prints the work directory on stdout (a path, not a secret); everything else goes to stderr.
# Usage: node-init.sh <op|oz> <rootfs.tar>
source "$(dirname "$0")/lib.sh"
component "${1:-}"
TAR=${2:?usage: node-init.sh <op|oz> <rootfs.tar>}
# Every tool this start needs from the node, up front: one missing fails it now, named, before anything is made.
tools=(findmnt sha256sum tar df stat install mktemp od "$HPCOP_PY")
if [[ ${HPCOP_SKIP_PORTCHECK:-0} != 1 ]]; then tools+=(ss); fi
need_tools "${tools[@]}"

hidepid_ok() {  # the effective /proc mount hides other users' processes (hidepid=1, 2 or 4)
  local o
  o=$(findmnt -n -o OPTIONS --mountpoint /proc | tail -n 1)
  [[ ,$o, =~ ,hidepid=(1|2|4|noaccess|invisible|ptraceable), ]]
}
hidepid_ok || die "/proc is mounted without hidepid: other users could read this service's command lines; refusing to run"
ok "/proc hides other users' processes ($(findmnt -n -o OPTIONS --mountpoint /proc | tail -n 1 | tr ',' '\n' | grep -E '^(hidepid|gid)=' | tr '\n' ' '))"
sweep_leftovers

if [[ ${HPCOP_SKIP_PORTCHECK:-0} == 1 ]]; then say "port check skipped (offline test)"; else ports_free "$COMP"; fi

open_tarball() {  # <tar>: open it on fd 3 and prove that file is ours, private to us, and the one we built
  local t=$1 p=$1.provenance d want got
  [[ -f $t && ! -L $t ]] || die "$t is not a regular file"
  d=$(dirname -- "$t")
  [[ -O $d || $(stat -c %u -- "$d") == 0 ]] || die "$d (holding the tarball) is owned by uid $(stat -c %u -- "$d")"
  if (( 8#$(mode_of "$d") & 8#022 )) && [[ ! -k $d ]]; then die "$d (holding the tarball) is writable by others"; fi
  [[ -f $p && ! -L $p && -O $p ]] || die "no .provenance of yours beside $t (build-rootfs.sh writes it)"
  if (( 8#$(mode_of "$p") & 8#022 )); then die "$p is writable by others"; fi
  want=$(sed -n 's/^sha256 \([0-9a-f]\{64\}\)$/\1/p' "$p")
  [[ $want =~ ^[0-9a-f]{64}$ ]] || die "no sha256 line in $p"
  exec 3< "$t"
  # From here on only the open file counts: a path swapped after this point is never read.
  [[ $(stat -L -c %u /dev/fd/3) == "$(id -u)" ]] || die "$t is not owned by you"
  if (( 8#$(stat -L -c %a /dev/fd/3) & 8#022 )); then die "$t is writable by others"; fi
  got=$(sha256sum < /dev/fd/3 | cut -d' ' -f1)
  [[ $got == "$want" ]] || die "$t has sha256 $got, but its .provenance records $want"
  ok "tarball is yours, not writable by others, sha256 $got as built ($(sed -n 's/^image //p' "$p"))"
}
open_tarball "$TAR"

W=$(make_workdir "$COMP")
trap 'remove_workdir "$W"' EXIT   # a failed init leaves nothing behind; cleared on success
R=$W/rootfs
install -d -m 700 -- "$R" "$W/tmp"
check_tmpdir
findmnt -no OPTIONS --target "$W" | grep -qw noexec && die "$W is on a noexec mount"
need=$(( $(stat -L -c %s /dev/fd/3) / 1024 / 1024 / 1024 + 4 ))
avail=$(df --output=avail -BG -- "$W" | tail -n 1 | tr -dc 0-9)
(( avail >= need )) || die "only ${avail}G free under $TMPDIR, need ${need}G (and it counts against --mem)"

say "unpacking $TAR into $R"
tar -xf /dev/fd/3 -C "$R"
exec 3<&-
: > "$W/rootfs.complete"

SHIM=$R/usr/local/lib/hpcop-portshim.so
if [[ $COMP == op ]]; then bash "$HPCOP_BUNDLE/prep.sh" "$R" "$SHIM"
else bash "$HPCOP_BUNDLE/prep-zone.sh" "$R" "$SHIM"; fi

# Per-launch secrets. Values live only in shell variables and 0600 files.
S=$(secrets_dir "$W")
install -d -m 700 -- "$S"
# The Couchbase password: 24 alphanumerics (142.9 bits), the most couchbase-cli 4.5.1 accepts; a longer one
# leaves Couchbase with no administrator (live run 3; cb_cred_ok in lib.sh has the limits and their sources).
pass=$(gen_hex 24); cookie=$(gen_hex 32); cb_user=cb$(gen_hex 6); cb_pass=$(gen_alnum "$CB_PASSWORD_LEN")
cb_cred_ok "$cb_user" "$cb_pass" || die "the generated Couchbase administrator is outside couchbase-cli's limits"
put_secret "$S/passphrase" "$pass"
put_secret "$S/erlang-cookie" "$cookie"
put_secret "$S/couchbase-user" "$cb_user"
put_secret "$S/couchbase-password" "$cb_pass"
for home in /root "/var/lib/$PANEL" "/var/lib/$WORKER" /var/lib/cluster_manager; do
  [[ -d $R$home && ! -L $R$home ]] || die "$home is not a directory in the rootfs"
  [[ ! -e $R$home/.config/erlang/.erlang.cookie ]] || die "$home/.config/erlang/.erlang.cookie would shadow ours"
  put_secret "$R$home/.erlang.cookie" "$cookie"   # $HOME of the stock launcher and of each relx service
done
write_cb_overlay "$R" "$cb_user" "$cb_pass"
unset pass cookie cb_user cb_pass
ok "per-launch secrets written (0600, under $S)"

bash "$HPCOP_BUNDLE/check-defaults.sh" "$COMP" --workdir "$W" >&2
trap - EXIT
printf '%s\n' "$W"
