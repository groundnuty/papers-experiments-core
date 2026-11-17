"""Find every construction of a client of a federation's services (O73, #415).

A federation whose services are reached by names that resolve nowhere (the HPC federation's declared names) is reached
only through its address map, so a client built beside the one constructor, `hpcboot.service_client`, is a client that
may reach no service by name. `scan_source` reads one module and returns its `Site`s: every place it builds a client of
a service, or could, found by the name of the thing built:

- **The platform layer's own clients and transports**: `Rest` (the bootstrap's, `fedrest`), `OnezoneClient` (the
  platform layer's, built from a `PlatformConfig`, whose `addresses` the rendered `platform.toml` carries) and
  `MappedTransport` (the transport that dials a mapped address), whether called, passed on or kept as a value (a
  default `rest: Callable[..., Any] = Rest` is a site: it is the factory a caller of the constructor hands it).
- **A factory handed in under the name `rest`**: a call of the name `rest` is a site, unless the module defines a
  function of that name (a local helper that is no client).
- **The raw HTTP clients**: `httpx.Client` and `httpx.AsyncClient`, and `http.client.HTTPConnection` and
  `HTTPSConnection`, called or subclassed (a subclass is a site at its class statement, and a call of a class a module
  defines as such a subclass is a site too).

What is no site: an import, an annotation (`rest: Rest`), a name in a docstring or a comment, and an attribute that
is only named like one (`.Client` of anything but `httpx`).

A site is keyed by the file, the function it is in (a nested one as `outer.inner`; `<module>` for the module's own
statements; a default value belongs to the function it is the default of) and the name, so a line moving is no change
and a second site in a function that already has one is a change the count shows.

Known limits, none of which the tree has: a client reached through `getattr`, `importlib` or `__import__`, a class that
subclasses one of the client classes by an alias imported from another module, and a client run as a shell command
(`curl` in `bundle/`), which no Python scan sees.
"""

import ast
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CLIENT_CLASSES = {"Rest", "OnezoneClient", "MappedTransport"}  # the platform layer's own, by the name they are built by
FACTORY_NAME = "rest"  # the parameter that carries a client factory into a caller of the constructor
HTTPX_CLIENTS = {"Client", "AsyncClient"}  # as `httpx.<name>`
HTTP_CONNECTIONS = {"HTTPConnection", "HTTPSConnection"}  # as `http.client.<name>` or imported from it
MODULE = "<module>"


@dataclass(frozen=True)
class Site:
    """One construction of a client: where, in what function, of what, and how (`call`, `value` or `subclass`)."""

    path: str
    function: str
    name: str
    how: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.name)


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for a chain of names and attributes, or None for any other expression."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _imported_connections(tree: ast.Module) -> set[str]:
    """The names a module imports from `http.client` (an alias included), as it writes them."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "http.client":
            found |= {alias.asname or alias.name for alias in node.names if alias.name in HTTP_CONNECTIONS}
    return found


def _client_of(dotted: str | None, connections: set[str]) -> str | None:
    """The client a dotted name stands for, as the scanner names it, or None."""
    if dotted is None:
        return None
    last = dotted.rsplit(".", 1)[-1]
    head = dotted.split(".", 1)[0]
    if last in CLIENT_CLASSES:
        return last
    if dotted in {f"httpx.{name}" for name in HTTPX_CLIENTS}:
        return dotted
    if last in HTTP_CONNECTIONS and (dotted == f"http.client.{last}" or dotted in connections or head == "client"):
        return f"http.client.{last}"
    if dotted in connections:  # an alias: `from http.client import HTTPSConnection as Https`
        return "http.client.connection"
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.sites: list[Site] = []
        self.scope: list[str] = []
        self.connections = _imported_connections(tree)
        self.defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.subclasses: set[str] = set()
        for node in ast.walk(tree):  # a class that subclasses an HTTP connection class, wherever it is defined
            if isinstance(node, ast.ClassDef) and any(
                (_client_of(_dotted(b), self.connections) or "").startswith("http.client.") for b in node.bases
            ):
                self.subclasses.add(node.name)

    @property
    def function(self) -> str:
        return ".".join(self.scope) or MODULE

    def add(self, node: ast.AST, name: str, how: str) -> None:
        self.sites.append(Site(self.path, self.function, name, how, getattr(node, "lineno", 0)))

    def visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)  # defaults and decorators are the function's own, so its entry covers them
        for child in [*node.decorator_list, *node.args.defaults, *[d for d in node.args.kw_defaults if d]]:
            self.visit(child)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    visit_FunctionDef = visit_function
    visit_AsyncFunctionDef = visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            found = _client_of(_dotted(base), self.connections)
            if found is not None:
                self.add(node, found, "subclass")
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:  # the annotation builds nothing, the value may
            self.visit(node.value)

    def visit_arg(self, node: ast.arg) -> None:
        return  # an annotation of a parameter

    def visit_Import(self, node: ast.Import) -> None:
        return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        found = _client_of(dotted, self.connections)
        if found is not None:
            self.add(node, found, "call")
        elif isinstance(node.func, ast.Name) and node.func.id == FACTORY_NAME and FACTORY_NAME not in self.defined:
            self.add(node, FACTORY_NAME, "call")
        elif isinstance(node.func, ast.Name) and node.func.id in self.subclasses:
            self.add(node, node.func.id, "call")
        if found is None and _dotted(node.func) is None:
            self.visit(node.func)  # a call of a call, a subscript...: its parts may build one
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Name(self, node: ast.Name) -> None:
        found = _client_of(node.id, self.connections)
        if isinstance(node.ctx, ast.Load) and found is not None:
            self.add(node, found, "value")  # a class kept, passed on or used as a default: a factory of the client

    def visit_Attribute(self, node: ast.Attribute) -> None:
        found = _client_of(_dotted(node), self.connections)
        if isinstance(node.ctx, ast.Load) and found is not None:
            self.add(node, found, "value")
            return
        self.generic_visit(node)


def scan_source(source: str, path: str = "<source>") -> list[Site]:
    """Every site of one module's text, in order of line. A module that does not parse is a SyntaxError."""
    tree = ast.parse(source, filename=path)
    scan = _Scan(path, tree)
    scan.visit(tree)
    return sorted(scan.sites, key=lambda site: (site.line, site.name))


def scan_tree(base: Path, roots: Iterable[str]) -> list[Site]:
    """Every site of every `.py` file under each root (relative to `base`), by the file's path relative to `base`."""
    sites: list[Site] = []
    for root in roots:
        for path in sorted((base / root).rglob("*.py")):
            sites += scan_source(path.read_text(encoding="utf-8"), path.relative_to(base).as_posix())
    return sites


def audit(
    sites: Sequence[Site],
    constructor: tuple[str, str],
    allowed: Mapping[tuple[str, str, str], tuple[int, str]],
) -> tuple[list[Site], list[str]]:
    """The sites that are neither inside the constructor (`(path, function)`) nor allowed, and what is wrong with the
    allow-list: an entry with no reason, and an entry whose count is not the number of sites it matches (a stale entry
    included, and a second site in a function that already has one)."""
    found = Counter(site.key for site in sites if (site.path, site.function) != constructor)
    unlisted = [site for site in sites if (site.path, site.function) != constructor and site.key not in allowed]
    problems = []
    for key, (count, reason) in sorted(allowed.items()):
        if not reason.strip():
            problems.append(f"{key}: no reason")
        if found[key] != count:
            problems.append(f"{key}: allowed {count} site(s), found {found[key]}")
    return unlisted, problems
