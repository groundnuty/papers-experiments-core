# What a deleted entry answers when it is read by id, 13 Nov 2026

On the **25.0 development build (op-worker f118ed9)**, `GET /data/{id}` for a deleted entry answers by the attributes it
is asked for. A read with no `attribute` parameter, or with `name` or `type` among them, answers 400 `posix` `enoent`.
A read for `parent_id` or `file_id` alone, or both and nothing else, keeps answering 200 from the entry's last state for
**14 days by default** (`document_expiry`, from the source, not measured), on every provider that holds the tombstone. So
does a read of `size` alone for a deleted **directory**, the case the source trace covered and the live 120 s
observation was of. For a deleted regular file the `size` branch was **not traced** in the source, but it was reported live by another study's Day-0 probe on 13 Nov: a deleted file's `size` alone kept answering for its whole 300 s at all three providers. An entry is gone only when a read that includes a strict attribute answers
`enoent`; never decide that from `parent_id`, `file_id` or `size` alone. The id attribute is named `file_id` on 25.0:
`?attribute=guid` answers 400 `badValueNotAllowed` for every entry, deleted or not, before any lookup.

This file explains observations already recorded and amends two earlier files by naming them; neither is edited:

- `2026-11-10-federation-facts-parent-walk.md`: its row "`GET /data/{deleted file}?attribute=parent_id` answers 200, the
  old parent" is a read of the tombstone, not a property of a deleted file's parent link in general. The same entry read
  with no attribute answers `enoent`, as that file's row for `GET /data/{deleted file}`, three times in a row, says; no
  row there reads `name`, which the source makes strict.
