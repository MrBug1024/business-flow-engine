"""Safe, on-demand transport checks for an OpenAI-compatible model gateway.

The Studio's normal model turn is a POST to ``/chat/completions`` with an API
key.  This module deliberately does neither: it makes one short TCP connection
to the configured endpoint host so the UI can distinguish missing configuration,
local socket permission failures, and an endpoint that cannot be reached from
this server before a long agent run begins.
"""

from __future__ import annotations

import errno
import socket
from time import monotonic, time
from typing import Any
from urllib.parse import urlsplit


DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
_MAX_CONNECT_TIMEOUT_SECONDS = 5.0
_PLACEHOLDER_API_KEYS = frozenset({"", "your-api-key-here", "sk-xxx", "changeme"})
_LOCAL_SOCKET_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM, 10013})


def preflight_model_gateway(
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return a sanitized, TCP-only readiness report for one configured model.

    No HTTP request is constructed, no completion path is requested, and no
    credentials are put on the wire.  A successful report therefore proves
    only that this server can establish a TCP connection to the configured
    provider endpoint; it intentionally does not claim that the key, model, or
    completion API has been validated.
    """

    report = _base_report(model)
    normalized_key = api_key.strip()
    normalized_url = base_url.strip()
    if normalized_key.casefold() in _PLACEHOLDER_API_KEYS:
        return _finish(
            report,
            status="configuration_incomplete",
            ready=False,
            message="An API key is not configured for this model.",
            configuration="api_key_missing",
        )
    if not normalized_url:
        return _finish(
            report,
            status="configuration_incomplete",
            ready=False,
            message="A model endpoint is not configured.",
            configuration="base_url_missing",
        )

    endpoint = _endpoint(normalized_url)
    if endpoint is None:
        return _finish(
            report,
            status="invalid_endpoint",
            ready=False,
            message="The model endpoint must be a valid HTTP or HTTPS URL.",
            configuration="invalid_endpoint",
        )

    host, port = endpoint
    started = monotonic()
    try:
        # ``create_connection`` only opens a TCP socket.  It sends no HTTP
        # request, authorization header, completion payload, or API key.
        connection = socket.create_connection(
            (host, port),
            timeout=_bounded_timeout(timeout_seconds),
        )
        connection.close()
    except OSError as exc:
        return _socket_failure(report, exc, started)

    report["latency_ms"] = _elapsed_ms(started)
    return _finish(
        report,
        status="ready",
        ready=True,
        message=(
            "The endpoint accepted a TCP connection. This did not send credentials "
            "or test a completion."
        ),
        configuration="ready",
        local_socket="available",
        provider_transport="reachable",
    )


def _base_report(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "status": "unknown",
        "ready": False,
        "message": "",
        "checked_at": int(time()),
        "latency_ms": None,
        "checks": {
            "configuration": "not_checked",
            "local_socket": "not_checked",
            "provider_transport": "not_checked",
        },
        "probe": {
            "kind": "tcp_connect",
            "completion_called": False,
            "credentials_sent": False,
        },
    }


def _finish(
    report: dict[str, Any],
    *,
    status: str,
    ready: bool,
    message: str,
    configuration: str,
    local_socket: str = "not_checked",
    provider_transport: str = "not_checked",
) -> dict[str, Any]:
    report["status"] = status
    report["ready"] = ready
    report["message"] = message
    checks = report["checks"]
    checks["configuration"] = configuration
    checks["local_socket"] = local_socket
    checks["provider_transport"] = provider_transport
    return report


def _endpoint(base_url: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    # Credentials in a provider URL would be easy to leak through browser
    # configuration.  The Studio's separate API-key field is the only allowed
    # credential source, even though this probe never uses it.
    if parsed.username is not None or parsed.password is not None:
        return None
    return parsed.hostname, port or (443 if parsed.scheme == "https" else 80)


def _socket_failure(
    report: dict[str, Any],
    exc: OSError,
    started: float,
) -> dict[str, Any]:
    report["latency_ms"] = _elapsed_ms(started)
    error_number = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    if (
        isinstance(exc, PermissionError)
        or error_number in _LOCAL_SOCKET_PERMISSION_ERRNOS
        or winerror in _LOCAL_SOCKET_PERMISSION_ERRNOS
    ):
        return _finish(
            report,
            status="local_socket_permission_denied",
            ready=False,
            message=(
                "This server is not permitted to open the outbound socket needed for "
                "the model gateway. Check local process or network permissions."
            ),
            configuration="ready",
            local_socket="permission_denied",
            provider_transport="not_checked",
        )
    if isinstance(exc, (TimeoutError, socket.timeout)):
        transport = "timeout"
    elif isinstance(exc, socket.gaierror):
        transport = "name_unresolved"
    elif isinstance(exc, ConnectionRefusedError):
        transport = "connection_refused"
    else:
        transport = "unreachable"
    return _finish(
        report,
        status="provider_unreachable",
        ready=False,
        message=(
            "The configured provider endpoint could not be reached from this server. "
            "This check does not reveal endpoint or socket details."
        ),
        configuration="ready",
        local_socket="available",
        provider_transport=transport,
    )


def _bounded_timeout(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = DEFAULT_CONNECT_TIMEOUT_SECONDS
    return min(max(numeric, 0.1), _MAX_CONNECT_TIMEOUT_SECONDS)


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1_000))


__all__ = ["DEFAULT_CONNECT_TIMEOUT_SECONDS", "preflight_model_gateway"]
