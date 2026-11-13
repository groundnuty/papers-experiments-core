# The Compose federation's facts, 2026-11-12: the writing token's two view tools, live

Step 3 of the bundled live slot, on the 25.0 development build (op-worker f118ed9), for #180's writing tool-server
token on the pinned onedata-mcp server. The 12 Nov run (`2026-11-12-federation-facts-tool-server-write.md`) served
`get_transfer`, `get_qos_requirement` and `get_file_distribution` on that token and left two view tools uncalled.

**Both answer on the writing token.** `list_space_transfers` for the study space, given the space's id, listed the
transfer scheduled a moment before it (state `waiting`, 0.029 s) and, after the transfer completed, listed it under
`ended` (0.027 s). `get_file_qos_summary` served after `add_file_qos_requirement`: at once it held the new requirement
as `pending` (0.104 s), and once the requirement was `fulfilled` it held it as `fulfilled` (0.082 s). **One call
failed:** `list_space_transfers` given the space's name instead of its id was refused, because the tool resolves a name
by `GET /spaces` on Onezone, which answered 403 `forbidden` for the slot's tool-server user (0.245 s).

Every name below is a role, never a configured name; no id, token, address or host is recorded.

## The slot

| Fact | Value |
|---|---|
| Time (UTC) | 17:04 to 17:06, checked again about 17:08 |
| Build | 25.0 development build (op-worker f118ed9), the pinned images |
| Federation | the slot's layout: the study space owned by one provider (its data provider) and mirrored by two; the agent's local provider is one of the mirrors and owns no space |
| Core | main at `36e2351`, nothing installed |
| Tool server | the real onedata-mcp server at the OMCP pin of `docs/inventory.md` (`0cc17ce`), from an existing local checkout with its own environment, verified at the pinned commit with a clean tree |
| Launch | the core's `ToolServerLauncher.launch`, with the slot's writing tool-server user as `principal`; tokens from `TokenIssuer.mint_run_tokens` for the writing slot's two users (data path and tool server), ceiling 900 s |
| Condition | one writing condition; allow-list `schedule_file_replication`, `add_file_qos_requirement`, `get_file_distribution`, `get_transfer`, `list_space_transfers`, `get_file_qos_summary`, `get_qos_requirement`; the manifest frozen from the server with these seven (the server lists its tools without a host or token) |
| Tool-server user's rights | exactly the writing condition's six: `space_view`, `space_read_data`, `space_view_transfers`, `space_schedule_replication`, `space_view_qos`, `space_manage_qos` |
| Mint, server start | 0.16 s, 3.2 s; the server showed the seven tools |
| Teardown check | the launcher's check after the server stopped passed: no event, no error |
| Server's own examination | its log holds no fail-open warning ("Could not examine Oneprovider token via Onezone"), as in the 12 Nov writing run; its only error is the name resolution below |

Assumptions of this slot, none of it committed:

- **The platform file** is a copy of the rendered one in a scratch directory outside the federation's root (the root is
  read-only inside the sandbox), with absolute paths for the secrets, the CA bundle, the manifest and the checkout;
  `[principals]` names the writing slot's two users and `[toolserver]` declares the one condition. It carries no
  `[slots]` and no `[faults]`: this step injects no fault, and `[faults]` would make the launch need the federation's
  admission.
- **The test file** is 64 KiB of random bytes in a new directory of the study space, written through the data provider
  with the admin credential and awaited on the other two providers before the tools ran (6.6 s on the mirror that is
  not local, 0.02 s on the local provider).
- The tool server ran for 26 s, from its start to its stop.

## `list_space_transfers`

The file was replicated to the mirror that is not the agent's local provider, through the tool server.

| Call | Outcome | Seconds |
|---|---|---|
| `schedule_file_replication` | served: a transfer id | 0.467 |
| `list_space_transfers`, `waiting`, space id | served: exactly the new transfer | 0.029 |
| `list_space_transfers`, `ongoing`, space id | served: empty (the transfer was still waiting) | 0.051 |
| `list_space_transfers`, `ended`, space id | served: one ended transfer that was there before this step, not the new one | 0.028 |
| `get_transfer`, polled every 2 s | `scheduled` at the first 5 polls, `replicating` at the next 5, `completed` at the 11th, about 21 s after scheduling | 0.027 to 0.032 each |
| `list_space_transfers`, `ended`, space id, after completion | served: two ids, the new transfer and the older one | 0.027 |
| `list_space_transfers`, `ended`, the space's name | **refused:** `GET /spaces` 403 `forbidden`, "You are not authorized to perform this operation."; no transfer id | 0.245 |

The tool's description says the argument is "Space id OR human-readable name". On this token only the id worked: the
request that failed for the name was `GET /spaces` on Onezone, and the same call by id made no such request (0.027 s).

## `get_file_qos_summary`

| Call | Outcome | Seconds |
|---|---|---|
| `add_file_qos_requirement`, the local provider's `providerId` expression, 1 replica | served: a requirement id | 0.278 |
| `get_file_qos_summary`, at once | served: status `pending`, `requirements` holds the new requirement's id with its expression and `pending` | 0.104 |
| `get_qos_requirement`, polled every 2 s | `pending`, `pending`, `fulfilled` (the 3rd poll, about 5 s after the add) | 0.035, 0.036, 0.072 |
| `get_file_qos_summary`, after `fulfilled` | served: status `fulfilled`, the requirement `fulfilled` | 0.082 |

## Clean-up

Everything below was done with the admin credential through the data provider, after the tool server had stopped.

| Step | Result |
|---|---|
| The QoS requirement removed (`DELETE /qos_requirements/{id}`, through the client's gate) | accepted |
| The file's QoS summary, in the same second | the data provider: no requirement; each of the two mirrors: still 1 (the removal had not reached them yet) |
| The requirement read on each of the three providers, about 1.5 min later | 404 `notFound` on all three |
| The file and its directory deleted (the change log's two entries) | done in 4 s; the file answered absent on the data provider at once and on both mirrors at the next check, 2 s later |
| The study space's root listed on each of the three providers | holds no such directory on any |
| Transfers of the space after the run | 0 `waiting`, 0 `ongoing`, 2 `ended` (this step's and the older one) |

A transfer record cannot be removed, so this step's `completed` replication stays in the space's `ended` list, which
now holds two ids; its file is gone, and so are the replicas the transfer and the requirement made. Nothing else
remains of the step: no requirement, no file, no directory, no running transfer, and no tool-server process.

## What this run does not show

- **The read-only condition:** its transfer and QoS views were refused with 401 `tokenCaveatUnverified` on 12 Nov
  (#181); this run called nothing on a read-only token.
- **`get_file_distribution`** was not called here, and `get_transfer` and `get_qos_requirement` only as polls; their
  answers on this token are the 12 Nov file's.
- **A transfer in `ongoing`:** the replication went from `waiting` to `completed` between two list calls, so no call
  listed a transfer as `ongoing`.
