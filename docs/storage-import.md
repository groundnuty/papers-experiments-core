# Storage import

How a space can be provisioned by Onedata 25.0's storage import instead of a REST upload, what the core checks, what an imported space cannot take, and the live checks that must pass before a study relies on it. Issue #184, regression O41. Everything here was read from the sources of the build the federation runs; nothing was run against a federation yet.

**The answer to the issue's question 5, from the source:** each federation's owner can import one shared, read-only copy of a dataset. A read-only imported storage is never written by Onedata, the import keeps its state in the provider's own database, and everything the core needs from the owner's copy is a read: reads and on-demand reads through the mirrors, replications onto them, their eviction by the reset, the block maps, the transfer views and QoS requirements. What it cannot take is any write through the owner, so the progressive ingest (O27), `reset --clean` and the day-0 round trip cannot run in such a space, and a write or an eviction that takes the owner's blocks cannot be undone. The live checks below decide whether the import is equivalent to an upload in practice.

Sources: op-worker `f118ed90074434045bad5d7bd2611bd206f1a405` (the pinned provider image's build) and onepanel `e0de555b6fed1ce642929f2a0c305e74ee52f879` (the 25.0 tag), read through the GitHub API on 13 Nov 2026. Paths below are under op-worker's `src/` unless they say otherwise.

## What the core does

- **Declaring it.** `platform.toml` marks a space whose data provider holds it on an imported storage: `import = { readonly = true }` in `[space]` or in a `[[spaces]]` entry (`spaces.py`), checked at load and entered in the campaign fingerprint only when declared. The Compose generator's layout declares it too: `import = { readonly = ..., source_env = "EXPCORE_FED_IMPORT_..." }` on a space with `baseline = false` (`deploy/compose-federation`, README "An imported space").
- **Provisioning by import** (`storageimport.ImportedSpace.provision`): the owner's Onepanel shows the support as an imported storage, read-only as declared, in the `auto` mode with continuous scans off; the scan is waited for until it completes with no failed file; the space's listing then holds exactly the dataset's manifest (`importmanifest.py`: paths, kinds and sizes, every directory, nothing more) on each provider named, on consecutive reads; every file is whole on the owner by its block map; and, when asked, every file reads back with the manifest's SHA-256. The record names files by their position in the manifest and providers by their position in `platform.toml`. `SpaceCheck` checks an uploaded space by the same code, so the two copies are compared like with like.
- **Refusals.** In a space held on a read-only imported storage the provisioner refuses, before anything is sent, every creation, write and delete of a file or directory, through any provider, and every replication onto the owner (`Provisioner.read_only`); `ProgressiveIngest` refuses such a space when it is built; the day-0 round trip is unknown there.
- **The federation.** `fedboot.py` makes the imported storage over the panel's REST (`importedStorage`, `readonly`, its own mount point), supports the space from it with `storageImport: {mode: auto, autoStorageImportConfig: {continuousScan: false, detectModifications: false, detectDeletions: false}}`, picks every support's storage by name rather than taking the first id, and waits for the scan. `reset --clean` refuses a layout with an imported space. A read-only source lies outside the root, so `down` never touches it.

## What op-worker does with an imported file

| What | Source | What it means for the core |
|---|---|---|
| The space's root on the storage | `modules/storage/file/storage_file_id.erl:38` (`space_dir_id`): an imported storage's space directory is `/` | The storage's root is the space's root: the manifest's paths are both. One storage per imported space. |
| A file's id | `modules/storage/import/storage_import_engine.erl:680`: `datastore_key:new()`, packed with the space's id | Made as a created file's is, random. The ids of an imported and an uploaded copy differ, as two uploads' do. |
| Its location and block map | `storage_import_engine.erl:681-686` makes the location before the file's metadata; `modules/fslogic/utils/fslogic_location.erl:109-128` (`create_imported_file_doc`) sets the importing provider, the storage file created, the size from `stat` and the blocks of `create_file_blocks/1` (`:190-191`): one block `[0, size)`, none for an empty file | A file the listing shows is whole on the owner's block map, as an uploaded file is once its upload ends. `verify` checks it file by file. |
| A later scan | `modules/storage/import/auto_storage_import_worker.erl:303` and `:323`: after the initial scan, another is scheduled only with `continuousScan`; `storage_import_engine.erl:979`: with `detect_modifications` false a known file is left unmodified | With continuous scans off a scan runs only when forced, and with change detection off it only adds what is new on the storage, so file ids stay. `check_storage` refuses continuous scans. |
| A scan with detection on | `storage_import_engine.erl:1085` (`maybe_update_file_location`); `fslogic_location.erl:132` (`update_imported_file_doc`) claims the whole file on the owner, which invalidates the other providers' blocks | Why continuous scans are refused: a scan during a run could change the space and the mirrors' block maps under the probes. |
| A delete on a read-only storage | `modules/fslogic/deletion/fslogic_delete.erl:335` (`maybe_delete_storage_file`) skips the storage's unlink when the storage is read-only; the metadata's removal then deletes the file's sync record (`:684`, `:542`) and its deletion marker (`:388`) | The file leaves the space and stays on the storage; a forced scan imports it again under a new id (`storage_import_engine.erl:92-160`, `sync_file`). The core sends no such delete. |
| A delete on a writable imported storage | the same function unlinks the storage file | A REST delete destroys the import directory's copy: `reset --clean` refuses an imported space. |
| What a read-only storage refuses | `test_distributed/readonly_storage_test_SUITE.erl`: a creation, a directory, a write, a truncation, a rename (`create_should_fail` at `:120`, `write_should_fail` at `:156`, ...), an on-the-fly replication onto it (`:515`), a replication job onto it (`:576`) and a migration onto it (`:633`) fail with EROFS; a read succeeds (`:144`); an unlink succeeds and leaves the file on the storage (`:244`); a write through another provider takes the read-only provider's blocks and leaves the storage file as it was (`:529`); an eviction of the read-only provider's own replica succeeds, the storage file unchanged (`:605`) | The core refuses every write; reads are untouched. A write or an eviction that takes the owner's blocks cannot be undone, since nothing can be replicated back onto a read-only storage. |
| Opening a missing storage file | `modules/fslogic/operations/file_req.erl:656-676`: on an imported or read-only storage the open answers ENOENT instead of making the file again | A file taken off the storage behind Onedata's back reads as absent. `verify(hashes=True)` finds it. |
| The read-only flag | `modules/storage/storage.erl:677` (`sanitize_readonly_option`): read-only only on an imported storage; onepanel `src/services/helpers/op_worker_storage.erl:344-368` (`add`) skips the read-write test of a read-only storage | A read-only source can be bound read-only: the panel never writes a test file into it. |
| An eviction from a mirror | `modules/replica_deletion/replica_deletion_changes.erl:151` (`can_support_deletion`): the owner supports a deletion by its own location's blocks in its database; `modules/replica_deletion/replica_deletion_master.erl:364` cancels one a QoS requirement needs | The reset's eviction from a mirror reads nothing from the owner's storage: it works on a read-only one. |
| A QoS requirement | no QoS module of the build names a read-only or an imported storage | A requirement the owner fulfils by its whole blocks is fulfilled; one that needs bytes replicated onto a read-only storage cannot be. |

## What a read-only imported space cannot take

Say so plainly, since a study that needs one of these needs a writable space:

- **The progressive ingest (O27).** It removes files and writes them back through the owner. Refused when built. On a writable imported storage it runs, but its removals unlink the import directory's files and its write-backs are REST-made files with new ids, owned by the admin, mode 664: the dataset drifts from the import with every run. Run the ingest in a separate, uploaded space.
- **`reset --clean`.** Refused for any imported space. To start again: `down`, then `up`, which scans again.
- **The day-0 round trip.** Unknown in such a space; it runs in the others.
- **A condition that writes into the space.** A write through the agent's provider, a mirror, lands on the mirror and takes the owner's blocks of that file for good; a delete removes the file from the space until a forced scan brings it back under a new id. The core cannot stop the agent's own writes: keep conditions with write rights away from a read-only imported space, or check it (`ImportedSpace.verify`) before each run.

## One read-only copy for many federations

A read-only imported storage is only read: the panel skips its write test, op-worker never creates, writes or deletes there, and the import's state (file metadata, locations, sync records) lives in each provider's own database, keyed by the space. So providers of separate federations can each import the same directory, read-only, with ids of their own and nothing shared but the bytes. What the source does not settle, and the live checks must:

- the scan's load on a shared filesystem: one `stat` per entry per federation, and each federation's reads of the bytes;
- the owner of each imported file, which is the storage file's uid through LUMA, or the space's virtual owner when it maps to no user (op-worker `modules/storage/luma/luma.erl`); the agent's user reads a file as a member of the space, so the tree must be readable by its group;
- the mode and times, which are the storage file's (`storage_import_engine.erl:888-905`: the mode's permission bits, and the creation time the least of `stat`'s three), where an uploaded file gets 664 and the upload's times.

## The live checks

`tests/live/test_storage_import.py`, gated by `EXPCORE_LIVE_IMPORT=1` and `EXPCORE_LIVE_MUTATE=1`, on `deploy/compose-federation/layouts/storage-import.toml` brought up with `EXPCORE_FED_IMPORT_ALPHA` naming a tree that `make_import_tree.py` wrote. The tree is imported read-only into `alpha-import` and uploaded into `alpha-upload` by `Provisioner.upload`; both are checked against the same manifest. Each test asserts one thing equal for the two copies:

| Property | Test |
|---|---|
| The declared storage on the owner's panel | `test_o41_the_owner_supports_the_imported_space_on_a_read_only_auto_import_that_does_not_rescan` |
| The tree on every provider | `test_o41_both_copies_hold_the_manifests_tree_on_every_provider` |
| The bytes through the owner, and a sample through each mirror | `test_o41_every_file_of_both_copies_reads_back_...`, `test_o41_a_sample_reads_back_through_each_mirror_...` |
| The file ids' form | `test_o41_file_ids_have_one_form_in_both_copies` |
| The ids across a forced re-scan | `test_o41_a_forced_rescan_keeps_every_imported_file_id_and_finds_nothing_new` |
| The owner's block maps | `test_o01_the_owner_holds_every_file_as_one_whole_block_in_both_copies` |
| An on-demand read through a mirror | `test_o41_an_on_demand_read_through_a_mirror_places_the_same_window_in_both_copies` |
| A replication and its transfer record | `test_o41_a_replication_onto_a_mirror_places_the_file_whole_with_the_same_transfer_record` |
| The reset's eviction and verification | `test_o41_the_reset_evicts_and_verifies_both_copies_alike` |
| A QoS requirement's record and summary | `test_o41_a_qos_requirement_is_fulfilled_and_viewed_alike_in_both_copies` |
| Every attribute but the documented ones | `test_o41_the_attributes_differ_only_where_the_import_is_documented_to` |

With `EXPCORE_LIVE_IMPORT_OUT` the tests write what they read, the timings of the import's check and of the upload among it, as JSON for the dated facts file. Still open after them, and for a slot of its own: the timing at full size against the upload, a provider restart during the scan, and two federations importing one directory.
