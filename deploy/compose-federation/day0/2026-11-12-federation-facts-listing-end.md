# The Compose federation: how a complete listing ends, 12 Nov 2026

A fresh `up` of the default layout (210 s), shortly before midnight UTC on 11 Nov; the listings were measured before
midnight, the mount test after. Through each provider's REST API, with the platform layer's own client
(`OnezoneClient.get` with the admin credential, from the federation's `platform.toml`), every page of a directory's
children and of the study space's ended transfers was read, and its top-level keys and end markers recorded. The live
test of the mount path then ran on the same federation (last section). The earlier facts files stay as they were. Every
name below is the federation's own invented one: `pine` is the data provider, which wrote the baseline into
`alpha-space`, and `birch` is its first mirror. No token value is recorded here: a token is described by its JSON type
and its length.

The calls:

- **children:** `GET /data/{id}/children` with `limit` and `attribute` `name`, `file_id`, `size`, `type` (the
  provisioner's own), the next page asked with `token` set to the previous page's `nextPageToken`, followed while
  `isLast` is false;
- **transfers:** `GET /spaces/{space}/transfers` with `state` `ended` and `limit`, the next page asked with
  `page_token`, followed while `nextPageToken` is a non-empty string.

Every listing ran on `pine` and on `birch`, with the same shapes on both, and the whole measurement ran twice in a row,
with the same shapes both times.

## Children

The space root holds 4 entries (`hello.txt`, `listing-1200`, `replicated.bin`, `samples`); `listing-1200` holds 1200.
Each run listed every entry once.

| Directory | `limit` | Pages | Entries per page | Pages before the last | The last page |
|---|---|---|---|---|---|
| space root | 1000 | 1 | 4 | none | `isLast` true, token a string of 148 |
| space root | 7 | 1 | 4 | none | `isLast` true, token a string of 148 |
| space root | 4 | 2 | 4, 0 | `isLast` false, token a string of 148 | no entries, `isLast` true, token a string of 71 |
| space root | 2 | 3 | 2, 2, 0 | `isLast` false, tokens strings of 155 and 148 | no entries, `isLast` true, token a string of 71 |
| `listing-1200` | 1000 | 2 | 1000, 200 | `isLast` false, token a string of 158 | `isLast` true, token a string of 158 |
| `listing-1200` | 7 | 172 | 7 on each of 171, then 3 | all 171: `isLast` false, token a string of 158 | `isLast` true, token a string of 158 |
| `listing-1200` | 600 | 3 | 600, 600, 0 | `isLast` false, token a string of 158 | no entries, `isLast` true, token a string of 71 |

Every page of every run had exactly three top-level keys, in the order `nextPageToken`, `isLast`, `children`. `isLast`
was a JSON boolean on every page: false on each page before the last, true on the last. `nextPageToken` was present
and a non-empty JSON string on every page, the last included; it was never null, empty or absent.

- The end of a children listing is `isLast` true, and only that: the last page still carries a token.
- A page that fills the limit and holds the directory's last entries says `isLast` false, and one more page follows,
  with no entries, `isLast` true and a token of 71 characters.
- Asking for the page after a last page, with the last page's token, answered 400 with error id `posix` and errno
  `eagain`, from every run's last page, the empty ones included.

## Transfers

The space listed 5 ended transfers: the baseline's replication of `replicated.bin` to `birch`, and four that
`../measure_transfer_shapes.py`, run once just before, left behind (two replications and two evictions). Each run
listed 5 distinct ids.

| `limit` | Pages | Ids per page | Pages before the last | The last page |
|---|---|---|---|---|
| 1 | 6 | 1 on each of 5, then 0 | token a string of 16 | no ids, token null |
| 2 | 3 | 2, 2, 1 | token a string of 16 | 1 id, token null |
| 5 | 2 | 5, 0 | token a string of 16 | no ids, token null |
| 1000 | 1 | 5 | none | 5 ids, token null |

Every page had exactly two top-level keys, in the order `transfers`, `nextPageToken`, and none named `isLast`.
`nextPageToken` was present on every page: a JSON string of 16 characters on each page before the last, JSON null on
the last.

- The end of a transfer listing is `nextPageToken` null: the key stays, and there is no `isLast`.
- A page that fills the limit gets a token, and one more page follows, with no ids and a null token.

## The two listings side by side

| | Children | Transfers |
|---|---|---|
| Top-level keys, in order | `nextPageToken`, `isLast`, `children` | `transfers`, `nextPageToken` |
| `isLast` | a boolean: false, then true on the last page | absent |
| `nextPageToken` before the last page | a non-empty string | a string of 16 |
| `nextPageToken` on the last page | a non-empty string (71 to 158 characters) | null |
| The entries end exactly at a full page | one more page, empty | one more page, empty |
| The next page's query parameter | `token` | `page_token` |

## The mount test on the same federation

`onedata/oneclient:25.0` was gone from the host, so it was pulled again by its digest,
`sha256:aff60c5ff78c99f4cb0c5d0acb5392b37a1fdd3cdd73b73bca9b640d067a51d8`, which is also its id; nothing else was
pulled. `tests/live/test_mount_path.py`, on #116's branch with `main` merged in (several spaces, #159; the testing
module, #160; the published client through the gate, #163), with `EXPCORE_LIVE_ONECLIENT_IMAGE` naming the image by
that digest and the default oneclient arguments:

| Run | Result |
|---|---|
| first | 14 passed, 1 failed in 152 s: the probes now key a provider by its position (`providers[i]`), and the test still looked it up by name |
| after the test read the probes by position | 15 passed in 153 s |

In the first run the probes' readings showed the same as the second's: the local provider gained the whole file,
4194427 bytes, all from the owner. Every outcome the second run recorded is the one the 11 Nov mount facts record (the
launch, confinement, the on-demand read, the unwritten ranges, the freeze, what each read placed, the teardown); only
the timings differ.

## The federation taken down

`federation.py down` took 37 s (containers 35 s, data 2 s); peak memory over the federation's life was 1.12 GiB for
`zone`, 1.33 GiB for `pine`, 1.13 GiB for `birch` and 1.08 GiB for `cedar`. Afterwards `docker ps -a` (filtered by
`expcore`, by `fedsbx` and by the project's label), `docker network ls` and `docker volume ls` (filtered by `expcore`
and by `fedsbx`) listed nothing; no `fuse.oneclient` mount was left, the federation's root directory was gone, and
nothing listened on its four ports.
