# shellcheck shell=bash
# shellcheck disable=SC2016  # the $ in the sed and grep patterns below are literal text
# patch.sh: the rootfs patches shared by prep.sh (Oneprovider) and prep-zone.sh (Onezone). Source it
# after lib.sh, with R set to the rootfs and component() called.
#
# Every patch is idempotent and verified: after each sed or awk the expected text is counted, and a
# patch whose target has changed in the image fails loudly instead of passing as a silent no-op.

count() { grep -cE -- "$2" "$1" || true; }   # <file> <ERE>: number of matching lines
count_line() { grep -cxF -- "$2" "$1" || true; }   # <file> <exact line>: number of such lines
assert_line() {  # <file> <n> <exact line> <what>
  local n
  n=$(count_line "$1" "$3")
  [[ $n == "$2" ]] || die "prep: $4: expected $2 line(s) '$3' in ${1#"$R"}, found $n (image changed?)"
  ok "$4"
}
nlines() { awk 'END { print NR }' "$1"; }   # counts a last line without a newline too
is_elf() { [[ -f $1 && $(head -c 4 -- "$1" | od -An -c | tr -d ' ') == 177ELF ]]; }

# Every Erlang VM start path sources this line: `service` runs init scripts under env -i, so the
# distribution interface and the job's epmd port cannot come from the launcher's environment.
ERLENV_LINE="if [ -r $HPCOP_ERLANG_ENV ]; then . $HPCOP_ERLANG_ENV; fi"
assert_count() {  # <file> <n> <ERE> <what>
  local n
  n=$(count "$1" "$3")
  [[ $n == "$2" ]] || die "prep: $4: expected $2 line(s) matching /$3/ in ${1#"$R"}, found $n (image changed?)"
  ok "$4"
}
assert_same() {  # <file> <expected content> <what>
  [[ $(cat -- "$1") == "$2" ]] || die "prep: $3: ${1#"$R"} does not hold the expected text"
  ok "$3"
}
lines() { wc -l < "$1"; }
changed_lines() {  # <a> <b>: number of differing lines between two files of equal length
  awk 'NR == FNR { a[FNR] = $0; next } a[FNR] != $0 { c++ } END { print c + 0 }' "$1" "$2"
}
keep_orig() { [[ -e $1.hpcop-orig ]] || cp -p -- "$1" "$1.hpcop-orig"; }
need_file() { [[ -f $1 && ! -L $1 ]] || die "prep: ${1#"$R"} is missing (image changed?)"; }

check_rootfs() {  # the rootfs must be <private hpcop work directory>/rootfs
  [[ $R == /* && -d $R && ! -L $R && -O $R ]] || die "prep: rootfs '$R' is not a directory of yours"
  is_private_dir "$(dirname -- "$R")" || die "prep: $R is not inside a private (0700) work directory"
  [[ $(dirname -- "$R") == */hpcop-* ]] || die "prep: $R is not inside an hpcop work directory"
}

# 1. Couchbase's init script: no root check, no chown, no user switch, ulimit failures tolerated.
patch_couchbase_init() {
  local f=$R/opt/couchbase/etc/couchbase_init.d
  need_file "$f"; keep_orig "$f"
  sed -i -e 's/^if \[ "$(id -u)" != "0" \]; then$/if false; then  # hpcop: non-root run/' \
         -e 's/^\([[:space:]]*\)chown couchbase /\1: hpcop-skip chown couchbase /' \
         -e '/^[[:space:]]*ulimit /{/|| true$/!s/$/ 2>\/dev\/null || true/;}' \
         -e 's/ --chuid couchbase / /' "$f"
  assert_count "$f" 1 '^if false; then  # hpcop: non-root run$' "Couchbase init: root check disabled"
  assert_count "$f" 0 '"\$\(id -u\)" != "0"' "Couchbase init: no root check left"
  assert_count "$f" 1 '^[[:space:]]*: hpcop-skip chown couchbase ' "Couchbase init: chown made a no-op"
  assert_count "$f" 0 '^[[:space:]]*chown ' "Couchbase init: no chown left"
  assert_count "$f" 3 '^[[:space:]]*ulimit ' "Couchbase init: three ulimit lines"
  assert_count "$f" 3 '^[[:space:]]*ulimit .* 2>/dev/null \|\| true$' "Couchbase init: ulimit failures tolerated"
  assert_count "$f" 0 '--chuid' "Couchbase init: no switch to the couchbase user"
  assert_count "$f" 1 'start-stop-daemon --start --quiet --oknodo --chdir "\$PWD" --pidfile /dev/null' \
    "Couchbase init: daemon started as the job user"
  [[ $(lines "$f") == "$(lines "$f.hpcop-orig")" && $(changed_lines "$f.hpcop-orig" "$f") == 6 ]] \
    || die "prep: Couchbase init: expected exactly 6 changed lines"
  ok "Couchbase init: exactly 6 lines changed"
}

