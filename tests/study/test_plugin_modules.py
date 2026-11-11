"""A plugin split over several files: modules beside the plugin, imported relatively, covered by the code identity.

The core imports the plugin as a package whose path is the plugin's directory, named by a hash of the plugin's path, so
`from .scoring import Scorer` finds `scoring.py` beside it and two studies' modules of one name never meet. Every file
it imports from there must be one `[study] code` lists, or the plugin is refused when it is loaded.
"""

import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from study_support import HEAD, Campaign, clean_environment, cli, finish

from expcore.config.loading import ConfigError
from expcore.package import build as package_build
from expcore.package.build import build_package, verify_package
from expcore.provenance.codestamp import write_stamp
from expcore.provenance.hashfile import render_hash_file
from expcore.study.config import load_study, read_study_identity
from expcore.study.plugin import PLUGIN_MODULE_PREFIX, StudyContext, import_plugin_module, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
FACTS_LINE = 'FACTS = {"lake": "deep", "river": "long", "hill": "green", "dune": "moving"}\n'
CODE_END = '"plugin.py", "prompt.txt"]'

__all__ = ["clean_environment"]  # a fixture a test below uses, imported from the support module


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def split_copy(base: Path, name: str, *, listed: bool = True, statement: str = "from .facts import FACTS") -> Path:
    """A copy of the example whose answer key lives in `facts.py` beside the plugin, listed in `[study] code` or not."""
    copy = base / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    edit(copy / "plugin.py", FACTS_LINE, statement + "\n")
    (copy / "facts.py").write_text('"""The answer key, beside the plugin."""\n\n' + FACTS_LINE, encoding="utf-8")
    if listed:
        edit(copy / "study.toml", CODE_END, '"plugin.py", "facts.py", "prompt.txt"]')
        edit(copy / "package.toml", '"plugin.py",\n', '"plugin.py",\n    "facts.py",\n')
    return copy


def context(study_dir: Path) -> StudyContext:
    return StudyContext(load_study(study_dir), {"commit": "a" * 40})


def test_p02_a_plugin_imports_a_module_beside_it_that_the_code_identity_covers(tmp_path):
    """P02: adds that a plugin may be split over files: a module beside it, imported relatively, is found, and is one
    of the files `[study] code` lists, so the identity covers the code that runs."""
    study_dir = split_copy(tmp_path, "study")
    plugin = load_plugin(context(study_dir))
    module = sys.modules[type(plugin.executor).__module__]
    assert module.FACTS["hill"] == "green"
    facts = sys.modules[f"{module.__name__}.facts"]
    assert Path(facts.__file__) == study_dir / "facts.py"


def test_p02_a_module_beside_the_plugin_that_the_code_identity_does_not_cover_is_refused(tmp_path):
    """P02: adds that a module the plugin imports from its directory must be in `[study] code`: one that is not would
    run uncovered, so a change to it would leave the identity as it was. Loading the plugin is refused, naming the
    file and the key; so is the replay check's import, which never shows the exception's text."""
    study_dir = split_copy(tmp_path, "study", listed=False)
    message = f"{study_dir / 'study.toml'}: [study]: key 'code' does not list 'facts.py'"
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert str(caught.value).startswith(message)
    with pytest.raises(ConfigError) as caught:
        import_plugin_module(read_study_identity(study_dir), error_text=False)
    assert str(caught.value).startswith(message)


def test_p02_a_module_imported_only_inside_build_is_held_to_the_code_identity_too(tmp_path):
    """P02: adds that the check runs again after `build`, so a module beside the plugin imported there is covered or
    refused like one imported at the top."""
    study_dir = split_copy(tmp_path, "study", listed=False, statement="FACTS = {}")
    late = "    from .facts import FACTS as KEY  # noqa\n\n    study = context.study\n"
    edit(study_dir / "plugin.py", "    study = context.study\n", late)
    with pytest.raises(ConfigError, match=r"\[study\]: key 'code' does not list 'facts.py'"):
        load_plugin(context(study_dir))