- `2026-11-12-federation-facts-pending-deletes.md` (on main once #192 merges; cited by path): its rows on a deleted
  entry's `parent_id` still reading, and on the old directory's `enoent` from 1 s, are the same effect.

Nothing here was measured against a container. The source was read at op-worker `f118ed90074434045bad5d7bd2611bd206f1a405`,
ctool `8ae7baf62f90129de7b9dbda5af9b44d944b7875` and cluster-worker `0e9cb4037bc51ae79ec3933d0d66d87bcf83b032`
(the commits `2026-11-12-federation-facts-build.md` records), and is checked against the live observations of 12 Nov
below. A claim not read in the source is marked **[inference]**. File references are `file:line` at those commits, in the
repository's own tree.

## What a deleted entry answers, by attribute selection

| The read asks for | Answer for an entry whose delete has landed | Why |
|---|---|---|
| no `attribute` parameter | 400 `posix` `enoent` | the default list holds `name` and `type` (ctool `include/onedata_file.hrl:142-145`) |
| `name`, `type`, or either among others (`mode`, `shares`, `owner_id`, ...) | 400 `posix` `enoent` | read through the strict getter |
| `parent_id`, `file_id`, or both | 200 from the last state | read through the lenient getter, or no getter (`file_id`) |
| `size` of a deleted directory, alone or with `parent_id` and `file_id` | 200 from the last state | read through the lenient getter (`resolve_location_attrs_for_dir`) |
| `size` of a deleted regular file, alone or with `parent_id` and `file_id` | **answers** (reported by another study's Day-0 probe on 13 Nov: a deleted file's `size` alone kept answering for its whole 300 s at all three providers); the source branch was not traced | the branch (`file_attr.erl:458-459`, `file_ctx:get_file_size` through the file location) was not read |
| `guid`, alone or among any others | 400 `badValueNotAllowed`, for a live entry too | not a name the parameter takes: refused before any lookup |

Deprecated names map `parent_id` to `parent_guid` and `file_id` to the id (ctool `src/onedata_file.erl:253-270`, which
has no clause for `guid`). A request with a strict and a lenient attribute fails as a whole: no partial answer is
returned.

## How the read resolves

- **Only the requested attributes are resolved.** The REST route, middleware and `lfm:stat` reach
  `file_attr:resolve` with the attribute list (`src/http/rest/routes/basic_file_operations_rest_routes.erl:103-115`,
  `src/middleware/plugins/file/handlers/file_middleware_get_handler.erl:353-360,611-624`,
  `src/modules/logical_file_manager/lfm_submodules/lfm_attrs.erl:53-62`,
  `src/modules/fslogic/operations/attr_req.erl:297-304`). It runs only the stages whose attributes were asked for
  (`src/modules/fslogic/operations/utils/file_attr.erl:127-139`, stage filter `359-365`). With none asked, the default
  is every deprecated attribute.
- **The `attribute` parameter takes the deprecated names** (`file_middleware_get_handler.erl:95-104`), so the id is
  `file_id`. `sanitize_attr_names` (ctool `src/onedata_file.erl:134-153`) checks the names before the route looks
  anything up, and answers 400 `badValueNotAllowed` for `guid`, which ctool's mapping (`src/onedata_file.erl:253-270`)
  has no clause for. That holds for a live entry, a deleted one and one that never existed alike.
- **The route looks no file up.** `fetch_entity` returns `{undefined, 1}` and validation checks only that the space is
  supported (`file_middleware_get_handler.erl:237-239,310-333`), so nothing fails before the stages run.
- **Two getters of the `file_meta` document.**
  - *Strict*: `file_ctx:get_file_doc/1` (`src/modules/fslogic/context/file_ctx.erl:355-358`) calls `file_meta:get/1`,
    which answers `{error, not_found}` when either `#file_meta.deleted` or `#document.deleted` is true
    (`src/modules/datastore/models/file/metadata/file_meta.erl:219-227`). The badmatch becomes posix ENOENT, which REST
    answers as 400 `enoent` (`src/modules/fslogic/worker/fslogic_errors.erl:88,104-105,115-116`).
  - *Lenient*: `file_ctx:get_file_doc_including_deleted/1` (`file_ctx.erl:425-431`) reads through
    `file_meta:get_including_deleted` (`file_meta.erl:236-264`) and returns the tombstone.
- **Stage by stage** (`file_attr.erl`; attribute groups in ctool `include/onedata_file.hrl:75-114`):
  - `file_id`, the id (internally `guid`), is implicit (`file_attr.erl:138`) and reads no document.
  - `parent_id` goes through `resolve_parent_attrs` (`file_attr.erl:193-199`) to `file_tree:get_parent_internal`
    (`src/modules/fslogic/file_tree/file_tree.erl:118-139,236-240`), lenient.
  - `size` of a directory goes through `resolve_location_attrs` (`file_attr.erl:234-242`) and
    `dir_size_stats:get_stats`; every result is mapped without a throw (`file_attr.erl:505-514`), lenient.
    `size` of a regular file takes another branch (`file_attr.erl:458-459`, `file_ctx:get_file_size` through the file
    location), which was **not traced**; reported live by another study's Day-0 probe on 13 Nov: a deleted file's `size` alone kept answering for its whole 300 s at all three providers.
  - `name` goes through `resolve_name_attrs` (`file_attr.erl:203-213,561-578`) to `get_file_doc`
    (`file_attr.erl:620-628`; `allow_deleted_files` defaults to false), strict.
  - `type`, `mode`, `shares`, `owner_id`, `provider_id` and the rest of the `file_meta` attributes go through
    `resolve_file_meta_attrs` (`file_attr.erl:170-189`) to `get_file_doc`, strict.
- **The permission check never applies the deleted test.** `data_access_control` reads through the lenient getter
  (`src/modules/fslogic/security/data_access_control.erl:246,389,402,424,444`) and walks ancestors with the lenient
  `file_tree:get_parent` (`data_access_control.erl:260-272`).

## How a REST delete leaves a tombstone

1. `DELETE /data/{id}` (`basic_file_operations_rest_routes.erl:93-102`) runs `lfm:rm_recursive` for a directory
   (`src/middleware/plugins/file/handlers/file_middleware_delete_handler.erl:118-127`), which moves the entry into the
   space's trash directory, `?TRASH_DIR_UUID(SpaceId)`, before the HTTP reply (`src/modules/logical_file_manager/lfm_submodules/lfm_files.erl:83-97,773-779`,
   `src/modules/fslogic/file_tree/special_dirs/trash_dir.erl:86-93,107-130`). The name leaves the old parent's listing at once.
2. The deletion itself runs afterwards, as a traverse (`trash_dir.erl:139-151`,
   `src/modules/fslogic/deletion/tree_deletion_traverse.erl:76-109`). A directory is deleted once its subtree is
   (`tree_deletion_traverse.erl:216-256`). Setting `#file_meta.deleted = true` (`src/modules/fslogic/operations/delete_req.erl:146-151`)
   is the point from which strict reads answer `enoent`.
3. The deletion then tombstones the document: cluster-worker `datastore_doc:delete` saves it with `deleted = true` and
   **keeps its value** (`src/modules/datastore/datastore_doc.erl:211-235`, value kept at `221-223`). A fetch with
   `include_deleted` still returns it; only a document whose value is `undefined` counts as not found
   (`datastore_doc.erl:172-178`).
