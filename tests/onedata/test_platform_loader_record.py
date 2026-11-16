"""O35: the Onedata layer's own loaders record that they ran, so a study cannot build the platform unseen.

`load_platform` and `load_faults` are how a study's plugin gets a `PlatformConfig` and its `[faults]` settings. Each
records the module a study lists in `[study] platforms` for this platform (`expcore_onedata.sandbox.kinds`) and the file
it read (`expcore.config.platform_loads`), when a study build is open; the scheduler then refuses a build whose record
names a module the study does not list. A record changes nothing a loader returns, and no fingerprint.
"""

import pytest
from fault_support import write_faults
from hpc_support import write_hpc_platform

from expcore.config.loading import ConfigError
from expcore.config.platform_loads import PlatformLoad, watching_platform_loads
from expcore_onedata import fault_deferral
from expcore_onedata.config import PLATFORM_MODULE, load_platform
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.sandbox import kinds
from expcore_onedata.testing import write_platform

MODULE = "expcore_onedata.sandbox.kinds"


def test_o35_the_recorded_module_is_the_one_a_study_lists_for_the_platform():
    """O35: the module the loaders record is the one `[study] platforms` lists and whose hook the core calls."""
    assert PLATFORM_MODULE == kinds.__name__ == MODULE == fault_deferral.PLATFORM_MODULE


def test_o35_load_platform_records_the_module_and_the_file_it_read(tmp_path):
    """O35: a study build that loads a platform.toml leaves one record of the module and the resolved file, and the
    loader returns what it returned without a watch."""
    path = write_platform(tmp_path)
    plain = load_platform(path, require_secrets=False)
    with watching_platform_loads() as loads:
        watched = load_platform(path, require_secrets=False)
    assert loads == [PlatformLoad(MODULE, path.resolve())]
    assert watched == plain
    assert watched.fingerprint() == plain.fingerprint()


def test_o35_load_faults_records_the_module_and_the_file_it_read(tmp_path):
    """O35: a plugin that reads only `[faults]` against a configuration it already holds is seen too."""
    path = write_faults(tmp_path, margin_s=100_000)
    config = load_platform(path, require_secrets=False)
    with watching_platform_loads() as loads:
        settings = load_faults(path, config)
    assert loads == [PlatformLoad(MODULE, path.resolve())]
    assert settings == load_faults(path, config)


def test_o35_the_hpc_backends_file_is_recorded_as_the_composes_is(tmp_path):
    """O35: the record does not depend on the backend the file declares."""
    path = write_hpc_platform(tmp_path)
    with watching_platform_loads() as loads:
        load_faults(path, load_platform(path, require_secrets=False))
    assert loads == [PlatformLoad(MODULE, path.resolve())]


def test_o35_a_load_that_is_refused_is_recorded_too(tmp_path):
    """O35: a loader that ran and refused its file still ran: a plugin that catches the refusal and builds the platform
    some other way has still read the study's platform configuration."""
    path = write_platform(tmp_path, lambda data: data.update(no_such_table={}))
    with watching_platform_loads() as loads, pytest.raises(ConfigError, match="no_such_table"):
        load_platform(path, require_secrets=False)
    assert loads == [PlatformLoad(MODULE, path.resolve())]


def test_o35_a_file_that_is_not_there_is_recorded_by_its_resolved_path(tmp_path):
    """O35: a missing file is a refusal, and still a record of the loader's run."""
    with watching_platform_loads() as loads, pytest.raises(ConfigError, match="file not found"):
        load_platform(tmp_path / "missing.toml")
    assert loads == [PlatformLoad(MODULE, (tmp_path / "missing.toml").resolve())]


def test_o35_the_record_holds_the_module_and_the_path_and_nothing_of_the_file(tmp_path):
    """O35: a record is a module name and a path: no value of the platform, no credential, no URL."""
    path = write_platform(tmp_path)
    with watching_platform_loads() as loads:
        load_platform(path, require_secrets=False)
    (load,) = loads
    assert tuple(load) == (MODULE, path.resolve())
    text = repr(load)
    for secret in ("token", "password", "http"):
        assert secret not in text.replace(str(path.resolve()), ""), text


def test_o35_the_loaders_run_by_a_platforms_own_tooling_outside_a_study_build_are_not_recorded(tmp_path):
    """O35: a day-0 tool or a federation script loads the same file with no study build open: nothing is recorded, and
    a build opened afterwards starts empty."""
    path = write_faults(tmp_path, margin_s=100_000)
    config = load_platform(path, require_secrets=False)
    load_faults(path, config)
    with watching_platform_loads() as loads:
        pass
    assert loads == []


def test_o35_the_hook_the_core_calls_runs_both_loaders_so_a_study_that_lists_the_module_is_covered(tmp_path):
    """O35: the core reads the declared deferral through the listed module's hook, which loads the file as a run does:
    both loaders record the module the study lists, so a listed module speaks for what its hook loaded."""
    path = write_faults(tmp_path, margin_s=100_000)
    with watching_platform_loads() as loads:
        kinds.declared_deferral_from_config(path)
    assert loads == [PlatformLoad(MODULE, path.resolve())]  # one file, loaded by both loaders, is one record
