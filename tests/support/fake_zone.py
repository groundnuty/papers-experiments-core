"""Onezone's users and space members, as the bootstrap's user and member calls see them (deploy/compose-federation,
`fedboot.Bootstrap`), for the tests of provisioning (O83). A federation may start with users already in it, made by hand
or by an earlier up; every call that would change something is recorded in `writes`, so a test can show that nothing was
sent. The answers are those of the 25.0 interfaces the calls use.
"""

import base64
import re


class FakeZone:
    """Users (login -> id and password), space members (space id -> user id -> privileges), each user's groups and
    clusters, and the privileges a user holds through a group (`effective_extra`, which no direct read shows)."""

    JOINED = frozenset({"space_view"})  # what a user who joined a space holds until its privileges are set

    def __init__(self, spec):
        self.users: dict[str, tuple[str, str]] = {}
        self.members: dict[str, dict[str, set[str]]] = {f"sid-{s.name}": {} for s in spec.spaces}
        self.groups: dict[str, list[str]] = {}
        self.clusters: dict[str, list[str]] = {}
        self.effective_extra: dict[tuple[str, str], set[str]] = {}
        self.invites: dict[str, str] = {}
        self.writes: list[tuple] = []

    # -- what a test sets up before the bootstrap runs ----------------------------------------------------------------

    def add_user(self, login: str, password: str) -> str:
        self.users[login] = (f"uid-{login}", password)
        return f"uid-{login}"

    def join(self, space: str, login: str, privileges) -> None:
        self.members[f"sid-{space}"][self.users[login][0]] = set(privileges)

    def snapshot(self):
        """Everything a call could change, for a before and after."""
        return (
            dict(self.users),
            {sid: {uid: set(p) for uid, p in members.items()} for sid, members in self.members.items()},
            {k: list(v) for k, v in self.groups.items()},
        )

    # -- what the bootstrap calls (`boot.zone`, and `boot.rest.json` for the panel's user creation) -------------------

    def login_of(self, headers):
        user, password = base64.b64decode(headers["Authorization"].split()[1]).decode().split(":", 1)
        return user, password

    def json(self, method, url, *, label, headers=None, json=None, **options):
        assert (method, url.rsplit("/", 1)[-1]) == ("POST", "users"), (method, url)
        self.writes.append(("create user", json["username"]))
        if json["username"] in self.users:
            from fedrest import RestError  # on the path while the federation's modules are (the `fed` fixture)

            raise RestError("the user exists", 409)
        self.users[json["username"]] = (f"uid-{json['username']}", json["password"])

    def zone(self, method, path, label, *, auth=None, json=None, **options):
        if (method, path) == ("GET", "/user"):
            user, password = self.login_of(auth)
            if user not in self.users or self.users[user][1] != password:
                from fedrest import RestError

                raise RestError("no such user", 401)
            return {"userId": self.users[user][0]}
        if method == "GET" and path in ("/user/spaces", "/user/groups", "/user/effective_clusters"):
            uid = self.users[self.login_of(auth)[0]][0]
            if path == "/user/spaces":
                return {"spaces": sorted(sid for sid, members in self.members.items() if uid in members)}
            if path == "/user/groups":
                return {"groups": list(self.groups.get(uid, []))}
            return {"clusters": list(self.clusters.get(uid, []))}
        if (method, path) == ("POST", "/user/tokens/temporary"):
            token = f"invite-{len(self.invites)}"
            self.invites[token] = json["type"]["inviteToken"]["spaceId"]
            self.writes.append(("invite", self.invites[token]))
            return {"token": token}
        if (method, path) == ("POST", "/user/spaces/join"):
            user, _ = self.login_of(auth)
            self.writes.append(("join", user, self.invites[json["token"]]))
            self.members[self.invites[json["token"]]][self.users[user][0]] = set(self.JOINED)
            return {}
        found = re.fullmatch(r"/spaces/([^/]+)/users", path)
        if found and method == "GET":
            return {"users": sorted(self.members[found.group(1)])}
        found = re.fullmatch(r"/spaces/([^/]+)/(users|effective_users)/([^/]+)/privileges", path)
        assert found, (method, path)
        sid, kind, uid = found.groups()
        if method == "PATCH":
            self.writes.append(("privileges", sid, uid))
            held = self.members[sid][uid]
            held |= set(json["grant"])
            held -= set(json["revoke"])
            return {}
        held = set(self.members[sid][uid])
        if kind == "effective_users":
            held |= self.effective_extra.get((sid, uid), set())
        return {"privileges": sorted(held)}