4. **The tombstone expires after `document_expiry`, 1 209 600 s, 14 days by default**
   (cluster-worker `src/cluster_worker.app.src:277-278`; applied through `ensure_expiry_set_on_delete`,
   `src/modules/datastore/datastore_model.erl:217-224,466-484`, for a `sync_enabled` model such as `file_meta`,
   `file_meta.erl:80-87`). op-worker's `rel/files/app.config` does not override it (checked by grep). **[inference:
   this assumes the deployment's `sys.config` does not set `document_expiry`; none was read.]**
5. **Every provider has it.** The tombstone replicates with its value (`src/modules/dbsync/dbsync_changes.erl:83-110`,
   expiry at `375-387`), and the remote handler cleans up local documents only (`src/modules/dbsync/dbsync_file_meta_handler.erl:133-138`,
   `fslogic_delete.erl:125-149,390-395`), never the `file_meta` tombstone. Each provider's 14 days run from when it
   stored the tombstone **[inference]**.
6. **The size is the last logical one.** A directory's size statistics survive the delete while directory statistics are
   active (`src/modules/dir_stats_collector/dir_stats_collector.erl:295-311`, "TODO VFS-9204"), so `size` answers the
   last figure, most likely 0 once the children are gone **[inference]**. With statistics disabled for the space the
   read still answers 200 with no size (`file_attr.erl:508,512-514`) **[inference about how REST renders it]**.
7. If listing fails, the traverse is restarted after a backoff of 16 s, doubling to a day
   (`tree_deletion_traverse.erl:57,121-138,179-189`). Until it finishes, the directory sits in the trash under the name
   `<name>@@<uuid>` (`trash_dir.erl:66-67,122-123`) and every attribute, strict ones included, still answers.

## The 12 Nov live observations, and what they now mean

| Observation (federation, 25.0 development build (op-worker f118ed9)) | Reading |
|---|---|
| A deleted directory read with `name`, `size` and `parent_id` together answered `enoent` from about 1 s after the delete's 204 (`2026-11-12-federation-facts-pending-deletes.md`) | `name` is strict, so the combined read fails as a whole once `deleted = true` is set, here within about a second of the 204 |
| `parent_id` read alone still answered at 0.37 s and at 2.0 s: an old file's link named its old directory, and the old directory's link named a directory that was neither its old parent nor the space root (the same file) | `parent_id` is lenient: it answers from the tombstone, not from a live entry. That other directory is the space's trash directory **[inference]**. Nothing in the source ends the window before expiry |
| Another study's reads of a directory's `size` alone answered 200 on all three providers for 120 s after a delete, with the name already out of every listing and an empty trash. This is that study's Day-0 report; this repository does not record the reads | `size` of a directory is lenient, and the tombstone is on every provider. The 120 s is the length of that study's watch, not the length of the window; the source gives 14 days **[inference]**. It says nothing of a file's `size` |
| In the first second the directory answered its attributes, smaller than before, with a `parent_id` that was the trash directory's | the directory was already renamed into the trash and not yet marked deleted, while its children were going **[inference]** |

## Which read decides that an entry is gone

- **Gone:** `GET /data/{id}` with no `attribute` parameter, or with `name` or `type` among the attributes, answers 400
  `posix` `enoent`. A caller that waits for it bounds the wait by the size of the tree and expects a failed traverse
  to retry after 16 s or more.
- **Not a test of absence:** `parent_id` or `file_id` alone, a directory's `size` alone, however many of them. They
  answer for 14 days by default after the delete (`document_expiry`, not measured). A file's `size` alone answered for
  300 s in another study's live probe (13 Nov); do not test absence with it either.
- **Deletion accepted, not finished:** the name is out of the old parent's listing and `parent_id` is the space's trash
  directory, which is the same for every directory of the space **[inference]**. An empty trash listing shows only that
  the link is gone, not the document.
- **A visibility wait** that checks a file by `parent_id` alone reports a file deleted meanwhile as visible, and by
  `size` alone it did too in another study's live probe (13 Nov). Read `name` with them.
- `is_deleted` exists in the attribute record but is not exposed to the API (ctool `include/onedata_file.hrl:123-128`),
  so a client cannot ask for it.

## What was not read

`data_constraints:inspect`, `file_ctx:new_by_partial_context`, cluster-worker `datastore_reader:get` (the
`include_deleted` branch at `src/modules/datastore/datastore_reader.erl:38`), the regular-file branch of `size`
(`file_attr.erl:458-459`, `file_ctx:get_file_size` through the file location), how `file_attr_translator` renders an
unset `size`, the LUMA, times and path stages, and any deployment `sys.config` override of `document_expiry`.
