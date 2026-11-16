#!/usr/bin/env bash
# node.sh: the step side of the HPC test runner (README.md). BOOT (hpctestlib.py) starts it inside a Slurm step, after
# unpacking the payload into <root>/runs/<run>/kit:
#
#   node.sh <root> <run> run                    clone the shipped commit, build its environment with uv, check which
#                                               core the interpreter imports, run pytest, and send the results back
#                                               on stdout as one archive with its SHA-256
#   node.sh <root> <run> clean <minutes>|all    remove run directories of ours that no live node.sh holds and that
#                                               are older than <minutes> (a step that was killed); `all` removes every
#                                               such directory, and the uv cache and Python too when no run is live
#
# It writes only under <root>, a 0700 directory of ours on the node's tmpfs that BOOT has checked: never under $HOME,
# never under Slurm's per-job TMPDIR (a link into a shared filesystem with an inode quota on some sites). Its own
# environment is the step's allowlist, and pytest gets one built here: nothing of the login node reaches a test.
# Every line it prints to stdout starts with HPCT-; the tools' own output goes to files under results/.
set -u
root=$1 run=$2 sub=$3
shift 3
dir=$root/runs/$run
kit=$dir/kit
res=$dir/results
child=""
RUN_ENV=()

say() { printf 'HPCT-LOG %s\n' "$*"; }
fail() { printf 'HPCT-FAIL node: %s\n' "$*"; exit 72; }

# rm -rf of a tree a test may have left read-only: only its DIRECTORIES are made writable first, and a link given as
# the path is never followed (chmod -R would follow one and could reach a file outside the tree).
force_rm() {
  local p
  for p in "$@"; do
    [ -e "$p" ] || [ -L "$p" ] || continue
    [ -L "$p" ] || find "$p" -type d ! -perm -u=rwx -exec chmod u+rwx -- {} + 2> /dev/null || true
    rm -rf -- "$p"
  done
}

cleanup() {
  if [ -n "$child" ]; then
    kill -TERM "$child" 2> /dev/null
    wait "$child" 2> /dev/null
  fi
  [ "${HPCT_KEEP:-0}" = 1 ] || force_rm "$dir"
  rmdir "$root/runs" 2> /dev/null
  return 0
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 130' INT

# The payload's settings, read line by line, never sourced: known keys only, values in a plain alphabet.
HPCT_SHA="" HPCT_UV="" HPCT_UV_VERSION="" HPCT_PYTHON="" HPCT_TIMEOUT_S="" HPCT_CORES="" HPCT_KEEP=0 HPCT_STEP_PATH=""
while IFS='=' read -r key value; do
  case "$key" in
    HPCT_SHA | HPCT_UV | HPCT_UV_VERSION | HPCT_PYTHON | HPCT_TIMEOUT_S | HPCT_CORES | HPCT_KEEP | HPCT_STEP_PATH)
      case "$value" in *[!A-Za-z0-9_./:+-]*) fail "the setting $key has characters it may not carry" ;; esac
      printf -v "$key" '%s' "$value"
      ;;
    "") ;;
    *) fail "an unknown setting $key" ;;
  esac
done < "$kit/settings"

