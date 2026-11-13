# The Compose federation's facts, 2026-11-12: pending deletes, live

Step 6 of the bundled live slot, on the 25.0 development build (op-worker f118ed9). It checks the public fake's model of a
delete still under way (`FakeData(delete_delay_s=...)`, #183; `src/expcore_onedata/testing/fake_data.py` and the
"Pending deletes" paragraph of `docs/interfaces.md`), which a study's ingest wait relies on.

**No re-created file was lost.** Four times, a directory of five files was deleted through the space's owner and
made again under the same name, with five new files, within 0.1 s of the delete's answer. The second-by-second watch
ran for 120 s through the owner and both mirrors. Every new file stayed at its size, with its parent and its content
(by SHA-256), on every provider, and so did a sixth file one level deeper. In the fourth trial the new files were
also hashed on the owner's storage itself, 110 s after the re-creation, and all five held the new content. The same
held for the control, re-created 30 s after the delete, and for a single file deleted and made again at once. All
three providers' storage is `canonical`, the condition under which the recorded loss was said to occur. **The old
entry went in under a second on the owner, and in 3 s or 8 s on the mirrors**, both mirrors in the same one-second
tick.

Every name below is a role, never a configured name. No id, token, address or host is recorded: an id that is not one
of the check's own appears as the first 8 hex digits of its SHA-256.

## The slot

| Fact | Value |
|---|---|
| Time (UTC) | 20:11:44 to 20:23:36; the fourth immediate trial 20:30:10 to 20:32:35 |
| Build | 25.0 development build (op-worker f118ed9), the pinned images |
| Federation | the slot's layout: the study space owned by one provider (the owner) and mirrored by two (the local mirror, which owns no space, and a third); the fourth provider does not support the study space and was not read |
| Core | main at `36e2351`, nothing installed |
| Client | the core's `OnezoneClient` and `Provisioner` (data provider: the owner) on the federation's rendered platform file, admin credential |
| Storage | `posix`, `storagePathType` `canonical`, not imported, on all three providers (read from each provider's panel) |
| Load | the pilot's federation idle; no other writer in the study space |

## How it was measured

- **Old entry.** A scratch directory under the space root held each trial's entry. The provisioner made the
  trial's directory and wrote five files `f0.bin` to `f4.bin` of 64 KiB random content (`put_file`), then waited
  until both mirrors showed every file at its size (`await_visible`: the local mirror 3.6 s to 5.1 s, the third
  0.01 s to 0.03 s), and 2 s more.
- **Delete and re-create.** `DELETE` of the directory through the owner (`Provisioner.mutate`, answered 204 in 0.046 s
  to 0.055 s). At once, with no read between, the same name was created through the owner (`POST .../children`),
  then five files of the same names with new random content (`POST` and a `PUT` of content each).
- **Watch.** From the delete's answer, a tick every second until 120 s after the re-creation (121 ticks; 151 for the
  control), each tick's reads in parallel, each with a 5 s timeout and one attempt. Through each of the three
  providers, a tick read:
  - the old directory's and old files' attributes;
  - the new directory's and new files' attributes (`name`, `size`, `parent_id`) and its listing;
  - the new files' content, by a whole-file range read, hashed;
  - the scratch parent's size and its listing, raw, so two entries of one name would both show.

  The new files' block map was read through the owner. The longest tick took 0.84 s; no read took over 5 s.
