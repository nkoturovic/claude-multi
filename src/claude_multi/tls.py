"""Certificate trust for the launcher's own HTTPS requests.

The release update, the Claude Code download and the release-manifest fetch
verify servers with one context from :func:`context`; verification is never
turned off. Where the trusted certificates come from (:func:`source`, which
doctor reports):

1. ``SSL_CERT_FILE`` and/or ``SSL_CERT_DIR`` when set (a corporate or
   private CA bundle; the same variables OpenSSL reads, and the gateway
   receives them too: the on-demand gateway from the launcher's
   environment, the supervised service from its unit, which
   ``claude-multi gateway service install`` writes with them, bound
   read-only; :func:`configured`).
2. Otherwise the interpreter's default locations, when they exist.
3. Otherwise the first system bundle found at a well-known path. The
   bundled Python of a release looks for ``/etc/ssl/cert.pem`` and
   ``/etc/ssl/certs`` by default, which some Linux distributions do not
   provide.

A configured location that is missing or cannot be loaded is trusted as
empty, as OpenSSL treats it: the context is still built (plain ``http``
requests through the same opener are unaffected) and an ``https`` request
fails its certificate check instead of falling back to other certificates.
doctor marks a missing location.

Claude Code itself (a Node program) reads ``NODE_EXTRA_CA_CERTS``, not
these; the launcher passes that variable through untouched.
"""

from __future__ import annotations

import os
import ssl
from typing import Callable, Mapping

# System CA bundles, most common first (Debian/Ubuntu, Fedora/RHEL, openSUSE,
# Alpine/macOS-style).
SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)


def _nonempty_dir(path: str | None) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            return any(True for _entry in entries)
    except OSError:
        return False


VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")


def _set(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    return value if value else None


def configured(environ: Mapping[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """``(variable, value)`` for each of ``SSL_CERT_FILE`` and
    ``SSL_CERT_DIR`` the environment sets: the certificates :func:`source`
    trusts first, and what the supervised gateway service carries."""

    env = os.environ if environ is None else environ
    return tuple((name, value) for name in VARIABLES if (value := _set(env, name)) is not None)


def source(environ: Mapping[str, str] | None = None, *,
           show: Callable[[str], str] | None = None) -> tuple[str | None, str | None, str]:
    """``(cafile, capath, description)`` of the certificates :func:`context` trusts.

    ``show`` formats each ``SSL_CERT_FILE``/``SSL_CERT_DIR`` value in the
    description (doctor's home-relative path display); the returned paths
    stay as set."""

    env = os.environ if environ is None else environ
    cafile, capath = _set(env, "SSL_CERT_FILE"), _set(env, "SSL_CERT_DIR")
    if cafile or capath:
        shown = show or (lambda value: value)
        parts = [f"{name}={shown(value)}" + ("" if exists(value) else " (missing)")
                 for name, value, exists in (("SSL_CERT_FILE", cafile, os.path.isfile),
                                             ("SSL_CERT_DIR", capath, os.path.isdir)) if value]
        return cafile, capath, " and ".join(parts)
    defaults = ssl.get_default_verify_paths()
    if (defaults.cafile and os.path.isfile(defaults.cafile)) or _nonempty_dir(defaults.capath):
        return None, None, "the system default certificates"
    for candidate in SYSTEM_BUNDLES:
        if os.path.isfile(candidate):
            return candidate, None, f"the system bundle {candidate}"
    return None, None, "the system default certificates (none found)"


def context(environ: Mapping[str, str] | None = None) -> ssl.SSLContext:
    """A verifying client context for the trust :func:`source` names."""

    cafile, capath, _description = source(environ)
    usable_file = cafile if cafile and os.path.isfile(cafile) else None
    usable_dir = capath if capath and os.path.isdir(capath) else None
    if (cafile or capath) and not (usable_file or usable_dir):
        result = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # trusts nothing
    else:
        try:
            result = ssl.create_default_context(cafile=usable_file, capath=usable_dir)
        except (OSError, ssl.SSLError):
            result = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # unloadable: trusts nothing
    result.check_hostname = True
    result.verify_mode = ssl.CERT_REQUIRED
    return result
