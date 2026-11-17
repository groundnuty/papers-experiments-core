# Provenance: new
"""The Apptainer backend of the platform layer's sandbox: the agent's cells on an HPC batch node (O56).

`ApptainerSandbox` (`sandbox.py`) is the `Sandbox` of a node where an unprivileged user gets no network, user or
mount namespace of its own and no per-container cgroup, and setuid Apptainer is the only isolation: every cell is one
`apptainer exec --containall`, the data gate a process of the driver's on the node's loopback, the egress collector
another. It keeps the Docker sandbox's guarantees where the node allows them and records each departure as an
infrastructure fact (`facts.py`).
"""
