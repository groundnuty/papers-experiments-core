"""Print the JSON body that asks a zone for a provider registration token (no secret is involved).

Usage: tokenbody.py PROVIDER_NAME ADMIN_USER_ID TTL_SECONDS USAGE_LIMIT
A short-lived registerOneprovider invite token: valid TTL_SECONDS, usable USAGE_LIMIT times
("infinity" for no limit). Run as python3.11 -I.
"""
import json
import re
import sys
import time
from typing import List


def main(argv: List[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 64
    name, admin_id, ttl, limit = argv
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", admin_id):
        print("tokenbody: invalid provider name or admin id", file=sys.stderr)
        return 64
    if not ttl.isdigit() or not 60 <= int(ttl) <= 86400:
        print("tokenbody: TTL must be 60..86400 seconds", file=sys.stderr)
        return 64
    if limit != "infinity" and not (limit.isdigit() and int(limit) >= 1):
        print("tokenbody: usage limit must be a positive integer or 'infinity'", file=sys.stderr)
        return 64
    now = int(time.time())
    body = {
        "name": f"{name}-registration-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}",
        "type": {"inviteToken": {"inviteType": "registerOneprovider", "adminUserId": admin_id}},
        "caveats": [{"type": "time", "validUntil": now + int(ttl)}],
        "usageLimit": limit if limit == "infinity" else int(limit),
    }
    print(json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
