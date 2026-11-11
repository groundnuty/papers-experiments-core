# A throwaway Onedata federation in Docker Compose

A Onezone and Oneproviders on this machine, for the live contract tests, the day-0 checks (#29) and federation
studies, as a layout file declares them. Without one, the layout is `federation.toml` here: one Onezone and three
Oneproviders. Every name in a layout is invented.

## Commands

```sh
uv run python deploy/compose-federation/federation.py up       # from nothing to a bootstrapped federation
uv run python deploy/compose-federation/federation.py reset    # back to the baseline, keeping every id
uv run python deploy/compose-federation/federation.py down     # nothing of it left, on Docker or on disk
uv run python deploy/compose-federation/federation.py status   # the project's containers, the root's parent
uv run python deploy/compose-federation/federation.py memory   # each service's limit, current use and peak
uv run python deploy/compose-federation/federation.py describe # the deployment's table for the day-0 report
uv run python deploy/compose-federation/federation.py compose  # the Compose file the layout generates
```

Each takes `--layout PATH` for a layout other than the default; give every command of one federation the same layout.
Run them outside any sandbox: they drive Docker, and they reach the services on the host's loopback.

- **up** checks that the host's available memory holds the services' limits together (before a first start), makes
  the root, the CA, one web certificate per service and the secrets, starts the zone, signs in as its admin, makes the
  admin's access token and the declared users, writes one registration token per provider, starts the providers, waits
  until each is online, makes the spaces, has each supported by exactly its owner and its mirrors, makes the users
  members of their spaces with exactly their privileges, writes the baseline files, and writes `facts.json` and, from
  the layout's platform template, `platform.toml`. Run on a federation that is up, it starts what is stopped and checks
  every step again.
- **reset** starts what is stopped, checks every step again, removes everything in every space and writes the baseline
  files again. The providers, spaces and users keep their ids, so `platform.toml` stays valid. A fault-injection
  journal left behind is emptied.
- **down** reads each service's peak memory, runs `docker compose down` for the project, then removes the root with
  everything in it, through a container of the project with no network (the services write their data as root), and
  checks that no container, network or file of the project is left.

## Declaring a layout

A layout is a TOML file; `federation.toml` is the default and the reference, and `layouts/five-providers.toml` is an
example with five providers and four spaces, one provider mirroring a second owner's space. Its generated Compose file
is `layouts/five-providers.compose.yaml`.

| Key | Holds |
|---|---|
| `project` | the Compose project name, as Compose accepts one (`[a-z0-9][a-z0-9_-]*`); the root's mark holds it |
| `domain` | the services' domain on the project's network, under `.test` |
| `study_space` | the space the core studies; its owner is the data provider |
| `memory_limit` | each service's memory limit (default `6g`); `[zone]` and `[providers.<name>]` may set their own |
| `support_bytes` | each provider's support of each space (default 10 GiB); `[spaces.<name>]` may set its own |
| `platform_template` | optional: the template of `platform.toml`, relative to the layout file |
| `[zone] port`, `[providers.<name>] port` | each service's port on `127.0.0.1` |
| `[spaces.<name>] owner`, `mirrors` | the provider the space's data is written through and kept on, and the other providers that support it |
| `[principals.<user>] privileges`, `spaces` | a fixed user's exact privileges, in the study space unless `spaces` names others |

Every key is checked when the file is read, and a refusal names the file and the key: names are unique and usable as
Compose services and domain labels, ports lie in 1024-65535 and never clash, every owner and mirror is a declared
provider, the study space and every member's space are declared, and nothing but `127.0.0.1` can be named as an
address. The images stay the two pinned ones whatever the layout says.

The Compose file is generated from the layout at every command (`fedcompose.py`), never edited: what keeps the
federation to itself is part of the generator. `compose.yaml` here is the default layout's output, committed for
review, and a test keeps it, and the example's, equal to what the generator makes.

A layout with no `platform_template` gets `facts.json` alone. A layout's template names its providers, spaces and users
by placeholder (`@provider_id.<name>@`, `@space_id.<name>@`, `@user_id.<user>@`, `@zone_url@`, `@space_name@`).

## What it touches

- **Docker:** only the layout's Compose project (default `expcore-fed`): its services, its network `<project>_fed`,
  and down's short-lived `janitor`. Every container carries a label naming the root it binds, and a command refuses,
  before it touches anything, a project whose containers bind another root or name none: that is another federation of
  the same name. The two images are exactly `docker.onedata.org/onezone:ID-ba7a778696` and
  `docker.onedata.org/oneprovider:ID-765309c84f`, never pulled (`pull_policy: never`) and never rebuilt. Nothing is
  pruned.
- **Ports:** each service publishes its 443 on `127.0.0.1` only, on the port the layout gives it (the default: the
  zone on 29443, `pine` on 29444, `birch` on 29445, `cedar` on 29446). Nothing else is published. On the project's
  network the services reach one another by their domains (`zone.<domain>`, `<provider>.<domain>`), which resolve
  nowhere else.
- **Memory:** each service is limited to the layout's `memory_limit`. A first up refuses when the limits together
  exceed the host's available memory, so the federation never starves the host. `memory` reads each service's current
  use and its peak since the container started from the container's own cgroup, and down prints the peaks before it
  stops anything; the day-0 report records them.
- **Disk:** the root, `EXPCORE_FED_ROOT` (mode 0700), and nothing outside it. The root must lie in the allowed parent,
  `EXPCORE_FED_ROOT_PARENT` (default `/mnt/volume2/expcore-federation`), which must be absolute and not `/`, with no
  symbolic link from it down to the root. Under the default parent the root defaults to the parent itself. A study
  confined to a directory of its own names that directory as the parent; it must then exist, and `EXPCORE_FED_ROOT`
  must be named and lie strictly below it, so a parent that holds other data is never itself the root. Every command
  checks this before it touches a file or a container, and `facts.json` and `status` record the parent and which
  setting gave it. Down deletes nothing in a root without the mark (`.expcore-fed-root`) naming the layout's project,
  so a root belongs to one layout: give a second layout's federation a root of its own. The root holds:

  | Path | Holds |
  |---|---|
  | `ca/federation-ca.pem` | the federation's CA, the one file the core trusts (`[tls] ca_file`) |
  | `ca/<service>.pem` | each service's web certificate, for its domain and `127.0.0.1` |
  | `secrets/` (0700, every file 0600) | the CA's key, the services' keys, the panels' passphrases and env files, the registration tokens, `admin.token`, and `users/<user>.pass` |
  | `<service>/persistence` | each service's state; up seeds the certificates and the CA into it before the first start |
  | `<provider>/storage` | each provider's POSIX storage |
  | `facts.json` | what an integrator needs to write `platform.toml`: addresses, ids, names, where the credentials are; no secret |
  | `platform.toml` | rendered from the layout's template, next to the CA and the secrets it names by relative path |
  | `fault-journal/` | the fault injector's journal, when the fault test runs |

  Nothing is kept between a down and the next up: up makes a new CA and new secrets every time.

## Certificates

The federation has its own CA (RSA, 30 days), made by `openssl` at up. Each service's certificate names its domain on
the project's network, its node (`node1.<domain>`) and `127.0.0.1`, so the core verifies it at the published port.
Up seeds the certificate, its key and the CA into each service's persistence directory, where the image's panel and
worker read them, and `start.sh` adds the CA to each container's system store, which the providers' transfer service
reads. Verification stays on everywhere; the images' test CA and their insecure-connection setting are not used.

The zone's GUI package check is off (`gui_package_verification`, as the images' own demo mode does), because the
images are development builds whose provider GUI packages the zone would refuse. Only the browser interface uses them.

