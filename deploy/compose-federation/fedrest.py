"""The bootstrap's HTTP client: every call verifies the federation's certificate against its own CA.

A failure raises `RestError` with the method, the path as the caller labelled it, the status and Onedata's error id,
never a body, a header or a URL with an id in it, since an answer can hold a token. Proxies from the environment are
ignored: the federation is on the host's loopback. A federation whose services' names resolve nowhere (the HPC
federation's, O73) gives `Rest` its address map: each such name is then dialled at its mapped address, with the name in
the URL, the handshake, the certificate check and `Host`.
"""

import base64
import ssl
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from expcore_onedata.addresses import AddressMap
from expcore_onedata.client import MappedTransport

ZONE_API = "/api/v3/onezone"
PROVIDER_API = "/api/v3/oneprovider"
PANEL_API = "/api/v3/onepanel"
PANEL_USER = "onepanel"  # the Basic-auth user name of a panel's emergency passphrase, as the images' scripts use it
CHUNK_BYTES = 256 * 1024  # what `Rest.stream` holds of a body at a time
ERROR_BODY_BYTES = 64 * 1024  # the most of a streamed error answer's body read to find Onedata's error id


class RestError(RuntimeError):
    """A call failed; `status` is None when no answer came back."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def basic(user: str, password: str) -> dict[str, str]:
    """An Authorization header for HTTP Basic."""
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def bearer(token: str) -> dict[str, str]:
    """The header Onedata reads an access token from."""
    return {"X-Auth-Token": token}


def _error_id(response: httpx.Response) -> str | None:
    """Onedata's error id, with the errno of a storage error (a short code such as `enoent`, never a value)."""
    try:
        error = response.json()["error"]
        found = error["id"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(found, str) or len(found) > 64:
        return None
    errno = error.get("details", {}).get("errno") if isinstance(error.get("details"), dict) else None
    return f"{found}: {errno}" if isinstance(errno, str) and errno.isalnum() and len(errno) <= 16 else found


def _streamed_error_id(response: httpx.Response) -> str | None:
    """`_error_id` of a streamed answer, reading at most `ERROR_BODY_BYTES` of its body (more is no Onedata error)."""
    body = b""
    for piece in response.iter_bytes():
        body += piece
        if len(body) > ERROR_BODY_BYTES:
            return None
    return _error_id(httpx.Response(response.status_code, content=body))


class Rest:
    """Calls to the federation's services by their base URL."""

    def __init__(self, ca_file: Path, timeout_s: float = 60.0, addresses: AddressMap | None = None) -> None:
        context = ssl.create_default_context(cafile=str(ca_file))
        if addresses is None:
            self._http = httpx.Client(verify=context, timeout=timeout_s, trust_env=False)
        else:
            transport = MappedTransport(addresses, verify=context)
            self._http = httpx.Client(transport=transport, timeout=timeout_s, trust_env=False)

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        method: str,
        url: str,
        *,
        label: str,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        """Send one request; raise RestError, named by `label`, unless the status is in `ok`."""
        try:
            response = self._http.request(
                method, url, headers=dict(headers or {}), json=json, params=params, content=content
            )
        except httpx.HTTPError as error:
            raise RestError(f"{method} {label}: no answer ({type(error).__name__})") from None
        if response.status_code not in ok:
            code = _error_id(response)
            detail = f" ({code})" if code else ""
            raise RestError(f"{method} {label}: answered {response.status_code}{detail}", response.status_code, code)
        return response

    def stream(
        self,
        method: str,
        url: str,
        *,
        label: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Iterator[bytes]:
        """Send one request and give the body of its answer in chunks of `CHUNK_BYTES` (decoded, as `call`'s `.content`
        is), holding one chunk and never the body: the answer is not read ahead, never stored on the response, and
        closed whether the caller reads it to the end, stops, or fails. Raise RestError, named by `label`, unless the
        status is in `ok`, as `call` does (an error answer's body is read to at most `ERROR_BODY_BYTES`), and for a
        connection that fails before the answer or part-way through it, which `call` would have raised inside its own
        send: a caller that hashes the chunks must drop what it has hashed. The request goes out on the first `next`."""
        request = self._http.build_request(method, url, headers=dict(headers or {}), params=params)
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise RestError(f"{method} {label}: no answer ({type(error).__name__})") from None
        try:
            if response.status_code not in ok:
                status, code = response.status_code, _streamed_error_id(response)
                detail = f" ({code})" if code else ""
                raise RestError(f"{method} {label}: answered {status}{detail}", status, code)
            yield from response.iter_bytes(CHUNK_BYTES)
        except httpx.HTTPError as error:
            raise RestError(f"{method} {label}: no answer ({type(error).__name__})") from None
        finally:
            response.close()

    def json(self, method: str, url: str, **options: Any) -> Any:
        """Send one request and return its decoded JSON body, or {} when it has none."""
        response = self.call(method, url, **options)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise RestError(f"{method} {options['label']}: the answer is not JSON") from None


def wait_until(what: str, probe: Callable[[], bool], timeout_s: float, interval_s: float = 5.0) -> float:
    """Call `probe` until it returns True; return the seconds it took, or raise TimeoutError naming `what`.

    A probe that raises RestError counts as not yet; any other exception is a defect and propagates.
    """
    began = time.monotonic()
    while True:
        try:
            if probe():
                return time.monotonic() - began
        except RestError:
            pass
        if time.monotonic() - began > timeout_s:
            raise TimeoutError(f"{what} did not happen within {timeout_s:.0f} s")
        time.sleep(interval_s)