# 1b. Couchbase's own start script: every Couchbase VM gets the loopback distribution and the job's epmd.
# The babysitter's -kernel line gains inet_dist_use_interface (a second layer, in place even without the
# env file); ns_server and ns_couchdb are started by the babysitter (child_erlang, open_port) and inherit
# its ERL_AFLAGS, as do the transient `-sname init` node (which starts epmd) and the -k executioner.
# Those two had no port range, so they listened on an ephemeral loopback port that the shim's owner check
# does not cover, the init node with the public cookie `nocookie`: both get Couchbase's 21100-21299.
CB_KERNEL_STOCK="        -kernel inet_dist_listen_min 21100 inet_dist_listen_max 21299 \\"
CB_KERNEL_HPCOP="        -kernel inet_dist_listen_min 21100 inet_dist_listen_max 21299 inet_dist_use_interface '{127,0,0,1}' \\"
CB_RANGE='-kernel inet_dist_listen_min 21100 inet_dist_listen_max 21299'
CB_INIT_STOCK='    erl -noshell -setcookie nocookie -sname init -run init stop 2>&1 > /dev/null'
CB_INIT_HPCOP="    erl -noshell -setcookie nocookie -sname init $CB_RANGE -run init stop 2>&1 > /dev/null"
CB_EXEC_STOCK="        -name executioner@executioner \\"
CB_EXEC_HPCOP="        -name executioner@executioner $CB_RANGE \\"
CB_ENV_BLOCK="# hpcop: every Couchbase VM (babysitter, ns_server, ns_couchdb, the init and executioner nodes) and the
# epmd it may start: distribution on loopback, this job's epmd port (configure.sh writes the file).
$ERLENV_LINE"
patch_couchbase_server() {
  local f=$R/opt/couchbase/bin/couchbase-server tmp
  need_file "$f"; keep_orig "$f"
  if [[ $(count_line "$f" "$ERLENV_LINE") == 0 ]]; then
    [[ $(count_line "$f" 'export ERL_LIBS') == 1 ]] || die "prep: couchbase-server: no single 'export ERL_LIBS' line (image changed?)"
    tmp=$f.tmp
    BLOCK=$CB_ENV_BLOCK awk '{ print } $0 == "export ERL_LIBS" { print ENVIRON["BLOCK"] }' "$f" > "$tmp"
    chmod --reference="$f" -- "$tmp"; mv -f -- "$tmp" "$f"
  fi
  if [[ $(count_line "$f" "$CB_KERNEL_STOCK") == 1 ]]; then
    STOCK=$CB_KERNEL_STOCK NEW=$CB_KERNEL_HPCOP awk '$0 == ENVIRON["STOCK"] { print ENVIRON["NEW"]; next } { print }' "$f" > "$f.tmp"
    chmod --reference="$f" -- "$f.tmp"; mv -f -- "$f.tmp" "$f"
  fi
  if [[ $(count_line "$f" "$CB_INIT_HPCOP") == 0 ]]; then
    [[ $(count_line "$f" "$CB_INIT_STOCK") == 1 ]] || die "prep: couchbase-server: no single stock '-sname init' line (image changed?)"
    STOCK=$CB_INIT_STOCK NEW=$CB_INIT_HPCOP awk '$0 == ENVIRON["STOCK"] { print ENVIRON["NEW"]; next } { print }' "$f" > "$f.tmp"
    chmod --reference="$f" -- "$f.tmp"; mv -f -- "$f.tmp" "$f"
  fi
  if [[ $(count_line "$f" "$CB_EXEC_HPCOP") == 0 ]]; then
    [[ $(count_line "$f" "$CB_EXEC_STOCK") == 1 ]] || die "prep: couchbase-server: no single stock executioner line (image changed?)"
    STOCK=$CB_EXEC_STOCK NEW=$CB_EXEC_HPCOP awk '$0 == ENVIRON["STOCK"] { print ENVIRON["NEW"]; next } { print }' "$f" > "$f.tmp"
    chmod --reference="$f" -- "$f.tmp"; mv -f -- "$f.tmp" "$f"
  fi
  assert_line "$f" 1 "$ERLENV_LINE" "couchbase-server: sources the Erlang env file"
  [[ $(grep -B1 -xF "$ERLENV_LINE" "$f" | head -n 1) == *"configure.sh writes the file)." && \
     $(grep -B3 -xF "$ERLENV_LINE" "$f" | head -n 1) == "export ERL_LIBS" ]] \
    || die "prep: couchbase-server: the Erlang env block is not right after 'export ERL_LIBS'"
  assert_line "$f" 1 "$CB_KERNEL_HPCOP" "couchbase-server: babysitter distribution on loopback (inet_dist_use_interface)"
  assert_line "$f" 0 "$CB_KERNEL_STOCK" "couchbase-server: no stock -kernel line left"
  assert_line "$f" 1 "        -name 'babysitter_of_ns_1@127.0.0.1' \\" "couchbase-server: babysitter node name kept"
  assert_line "$f" 1 "$CB_INIT_HPCOP" "couchbase-server: the transient init node listens in 21100-21299"
  assert_line "$f" 1 "$CB_EXEC_HPCOP" "couchbase-server: the -k executioner node listens in 21100-21299"
  assert_line "$f" 0 "$CB_INIT_STOCK" "couchbase-server: no stock init line left"
  assert_line "$f" 0 "$CB_EXEC_STOCK" "couchbase-server: no stock executioner line left"
  assert_count "$f" 0 'ERL_EPMD_PORT=|ERL_AFLAGS=|ERL_EPMD_ADDRESS=' "couchbase-server: sets no Erlang variable of its own"
  [[ $(nlines "$f") == $(( $(nlines "$f.hpcop-orig") + 3 )) && $(diff "$f.hpcop-orig" "$f" | grep -c '^[<>]') == 9 ]] \
    || die "prep: couchbase-server: expected 3 added lines and 3 changed lines"
  bash -n "$f" || die "prep: couchbase-server: syntax error after patching"
  ok "couchbase-server: exactly 3 lines added and 3 changed, syntax ok"
}