def test_p02_an_absolute_import_of_a_module_beside_the_plugin_says_how_to_import_it(tmp_path):
    """P02: adds that `import facts`, which cannot find the module beside the plugin, is refused with the fix: a
    relative import, and the file in `[study] code`."""
    study_dir = split_copy(tmp_path, "study", statement="from facts import FACTS")
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert "ModuleNotFoundError" in str(caught.value)
    assert "'facts' is a module beside the plugin: import it relatively (from .facts import ...)" in str(caught.value)
    with pytest.raises(ConfigError) as caught:
        import_plugin_module(read_study_identity(study_dir), error_text=False)
    assert str(caught.value).endswith("and list its file in [study] code")
    assert "No module named" not in str(caught.value)  # the exception's own text stays out of the check's refusal


BY_PATH = """\
import importlib.util as _found
import sys as _sys
from pathlib import Path as _Path

_spec = _found.spec_from_file_location("facts", _Path(__file__).resolve().parent / "facts.py")
_facts = _found.module_from_spec(_spec)
_REGISTER
_spec.loader.exec_module(_facts)
FACTS = _facts.FACTS
"""


@pytest.mark.parametrize(
    ("listed", "register", "needle"),
    [
        (False, False, "[study]: key 'code' does not list 'facts.py'"),
        (True, False, "[study]: 'facts.py' runs from the study directory outside the plugin's package"),
        (True, True, "[study]: 'facts.py' runs from the study directory outside the plugin's package (as 'facts')"),
    ],
)
def test_p02_a_file_of_the_study_loaded_by_path_is_refused_listed_or_not(tmp_path, listed, register, needle):
    """P02: adds that the check goes by where code comes from, not by its module name: a file of the study directory
    the plugin runs by path (registered as a module or not) is refused when `[study] code` leaves it out, and refused
    when listed too, since only a module of the plugin's package is loaded from its source and kept from others."""
    statement = BY_PATH.replace("_REGISTER", '_sys.modules["facts"] = _facts' if register else "pass")
    study_dir = split_copy(tmp_path, "study", listed=listed, statement=statement)
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert str(caught.value).startswith(f"{study_dir / 'study.toml'}: {needle}")
    assert "facts" not in sys.modules  # a refused load leaves none of its modules behind


def test_p02_a_module_found_through_the_search_path_is_refused_and_forgotten(tmp_path, monkeypatch):
    """P02: adds that `import facts` with the study directory on the module search path is refused even though
    `[study] code` lists the file: the module would be cached under a name any other study could be served, so the
    refusal drops it and says to import it relatively."""
    study_dir = split_copy(tmp_path, "study", statement="from facts import FACTS")
    monkeypatch.syspath_prepend(str(study_dir))
    with pytest.raises(ConfigError, match=r"'facts.py' runs from the study directory outside the plugin's package"):
        load_plugin(context(study_dir))
    assert "facts" not in sys.modules


def test_p02_no_module_of_one_study_is_served_to_the_next(tmp_path):
    """P02: adds that a study refused for a module it registered under a plain name leaves it nowhere, so the next
    study's `import facts` finds no other study's answer key and is refused with the fix."""
    registered = BY_PATH.replace("_REGISTER", '_sys.modules["facts"] = _facts')
    first = split_copy(tmp_path, "first", statement=registered)
    second = split_copy(tmp_path, "second", statement="from facts import FACTS")
    with pytest.raises(ConfigError, match="outside the plugin's package"):
        load_plugin(context(first))
    with pytest.raises(ConfigError, match=r"'facts' is a module beside the plugin: import it relatively"):
        load_plugin(context(second))


