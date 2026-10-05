"""Anthropic's signed Claude Code release manifest (maintainer tooling).

Clients need no GnuPG: the native contract records the per-platform size and
sha256 a maintainer verified against the signed manifest
(``tools/pin_claude.py`` and ``claude-multi-dev repin``), and launch, setup
and doctor check files against those values only. ``platform_key`` is the one
client-side piece: it names this machine in the manifest's spelling.

Every network fetch is preceded by the exact per-candidate
:class:`RequestPlan` (two GETs, no authentication, the redirect/proxy
behaviour stated as it is), and :func:`fetch` has a total wall-clock
deadline. ``load_local`` (``--manifest-dir``) never falls back to the
network. GnuPG runs only here, with the vendored key in an ephemeral
keyring, ``--no-auto-key-retrieve`` and ``--no-autostart`` (no dirmngr, no
keyserver).
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform as host_platform
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import errors, strict_json


MANIFEST_URL = "https://downloads.claude.ai/claude-code-releases/{version}/manifest.json"
KEY_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
RELEASE_KEY = """-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGnK73ABEACnbytJXkjweYrwIr0aLEFRlH+C0nF44KxFc7gQmJ6PjSPMGZAD
dxZcaixU7zZl8WxEpVO0wLmIH8cf2zGOdyuZg1Yaugk1vHb2b8WBhAGCQJdPgB8W
XquedepEYtk56uP/gCoTjJDUZluEGBHnlnuujSJ4orxEdhSykEoAUfJZGEILPpMd
bphFt/Sn+Eb/TxM5jpKPdwnv8AShNF/1mZU1fWTQq9tRKJUakZj04gdaDFElQXak
CtTij+GT6yoYCARSHwGO+PC/Pr6q4tc+D7LRjxSBvUWDoFSmlqb/PJ1hj9D/7I2O
e4XXniAPWMR56KvxHlzOzrNQdJujbJdSkCwh1ZijkSd3y8ayW5WYUTGdRab99NUw
agzlabe/VVF6kzJ0Scn5q3PihB2Y9Bwo0CKnkYk7a7KT77EWv0Kkq+VHmOtqX3a2
hhX+b6a6ve9rzJ1qZYGj+obv/C3Sx1LzUjAfqVy7RJDf2uAoP5t2g8u/TkSpUxhM
VEjZBkSxYZhMyzQM6t8IgkUfnSrIPTHixbDWARZ4beMOBjxyPZK1nP7OOrNR3TkK
JtwLMQAabURCDnL0PjS0iwBTU4jtumBD1XSULyWuoTvMljrpQr1nV1oDyOt0OLqa
KA2McWtd9PdXhC8y2EIg7TmrTlJLfHYbdmkiCYj4J49Q8HWkN/6WE+RTUwARAQAB
tD5BbnRocm9waWMgQ2xhdWRlIENvZGUgUmVsZWFzZSBTaWduaW5nIDxzZWN1cml0
eUBhbnRocm9waWMuY29tPokCUQQTAQoAOxYhBDHd3iTd+rZ59C170rqpKf8afsrO
BQJpyu9wAhsPBQsJCAcCAiICBhUKCQgLAgQWAgMBAh4HAheAAAoJELqpKf8afsrO
l5IP/2I8X1dFy5xYczWB/coIxGjuzS/V6ByZGZZEJsbr04pmuHiFUykJqPGWGQ6q
U0YF5iEwvEkaagS5m7DzhSEf3FM3Cgafax/6d70tar9Vr1D+w6uPfxetu7u/WYJp
aolIsdh5fTrBh9zSM1Njl8FM8wG8CwZQjS33Oa7d8cwRkgdUWbt6LXgz+cTQNuBn
BgW6Ks7oZFI25dfu0ojDR+aDFJg4+4wZoyDLPvJz1SIrJ5WFGs67zsx9SfS3yZnf
XKmBe+f0dUy+GJ2nFZrXFf99+c0dPEHYO8DCeAHZizjkFrdYtUHdDU0YDYEGkLJa
bE+pgcpkHf5EvsZzHsyDbl95W/eh7pcXMbwkN+W4CBYUE9X4uHhqzWaC5yAVRWUA
1BJ9V4LjZfHPLEJt0I3TxzXiEg9/BVeaTYq9RjaxIFo9Nfk158HqJY6SA5jslBlx
Gv/No8u+xVcze2UJyGVfEIUfm92+0UAIkny3+5cuVV0ICzJxXlXj0CnLM9Lt50wE
p3suVwuBEviCbZ08eAH1Ht8gbBdSsiOkIU8CX3v/scwHHx5q0+NBL6xLrQObg13a
tRXBlKObfElkPN3lTUbUnJOW4U8uSjH8VRP+AujKWMDFe7x0zCs+iYY1mTOvbrTS
9n3CmZUmbynZ+E/QWNENpW/pDNZdWFy43PASmML5FHu4m9Sn
=oqMI
-----END PGP PUBLIC KEY BLOCK-----
"""
MAX_BYTES = 1 << 20
# One fetch (both requests together) never outlives this deadline.
FETCH_DEADLINE_SECONDS = 60.0
REQUEST_PLAN_TITLE = "Re-pin release-evidence request plan"
# The exact redirect/proxy behaviour of the fetch below (urllib follows
# a redirect only after _ReleaseRedirect accepted its target, and honours a
# configured https_proxy).
REDIRECT_PROXY_TEXT = ("Same-host HTTPS redirects on downloads.claude.ai may be followed; any other "
                       "destination is refused before a request; a configured HTTPS proxy is used.")
REQUEST_PLAN_QUESTION = "Proceed with these listed requests? [y/N] "
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_GPG_MISSING = "gpg not found (install GnuPG or set CLAUDE_MULTI_GPG)"


class ReleaseManifestError(errors.ClaudeMultiError, RuntimeError):
    """Release evidence could not be established."""


class ReleaseManifestUnavailable(ReleaseManifestError):
    """Unavailable evidence aborts update, never falls back to another candidate."""


class ReleaseManifestMismatch(ReleaseManifestError):
    """Evidence contradicts this candidate; update may try the next one."""


class ReleaseFetchDeclined(ReleaseManifestError):
    """The operator declined (or was never asked for) this candidate's
    request plan: nothing was sent."""


@dataclass(frozen=True)
class RequestPlan:
    """The exact requests one candidate's verification may send.

    Consent covers exactly these two URLs for exactly this version; another
    candidate gets its own plan and its own question."""

    version: str
    urls: tuple[str, str]

    def lines(self) -> tuple[str, ...]:
        return (
            REQUEST_PLAN_TITLE,
            f"  1. GET {self.urls[0]}",
            f"  2. GET {self.urls[1]}",
            "Authentication: none",
            f"Purpose: verify candidate {self.version} before executing it",
            REDIRECT_PROXY_TEXT,
            "No keyserver access and no automatic retries.",
            f"Caps: {MAX_BYTES // 1024} KiB per file; {int(FETCH_DEADLINE_SECONDS)} s for both requests together.",
        )

    def text(self) -> str:
        """The terminal form: the plan, then the y/N question (default no)."""

        return "\n".join(self.lines()) + "\n" + REQUEST_PLAN_QUESTION


def request_plan(version: str) -> RequestPlan:
    """The plan :func:`fetch` follows for ``version`` (validated, no I/O)."""

    _version(version)
    url = MANIFEST_URL.format(version=version)
    for target in (url, url + ".sig"):
        _check_url(target)
    return RequestPlan(version, (url, url + ".sig"))


@dataclass(frozen=True)
class ReleaseProof:
    version: str
    platform: str
    sha256: str
    size: int
    manifest_sha256: str
    signature_sha256: str
    fingerprint: str
    # Every build the signed manifest lists: {platform: {"sha256", "size"}}.
    platforms: Mapping[str, Mapping[str, object]] | None = None


_ARCH = {"x86_64": "x64", "amd64": "x64", "x64": "x64", "aarch64": "arm64", "arm64": "arm64"}
_SYSTEM = {"linux": "linux", "darwin": "darwin", "windows": "win32"}


def platform_key(*, system: str | None = None, machine: str | None = None,
                 musl: bool | None = None) -> str:
    """This machine in the manifest's spelling: ``linux-x64``, ``linux-arm64``
    (``-musl`` on a musl libc), ``darwin-x64``, ``darwin-arm64``,
    ``win32-x64`` or ``win32-arm64``."""

    system = (host_platform.system() if system is None else system).lower()
    machine = (host_platform.machine() if machine is None else machine).lower()
    arch = _ARCH.get(machine)
    name = _SYSTEM.get(system)
    if name is None or arch is None:
        raise ReleaseManifestUnavailable(f"unsupported platform {system}/{machine}")
    if name != "linux":
        return f"{name}-{arch}"
    if musl is None:
        musl = any(Path("/lib").glob("ld-musl-*.so.1"))
    return f"linux-{arch}" + ("-musl" if musl else "")


def _version(version: str) -> None:
    if not _VERSION.fullmatch(version):
        raise ReleaseManifestUnavailable("invalid release version")


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "downloads.claude.ai":
        raise ReleaseManifestUnavailable("network: release URL must use https://downloads.claude.ai")


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Refuse before following, not merely after disclosing a request elsewhere.
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_release(url: str, *, timeout: float):
    from . import tls

    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls.context()),
                                       _ReleaseRedirect()).open(url, timeout=timeout)


def _read_bounded(stream) -> bytes:
    data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ReleaseManifestUnavailable("file too large")
    return data


def _request(target: str, *, opener, timeout: float, holder: dict) -> bytes:
    """One GET: URL checks before the request and on the final URL, bounded read."""

    _check_url(target)
    try:
        with opener(target, timeout=timeout) as response:
            holder["response"] = response
            _check_url(response.geturl())
            return _read_bounded(response)
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        # Don't echo URLs/proxy credentials or arbitrary server error bodies.
        raise ReleaseManifestUnavailable(f"network: {type(exc).__name__}") from exc


def _abandon(response) -> None:
    """Close an abandoned response without the caller ever waiting on it.

    A real ``http.client.HTTPResponse`` reads through a buffered reader
    whose ``close()`` takes the lock the worker's in-progress read holds, so
    the close runs on its own daemon thread. Shutting the socket down first
    (no buffer lock involved) wakes that blocked read, so the worker ends
    promptly instead of at its per-operation timeout."""

    def close() -> None:
        raw = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if isinstance(sock, socket.socket):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            response.close()
        except Exception:  # best effort: the worker thread is abandoned either way
            pass

    threading.Thread(target=close, name="claude-multi-release-close", daemon=True).start()


def fetch(version: str, *, opener=_open_release, timeout: float = 30.0,
          deadline: float = FETCH_DEADLINE_SECONDS, clock=time.monotonic) -> tuple[bytes, bytes]:
    """Download only on explicit, consented update; callers/tests can inject the opener.

    ``timeout`` bounds each blocking socket operation; ``deadline`` bounds the
    whole fetch (both requests, connect and slow reads included): a request
    still running at the deadline is abandoned (its response closed off this
    thread, never waited on) and the fetch fails with a fixed reason. No retry, no other candidate."""

    plan = request_plan(version)
    started = clock()
    result: list[bytes] = []
    for target in plan.urls:
        remaining = deadline - (clock() - started)
        if remaining <= 0:
            raise ReleaseManifestUnavailable("network: total deadline exceeded")
        holder: dict = {}

        def work(target: str = target, per_call: float = min(timeout, remaining), holder: dict = holder) -> None:
            try:
                holder["data"] = _request(target, opener=opener, timeout=per_call, holder=holder)
            except BaseException as exc:  # handed to the caller's thread below
                holder["error"] = exc

        worker = threading.Thread(target=work, name="claude-multi-release-fetch", daemon=True)
        worker.start()
        worker.join(max(0.0, deadline - (clock() - started)))
        if worker.is_alive():
            response = holder.get("response")
            if response is not None:
                _abandon(response)  # never a blocking close on this thread
            raise ReleaseManifestUnavailable("network: total deadline exceeded")
        if "error" in holder:
            raise holder["error"]
        result.append(holder["data"])
    return result[0], result[1]


def load_local(directory: Path, version: str) -> tuple[bytes, bytes]:
    """Read one complete offline pair, never mix layouts or fall back to network."""
    _version(version)
    path = Path(directory) / version / "manifest.json"
    if not path.exists():
        path = Path(directory) / f"manifest-{version}.json"
    try:
        with path.open("rb") as manifest, Path(str(path) + ".sig").open("rb") as signature:
            return _read_bounded(manifest), _read_bounded(signature)
    except OSError as exc:
        raise ReleaseManifestUnavailable(f"offline files: {type(exc).__name__}") from exc


def gpg_executable(environ: Mapping[str, str]) -> str:
    configured = environ.get("CLAUDE_MULTI_GPG")
    executable = shutil.which(configured or "gpg", path=environ.get("PATH", os.defpath))
    if executable is None:
        raise ReleaseManifestUnavailable(_GPG_MISSING)
    return executable


def verify_signature(manifest: bytes, signature: bytes, *, gpg: str,
                     runner=subprocess.run) -> str:
    """Verify with only the vendored key in a private, ephemeral keyring."""
    if max(len(manifest), len(signature)) > MAX_BYTES:
        raise ReleaseManifestUnavailable("file too large")
    try:
        with tempfile.TemporaryDirectory(prefix="claude-multi-release-") as directory:
            root = Path(directory)
            (root / "manifest.json").write_bytes(manifest)
            (root / "manifest.json.sig").write_bytes(signature)
            (root / "release.asc").write_text(RELEASE_KEY, encoding="ascii")
            argv = [gpg, "--no-options", "--homedir", directory, "--batch", "--no-tty",
                    "--no-auto-key-retrieve", "--no-autostart"]
            kwargs = dict(capture_output=True, text=True, timeout=30,
                          env={"PATH": os.defpath, "HOME": directory, "GNUPGHOME": directory})
            imported = runner([*argv, "--import", str(root / "release.asc")], **kwargs)
            if imported.returncode != 0:
                raise ReleaseManifestUnavailable("gpg key import failed")
            verified = runner([*argv, "--status-fd", "1", "--verify",
                               str(root / "manifest.json.sig"), str(root / "manifest.json")],
                              **kwargs)
    except FileNotFoundError as exc:
        raise ReleaseManifestUnavailable(_GPG_MISSING) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseManifestUnavailable(f"gpg verification unavailable: {type(exc).__name__}") from exc
    statuses = [line.split() for line in (verified.stdout or "").splitlines()
                if line.startswith("[GNUPG:] ")]
    goodsig = any(len(row) >= 3 and row[1] == "GOODSIG" for row in statuses)
    valid = [row for row in statuses if len(row) == 12 and row[1] == "VALIDSIG"]
    if verified.returncode != 0 or not goodsig or not valid:
        raise ReleaseManifestMismatch("bad signature")
    if any(row[-1] != KEY_FINGERPRINT for row in valid):
        raise ReleaseManifestMismatch("signed by another key")
    return KEY_FINGERPRINT


def _manifest_document(manifest: bytes, version: str) -> dict:
    try:
        doc = strict_json.loads(manifest, strict_json.JSONLimits(max_bytes=MAX_BYTES))
    except (ValueError, RecursionError) as exc:
        raise ReleaseManifestMismatch("invalid manifest") from exc
    if not isinstance(doc, dict):
        raise ReleaseManifestMismatch("invalid manifest")
    if doc.get("version") != version:
        raise ReleaseManifestMismatch(f"manifest names version {doc.get('version')}")
    return doc


def manifest_platforms(manifest: bytes, *, version: str) -> dict[str, dict[str, object]]:
    """Every platform build the manifest lists, as ``{platform: {sha256, size}}``.

    Entries are checked strictly (lowercase 64-hex checksum, positive integer
    size, the expected binary name); a malformed entry refuses the manifest.
    """

    from . import pin

    doc = _manifest_document(manifest, version)
    platforms = doc.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ReleaseManifestMismatch("no platform entries")
    result: dict[str, dict[str, object]] = {}
    for name in sorted(platforms):
        entry = platforms[name]
        if name not in pin.PLATFORMS:
            continue  # a platform this release does not know is not pinned
        if not isinstance(entry, dict):
            raise ReleaseManifestMismatch(f"invalid {name} entry")
        checksum, size = entry.get("checksum"), entry.get("size")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ReleaseManifestMismatch(f"invalid {name} checksum")
        if type(size) is not int or size <= 0:
            raise ReleaseManifestMismatch(f"invalid {name} size")
        if entry.get("binary", pin.binary_name(name)) != pin.binary_name(name):
            raise ReleaseManifestMismatch(f"unexpected {name} binary name")
        result[name] = {"sha256": checksum, "size": size}
    if not result:
        raise ReleaseManifestMismatch("no known platform entries")
    return result


def check_manifest(manifest: bytes, *, version: str, platform: str,
                   sha256: str, size: int) -> None:
    doc = _manifest_document(manifest, version)
    platforms = doc.get("platforms")
    entry = platforms.get(platform) if isinstance(platforms, dict) else None
    if not isinstance(entry, dict):
        raise ReleaseManifestMismatch(f"no {platform} entry")
    checksum = entry.get("checksum")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or checksum != sha256:
        raise ReleaseManifestMismatch("sha256 differs")
    if type(entry.get("size")) is not int or entry["size"] != size or size < 0:
        raise ReleaseManifestMismatch("size differs")


def verify_release(version: str, sha256: str, size: int, *,
                   source: Callable[[str], tuple[bytes, bytes]], gpg: str,
                   platform: str | None = None, runner=subprocess.run) -> ReleaseProof:
    platform = platform_key() if platform is None else platform
    manifest, signature = source(version)
    fingerprint = verify_signature(manifest, signature, gpg=gpg, runner=runner)
    check_manifest(manifest, version=version, platform=platform, sha256=sha256, size=size)
    return ReleaseProof(version, platform, sha256, size, hashlib.sha256(manifest).hexdigest(),
                        hashlib.sha256(signature).hexdigest(), fingerprint,
                        manifest_platforms(manifest, version=version))