# 1c. projector is a Go program: its admin port never passes through the shim, ns_server starts it with
# -adminport=:<port> (all interfaces; ns_ports_setup.beam), and that port takes unauthenticated requests
# to stream a bucket's mutations. It only feeds GSI indexes, which a kv-only Couchbase never has. On a
# node shared with other users even loopback is too open, so a stand-in that never listens replaces it.
# (The real binary stays as projector.hpcop-real; a loopback-only wrapper is the fallback, README.)
PROJECTOR_WRAPPER='#!/bin/bash
# hpcop: projector feeds GSI indexes, which this kv-only Couchbase never has. Its admin port is a Go
# listener out of the port shim'"'"'s reach and unauthenticated, so this stand-in takes its place and never
# listens. ns_server (through goport) stops it like the real one.
exec /bin/sleep infinity'
wrap_projector() {
  local f=$R/opt/couchbase/bin/projector real=$R/opt/couchbase/bin/projector.hpcop-real
  if [[ ! -e $real ]]; then
    is_elf "$f" || die "prep: /opt/couchbase/bin/projector is not the stock ELF program (image changed?)"
    mv -- "$f" "$real"
  fi
  is_elf "$real" || die "prep: projector.hpcop-real is not an ELF program"
  printf '%s\n' "$PROJECTOR_WRAPPER" > "$f"; chmod 755 -- "$f"
  assert_same "$f" "$PROJECTOR_WRAPPER" "projector: replaced by a stand-in that never listens"
  bash -n "$f" || die "prep: projector wrapper: syntax error"
}

