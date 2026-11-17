"""Two platform templates that declare every key the platform's loaders accept, rendered per federation, and the
classification of every such key: in the platform's fingerprint (it shapes a run) or out (one federation's or one
host's own, a path, a secret, or a setting that decides only a check before a campaign or how a reset reaches a state
it verifies). Every name is invented.

`DOCKER` is the one-space form on the Docker backend, with a tool server, slots, `[placement]` and a Compose `[faults]`;
`APPTAINER` is the `[[spaces]]` form on the Apptainer backend with an HPC `[faults]`. A template is a mapping of files
of the federation's root; `@key@` is filled from a federation's own values (`FEDERATIONS`), which differ in every
address, id, port, host and network name, registry, pool, Compose project and root, and agree in everything else.
"""

import hashlib
import json
from pathlib import Path

PINNED = ("sif", "shim", "collector", "preload", "bind", "bundle", "agent", "gate", "oneclient")
SHA = {name: hashlib.sha256(name.encode()).hexdigest() for name in PINNED}
COMMIT = "0123456789abcdef0123456789abcdef01234567"
MANIFEST = json.dumps({"tools": [{"name": "list_files"}, {"name": "read_file"}]})
DATA = json.dumps({"version": 1, "files": [{"path": "a.csv", "size": 1, "sha256": "0" * 64}], "directories": []})
OTHER_DATA = json.dumps({"version": 1, "files": [{"path": "a.csv", "size": 2, "sha256": "1" * 64}], "directories": []})

DOCKER_PLATFORM = f"""\
zone = {{ name = "zone", url = "https://@zone_host@:@port@" }}
providers.pine = {{ url = "https://@pine_host@:@port@", id = "@pine_id@" }}
providers.birch = {{ url = "https://@birch_host@:@port@", id = "@birch_id@" }}
providers.cedar = {{ url = "https://@cedar_host@:@port@", id = "@cedar_id@" }}
credentials.admin = {{ file = "@root@/secrets/admin.token" }}
credentials.users = {{ directory = "@root@/secrets/users", pattern = "{{user}}.pass" }}
principals.data_path = {{ username = "alpha-reader", user_id = "@reader_id@" }}
principals.toolserver = {{ username = "alpha-tools", user_id = "@tools_id@" }}
principals.service = {{ username = "alpha-health", user_id = "@health_id@" }}
tls = {{ ca_file = "@root@/ca/federation-ca.pem" }}

[space]
name = "alpha-space"
id = "@space_id@"
prefixes = ["alpha-"]
import = {{ readonly = true }}

[support]
providers = ["pine", "birch", "cedar"]
local = "pine"

[link_check]
name = "link-probes-@space_suffix@"
id = "@probe_id@"
prefixes = ["link-"]
providers = ["pine", "birch", "cedar"]
probe_bytes = 65536
timeout_s = 30
visible_timeout_s = 60
evict_timeout_s = 60
every_s = 600

[tokens]
margin_s = 3000
max_excess_s = 600
mint_headroom_s = 300
max_ttl_s = 86400
revocation_settle_s = 330

[toolserver]
command = "@root@/venv/bin/server"
args = ["--relay", "https://@zone_host@:@port@", "--provider", "@pine_id@", "--config", "@root@/server.toml",
        "--max-rows", "50"]
start_timeout_s = 120
clone = "@root@/clone"
commit = "{COMMIT}"

[toolserver.conditions.reader]
allow = ["list_files", "read_file"]
rights = ["space_view", "space_read_data"]
manifest = "@root@/manifests/reader.json"

[slots.one]
data_path = {{ username = "slot-reader", user_id = "@slot_reader_id@" }}
toolserver = {{ username = "slot-tools", user_id = "@slot_tools_id@", rights = ["space_view", "space_read_data"] }}

[sandbox]
backend = "docker"
work_root = "@root@/work"
name_prefix = "@prefix@"
label_namespace = "org.@prefix@"
agent_uid = 10001
python = "python"
sudo = ["sudo", "-n"]
earlyoom_comm = "@prefix@-drv"
trust_paths = ["/etc/ssl/certs/ca-certificates.crt", "/opt/certs/extra.pem"]
content_check_file = "dir/check-@space_suffix@.bin"
placeholder_token = "placeholder"
kill_grace_s = 5

[sandbox.images.agent]
name = "@registry@/agent:1"
id = "sha256:{SHA["agent"]}"

[sandbox.images.gate]
name = "@registry@/gate:1"
id = "sha256:{SHA["gate"]}"

[sandbox.images.oneclient]
name = "@registry@/oneclient:1"
id = "sha256:{SHA["oneclient"]}"

[sandbox.network]
pools = ["@pool@"]
prefix = @pool_prefix@
max_attempts = @attempts@

[sandbox.limits]
cpus = 2.0
memory_bytes = 8589934592
pids = 512
nofile = 4096
tmp_bytes = 1073741824
shm_bytes = 1073741824
work_disk_bytes = 4294967296
gate_memory_bytes = 1073741824

[sandbox.gate]
upstream_read_timeout_s = 3600

[sandbox.mount]
path = "/data"
io_mode = "proxy"
extra_args = ["--provider-timeout=60", "--space-id=@space_id@"]

[sandbox.data_paths]
mounted = "mount"
served = {{ kind = "rest", listing = "paged" }}

[sandbox.served_bytes]
max_files = 1000
max_directories = 1000
max_ranges_per_file = 64

[sandbox.federation]
network = "@network@"
zone = "@zone_host@"
providers.pine = "@pine_host@"

[placement]
holder = "birch"
map = "ranges.toml"
poll_s = 2.0
transfer_timeout_s = 3600
settle_timeout_s = 300
consecutive_polls = 2
request_timeout_s = 900
chunk_bytes = 16777216
fresh_connection_bytes = 16777216
probe_attempts = 3
probe_backoff_s = 1.0
transfer_settle_s = 30.0
transfer_settle_polls = 4
clock_skew_allowance_s = 5.0
concurrency = 16
qos_scan = "listing"
qos_fulfil_timeout_s = 120
reset_concurrency = 4
reset_shortcut = false
reset_exclusive_wait_s = 60
provider_concurrency = 16

[faults]
sanctioned = true
journal = "@root@/fault-journal"
compose_file = "@root@/compose.yaml"
project = "@project@"
command = ["docker", "compose"]
docker = ["docker"]
poll_s = 2
settle_timeout_s = 180
command_timeout_s = 120
ready_timeout_s = 180
clear_attempts = 3
admission_timeout_s = 3600
health_wait_s = 30
gate_timeout_s = 240
heal = "off"
heal_settle_s = 180
session_drain_s = 320
heal_container_fallback = true

[faults.services]
pine = "pine"
birch = "birch"
cedar = "cedar"
"""

