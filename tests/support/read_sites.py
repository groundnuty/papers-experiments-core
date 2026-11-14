"""Find every call of the platform layer that reaches a GET, and say what policy for a timeout it names (O29, #185).

`scan_source` reads one module and returns a `Scan`: the reads (`(module, function, callee)` -> the expression each call
passes as its policy), the provisioner helpers and layer functions that retry by default, and every call it could not
classify. The test that holds the table (`tests/onedata/test_read_retry_sites.py`) fails on any call in the last list,
naming it, so a call the scanner does not understand is never a way round the table.

What counts as reaching a GET, and why nothing that retries can go unseen:

- **A policy is a keyword.** `OnezoneClient.get` and the reads that find a target's space take `retry=`, and the
  mutation gate takes `gate_retry=`; a mapping's `.get(key, default)` takes no keyword at all. So a `.get(` with any
  keyword is a client read, whatever its receiver is called, and a `.get(` with a path in it (a string or an f-string
  that starts with a slash) is one too. The one `.get(x)` left, a single positional argument that is no path, on a
  receiver that is not named for a client, can only be a mapping or a read with no policy, which is one attempt: it can
  neither retry nor hide a retry, so it is not a site.
- **The client's gated and space reads are found by name, on any receiver.** `mutate`, `read_range` and `read_content`
  (`gate_retry=`) and `space_name`, `file_space_id` and `file_space_name` (`retry=`) are the client's wherever they are
  called (`self._onezone.read_range(...)`, `oz.space_name(...)`): a call that names its keyword is a read with that
  policy, and one that names none is `<missing>` on a receiver named for a client and unclassified on any other, since
  `**options` may carry the policy. The provisioner's own `mutate` (no `gate_retry`) is its helper, below.
- **The client's dated read** (`get_dated`, which returns the answer's `Date` header too) takes `retry=` as `get` does,
  and is found on a receiver named for a client: one that names no policy is `<missing>`.
- **The consistency waits and the occupancy read are found by name**, as `consistency.space_root(...)`, as a name
  imported from the module (an alias included) and as a name handed to `functools.partial`. A reference to one that is
  no call and no partial is unclassified.
- **The provisioner's helpers** (`PROVISIONER_HELPERS`) retry unless told not to, so a call of one is a site: on a
  recognised provisioner, with the switch that makes it an instrument's read (`wait=False`, `retry=False`); on any other
  receiver of a module of the layer, unclassified.
- **The layer's functions that retry** (`LAYER_RETRIES`: `place_ranges`, `evict_ids`, `evict_tree`, and a `replicate` or
  `evict` imported by name) retry under their provisioner's policy with no switch, so a call of one, imported by name
  (an alias included), through a module (`eviction.evict_tree(...)`) or bound by `functools.partial`, is a helper in its
  retrying form, which a measuring module may not call. One handed on as a value is unclassified.
- **A method got by `getattr`** under a constant name is the call it stands for (`getattr(oz, "get")(path, retry=r)` is
  `oz.get(path, retry=r)`); under a name the scanner cannot read (`getattr(oz, name)(...)`), or a watched method got
  that way and kept as a value, it is unclassified.
- **The client's own internal calls** are classified in `client.py` itself: a `_send` or a `_request` of a GET names its
  `retry`, one of a method it is given passes it on, a streamed read and the transport are what they are, and every
  call into the gate names the reads it makes under.

Known limits, none of which the layer has (and a reviewer sees where one would be added): a bound `.get` kept as a value
from a receiver that is not named for a client (`g = oz.get`), since a mapping's is the same expression; a method of the
client reached through `setattr`, `operator.methodcaller` or `__getattribute__`; a read made by a module outside the
layer; and a layer function that retries and is not named in `LAYER_RETRIES`, which a new one must be added to.
"""

import ast
from dataclasses import dataclass, field

