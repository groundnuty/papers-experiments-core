"""A made-up `/proc` of one survivor, for the tests of the Docker diagnosis (O97)."""

from pathlib import Path

START = "4242"


def stat_line(pid: int, state: str, start: str = START, comm: str = "python") -> str:
    return f"{pid} ({comm}) {state} 1 " + " ".join(["0"] * 17) + f" {start} 0\n"


def make_tree(tmp_path: Path) -> Path:
    """A `/proc` of one process, 31, with a main thread (a zombie) and a second thread (in D) that waits in a read of fd
    3, which is a file on a FUSE mount (mount id 60), and a fd 4 that is not (61)."""
    root = tmp_path / "proc"
    base = root / "31"
    (base / "task" / "31").mkdir(parents=True)
    (base / "task" / "32").mkdir(parents=True)
    (base / "fd").mkdir()
    (base / "fdinfo").mkdir()
    (base / "stat").write_text(stat_line(31, "Z"))
    (base / "mountinfo").write_text("60 25 0:70 / /data ro - fuse.oneclient oneclient ro\n")
    (base / "cwd").symlink_to("/work")
    for tid, state in ((31, "Z"), (32, "D")):
        (base / "task" / str(tid) / "stat").write_text(stat_line(tid, state))
        (base / "task" / str(tid) / "wchan").write_text("request_wait_answer")
        (base / "task" / str(tid) / "syscall").write_text("0 0x3 0x7f00 0x400 0x0 0x0 0x0 0x7ffd 0x7f11\n")
    (base / "fd" / "3").symlink_to("/data/space-a/file")
    (base / "fd" / "4").symlink_to("/work/out")
    (base / "fdinfo" / "3").write_text("pos:\t0\nflags:\t0100000\nmnt_id:\t60\n")
    (base / "fdinfo" / "4").write_text("pos:\t5\nflags:\t0100001\nmnt_id:\t61\n")
    (base / "maps").write_text("7f00-7f10 r--p 00000000 00:46 12 /data/space-a/file\n")
    (base / "mem").write_bytes(b"\0" * 16 + b"/data/space-a/other\0" + b"x" * 8)
    return root
