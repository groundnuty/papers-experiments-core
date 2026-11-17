---
paths:
  - "deploy/hpc-federation/**"
  - "src/expcore_onedata/**"
  - "src/expcore/campaign/**"
---

# Shared filesystems and batch clusters: what broke live

Each line was found on a real cluster run; the fix is on main.

- **Never order two events by a file's mtime against a process clock.** The kernel stamps mtimes from a coarse clock (about 1 ms behind the wall clock) or a file server's clock, so a file written after an event can look older, and a stale one stamped ahead can look newer. Order by an explicit sequence written inside the files (C13's release identity).
- **An exclusive `flock` needs a write descriptor on NFS** (it becomes a POSIX lock). Open lock files read-write; a read-only open fails there with `EBADF`.
- **Never hold a lock file open inside a directory you are removing** on NFS: the open file becomes a silly-rename file and the removal fails with `ENOTEMPTY`. Keep the lock beside the directory.
- **A login node may lack `pidfd_open`** (older kernels). Fall back to checking the process's start tick, then `os.kill`, then re-checking, and match its command line, so a recycled pid is never signalled.
- **On this Slurm, every step of a heterogeneous job names its `--het-group`**, and a component's steps are listed only under the component's own job id.
- **Steps run under `env -i`** with an explicit allowlist, and `HOME`, XDG and tool caches pointed into the step's own private directory: an account's home is shared by every agent on it.
- **A count from a tool that is missing or fails is an error, never 0** (a missing `ss` once read as "no listeners").
- **`sbatch --test-only` start estimates are unreliable;** only a real submission's wait counts.
