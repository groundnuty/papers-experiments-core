#!/bin/bash
# build-rootfs.sh: build one patched rootfs tarball, once per image, in a job on a compute node.
#   - pulls the study's pinned image from docker.onedata.org, with Apptainer's layer cache and temp
#     files in a private mktemp directory under the job's $TMPDIR (not $HOME);
#   - compiles the port shim inside the rootfs with the image's own gcc, against the image's glibc;
#     a failed build stops the script;
#   - runs prep.sh / prep-zone.sh (verified patches), then checks that the preload loads cleanly and
#     that bind() to 443 really lands on 8443;
#   - tars the rootfs (no secrets in it; persistence and posix data left out) to <out.tar>. Any tar
#     error fails the script, and the tar only takes its final name once complete.
# The work directory is removed on exit. Peak memory: the layer cache plus the sandbox (several GB of
# tmpfs, charged to the job's --mem).
# Usage (inside a job): bash build-rootfs.sh <op|oz> <out.tar>
#   IMG=<registry ref> overrides the pinned tag (e.g. an @sha256 digest); IMG=/path/to/image.sif builds from
#   a SIF pulled elsewhere, for nodes without registry egress.
source "$(dirname "$0")/lib.sh"
private_output   # the build log: 0600
component "${1:-}"
OUT=${2:?usage: build-rootfs.sh <op|oz> <out.tar>}
[[ $OUT == /* && -d $(dirname -- "$OUT") ]] || die "<out.tar> must be an absolute path in an existing directory"
IMG=${IMG:-$IMAGE}
if [[ $IMG == /*.sif ]]; then SRC=$IMG; [[ -f $SRC ]] || die "no SIF at $SRC"; else SRC=docker://$IMG; fi
command -v apptainer >/dev/null || die "apptainer not on PATH"

sweep_leftovers   # what an earlier job of ours left in this node's /tmp
W=$(make_workdir "build-$COMP")
trap 'remove_workdir "$W"' EXIT
R=$W/rootfs
export APPTAINER_CACHEDIR=$W/cache APPTAINER_TMPDIR=$W/tmp
mkdir -p -- "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"

say "building the sandbox from $SRC into $R"
apptainer build --sandbox --fix-perms "$R" "$SRC" < /dev/null
rm -rf -- "$APPTAINER_CACHEDIR"   # the layers are no longer needed; free their tmpfs memory now

say "compiling the port shim inside the rootfs (image gcc and glibc)"
install -d -m 700 -- "$R/root/hpcop-build"
install -m 600 -- "$HPCOP_BUNDLE/portshim.c" "$HPCOP_BUNDLE/portshim-owner.h" "$R/root/hpcop-build/"
apptainer exec --containall --no-home --writable --pwd /root/hpcop-build "$R" \
  gcc -shared -fPIC -O2 -fno-stack-protector -Wall -Wextra -Werror -o portshim.so portshim.c < /dev/null \
  || die "the port shim did not compile inside the rootfs"

if [[ $COMP == op ]]; then bash "$HPCOP_BUNDLE/prep.sh" "$R" "$R/root/hpcop-build/portshim.so"
else bash "$HPCOP_BUNDLE/prep-zone.sh" "$R" "$R/root/hpcop-build/portshim.so"; fi
rm -rf -- "$R/root/hpcop-build"

say "checking the preload inside the container"
msg=$(apptainer exec --containall --no-home --pwd / "$R" /bin/true 2>&1 < /dev/null) || die "the container does not start: $msg"
[[ -z $msg ]] || die "the container printed '$msg' (an ld.so preload error means the shim is not loaded)"
probe='import errno, socket, sys
s = socket.socket()
try:
    s.bind((sys.argv[1], int(sys.argv[2]))); print("%s:%d" % s.getsockname())
except OSError as e:
    print("EADDRINUSE" if e.errno == errno.EADDRINUSE else "errno %d" % e.errno)'
got=$(apptainer exec --containall --no-home --pwd / "$R" python3 -c "$probe" 127.0.0.1 443 < /dev/null)
[[ $got == 127.0.0.1:8443 || $got == EADDRINUSE ]] || die "bind() to 443 inside the container gave '$got', not 8443: the shim is not active"
ok "preload clean; bind(443) lands on 8443 ($got)"
got=$(apptainer exec --containall --no-home --pwd / "$R" python3 -c "$probe" 0.0.0.0 8091 < /dev/null)
[[ $got == 127.0.0.1:8091 || $got == EADDRINUSE ]] || die "bind() to 0.0.0.0:8091 inside the container gave '$got', not 127.0.0.1:8091"
ok "bind(0.0.0.0:8091), a private port, lands on loopback ($got)"

say "writing $OUT"
tar -cf "$OUT.partial" --exclude='./volumes/persistence/*' --exclude='./volumes/posix/*' --exclude='./root/.hpcop/*' -C "$R" .
mv -f -- "$OUT.partial" "$OUT"
{ printf 'image %s\nbuilt %s on %s\nsha256 ' "$IMG" "$(date -u +%FT%TZ)" "$(hostname -s)"; sha256sum < "$OUT" | cut -d' ' -f1; } > "$OUT.provenance"
say "built $OUT ($(du -h -- "$OUT" | cut -f1))"
