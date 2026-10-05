"""Resolve the gateway a test runs: offline only, never a guessed binary.

Two kinds of gateway are accepted:

- A Nix store output (``/nix/store/...``): the explicitly selected one
  (``CLAUDE_MULTI_TEST_CLI_PROXY_API``) or, without a selection, the one the
  installed ``claude-multi-proxy`` wrapper, ``PATH`` or the Nix profile
  names. It must resolve to a regular executable that is not group- or
  other-writable.
- A content-bound build outside the store (a ``tools/build.py gateway``
  dist, as CI builds it): only the explicitly selected binary, and only
  with its build record named by ``CLAUDE_MULTI_TEST_GATEWAY_BUILD`` (the
  ``BUILD.json`` of ``tools/build.py gateway ... record``). The binary must
  be a regular executable, neither it nor a directory above it may be
  group- or other-writable (a root-owned sticky directory such as ``/tmp``
  excepted), its sha256 must be the record's entry for this host's target,
  and the record's patch series (basenames and sha256, in order) must be
  this checkout's ``gateway/UPSTREAM.json`` admitted series. An ambient
  gateway outside the store is never accepted.

The diagnostic probes built against a gateway (``tests/gateway-startup-
probe.nix``, or ``.github/scripts/gateway_probes.py`` for a content-bound
build) are bound the same way: :func:`diagnostic_problem`.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from claude_multi import probe
from claude_multi.probe import ProbeError

from _layout import UPSTREAM_JSON

BINARY_ENV = "CLAUDE_MULTI_TEST_CLI_PROXY_API"
BUILD_RECORD_ENV = "CLAUDE_MULTI_TEST_GATEWAY_BUILD"
NIX_STORE = "/nix/store/"
# A content-bound probe build: <out>/bin/<probe> and this record beside it,
# naming the gateway sha256 the probes were built against.
PROBE_RECORD = Path("share") / "probe-build.json"
PROBE_RECORD_FORMAT = 1
BUILD_FORMAT = 1


def _wrapper_gateway() -> str | None:
    wrapper = shutil.which("claude-multi-proxy")
    if wrapper is None:
        return None
    try:
        text = Path(wrapper).read_text()
    except (OSError, UnicodeError):
        return None
    match = re.search(r"(/nix/store/[a-z0-9]{32}-cli-proxy-api-[^/'\"\s]+/bin/cli-proxy-api)", text)
    return match.group(1) if match else None


def host_target() -> str | None:
    """This host's gateway target name (the recipe's ``linux-amd64`` form)."""

    system = {"linux": "linux", "darwin": "darwin"}.get(sys.platform)
    machine = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower())
    return f"{system}-{machine}" if system and machine else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def admitted_series() -> list[dict[str, str]]:
    """This checkout's admitted patch series, in order: basename and sha256."""

    document = json.loads(UPSTREAM_JSON.read_text())
    return [{"basename": entry["basename"], "sha256": entry["sha256"]}
            for entry in document["series"] if entry["admitted"]]


def unsafe_location(path: Path) -> str | None:
    """Why ``path`` (resolved) could be replaced by someone else: it or a
    directory above it is group- or other-writable. A root-owned sticky
    directory (``/tmp``) is no such directory: nobody else can rename or
    remove what the caller made in it. None when it is safe."""

    if os.stat(path).st_mode & 0o022:
        return f"{path} is group- or other-writable"
    for directory in path.parents:
        info = os.stat(directory)
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            return f"{directory} is group- or other-writable"
    return None


def _regular_executable(path: Path) -> str | None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        return f"{path} is not a regular executable file"
    return None


def build_record_problem(record_path: str | None) -> tuple[dict | None, str | None]:
    """The build record a content-bound gateway is checked against:
    ``(record, None)``, or ``(None, why it cannot bind anything)``."""

    if not record_path:
        return None, f"{BINARY_ENV} names a gateway outside {NIX_STORE}; set {BUILD_RECORD_ENV} to its build record"
    path = Path(record_path)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None, f"{BUILD_RECORD_ENV} {path} is not a regular file"
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return None, f"{BUILD_RECORD_ENV} {path} cannot be read ({exc})"
    if not isinstance(record, dict) or record.get("format") != BUILD_FORMAT:
        return None, f"{BUILD_RECORD_ENV} {path} is not a gateway build record"
    unsafe = unsafe_location(path.resolve())
    if unsafe:
        return None, f"the build record: {unsafe}"
    series = record.get("series")
    if record.get("admitted_series") is not True or series != admitted_series():
        return None, f"the build record {path} names another patch series than gateway/UPSTREAM.json"
    return record, None