RANGES = """\
[[map]]
file = "dir/file.bin"
provider = "cedar"
ranges = [[0, 4096]]
space = "space"
"""

APPTAINER_PLATFORM = f"""\
zone = {{ name = "zone", url = "https://@zone_host@:@port@" }}
providers.aspen = {{ url = "https://@pine_host@:@port@", id = "@pine_id@" }}
providers.larch = {{ url = "https://@birch_host@:@port@", id = "@birch_id@" }}
providers.rowan = {{ url = "https://@cedar_host@:@port@", id = "@cedar_id@" }}
providers.hazel = {{ url = "https://@hazel_host@:@port@", id = "@hazel_id@" }}
credentials.admin = {{ env = "@admin_env@" }}
credentials.users = {{ directory_env = "@users_env@" }}
tls = {{ ca_file = "@root@/ca/federation-ca.pem" }}
addresses = {{ file = "address-map.json", resolver = "toolshim/resolve_launch.py" }}

[tokens]
margin_s = 9000

[[spaces]]
label = "alpha"
name = "alpha-space"
id = "@space_id@"
prefixes = ["alpha-"]
providers = ["larch", "aspen", "hazel"]
local = "aspen"
holder = "larch"
tokens = ["data_path", "tool_server"]
import = {{ readonly = true, shared = false }}
data_manifest = "@root@/data-manifests/alpha.json"

[[spaces]]
label = "beta"
name = "beta-space"
id = "@beta_id@"
prefixes = ["beta-"]
providers = ["rowan", "aspen", "hazel"]
local = "aspen"
holder = "rowan"
data_manifest = "@root@/data-manifests/beta.json"

[placement]
holder = "larch"

[[placement.map]]
file = "dir/file.bin"
provider = "aspen"
ranges = [[0, 4096]]
space = "alpha"

[faults]
sanctioned = false
backend = "hpc"
journal = "@root@/fault-journal"
stop_bound_s = 690
relay_wait_s = 30
step_launch_wait_s = 240

[faults.services]
aspen = "aspen"
larch = "larch"
rowan = "rowan"

[sandbox]
backend = "apptainer"
work_root = "@root@/work"
name_prefix = "@prefix@"
content_check_file = "dir/check-apptainer.bin"
trust_paths = ["/etc/ssl/certs/ca-certificates.crt"]
placeholder_token = "placeholder"
kill_grace_s = 5

[sandbox.limits]
cpus = 6
memory_bytes = 8589934592
pids = 512
nofile = 4096
tmp_bytes = 1073741824
shm_bytes = 1073741824
work_disk_bytes = 4294967296

[sandbox.gate]
upstream_read_timeout_s = 7200

[sandbox.data_paths]
mounted = "mount"
served = {{ kind = "rest", listing = "paged" }}

[sandbox.mount]
path = "/data"
io_mode = "proxy"

[sandbox.federation]
zone = "zone.invented.test"
providers.aspen = "aspen.invented.test"

[sandbox.apptainer]
apptainer = ["@apptainer@"]
image = "@root@/pins/agent.sif"
image_sha256 = "{SHA["sif"]}"
python = "python3"
host_python = "@host_python@"
egress_shim = "@root@/pins/egresslog.so"
egress_shim_sha256 = "{SHA["shim"]}"
egress_collector = "@root@/pins/egress-collector.py"
egress_collector_sha256 = "{SHA["collector"]}"
memory_poll_s = 0.1
disk_poll_s = 1.0
hidden_memory_margin_bytes = 134217728
preload = [{{ path = "@root@/pins/hook.so", sha256 = "{SHA["preload"]}" }}]
binds = [{{ path = "@root@/pins/extra.conf", sha256 = "{SHA["bind"]}", target = "/etc/extra.conf" }}]
oneclient_bundle = "@root@/pins/oneclient.tar"
oneclient_bundle_sha256 = "{SHA["bundle"]}"
provider_storage = "tmpfs"
work_disk = "directory"
work_disk_mkfs = ["@mkfs@"]

[sandbox.apptainer.gate_options]
read_timeout_s = 30
"""

