"""The declared package indexes (O95): a plain `pip install` and a URL on an index's host are reported in their own
class and flag nothing; every other hit, and every outside connection, flags as before."""

import pytest
from egress_logs import CANARY, NONCE, log, rec, record

from expcore.egress import NAMESPACE, OUTSIDE_FLAG, STATIC_SCAN, EgressLog, declaration, judge, scan_cells
from expcore.egress.package_index import index_hosts, plain_installs, recorded_package_indexes

DECLARED = declaration([CANARY], CANARY, hosts=["provider-a.test"])
INDEXES = ("files.pythonhosted.org", "pypi.org")


def hits(code: str, indexes=INDEXES) -> list[tuple[str, str, bool]]:
    return [(h["kind"], h["what"], h["flag"]) for h in scan_cells([code], DECLARED, package_indexes=indexes)["hits"]]


@pytest.mark.parametrize(
    "code",
    [
        "!pip install pandas",
        "%pip install -q 'scikit-learn>=1.3' numpy==1.26.*",
        "!pip3 install --quiet --upgrade --no-cache-dir 'pandas[excel]>=2,<3'  # for the frames",
        "!pip install -qU pandas 2>&1 | tail -1",
        "!pip install pandas > /dev/null 2>&1",
        "os.system('python -m pip install pandas')",
        "subprocess.run('pip install pandas seaborn', shell=True, check=True)",
        "subprocess.run(['pip', 'install', 'pandas'])",
        "subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'pandas', 'pyarrow'])",
        "subprocess.check_call([\n    sys.executable, '-m', 'pip', 'install',\n    'pandas',\n    'pyarrow',\n])",
        "!pip install --root-user-action=ignore pandas\n!pip install --progress-bar off numpy",
    ],
)
def test_a_plain_install_from_the_default_index_is_a_package_index_hit_that_does_not_flag(code):
    """O95: every form the scan matches, with plain requirements and harmless options only."""
    assert ("package_index", "pip install", False) in hits(code)
    assert not scan_cells([code], DECLARED, package_indexes=INDEXES)["flagged"]


@pytest.mark.parametrize(
    "code",
    [
        "!pip install -i https://pypi.org/simple pandas",
        "!pip install --index-url=https://mirror.example.net/simple pandas",
        "!pip install pandas --extra-index-url https://mirror.example.net/simple",
        "!pip install --ind https://mirror.example.net/simple pandas",  # an abbreviation pip accepts
        "!pip install -qi https://mirror.example.net/simple pandas",  # a short-option cluster
        "!pip install -f /mnt/wheels pandas",
        "!pip install --find-links //fileserver.example.net/wheels pandas",
        "!pip install --trusted-host mirror.example.net pandas",
        "!pip install -r requirements.txt",
        "!pip install -c constraints.txt pandas",
        "!pip install -e ./local",
        "!pip install git+https://example.org/r.git",
        "!pip install 'pkg @ https://example.org/pkg.whl'",
        "!pip install https://files.pythonhosted.org/packages/pkg.whl",
        "!pip install file://fileserver.example.net/share/pkg.whl",
        "!pip install //fileserver.example.net/share/pkg.whl",
        "!pip install $PKG",
        "os.system(f'pip install {name}')",
        "subprocess.run(['pip', 'install', name])",
        "subprocess.run(['pip', 'install', *names])",
        "subprocess.run(['pip', 'install'] + names)",
        "os.system('pip install ' + name)",
        "os.system('pip install %s' % name)",
        "cmd = 'pip install'\nos.system(cmd + ' ' + name)",
        "!pip install demo-1.0-py3-none-any.whl",
        "!pip install 'demo-1.0.tar.gz[extra]'",
        'os.system("pip install demo" r" -r requirements.txt")',
        "os.system('pip install demo' ' -r requirements.txt')",
        "os.system('pip install demo'\n          ' -r requirements.txt')",
        "!pip install demo `printf %s ./wheels/demo`",
        "subprocess.run(\n    ['pip', 'install',\n     'pandas', '-i', 'https://pypi.org/simple'])",  # over lines
        "!PIP_INDEX_URL=https://mirror.example.net/simple pip install pandas",
        "os.environ['PIP_EXTRA_INDEX_URL'] = u\n!pip install pandas",
        "!pip config set global.index-url https://mirror.example.net/simple\n!pip install pandas",
        "!uv pip install --index https://mirror.example.net/simple pandas",
        "!pip install pandas\n!pip install -i https://mirror.example.net/simple numpy",  # one of two names another
    ],
)
def test_an_install_that_names_another_index_url_or_file_keeps_its_flag(code):
    """O95: an index or find-links option of any spelling, a file, a path, a URL, a VCS requirement, a substitution or
    an index setting in the cell: the `pip install` hit flags as before, and so does the run."""
    assert ("download", "pip install", True) in hits(code)
    assert scan_cells([code], DECLARED, package_indexes=INDEXES)["flagged"]


@pytest.mark.parametrize("code", ["print(1)", "x = 'pipeline install'"])
def test_a_cell_without_an_install_has_no_plain_install(code):
    assert plain_installs(code) is False


