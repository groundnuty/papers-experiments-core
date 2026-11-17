"""Fixtures for the Apptainer backend's tests: a work root short enough for a collector's socket."""

import shutil

import pytest
from apptainer_support import short_dir


@pytest.fixture
def short_tmp():
    """A private directory short enough for a collector's socket path (`apptainer_support.short_dir`: on a batch node
    pytest's own paths are too long for a Unix socket's address), removed after the test."""
    path = short_dir()
    yield path
    shutil.rmtree(path, ignore_errors=True)