is_live() { # is_live <run dir>: a node.sh of ours still holds it (its pid is alive and is a node.sh)
  local pid
  pid=$(cat "$1/pid" 2> /dev/null) || return 1
  case "$pid" in "" | *[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2> /dev/null || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2> /dev/null | grep -q 'node\.sh'
}

do_clone() {
  git init -q -b hpct-unborn "$dir/repo"
  git -C "$dir/repo" fetch -q "$kit/repo.bundle" '+refs/heads/*:refs/heads/*' '+refs/remotes/*:refs/remotes/*'
  git -C "$dir/repo" checkout -q --detach "$HPCT_SHA"
  [ "$(git -C "$dir/repo" rev-parse HEAD)" = "$HPCT_SHA" ] || { echo "the checkout is not $HPCT_SHA"; return 1; }
}

do_env() {
  local version
  [ -x "$HPCT_UV" ] || { echo "no uv at $HPCT_UV"; return 1; }
  version=$("$HPCT_UV" --version | awk '{print $2}')
  echo "uv $version, python $HPCT_PYTHON"
  if [ -n "$HPCT_UV_VERSION" ] && [ "$version" != "$HPCT_UV_VERSION" ]; then
    echo "uv is $version, not $HPCT_UV_VERSION"
    return 1
  fi
  local d
  for d in uv-cache python; do mkdir -m 0700 "$root/$d" 2> /dev/null || [ -d "$root/$d" ] || return 1; done
  flock "$root/python.lock" "$HPCT_UV" python install --no-bin "$HPCT_PYTHON" || return 1
  (cd "$dir/repo" && UV_PROJECT_ENVIRONMENT=$dir/venv "$HPCT_UV" sync --frozen --python "$HPCT_PYTHON")
}

# The SELF-CHECK: an isolated interpreter (python -I, as the core's own subprocess tests start one) of the venv pytest
# will use must import the core from this clone's src/, or no test runs: a venv that found an installed copy would
# test something other than the shipped commit.
do_check() {
  local want line out
  want=$(readlink -f "$dir/repo/src")/
  out=$(cd "$dir" && env -i PATH=/usr/bin:/bin HOME="$dir/home" "$dir/venv/bin/python" -I -c \
    'import os, expcore, expcore_onedata; print(os.path.realpath(expcore.__file__)); print(os.path.realpath(expcore_onedata.__file__))' 2>&1) \
    || { echo "$out"; return 1; }
  while IFS= read -r line; do
    case "$line" in "$want"*) echo "imports ${line#"$dir"/}" ;; *) echo "imports $line, outside the clone's src/"; return 1 ;; esac
  done <<< "$out"
}

do_pytest() {
  local -a args=()
  [ ! -s "$kit/pytest-args" ] || mapfile -d '' -t args < "$kit/pytest-args"
  (
    cd "$dir/repo" || exit 72
    # pytest's whole environment, set with builtins (no variable's value is on any process's command line): everything
    # this shell holds is dropped, then RUN_ENV and the few more pytest needs are exported.
    local path="$dir/venv/bin:$PATH" v
    for v in $(compgen -e); do unset "$v"; done
    export "${RUN_ENV[@]}" "PATH=$path" LC_ALL=C.UTF-8 LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
      "OMP_NUM_THREADS=$HPCT_CORES" "OPENBLAS_NUM_THREADS=$HPCT_CORES" "MKL_NUM_THREADS=$HPCT_CORES" \
      GIT_AUTHOR_NAME=hpct GIT_AUTHOR_EMAIL=hpct@invalid GIT_COMMITTER_NAME=hpct GIT_COMMITTER_EMAIL=hpct@invalid
    exec timeout --signal=TERM --kill-after=30 "$HPCT_TIMEOUT_S" \
      "$dir/venv/bin/python" -m pytest "${args[@]}" -p no:cacheprovider -ra "--junitxml=$res/junit.xml" \
      "--basetemp=$dir/tmp/pytest"
  ) > "$res/pytest.log" 2>&1 &
  child=$!
  wait "$child"
  local rc=$?
  child=""
  return "$rc"
}

send() { # send <rc>: the results directory as one archive, base64 between a header with its SHA-256 and the end
  local rc=$1 sum size
  echo "$rc" > "$res/rc"
  tar -C "$dir" -czf "$dir/results.tgz" results
  sum=$(sha256sum "$dir/results.tgz" | cut -d' ' -f1)
  size=$(stat -c %s "$dir/results.tgz")
  printf 'HPCT-RESULT %s %s\n' "$sum" "$size"
  base64 -w 76 "$dir/results.tgz"
  printf 'HPCT-END %s\n' "$rc"
}