def test_a_url_on_a_declared_index_host_is_a_package_index_hit_and_one_elsewhere_flags():
    """O95: exact host match only, never a suffix; http(s) only."""
    code = "a = 'https://pypi.org/simple/numpy/'\nb = 'https://pypi.org.example.net/x'\nc = 'https://example.org/'"
    assert hits(code) == [
        ("package_index", "pypi.org", False),
        ("url", "pypi.org.example.net", True),
        ("url", "example.org", True),
    ]
    assert hits("u = 'ssh://pypi.org/x'") == [("url", "pypi.org", True)]
    assert hits("u = 'https://pypi.org)@mirror.example.net/pkg'") == [("url", "pypi.org", True)]  # elsewhere's
    assert hits('u = "https://pypi.org\'@mirror.example.net/pkg"') == [("url", "pypi.org", True)]
    assert hits("get('https://pypi.org')\nget(\"https://pypi.org/simple/\")") == [("package_index", "pypi.org", False)]


def test_an_index_setting_in_one_cell_keeps_every_install_of_the_run_flagged():
    """O95: a setting made in one cell holds in the next."""
    cells = ["os.environ['PIP_FIND_LINKS'] = '/mnt/wheels'", "%pip install demo"]
    found = scan_cells(cells, DECLARED, package_indexes=INDEXES)
    assert ("download", "pip install", True) in [(h["kind"], h["what"], h["flag"]) for h in found["hits"]]
    assert found["flagged"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("os.system('curl -s https://pypi.org/simple/')", ("download", "curl", True)),
        ("subprocess.run(['wget', 'https://files.pythonhosted.org/x'])", ("download", "wget", True)),
        ("!git clone https://example.org/r.git", ("download", "git clone", True)),
        ("os.system('conda install -y scipy')", ("download", "conda install", True)),
        ("import requests\nrequests.get('https://pypi.org/pypi/pandas/json')", ("network_call", "requests", True)),
    ],
)
def test_every_other_download_and_network_call_flags_as_before(code, expected):
    assert expected in hits(code)
    assert scan_cells([code], DECLARED, package_indexes=INDEXES)["flagged"]


def test_undeclared_the_scan_is_what_it_was():
    """O95: no declared indexes, no new key and no reclassified hit."""
    code = "!pip install pandas\nu = 'https://pypi.org/simple/'"
    assert scan_cells([code], DECLARED, package_indexes=()) == scan_cells([code], DECLARED)
    assert set(scan_cells([code], DECLARED)) == {"version", "cells", "hits", "hits_cut", "flagged"}
    assert hits(code, ()) == [("url", "pypi.org", True), ("download", "pip install", True)]


def test_a_declared_scan_says_under_which_indexes_and_how_many_package_index_hits():
    scanned = scan_cells(["!pip install pandas", "u = 'https://pypi.org/'"], DECLARED, package_indexes=INDEXES)
    assert (scanned["package_indexes"], scanned["package_index"]) == (list(INDEXES), 2)


class Source:
    def __init__(self, data):
        self.data = data

    def egress_log(self):
        return self.data


def snapshot(records) -> dict:
    return {NAMESPACE: EgressLog(Source(log(records)), DECLARED, NONCE).finish()}


def test_a_package_index_only_run_is_not_flagged_and_says_so():
    verdict = judge(record(instruments=snapshot([]), cells=["!pip install pandas"]), "c", package_indexes=INDEXES)
    assert (verdict.flags, verdict.package_index) == ((), True)


def test_an_outside_connection_flags_with_the_class_declared():
    """O95: the log holds addresses only, so an outside connection is never allow-listed."""
    run = record(instruments=snapshot([rec("connect", "192.0.2.1", 443)]), cells=["!pip install pandas"])
    verdict = judge(run, "c", package_indexes=INDEXES)
    assert (verdict.flags, verdict.package_index) == ((OUTSIDE_FLAG,), True)


def test_a_foreign_install_flags_the_run_with_the_class_declared():
    run = record(instruments=snapshot([]), cells=["!pip install -i https://mirror.example.net/simple pandas"])
    assert judge(run, "c", package_indexes=INDEXES).flags == (STATIC_SCAN,)


def test_the_declared_hosts_are_bare_names_once_each_sorted():
    assert index_hosts(["PyPI.org.", "files.pythonhosted.org"]) == INDEXES
    for bad in ([], "pypi.org"):
        with pytest.raises(ValueError, match="at least one host name"):
            index_hosts(bad)
    for bad in (["https://pypi.org"], ["pypi.org/simple"], ["pypi.org:443"], ["*.pypi.org"], [1]):
        with pytest.raises(ValueError, match="not a bare host name"):
            index_hosts(bad)
    with pytest.raises(ValueError, match="more than once"):
        index_hosts(["pypi.org", "PYPI.org"])


def test_the_recorded_indexes_are_read_from_the_fingerprints_caps_and_any_other_form_is_none():
    assert recorded_package_indexes({"caps": {"egress": {"package_indexes": list(INDEXES)}}}) == INDEXES
    for fingerprint in (None, {}, {"caps": {}}, {"caps": {"egress": {"package_indexes": ["pypi.org", "a.test"]}}}):
        assert recorded_package_indexes(fingerprint) == ()  # unsorted is not the form the campaign records
    assert recorded_package_indexes({"caps": {"egress": {"package_indexes": ["pypi.org"], "x": 1}}}) == ()