def bound_gateway_problem(candidate: str, record_path: str | None) -> tuple[str | None, str]:
    """``(resolved path, "")`` when ``candidate`` is the content-bound
    gateway of its build record, else ``(None, why not)``."""

    real = Path(os.path.realpath(candidate))
    problem = _regular_executable(real)
    if problem:
        return None, problem
    unsafe = unsafe_location(real)
    if unsafe:
        return None, f"cli-proxy-api: {unsafe}"
    record, problem = build_record_problem(record_path)
    if record is None:
        return None, problem
    target = host_target()
    entry = (record.get("targets") or {}).get(target) if target else None
    if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
        return None, f"the build record names no {target} gateway"
    if sha256_file(real) != entry["sha256"]:
        return None, f"cli-proxy-api {real} is not the {target} gateway its build record names (sha256 differs)"
    return str(real), ""


def _version(real: Path) -> tuple[str | None, str]:
    log = Path(tempfile.mkdtemp(prefix="check-s12-version-"))
    try:
        os.chmod(log, 0o700)
        out = log / "version.txt"
        with open(out, "wb") as handle:
            process = probe.start_isolated_process(
                [str(real), "--version"],
                cwd=log,
                env={"HOME": str(log), "PATH": "/usr/bin:/bin"},
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            try:
                process.wait(timeout=15)
            finally:
                probe.stop_isolated_process(process)
        text = out.read_text(encoding="utf-8", errors="replace")
    except (OSError, ProbeError, subprocess.SubprocessError) as exc:
        return None, f"cli-proxy-api --version failed ({exc})"
    finally:
        shutil.rmtree(log, ignore_errors=True)
    found = re.search(r"Version:\s*([0-9][0-9A-Za-z.+-]*)", text)
    return str(real), found.group(1).rstrip(",") if found else "unknown"


def _gateway_binary() -> tuple[str | None, str]:
    """(path, version) of the gateway the tests run, or (None, reason).

    A store binary must resolve to a regular, non-writable file under
    ``/nix/store``; a content-bound binary must pass
    :func:`bound_gateway_problem`. The version comes from ``--version`` run
    inside a loopback-only namespace (offline). It is named, never required
    to equal ``catalog/gateway.json`` ``cliproxyapi_baseline``.
    """

    override = os.environ.get(BINARY_ENV)
    candidates = (
        (override,)
        if override
        else (
            _wrapper_gateway(),
            shutil.which("cli-proxy-api"),
            str(Path.home() / ".nix-profile" / "bin" / "cli-proxy-api"),
        )
    )
    for candidate in candidates:
        if not candidate:
            continue
        real = Path(os.path.realpath(candidate))
        if not str(real).startswith(NIX_STORE):
            if not override:
                # An installed gateway outside the store is never a test's.
                return None, f"cli-proxy-api {real} is not a /nix/store path"
            bound, reason = bound_gateway_problem(candidate, os.environ.get(BUILD_RECORD_ENV))
            if bound is None:
                return None, reason
            return _version(Path(bound))
        if not real.is_file() or not os.access(real, os.X_OK):
            continue
        if os.stat(real).st_mode & 0o022:
            return None, f"cli-proxy-api {real} is group/other-writable"
        return _version(real)
    return None, "packaged cli-proxy-api unavailable"


def diagnostic_problem(binary: Path, gateway: str) -> str | None:
    """Why the diagnostic probe ``binary`` (resolved) is not one built
    against ``gateway`` (the resolved gateway the tests run), or None.

    A store probe names its gateway's store output in
    ``share/gateway-outpath``; a content-bound probe's record
    (``share/probe-build.json`` beside its ``bin/``) names the gateway's
    sha256 and its own."""

    problem = _regular_executable(binary)
    if problem:
        return problem
    root = binary.parent.parent
    if str(binary).startswith(NIX_STORE):
        if binary.stat().st_mode & 0o022:
            return f"{binary} is group- or other-writable"
        try:
            built_for = (root / "share" / "gateway-outpath").read_text().strip()
        except OSError:
            return f"{binary} records no gateway (share/gateway-outpath)"
        if built_for != str(Path(gateway).parent.parent):
            return "the diagnostic belongs to another gateway build"
        return None
    unsafe = unsafe_location(binary)
    if unsafe:
        return unsafe
    path = root / PROBE_RECORD
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return f"{path} is not a regular file"
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return f"{binary} has no readable probe record ({PROBE_RECORD})"
    unsafe = unsafe_location(path)
    if unsafe:
        return unsafe
    probes = record.get("probes") if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get("format") != PROBE_RECORD_FORMAT
            or not isinstance(probes, dict) or probes.get(binary.name) != sha256_file(binary)):
        return f"{binary} is not the probe its record names"
    if record.get("series") != admitted_series():
        return "the diagnostic was built from another patch series"
    if record.get("gateway_sha256") != sha256_file(Path(gateway)):
        return "the diagnostic belongs to another gateway build"
    return None
