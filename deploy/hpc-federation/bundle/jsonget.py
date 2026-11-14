"""Print one field of a JSON file: jsonget.py FILE PATH [--default VALUE].

PATH is dot-separated object keys and list indexes, e.g. "ids.0". Without --default, a missing field
exits 2, so a shell caller never mistakes a missing field for an empty one. Run as python3.11 -I.
"""
import json
import sys
from typing import Any, List, Optional


def lookup(doc: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(doc, list):
            doc = doc[int(part)]
        elif isinstance(doc, dict):
            doc = doc[part]
        else:
            raise KeyError(part)
    return doc


def main(argv: List[str]) -> int:
    if len(argv) not in (2, 4) or (len(argv) == 4 and argv[2] != "--default"):
        print("usage: jsonget.py FILE PATH [--default VALUE]", file=sys.stderr)
        return 64
    default: Optional[str] = argv[3] if len(argv) == 4 else None
    try:
        with open(argv[0], encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(f"jsonget: cannot read JSON from {argv[0]}: {e}", file=sys.stderr)
        return 2
    try:
        value = lookup(doc, argv[1])
    except (KeyError, IndexError, ValueError):
        if default is None:
            print(f"jsonget: {argv[1]} not found in {argv[0]}", file=sys.stderr)
            return 2
        value = default
    print(value if isinstance(value, str) else json.dumps(value))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