def test_p02_a_reload_runs_the_edited_source_even_at_the_same_size_and_modification_time(tmp_path, monkeypatch):
    """P02: adds that the plugin and the modules beside it are compiled from their files' bytes on every load, never
    from cached bytecode, which a same-size edit within the same second would leave looking current: what runs is what
    the code identity hashed, and no bytecode is written into the study."""
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    study_dir = split_copy(tmp_path, "study")
    load_plugin(context(study_dir))
    for name, old, new in (("facts.py", '"green"', '"grass"'), ("plugin.py", '"Be brief."', '"Be terse."')):
        path = study_dir / name
        before = path.stat()
        edit(path, old, new)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_size == before.st_size
    module = sys.modules[type(load_plugin(context(study_dir)).executor).__module__]
    assert (module.FACTS["hill"], module.HINTS["basic"]) == ("grass", "Be terse.")
    assert not list(study_dir.rglob("__pycache__"))


def test_p02_two_studies_with_a_module_of_one_name_each_import_their_own_and_a_reload_reads_it_anew(tmp_path):
    """P02: adds that modules beside a plugin are named under the plugin's own package, so a second study's
    `facts.py` is never served the first one's, and loading a study again reads an edited module afresh."""
    first, second = split_copy(tmp_path, "first"), split_copy(tmp_path, "second")
    facts = second / "facts.py"
    facts.write_text(facts.read_text(encoding="utf-8").replace('"green"', '"grey"'), encoding="utf-8")
    keys = [sys.modules[type(load_plugin(context(s)).executor).__module__].FACTS for s in (first, second)]
    assert [k["hill"] for k in keys] == ["green", "grey"]
    facts.write_text(facts.read_text(encoding="utf-8").replace('"grey"', '"brown"'), encoding="utf-8")
    again = sys.modules[type(load_plugin(context(second)).executor).__module__]
    assert again.FACTS["hill"] == "brown"
    assert again.__name__.startswith(PLUGIN_MODULE_PREFIX)


def test_p02_a_backend_factory_in_a_module_beside_the_plugin_is_named_by_that_module_and_not_by_the_path(tmp_path):
    """P02: adds that a factory in a module beside the plugin goes into the fingerprint as `module.name`, without the
    hashed path, so moving the study keeps its fingerprint and two factories of one name in two files stay apart."""
    study_dir = split_copy(tmp_path, "study")
    factory = '\n\nclass Factory:\n    version = "1"\n\n    def __call__(self, spec, **kw):\n        raise TypeError\n'
    edit(study_dir / "facts.py", FACTS_LINE, FACTS_LINE + factory)
    edit(study_dir / "plugin.py", "from .facts import FACTS", "from .facts import FACTS, Factory")
    keys = "        lease_keys=lease_keys,\n"
    edit(study_dir / "plugin.py", keys, keys + "        backend_factory=Factory(),\n")
    assert load_plugin(context(study_dir)).backend_factory_identity() == {"name": "facts.Factory", "version": "1"}


def test_p02_a_split_plugin_runs_checks_analyses_and_ships_in_a_package_that_passes_its_own_check(
    tmp_path, monkeypatch, clean_environment
):
    """P02: adds the whole path for a split plugin: the run, the replay check, the analysis and the report, and a
    replication package whose own check imports the plugin and the module beside it from the unpacked files."""
    monkeypatch.setattr(package_build, "vendor_wheels", stand_in_vendor)
    study_dir = split_copy(tmp_path, "study")
    write_stamp(study_dir, HEAD, load_study(study_dir).scope)
    campaign = Campaign(study_dir)
    campaign.codes["run"], _ = cli(clean_environment, "run", str(study_dir))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    hashes = tmp_path / "private.sha256"
    hashes.write_text(render_hash_file(["zorvathune"]), encoding="utf-8")  # an invented name, private everywhere
    archive = build_package(study_dir, tmp_path / "dist", hashes=hashes)
    with tarfile.open(archive) as members:
        members.extractall(tmp_path / "unpacked", filter="data")
    unpacked = tmp_path / "unpacked" / "tiny-study-package"
    assert (unpacked / "facts.py").is_file()
    assert verify_package(unpacked) == []
