"""What a record says about the route: the URL without credentials, SDK versions, the settings actually sent (B15)."""

import pytest

from expcore.models.route import public_url, sdk_version, settings_sent


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:pass@llm.example.invalid/v1", "https://llm.example.invalid/v1"),
        ("https://token@llm.example.invalid:8443/v1/", "https://llm.example.invalid:8443/v1/"),
        ("https://llm.example.invalid/v1?api_key=abc#frag", "https://llm.example.invalid/v1"),
        ("http://[2001:db8::1]:8000/v1", "http://[2001:db8::1]:8000/v1"),
        ("https://llm.example.invalid", "https://llm.example.invalid"),
        (None, None),
        ("", ""),
    ],
)
def test_b15_credentials_query_and_fragment_are_stripped_from_a_recorded_url(url, expected):
    """B15: a base URL with credentials is recorded without them."""
    assert public_url(url) == expected


def test_b15_a_url_that_cannot_be_parsed_is_not_recorded_as_given():
    """B15: if the URL cannot be taken apart, nothing of it is kept, since it may hold a secret."""
    assert public_url("https://user:secret@host:notaport/v1") == "<unparseable url>"


def test_b15_the_settings_sent_are_a_copy_of_the_request_without_the_conversation():
    """B15: The settings sent are a copy of the request without the conversation."""
    request = {
        "model": "test-model",
        "temperature": 0.2,
        "max_tokens": 512,
        "extra_body": {"provider": {"order": ["a"], "allow_fallbacks": False}},
        "messages": [{"role": "user", "content": "private prompt"}],
        "tools": [{"name": "t"}],
        "system": [{"type": "text", "text": "private system"}],
    }
    sent = settings_sent(request)
    assert sent == {
        "model": "test-model",
        "temperature": 0.2,
        "max_tokens": 512,
        "extra_body": {"provider": {"order": ["a"], "allow_fallbacks": False}},
    }
    request["extra_body"]["provider"]["order"].append("b")
    assert sent["extra_body"]["provider"]["order"] == ["a"]


def test_sdk_version_reads_an_installed_package_and_none_for_a_missing_one():
    assert sdk_version("openai")
    assert sdk_version("anthropic")
    assert sdk_version("no-such-distribution-xyz") is None
