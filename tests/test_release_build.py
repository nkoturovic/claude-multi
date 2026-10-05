"""``tools/build.py bundle|release``: the release bundles, end to end.

The real consumer runs for all four targets over this repository's own
inputs (the package, its documents and licence, packaging/product.json, the
gateway recipe, notices and SBOMs, the installers) with two stand-ins: the
gateway dist holds synthetic executables (static ELF and Mach-O with the
recipe's patch markers, recorded in a BUILD.json that names the real series)
and the pinned runtimes are synthetic archives whose interpreter is the one
running the tests (a runtime manifest pinning its version). Nothing is
downloaded; no gateway or provider is contacted.

Covered: the layout, the deterministic archives and manifests, the bytecode
(the launcher's and the runtime's), the installers' release metadata, the
import check of the host target's bundle, the refusals of inconsistent
gateway outputs and product descriptions, compare and repro, and the
generated launchers end to end (launcher, launcher process, gateway exec)
with a hostile inherited environment.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import marshal
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _layout import (BUILD_TOOL, GATEWAY_CONTRACT, LICENSES_DIR, REPO_ROOT, RESOURCES_ROOT, SBOM_DIR, UPSTREAM_JSON,
                     pyproject)
from _release import PRODUCT_JSON, RUNTIME_LICENSES, RUNTIMES_JSON, keyless_repo, load_tool, requires_release_tree

# POSIX launchers and archives (tools/test.py reads this marker).
PLATFORMS = ("linux", "darwin")
EPOCH = 1700000000
PYTHON_VERSION = platform.python_version()
MINOR = ".".join(PYTHON_VERSION.split(".")[:2])
SITE = f"lib/python{MINOR}/site-packages"
STDLIB = f"runtime/python/lib/python{MINOR}"
TARGETS = ("darwin-arm64", "darwin-x86_64", "linux-aarch64", "linux-x86_64")


def build_tool():
    return load_tool(BUILD_TOOL, "build")


# ------------------------------------------------------------------ synthetic inputs


def elf(machine: int, payload: bytes) -> bytes:
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
    header = struct.pack("<HHIQQQIHHHHHH", 2, machine, 1, 0x400000, 64, 0, 0, 64, 56, 1, 64, 0, 0)
    return ident + header + struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 0, 0, 0x1000) + payload


def macho(cpu: int, payload: bytes, *, signed: bool) -> bytes:
    name = b"/usr/lib/libSystem.B.dylib\0"
    name += b"\0" * (-len(name) % 8)
    commands = [struct.pack("<IIIIII", 0xC, 24 + len(name), 24, 2, 0, 0) + name]
    blob = b""
    if signed:
        directory = struct.pack(">IIII", 0xFADE0C02, 44, 0x20400, 0x00020002) + b"\0" * 28
        blob = struct.pack(">IIIII", 0xFADE0CC0, 20 + len(directory), 1, 0, 20) + directory
    size = sum(len(command) for command in commands) + (16 if signed else 0)
    if signed:
        commands.append(struct.pack("<IIII", 0x1D, 16, 32 + size, len(blob)))
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, 2, len(commands), size, 0, 0)
    return header + b"".join(commands) + blob + payload


def runtime_archive(target: str) -> bytes:
    """A python-build-standalone-shaped archive whose interpreter runs the
    test interpreter (its path written in two quoted halves, so the file
    names no store path even where the interpreter lives in one)."""

    executable = sys.executable
    half = len(executable) // 2
    script = f"#!/bin/sh\nexec '{executable[:half]}''{executable[half:]}' \"$@\"\n".encode()
    entries: list[tuple[str, str, object]] = [
        (f"python/bin/python{MINOR}", "file", script),
        ("python/bin/python3", "symlink", f"python{MINOR}"),
        ("python/bin/pip3", "file", b"pip"),
        (f"python/lib/python{MINOR}/LICENSE.txt", "file", f"CPython licence ({target})\n".encode()),
        (f"python/lib/python{MINOR}/stdmod.py", "file", b"VALUE = 1\n"),
        (f"python/lib/python{MINOR}/sub/__init__.py", "file", b""),
        (f"python/lib/python{MINOR}/ensurepip/__init__.py", "file", b""),
        ("python/lib/itcl4.3.8/itcl.tcl", "file", b"itcl"),
        ("python/lib/thread3.0.6/thread.tcl", "file", b"thread"),
        ("python/share/man/man1/python3.1", "file", b"man"),
    ]
    if target.startswith("linux"):
        entries.append(("python/share/terminfo/x/xterm", "file", b"terminfo"))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, str(payload)
                tar.addfile(info)
            else:
                data = payload if isinstance(payload, bytes) else str(payload).encode()
                info.size, info.mode = len(data), 0o755 if "/bin/" in name else 0o644
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Fixture:
    """The synthetic runtimes (manifest and cache) and gateway dist."""

    def __init__(self, root: Path) -> None:
        tool = build_tool()
        self.root = root
        self.cache = root / "cache"
        shipped = json.loads(RUNTIMES_JSON.read_text())
        shipped["python"] = PYTHON_VERSION
        for target, entry in shipped["targets"].items():
            data = runtime_archive(target)
            entry["filename"] = f"cpython-{PYTHON_VERSION}+{shipped['release']}-{entry['triple']}-install_only_stripped.tar.gz"
            entry["size"], entry["sha256"] = len(data), hashlib.sha256(data).hexdigest()
            stored = self.cache / "python-runtimes" / entry["sha256"] / entry["filename"]
            stored.parent.mkdir(parents=True)
            stored.write_bytes(data)
        shipped["prune"] = sorted(set(shipped["prune"]) | {f"lib/python{MINOR}/ensurepip"})
        self.runtimes = root / "python-runtimes.json"
        self.runtimes.write_text(json.dumps(shipped, indent=2))
        # The checked-in licence inventory, describing the synthetic runtimes.
        self.runtime_licenses = root / "runtime-licenses"
        shutil.copytree(RUNTIME_LICENSES, self.runtime_licenses)
        inventory = json.loads((self.runtime_licenses / "inventory.json").read_text())
        inventory["python"] = PYTHON_VERSION
        for target, entry in shipped["targets"].items():
            inventory["targets"][target]["install_only"] = {"filename": entry["filename"], "sha256": entry["sha256"]}
        (self.runtime_licenses / "inventory.json").write_text(json.dumps(inventory, indent=2))
        self.dist = root / "dist-gateway"
        self.write_gateway_dist(tool)

    def write_gateway_dist(self, tool) -> None:
        doc = json.loads(UPSTREAM_JSON.read_text())
        admitted = [entry for entry in doc["series"] if entry["admitted"]]
        payload = b"".join(marker["text"].encode() + b"\0" for marker in doc["markers"])
        binaries = {"linux-amd64": elf(62, payload), "linux-arm64": elf(183, payload),
                    "darwin-arm64": macho(0x0100000C, payload, signed=True),
                    "darwin-amd64": macho(0x01000007, payload, signed=False)}
        targets = {}
        for name, data in binaries.items():
            (self.dist / name).mkdir(parents=True)
            path = self.dist / name / "cli-proxy-api"
            path.write_bytes(data)
            path.chmod(0o755)
            shutil.copyfile(SBOM_DIR / f"{name}.cdx.json", self.dist / name / "cli-proxy-api.cdx.json")
            targets[name] = {"file": f"{name}/cli-proxy-api", "sha256": hashlib.sha256(data).hexdigest(),
                             "size": len(data), "shipped": True}
        record = {
            "format": 1,
            "upstream": {key: doc["upstream"][key] for key in ("name", "repository", "tag", "version", "commit")},
            "toolchain": {"version": doc["toolchain"]["version"], "host": "linux-amd64"},
            "source": {"tree_sha256": doc["source"]["tree_sha256"], "archive_sha256": doc["source"]["archive_sha256"]},
            "vendor": {"tree_sha256": doc["vendor"]["tree_sha256"]},
            "series": [{"basename": entry["basename"], "sha256": entry["sha256"]} for entry in admitted],
            "admitted_series": True,
            "post_apply_tree_sha256": "0" * 64,
            "build": {"env": doc["build"]["env"], "flags": doc["build"]["flags"], "ldflags": doc["build"]["ldflags"],
                      "main_package": doc["upstream"]["main_package"]},
            "targets": targets,
        }
        (self.dist / "BUILD.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        shutil.copyfile(GATEWAY_CONTRACT, self.dist / "gateway-contract.json")
        notices = tool.read_notice_tree(LICENSES_DIR)
        tool.write_notice_tree(self.dist / "licenses", notices)
        (self.dist / "licenses" / tool.THIRD_PARTY_FILE).write_bytes(tool.render_third_party_notices(notices))

    def keyless_tree(self) -> Path:
        """A copy of the source tree whose packaged release trust names no
        key (a release of it is refused unless it is a test build)."""

        tree = self.root / "keyless-tree"
        if not tree.exists():
            for part in ("src", "gateway", "packaging", "docs"):
                shutil.copytree(REPO_ROOT / part, tree / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("LICENSE", "pyproject.toml"):
                shutil.copyfile(REPO_ROOT / name, tree / name)
            keyless_repo(tree)
        return tree

    def argv(self, command: str, out: Path, work: Path, *extra: str, test_build: bool = True) -> list[str]:
        """``build.py COMMAND`` over the fixture. A release over the fixture
        is a test build: its gateway and runtimes are synthetic."""

        flag = ["--test-build"] if command == "release" and test_build else []
        return [command, *extra, *flag, "--out", str(out), "--work", str(work), "--gateway-dist", str(self.dist),
                "--runtimes", str(self.runtimes), "--runtime-licenses", str(self.runtime_licenses),
                "--cache", str(self.cache), "--offline",
                "--python", sys.executable, "--source-date-epoch", str(EPOCH)]


def run_tool(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = build_tool().main(argv)
        except SystemExit as exc:  # argparse usage errors
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def members(archive: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getmembers()


def read_member(archive: Path, name: str) -> bytes:
    with tarfile.open(archive, "r:gz") as tar:
        handle = tar.extractfile(name)
        assert handle is not None, name
        return handle.read()


@requires_release_tree
class ReleaseBuildTests(unittest.TestCase):
    """One release over the fixture, built in setUpClass and inspected."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-build-"))
        cls.fixture = Fixture(cls.tmp)
        cls.out = cls.tmp / "out"
        code, stdout, stderr = run_tool(cls.fixture.argv("release", cls.out, cls.tmp / "work"))
        if code != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise AssertionError(f"release build failed:\n{stderr}")
        cls.summary = json.loads(stdout)
        cls.manifest = json.loads((cls.out / "MANIFEST.json").read_text())
        cls.version = json.loads((RESOURCES_ROOT / "version.json").read_text())["launcher_version"]
        cls.host = load_tool(REPO_ROOT / "tools" / "_build" / "python_runtime.py", "python_runtime").host_target()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def archive(self, target: str) -> Path:
        return self.out / f"claude-multi-{self.version}-{target}.tar.gz"

    def top(self, target: str) -> str:
        return f"claude-multi-{self.version}-{target}"

    def test_the_release_directory(self) -> None:
        names = sorted(path.name for path in self.out.iterdir())
        archives = [f"claude-multi-{self.version}-{target}.tar.gz" for target in TARGETS]
        self.assertEqual(names, sorted(["MANIFEST.json", "SHA256SUMS", "install.sh", "install.ps1", *archives]))
        sums = (self.out / "SHA256SUMS").read_text().splitlines()
        listed = {line.split("  ")[1]: line.split("  ")[0] for line in sums}
        # Every release member, the installers included: each is checked by
        # the signed checksums before it runs.
        self.assertEqual(sorted(listed), sorted(["MANIFEST.json", "install.sh", "install.ps1", *archives]))
        self.assertEqual([line.split("  ")[1] for line in sums], sorted(listed))
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((self.out / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(self.manifest["version"], self.version)
        self.assertEqual(self.manifest["release_date"], "2023-11-14")
        # Every signed platform build of the pinned Claude Code, so an update
        # plan can size its download before fetching a bundle.
        contract = json.loads((RESOURCES_ROOT / "catalog" / "native-contract.json").read_text())
        expected_platforms = {name: {"sha256": record["sha256"], "size": record["size"]}
                              for name, record in contract["verified"][0]["platforms"].items()}
        self.assertEqual(self.manifest["claude_code"]["platforms"], expected_platforms)
        self.assertEqual(self.manifest["source_date_epoch"], EPOCH)
        self.assertEqual(sorted(self.manifest["assets"]), sorted(archives))
        for name, asset in self.manifest["assets"].items():
            with self.subTest(name):
                path = self.out / name
                self.assertEqual(name, f"claude-multi-{self.version}-{asset['target']}.tar.gz")
                self.assertEqual(asset["size"], path.stat().st_size)
                self.assertEqual(asset["sha256"], listed[name])
                self.assertEqual(asset["tar_sha256"], hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest())
        self.assertTrue(self.summary["release_key"])  # the production key
        self.assertTrue(self.summary["test_build"])  # labelled: the fixture's gateway and runtimes
        # Where the release's installations look for updates: product.json's locations.
        product = json.loads(PRODUCT_JSON.read_text())
        self.assertEqual(self.manifest["release"], {"base_url": product["release_base_url"],
                                                    "latest_url": product["release_latest_url"]})
        self.assertEqual(self.manifest["release_trust"]["signers"], 1)

    def test_archives_are_normalised(self) -> None:
        for target in TARGETS:
            with self.subTest(target):
                raw = self.archive(target).read_bytes()
                self.assertEqual(raw[:4], b"\x1f\x8b\x08\x00")  # gzip, no FNAME flag
                self.assertEqual(raw[4:8], b"\0\0\0\0")  # mtime 0
                entries = members(self.archive(target))
                top = self.top(target)
                self.assertEqual(entries[0].name, top)
                self.assertTrue(all(e.name == top or e.name.startswith(top + "/") for e in entries))
                names = [tuple(e.name.split("/")) for e in entries]
                self.assertEqual(names, sorted(names))
                for entry in entries:
                    self.assertEqual((entry.uid, entry.gid, entry.uname, entry.gname, entry.mtime),
                                     (0, 0, "", "", EPOCH), entry.name)
                    self.assertFalse(entry.islnk(), entry.name)
                    if entry.isdir():
                        self.assertEqual(entry.mode, 0o755, entry.name)
                    elif entry.isfile():
                        self.assertIn(entry.mode, (0o644, 0o755), entry.name)
                    else:
                        self.assertTrue(entry.issym(), entry.name)

    def test_bundle_layout(self) -> None:
        for target in TARGETS:
            with self.subTest(target):
                top = self.top(target)
                names = {entry.name.removeprefix(top + "/"): entry for entry in members(self.archive(target))}
                for required in ("MANIFEST.json", "bin/claude-multi", "bin/claude-multi-proxy",
                                 "runtime/python/bin/python3", f"{SITE}/claude_multi/__init__.py",
                                 f"{SITE}/claude_multi/data/version.json",
                                 f"{SITE}/claude_multi/data/release-trust/allowed_signers",
                                 f"{SITE}/claude_multi/data/gateway-contract.json",
                                 f"{SITE}/claude_multi/data/registry/models.json",
                                 "libexec/claude-multi/cli-proxy-api",
                                 "share/claude-multi/USAGE.md", "share/claude-multi/CHEATSHEET.md",
                                 "share/claude-multi/STANDALONE.md", "share/licenses/claude-multi/LICENSE",
                                 "share/licenses/cli-proxy-api/THIRD_PARTY_NOTICES.txt",
                                 "share/licenses/cli-proxy-api/modules.json",
                                 "share/licenses/cli-proxy-api/CLIProxyAPI/MODIFICATIONS.txt",
                                 "share/licenses/python/LICENSE.txt", "share/sbom/claude-multi.cdx.json",
                                 "share/sbom/cli-proxy-api.cdx.json", "share/sbom/python.cdx.json"):
                    self.assertIn(required, names)
                for name in ("bin/claude-multi", "bin/claude-multi-proxy", "libexec/claude-multi/cli-proxy-api"):
                    self.assertEqual(names[name].mode, 0o755)
                # The launchers are exactly product.json's: never the contributor's command
                # or the one-model alias (claude-multi direct).
                self.assertEqual(sorted(name for name in names if name.startswith("bin/")),
                                 ["bin/claude-multi", "bin/claude-multi-proxy"])
                for pruned in ("runtime/python/lib/itcl4.3.8", "runtime/python/lib/thread3.0.6",
                               "runtime/python/bin/pip3", "runtime/python/share/man",
                               f"{STDLIB}/ensurepip"):
                    self.assertNotIn(pruned, names)
                self.assertEqual(target.startswith("linux"), "runtime/python/share/terminfo/x/xterm" in names)
                self.assertFalse([name for name in names if name.endswith((".pyo",)) or "/tests/" in name])
                gateway = read_member(self.archive(target), f"{top}/libexec/claude-multi/cli-proxy-api")
                gateway_target = json.loads(PRODUCT_JSON.read_text())["targets"][target]["gateway"]
                self.assertEqual(gateway, (self.fixture.dist / gateway_target / "cli-proxy-api").read_bytes())
                self.assertEqual(read_member(self.archive(target), f"{top}/share/sbom/cli-proxy-api.cdx.json"),
                                 (SBOM_DIR / f"{gateway_target}.cdx.json").read_bytes())
                self.assertEqual(read_member(self.archive(target), f"{top}/share/licenses/claude-multi/LICENSE"),
                                 (REPO_ROOT / "LICENSE").read_bytes())

    def test_runtime_licences_notices_and_sbom(self) -> None:
        inventory = json.loads((RUNTIME_LICENSES / "inventory.json").read_text())
        for target in TARGETS:
            with self.subTest(target):
                top = self.top(target)
                entry = inventory["targets"][target]
                texts = sorted({text for library in entry["libraries"] for text in library["texts"]})
                shipped = sorted(entry.name.removeprefix(f"{top}/share/licenses/python/")
                                 for entry in members(self.archive(target))
                                 if entry.name.startswith(f"{top}/share/licenses/python/"))
                self.assertEqual(shipped, sorted(["LICENSE.txt", "INVENTORY.json", "THIRD_PARTY_NOTICES.txt", *texts]))
                for name in texts:
                    self.assertEqual(read_member(self.archive(target), f"{top}/share/licenses/python/{name}"),
                                     (RUNTIME_LICENSES / name).read_bytes())
                notices = read_member(self.archive(target), f"{top}/share/licenses/python/THIRD_PARTY_NOTICES.txt")
                for library in entry["libraries"]:
                    self.assertIn(f"{library['name']} {library['version']}".encode(), notices)
                self.assertIn(b"_dbm:", notices)  # removed, with its reason
                described = json.loads(read_member(self.archive(target), f"{top}/share/licenses/python/INVENTORY.json"))
                self.assertEqual((described["target"], described["libraries"]), (target, entry["libraries"]))
                bom = json.loads(read_member(self.archive(target), f"{top}/share/sbom/python.cdx.json"))
                self.assertEqual(sorted(item["name"] for item in bom["components"]),
                                 sorted(library["name"] for library in entry["libraries"]))
                self.assertEqual(bom["dependencies"][0]["dependsOn"],
                                 [item["bom-ref"] for item in bom["components"]])
                properties = {(item["name"], item["value"]) for item in bom["metadata"]["component"]["properties"]}
                self.assertIn(("claude-multi:runtime:removed-extension", "_dbm"), properties)
                self.assertEqual([item["license"]["id"] for item in bom["metadata"]["component"]["licenses"]],
                                 entry["runtime"]["spdx"])
                for component in bom["components"]:
                    for licence in component["licenses"]:
                        self.assertFalse(str(licence["license"].get("id", "")).startswith(("GPL-", "AGPL-",
                                                                                          "Sleepycat")))

    def test_bytecode(self) -> None:
        tag = sys.implementation.cache_tag
        launcher_sets = {}
        for target in TARGETS:
            with self.subTest(target):
                top = self.top(target)
                entries = {entry.name.removeprefix(top + "/") for entry in members(self.archive(target))}
                sources = sorted(name for name in entries if name.startswith(f"{SITE}/claude_multi/")
                                 and name.endswith(".py"))
                self.assertGreater(len(sources), 50)
                pycs = {}
                for source in sources:
                    directory, _, file = source.rpartition("/")
                    pyc = f"{directory}/__pycache__/{file[:-3]}.{tag}.pyc"
                    self.assertIn(pyc, entries)
                    pycs[pyc] = read_member(self.archive(target), f"{top}/{pyc}")
                launcher_sets[target] = hashlib.sha256(json.dumps(
                    {name: hashlib.sha256(data).hexdigest() for name, data in pycs.items()}, sort_keys=True).encode()
                ).hexdigest()
                sample = pycs[f"{SITE}/claude_multi/__pycache__/__init__.{tag}.pyc"]
                self.assertEqual(int.from_bytes(sample[4:8], "little"), 0b01)  # unchecked hash
                self.assertEqual(marshal.loads(sample[16:]).co_filename, f"{SITE}/claude_multi/__init__.py")
                stdlib = read_member(self.archive(target), f"{top}/{STDLIB}/__pycache__/stdmod.{tag}.pyc")
                self.assertEqual(int.from_bytes(stdlib[4:8], "little"), 0b01)
                self.assertEqual(marshal.loads(stdlib[16:]).co_filename, f"{STDLIB}/stdmod.py")
        self.assertEqual(len(set(launcher_sets.values())), 1, "every target ships the same launcher bytecode")

    def test_bundle_manifests(self) -> None:
        product = json.loads(PRODUCT_JSON.read_text())
        contract = json.loads((RESOURCES_ROOT / "catalog" / "native-contract.json").read_text())
        pin = max(contract["verified"], key=lambda item: tuple(int(p) for p in item["version"].split(".")))
        doc = json.loads(UPSTREAM_JSON.read_text())
        series = [f"{entry['basename']}@{entry['sha256']}" for entry in doc["series"] if entry["admitted"]]
        bundle = load_tool(REPO_ROOT / "tools" / "_build" / "bundle.py", "bundle")
        from claude_multi import release_update, self_update

        for target in TARGETS:
            with self.subTest(target):
                top = self.top(target)
                raw = read_member(self.archive(target), f"{top}/MANIFEST.json")
                manifest = json.loads(raw)
                asset = self.manifest["assets"][f"{top}.tar.gz"]
                self.assertEqual(hashlib.sha256(raw).hexdigest(), asset["manifest_sha256"])
                self.assertEqual((manifest["name"], manifest["version"], manifest["target"]),
                                 ("claude-multi", self.version, target))
                platform_name = product["targets"][target]["claude_code"]
                self.assertEqual(manifest["claude_code"], {"version": pin["version"], "platform": platform_name,
                                                           **pin["platforms"][platform_name]})
                self.assertEqual(manifest["gateway"]["patches"], series)
                self.assertEqual(manifest["gateway"]["sha256"], asset["gateway_sha256"])
                self.assertEqual(manifest["gateway"]["target"], product["targets"][target]["gateway"])
                self.assertEqual(manifest["python"]["version"], PYTHON_VERSION)
                self.assertIn("lib/itcl4.3.8", manifest["python"]["pruned"])
                self.assertEqual(manifest["launcher"]["bytecode_sha256"], self.manifest["launcher"]["bytecode_sha256"])
                self.assertEqual(manifest["launcher"]["launchers"], product["launchers"])
                self.assertEqual(manifest["release_trust"]["signers"], 1)
                # Where the installed bundle looks for updates: product.json's locations, the
                # download location kept with its {version} placeholder (filled per version).
                self.assertEqual(manifest["release"], {"base_url": product["release_base_url"],
                                                       "latest_url": product["release_latest_url"]})
                self.assertIn("{version}", manifest["release"]["base_url"])
                self.assertEqual(manifest["state_format"], self.manifest["state_format"])
                with tempfile.TemporaryDirectory() as unpacked:
                    with tarfile.open(self.archive(target), "r:gz") as tar:
                        tar.extractall(unpacked, filter="data")
                    root = Path(unpacked) / top
                    self.assertEqual(bundle.content_digest(root, exclude=("MANIFEST.json",)),
                                     (manifest["content"]["sha256"], manifest["content"]["files"]))
                    installed = self_update.read_bundle_manifest(root, version=self.version, target=target)
                    self.assertEqual(installed.gateway_sha256, asset["gateway_sha256"])
                    self.assertEqual(installed.claude_code, pin["version"])
                    # claude-multi update (and update --check) of this installation: the
                    # latest release's MANIFEST from the latest location, a version's files
                    # from the download location with {version} filled in.
                    base, latest = release_update.release_location(root)
                    self.assertEqual((base, latest), (product["release_base_url"], product["release_latest_url"]))
                    transport = release_update.HttpsTransport(base, latest, environ={})
                    self.assertEqual(transport.url("MANIFEST.json", None),
                                     product["release_latest_url"] + "/MANIFEST.json")
                    self.assertEqual(transport.url("SHA256SUMS", "1.2.0"),
                                     product["release_base_url"].replace("v{version}", "v1.2.0") + "/SHA256SUMS")

    def test_host_bundle_was_run_before_archiving(self) -> None:
        checked = {name: asset["import_check"] for name, asset in self.summary["assets"].items()}
        host = f"claude-multi-{self.version}-{self.host}.tar.gz"
        self.assertIsNotNone(checked.pop(host))
        self.assertEqual(set(checked.values()), {None})
        report = self.summary["assets"][host]["import_check"]
        self.assertTrue(report["version"].startswith(f"claude-multi {self.version} (catalog "), report["version"])
        self.assertGreater(report["launcher_modules"], 50)
        self.assertGreater(report["stdlib_modules"], 10)

    def test_installers_carry_the_release(self) -> None:
        shell = (self.out / "install.sh").read_text()
        # The installer carries the bundles' checksums only (the lines of
        # SHA256SUMS that name an archive), so SHA256SUMS can list it.
        sums = "\n".join(line for line in (self.out / "SHA256SUMS").read_text().splitlines()
                         if line.endswith(".tar.gz"))
        self.assertEqual(len(sums.splitlines()), len(TARGETS))
        self.assertIn(f"\nRELEASE_VERSION='{self.version}'\n", shell)
        self.assertIn(f"\nRELEASE_SUMS='{sums}'\n", shell)
        listed = dict(reversed(line.split("  ")) for line in (self.out / "SHA256SUMS").read_text().splitlines())
        for name in ("install.sh", "install.ps1"):
            self.assertEqual(listed[name], hashlib.sha256((self.out / name).read_bytes()).hexdigest(), name)
        # The production key line of the packaged trust.
        signers = [line for line in (RESOURCES_ROOT / "release-trust" / "allowed_signers").read_text().splitlines()
                   if line.strip() and not line.startswith("#")]
        self.assertEqual(len(signers), 1)
        self.assertIn(f"\nRELEASE_SIGNERS='{signers[0]}'\n", shell)
        base = json.loads(PRODUCT_JSON.read_text())["release_base_url"]
        self.assertIn(f"\nRELEASE_BASE_URL='{base}'\n", shell)  # install.sh fills in {version} itself
        self.assertEqual(shell.replace(f"RELEASE_VERSION='{self.version}'", "RELEASE_VERSION=''")
                         .replace(f"RELEASE_SUMS='{sums}'", "RELEASE_SUMS=''")
                         .replace(f"RELEASE_SIGNERS='{signers[0]}'", "RELEASE_SIGNERS=''")
                         .replace(f"RELEASE_BASE_URL='{base}'", "RELEASE_BASE_URL=''"),
                         (REPO_ROOT / "packaging" / "install.sh").read_text())
        self.assertTrue(os.access(self.out / "install.sh", os.X_OK))
        powershell = (self.out / "install.ps1").read_text()
        digest = hashlib.sha256(shell.encode()).hexdigest()
        self.assertIn(f"        Version         = '{self.version}'\n", powershell)
        self.assertIn(f"        InstallerSha256 = '{digest}'\n", powershell)
        installer_url = base.replace("{version}", self.version) + "/install.sh"
        self.assertIn(f"        InstallerUrl    = '{installer_url}'\n", powershell)

    def test_compare_reports_content_and_archive_identity(self) -> None:
        other = self.tmp / "compare"
        shutil.copytree(self.out, other)
        code, stdout, _ = run_tool(["release", "compare", "--out", str(self.out), "--other", str(other)])
        self.assertEqual(code, 0)
        report = json.loads(stdout)
        self.assertEqual({key: report[key] for key in ("archives_identical", "gateways_identical", "tar_identical",
                                                        "bytecode_identical")},
                         dict.fromkeys(("archives_identical", "gateways_identical", "tar_identical",
                                        "bytecode_identical"), True))
        # The same tar compressed differently: the contents still match.
        name = f"claude-multi-{self.version}-linux-x86_64.tar.gz"
        tar_bytes = gzip.decompress((other / name).read_bytes())
        (other / name).write_bytes(gzip.compress(tar_bytes, compresslevel=1, mtime=0))
        self._resign(other, name)
        code, stdout, stderr = run_tool(["release", "compare", "--out", str(self.out), "--other", str(other)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual((json.loads(stdout)["content_identical"], json.loads(stdout)["archives_identical"]),
                         (True, False))
        # Other content: refused.
        (other / name).write_bytes(gzip.compress(tar_bytes + b"\0" * 1024, mtime=0))
        self._resign(other, name)
        code, stdout, stderr = run_tool(["release", "compare", "--out", str(self.out), "--other", str(other)])
        self.assertEqual(code, 1)
        self.assertIn("the builds differ", stderr)
        self.assertEqual((json.loads(stdout)["tar_identical"], json.loads(stdout)["gateways_identical"]), (False, True))

    def test_compare_checks_the_checksums_and_installers(self) -> None:
        """The release files beside the bundles are part of the comparison:
        each directory's SHA256SUMS must list its files, both installers must
        be there and carry the release's checksums, and an installer that
        differs otherwise is a difference."""

        def copy(label: str) -> Path:
            other = self.tmp / f"compare-{label}"
            shutil.copytree(self.out, other)
            return other

        def compare(other: Path) -> tuple[int, str, str]:
            return run_tool(["release", "compare", "--out", str(self.out), "--other", str(other)])

        sums = (self.out / "SHA256SUMS").read_text()
        bundles = "\n".join(line for line in sums.splitlines() if line.endswith(".tar.gz"))
        # The reported case: a broken install.sh, no install.ps1, invalid checksums.
        damaged = copy("damaged")
        (damaged / "install.sh").write_text("#!/bin/sh\nexit 71\n")
        (damaged / "install.ps1").unlink()
        (damaged / "SHA256SUMS").write_text("not checksums\n")
        cases = {"not a release checksum list": damaged}
        wrong = copy("wrong-sum")
        (wrong / "SHA256SUMS").write_text(sums.replace(sums[:64], "0" * 64, 1))
        cases["does not match"] = wrong
        unlisted = copy("unlisted")
        (unlisted / "SHA256SUMS").write_text("".join(sums.splitlines(keepends=True)[1:]))
        cases["does not list exactly MANIFEST.json, the release's archives and its installers"] = unlisted
        uninstalled = copy("installers-unlisted")
        (uninstalled / "SHA256SUMS").write_text("".join(line for line in sums.splitlines(keepends=True)
                                                        if " install." not in line))
        cases["does not list exactly MANIFEST.json, the release's archives and its installers "] = uninstalled
        half = copy("half")
        (half / "install.ps1").unlink()
        cases["must hold both installers"] = half
        stale = copy("stale-sums")
        shell = (stale / "install.sh").read_text().replace(bundles, bundles[:-1] + "x")
        (stale / "install.sh").write_text(shell)
        self._relist(stale)
        cases["does not carry the checksums of this release's bundles"] = stale
        unpinned = copy("unpinned")
        (unpinned / "install.ps1").write_text((unpinned / "install.ps1").read_text().replace(
            hashlib.sha256((unpinned / "install.sh").read_bytes()).hexdigest(), "f" * 64))
        self._relist(unpinned)
        cases["does not carry the sha256 of this release's install.sh"] = unpinned
        replaced = copy("replaced-installer")
        (replaced / "install.sh").write_text((replaced / "install.sh").read_text() + "\n")
        cases[f"{replaced / 'install.sh'} does not match"] = replaced
        for needle, other in cases.items():
            with self.subTest(needle):
                code, _, stderr = compare(other)
                self.assertEqual(code, 1, stderr)
                self.assertIn(needle.rstrip(), stderr)
        # A consistent but different installer: a difference, not a gzip encoding.
        edited = copy("edited")
        shell = (edited / "install.sh").read_text().replace("\nset -eu\n", "\nset -eu\nexit 71\n", 1)
        (edited / "install.sh").write_text(shell)
        powershell = (edited / "install.ps1").read_text()
        (edited / "install.ps1").write_text(powershell.replace(
            hashlib.sha256((self.out / "install.sh").read_bytes()).hexdigest(),
            hashlib.sha256(shell.encode()).hexdigest()))
        self._relist(edited)
        code, stdout, stderr = compare(edited)
        self.assertEqual(code, 1, stderr)
        self.assertIn("the builds differ: install.sh", stderr)
        report = json.loads(stdout)
        self.assertEqual((report["installers_identical"], report["tar_identical"], report["manifest_identical"]),
                         (False, True, True))
        # A release without installers is not the same release as one with them.
        bare = copy("bare")
        for name in ("install.sh", "install.ps1"):
            (bare / name).unlink()
        self._relist(bare)
        code, stdout, stderr = compare(bare)
        self.assertEqual(code, 1, stderr)
        self.assertIn("the installers (one build has none)", stderr)
        # The manifest beyond the archive records counts too.
        dated = copy("dated")
        manifest = json.loads((dated / "MANIFEST.json").read_text())
        manifest["release_date"] = "1999-01-01"
        (dated / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        self._rewrite_release_files(dated)
        code, stdout, stderr = compare(dated)
        self.assertEqual(code, 1, stderr)
        self.assertIn("the builds differ: MANIFEST.json", stderr)

    def _resign(self, directory: Path, name: str) -> None:
        """The release files as a build whose ``name`` archive is compressed
        differently writes them: the manifest's record of it, SHA256SUMS and
        the installer fields that carry checksums."""

        manifest = json.loads((directory / "MANIFEST.json").read_text())
        data = (directory / name).read_bytes()
        manifest["assets"][name].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                                        tar_sha256=hashlib.sha256(gzip.decompress(data)).hexdigest())
        (directory / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        self._rewrite_release_files(directory)

    @staticmethod
    def _relist(directory: Path) -> None:
        """SHA256SUMS listing the files ``directory`` holds now: the
        manifest, its archives and whichever installers are there."""

        manifest = json.loads((directory / "MANIFEST.json").read_text())
        names = ["MANIFEST.json", *manifest["assets"],
                 *(name for name in ("install.sh", "install.ps1") if (directory / name).is_file())]
        (directory / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n" for name in sorted(names)))

    @classmethod
    def _rewrite_release_files(cls, directory: Path) -> None:
        manifest = json.loads((directory / "MANIFEST.json").read_text())
        old_sums = "\n".join(line for line in (directory / "SHA256SUMS").read_text().splitlines()
                             if line.endswith(".tar.gz"))
        sums = "\n".join(f"{record['sha256']}  {asset}" for asset, record in sorted(manifest["assets"].items()))
        shell_before = (directory / "install.sh").read_bytes()
        shell = shell_before.decode().replace(f"RELEASE_SUMS='{old_sums}'", f"RELEASE_SUMS='{sums}'")
        (directory / "install.sh").write_text(shell)
        powershell = (directory / "install.ps1").read_text()
        (directory / "install.ps1").write_text(powershell.replace(hashlib.sha256(shell_before).hexdigest(),
                                                                  hashlib.sha256(shell.encode()).hexdigest()))
        cls._relist(directory)

    def test_a_second_build_elsewhere_is_identical(self) -> None:
        out = self.tmp / "second" / "nested" / "out"
        code, _, stderr = run_tool(self.fixture.argv("release", out, self.tmp / "elsewhere" / "w"))
        self.assertEqual(code, 0, stderr)
        for name in sorted(path.name for path in self.out.iterdir()):
            self.assertEqual((out / name).read_bytes(), (self.out / name).read_bytes(), name)


@requires_release_tree
class ReleaseCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture = Fixture(self.tmp)

    def test_bundle_builds_the_named_targets_without_installers(self) -> None:
        out = self.tmp / "out"
        code, stdout, stderr = run_tool(self.fixture.argv("bundle", out, self.tmp / "w", "--target", "darwin-arm64"))
        self.assertEqual(code, 0, stderr)
        version = json.loads(stdout)["version"]
        self.assertEqual(sorted(path.name for path in out.iterdir()),
                         sorted(["MANIFEST.json", "SHA256SUMS", f"claude-multi-{version}-darwin-arm64.tar.gz"]))
        self.assertEqual(list(json.loads((out / "MANIFEST.json").read_text())["assets"]),
                         [f"claude-multi-{version}-darwin-arm64.tar.gz"])

    def test_repro_builds_twice_and_compares(self) -> None:
        code, stdout, stderr = run_tool(["release", "repro", "--scratch", str(self.tmp / "scratch"),
                                         *self.fixture.argv("release", self.tmp / "unused", self.tmp / "w")[1:]])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual((report["content_identical"], report["archives_identical"]), (True, True))

    def test_refusals(self) -> None:
        out = self.tmp / "out"
        cases = [
            (self.fixture.argv("release", out, self.tmp / "w", "--target", "linux-x86_64"), "--target"),
            (self.fixture.argv("bundle", out, self.tmp / "w", "--target", "windows-amd64"), "unknown bundle target"),
        ]
        for argv, needle in cases:
            with self.subTest(needle):
                code, _, stderr = run_tool(argv)
                self.assertNotEqual(code, 0)
                self.assertIn(needle, stderr)
        out.mkdir()
        (out / "leftover").write_text("x")
        code, _, stderr = run_tool(self.fixture.argv("bundle", out, self.tmp / "w"))
        self.assertEqual(code, 1)
        self.assertIn("not an empty directory", stderr)
        code, _, stderr = run_tool([*self.fixture.argv("bundle", self.tmp / "o2", self.tmp / "w")[:-2],
                                    "--source-date-epoch", "-5"])
        self.assertEqual(code, 1)
        self.assertIn("out of range", stderr)
        # A refusal after the output directory was claimed leaves it empty.
        (self.fixture.dist / "darwin-arm64" / "cli-proxy-api.cdx.json").write_text("{}")
        code, _, stderr = run_tool(self.fixture.argv("bundle", self.tmp / "o4", self.tmp / "w", "--target", "all"))
        self.assertEqual(code, 1)
        self.assertIn("the SBOM is not the checked-in", stderr)
        self.assertEqual(list((self.tmp / "o4").iterdir()), [])
        other = json.loads(self.fixture.runtimes.read_text())
        other["python"] = "3.99.0"
        for entry in other["targets"].values():
            entry["filename"] = f"cpython-3.99.0+{other['release']}-{entry['triple']}-install_only_stripped.tar.gz"
        (self.tmp / "other-runtimes.json").write_text(json.dumps(other))
        argv = self.fixture.argv("bundle", self.tmp / "o3", self.tmp / "w")
        argv[argv.index("--runtimes") + 1] = str(self.tmp / "other-runtimes.json")
        code, _, stderr = run_tool(argv)
        self.assertEqual(code, 1)
        self.assertIn("the bundles pin 3.99.0", stderr)

    def test_the_runtime_licences_must_describe_the_shipped_runtime(self) -> None:
        inventory_path = self.fixture.runtime_licenses / "inventory.json"
        pristine = inventory_path.read_text()

        def edit(change) -> None:
            document = json.loads(pristine)
            change(document)
            inventory_path.write_text(json.dumps(document))

        def no_text(document) -> None:
            document["targets"]["linux-x86_64"]["libraries"][0]["texts"] = []

        def other_pin(document) -> None:
            document["targets"]["darwin-arm64"]["install_only"]["sha256"] = "0" * 64

        def copyleft_shipped(document) -> None:
            document["targets"]["linux-aarch64"]["libraries"][0]["spdx"] = ["GPL-3.0-only"]

        cases = {
            "has no licence text": no_text,
            "the pin is": other_pin,
            "strong-copyleft": copyleft_shipped,
        }
        for index, (needle, change) in enumerate(cases.items()):
            with self.subTest(needle):
                edit(change)
                code, _, stderr = run_tool(self.fixture.argv("bundle", self.tmp / f"r{index}", self.tmp / "w",
                                                             "--target", "all"))
                self.assertEqual(code, 1, stderr)
                self.assertIn(needle, stderr)
        inventory_path.write_text(pristine)
        (self.fixture.runtime_licenses / "LICENSE.zstd.txt").write_text("changed")
        code, _, stderr = run_tool(self.fixture.argv("bundle", self.tmp / "r9", self.tmp / "w"))
        self.assertEqual(code, 1)
        self.assertIn("LICENSE.zstd.txt: sha256 differs", stderr)


@requires_release_tree
class GatewayInputTests(unittest.TestCase):
    """The gateway dist is checked against the recipe and the checked-in
    notices and SBOMs before anything is assembled."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-gw-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture = Fixture(self.tmp)
        self.tool = build_tool()
        args = self.tool.parse_args(self.fixture.argv("bundle", self.tmp / "out", self.tmp / "w"))
        self.ctx = self.tool.ReleaseContext(args)
        self.targets = ["linux-amd64", "linux-arm64", "darwin-arm64", "darwin-amd64"]

    def check(self) -> None:
        self.tool.check_gateway_dist(self.ctx, self.fixture.dist, self.targets)

    def edit_record(self, change) -> None:
        path = self.fixture.dist / "BUILD.json"
        record = json.loads(path.read_text())
        change(record)
        path.write_text(json.dumps(record))

    def test_the_fixture_passes(self) -> None:
        binaries = self.tool.check_gateway_dist(self.ctx, self.fixture.dist, self.targets)
        self.assertEqual(sorted(binaries), sorted(self.targets))

    def test_refusals(self) -> None:
        dist = self.fixture.dist
        pristine = self.tmp / "pristine"
        shutil.copytree(dist, pristine)
        cases = {
            "not the binary BUILD.json records": lambda: (dist / "linux-amd64" / "cli-proxy-api").write_bytes(
                (dist / "linux-amd64" / "cli-proxy-api").read_bytes() + b"x"),
            "series": lambda: self.edit_record(lambda r: r["series"].pop()),
            "admitted_series": lambda: self.edit_record(lambda r: r.update(admitted_series=False)),
            "upstream": lambda: self.edit_record(lambda r: r["upstream"].update(commit="0" * 40)),
            "no shipped record of darwin-amd64": lambda: self.edit_record(lambda r: r["targets"].pop("darwin-amd64")),
            "the SBOM is not the checked-in": lambda: (dist / "linux-arm64" / "cli-proxy-api.cdx.json").write_text("{}"),
            "licenses differ": lambda: (dist / "licenses" / "CLIProxyAPI" / "MODIFICATIONS.txt").write_text("x"),
            "THIRD_PARTY_NOTICES.txt": lambda: (dist / "licenses" / "THIRD_PARTY_NOTICES.txt").write_text("x"),
            "gateway-contract.json is not the recipe's": lambda: (dist / "gateway-contract.json").write_text("{}"),
            "patch marker": lambda: self._rewrite_binary("darwin-arm64", lambda data: data.replace(
                b"/v0/management/model-definitions/", b"/v0/management/model-definition_/")),
            "dynamically linked": lambda: self._rewrite_binary("linux-arm64", lambda data: data[:64] + struct.pack(
                "<I", 3) + data[68:]),
            "references a Nix store path": lambda: self._rewrite_binary("linux-amd64", lambda data: data + (
                b"/nix/store/" + b"a" * 32 + b"-glibc")),
        }
        for needle, damage in cases.items():
            with self.subTest(needle):
                shutil.rmtree(dist)
                shutil.copytree(pristine, dist)
                damage()
                with self.assertRaises(self.tool.BuildError) as caught:
                    self.check()
                self.assertIn(needle, str(caught.exception))
        shutil.rmtree(dist)
        with self.assertRaisesRegex(self.tool.BuildError, "holds no gateway build"):
            self.check()

    def _rewrite_binary(self, target: str, change) -> None:
        path = self.fixture.dist / target / "cli-proxy-api"
        data = change(path.read_bytes())
        path.write_bytes(data)
        self.edit_record(lambda r: r["targets"][target].update(sha256=hashlib.sha256(data).hexdigest(),
                                                                size=len(data)))


@requires_release_tree
class ProductAndTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-product-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tool = build_tool()
        self.doc = json.loads(UPSTREAM_JSON.read_text())
        runtime_tool = load_tool(REPO_ROOT / "tools" / "_build" / "python_runtime.py", "python_runtime")
        self.runtimes = runtime_tool.load(RUNTIMES_JSON)

    def product(self, change=None) -> Path:
        document = json.loads(PRODUCT_JSON.read_text())
        if change is not None:
            change(document)
        path = self.tmp / f"product-{len(list(self.tmp.iterdir()))}.json"
        path.write_text(json.dumps(document))
        return path

    def test_the_shipped_product(self) -> None:
        product = self.tool.load_product(PRODUCT_JSON, self.doc, self.runtimes)
        self.assertEqual(sorted(product["targets"]), list(TARGETS))
        self.assertEqual({entry["gateway"] for entry in product["targets"].values()},
                         {"linux-amd64", "linux-arm64", "darwin-arm64", "darwin-amd64"})
        self.assertEqual(product["launchers"], ["claude-multi", "claude-multi-proxy"])
        # The release locations: the public repository's GitHub releases.
        self.assertEqual(product["release_base_url"],
                         "https://github.com/nkoturovic/claude-multi/releases/download/v{version}")
        self.assertEqual(product["release_latest_url"],
                         "https://github.com/nkoturovic/claude-multi/releases/latest/download")

    def test_product_refusals(self) -> None:
        cases = {
            "windows canary": lambda d: d["targets"]["linux-x86_64"].update(gateway="windows-amd64"),
            "shared gateway": lambda d: d["targets"]["linux-aarch64"].update(gateway="linux-amd64"),
            "developer launcher": lambda d: d.update(launchers=sorted(d["launchers"] + ["claude-multi-dev"])),
            "one-model alias": lambda d: d.update(launchers=sorted(d["launchers"] + ["claude-gateway"])),
            "missing proxy": lambda d: d.update(launchers=["claude-multi"]),
            "target set": lambda d: d["targets"].pop("darwin-x86_64"),
            "plain http": lambda d: d.update(release_base_url="http://example.invalid"),
            "extra key": lambda d: d.update(extra=1),
        }
        for label, change in cases.items():
            with self.subTest(label):
                with self.assertRaises(self.tool.BuildError):
                    self.tool.load_product(self.product(change), self.doc, self.runtimes)
        with self.assertRaisesRegex(self.tool.BuildError, "a bundle never ships claude-gateway .*claude-multi direct"):
            self.tool.load_product(self.product(lambda d: d.update(launchers=["claude-gateway", "claude-multi",
                                                                              "claude-multi-proxy"])),
                                   self.doc, self.runtimes)

    def test_release_trust_lines(self) -> None:
        resources = self.tmp / "resources"
        trust_file = resources / "release-trust" / "allowed_signers"
        trust_file.parent.mkdir(parents=True)
        trust_file.write_text("# comments only\n\n")
        self.assertEqual(self.tool.release_trust(resources)["signers"], [])
        line = 'release@claude-multi namespaces="claude-multi-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample'
        trust_file.write_text(f"# key\n{line}\n")
        self.assertEqual(self.tool.release_trust(resources)["signers"], [line])
        for bad in ("someone@else ssh-ed25519 AAAA", "release@claude-multi ssh-rsa AAAA",
                    "release@claude-multi ssh-ed25519 AAAA it's"):
            with self.subTest(bad):
                trust_file.write_text(bad + "\n")
                with self.assertRaises(self.tool.BuildError):
                    self.tool.release_trust(resources)

    def test_installer_fields(self) -> None:
        shell = (REPO_ROOT / "packaging" / "install.sh").read_text()
        filled = self.tool.fill_shell_fields(shell, {"RELEASE_VERSION": "1.2.3", "RELEASE_SIGNERS": "a\nb",
                                                     "RELEASE_SUMS": "x  y", "RELEASE_BASE_URL": "https://e.invalid"})
        self.assertIn("\nRELEASE_SIGNERS='a\nb'\n", filled)
        self.assertIn("\nRELEASE_BASE_URL='https://e.invalid'\n", filled)
        result = subprocess.run(["sh", "-n"], input=filled, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(self.tool.BuildError):
            self.tool.fill_shell_fields(filled, {})  # already filled
        with self.assertRaises(self.tool.BuildError):
            self.tool.fill_shell_fields(shell, {"RELEASE_SUMS": "it's"})
        powershell = (REPO_ROOT / "packaging" / "install.ps1").read_text()
        filled = self.tool.fill_powershell_fields(powershell, {"Version": "1.2.3", "InstallerUrl": "https://e.invalid/x",
                                                               "InstallerSha256": "ab"})
        self.assertIn("        InstallerUrl    = 'https://e.invalid/x'\n", filled)
        with self.assertRaises(self.tool.BuildError):
            self.tool.fill_powershell_fields(filled, {})


@requires_release_tree
class ReleaseInputTests(unittest.TestCase):
    """A release carries its installations' update inputs: the release key
    and the download locations. Without either, or with --base-url, the
    release build refuses before it builds anything; --test-build (tests and
    local trials only) builds anyway and says so."""

    KEY_LINE = 'release@claude-multi namespaces="claude-multi-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample'
    REMEDIES = {
        "key": "names no signing key: add the production public key line",
        "base": "names no release_base_url: set the https download location",
        "latest": "names no release_latest_url: set the https download location",
        "override": "--base-url replaces the download location of packaging/product.json",
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-inputs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture = Fixture(self.tmp)
        self.tool = build_tool()

    def product(self, **changes) -> Path:
        document = json.loads(PRODUCT_JSON.read_text())
        document.update(changes)
        path = self.tmp / f"product-{len(list(self.tmp.glob('product-*')))}.json"
        path.write_text(json.dumps(document))
        return path

    def context(self, *extra: str):
        args = self.tool.parse_args(self.fixture.argv("release", self.tmp / "out", self.tmp / "w", *extra,
                                                      test_build=False))
        return self.tool.ReleaseContext(args)

    def test_each_missing_input_is_named_with_its_remedy(self) -> None:
        keyed = {"sha256": "0" * 64, "signers": [self.KEY_LINE]}
        empty = {"sha256": "0" * 64, "signers": []}
        self.assertEqual(self.tool.release_input_problems(self.context(), keyed), [])
        cases = {
            "key": (self.context(), empty),
            "base": (self.context("--product", str(self.product(release_base_url=None))), keyed),
            "latest": (self.context("--product", str(self.product(release_latest_url=None))), keyed),
            "override": (self.context("--base-url", "https://example.invalid/r/v{version}"), keyed),
        }
        for name, (ctx, trust) in cases.items():
            with self.subTest(name):
                problems = self.tool.release_input_problems(ctx, trust)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(self.REMEDIES[name], problems[0])

    def test_a_production_release_refuses_before_building(self) -> None:
        out = self.tmp / "out"
        argv = self.fixture.argv("release", out, self.tmp / "w", "--product",
                                 str(self.product(release_base_url=None, release_latest_url=None)),
                                 "--base-url", "https://example.invalid/r/v{version}",
                                 "--repo", str(self.fixture.keyless_tree()), test_build=False)
        code, stdout, stderr = run_tool(argv)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("this release cannot be published:", stderr)
        for remedy in self.REMEDIES.values():
            self.assertIn(remedy, stderr)
        self.assertIn("--test-build builds it anyway for tests and local trials, never for a release", stderr)
        self.assertFalse(out.exists())  # refused before the output directory was claimed
        # The help names the flag test-only.
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit):
            self.tool.parse_args(["release", "--help"])
        self.assertRegex(" ".join(help_text.getvalue().split()), r"--test-build\s+test only, never a release")

    def test_a_production_repro_refuses_before_building_the_gateway(self) -> None:
        scratch = self.tmp / "scratch"
        argv = self.fixture.argv("release", self.tmp / "out", self.tmp / "w", "--scratch", str(scratch),
                                 "--repo", str(self.fixture.keyless_tree()), test_build=False)
        position = argv.index("--gateway-dist")
        del argv[position:position + 2]  # no earlier gateway build: repro would build one first
        args = self.tool.parse_args([argv[0], "repro", *argv[1:]])
        with mock.patch.object(self.tool, "gateway_outputs", side_effect=AssertionError("gateway built")) as built, \
                self.assertRaisesRegex(self.tool.BuildError, "this release cannot be published:") as caught:
            self.tool.release_main(args)
        self.assertIn(self.REMEDIES["key"], str(caught.exception))
        built.assert_not_called()
        self.assertFalse(scratch.exists())
        # Comparing two existing builds stays available.
        compare = self.tool.parse_args([argv[0], "compare", *argv[1:], "--other", str(self.tmp / "other")])
        with self.assertRaisesRegex(self.tool.BuildError, "MANIFEST.json"):
            self.tool.release_main(compare)

    def test_test_build_designation_is_checksummed_with_release_inputs(self) -> None:
        for test_build in (False, True):
            with self.subTest(test_build=test_build):
                out = self.tmp / f"out-{test_build}"
                code, stdout, stderr = run_tool(self.fixture.argv(
                    "release", out, self.tmp / f"work-{test_build}", test_build=test_build))
                self.assertEqual(code, 0, stderr)
                manifest_path = out / "MANIFEST.json"
                manifest = json.loads(manifest_path.read_text())
                self.assertGreater(manifest["release_trust"]["signers"], 0)
                self.assertTrue(all(manifest["release"][key].startswith("https://")
                                    for key in ("base_url", "latest_url")))
                self.assertIs(manifest.get("test_build"), test_build)
                self.assertIn(hashlib.sha256(manifest_path.read_bytes()).hexdigest() + "  MANIFEST.json\n",
                              (out / "SHA256SUMS").read_text())
                self.assertIs(json.loads(stdout)["test_build"], test_build)

    def test_a_test_build_passes_and_is_labelled(self) -> None:
        out = self.tmp / "out"
        argv = self.fixture.argv("release", out, self.tmp / "w", "--product",
                                 str(self.product(release_base_url=None, release_latest_url=None)),
                                 "--base-url", "https://example.invalid/r/v{version}",
                                 "--repo", str(self.fixture.keyless_tree()))
        code, stdout, stderr = run_tool(argv)
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual((summary["test_build"], summary["release_key"]), (True, False))
        self.assertIn("a test build (--test-build): never sign or publish it", stderr)
        manifest = json.loads((out / "MANIFEST.json").read_text())
        self.assertEqual(manifest["release"], {"base_url": "https://example.invalid/r/v{version}", "latest_url": None})
        self.assertIn("\nRELEASE_BASE_URL='https://example.invalid/r/v{version}'\n", (out / "install.sh").read_text())


@requires_release_tree
class DocumentLayoutTests(unittest.TestCase):
    """pyproject.toml's data-files table is the one document layout of the
    wheel, the bundles and the Nix package: keys share/claude-multi[/<dir>],
    files under docs/<dir>/, installed at <key>/<name>."""

    def setUp(self) -> None:
        self.tool = build_tool()

    def test_the_shipped_documents(self) -> None:
        found = self.tool.documents(REPO_ROOT)
        table = self.tool.document_layout(pyproject()["tool"]["setuptools"]["data-files"])
        self.assertEqual([(path.relative_to(REPO_ROOT).as_posix(), str(target)) for path, target in found],
                         [(str(source), str(target)) for source, target in table])
        for name in ("USAGE.md", "CHEATSHEET.md", "STANDALONE.md"):
            self.assertIn((f"docs/{name}", f"share/claude-multi/{name}"),
                          [(str(source), str(target)) for source, target in table])

    def test_the_rule(self) -> None:
        layout = self.tool.document_layout
        accepted = layout({"share/claude-multi": ["docs/USAGE.md"],
                           "share/claude-multi/reference": ["docs/reference/licenses.md", "docs/reference/a.md"],
                           "share/claude-multi/guide/deep": ["docs/guide/deep/x.md"]})
        self.assertEqual([(str(source), str(target)) for source, target in accepted], [
            ("docs/USAGE.md", "share/claude-multi/USAGE.md"),
            ("docs/reference/licenses.md", "share/claude-multi/reference/licenses.md"),
            ("docs/reference/a.md", "share/claude-multi/reference/a.md"),
            ("docs/guide/deep/x.md", "share/claude-multi/guide/deep/x.md")])
        refused = {
            "both install": {"share/claude-multi": ["docs/USAGE.md", "docs/USAGE.md"]},
            "its directory below docs/ is not reference": {"share/claude-multi/reference": ["docs/licenses.md"]},
            "its directory below docs/ is not the top": {"share/claude-multi": ["docs/reference/licenses.md"]},
            "is not a file under docs/": {"share/claude-multi": ["README.md"]},
            "is not share/claude-multi or a directory under it": {"share/doc/claude-multi": ["docs/USAGE.md"]},
            "lists no files": {"share/claude-multi": []},
        }
        for needle, table in refused.items():
            with self.subTest(needle):
                with self.assertRaisesRegex(self.tool.BuildError, re.escape(needle)):
                    layout(table)
        for name in ("docs/../README.md", "/docs/USAGE.md", "docs"):
            with self.subTest(name), self.assertRaises(self.tool.BuildError):
                layout({"share/claude-multi": [name]})
        with self.assertRaises(self.tool.BuildError):
            layout({"share/claude-multi/../etc": ["docs/USAGE.md"]})

    def test_a_nested_document_is_installed_at_its_relative_path(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-docs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        fixture = Fixture(tmp)
        tree = tmp / "tree"
        for part in ("src", "gateway", "packaging", "docs"):
            shutil.copytree(REPO_ROOT / part, tree / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(REPO_ROOT / "LICENSE", tree / "LICENSE")
        # A directory the shipped table does not have yet.
        nested = tree / "docs" / "extra" / "nested.md"
        nested.parent.mkdir()
        nested.write_text("# Nested\n\nSee [usage](../USAGE.md).\n")
        project = (REPO_ROOT / "pyproject.toml").read_text()
        self.assertEqual(project.count('\n[tool.setuptools.data-files]\n'), 1)
        project += '"share/claude-multi/extra" = ["docs/extra/nested.md"]\n'
        (tree / "pyproject.toml").write_text(project)
        out = tmp / "out"
        host = load_tool(REPO_ROOT / "tools" / "_build" / "python_runtime.py", "python_runtime").host_target()
        code, stdout, stderr = run_tool(fixture.argv("bundle", out, tmp / "w", "--repo", str(tree), "--target", host))
        self.assertEqual(code, 0, stderr)
        version = json.loads(stdout)["version"]
        archive = out / f"claude-multi-{version}-{host}.tar.gz"
        top = f"claude-multi-{version}-{host}"
        self.assertEqual(read_member(archive, f"{top}/share/claude-multi/extra/nested.md"), nested.read_bytes())
        self.assertEqual(read_member(archive, f"{top}/share/claude-multi/USAGE.md"),
                         (REPO_ROOT / "docs" / "USAGE.md").read_bytes())
        # The relative link resolves inside the installation as it does in the checkout.
        names = {entry.name for entry in members(archive)}
        self.assertIn(f"{top}/share/claude-multi/USAGE.md", names)
        # A table that breaks the rule refuses the bundle.
        (tree / "pyproject.toml").write_text(project.replace('"share/claude-multi/extra"',
                                                             '"share/claude-multi/elsewhere"'))
        code, _stdout, stderr = run_tool(fixture.argv("bundle", tmp / "out2", tmp / "w", "--repo", str(tree),
                                                      "--target", host))
        self.assertEqual(code, 1)
        self.assertIn("is installed into share/claude-multi/elsewhere, but its directory below docs/ is not "
                      "elsewhere", stderr)


@requires_release_tree
class BundleLauncherTests(unittest.TestCase):
    """The generated launchers, run as an installation runs them: through
    the install's ``current`` link, from another directory, with an empty
    HOME and a hostile inherited environment. The gateway is a recording
    stand-in; no network is used."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-launch-"))
        fixture = Fixture(cls.tmp)
        out = cls.tmp / "out"
        host = load_tool(REPO_ROOT / "tools" / "_build" / "python_runtime.py", "python_runtime").host_target()
        code, stdout, stderr = run_tool(fixture.argv("bundle", out, cls.tmp / "w", "--target", host))
        if code != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise AssertionError(stderr)
        version = json.loads(stdout)["version"]
        cls.version = version
        cls.install = cls.tmp / "home" / ".local" / "share" / "claude-multi" / "install"
        (cls.install / "versions").mkdir(parents=True)
        with tarfile.open(out / f"claude-multi-{version}-{host}.tar.gz", "r:gz") as tar:
            tar.extractall(cls.install / "versions", filter="data")
        (cls.install / "versions" / f"claude-multi-{version}-{host}").rename(cls.install / "versions" / version)
        os.symlink(f"versions/{version}", cls.install / "current")
        cls.bundle = cls.install / "versions" / version
        recorder = cls.bundle / "libexec" / "claude-multi" / "cli-proxy-api"
        executable = sys.executable
        half = len(executable) // 2
        recorder.write_text(
            "#!/bin/sh\n"
            f"exec '{executable[:half]}''{executable[half:]}' -I -c 'import json, os, sys; "
            "json.dump({\"argv\": sys.argv, \"env\": dict(os.environ)}, open(os.environ[\"HOME\"] + \"/gateway.json\", \"w\"))' "
            "\"$0\" \"$@\"\n")
        recorder.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="claude-multi-launch-home-", dir=self.tmp))
        self.cwd = Path(tempfile.mkdtemp(prefix="claude-multi-launch-cwd-", dir=self.tmp))

    def files(self) -> set[str]:
        return {str(path.relative_to(self.bundle)) for path in self.bundle.rglob("*")}

    def hostile(self) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
                "CLAUDE_MULTI_CHANNEL": "nix", "CLAUDE_MULTI_PROXY_PATCHES": "cli-proxy-api-management-readonly-allowlist.patch",
                "CLAUDE_MULTI_PROXY_BIN": str(self.tmp / "hostile-gateway"), "MANAGEMENT_PASSWORD": "INHERITED-VALUE",
                "CLAUDE_MULTI_ASSETS": str(self.tmp / "hostile-assets"),
                "CLAUDE_MULTI_HOOK_COMMAND": str(self.tmp / "hostile-hook"),
                "PYTHONPATH": str(self.tmp / "hostile-path"), "PYTHONHOME": str(self.tmp / "hostile-home"),
                "TERMINFO_DIRS": "/usr/share/terminfo", "SSL_CERT_FILE": "/etc/ssl/custom.pem",
                "SSL_CERT_DIR": "/etc/ssl/custom", "XDG_STATE_HOME": str(self.home / "state")}

    def selected_key(self, home: Path) -> str:
        """A selected management key in ``home``, as a Nix install leaves it
        (a home whose gateway is set up: its endpoint is recorded)."""

        from claude_multi import endpoint, management, state

        endpoint.write_config(home, endpoint.EndpointConfig(port=18329))
        directory = management.key_dir(home)
        state.ensure_private_dir(directory)
        key = hashlib.sha256(os.urandom(32)).hexdigest()
        state.atomic_write(directory / management.KEY_FILE, (key + "\n").encode())
        state.atomic_write(directory / management.PREPARED_FILE, management._digest(key))
        return key

    def run_launcher(self, argv: list[str], environ: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=self.cwd, env=environ, capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL)

    def test_version_through_the_installation_link(self) -> None:
        before = self.files()
        for name in ("claude-multi", "claude-multi-proxy"):
            with self.subTest(name):
                result = self.run_launcher([str(self.install / "current" / "bin" / name), "--version"], self.hostile())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith(f"{name} {self.version}"), result.stdout)
        self.assertEqual(self.files(), before, "running the launchers wrote into the installation")

    def test_the_installed_documents_link_only_inside_the_installation(self) -> None:
        # The bundle's documents as an installation holds them: every page
        # the data-files table names, nested directories included; every
        # relative link and heading anchor resolves inside share/claude-multi
        # and a root page is reached only by its public URL; the help's
        # pointers name those installed pages.
        import _docs_vocab as vocab

        share = self.bundle / "share" / "claude-multi"
        pages = sorted(path.relative_to(share).as_posix() for path in share.rglob("*.md"))
        table = pyproject()["tool"]["setuptools"]["data-files"]
        self.assertEqual(pages, sorted(Path(source).relative_to("docs").as_posix()
                                       for files in table.values() for source in files))
        self.assertTrue(any("/" in page for page in pages))
        self.assertEqual(vocab.installed_link_problems(share), [])
        result = self.run_launcher([str(self.install / "current" / "bin" / "claude-multi"), "--help"], self.hostile())
        self.assertEqual(result.returncode, 0, result.stderr)
        pointers = re.findall(r"(?m)^(?:Symptom → command|Documentation map): (.+)$", result.stdout)
        self.assertEqual(len(pointers), 2, result.stdout[-600:])
        for pointer in pointers:
            with self.subTest(pointer):
                self.assertTrue(Path(pointer).is_file())
                self.assertEqual(Path(pointer).resolve().parent, share.resolve())

    def test_a_link_to_a_launcher_finds_its_installation(self) -> None:
        link = self.cwd / "cm"
        os.symlink(self.install / "current" / "bin" / "claude-multi", link)
        result = self.run_launcher(["./cm", "--version"], self.hostile())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.version, result.stdout)

    def test_gateway_exec_keeps_management_off_with_a_hostile_environment(self) -> None:
        # Control (its own HOME): the inherited channel and patch list,
        # without the launcher, enable management and inject the key.
        control_home = Path(tempfile.mkdtemp(prefix="claude-multi-launch-control-", dir=self.tmp))
        control_key = self.selected_key(control_home)
        control = self.run_launcher(
            [str(self.install / "current" / "runtime" / "python" / "bin" / "python3"), "-I", "-B", "-c",
             "import sys; sys.path.insert(0, sys.argv.pop(1)); from claude_multi.entrypoints import main; "
             "raise SystemExit(main())", str(self.install / "current" / SITE), "claude-multi-proxy", "run"],
            {"HOME": str(control_home), "PATH": os.defpath, "LC_ALL": "C", "CLAUDE_MULTI_CHANNEL": "nix",
             "CLAUDE_MULTI_PROXY_PATCHES": "cli-proxy-api-management-readonly-allowlist.patch",
             "CLAUDE_MULTI_PROXY_BIN": str(self.bundle / "libexec" / "claude-multi" / "cli-proxy-api")})
        self.assertEqual(control.returncode, 0, control.stderr)
        recorded = json.loads((control_home / "gateway.json").read_text())
        self.assertEqual(recorded["env"].get("MANAGEMENT_PASSWORD"), control_key)
        # The launcher, with a selected key in its HOME too.
        self.selected_key(self.home)
        before = self.files()
        result = self.run_launcher([str(self.install / "current" / "bin" / "claude-multi-proxy"), "run"],
                                   self.hostile())
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads((self.home / "gateway.json").read_text())
        env = recorded["env"]
        current = self.install / "current"
        self.assertEqual(Path(recorded["argv"][1]).resolve(),
                         (self.bundle / "libexec" / "claude-multi" / "cli-proxy-api").resolve())
        self.assertNotIn("MANAGEMENT_PASSWORD", {name.upper() for name in env})
        self.assertNotIn("CLAUDE_MULTI_PROXY_PATCHES", env)
        self.assertNotIn("INHERITED-VALUE", result.stdout + result.stderr + json.dumps(env))
        self.assertIn("management is unavailable in this build", result.stderr)
        self.assertEqual(env["CLAUDE_MULTI_CHANNEL"], "bundle")
        self.assertEqual(env["CLAUDE_MULTI_PROXY_BIN"], str(current / "libexec" / "claude-multi" / "cli-proxy-api"))
        self.assertEqual(env["CLAUDE_MULTI_HOOK_COMMAND"], str(current / "bin" / "claude-multi"))
        self.assertEqual(env["CLAUDE_MULTI_ASSETS"], str(current / SITE / "claude_multi" / "data"))
        self.assertEqual(self.files(), before, "running the gateway wrote into the installation")

    def test_launcher_environment(self) -> None:
        probe = self.cwd / "probe.py"
        # The launcher text, with the interpreter's program replaced by a
        # report of what it was given (same shell logic, same exports).
        text = (self.bundle / "bin" / "claude-multi").read_text()
        bundle = load_tool(REPO_ROOT / "tools" / "_build" / "bundle.py", "bundle")
        self.assertIn(bundle.BOOTSTRAP, text)
        probe.write_text(text.replace(bundle.BOOTSTRAP, 'import json, os, sys; print(json.dumps({"argv": sys.argv, '
                                                       '"env": dict(os.environ), "path": sys.path, '
                                                       '"flags": [sys.flags.isolated, sys.dont_write_bytecode]}))'))
        probe.chmod(0o755)
        shutil.copyfile(probe, self.bundle / "bin" / "probe")
        (self.bundle / "bin" / "probe").chmod(0o755)
        self.addCleanup(os.unlink, self.bundle / "bin" / "probe")
        result = self.run_launcher([str(self.install / "current" / "bin" / "probe"), "one", "two words"],
                                   self.hostile())
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        current = self.install / "current"
        self.assertEqual(report["argv"], ["-c", str(current / SITE), "claude-multi", "one", "two words"])
        self.assertEqual(report["flags"], [1, True])
        self.assertNotIn(str(self.tmp / "hostile-path"), report["path"])
        env = report["env"]
        self.assertEqual(env["TERMINFO_DIRS"].split(":")[0], "/usr/share/terminfo")
        terminfo = str(current / "runtime" / "python" / "share" / "terminfo")
        self.assertEqual(env["TERMINFO_DIRS"].endswith(":" + terminfo), (self.bundle / "runtime" / "python" / "share"
                                                                         / "terminfo").is_dir())
        self.assertEqual((env["SSL_CERT_FILE"], env["SSL_CERT_DIR"]), ("/etc/ssl/custom.pem", "/etc/ssl/custom"))
        self.assertEqual(env["XDG_STATE_HOME"], str(self.home / "state"))  # the state root the user selected
        self.assertEqual(env["CLAUDE_MULTI_REGISTRY_DIR"], str(current / SITE / "claude_multi" / "data" / "registry"))
        self.assertNotIn("CLAUDE_MULTI_PROXY_PATCHES", env)


if __name__ == "__main__":
    unittest.main()