CHECK_FILE_LINE = 'content_check_file = "dir/check-@space_suffix@.bin"'
CHECK_PROVIDER_LINE = '\ncontent_check_provider = "birch"'  # supports the space; not the local provider (O87)
DOCKER = {
    "platform.toml": DOCKER_PLATFORM,
    "ranges.toml": RANGES,
    "manifests/reader.json": MANIFEST,
    "manifests/other.json": MANIFEST.replace("read_file", "stat_file"),
    "data-manifests/alpha.json": DATA,
}
# the file `up` writes (mode 0600) and `[addresses]` names (O73)
ADDRESS_MAP = json.dumps(
    {"format": "expcore-address-map/1", "hosts": {"aspen.invented.test": {"address": "192.0.2.10"}}}
)
APPTAINER = {
    "platform.toml": APPTAINER_PLATFORM,
    "address-map.json": ADDRESS_MAP,
    "toolshim/resolve_launch.py": "# the resolve launcher, invented\n",
    "data-manifests/alpha.json": DATA,
    "data-manifests/beta.json": DATA,
    "data-manifests/other.json": OTHER_DATA,
}

# Two federations of one template: every address, id, port, host and network name, registry, pool, Compose project,
# environment variable, site command and root of their own.
FEDERATIONS = (
    {
        "zone_host": "zone.fed-a.example.org", "pine_host": "pine.fed-a.example.org",
        "birch_host": "birch.fed-a.example.org", "cedar_host": "cedar.fed-a.example.org",
        "hazel_host": "hazel.fed-a.example.org", "hazel_id": "9a" * 16, "port": "8443",
        "pine_id": "a" * 32, "birch_id": "b" * 32, "cedar_id": "c" * 32, "space_id": "d" * 32, "beta_id": "e" * 32,
        "probe_id": "f" * 32, "reader_id": "1" * 32, "tools_id": "2" * 32, "health_id": "3" * 32,
        "slot_reader_id": "4" * 32, "slot_tools_id": "5" * 32, "space_suffix": "nordic", "prefix": "sbx",
        "registry": "registry-a.example.org", "pool": "10.10.0.0/16", "pool_prefix": "28", "attempts": "64",
        "network": "fed-a_default", "project": "fed-a", "admin_env": "FED_A_ADMIN", "users_env": "FED_A_USERS",
        "apptainer": "/usr/bin/apptainer", "host_python": "python3.11", "mkfs": "mkfs.ext3",
    },
    {
        "zone_host": "zone.fed-b.example.net", "pine_host": "n1.fed-b.example.net",
        "birch_host": "n2.fed-b.example.net", "cedar_host": "n3.fed-b.example.net",
        "hazel_host": "n4.fed-b.example.net", "hazel_id": "9b" * 16, "port": "9443",
        "pine_id": "6" * 32, "birch_id": "7" * 32, "cedar_id": "8" * 32, "space_id": "9" * 32, "beta_id": "0" * 32,
        "probe_id": "ab" * 16, "reader_id": "cd" * 16, "tools_id": "ef" * 16, "health_id": "12" * 16,
        "slot_reader_id": "34" * 16, "slot_tools_id": "56" * 16, "space_suffix": "austral", "prefix": "box",
        "registry": "registry-b.example.net", "pool": "172.20.0.0/14", "pool_prefix": "29", "attempts": "32",
        "network": "fed-b_net", "project": "fed-b", "admin_env": "FED_B_ADMIN", "users_env": "FED_B_USERS",
        "apptainer": "/opt/site/bin/apptainer", "host_python": "/usr/bin/python3", "mkfs": "/usr/sbin/mkfs.ext3",
    },
)  # fmt: skip