# 1d. The relx services' vm.args (not in the persistence directory, so onepanel never rewrites it):
# distribution on loopback, in the fixed range the onepanel and op_worker app.config already use.
# oz_worker's app.config sets no range at all, so without this its port would be ephemeral.
VMARGS_BLOCK='## hpcop: Erlang distribution on loopback only, in a fixed range (each service is a single-node cluster)
-kernel inet_dist_use_interface {127,0,0,1}
-kernel inet_dist_listen_min 9100
-kernel inet_dist_listen_max 9139'
patch_vm_args() {  # <service>
  local f=$R/var/lib/$1/vm.args n0
  need_file "$f"; keep_orig "$f"; n0=$(nlines "$f.hpcop-orig")
  assert_line "$f" 1 "-args_file /etc/$1/autogenerated.vm.args" "$1 vm.args: stock -args_file line"
  if [[ $(count_line "$f" '-kernel inet_dist_use_interface {127,0,0,1}') == 0 ]]; then
    if [[ -s $f && $(tail -c 1 -- "$f" | od -An -tx1 | tr -d ' ') != 0a ]]; then printf '\n' >> "$f"; fi
    printf '%s\n' "$VMARGS_BLOCK" >> "$f"
  fi
  assert_line "$f" 1 '-kernel inet_dist_use_interface {127,0,0,1}' "$1 vm.args: distribution on loopback"
  assert_line "$f" 1 '-kernel inet_dist_listen_min 9100' "$1 vm.args: distribution range start"
  assert_line "$f" 1 '-kernel inet_dist_listen_max 9139' "$1 vm.args: distribution range end"
  assert_count "$f" 3 '^-kernel inet_dist_' "$1 vm.args: no other distribution setting"
  [[ $(nlines "$f") == $((n0 + 4)) ]] || die "prep: $1 vm.args: expected exactly 4 added lines"
}

# 2. relx env.sh: accept the job user, and take the Erlang cookie from $HOME/.erlang.cookie only.
COOKIE_MARK='COOKIE_ARG=""  # hpcop: cookie from $HOME/.erlang.cookie'
COOKIE_BLOCK="# hpcop: the Erlang cookie is read from \$HOME/.erlang.cookie (mode 0600), never from a command line.
# onepanel writes a -setcookie line into a service's vm.args when it configures it, and erlexec would copy
# it onto beam's argv; drop it before the node starts. nodetool (ping, stop) falls back to the file too.
if grep -q '^-setcookie' \"\$RUNNER_ETC_DIR/autogenerated.vm.args\" 2>/dev/null; then
    sed -i '/^-setcookie/d' \"\$RUNNER_ETC_DIR/autogenerated.vm.args\"
fi
# hpcop: distribution and epmd on loopback, on this job's epmd port, for the node and for every nodetool
# call (ERL_AFLAGS, ERL_EPMD_PORT, ERL_EPMD_ADDRESS); \`service\` runs this under env -i, hence a file.
$ERLENV_LINE
$COOKIE_MARK"
COOKIE_BLOCK_LINES=10