## The baseline

Every space's files are written through its owner. In the default layout:

- `alpha-space`, the study space, written through `pine` (the data provider): `hello.txt` (64 bytes), `replicated.bin`
  (1 MiB, also replicated to the first mirror, `birch`, so the space lists an ended transfer), `samples/sample-1.bin`
  and `samples/sample-2.bin`, and `listing-1200/` with 1200 entries of 16 bytes, for the complete-listing test
  (`EXPCORE_LIVE_LISTING_PATH`). A study space without a mirror gets no replica and no transfer record.
- `beta-space`, written through `birch`: `beta.txt`. Every other space holds one such file, named after it. Its name
  is outside the study's prefix, so the core may never change it.

Every file's bytes are derived from its path, so each reset writes the same files.

## Running the core against it

`platform.toml` sits in the root, with every path relative to it. For the live tests:

```sh
EXPCORE_LIVE_PLATFORM=/mnt/volume2/expcore-federation/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt \
EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_SLOW=1 uv run pytest tests/live/test_contract.py
uv run python -m expcore_onedata.day0 /mnt/volume2/expcore-federation/platform.toml --out <directory>
```

The sandbox tests (`EXPCORE_LIVE_DOCKER`) start containers and networks of their own, outside this project, so they
are not run against it here.

`day0/` holds the dated reports: the day-0 checks' own (`day0-<UTC>.md` and `.json`) and the facts they do not
measure (`<date>-federation-facts.md`): releases, live test results, the fault test, what listens on the host, the
commands' timings and what is unknown. `measure_partial_file.py` measures how a file at its final size with only a
prefix written reads back through each provider; its commands and results are in the facts file.