def render(template: dict, root: Path, values: dict, ca: bytes) -> Path:
    """Write a template's files into `root`, its `@key@` filled from `values` and the root itself, with the CA bundle,
    the journal's directory and the paths a loader checks; return the platform file."""
    filled = {**values, "root": str(root)}
    for name, text in template.items():
        for key, value in filled.items():
            text = text.replace(f"@{key}@", value)
        assert "@" not in text.replace("{user}", ""), f"{name}: a placeholder is left"
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if name == "address-map.json":
            path.chmod(0o600)  # the platform layer reads no map that others can read
    (root / "ca").mkdir(exist_ok=True)
    (root / "ca" / "federation-ca.pem").write_bytes(ca)
    (root / "fault-journal").mkdir(exist_ok=True)
    return root / "platform.toml"


def edit(template: str, *edits: tuple[str, str, str]) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    """A change to one template: each (file, text, replacement), the text found exactly once in the file."""
    return template, edits


def platform(old: str, new: str) -> tuple[str, str, str]:
    return "platform.toml", old, new


# Every key a loader of platform.toml accepts (`[i]` and a configured name are `*`), and the keys it reads by hand from
# a table it does not check with `reject_unknown` (`HAND_READ`), each in or out of the fingerprint. IN maps a key to an
# edit that changes that setting alone (with what the loader requires beside it), or to None where no one key can be
# changed alone and a test of its own shows the difference. A table is no setting: its keys are classified.
SWAP_CA = ("docker", "ca")
IN = {
    "tls.ca_file": SWAP_CA,
    "tls.insecure_tls": edit("docker", platform('tls = { ca_file = "@root@/ca/federation-ca.pem" }',
                                                "tls = { insecure_tls = true }")),
    "space.prefixes": edit("docker", platform('prefixes = ["alpha-"]\nimport', 'prefixes = ["alpha-", "al"]\nimport')),
    "space.import": edit("docker", platform("import = { readonly = true }\n", "")),
    "space.import.readonly": edit("docker", platform("{ readonly = true }", "{ readonly = false }")),
    "space.import.shared": edit("docker", platform("{ readonly = true }", "{ readonly = true, shared = true }")),
    "space.data_manifest": edit("docker", platform("import = { readonly = true }\n", 'import = { readonly = true }\n'
                                                   'data_manifest = "data-manifests/alpha.json"\n')),
    "support.providers": edit("docker", platform('providers = ["pine", "birch", "cedar"]\nlocal',
                                                 'providers = ["pine", "birch"]\nlocal')),
    "support.local": edit("docker", platform('providers = ["pine", "birch", "cedar"]\nlocal = "pine"',
                                             'providers = ["pine", "birch", "cedar"]\nlocal = "cedar"'),
                          platform('providers.pine = "@pine_host@"', 'providers.cedar = "@cedar_host@"')),
    "spaces.*.label": edit("apptainer", platform('label = "alpha"', 'label = "gamma"'),
                           platform('space = "alpha"', 'space = "gamma"')),
    "spaces.*.name": edit("apptainer", platform('name = "beta-space"', 'name = "beta-other"')),
    "spaces.*.prefixes": edit("apptainer", platform('prefixes = ["beta-"]', 'prefixes = ["beta-", "be"]')),
    "spaces.*.providers": edit("apptainer", platform('providers = ["rowan", "aspen", "hazel"]',
                                                     'providers = ["rowan", "aspen", "hazel", "larch"]')),
    "spaces.*.local": edit("apptainer", platform('local = "aspen"\nholder = "larch"',
                                                 'local = "hazel"\nholder = "larch"'),
                           platform('local = "aspen"\nholder = "rowan"', 'local = "hazel"\nholder = "rowan"'),
                           platform('providers.aspen = "aspen.invented.test"',
                                    'providers.hazel = "aspen.invented.test"')),  # the names are the local one's
    "spaces.*.holder": edit("apptainer", platform('holder = "rowan"\ndata', 'holder = "hazel"\ndata')),
    "spaces.*.tokens": edit("apptainer", platform('tokens = ["data_path", "tool_server"]', 'tokens = ["data_path"]')),
    "spaces.*.import": edit("apptainer", platform("import = { readonly = true, shared = false }\n", "")),
    "spaces.*.import.readonly": edit("apptainer", platform("readonly = true, shared = false",
                                                           "readonly = false, shared = false")),
    "spaces.*.import.shared": edit("apptainer", platform("readonly = true, shared = false",
                                                         "readonly = true, shared = true")),
    "spaces.*.data_manifest": edit("apptainer", platform("data-manifests/beta.json", "data-manifests/other.json")),
    "tokens.margin_s": edit("docker", platform("margin_s = 3000", "margin_s = 3001")),
    "tokens.max_excess_s": edit("docker", platform("max_excess_s = 600", "max_excess_s = 601")),
    "tokens.mint_headroom_s": edit("docker", platform("mint_headroom_s = 300", "mint_headroom_s = 299")),
    "tokens.max_ttl_s": edit("docker", platform("max_ttl_s = 86400", "max_ttl_s = 86399")),
    "tokens.revocation_settle_s": edit("docker", platform("revocation_settle_s = 330", "revocation_settle_s = 331")),
    "toolserver.commit": edit("docker", platform(COMMIT, "f" * 40)),
    "toolserver.command": edit("docker", platform('command = "@root@/venv/bin/server"',
                                                  'command = "@root@/venv/bin/other"')),
    "toolserver.args": edit("docker", platform('"--max-rows", "50"', '"--max-rows", "51"')),
    "toolserver.start_timeout_s": edit("docker", platform("start_timeout_s = 120", "start_timeout_s = 121")),
    "toolserver.conditions": edit("docker", platform("[toolserver.conditions.reader]",
                                                     "[toolserver.conditions.lookup]")),
    "toolserver.conditions.*.allow": edit("docker", platform('allow = ["list_files", "read_file"]',
                                                             'allow = ["read_file"]')),
    "toolserver.conditions.*.rights": edit("docker", platform('rights = ["space_view", "space_read_data"]\nmanifest',
                                                              'rights = ["space_view", "space_read_data", '
                                                              '"space_view_qos"]\nmanifest')),
    "toolserver.conditions.*.manifest": edit("docker", platform("manifests/reader.json", "manifests/other.json")),
    "sandbox.backend": None,  # a Docker and an Apptainer [sandbox] differ in the keys they take: a test of its own
    "sandbox.agent_uid": edit("docker", platform("agent_uid = 10001", "agent_uid = 10002")),
    "sandbox.python": edit("docker", platform('python = "python"', 'python = "python3"')),
    "sandbox.trust_paths": edit("docker", platform(', "/opt/certs/extra.pem"]', "]")),
    "sandbox.placeholder_token": edit("docker", platform('placeholder_token = "placeholder"',
                                                         'placeholder_token = "nothing"')),
    "sandbox.kill_grace_s": edit("docker", platform("kill_grace_s = 5", "kill_grace_s = 6")),
    "sandbox.content_check_provider": edit("docker", platform(CHECK_FILE_LINE, CHECK_FILE_LINE + CHECK_PROVIDER_LINE)),
    "sandbox.images.agent.id": edit("docker", platform(SHA["agent"], SHA["sif"])),
    "sandbox.images.gate.id": edit("docker", platform(SHA["gate"], SHA["sif"])),
    "sandbox.images.oneclient.id": edit("docker", platform(SHA["oneclient"], SHA["sif"])),
    "sandbox.limits.cpus": edit("docker", platform("cpus = 2.0", "cpus = 2.5")),
    "sandbox.limits.memory_bytes": edit("docker", platform("memory_bytes = 8589934592", "memory_bytes = 8589934593")),
    "sandbox.limits.pids": edit("docker", platform("pids = 512", "pids = 513")),
    "sandbox.limits.nofile": edit("docker", platform("nofile = 4096", "nofile = 4097")),
    "sandbox.limits.tmp_bytes": edit("docker", platform("tmp_bytes = 1073741824", "tmp_bytes = 1073741825")),
    "sandbox.limits.shm_bytes": edit("docker", platform("shm_bytes = 1073741824", "shm_bytes = 1073741825")),
    "sandbox.limits.work_disk_bytes": edit("docker", platform("work_disk_bytes = 4294967296",
                                                              "work_disk_bytes = 4294967297")),
    "sandbox.limits.gate_memory_bytes": edit("docker", platform("gate_memory_bytes = 1073741824",
                                                                "gate_memory_bytes = 1073741825")),
    "sandbox.gate.upstream_read_timeout_s": edit("docker", platform("upstream_read_timeout_s = 3600",
                                                                    "upstream_read_timeout_s = 3601")),
    "sandbox.mount.path": edit("docker", platform('path = "/data"', 'path = "/space"')),
    "sandbox.mount.io_mode": edit("docker", platform('io_mode = "proxy"', 'io_mode = "direct"\n'
                                                     'direct_check_file = "alpha-space/f"\n'
                                                     'storages = { "st1" = "/srv/storage-1" }')),
    "sandbox.mount.extra_args": edit("docker", platform('"--provider-timeout=60"', '"--provider-timeout=61"')),
    "sandbox.data_paths": edit("docker", platform('mounted = "mount"\n', 'mounted = "mount"\nbare = "none"\n')),
    "sandbox.data_paths.*.kind": edit("docker", platform('mounted = "mount"', 'mounted = "none"')),
    "sandbox.data_paths.*.listing": edit("docker", platform('listing = "paged"', 'listing = "complete"')),
    "sandbox.served_bytes.max_files": edit("docker", platform("max_files = 1000", "max_files = 1001")),
    "sandbox.served_bytes.max_directories": edit("docker", platform("max_directories = 1000",
                                                                    "max_directories = 1001")),
    "sandbox.served_bytes.max_ranges_per_file": edit("docker", platform("max_ranges_per_file = 64",
                                                                        "max_ranges_per_file = 65")),
    "sandbox.apptainer.image_sha256": edit("apptainer", platform(SHA["sif"], SHA["gate"])),
    "sandbox.apptainer.python": edit("apptainer", platform('python = "python3"', 'python = "python3.12"')),
    "sandbox.apptainer.egress_shim_sha256": edit("apptainer", platform(SHA["shim"], SHA["gate"])),
    "sandbox.apptainer.egress_collector_sha256": edit("apptainer", platform(SHA["collector"], SHA["gate"])),
    "sandbox.apptainer.memory_poll_s": edit("apptainer", platform("memory_poll_s = 0.1", "memory_poll_s = 0.2")),
    "sandbox.apptainer.disk_poll_s": edit("apptainer", platform("disk_poll_s = 1.0", "disk_poll_s = 2.0")),
    "sandbox.apptainer.hidden_memory_margin_bytes": edit("apptainer", platform(
        "hidden_memory_margin_bytes = 134217728", "hidden_memory_margin_bytes = 134217729"
    )),
    "sandbox.apptainer.preload": edit("apptainer", platform(f'preload = [{{ path = "@root@/pins/hook.so", sha256 = '
                                                            f'"{SHA["preload"]}" }}]\n', "")),
    "sandbox.apptainer.preload.*.sha256": edit("apptainer", platform(SHA["preload"], SHA["gate"])),
    "sandbox.apptainer.binds": edit("apptainer", platform(f'binds = [{{ path = "@root@/pins/extra.conf", sha256 = '
                                                          f'"{SHA["bind"]}", target = "/etc/extra.conf" }}]\n', "")),
    "sandbox.apptainer.binds.*.sha256": edit("apptainer", platform(SHA["bind"], SHA["gate"])),
    "sandbox.apptainer.binds.*.target": edit("apptainer", platform('target = "/etc/extra.conf"',
                                                                   'target = "/etc/other.conf"')),
    "sandbox.apptainer.gate_options": edit("apptainer", platform("read_timeout_s = 30", "read_timeout_s = 31")),
    "sandbox.apptainer.oneclient_bundle_sha256": edit("apptainer", platform(SHA["bundle"], SHA["gate"])),
    "sandbox.apptainer.provider_storage": edit("apptainer", platform('provider_storage = "tmpfs"',
                                                                     'provider_storage = "shared_filesystem"')),
    "sandbox.apptainer.work_disk": edit("apptainer", platform('work_disk = "directory"', 'work_disk = "image"')),
    # another collector is another experiment (O91); a platform that declares none keeps the fingerprint it had
    "sandbox.apptainer.readlog_collector_sha256": edit("apptainer", platform(
        'work_disk = "directory"', f'work_disk = "directory"\nreadlog_collector_sha256 = "{SHA["gate"]}"'
    )),
    "placement.holder": edit("docker", platform('holder = "birch"', 'holder = "pine"')),
    "placement.map": edit("docker", platform('map = "ranges.toml"\n', "")),
    "placement.map.*.file": edit("docker", ("ranges.toml", 'file = "dir/file.bin"', 'file = "dir/other.bin"')),
    "placement.map.*.provider": edit("docker", ("ranges.toml", 'provider = "cedar"', 'provider = "pine"')),
    "placement.map.*.ranges": edit("docker", ("ranges.toml", "[[0, 4096]]", "[[0, 4097]]")),
    "placement.map.*.space": edit("apptainer", platform('space = "alpha"', 'space = "beta"')),
    "placement.map_file.map": edit("docker", ("ranges.toml", "[[map]]\n", '[[map]]\nfile = "dir/more.bin"\n'
                                              'provider = "pine"\nranges = [[0, 1]]\nspace = "space"\n\n[[map]]\n')),
    "faults.sanctioned": edit("docker", platform("sanctioned = true", "sanctioned = false")),
    "faults.services": edit("docker", platform('cedar = "cedar"\n', "")),
    "faults.services.*": edit("docker", platform('cedar = "cedar"\n', 'cedar = "cedar-2"\n')),
    "faults.backend": None,  # a Compose and an HPC [faults] differ in the keys they take: a test of its own
    "faults.poll_s": edit("docker", platform("poll_s = 2\nsettle", "poll_s = 3\nsettle")),
    "faults.settle_timeout_s": edit("docker", platform("settle_timeout_s = 180\ncommand",
                                                       "settle_timeout_s = 181\ncommand")),
    "faults.command_timeout_s": edit("docker", platform("command_timeout_s = 120", "command_timeout_s = 121")),
    "faults.ready_timeout_s": edit("docker", platform("ready_timeout_s = 180", "ready_timeout_s = 181")),
    "faults.clear_attempts": edit("docker", platform("clear_attempts = 3", "clear_attempts = 4")),
    "faults.admission_timeout_s": edit("docker", platform("admission_timeout_s = 3600", "admission_timeout_s = 3601")),
    "faults.health_wait_s": edit("docker", platform("health_wait_s = 30", "health_wait_s = 31")),
    "faults.gate_timeout_s": edit("docker", platform("gate_timeout_s = 240", "gate_timeout_s = 241")),
    "faults.heal": edit("docker", platform('heal = "off"', 'heal = "link"')),
    "faults.heal_timeout_s": edit("docker", platform('heal = "off"', 'heal = "off"\nheal_timeout_s = 60')),
    "faults.heal_settle_s": edit("docker", platform("heal_settle_s = 180", "heal_settle_s = 181")),
    "faults.session_drain_s": edit("docker", platform("session_drain_s = 320", "session_drain_s = 321")),
    "faults.heal_container_fallback": edit("docker", platform("heal_container_fallback = true",
                                                              "heal_container_fallback = false")),
    "faults.stop_bound_s": edit("apptainer", platform("stop_bound_s = 690", "stop_bound_s = 691")),
    "faults.relay_wait_s": edit("apptainer", platform("relay_wait_s = 30", "relay_wait_s = 31")),
    "faults.step_launch_wait_s": edit("apptainer", platform("step_launch_wait_s = 240", "step_launch_wait_s = 241")),
    "space.name": edit("docker", platform('name = "alpha-space"', 'name = "alpha-other"')),
    "placement.clock_skew_allowance_s": edit("docker", platform("clock_skew_allowance_s = 5.0",
                                                                "clock_skew_allowance_s = 6.0")),
    "placement.transfer_settle_s": edit("docker", platform("transfer_settle_s = 30.0", "transfer_settle_s = 31.0")),
    "placement.transfer_settle_polls": edit("docker", platform("transfer_settle_polls = 4",
                                                             "transfer_settle_polls = 5")),
    "placement.probe_attempts": edit("docker", platform("probe_attempts = 3", "probe_attempts = 4")),
    "placement.probe_backoff_s": edit("docker", platform("probe_backoff_s = 1.0", "probe_backoff_s = 1.5")),
    "placement.poll_s": edit("docker", platform("poll_s = 2.0\ntransfer", "poll_s = 2.5\ntransfer")),
    "placement.settle_timeout_s": edit("docker", platform("settle_timeout_s = 300", "settle_timeout_s = 301")),
    "placement.consecutive_polls": edit("docker", platform("consecutive_polls = 2", "consecutive_polls = 3")),
    "placement.transfer_timeout_s": edit("docker", platform("transfer_timeout_s = 3600", "transfer_timeout_s = 3601")),
    "placement.request_timeout_s": edit("docker", platform("request_timeout_s = 900", "request_timeout_s = 901")),
    "placement.chunk_bytes": edit("docker", platform("chunk_bytes = 16777216", "chunk_bytes = 8388608")),
    "placement.fresh_connection_bytes": edit("docker", platform("fresh_connection_bytes = 16777216",
                                                                "fresh_connection_bytes = 8388608")),
}  # fmt: skip