cmd_run() {
  cd "$dir" || fail "cannot enter the run directory"
  case "$HPCT_SHA" in "" | *[!0-9a-f]*) fail "the commit is not hex" ;; esac
  case "$HPCT_TIMEOUT_S$HPCT_CORES" in "" | *[!0-9]*) fail "the timeout and cores are whole numbers" ;; esac
  mkdir -m 0700 "$res" "$dir/home" "$dir/tmp"
  echo "$$" > "$dir/pid"
  # From here on nothing reads or writes the account's home, which every agent of the account shares: uv, pip, git
  # and Python would otherwise fall back to the passwd entry's home when HOME is unset. RUN_ENV is exported for the
  # clone and uv, and is the core of pytest's own environment.
  RUN_ENV=(
    "HOME=$dir/home" "TMPDIR=$dir/tmp" "XDG_CACHE_HOME=$dir/home/.cache" "XDG_CONFIG_HOME=$dir/home/.config"
    "XDG_DATA_HOME=$dir/home/.local/share" "XDG_STATE_HOME=$dir/home/.local/state" "XDG_BIN_HOME=$dir/home/.local/bin"
    "UV_CACHE_DIR=$root/uv-cache" "UV_PYTHON_INSTALL_DIR=$root/python" "UV_PYTHON_BIN_DIR=$root/python-bin"
    "UV_TOOL_DIR=$root/uv-tools" "UV_TOOL_BIN_DIR=$root/uv-tools/bin" "UV_NO_CONFIG=1" "UV_NO_PROGRESS=1"
    "UV_PYTHON_PREFERENCE=only-managed" "PIP_CONFIG_FILE=/dev/null" "PIP_CACHE_DIR=$dir/home/.cache/pip"
    "GIT_CONFIG_GLOBAL=/dev/null" "GIT_CONFIG_NOSYSTEM=1" "PYTHONNOUSERSITE=1" "PYTHONUSERBASE=$dir/home/.local"
    "APPTAINER_CACHEDIR=$dir/home/.apptainer/cache" "APPTAINER_CONFIGDIR=$dir/home/.apptainer"
    "SINGULARITY_CACHEDIR=$dir/home/.singularity/cache" "SINGULARITY_CONFIGDIR=$dir/home/.singularity"
  )
  export "${RUN_ENV[@]}"
  [ -z "$HPCT_STEP_PATH" ] || export PATH=$HPCT_STEP_PATH
  local t0 t1 t2 t3 t4 rc=0
  t0=$(date +%s)
  if ! do_clone > "$res/clone.log" 2>&1; then rc=73; fi
  t1=$(date +%s)
  say "clone ${HPCT_SHA:0:12}: $([ "$rc" = 0 ] && echo ok || echo FAILED) in $((t1 - t0)) s"
  if [ "$rc" = 0 ] && ! do_env > "$res/env.log" 2>&1; then rc=74; fi
  t2=$(date +%s)
  [ "$rc" = 73 ] || say "environment (uv sync --frozen): $([ "$rc" = 0 ] && echo ok || echo FAILED) in $((t2 - t1)) s"
  if [ "$rc" = 0 ] && ! do_check > "$res/check.log" 2>&1; then rc=97; fi
  [ "$rc" != 97 ] || say "SELF-CHECK FAILED: the venv does not import the core from the shipped clone; no test ran"
  t3=$(date +%s)
  if [ "$rc" = 0 ]; then
    say "pytest started on $HPCT_CORES cores, bounded at $HPCT_TIMEOUT_S s"
    do_pytest
    rc=$?
  fi
  t4=$(date +%s)
  printf 'clone_s=%s\nenv_s=%s\ncheck_s=%s\npytest_s=%s\n' $((t1 - t0)) $((t2 - t1)) $((t3 - t2)) $((t4 - t3)) > "$res/times"
  say "pytest exited $rc in $((t4 - t3)) s"
  send "$rc"
}

cmd_clean() {
  local mode=${1:-} d live=0 removed=0 kept=0
  cd "$dir" || fail "cannot enter the run directory"
  case "$mode" in all) ;; "" | *[!0-9]*) fail "clean takes a number of minutes or all" ;; esac
  for d in "$root"/runs/*; do
    [ -d "$d" ] && [ "$d" != "$dir" ] || continue
    if is_live "$d"; then
      live=$((live + 1))
    elif [ "$mode" = all ] || [ -n "$(find "$d" -maxdepth 0 -mmin +"$mode")" ]; then
      force_rm "$d"
      removed=$((removed + 1))
    else
      kept=$((kept + 1))
    fi
  done
  if [ "$mode" = all ] && [ "$live" = 0 ]; then
    force_rm "$root/uv-cache" "$root/python" "$root/python-bin" "$root/uv-tools" "$root/python.lock"
    say "clean: the uv cache and Python removed"
  fi
  say "clean: removed $removed run directories, kept $kept younger ones, $live live"
}

case "$sub" in
  run) cmd_run ;;
  clean) cmd_clean "$@" ;;
  *) fail "unknown subcommand $sub" ;;
esac
