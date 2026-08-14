import pytest
from nebo.core.uri import resolve_uri, Mode


@pytest.mark.parametrize(
    "uri,expected_mode,expected_dest",
    [
        (None, Mode.FILE, ".nebo/"),
        ("", Mode.FILE, ".nebo/"),
        (".nebo/", Mode.FILE, ".nebo/"),
        ("runs/today/", Mode.FILE, "runs/today/"),
        ("/var/log/nebo/", Mode.FILE, "/var/log/nebo/"),
        ("./runs", Mode.FILE, "./runs"),
        ("http://localhost:7861", Mode.NETWORK, "http://localhost:7861"),
        ("https://me.hf.space", Mode.NETWORK, "https://me.hf.space"),
        ("localhost:7861", Mode.NETWORK, "http://localhost:7861"),
        ("my-host:9000/api", Mode.NETWORK, "http://my-host:9000/api"),
        ("1.2.3.4:80", Mode.NETWORK, "http://1.2.3.4:80"),
    ],
)
def test_resolve_uri_happy_paths(uri, expected_mode, expected_dest):
    mode, dest = resolve_uri(uri)
    assert mode is expected_mode
    assert dest == expected_dest


def test_resolve_uri_rejects_ws():
    with pytest.raises(ValueError, match="ws not supported"):
        resolve_uri("ws://localhost:7861")
    with pytest.raises(ValueError, match="ws not supported"):
        resolve_uri("wss://example.com")


def test_resolve_uri_ipv6_must_use_full_url():
    mode, dest = resolve_uri("http://[::1]:7861/")
    assert mode is Mode.NETWORK
    assert dest == "http://[::1]:7861/"