patch_env_sh() {  # <service>
  local f=$R/usr/lib/$1/lib/env.sh n0
  need_file "$f"; keep_orig "$f"; n0=$(lines "$f.hpcop-orig")
  sed -i -e 's/^\([[:space:]]*\)if \[ "x$WHOAMI" != "xroot" \]; then$/\1if false; then  # hpcop: non-root run/' \
         -e 's/^RUNNER_USER=\([a-z_]*\)$/RUNNER_USER=$(whoami)  # hpcop: was \1/' "$f"
  if ! grep -qF "$COOKIE_MARK" "$f"; then
    BLOCK=$COOKIE_BLOCK awk '
      state == 0 && $0 == "# Extract the target cookie" { state = 1; removed = 1; next }
      state == 1 {
        removed++
        if (index($0, "COOKIE_ARG=`grep") && index($0, "autogenerated.vm.args")) grep_seen = 1
        if (index($0, "COOKIE_ARG=\"-setcookie $COOKIE\"")) set_seen = 1
        if ($0 == "fi") { state = 2; print ENVIRON["BLOCK"] }
        next
      }
      { print }
      END { if (state != 2 || removed != 11 || !grep_seen || !set_seen) exit 3 }
    ' "$f" > "$f.tmp" || die "prep: $1 env.sh: the cookie block is not the expected 11 lines (image changed?)"
    mv -f -- "$f.tmp" "$f"
  fi
  assert_count "$f" 1 '^[[:space:]]*if false; then  # hpcop: non-root run$' "$1 env.sh: root-only check disabled"
  assert_count "$f" 0 '"x\$WHOAMI" != "xroot"' "$1 env.sh: no root-only check left"
  assert_count "$f" 1 '^RUNNER_USER=\$\(whoami\)  # hpcop: was (root|cluster_manager)$' "$1 env.sh: runs as the job user"
  assert_count "$f" 1 '^RUNNER_USER=' "$1 env.sh: one RUNNER_USER"
  assert_count "$f" 1 '^COOKIE_ARG=""  # hpcop: cookie from \$HOME/\.erlang\.cookie$' "$1 env.sh: cookie never on argv"
  assert_count "$f" 0 'COOKIE_ARG=`grep|-setcookie parameter|COOKIE_ARG="-setcookie' "$1 env.sh: stock cookie block gone"
  assert_count "$f" 1 '^NODETOOL="\$ERTS_PATH/escript \$ERTS_PATH/nodetool \$NET_TICKTIME_ARG \$NAME_ARG \$COOKIE_ARG"$' \
    "$1 env.sh: nodetool gets no -setcookie"
  assert_line "$f" 1 "$ERLENV_LINE" "$1 env.sh: sources the Erlang env file (loopback distribution, job epmd port)"
  [[ $(grep -A1 -xF "$ERLENV_LINE" "$f" | tail -n 1) == "$COOKIE_MARK" ]] \
    || die "prep: $1 env.sh: the Erlang env line is not inside the hpcop block"
  assert_count "$f" 0 'ERL_EPMD_PORT=|ERL_AFLAGS=|ERL_EPMD_ADDRESS=' "$1 env.sh: sets no Erlang variable of its own"
  [[ $(grep -A1 -F "$COOKIE_MARK" "$f" | tail -n 1) == "" && $(grep -A2 -F "$COOKIE_MARK" "$f" | tail -n 1) == "# Extract the target net_ticktime" ]] \
    || die "prep: $1 env.sh: the cookie block did not end where expected"
  [[ $(lines "$f") == $((n0 - 11 + COOKIE_BLOCK_LINES)) ]] || die "prep: $1 env.sh: unexpected length after the cookie patch"
  bash -n "$f" || die "prep: $1 env.sh: syntax error after patching"
  ok "$1 env.sh: exactly one block replaced, syntax ok"
}

# 3. The stock start script insists on PID 1; under --containall the Apptainer init is PID 1.
make_nopid() {
  local s=$R/root/$START.sh n=$R/root/$START-nopid.sh
  need_file "$s"
  awk '
    $0 == "if [ $$ -ne 1 ]; then" { skip = 1; removed = 1; print "# hpcop: PID-1 check removed (under --containall the Apptainer init is PID 1)"; next }
    skip { removed++; if ($0 == "fi") skip = 0; next }
    { print }
    END { if (removed != 8 || skip) exit 3 }
  ' "$s" > "$n.tmp" || die "prep: /root/$START.sh: the PID-1 check is not the expected 8-line block (image changed?)"
  mv -f -- "$n.tmp" "$n"; chmod 700 -- "$n"
  assert_count "$n" 1 '^# hpcop: PID-1 check removed' "$START-nopid.sh: PID-1 check removed"
  assert_count "$n" 0 '\$\$ -ne 1' "$START-nopid.sh: no PID-1 test left"
  assert_count "$n" 1 '^trap on_termination_signal SIGHUP SIGINT SIGTERM$' "$START-nopid.sh: signal trap kept"
  assert_count "$n" 1 '^/root/persistence-dir\.py --copy-missing-files$' "$START-nopid.sh: persistence seeding kept"
  assert_count "$n" 1 "^/root/$START\\.py &\$" "$START-nopid.sh: launcher kept"
  [[ $(awk 'NF { l = $0 } END { print l }' "$n") == 'wait $!' ]] || die "prep: $START-nopid.sh no longer ends in 'wait \$!' (truncated?)"
  [[ $(lines "$n") == $(( $(lines "$s") - 7 )) ]] || die "prep: $START-nopid.sh: unexpected length"
  bash -n "$n" || die "prep: $START-nopid.sh: syntax error"
  ok "$START-nopid.sh: complete, ends in the stock 'wait \$!'"
}

