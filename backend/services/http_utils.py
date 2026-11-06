from __future__ import annotations

import logging
import os
import ssl
from typing import Any

import certifi
import httpx

logger = logging.getLogger(__name__)

# TLS verification is always on. There is no insecure fallback and no switch to disable
# verification: a certificate failure is reported, never bypassed.
#
# Behind a TLS-inspecting corporate proxy the proxy's root CA is not in the default trust
# store. Point GEO_HTTP_CA_BUNDLE (or the standard SSL_CERT_FILE / REQUESTS_CA_BUNDLE) at a PEM
# file containing that CA; it is *added* to the default trust store.
_LEGACY_INSECURE_FLAGS = ("GEO_HTTP_ALLOW_INSECURE_FALLBACK", "GEO_HTTP_VERIFY_SSL")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() not in {"0", "false", "no", "off"}


def _build_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    extra = (
        os.getenv("GEO_HTTP_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE") or ""
    ).strip()
    if extra:
        try:
            context.load_verify_locations(cafile=os.path.expanduser(extra))
            logger.info("event=tls_extra_ca_loaded")
        except (OSError, ssl.SSLError) as exc:
            logger.warning("event=tls_extra_ca_failed error=%s", type(exc).__name__)
    return context


SSL_CONTEXT = _build_ssl_context()

for _flag in _LEGACY_INSECURE_FLAGS:
    _value = os.getenv(_flag)
    if _value is not None and ((_flag.endswith("FALLBACK") and _env_bool(_flag, False)) or (_flag.endswith("SSL") and not _env_bool(_flag, True))):
        logger.warning("event=tls_legacy_flag_ignored flag=%s", _flag)


async def request(
    method: str,
    url: str,
    *,
    timeout: httpx.Timeout | float,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request with certificate verification always enabled."""
    async with httpx.AsyncClient(timeout=timeout, verify=SSL_CONTEXT, headers=headers) as client:
        return await client.request(method, url, **kwargs)