- **Trials.**
  - Four immediate trials. In the second, content was read through the owner only, so no mirror fetched a copy that
    could mask a loss on the owner's storage. In the third, the new directory also held a subdirectory with a sixth
    file, and a replication of an old file was scheduled through the owner 0.54 s after the delete.
  - The fourth ran under a scratch parent of its own name. It read `parent_id` alone (`?attribute=parent_id`, the
    core's parent-walk read) of an old file and of the old directory through the owner, at 0.37 s and 2.0 s. At
    110 s after the re-creation it hashed the trial's directory on the owner's storage, read from the host. It
    listed that directory again after the clean-up.
  - One control, re-created 30 s after the delete.
  - One single file at the scratch parent's top, deleted and made again at once.
- **Clean-up.** Each trial's new entry was deleted through the owner and the scratch parent at the end. Every old and
  new id answered absent, and the name was gone from its parent's listing, on all three providers, 7.4 s to 7.8 s
  after each delete (the parents' 4.3 s and 5.4 s).

## What was seen

Times are seconds after the delete's 204, at one-second resolution. The first tick began at the 204 and its reads ended within 0.14 s.

| Trial | Re-created (dir, last file written) | Old entry absent: owner | Old entry absent: both mirrors | New entries visible on both mirrors | New files lost or changed |
|---|---|---|---|---|---|
| immediate 1 | 0.09, 0.42 | files at the first tick, dir at 1 | 3 | 3 | none, of 5 |
| immediate 2 (content via owner only) | 0.13, 0.42 | files at the first tick, dir at 1 | 3 | 3 (one file listed), 8 (all five) | none, of 5 |
| immediate 3 (with a subdirectory) | 0.11, 0.54 | files at the first tick, dir at 1 | 3 | 8 | none, of 6 |
| immediate 4 (storage hashed) | 0.08, 0.37 | files at the first tick, dir at 1 | 3 | 3 (one file at size 0), 8 (all at size) | none, of 5; on storage at 110 s, 5 of 5 with the new content |
| control (30 s pause) | 30.10, 30.37 | files at the first tick, dir at 1 | 8 | 38 (7.9 s after the re-creation) | none, of 5 |
| single file | 0.13, 0.17 | the first tick | 8 | 8 | none, of 1 |

- **The owner.**
  - It accepted every re-creation at once: no refusal, no retry.
  - At the first tick, the old files answered 400 `posix` `enoent`. The old directory still answered its attributes
    in all five directory trials, smaller than its 327,680 bytes (131,072, 131,072, 65,536, 262,144 and 131,072)
    and with a `parent_id` that was neither its old parent nor the space root (one same id, `f6c3fe75`, in all
    five). It answered `enoent` from 1 s.
  - Read alone (`?attribute=parent_id`), the parent link still read at 0.37 s and 2.0 s, while the attributes read
    together answered `enoent`: an old file's link named the old directory, and the old directory's link named that
    same other directory.
  - Its listing of the scratch parent left the old directory out from the first tick. For the single file and in
    immediate 4, the first tick's listing showed only the new entry, at size 0 before its content was written.
- **The mirrors.**
  - Until the old entry went, both mirrors showed it at its full size under its old parent, in attributes and in the
    listing. Its files went in the same tick as the directory.
  - No tick showed the old and the new entry beside each other, and no listing held a suffixed or repeated name. In
    immediate 1 and 2 and for the single file, the listing switched from the old entry to the new one in one tick. In
    immediate 3 and the control, the name was absent from the listing between the two.
  - In immediate 2, the mirrors listed one new file at 3 s and all five at 8 s. In immediate 4, both mirrors showed
    one new file at size 0 at 3 s (a range read answered 416) and at its size, with the right content, from 8 s.
- **Content and placement.**
  - Every new file read its own content (SHA-256) through the owner at every tick from its creation, and through each
    mirror at every tick once the mirror showed it.
  - The owner alone held each new file's 64 KiB until a mirror's read fetched it. In immediate 2, nothing was read
    through a mirror, and the owner alone held the bytes throughout, read correctly through it at all 121 ticks.
  - On the owner's storage, at 110 s in immediate 4, the trial's directory held exactly the five file names, each
    65,536 bytes with the new content's SHA-256, and no old content.
- **Depth.** The subdirectory and its file kept their size, parent and content for 120 s on all three providers.
- **The transfer of a deleted file.** The replication of an old file, which the owner already answered as `enoent`,
  was accepted through the owner: a transfer id, no error. It completed at once with 0 files processed, 0 replicated
  and 0 failed. None was left waiting or ongoing.
- **Directory sizes.**
  - The scratch parent's size attribute followed changes late on every provider. The owner counted a re-created
    directory 5 s to 6 s after its creation, the mirrors 8 s to 13 s after it.
  - In the control, with no re-creation in the way, the mirrors' size for the parent fell to 0 at 8 s after the
    delete.
- **Storage after the clean-up.**
  - Every entry was gone from all three providers' metadata.
  - The owner's storage still held an empty directory at the path of each of the four immediate trials (no file in
    them), and the two scratch parents' directories that hold them.
  - It held none for the control or the single file. The mirrors' storage held none. Nothing was removed from
    storage behind the platform.

## Against the fake's model

**Recorded on 25.0 (`DELETE_FACTS`):**

| Claim | Here |
|---|---|
| The provider the DELETE went to answers the entry as deleted at once, 400 `posix` `enoent` | holds for files, and for the files under a deleted directory. A deleted directory still answered its attributes within 0.14 s of the 204, moved under another parent and partly emptied, and `enoent` by 1 s |
| A deleted file's `parent_id` still reads | holds for `?attribute=parent_id` alone, at 0.37 s and 2.0 s. The same entry read with `name`, `size` and `parent_id` together answered `enoent` |
| A provider finishes deleting a directory after the call returns | holds, briefly: under 1 s on the owner (above); 3 s or 8 s before the mirrors stopped showing it |
| A directory made again under the same name before then loses its files when the delete lands; the create is accepted | **the create is accepted (holds); the loss fails**: 0 of 21 files lost or changed in four immediate trials, 120 s each, on canonical storage, on every provider, by hash, and on the owner's storage itself in the fourth |
| Other providers' directory sizes follow a delete about 5 s late | consistent: 8 s in the control at one-second resolution; directory sizes lag 5 s to 13 s for additions too, the owner included |

**Assumed, for a live check to confirm:**

| Assumption | Here |
|---|---|
| (a) Providers the DELETE did not go to keep showing the entry, in reads and listings, until it lands | **holds**: both mirrors showed the old entry, its files and its listing entry until 3 s or 8 s. The 409 for its name and a second DELETE through them were **not tested** (no mutation was sent through a mirror) |
| (b) Every provider lands it at once, after the one delay | **at once holds** at one-second resolution: both mirrors stopped showing the entry in the same tick in every trial. **The delay is not one value**: 3 s in four trials, 8 s in two. Whether a loss would land everywhere at once is unobservable here, since none landed |
| (c) A file's delete is late in the same way, and a file's name made again loses nothing | **holds**: the owner answered the file absent at once, the mirrors showed it until 8 s, and the file made again lost nothing in 120 s |
| (d) The late removal takes files at any depth of the directory made again, and no directory | **not exercised**: no removal took anything. The file one level deeper and its directory stayed |
| (e) A listing through the DELETE's provider already leaves out a directory still going | **holds**: from the first tick in all five directory trials, while the directory itself still answered its attributes |
| (f) A provider that still shows the old entry shows one made again beside it, once visible | **not seen**: no tick held both, and no suffixed name appeared. The mirrors switched from the old entry to the new one, or showed neither for some seconds between |
| (g) What is under a directory deleted through a provider answers as deleted there at once | **holds**: the five old files answered `enoent` on the owner at the first tick |
| (h) A transfer scheduled through that provider for an entry it answers as deleted is refused, 400 | **fails**: accepted, and it completed at once having processed 0 files |

**For a study's wait.** In this check, an immediate re-creation lost nothing on 25.0 (op-worker f118ed9) with
canonical storage. That covers four directory trials and one file, at 64 KiB, with a 120 s watch at one-second
resolution. The 30 s control behaved the same. Two cases were not covered: a larger file, and a delete that is still
working through many files when the name is re-created. The owner's own reads answered absent before the mirrors did
(by 3 s or 8 s), so a wait on absence that reads every provider of the space covers the mirrors' lag. A wait on the
owner alone does not.