# 3b. The stock launcher waits for onepanel's REST, polling once a second, then sets the emergency
# passphrase (oneprovider.py / onezone.py wait_for_rest_listener). Poll every 50 ms instead, so 9443 is
# without a passphrase for as short a time as the image allows (the owner check keeps it ours anyway).
# Before its first request it waits until a listener of the job's uid holds 127.0.0.1:9443 (read from
# /proc/net/tcp by hpcop-cb-init.py --wait-listener), a check of its own besides the shim's.
LAUNCHER_POLL_STOCK='            time.sleep(1)'
LAUNCHER_POLL_HPCOP='            time.sleep(0.05)  # hpcop: set the passphrase as soon as 9443 answers'
LAUNCHER_WAIT_HPCOP='    sp.run(["python3", "-I", "/root/hpcop-cb-init.py", "--wait-listener", "9443", "1800"], check=True)  # hpcop: our listener first'
patch_launcher_poll() {
  local f=$R/root/$START.py
  need_file "$f"; keep_orig "$f"
  if [[ $(count_line "$f" "$LAUNCHER_POLL_HPCOP") == 0 ]]; then
    STOCK=$LAUNCHER_POLL_STOCK NEW=$LAUNCHER_POLL_HPCOP awk '
      /^def wait_for_rest_listener\(\) -> None:$/ { on = 1 }
      on && /^def / && !/^def wait_for_rest_listener/ { on = 0 }
      on && $0 == ENVIRON["STOCK"] { print ENVIRON["NEW"]; n++; next }
      { print }
      END { if (n != 1) exit 3 }
    ' "$f" > "$f.tmp" || die "prep: /root/$START.py: wait_for_rest_listener does not hold one stock 1 s poll (image changed?)"
    chmod --reference="$f" -- "$f.tmp"; mv -f -- "$f.tmp" "$f"
  fi
  if [[ $(count_line "$f" "$LAUNCHER_WAIT_HPCOP") == 0 ]]; then
    WAIT=$LAUNCHER_WAIT_HPCOP awk '
      prev == "def wait_for_rest_listener() -> None:" { if ($0 != "    first = True") exit 3; print ENVIRON["WAIT"]; n++ }
      { print; prev = $0 }
      END { if (n != 1) exit 3 }
    ' "$f" > "$f.tmp" || die "prep: /root/$START.py: wait_for_rest_listener does not start with the stock 'first = True' (image changed?)"
    chmod --reference="$f" -- "$f.tmp"; mv -f -- "$f.tmp" "$f"
  fi
  assert_line "$f" 1 "$LAUNCHER_POLL_HPCOP" "$START.py: onepanel's REST polled every 50 ms before the passphrase is set"
  assert_line "$f" 1 "$LAUNCHER_WAIT_HPCOP" "$START.py: waits for onepanel's own listener on 9443 before its first request"
  [[ $(grep -A1 -xF 'def wait_for_rest_listener() -> None:' "$f" | tail -n 1) == "$LAUNCHER_WAIT_HPCOP" ]] \
    || die "prep: $START.py: the wait for onepanel's listener is not the first line of wait_for_rest_listener"
  [[ $(lines "$f") == $(( $(lines "$f.hpcop-orig") + 1 )) && $(diff "$f.hpcop-orig" "$f" | grep -c '^[<>]') == 3 ]] \
    || die "prep: $START.py: expected exactly 1 added line and 1 changed line"
  [[ $(grep -B15 -xF "$LAUNCHER_POLL_HPCOP" "$f" | grep -c '^def wait_for_rest_listener() -> None:$') == 1 ]] \
    || die "prep: $START.py: the changed poll is not in wait_for_rest_listener"
  ok "$START.py: exactly 1 line added and 1 changed, in wait_for_rest_listener"
}

