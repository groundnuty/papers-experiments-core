# A throwaway Onedata federation in Docker Compose

One Onezone and three Oneproviders on this machine, for the live contract tests and the day-0 checks (#29). Every name in
it is invented: the domain `expcore-fed.test`, the providers `pine`, `birch` and `cedar`, the spaces `alpha-space` and
`beta-space`, the users `alpha-reader`, `alpha-tools` and `alpha-health`.

## Commands

```sh
uv run python deploy/compose-federation/federation.py up       # from nothing to a bootstrapped federation
uv run python deploy/compose-federation/federation.py reset    # back to the baseline, keeping every id
uv run python deploy/compose-federation/federation.py down     # nothing of it left, on Docker or on disk
uv run python deploy/compose-federation/federation.py status
```

Run them outside any sandbox: they drive Docker, and they reach the services on the host's loopback.

- **up** makes the root, the CA, the four web certificates and the secrets, starts the zone, signs in as its admin,
  makes the admin's access token and the three users, writes one registration token per provider, starts the
  providers, waits until each is online, makes the two spaces, has them supported (`alpha-space` by all three,
  `beta-space` by `birch` and `cedar`), makes the three users members of `alpha-space` with exactly `space_view` and
  `space_read_data`, writes the baseline files, and writes `facts.json` and, from `platform.toml.template`,
  `platform.toml`. Run on a federation that is up, it starts what is stopped and checks every step again.
- **reset** starts what is stopped, checks every step again, removes everything in both spaces and writes the baseline
  files again. The providers, spaces and users keep their ids, so `platform.toml` stays valid. A fault-injection
  journal left behind is emptied.
- **down** runs `docker compose down` for this project, then removes the root with everything in it, through a
  container of the project with no network (the services write their data as root), and checks that no container,
  network or file of the project is left.

## What it touches

- **Docker:** only the Compose project `expcore-fed`: its four services, its network `expcore-fed_fed`, and down's
  short-lived `janitor`. The two images are exactly `docker.onedata.org/onezone:ID-ba7a778696` and
  `docker.onedata.org/oneprovider:ID-765309c84f`, never pulled (`pull_policy: never`) and never rebuilt. Nothing is
  pruned.
- **Ports:** each service publishes its 443 on `127.0.0.1` only: the zone on 29443, `pine` on 29444, `birch` on
  29445, `cedar` on 29446. Nothing else is published. On the project's network the services reach one another by
  their domains (`zone.expcore-fed.test`, `pine.expcore-fed.test`, ...), which resolve nowhere else.
- **Disk:** the root, `EXPCORE_FED_ROOT` (default `/mnt/volume2/expcore-federation`, mode 0700), and nothing outside
  it:

  | Path | Holds |
  |---|---|
  | `ca/federation-ca.pem` | the federation's CA, the one file the core trusts (`[tls] ca_file`) |
  | `ca/<service>.pem` | each service's web certificate, for its domain and `127.0.0.1` |
  | `secrets/` (0700, every file 0600) | the CA's key, the services' keys, the panels' passphrases and env files, the registration tokens, `admin.token`, and `users/<user>.pass` |
  | `<service>/persistence` | each service's state; up seeds the certificates and the CA into it before the first start |
  | `<provider>/storage` | each provider's POSIX storage |
  | `facts.json` | what an integrator needs to write `platform.toml`: addresses, ids, names, where the credentials are; no secret |
  | `platform.toml` | rendered from the template, next to the CA and the secrets it names by relative path |
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

- `alpha-space`, written through `pine` (the data provider): `hello.txt` (64 bytes), `replicated.bin` (1 MiB, also
  replicated to `birch` so the space lists an ended transfer), `samples/sample-1.bin` and `samples/sample-2.bin`, and
  `listing-1200/` with 1200 entries of 16 bytes, for the complete-listing test (`EXPCORE_LIVE_LISTING_PATH`).
- `beta-space`, written through `birch`: `beta.txt`. Its name is outside the study's prefix, so the core may never
  change it.

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
