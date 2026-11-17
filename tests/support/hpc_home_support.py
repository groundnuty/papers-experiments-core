"""The invented home quota command of the home guard's tests (`hpcquota.home_guard`): its name, the invented format of
its output, the figures of an invented home and the fake that prints them. Nothing here is a real site's tool, path,
user or figure."""

TOOL = "fake-home-quota"  # an invented command name
# An invented format, as invented as its tool: the soft column reads 0 and the hard one binds, so the pattern captures
# the hard figure of each (a pattern on the soft column reads a limit of 0, which is refused).
PATTERN = (
    r"files (?P<files_used>[0-9,]+) \(soft [0-9,]+, hard (?P<files_limit>[0-9,]+)\); "
    r"bytes (?P<bytes_used>[0-9,]+) \(soft [0-9,]+, hard (?P<bytes_limit>[0-9,]+)\)"
)
FILES_LIMIT, BYTES_LIMIT = 40_000, 8_000_000
FILES_LINE, BYTES_LINE = 38_000, 7_600_000  # 95% of each limit


def printed(files=0, bytes_=0, files_limit=FILES_LIMIT, bytes_limit=BYTES_LIMIT, soft=0) -> str:
    """What the invented command prints for a home with these figures."""
    return (
        f"files {files:,} (soft {soft}, hard {files_limit:,}); bytes {bytes_:,} (soft {soft}, hard {bytes_limit:,})\n"
    )


class Quota:
    """The invented home quota command: what it prints, its exit status, or what the runner raises for it."""

    def __init__(self, hpc, files=0, bytes_=0, *, text=None, rc=0, stderr="", raises=None, **limits):
        self.completed, self.raises, self.calls = hpc.slurm.Completed, raises, []
        self.text = printed(files, bytes_, **limits) if text is None else text
        self.rc, self.stderr = rc, stderr

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raises is not None:
            raise self.raises
        return self.completed(self.rc, self.text, self.stderr)