CLIENT_NAMES = {"client", "zone", "http"}  # receivers (leading underscores dropped) that are a client or its transport
POLICY_KEYWORDS = {"retry", "provider", "token", "timeout_s", "deadline_s", "params"}  # a mapping's `get` has none
WAITS = {"space_root", "wait_visible", "wait_transfers"}  # the consistency module's reads, named `consistency.<name>`
OCCUPANCY = {"physical_sizes"}  # `occupancy.physical_sizes`, which reads a distribution
GATED = {"mutate", "read_range", "read_content"}  # client calls whose gate reads under `gate_retry=`
SPACE_READS = {"space_name", "file_space_id", "file_space_name"}  # client reads that find a space, under `retry=`
NO_POLICY = {"content_answer"}  # client calls with no policy at all: one attempt by construction
DATED = {"get_dated"}  # the client's read that returns its answer's Date header too, under `retry=` as `get` is
# The client's internal calls into the gate; each passes how the gate reads as its `reads`, last positional or named.
GATE_INTERNALS = {
    "_target",
    "_record_space",
    "_space_roots",
    "_space_root",
    "_parent_of",
    "_walk",
    "_file_space_id",
    "_space_name",
}
# The provisioner's methods and how each says that it is an instrument's read: the keyword that switches the retry off,
# or None when the method always retries. `blocks` and `holds` are the other way round: one attempt unless `retry=`.
PROVISIONER_HELPERS: dict[str, str | None] = {
    "children": "retry",
    "resolve": "wait",
    "root": "wait",
    "resolve_many": None,
    "walk": None,
    "make_dir": None,
    "put_file": None,
    "upload": None,
    "read_file": None,
    "await_visible": None,
    "fetch": None,
    "read_through": None,
    "schedule_transfer": None,
    "replicate": None,
    "replicate_file": None,
    "evict": None,
    "teardown": None,
    "verify_space": None,
    "mutate": None,
}
# The layer's own functions that retry under their provisioner's policy, with no switch to turn it off: called by name
# (imported from the layer, an alias included) or through a module. `replicate` and `evict` are the provisioner's
# methods too, found on a provisioner as helpers, so only a bare name imported from the layer is added here.
LAYER_RETRIES = {"place_ranges", "evict_ids", "evict_tree"}
IMPORTED_RETRIES = LAYER_RETRIES | {"replicate", "evict"}
POLICY_HELPERS = {"blocks", "holds"}  # one attempt by default, a `retry=` policy when the caller is plumbing
PROVISIONER_RECEIVERS = {"provisioner", "p"}  # as written, leading underscores dropped, last segment: `self._p`
# What a call's shown policy means for a timeout: these are one attempt, everything else retries or is unnamed.
ONE_ATTEMPT_SHOWN = {"ONE_ATTEMPT", "_ONE_ATTEMPT", "<one attempt>", "<streamed>", "<transport>", "<default>"}
# Every attribute name a call is classified by: a method of these got by `getattr` and kept as a value is unclassified.
WATCHED_ATTRS = (
    {"get", "stream", "request", "_send", "_request"}
    | WAITS
    | OCCUPANCY
    | GATED
    | SPACE_READS
    | NO_POLICY
    | DATED
    | GATE_INTERNALS
    | set(PROVISIONER_HELPERS)
    | POLICY_HELPERS
    | IMPORTED_RETRIES
)
MISSING = "<missing>"  # a read that names no policy
NONE = "<none>"  # a client call that passes the method on and never a policy: a write, by construction


@dataclass
class Scan:
    """What one module's calls came to: its reads and provisioner helpers by site, and what it could not classify."""

    reads: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    helpers: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    unclassified: list[str] = field(default_factory=list)


def _segment(expression: ast.expr) -> str:
    """The last name of a receiver, leading underscores dropped: `self._p` -> `p`, `ctx.client` -> `client`."""
    text = ast.unparse(expression)
    return text.split(".")[-1].lstrip("_")


