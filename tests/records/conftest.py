"""Fixtures for these tests: a process environment that holds no secret, so a record is redacted only on request."""

import pytest
from record_support import no_secrets_in_the_environment

__all__ = ["no_secrets_in_the_environment"]


@pytest.fixture(autouse=True)
def _clean_environment(no_secrets_in_the_environment):
    """Use the shared fixture for every test of the directory."""