# Why each key that is in is in, by the table (the longest prefix that names it): what the PR's table says.
IN_REASONS = {
    "tls": "every client's certificate check, by its mode and the CA bundle's SHA-256",
    "space": "the one-space form's space as the agent sees it: its name (hashed), prefixes (hashed), import, data",
    "support": "the providers that hold the space and the agent's own, by label",
    "spaces": "each [[spaces]] entry's declared identity, in its digest (C06)",
    "tokens": "a run's token lifetime: a token that expires fails the run's last check of its tool plane (O06)",
    "toolserver": "the tool server's code (commit), command line (federation-neutral), start bound and conditions",
    "sandbox": "what the agent runs in and is held to: images by id, limits, mount, interpreter, user, grace, pins",
    "sandbox.gate": "when the gate gives up a hung upstream read, which ends an agent's REST read in an outage, only "
    "when it is not the default (O85)",
    "sandbox.data_paths": "each level's data path, listing and I/O mode (O21, O36)",
    "sandbox.served_bytes": "the caps of a run's tables of the bytes served, which bound its evidence (O22)",
    "sandbox.apptainer": "the Apptainer backend's pins and watch (image, egress shim and collector, memory guard: O56) "
    "and the kind of storage the providers keep their data on, tmpfs or shared (O57)",
    "placement": "where a run's bytes lie (data provider, range map), and the bounds its probes and ingest read",
    "faults": "the injector's sanction, targets and every bound of its restore and heal, which decide a run's fate",
}


