"""pick.py (login node, python3 -I): read prepare-n's JSON on stdin and print only what one step needs.

  pick.py support I   -> {"space_id", "token": tokens[I], "users", "size"}  (provider I's onboarding)
  pick.py access      -> {"space_id", "access_token"}                       (the round trip's REST calls)
  pick.py gate        -> the read-only gate token alone, one line           (the data gate's token file; the input
                                                                            is prepare-n's or gate-token's JSON)

No value is ever on a command line: everything moves through stdin and stdout pipes.
"""

import json
import sys


def main(argv):
    data = json.loads(sys.stdin.readline())
    if argv[:1] == ["support"] and len(argv) == 2:
        out = {
            "space_id": data["space_id"],
            "token": data["tokens"][int(argv[1])],
            "users": data["users"],
            "size": data["size"],
        }
        print(json.dumps(out))
    elif argv == ["access"]:
        print(json.dumps({"space_id": data["space_id"], "access_token": data["access_token"]}))
    elif argv == ["gate"]:
        print(data["gate_token"])
    else:
        sys.exit("usage: pick.py support I | access | gate")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
