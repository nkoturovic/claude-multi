"""A local fake release directory for installer and self-update tests.

Bundles are small: a ``MANIFEST.json``, a ``runtime/python/bin/python3``
wrapper around the test interpreter, ``bin/`` launchers that log their
arguments to ``$HOME/launcher.log`` (or, with ``real_launchers``, the
release launchers that run the package; ``launcher``, a template of
``{name}`` and ``{version}``, replaces both), and a copy of this checkout's
``claude_multi`` package in ``lib/python3.14/site-packages`` (the
installer runs its safety checks from the bundle's own package). The
package's ``release-trust/allowed_signers`` can carry a test signer: that
is the installed trust an update of the fake installation verifies with.
``SHA256SUMS`` is signed with a throwaway key that ``ssh-keygen`` creates
per test class (no key is stored in the tree). Archives are deterministic
(sorted entries, uid 0, mtime 0). The release ``MANIFEST.json`` names a
release key and the release locations as a release build writes them
(``release_signers=0`` or ``location=None`` describe a test build).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import unittest
from functools import lru_cache
from pathlib import Path

from _layout import PACKAGE_DIR

NAMESPACE = "claude-multi-release"
PRINCIPAL = "release@claude-multi"
SSH_KEYGEN = shutil.which("ssh-keygen")
# Set (to 1) where the release integration tests must run, as in the Nix
# sandbox: there a missing ssh-keygen fails them instead of skipping them.
REQUIRE_RELEASE_ENV = "CLAUDE_MULTI_TEST_REQUIRE_RELEASE"


# Where a fake release says its installations look for updates.
LOCATION = {"base_url": "https://example.invalid/releases/download/v{version}",
            "latest_url": "https://example.invalid/releases/latest/download"}


def needs_ssh_keygen(reason: str = "BOUNDARY: ssh-keygen is not installed"):
    """Skip a release integration test (or class) without ssh-keygen,
    unless ``CLAUDE_MULTI_TEST_REQUIRE_RELEASE=1`` requires it: then every
    such test fails, naming the missing tool."""

    if SSH_KEYGEN:
        return lambda item: item
    if os.environ.get(REQUIRE_RELEASE_ENV) != "1":
        return unittest.skip(reason)
    message = f"{REQUIRE_RELEASE_ENV}=1 requires this release test, but {reason.removeprefix('BOUNDARY: ')}"

    def required(item):
        if isinstance(item, type):
            def refuse(cls) -> None:  # the class fails as one error; its tearDownClass never runs
                raise AssertionError(message)

            item.setUpClass = classmethod(refuse)
            return item

        def test(self, *_args, **_kwargs) -> None:
            self.fail(message)

        test.__name__ = item.__name__
        test.__doc__ = item.__doc__
        return test

    return required

LAUNCHER = """#!/bin/sh
printf '%s\\n' "{name} {version} $*" >>"$HOME/launcher.log"
echo "{name} {version}"
"""


def ssh_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C"}


def make_key(directory: Path, name: str = "release") -> Path:
    key = directory / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{name} test key", "-f", str(key)],
                   check=True, capture_output=True, env=ssh_env(directory), timeout=60, stdin=subprocess.DEVNULL)
    return key


def signers_line(key: Path, options: str | None = None) -> str:
    key_type, encoded = Path(f"{key}.pub").read_text().split()[:2]
    head = PRINCIPAL if options is None else f"{PRINCIPAL} {options}"
    return f"{head} {key_type} {encoded}\n"


def _add(tar: tarfile.TarFile, name: str, data: bytes | None, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = mode
    if data is None:
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    else:
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


SITE = "lib/python3.14/site-packages"
TRUST = "claude_multi/data/release-trust/allowed_signers"
REAL_LAUNCHER = """#!/bin/sh
# {name}: a fake-release launcher that runs this bundle's package.
root=$(CDPATH='' cd "${{0%/*}}/.." && pwd)
site="$root/{site}"
CLAUDE_MULTI_CHANNEL=bundle
CLAUDE_MULTI_ASSETS="$site/claude_multi/data"
CLAUDE_MULTI_HOOK_COMMAND="$root/bin/claude-multi"
CLAUDE_MULTI_PROXY_BIN="$root/libexec/claude-multi/cli-proxy-api"
export CLAUDE_MULTI_CHANNEL CLAUDE_MULTI_ASSETS CLAUDE_MULTI_HOOK_COMMAND CLAUDE_MULTI_PROXY_BIN
unset CLAUDE_MULTI_PROXY_PATCHES
exec "$root/runtime/python/bin/python3" -I -B -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from claude_multi.entrypoints import main; raise SystemExit(main())' "$site" {name} "$@"
"""


@lru_cache(maxsize=1)
def package_files() -> tuple[tuple[str, bytes, int], ...]:
    """This checkout's package as bundle entries (relative to the site directory)."""

    entries = []
    for path in sorted(PACKAGE_DIR.rglob("*")):
        relative = path.relative_to(PACKAGE_DIR.parent)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_dir():
            entries.append((relative.as_posix(), b"", -1))
        elif path.is_file():
            entries.append((relative.as_posix(), path.read_bytes(), 0o644))
    return tuple(entries)