def _keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.unparse(keyword.value)
    return None


def _reads_of(call: ast.Call) -> str:
    """The `reads` a call into the gate passes: the keyword of that name, else the last positional argument."""
    named = _keyword(call, "reads")
    if named is not None:
        return named
    return ast.unparse(call.args[-1]) if call.args else MISSING


def _is_path(argument: ast.expr) -> bool:
    """Whether a positional argument is a path: a string, or an f-string, that starts with a slash."""
    if isinstance(argument, ast.Constant):
        return isinstance(argument.value, str) and argument.value.startswith("/")
    if isinstance(argument, ast.JoinedStr) and argument.values:
        first = argument.values[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("/")
    return False


def _unwrapped(call: ast.Call) -> ast.Call:
    """A `functools.partial(f, ...)` as the call of `f` it stands for, so a bound read is a site like any other."""
    func = call.func
    named = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    if named == "partial" and call.args:
        return ast.copy_location(ast.Call(func=call.args[0], args=call.args[1:], keywords=call.keywords), call)
    return call


class _Scanner:
    def __init__(self, module: str, others: set[tuple[str, str, str]]) -> None:
        self.module, self.others, self.scan = module, others, Scan()
        self.aliases: dict[str, str] = {}  # a name imported from the layer -> the name it had there
        self.foreign: set[str] = set()  # names imported from outside the layer (`os`, `Path`): not a provisioner
        self.followed: set[int] = set()  # ids of the nodes a call or a partial follows, which are no bare references

    def run(self, tree: ast.Module) -> Scan:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom | ast.Import):
                self._imports(node)
            if isinstance(node, ast.Call):
                call = _unwrapped(node)
                self.followed.add(id(call.func))
        self._visit(tree, [], [])
        return self.scan

    def _imports(self, node: ast.ImportFrom | ast.Import) -> None:
        layer = (node.module or "").startswith("expcore_onedata") if isinstance(node, ast.ImportFrom) else False
        for item in node.names:
            bound = item.asname or item.name.split(".")[0]
            if layer:
                self.aliases[bound] = item.name
            else:
                self.foreign.add(bound)

    def _visit(self, node: ast.AST, stack: list[str], classes: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self._visit(child, [*stack, child.name], classes)
                continue
            if isinstance(child, ast.ClassDef):
                self._visit(child, [*stack, child.name], [*classes, child.name])
                continue
            where = ".".join(stack)
            if isinstance(child, ast.Call):
                self._call(_unwrapped(child), where, classes)
                if id(child) not in self.followed:
                    self._getattr_value(child, where)
            elif isinstance(child, ast.Attribute | ast.Name) and id(child) not in self.followed:
                self._reference(child, where)
            self._visit(child, stack, classes)

    def _read(self, where: str, callee: str, shown: str) -> None:
        self.scan.reads.setdefault((self.module, where, callee), []).append(shown)

    def _helper(self, where: str, callee: str, shown: str) -> None:
        self.scan.helpers.setdefault((self.module, where, callee), []).append(shown)

    def _unknown(self, node: ast.AST, where: str, why: str) -> None:
        self.scan.unclassified.append(f"{self.module}:{node.lineno} {where or '<module>'}: {why}")

    def _reference(self, node: ast.Attribute | ast.Name, where: str) -> None:
        """A watched name used as a value, which no call follows: the scanner cannot see what is passed to it."""
        name = node.attr if isinstance(node, ast.Attribute) else self.aliases.get(node.id, node.id)
        watched = WAITS | OCCUPANCY | GATED | SPACE_READS | NO_POLICY | DATED | LAYER_RETRIES
        imported_retry = isinstance(node, ast.Name) and name in IMPORTED_RETRIES and node.id in self.aliases
        client_get = isinstance(node, ast.Attribute) and name == "get" and _segment(node.value) in CLIENT_NAMES
        watched_name = name in watched and (isinstance(node, ast.Attribute) or node.id in self.aliases)
        if client_get or watched_name or imported_retry:
            self._unknown(node, where, f"`{ast.unparse(node)}` is used as a value, so its calls cannot be followed")

    def _getattr_call(self, call: ast.Call, where: str) -> ast.Call | None:
        """`getattr(x, "name")(...)` as the `x.name(...)` it stands for; None, and unclassified, when the name is not a
        constant string, since it may be any method of the client."""
        inner = call.func
        assert isinstance(inner, ast.Call)
        name = inner.args[1] if len(inner.args) >= 2 else None
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            self._unknown(call, where, "a method got by `getattr` under a name the scanner cannot read is called")
            return None
        attribute = ast.copy_location(ast.Attribute(value=inner.args[0], attr=name.value, ctx=ast.Load()), call)
        return ast.copy_location(ast.Call(func=attribute, args=call.args, keywords=call.keywords), call)

    def _getattr_value(self, call: ast.Call, where: str) -> None:
        """A watched method got by `getattr` and not called at once: what is done with it cannot be followed."""
        func = call.func
        name = call.args[1] if len(call.args) >= 2 else None
        watched = isinstance(name, ast.Constant) and name.value in WATCHED_ATTRS
        if isinstance(func, ast.Name) and func.id == "getattr" and watched:
            self._unknown(call, where, f"`{ast.unparse(call)}` is kept as a value, so its calls cannot be followed")

    def _call(self, call: ast.Call, where: str, classes: list[str]) -> None:
        func = call.func
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
            got = self._getattr_call(call, where)
            if got is None:
                return
            call, func = got, got.func
        if isinstance(func, ast.Name):
            name = self.aliases.get(func.id, func.id)
            if name in WAITS and func.id in self.aliases:
                self._read(where, f"consistency.{name}", _keyword(call, "retry") or MISSING)
            elif name in OCCUPANCY and func.id in self.aliases:
                self._read(where, name, _keyword(call, "retry") or MISSING)
            elif name in IMPORTED_RETRIES and func.id in self.aliases:
                self._helper(where, f"layer.{name}", "<default>")
            return
        if not isinstance(func, ast.Attribute):
            return
        attr, receiver = func.attr, ast.unparse(func.value)
        if attr == "get":
            self._get(call, where, func)
        elif attr in LAYER_RETRIES:
            self._helper(where, f"layer.{attr}", "<default>")
        elif attr in WAITS:
            self._read(where, f"consistency.{attr}", _keyword(call, "retry") or MISSING)
        elif attr in OCCUPANCY:
            self._read(where, attr, _keyword(call, "retry") or MISSING)
        elif attr in ("_send", "_request") and receiver == "self":
            self._internal_request(call, where, attr)
        elif attr in ("stream", "request", "send") and _segment(func.value) in CLIENT_NAMES | {"one_shot"}:
            self._read(where, f"http.{attr}", "<streamed>" if attr == "stream" else "<transport>")
        elif attr in GATE_INTERNALS and receiver == "self":
            self._read(where, f"self.{attr}", _reads_of(call))
        elif attr in GATED | SPACE_READS or (attr in NO_POLICY | DATED and _segment(func.value) in CLIENT_NAMES):
            self._client_call(call, where, func, classes)
        elif attr in set(PROVISIONER_HELPERS) | POLICY_HELPERS:
            self._provisioner(call, where, func, classes)

    def _get(self, call: ast.Call, where: str, func: ast.Attribute) -> None:
        """A `.get(`: a client read when it takes a keyword (`retry=` and the like, which a mapping's never does), a
        path, or a receiver named for a client; otherwise a mapping's, which no policy can be hidden in."""
        keywords = {k.arg for k in call.keywords}
        path = len(call.args) == 1 and _is_path(call.args[0])
        if keywords & POLICY_KEYWORDS or None in keywords or path or _segment(func.value) in CLIENT_NAMES:
            self._read(where, "client.get", _keyword(call, "retry") or MISSING)
        elif keywords:
            self._unknown(call, where, f"`{ast.unparse(call)[:80]}` is a get with a keyword the scanner does not know")

    def _internal_request(self, call: ast.Call, where: str, attr: str) -> None:
        """The client's `_send` and `_request`: a GET names its `retry`; another constant method is a write, no read;
        a method passed on is a pass-through that says what it passes."""
        method = call.args[0] if call.args else None
        if isinstance(method, ast.Constant):
            if method.value == "GET":
                self._read(where, f"self.{attr}(GET)", _keyword(call, "retry") or MISSING)
            return
        self._read(where, f"self.{attr}(<method>)", _keyword(call, "retry") or NONE)

    def _client_call(self, call: ast.Call, where: str, func: ast.Attribute, classes: list[str]) -> None:
        """A call of the client's gated or space reads, which is the client's on any receiver, or of its dated read on
        a receiver named for a client: it names its policy in `gate_retry=` or `retry=`. Without one it is `<missing>`
        on a receiver named for a client; elsewhere it may be the provisioner's `mutate`, which takes none, and is
        otherwise unclassified."""
        attr = func.attr
        if attr in NO_POLICY:
            self._read(where, f"client.{attr}", "<one attempt>")
            return
        keyword = "gate_retry" if attr in GATED else "retry"
        shown = _keyword(call, keyword)
        if shown is not None or _segment(func.value) in CLIENT_NAMES:
            self._read(where, f"client.{attr}", shown or MISSING)
        elif attr in PROVISIONER_HELPERS:
            self._provisioner(call, where, func, classes)
        else:
            self._unknown(call, where, f"`{ast.unparse(func)}` is a read of the client that names no `{keyword}`")

    def _provisioner(self, call: ast.Call, where: str, func: ast.Attribute, classes: list[str]) -> None:
        """A provisioner helper, recognised by its receiver. A call that is no provisioner's (`Path(x).resolve()`, a
        method of the standard library) is no site; one on another object of the layer with a helper's name is
        unclassified, unless the test says what it is."""
        attr, value, receiver = func.attr, func.value, ast.unparse(func.value)
        if attr == "resolve" and not call.args:
            return  # `Path.resolve()`: the provisioner's takes the path
        if isinstance(value, ast.Call | ast.Constant | ast.Subscript | ast.JoinedStr):
            return
        root = receiver.split(".")[0]
        if root in self.foreign or root in {"os", "shutil", "sys", "re"}:
            return
        known = _segment(value) in PROVISIONER_RECEIVERS or (receiver == "self" and "Provisioner" in classes)
        if not known:
            if (self.module, where, receiver) not in self.others:
                self._unknown(call, where, f"`{ast.unparse(func)}` has a provisioner helper's name, a receiver unknown")
            return
        if attr in POLICY_HELPERS:
            self._read(where, f"provisioner.{attr}", _keyword(call, "retry") or "<default>")
            return
        self._helper(where, f"provisioner.{attr}", _keyword(call, PROVISIONER_HELPERS[attr] or "") or "<default>")


def scan_source(text: str, module: str, others: set[tuple[str, str, str]] | None = None) -> Scan:
    """Scan one module's source. `module` is its path in the layer, as the table spells it (`probes.py`); `others`
    names the `(module, function, receiver)` of calls that have a helper's name and are not the provisioner's."""
    return _Scanner(module, others or set()).run(ast.parse(text))


def retries(shown: str) -> bool:
    """Whether a read's shown policy is one that tries a timeout again, or at least is no one attempt that is named."""
    return shown not in ONE_ATTEMPT_SHOWN


def retrying_helper(shown: str) -> bool:
    """Whether a helper call retries: unless it says it is an instrument's read, with its switch a literal False."""
    return shown != "False"