# 4. Our entry point inside the container: settings and the passphrase come from /root/.hpcop files.
write_start_wrapper() {
  local w=$R/root/hpcop-start.sh body
  body="#!/bin/bash
# hpcop: the container's entry point (run-node.sh / run-zone.sh). Reads the per-launch settings from
# /root/.hpcop (0700), hands the emergency passphrase to the stock launcher through its environment only
# (never a command line), and becomes the PID-1-free copy of the stock start script.
set -euo pipefail
umask 077
export HOME=/root
S=/root/.hpcop
for f in env config.yaml passphrase couchbase-user couchbase-password; do [ -s \"\$S/\$f\" ] || { echo \"hpcop: \$S/\$f missing\" >&2; exit 1; }; done
[ -s $HPCOP_ERLANG_ENV ] || { echo 'hpcop: $HPCOP_ERLANG_ENV missing' >&2; exit 1; }
. $HPCOP_ERLANG_ENV
# onepanel starts Couchbase and gives it this launch's administrator (cluster-init, with the user and password
# of our 0600 overlay), as the image intends. Until then Couchbase has none, behind loopback and the owner check.
# The guard, in the background, prints into run.log how long that lasted, from 8091 opening, then checks that
# this launch's administrator answers and anonymous admin REST is refused. If Couchbase has no administrator
# once onepanel's setup has created a bucket, or another one, it sends SIGTERM to this script's PID (\$\$, kept
# by the exec below), whose stock trap stops every component, and finish*.sh fails the start.
$CB_WATCH_LINE
set -a; . \"\$S/env\"; set +a
IFS= read -r ONEPANEL_EMERGENCY_PASSPHRASE < \"\$S/passphrase\" || true
[ -n \"\$ONEPANEL_EMERGENCY_PASSPHRASE\" ] || { echo 'hpcop: empty passphrase' >&2; exit 1; }
export ONEPANEL_EMERGENCY_PASSPHRASE
$CONFIG_VAR=\$(cat \"\$S/config.yaml\")
export $CONFIG_VAR
rm -f /root/service-ready.lock
exec /root/$START-nopid.sh"
  printf '%s\n' "$body" > "$w"; chmod 700 -- "$w"
  assert_same "$w" "$body" "hpcop-start.sh written"
  bash -n "$w" || die "prep: hpcop-start.sh: syntax error"
}

# 4b. The Couchbase guard (it also measures the pre-admin window), which hpcop-start.sh starts before the launcher.
install_cb_init() {
  local f=$R/root/hpcop-cb-init.py
  install -m 700 -- "$HPCOP_BUNDLE/hpcop-cb-init.py" "$f"
  cmp -s -- "$HPCOP_BUNDLE/hpcop-cb-init.py" "$f" || die "prep: /root/hpcop-cb-init.py differs from the bundle's"
  assert_line "$R/root/hpcop-start.sh" 1 "$CB_WATCH_LINE" "hpcop-start.sh starts the Couchbase guard with its own PID"
  assert_count "$R/root/hpcop-start.sh" 0 'couchbase-server start' "hpcop-start.sh leaves the Couchbase start to onepanel"
}

# 4c. onepanel's wait_for_init sleeps couchbase_after_init_delay (4 s in the image) after 8091 first accepts a
# connection, then init_cluster runs cluster-init, which creates the administrator: most of the time Couchbase
# runs without one. 1 s is still a wide margin: live, Couchbase took REST POSTs 71 ms after 8091 opened (run 2).
# The same key delays the start of the workers after the (single-node) rebalance (service_couchbase.erl:323).
CB_WAIT_OVERLAY='% hpcop: Couchbase has no administrator until cluster-init; wait 1 s, not 4 s, after 8091 answers.
[{onepanel, [{couchbase_after_init_delay, 1000}]}].'
shorten_couchbase_wait() {
  need_key "$R/var/lib/$PANEL/app.config" '{couchbase_after_init_delay, 4000},' "onepanel's Couchbase after-init delay key"
  write_overlay "$PANEL" 61-hpcop-couchbase-wait.config "$CB_WAIT_OVERLAY"
}

# 5. The port shim, preloaded through the rootfs's own /etc/ld.so.preload.
install_shim() {  # <portshim.so>
  local dst=$R/usr/local/lib/hpcop-portshim.so
  [[ -f $1 && $(head -c 4 -- "$1" | od -An -c | tr -d ' ') == 177ELF ]] || die "prep: shim '$1' is not an ELF file: build it first"
  mkdir -p -- "$R/usr/local/lib"
  [[ $1 -ef $dst ]] || install -m 755 -- "$1" "$dst"
  printf '%s\n' /usr/local/lib/hpcop-portshim.so > "$R/etc/ld.so.preload"
  assert_same "$R/etc/ld.so.preload" /usr/local/lib/hpcop-portshim.so "ld.so.preload loads only the shim"
  [[ -s $R/etc/hpcop-peers ]] || printf '# hpcop peers: one IPv4/IPv6 address per line; connect() to 443 there goes to 8443\n' > "$R/etc/hpcop-peers"
  chmod 644 -- "$R/etc/hpcop-peers" "$R/etc/ld.so.preload"
  ok "shim installed; peers file present"
}

# 6. Config overlays at the /etc link targets, each checked against a key the image really has.
need_key() {  # <app.config> <exact stock text> <what>
  [[ $(grep -cF -- "$2" "$1" || true) == 1 ]] || die "prep: $3: '$2' not found once in ${1#"$R"} (key renamed?)"
}
write_overlay() {  # <service> <file name> <content>
  local d
  d=$(config_dir "$R" "$1")
  mkdir -p -- "$d"
  printf '%s\n' "$3" > "$d/$2"
  assert_same "$d/$2" "$3" "overlay $1/config.d/$2"
}

# 7. Image defaults that must never reach a running service.
strip_baked_defaults() {
  local svc f cb n
  for svc in $SERVICES; do
    f=$R/volumes/persistence_backup/etc/$svc/autogenerated.vm.args
    need_file "$f"
    sed -i '/^-setcookie /d' "$f"; chmod 600 -- "$f"
    assert_count "$f" 0 '^-setcookie' "$svc seed vm.args: default cookie removed"
    assert_count "$f" 1 '^-name ' "$svc seed vm.args: node name kept"
  done
  cb=$R/volumes/persistence_backup/opt/couchbase/var/lib/couchbase
  [[ -d $cb && ! -L $cb ]] || die "prep: ${cb#"$R"} missing (image changed?)"
  find "$cb" -xdev -type f -delete
  n=$(find "$cb" -type f | wc -l)
  [[ $n == 0 ]] || die "prep: $n files left in the shipped Couchbase state"
  ok "shipped Couchbase state removed (fresh cookies and memcached password at first start)"
  no_baked "$R/volumes" || die "prep: a shipped Couchbase cookie or password is still under /volumes (or the scan failed)"
  ok "no shipped Couchbase cookie or password under /volumes"
}

prep_common() {  # <portshim.so>
  check_rootfs
  patch_couchbase_init
  patch_couchbase_server
  wrap_projector
  local svc
  for svc in $SERVICES; do patch_env_sh "$svc"; patch_vm_args "$svc"; done
  make_nopid
  patch_launcher_poll
  write_start_wrapper
  install_cb_init
  shorten_couchbase_wait
  install_shim "$1"
  strip_baked_defaults
  mkdir -p -- "$R/volumes/persistence" "$R/volumes/posix"
  install -d -m 700 -- "$R/root/.hpcop"
}