def bundle_files(version: str, target: str, *, state_format: int = 4, gateway: str = "0" * 64,
                 claude: str = "0.0.0-test", release_date: str = "2026-10-15",
                 python: str | None = None, trust: str | None = None, real_launchers: bool = False,
                 gateway_binary: bytes | None = None, launcher: str | None = None,
                 overrides: dict[str, bytes] | None = None) -> dict[str, tuple[bytes | None, int]]:
    top = f"claude-multi-{version}-{target}"
    manifest = {
        "format": 1, "name": "claude-multi", "version": version, "target": target,
        "release_date": release_date, "state_format": state_format,
        "claude_code": {"version": claude}, "gateway": {"sha256": gateway},
        "python": {"version": "3.14.8"},
    }
    interpreter = python or sys.executable
    files: dict[str, tuple[bytes | None, int]] = {
        top: (None, 0o755),
        f"{top}/MANIFEST.json": (json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n", 0o644),
        f"{top}/bin": (None, 0o755),
        f"{top}/runtime": (None, 0o755),
        f"{top}/runtime/python": (None, 0o755),
        f"{top}/runtime/python/bin": (None, 0o755),
        f"{top}/runtime/python/bin/python3": (f"#!/bin/sh\nexec '{interpreter}' \"$@\"\n".encode(), 0o755),
        f"{top}/libexec": (None, 0o755),
        f"{top}/libexec/claude-multi": (None, 0o755),
        f"{top}/libexec/claude-multi/cli-proxy-api": (gateway_binary or f"gateway {gateway}\n".encode(), 0o755),
    }
    for name in ("claude-multi", "claude-multi-proxy"):
        if launcher is not None:
            text = launcher.format(name=name, version=version)
        elif real_launchers:
            text = REAL_LAUNCHER.format(name=name, site=SITE)
        else:
            text = LAUNCHER.format(name=name, version=version)
        files[f"{top}/bin/{name}"] = (text.encode(), 0o755)
    parts = SITE.split("/")
    for index in range(1, len(parts) + 1):
        files[f"{top}/{'/'.join(parts[:index])}"] = (None, 0o755)
    for relative, data, mode in package_files():
        files[f"{top}/{SITE}/{relative}"] = (None, 0o755) if mode < 0 else (data, mode)
    files[f"{top}/{SITE}/claude_multi/data/version.json"] = (_version_document(version), 0o644)
    if trust is not None:
        files[f"{top}/{SITE}/{TRUST}"] = (trust.encode(), 0o644)
    for relative, data in (overrides or {}).items():
        files[f"{top}/{SITE}/{relative}"] = (data, 0o644)
    return files


def _version_document(version: str) -> bytes:
    document = json.loads((PACKAGE_DIR / "data" / "version.json").read_text())
    document["launcher_version"] = version
    return (json.dumps(document, indent=2) + "\n").encode()


def bundle_bytes(files: dict[str, tuple[bytes | None, int]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            data, mode = files[name]
            _add(tar, name, data, mode)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="", compresslevel=1) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


class FakeRelease:
    """One release directory: bundles, MANIFEST.json, SHA256SUMS(.sshsig)."""

    def __init__(self, directory: Path, version: str, *, targets: tuple[str, ...] = ("linux-x86_64",),
                 state_format: int = 4, gateway: str = "0" * 64, claude: str = "0.0.0-test",
                 python: str | None = None, trust: str | None = None, real_launchers: bool = False,
                 gateway_binary: bytes | None = None, release_date: str = "2026-10-15",
                 claude_platforms: dict | None = None, overrides: dict[str, bytes] | None = None,
                 launcher: str | None = None, release_signers: int = 1,
                 location: dict | None = LOCATION) -> None:
        self.dir = directory
        self.version = version
        directory.mkdir(parents=True, exist_ok=True)
        assets = {}
        for target in targets:
            name = f"claude-multi-{version}-{target}.tar.gz"
            files = bundle_files(version, target, state_format=state_format, gateway=gateway, claude=claude,
                                 python=python, trust=trust, real_launchers=real_launchers,
                                 gateway_binary=gateway_binary, release_date=release_date, overrides=overrides,
                                 launcher=launcher)
            data = bundle_bytes(files)
            (directory / name).write_bytes(data)
            raw_tar = gzip.decompress(data)
            assets[name] = {"target": target, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "tar_sha256": hashlib.sha256(raw_tar).hexdigest(), "gateway_sha256": gateway}
        claude_record: dict = {"version": claude}
        if claude_platforms is not None:
            claude_record["platforms"] = claude_platforms
        self.manifest = {
            "format": 1, "name": "claude-multi", "version": version, "release_date": release_date,
            "state_format": state_format, "claude_code": claude_record, "assets": assets,
            "release_trust": {"sha256": hashlib.sha256((trust or "").encode()).hexdigest(),
                              "signers": release_signers},
            "release": dict(location) if location is not None else {"base_url": None, "latest_url": None},
        }
        (directory / "MANIFEST.json").write_text(json.dumps(self.manifest, indent=2, sort_keys=True) + "\n")
        self.write_sums()

    def asset(self, target: str = "linux-x86_64") -> Path:
        return self.dir / f"claude-multi-{self.version}-{target}.tar.gz"

    def write_sums(self) -> None:
        """SHA256SUMS over the release members present: the archives,
        MANIFEST.json and, once copied in, the installers."""

        lines = []
        for path in sorted(self.dir.iterdir()):
            if path.name.endswith(".tar.gz") or path.name in ("MANIFEST.json", "install.sh", "install.ps1"):
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
        (self.dir / "SHA256SUMS").write_text("".join(lines))

    def sign(self, key: Path, namespace: str = NAMESPACE) -> None:
        signature = self.dir / "SHA256SUMS.sshsig"
        if signature.exists():
            signature.unlink()
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace, str(self.dir / "SHA256SUMS")],
                       check=True, capture_output=True, env=ssh_env(key.parent), timeout=60, stdin=subprocess.DEVNULL)
        (self.dir / "SHA256SUMS.sig").rename(signature)  # ssh-keygen writes <file>.sig
