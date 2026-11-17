# Provenance: new
"""The read-log channel: a study's canonical scripts log every read they make to a collector the agent cannot reach.

The pieces: `paths` (the fixed names), `collector` (the program the harness runs), `client` (the module the cell's
scripts import), `reader` (intact or unknown), `instrument` (the run's `read_log` snapshot), `facts` and `files`. The
channel's contract is in `docs/interfaces.md`, "The read-log channel". A log is intact only with a start line, an end
line, matching counts, no cap and no malformed line; otherwise it is unknown, and a run with an unknown log reads as
unknown reads, never as fewer.
"""