PER_FEDERATION = "per federation: an address, an id, a port, or a host's, network's or deployment's name, each up's own"
PATH = "a path on the host or in the federation's root (what lies there is in by its digest where it shapes a run)"
SECRET = "a secret, or where one is read from"
HOST = "the host's or the site's own: how a host step is run or named, never what the agent sees or is held to"
PRECAMPAIGN = "decides only what is checked before a campaign, never a run's input (O35)"
C35 = (
    "how a reset reaches a state it then verifies, or how many requests are in flight, and no reading's result: "
    "recorded and compared at every resume (C35)"
)
ADDRESSES = "one federation's own paths: the map of its declared names' addresses and the launcher that reads it (O73)"
USER = "a platform user's name (O12): who holds a plane is the federation's; what it may do is its condition's rights"

OUT = {
    "zone.name": "the label messages give the zone; nothing of a run reads it",
    "zone.url": PER_FEDERATION,
    "providers.*.url": PER_FEDERATION,
    "providers.*.id": PER_FEDERATION,
    "space.id": PER_FEDERATION,
    "spaces.*.id": PER_FEDERATION + "; in its id-bearing digest while the entry declares no data, so such shards never "
                                    "merge (C06)",
    "credentials.admin.env": SECRET,
    "credentials.admin.file": SECRET,
    "credentials.users.directory_env": SECRET,
    "credentials.users.directory": SECRET,
    "credentials.users.pattern": SECRET,
    **{f"principals.{plane}.username": USER for plane in ("data_path", "toolserver", "service")},
    **{f"principals.{plane}.user_id": PER_FEDERATION for plane in ("data_path", "toolserver", "service")},
    "toolserver.clone": PATH,
    "sandbox.work_root": PATH,
    "sandbox.name_prefix": HOST,
    "sandbox.label_namespace": HOST,
    "sandbox.sudo": HOST,
    "sandbox.earlyoom_comm": HOST + " (earlyoom's kills are infrastructure, never a run's outcome: O17)",
    "sandbox.content_check_file": PRECAMPAIGN,
    **{f"sandbox.images.{image}.name": "a tag, a registry's host: an image is in by its pinned id alone"
       for image in ("agent", "gate", "oneclient")},
    "sandbox.network.pools": PER_FEDERATION,
    "sandbox.network.prefix": HOST + " (the size of each sandbox's own network, which needs two addresses)",
    "sandbox.network.max_attempts": HOST,
    "sandbox.mount.storages": PER_FEDERATION + ", and the host's paths; the mode that reads them, direct, is in",
    "sandbox.mount.direct_check_file": "read at every start to show direct I/O in effect, by the space's name (O40)",
    "sandbox.federation.network": PER_FEDERATION,
    "sandbox.federation.zone": PER_FEDERATION,
    "sandbox.federation.providers": PER_FEDERATION,
    "addresses.file": ADDRESSES,
    "addresses.resolver": ADDRESSES,
    "sandbox.apptainer.apptainer": HOST,
    "sandbox.apptainer.work_disk_mkfs": HOST + " (the node's mkfs, one program word with no option, refused at load "
    "otherwise, so it names only where the tool is; the run records its version, `work_disk.mkfs`; the mode that uses "
    "it, `work_disk`, is in)",
    "sandbox.apptainer.image": PATH,
    "sandbox.apptainer.host_python": HOST + " (the node's interpreter for the gate, as the driver's own is)",
    "sandbox.apptainer.egress_shim": PATH,
    "sandbox.apptainer.egress_collector": PATH,
    "sandbox.apptainer.oneclient_bundle": PATH,
    "sandbox.apptainer.preload.*.path": PATH,
    "sandbox.apptainer.binds.*.path": PATH,
    **{f"placement.{key}": C35 for key in (
        "concurrency", "qos_scan", "qos_fulfil_timeout_s", "reset_concurrency", "reset_shortcut",
        "reset_exclusive_wait_s", "provider_concurrency",
    )},
    "slots.*.data_path.username": USER,
    "slots.*.data_path.user_id": PER_FEDERATION,
    "slots.*.toolserver.username": USER,
    "slots.*.toolserver.user_id": PER_FEDERATION,
    "slots.*.toolserver.rights": "which conditions may lease the slot; a run's plane holds exactly its condition's "
                                 "rights, which are in (the launcher requires them)",
    "faults.journal": PATH,
    "faults.compose_file": PATH,
    "faults.project": PER_FEDERATION,
    "faults.command": HOST,
    "faults.docker": HOST,
    **{f"link_check.{key}": PRECAMPAIGN for key in ("name", "id", "prefixes", "providers", "probe_bytes", "timeout_s",
                                                      "visible_timeout_s", "evict_timeout_s", "every_s")},
}  # fmt: skip

# The keys a loader reads by hand, from a table it checks itself or one keyed by configured names, which
# `reject_unknown` never sees: a data path's table, an Apptainer pin's entry, a service of [faults.services].
HAND_READ = frozenset({
    "sandbox.data_paths.*.kind", "sandbox.data_paths.*.listing", "sandbox.apptainer.preload.*.path",
    "sandbox.apptainer.preload.*.sha256", "sandbox.apptainer.binds.*.path", "sandbox.apptainer.binds.*.sha256",
    "sandbox.apptainer.binds.*.target", "faults.services.*",
})  # fmt: skip


def normalised(where: str) -> str:
    """The table a loader's `Section` names, as the classification names it: `platform.toml.providers[0]` and
    `[[map]] row 1` become `providers.*` and `placement.map.*`."""
    import re

    if where == "the map file":
        return "placement.map_file"
    if where.startswith(("[[map]] row", "[[placement.map]] row")):
        return "placement.map.*"
    name = where.removeprefix("platform.toml").lstrip(".")
    name = name.strip("[]")
    name = re.sub(r"\[\d+\]?", ".*", name)
    return re.sub(r"\.\*(?=[^.])", ".*.", name)
