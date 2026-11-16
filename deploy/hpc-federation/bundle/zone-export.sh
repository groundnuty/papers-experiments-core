#!/bin/bash
# zone-export.sh: what the HPC federation driver's root needs of a running zone, on stdout, in a control step of the
# held job on the zone's node (hpcfed.py up, DESIGN.md section 5):
#   passphrase  the zone panel's emergency passphrase (also the zone admin's password), one line. The driver writes
#               it into the federation root's secrets/zone.passphrase (0600), where fedboot.Bootstrap reads it, as the
#               Compose federation's root holds it. It travels like a registration token: stdout to the driver's pipe,
#               never a file of the node, an argument or the environment; printf is a builtin, so no argv sees it.
#   ca          the Onedata test CA that signed the zone's generated web certificate (public), PEM.
# Usage: zone-export.sh <passphrase|ca> --workdir W
source "$(dirname "$0")/lib.sh"
component oz
what=${1:-}
[[ $what == passphrase || $what == ca ]] || die "usage: zone-export.sh <passphrase|ca> --workdir W"
[[ ${2:-} == --workdir && -n ${3:-} ]] || die "usage: zone-export.sh <passphrase|ca> --workdir W"
W=$(find_workdir oz "$3"); R=$W/rootfs; S=$(secrets_dir "$W")
[[ -e $R/root/service-ready.lock ]] || die "the zone in $W is not ready yet"
if [[ $what == passphrase ]]; then
  read_secret PW "$S/passphrase"
  printf '%s\n' "$PW"
  unset PW
  say "the zone's passphrase was written to stdout"
else
  CA=$R/volumes/persistence_backup/etc/oz_panel/OnedataTestWebServerCa.pem
  [[ -f $CA && ! -L $CA && -s $CA ]] || die "no Onedata test CA at $CA"
  grep -q -- '-----BEGIN CERTIFICATE-----' "$CA" || die "$CA holds no certificate"
  cat -- "$CA"
fi
