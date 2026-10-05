"""tools/build.py, the one gateway build implementation.

Unit tests for its pieces (normalised tree hash, safe unpacking, verified
downloads from a local server, fuzz-free patch application, gate selection,
binary inspection) and an end-to-end run of every step over a tiny fixture
recipe with a stand-in `go` (no real toolchain, no network beyond a loopback
server on an ephemeral port). The optional comparison of a Nix-built and a
plain-built gateway runs when both binaries are supplied.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from _layout import BUILD_TOOL

NIX_GATEWAY_ENV = "CLAUDE_MULTI_TEST_NIX_GATEWAY"
DIST_GATEWAY_ENV = "CLAUDE_MULTI_TEST_DIST_GATEWAY"


def load_tool():
    spec = importlib.util.spec_from_file_location("gateway_build_tool_under_test", BUILD_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_tool()


# ------------------------------------------------------------------ synthetic executables


def elf(machine=62, program_types=(1,)):
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
    header = struct.pack("<HHIQQQIHHHHHH", 2, machine, 1, 0x400000, 64, 0, 0, 64, 56, len(program_types), 64, 0, 0)
    tables = b"".join(struct.pack("<IIQQQQQQ", kind, 5, 0, 0, 0, 0, 0, 0x1000) for kind in program_types)
    return ident + header + tables


def macho(cpu, libraries=("/usr/lib/libSystem.B.dylib",), rpaths=(), signature_flags=None):
    def padded(text):
        raw = text.encode() + b"\0"
        return raw + b"\0" * (-len(raw) % 8)

    commands = []
    for library in libraries:
        name = padded(library)
        commands.append(struct.pack("<IIIIII", 0xC, 24 + len(name), 24, 2, 0, 0) + name)
    for path in rpaths:
        name = padded(path)
        commands.append(struct.pack("<III", 0x8000001C, 12 + len(name), 12) + name)
    blob = b""
    if signature_flags is not None:
        directory = struct.pack(">IIII", 0xFADE0C02, 44, 0x20400, signature_flags) + b"\0" * 28
        blob = struct.pack(">IIIII", 0xFADE0CC0, 20 + len(directory), 1, 0, 20) + directory
        commands.append(None)
    size = sum(16 if command is None else len(command) for command in commands)
    offset = 32 + size
    body = b"".join(struct.pack("<IIII", 0x1D, 16, offset, len(blob)) if command is None else command
                    for command in commands)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, 2, len(commands), size, 0, 0)
    return header + body + blob


def pe(machine=0x8664):
    return b"MZ" + b"\0" * 0x3A + struct.pack("<I", 0x40) + b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 32


TARGET = {"linux-amd64": {"name": "linux-amd64", "goos": "linux", "goarch": "amd64", "shipped": True},
          "darwin-arm64": {"name": "darwin-arm64", "goos": "darwin", "goarch": "arm64", "shipped": True},
          "darwin-amd64": {"name": "darwin-amd64", "goos": "darwin", "goarch": "amd64", "shipped": True},
          "windows-amd64": {"name": "windows-amd64", "goos": "windows", "goarch": "amd64", "shipped": False}}


class TreeHashTests(unittest.TestCase):
    def test_content_paths_and_execute_bit_count_modes_and_times_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            (root / "a").mkdir(parents=True)
            (root / "a" / "file.go").write_text("package a\n")
            (root / "run.sh").write_text("#!/bin/sh\n")
            os.chmod(root / "run.sh", 0o755)
            base = tool.tree_sha256(root)
            os.chmod(root / "a" / "file.go", 0o444)
            os.utime(root / "a" / "file.go", (1, 1))
            (root / "empty").mkdir()
            self.assertEqual(tool.tree_sha256(root), base)
            os.chmod(root / "run.sh", 0o644)
            self.assertNotEqual(tool.tree_sha256(root), base)
            os.chmod(root / "run.sh", 0o755)
            (root / "a" / "file.go").chmod(0o644)
            (root / "a" / "file.go").write_text("package b\n")
            self.assertNotEqual(tool.tree_sha256(root), base)

    def test_symlinks_hash_their_target_and_top_level_names_can_be_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target").write_text("x")
            os.symlink("target", root / "link")
            first = tool.tree_sha256(root)
            (root / "link").unlink()
            os.symlink("elsewhere", root / "link")
            self.assertNotEqual(tool.tree_sha256(root), first)
            plain = tool.tree_sha256(root)
            (root / "vendor").mkdir()
            (root / "vendor" / "modules.txt").write_text("# m\n")
            self.assertNotEqual(tool.tree_sha256(root), plain)
            self.assertEqual(tool.tree_sha256(root, exclude_top=("vendor",)), plain)


def write_tar(path, members, *, comment=None, top="pkg-1"):
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": comment} if comment else None) as tar:
        directory = tarfile.TarInfo(top)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        for name, data in members.items():
            if isinstance(data, tuple):  # (kind, value)
                info = tarfile.TarInfo(name)
                info.type, info.linkname = tarfile.SYMTYPE, data[1]
                tar.addfile(info)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith("/go") else 0o644
            tar.addfile(info, io.BytesIO(data))


class SafeExtractTests(unittest.TestCase):
    def test_strips_the_top_directory_and_returns_the_pax_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "a.tar.gz"
            write_tar(archive, {"pkg-1/go.mod": b"module m\n", "pkg-1/bin/go": b"#!/bin/sh\n"}, comment="c" * 40)
            headers = tool.safe_extract(archive, Path(tmp) / "out")
            self.assertEqual(headers["comment"], "c" * 40)
            self.assertEqual((Path(tmp) / "out" / "go.mod").read_bytes(), b"module m\n")
            self.assertTrue(os.access(Path(tmp) / "out" / "bin" / "go", os.X_OK))

    def test_refuses_escaping_members(self):
        for members in ({"pkg-1/../evil": b"x"}, {"pkg-1/link": ("sym", "../../outside")},
                        {"pkg-1/abs": ("sym", "/etc/passwd")}):
            with self.subTest(members=list(members)), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "a.tar.gz"
                write_tar(archive, members)
                with self.assertRaises(tool.BuildError):
                    tool.safe_extract(archive, Path(tmp) / "out")


class LocalServer:
    """A loopback file server on an ephemeral port (never a gateway port)."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    def __init__(self, root):
        handler = functools.partial(self.Handler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class DownloadTests(unittest.TestCase):
    def test_verified_download_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp, LocalServer(tmp) as server:
            (Path(tmp) / "file.bin").write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            target = Path(tmp) / "cache" / "file.bin"
            tool.download(server.url + "file.bin", target, digest, attempts=1)
            self.assertEqual(target.read_bytes(), b"payload")
            other = Path(tmp) / "cache" / "other.bin"
            with self.assertRaisesRegex(tool.BuildError, "differs from the pinned"):
                tool.download(server.url + "file.bin", other, "0" * 64, attempts=3)
            self.assertFalse(other.exists())
            self.assertFalse(other.with_name("other.bin.part").exists())

    def test_retries_then_fails(self):
        with tempfile.TemporaryDirectory() as tmp, LocalServer(tmp) as server, \
                mock.patch.object(tool.time, "sleep") as sleep:
            with self.assertRaisesRegex(tool.BuildError, "after 2 attempts"):
                tool.download(server.url + "missing", Path(tmp) / "x", "0" * 64, attempts=2)
            sleep.assert_called_once_with(1)


def unified(path, old_start, removed, added, before=(), after=()):
    """A one-hunk git-style patch replacing ``removed`` with ``added``."""

    old = len(before) + len(removed) + len(after)
    new = len(before) + len(added) + len(after)
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
             f"@@ -{old_start},{old} +{old_start},{new} @@"]
    lines += [" " + line for line in before] + ["-" + line for line in removed]
    lines += ["+" + line for line in added] + [" " + line for line in after]
    return "\n".join(lines) + "\n"


class ApplyTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("boundary: git is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir()
        (self.src / "f.txt").write_text("a\nb\nc\nd\ne\n")

    def tearDown(self):
        self.tmp.cleanup()

    def patch(self, text):
        path = Path(self.tmp.name) / "p.patch"
        path.write_text(text)
        return path

    def test_clean_apply(self):
        tool.apply_patch(self.src, self.patch(unified("f.txt", 2, ["c"], ["C"], ["b"], ["d"])), "p")
        self.assertEqual((self.src / "f.txt").read_text(), "a\nb\nC\nd\ne\n")

    def test_offset_is_refused_and_nothing_changes(self):
        (self.src / "f.txt").write_text("x\na\nb\nc\nd\ne\n")
        with self.assertRaisesRegex(tool.BuildError, "offset"):
            tool.apply_patch(self.src, self.patch(unified("f.txt", 2, ["c"], ["C"], ["b"], ["d"])), "p")
        self.assertEqual((self.src / "f.txt").read_text(), "x\na\nb\nc\nd\ne\n")

    def test_zero_context_hunks_apply_at_their_exact_line_only(self):
        tool.apply_patch(self.src, self.patch(unified("f.txt", 3, ["c"], ["C"])), "p")
        self.assertEqual((self.src / "f.txt").read_text(), "a\nb\nC\nd\ne\n")
        (self.src / "f.txt").write_text("x\na\nb\nc\nd\ne\n")
        with self.assertRaisesRegex(tool.BuildError, "offset"):
            tool.apply_patch(self.src, self.patch(unified("f.txt", 3, ["c"], ["C"])), "p")

    def test_an_omission_build_may_move_a_hunk_with_its_full_context(self):
        (self.src / "f.txt").write_text("x\na\nb\nc\nd\ne\n")
        moved = tool.apply_patch(self.src, self.patch(unified("f.txt", 2, ["c"], ["C"], ["b"], ["d"])), "p",
                                 allow_offsets=True)
        self.assertEqual(moved, ["Hunk #1 succeeded at 3 (offset 1 line)."])
        self.assertEqual((self.src / "f.txt").read_text(), "x\na\nb\nC\nd\ne\n")
        self.assertEqual(tool.apply_patch(self.src, self.patch(unified("f.txt", 3, ["C"], ["c"], ["b"], ["d"])),
                                          "p", allow_offsets=True), [])

    def test_a_hunk_without_context_never_moves(self):
        (self.src / "f.txt").write_text("x\na\nb\nc\nd\ne\n")
        with self.assertRaisesRegex(tool.BuildError, "offset"):
            tool.apply_patch(self.src, self.patch(unified("f.txt", 3, ["c"], ["C"])), "p", allow_offsets=True)
        self.assertEqual((self.src / "f.txt").read_text(), "x\na\nb\nc\nd\ne\n")
        self.assertTrue(tool.has_contextless_change(unified("f.txt", 3, [], ["C"])))
        self.assertFalse(tool.has_contextless_change(unified("f.txt", 2, ["c"], ["C"], ["b"], ["d"])))
        new_file = "diff --git a/n.txt b/n.txt\n--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        self.assertFalse(tool.has_contextless_change(new_file))

    def test_reject_fails(self):
        with self.assertRaisesRegex(tool.BuildError, "does not apply"):
            tool.apply_patch(self.src, self.patch(unified("f.txt", 2, ["z"], ["Z"], ["b"], ["d"])), "p")

    def test_an_enclosing_repository_is_never_used(self):
        outer = Path(self.tmp.name)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", str(outer)], check=True, env=env)
        tool.apply_patch(self.src, self.patch(unified("f.txt", 2, ["c"], ["C"], ["b"], ["d"])), "p")
        self.assertEqual((self.src / "f.txt").read_text(), "a\nb\nC\nd\ne\n")
        (self.src / ".git").mkdir()
        with self.assertRaisesRegex(tool.BuildError, "git checkout"):
            tool.apply_patch(self.src, self.patch(unified("f.txt", 2, ["d"], ["D"], ["C"], ["e"])), "p")


class GateSelectionTests(unittest.TestCase):
    GATE = {"name": "g", "packages": ["./p"], "platforms": "portable", "race": False, "count": None,
            "expected_tests": []}

    def test_listing_keeps_tests_examples_and_fuzz_targets(self):
        output = "TestA\nBenchmarkB\nExampleC\nFuzzD\nok  \tpkg\t0.01s\n?   \tpkg2\t[no test files]\nnoise line\n"
        self.assertEqual(tool.parse_test_list(output), ["TestA", "ExampleC", "FuzzD"])

    def test_skip_filter_and_exact_or_subset_expectations(self):
        gate = {**self.GATE, "skip": "^(TestSkipped)$", "expected_tests": ["TestA"]}
        selected = tool.gate_selection(gate, ["TestA", "TestSkipped", "TestB"])
        self.assertEqual(selected, ["TestA", "TestB"])
        self.assertEqual(tool.check_selection(gate, selected), [])
        self.assertTrue(tool.check_selection({**gate, "expected_tests": ["TestGone"]}, selected))
        run = {**self.GATE, "run": "^(TestA)$", "expected_tests": ["TestA"]}
        self.assertEqual(tool.check_selection(run, ["TestA"]), [])
        self.assertTrue(tool.check_selection(run, ["TestA", "TestNew"]))
        self.assertIn("the test selection is empty", tool.check_selection(self.GATE, []))

    def test_platforms_selection_and_flags(self):
        race = {**self.GATE, "platforms": "linux", "race": True, "count": 100}
        self.assertTrue(tool.gate_applies(self.GATE, "darwin-arm64"))
        self.assertFalse(tool.gate_applies(race, "darwin-arm64"))
        self.assertTrue(tool.gate_applies(race, "linux-arm64"))
        self.assertTrue(tool.gate_selected(race, "race") and not tool.gate_selected(race, "portable"))
        self.assertEqual(tool.gate_flags(race), ["-race", "-count=100"])
        # -count=1 runs once and defeats the test cache.
        self.assertEqual(tool.gate_flags(self.GATE), ["-count=1"])


class InspectTests(unittest.TestCase):
    MARKERS = [{"patch": "p.patch", "text": "marker-text"}]

    def inspect(self, data, target, markers=MARKERS, forbidden=()):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bin"
            path.write_bytes(data)
            return tool.inspect_binary(path, TARGET[target], markers, forbidden)

    def test_static_linux_binary_with_marker_passes(self):
        self.assertEqual(self.inspect(elf() + b"marker-text", "linux-amd64"), [])

    def test_dynamic_wrong_machine_and_missing_marker(self):
        problems = self.inspect(elf(machine=183, program_types=(1, 3, 2)), "linux-amd64")
        self.assertEqual(len(problems), 3, problems)

    def test_store_references_are_concrete_paths_only(self):
        bare = elf() + b"marker-text no /nix/store/ prefix here"
        self.assertEqual(self.inspect(bare, "linux-amd64"), [])
        concrete = elf() + b"marker-text /nix/store/" + b"a" * 32 + b"-go-1.26/lib"
        self.assertIn("Nix store", " ".join(self.inspect(concrete, "linux-amd64")))
        not_base32 = elf() + b"marker-text /nix/store/" + b"e" * 32 + b"-x"
        self.assertEqual(self.inspect(not_base32, "linux-amd64"), [])

    def test_build_paths_are_refused(self):
        self.assertTrue(self.inspect(elf() + b"marker-text /build/cm-work/src", "linux-amd64",
                                     forbidden=["/build/cm-work"]))

    def test_darwin_arm64_needs_the_linker_signature(self):
        signed = tool.CS_ADHOC | tool.CS_LINKER_SIGNED
        self.assertEqual(self.inspect(macho(0x0100000C, signature_flags=signed) + b"marker-text", "darwin-arm64"), [])
        self.assertTrue(self.inspect(macho(0x0100000C) + b"marker-text", "darwin-arm64"))
        self.assertTrue(self.inspect(macho(0x0100000C, signature_flags=tool.CS_ADHOC) + b"marker-text",
                                     "darwin-arm64"))
        self.assertEqual(self.inspect(macho(0x01000007) + b"marker-text", "darwin-amd64"), [])

    def test_darwin_libraries_must_be_system_libraries(self):
        foreign = macho(0x01000007, libraries=("/usr/lib/libSystem.B.dylib", "/opt/lib/libfoo.dylib"))
        self.assertIn("outside the system", " ".join(self.inspect(foreign + b"marker-text", "darwin-amd64")))
        rpath = macho(0x01000007, rpaths=("@loader_path/lib",))
        self.assertIn("run paths", " ".join(self.inspect(rpath + b"marker-text", "darwin-amd64")))

    def test_windows_canary_format(self):
        self.assertEqual(self.inspect(pe() + b"marker-text", "windows-amd64"), [])
        self.assertTrue(self.inspect(pe(0xAA64) + b"marker-text", "windows-amd64"))
        self.assertTrue(self.inspect(b"garbage marker-text", "windows-amd64"))


class EnvironmentTests(unittest.TestCase):
    def test_inherited_go_settings_never_reach_the_toolchain(self):
        base = {"PATH": "/usr/bin", "GOFLAGS": "-tags=evil", "GOAMD64": "v3", "GOEXPERIMENT": "x",
                "CGO_CFLAGS": "-O3", "XDG_CONFIG_HOME": "/real", "HOME": "/home/someone", "KEEP": "1"}
        env = tool.go_environment(Path("/w"), goos="linux", goarch="arm64", base=base)
        self.assertEqual(env["GOFLAGS"], "-mod=vendor")
        for key in ("GOAMD64", "GOEXPERIMENT", "CGO_CFLAGS", "XDG_CONFIG_HOME"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HOME"], "/w/home")
        self.assertEqual((env["GOTOOLCHAIN"], env["GOPROXY"], env["CGO_ENABLED"]), ("local", "off", "0"))
        self.assertEqual((env["GOOS"], env["GOARCH"]), ("linux", "arm64"))
        self.assertTrue(env["PATH"].startswith("/w/go/bin" + os.pathsep))
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(tool.go_environment(Path("/w"), cgo=True, base=base)["CGO_ENABLED"], "1")


# ------------------------------------------------------------------ end to end with a stand-in go


FAKE_GO = r'''#!{python}
"""A stand-in `go`: builds deterministic executables, lists and runs tests."""
import hashlib, os, pathlib, struct, sys

def fail(message):
    print("fake go: " + message, file=sys.stderr)
    sys.exit(3)

for key, value in (("GOTOOLCHAIN", "local"), ("GOFLAGS", "-mod=vendor"), ("GOPROXY", "off")):
    if os.environ.get(key) != value:
        fail(f"{{key}}={{os.environ.get(key)!r}}")
args = sys.argv[1:]
src = pathlib.Path.cwd()
if not (src / "vendor" / "modules.txt").is_file():
    fail("no vendor tree")
if args[0] == "build":
    if os.environ.get("CGO_ENABLED") != "0":
        fail("cgo")
    out = pathlib.Path(args[args.index("-o") + 1])
    flags = [arg for arg in args if arg.startswith("-ldflags=")]
    body = (src / "cmd" / "server" / "main.go").read_bytes() + flags[0].encode()
    goos, goarch = os.environ["GOOS"], os.environ["GOARCH"]
    if goos == "linux":
        machine = {{"amd64": 62, "arm64": 183}}[goarch]
        head = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
        head += struct.pack("<HHIQQQIHHHHHH", 2, machine, 1, 0, 64, 0, 0, 64, 56, 1, 64, 0, 0)
        head += struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 0, 0, 0)
    else:
        head = b"MZ" + b"\0" * 0x3A + struct.pack("<I", 0x40) + b"PE\0\0" + struct.pack("<H", 0x8664) + b"\0" * 32
    # Go's embedded build information: the modules vendor/modules.txt lists.
    deps = [line.split()[1:3] for line in (src / "vendor" / "modules.txt").read_text().splitlines()
            if line.startswith("# ")]
    modinfo = "path\texample.test/m/cmd/server\nmod\texample.test/m\t(devel)\t\n" + "".join(
        f"dep\t{{path}}\t{{version}}\t\n" for path, version in deps) + f"build\tGOOS={{goos}}\n"
    raw = (b"0w\xaf\x0c\x92t\x08\x02A\xe1\xc1\x07\xe6\xd6\x18\xe6" + modinfo.encode()
           + b"\xf92C1\x86\x18 r\x00\x82B\x10A\x16\xd8\xf2")
    def varint(value):
        out = b""
        while value >= 0x80:
            out, value = out + bytes([value & 0x7F | 0x80]), value >> 7
        return out + bytes([value])
    info = b"\xff Go buildinf:" + bytes([8, 2]) + b"\0" * 16 + varint(7) + b"go9.9.9" + varint(len(raw)) + raw
    out.write_bytes(head + body + info + os.environ.get("FAKE_GO_EXTRA", "").encode())
elif args[0] == "test":
    if "-list" in args:
        print("TestAlpha\nTestBeta\nok  \texample.test/m\t0.01s")
    elif os.environ.get("FAKE_GO_TEST_FAIL"):
        sys.exit(1)
else:
    fail("unsupported " + " ".join(args))
'''

MAIN_GO = "package main\n\nfunc main() {\n\tprintln(\"upstream\")\n}\n"
MIT_TEXT = """MIT License

Copyright (c) 2026 Example

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software, to deal in the Software without restriction.

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
"""
BSD3_TEXT = """Copyright 2026 Example Authors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
Neither the name of the copyright holder nor the names of its contributors may
be used to endorse or promote products derived from this software.
"""
SUBPACKAGE_TEXT = BSD3_TEXT.replace("Example Authors", "Subpackage Authors")
MODULE_SUM = "h1:" + base64.b64encode(bytes(range(32))).decode()


class EndToEndTests(unittest.TestCase):
    """Every step over a fixture recipe; the pipeline and its refusals."""

    COMMIT = "1" * 40

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("boundary: git is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = tool.host_target()
        # Upstream source archive with its commit in the pax comment.
        source = {f"CLIProxyAPI-{self.COMMIT}/go.mod": b"module example.test/m\n\ngo 1.26.0\n",
                  f"CLIProxyAPI-{self.COMMIT}/go.sum": f"example.dep v1.0.0 {MODULE_SUM}\n"
                                                       "example.dep v1.0.0/go.mod h1:AAAA\n"
                                                       f"example.dep/inner v0.1.0 {MODULE_SUM}\n".encode(),
                  f"CLIProxyAPI-{self.COMMIT}/LICENSE": MIT_TEXT.encode(),
                  f"CLIProxyAPI-{self.COMMIT}/cmd/server/main.go": MAIN_GO.encode()}
        self.downloads = self.root / "served"
        self.downloads.mkdir()
        write_tar(self.downloads / "src.tar.gz", source, comment=self.COMMIT, top=f"CLIProxyAPI-{self.COMMIT}")
        unpacked = self.root / "unpacked"
        tool.safe_extract(self.downloads / "src.tar.gz", unpacked)
        go = FAKE_GO.format(python=sys.executable).encode()
        write_tar(self.downloads / f"go9.9.9.{self.host}.tar.gz",
                  {"go/bin/go": go, "go/LICENSE": BSD3_TEXT.encode()}, top="go")
        self.vendor = self.root / "vendor-tree"
        (self.vendor / "example.dep").mkdir(parents=True)
        (self.vendor / "modules.txt").write_text("# example.dep v1.0.0\n")
        (self.vendor / "example.dep" / "dep.go").write_text("package dep\n")
        (self.vendor / "example.dep" / "LICENSE").write_text(MIT_TEXT)
        (self.vendor / "example.dep" / "NOTICE").write_text("Example notice text.\n")
        self.patches = self.root / "patches"
        self.patches.mkdir()
        first = unified("cmd/server/main.go", 3, ['\tprintln("upstream")'], ['\tprintln("upstream", "patched-marker")'],
                        ["func main() {"], ["}"])
        second = unified("cmd/server/main.go", 3, ['\tprintln("upstream", "patched-marker")'],
                         ['\tprintln("upstream", "patched-marker", "second")'], ["func main() {"], ["}"])
        (self.patches / "cli-proxy-api-first.patch").write_text(first)
        (self.patches / "cli-proxy-api-second.patch").write_text(second)
        sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()  # noqa: E731
        self.recipe = {
            "format": 1,
            "upstream": {"name": "CLIProxyAPI", "repository": "https://github.com/example/CLIProxyAPI",
                         "tag": "v1.2.3", "version": "1.2.3", "commit": self.COMMIT, "license": "MIT",
                         "module": "example.test/m", "main_package": "./cmd/server", "binary": "cli-proxy-api"},
            "source": {"archive_url": "SERVER/src.tar.gz", "archive_file": f"CLIProxyAPI-{self.COMMIT}.tar.gz",
                       "archive_sha256": sha(self.downloads / "src.tar.gz"),
                       "tree_sha256": tool.tree_sha256(unpacked), "nix_hash": "sha256-" + "A" * 43 + "="},
            "vendor": {"tree_sha256": tool.tree_sha256(self.vendor), "nix_hash": "sha256-" + "B" * 43 + "="},
            "toolchain": {"version": "9.9.9", "source": "https://go.dev/dl/?mode=json&include=all",
                          "url_prefix": "SERVER/",
                          "archives": {self.host: {"file": f"go9.9.9.{self.host}.tar.gz",
                                                   "sha256": sha(self.downloads / f"go9.9.9.{self.host}.tar.gz")}}},
            "series": [{"basename": "cli-proxy-api-first.patch", "sha256": sha(self.patches / "cli-proxy-api-first.patch"),
                        "admitted": True},
                       {"basename": "cli-proxy-api-second.patch",
                        "sha256": sha(self.patches / "cli-proxy-api-second.patch"), "admitted": True}],
            "candidates": [],
            "build": {"env": {"CGO_ENABLED": "0"}, "flags": ["-trimpath", "-buildvcs=false", "-mod=vendor"],
                      "ldflags": ["-s", "-w", "-buildid=", "-X", "main.Version=1.2.3"]},
            "targets": [{"name": "linux-amd64", "goos": "linux", "goarch": "amd64", "shipped": True},
                        {"name": "windows-amd64", "goos": "windows", "goarch": "amd64", "shipped": False}],
            "gates": [{"name": "whole", "packages": ["./..."], "platforms": "portable", "race": False, "count": None,
                       "expected_tests": ["TestAlpha"]},
                      {"name": "named", "packages": ["./cmd/server"], "run": "^(TestAlpha|TestBeta)$",
                       "platforms": "portable", "race": False, "count": None,
                       "expected_tests": ["TestAlpha", "TestBeta"]}],
            "source_pins": [{"name": "main", "path": "cmd/server/main.go", "text": "patched-marker"}],
            "markers": [{"patch": "cli-proxy-api-first.patch", "text": "patched-marker"}],
        }
        self.server = LocalServer(self.downloads).__enter__()
        self.write_recipe()
        self.cache = self.root / "cache"
        self.work = self.root / "work"
        self.dist = self.root / "dist"
        self.licenses = self.root / "licenses"
        self.sbom = self.root / "sbom"

    def tearDown(self):
        self.server.__exit__(None, None, None)
        tool.remove_tree(self.root / "work")
        self.tmp.cleanup()

    def write_recipe(self):
        text = json.dumps(self.recipe, indent=2).replace("SERVER/", self.server.url)
        self.recipe_path = self.root / "UPSTREAM.json"
        self.recipe_path.write_text(text)

    def run_tool(self, *argv, expect=0):
        stderr = io.StringIO()
        stdout = io.StringIO()
        common = ["--upstream", str(self.recipe_path), "--patches", str(self.patches),
                  "--cache", str(self.cache), "--work", str(self.work), "--dist", str(self.dist),
                  "--licenses", str(self.licenses), "--sbom", str(self.sbom)]
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = tool.main(["gateway", *argv, *common])
        self.assertEqual(code, expect, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def seed_vendor_cache(self):
        shutil.copytree(self.vendor, self.cache / "vendor" / self.recipe["vendor"]["tree_sha256"])

    def test_default_pipeline_downloads_verifies_builds_inspects_and_records(self):
        self.seed_vendor_cache()
        # The notices describe the shipped build; generate them first.
        self.run_tool("fetch", "vendor", "apply", "build")
        self.run_tool("notices")
        self.run_tool()
        self.assertTrue((self.cache / "downloads" / self.recipe["source"]["archive_file"]).is_file())
        record = json.loads((self.dist / "BUILD.json").read_text())
        self.assertEqual(sorted(record["targets"]), ["linux-amd64", "windows-amd64"])
        self.assertTrue(record["admitted_series"])
        self.assertEqual([entry["basename"] for entry in record["series"]],
                         ["cli-proxy-api-first.patch", "cli-proxy-api-second.patch"])
        self.assertEqual(record["vendor"]["tree_sha256"], self.recipe["vendor"]["tree_sha256"])
        binary = self.dist / "linux-amd64" / "cli-proxy-api"
        self.assertEqual(record["targets"]["linux-amd64"]["sha256"], hashlib.sha256(binary.read_bytes()).hexdigest())
        self.assertIn(b"-ldflags=-s -w -buildid= -X main.Version=1.2.3", binary.read_bytes())
        self.assertIn(b'"second"', binary.read_bytes())
        self.assertTrue((self.dist / "windows-amd64" / "cli-proxy-api.exe").is_file())
        contract = json.loads((self.dist / "gateway-contract.json").read_bytes())
        self.assertEqual(contract["patches"][0], "cli-proxy-api-first.patch@" + self.recipe["series"][0]["sha256"])
        # Offline, the gates run against the prepared tree.
        self.run_tool("gates", "--offline")
        report = json.loads((self.work / "gates.json").read_text())
        self.assertEqual([(gate["name"], gate["status"], gate["tests"]) for gate in report["gates"]],
                         [("whole", "passed", 2), ("named", "passed", 2)])

    def test_offline_inputs_build_the_same_bytes_and_compare(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build")
        first = (self.dist / "linux-amd64" / "cli-proxy-api").read_bytes()
        inputs = self.root / "inputs"
        inputs.mkdir()
        os.symlink(self.root / "unpacked", inputs / "source")
        os.symlink(self.vendor, inputs / "vendor")
        os.symlink(self.cache / "downloads" / f"go9.9.9.{self.host}.tar.gz", inputs / f"go9.9.9.{self.host}.tar.gz")
        other_dist = self.root / "dist-inputs"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = tool.main(["gateway", "fetch", "vendor", "apply", "build", "--offline", "--inputs", str(inputs),
                              "--upstream", str(self.recipe_path), "--patches", str(self.patches),
                              "--cache", str(self.root / "empty-cache"), "--work", str(self.root / "work2"),
                              "--dist", str(other_dist), "--target", "linux-amd64"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual((other_dist / "linux-amd64" / "cli-proxy-api").read_bytes(), first)
        out, _ = self.run_tool("compare", "--other", str(other_dist / "linux-amd64" / "cli-proxy-api"))
        self.assertTrue(json.loads(out)["identical"])
        tool.remove_tree(self.root / "work2")

    def test_relative_directories_resolve_where_they_were_given(self):
        # go and git run inside the prepared source tree; relative --work,
        # --cache, --patches and --dist must still name the caller's paths.
        self.seed_vendor_cache()
        self.run_tool("fetch")  # caches the verified downloads
        tool.remove_tree(self.work)
        caller = self.root.resolve()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GO", "CGO_")) and key != tool.CACHE_ENV}
        env["HOME"] = str(caller / "home")

        def run(*argv):
            return subprocess.run([sys.executable, str(BUILD_TOOL), "gateway", *argv], cwd=caller, env=env,
                                  capture_output=True, text=True, timeout=120)

        built = run("fetch", "vendor", "apply", "build", "--offline", "--target", "linux-amd64",
                    "--upstream", "UPSTREAM.json", "--patches", "patches", "--cache", "cache",
                    "--work", "rel/work", "--dist", "rel/dist")
        self.assertEqual(built.returncode, 0, built.stderr)
        state = json.loads((caller / "rel/work/state.json").read_text())
        self.assertEqual(state["done"], ["fetch", "vendor", "apply", "build"])
        self.assertIn(b'"second"', (caller / "rel/dist/linux-amd64/cli-proxy-api").read_bytes())
        printed = run("env", "--upstream", "UPSTREAM.json", "--cache", "cache", "--work", "rel/work")
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn(f"export GOROOT='{caller / 'rel/work/go'}'\n", printed.stdout)
        self.assertIn(f"export GOCACHE='{caller / 'rel/work/cache/go-build'}'\n", printed.stdout)
        # The default work dir lives in a relative --cache, also from the caller.
        defaulted = run("fetch", "vendor", "apply", "build", "--offline", "--target", "linux-amd64",
                        "--upstream", "UPSTREAM.json", "--patches", "patches", "--cache", "cache",
                        "--dist", "rel/dist-default")
        self.assertEqual(defaulted.returncode, 0, defaulted.stderr)
        self.assertTrue((caller / "cache/work/gateway/state.json").is_file())
        for path in ("rel/work", "cache/work"):
            tool.remove_tree(caller / path)

    def test_reproducibility_command(self):
        self.seed_vendor_cache()
        self.run_tool("fetch")  # caches the downloads
        out, _ = self.run_tool("repro", "--scratch", str(self.root / "scratch"))
        self.assertTrue(json.loads(out)["identical"])

    def test_refusals(self):
        # Offline with nothing cached.
        _, err = self.run_tool("fetch", "--offline", expect=1)
        self.assertIn("offline", err)
        # A pinned hash that does not match the served bytes.
        self.recipe["toolchain"]["archives"][self.host]["sha256"] = "0" * 64
        self.write_recipe()
        _, err = self.run_tool("fetch", expect=1)
        self.assertIn("differs from the pinned", err)

    def test_vendor_tree_mismatch_and_step_order(self):
        tree = self.cache / "vendor" / self.recipe["vendor"]["tree_sha256"]
        self.seed_vendor_cache()
        (tree / "modules.txt").write_text("# tampered\n")
        self.run_tool("fetch")
        _, err = self.run_tool("vendor", "--offline", expect=1)
        self.assertIn("vendored module tree", err)
        _, err = self.run_tool("apply", expect=1)
        self.assertIn("vendor step has not run", err)
        _, err = self.run_tool("build", "fetch", expect=1)
        self.assertIn("in this order", err)
        _, err = self.run_tool("env", "build", expect=1)
        self.assertIn("run alone", err)

    def test_changed_recipe_or_patch_bytes_refuse(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor")
        (self.patches / "cli-proxy-api-second.patch").write_text(
            (self.patches / "cli-proxy-api-second.patch").read_text() + "\n")
        _, err = self.run_tool("apply", expect=1)
        self.assertIn("differs from the pinned", err)
        self.recipe["gates"][0]["expected_tests"] = ["TestAlpha", "TestBeta"]
        self.write_recipe()
        _, err = self.run_tool("apply", expect=1)
        self.assertIn("different recipe", err)

    def test_explicit_series_is_recorded_as_not_admitted_and_inspect_refuses_it(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build", "--patch", str(self.patches / "cli-proxy-api-first.patch"),
                      "--target", "linux-amd64")
        state = json.loads((self.work / "state.json").read_text())
        self.assertFalse(state["admitted_series"])
        _, err = self.run_tool("inspect", expect=1)
        self.assertIn("not built from the admitted series", err)

    def test_omission_offsets_only_for_the_admitted_series_with_patches_left_out(self):
        # first inserts two lines; second is written against them, so leaving
        # first out moves second's hunk by two lines, context unchanged.
        first = unified("cmd/server/main.go", 1, [], ["// one", "// two"], ["package main"], [""])
        second = unified("cmd/server/main.go", 5, ['\tprintln("upstream")'], ['\tprintln("upstream", "patched-marker")'],
                         ["func main() {"], ["}"])
        for name, text in (("cli-proxy-api-first.patch", first), ("cli-proxy-api-second.patch", second)):
            (self.patches / name).write_text(text)
            next(entry for entry in self.recipe["series"] if entry["basename"] == name)["sha256"] = \
                hashlib.sha256(text.encode()).hexdigest()
        self.recipe["markers"] = [{"patch": "cli-proxy-api-second.patch", "text": "patched-marker"}]
        self.write_recipe()
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor")
        only_second = ("--patch", str(self.patches / "cli-proxy-api-second.patch"))
        both = ("--patch", str(self.patches / "cli-proxy-api-first.patch"), *only_second)
        reordered = (*only_second, "--patch", str(self.patches / "cli-proxy-api-first.patch"))
        for argv, refusal in (((), "explicit --patch list"), (both, "is the admitted series"),
                              (reordered, "not the admitted series with patches left out")):
            with self.subTest(argv=argv):
                _, err = self.run_tool("apply", "--omission-offsets", *argv, expect=1)
                self.assertIn("--omission-offsets is for diagnostic omission builds only", err)
                self.assertIn(refusal, err)
        _, err = self.run_tool("apply", *only_second, expect=1)
        self.assertIn("cli-proxy-api-second.patch applies only with offsets", err)
        _, err = self.run_tool("apply", "build", "record", "--omission-offsets", *only_second, "--target", "linux-amd64")
        self.assertIn("cli-proxy-api-second.patch moved by the omitted patches", err)
        record = json.loads((self.dist / "BUILD.json").read_text())
        self.assertFalse(record["admitted_series"])
        self.assertEqual(record["moved_hunks"], {"cli-proxy-api-second.patch": ["Hunk #1 succeeded at 3 (offset -2 lines)."]})
        self.assertIn(b'"patched-marker"', (self.dist / "linux-amd64" / "cli-proxy-api").read_bytes())
        # The admitted series applies offset-free and its record names no moved hunk.
        self.run_tool("fetch", "vendor", "apply", "build", "record", "--target", "windows-amd64")
        self.assertNotIn("moved_hunks", json.loads((self.dist / "BUILD.json").read_text()))

    def test_a_tree_edited_after_apply_or_vendor_is_refused(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply")
        main = self.work / "src" / "cmd" / "server" / "main.go"
        original = main.read_bytes()
        # An implementation edit that keeps the markers, modules and licences.
        main.write_bytes(original.replace(b"func main() {", b'func main() {\n\tprintln("edited")'))
        _, err = self.run_tool("build", expect=1)
        self.assertIn("build: the source tree", err)
        self.assertIn("changed after the apply step; run the fetch step again", err)
        self.assertFalse((self.dist / "linux-amd64" / "cli-proxy-api").exists())
        _, err = self.run_tool("gates", expect=1)
        self.assertIn("changed after the apply step", err)
        main.write_bytes(original)
        self.run_tool("build")
        state = json.loads((self.work / "state.json").read_text())
        # An edit between build and record: record never publishes the
        # recorded input hashes for it.
        main.write_bytes(original + b"// edited\n")
        _, err = self.run_tool("record", expect=1)
        self.assertIn("record: the source tree", err)
        self.assertFalse((self.dist / "BUILD.json").exists())
        main.write_bytes(original)
        vendored = self.work / "src" / "vendor" / "example.dep" / "dep.go"
        vendored.write_text("package dep\n\nvar Edited = 1\n")
        for step in ("build", "gates", "record", "notices"):
            with self.subTest(step=step):
                _, err = self.run_tool(step, expect=1)
                self.assertIn(f"{step}: the vendored modules", err)
                self.assertIn("changed after the vendor step", err)
        self.assertEqual(json.loads((self.work / "state.json").read_text()), state)
        vendored.write_text("package dep\n")
        self.run_tool("notices")
        self.run_tool("record")
        record = json.loads((self.dist / "BUILD.json").read_text())
        self.assertEqual(record["post_apply_tree_sha256"], state["post_apply_tree_sha256"])
        self.assertEqual(record["targets"], state["built"])

    def test_gate_failures(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply")
        with mock.patch.dict(os.environ, {"FAKE_GO_TEST_FAIL": "1"}):
            _, err = self.run_tool("gates", expect=1)
        self.assertIn("gates failed: whole, named", err)
        self.recipe["gates"][1]["expected_tests"] = ["TestAlpha"]
        self.recipe["gates"][1]["run"] = "^(TestAlpha)$"
        self.write_recipe()
        self.run_tool("fetch", "vendor", "apply")
        _, err = self.run_tool("gates", expect=1)
        self.assertIn("selection differs", err)

    def test_notices_and_sboms_are_generated_checked_and_copied(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build")
        _, err = self.run_tool("record", expect=1)
        self.assertIn("no modules.json", err)
        self.run_tool("notices")
        inventory = json.loads((self.licenses / "modules.json").read_text())
        self.assertEqual(inventory["targets"], ["linux-amd64"])
        self.assertEqual([(module["path"], module["version"], module["license"], module["sum"])
                          for module in inventory["modules"]], [("example.dep", "v1.0.0", "MIT", MODULE_SUM)])
        self.assertEqual([entry["kind"] for entry in inventory["modules"][0]["files"]], ["license", "notice"])
        self.assertEqual((inventory["upstream"]["license"], inventory["toolchain"]["license"]), ("MIT", "BSD-3-Clause"))
        self.assertEqual(inventory["inputs"]["series"],
                         [f"{entry['basename']}@{entry['sha256']}" for entry in self.recipe["series"]])
        self.assertEqual((self.licenses / "modules/example.dep/NOTICE").read_text(), "Example notice text.\n")
        modifications = (self.licenses / "CLIProxyAPI/MODIFICATIONS.txt").read_text()
        self.assertIn("\n1. cli-proxy-api-first.patch\n", modifications)
        self.assertIn("modified  cmd/server/main.go", modifications)
        self.assertEqual(sorted(path.name for path in self.sbom.iterdir()), ["linux-amd64.cdx.json"])
        self.run_tool("inspect", "record")
        sbom = (self.dist / "linux-amd64" / "cli-proxy-api.cdx.json").read_bytes()
        self.assertEqual(sbom, (self.sbom / "linux-amd64.cdx.json").read_bytes())
        self.assertFalse((self.dist / "windows-amd64" / "cli-proxy-api.exe.cdx.json").exists())
        bom = json.loads(sbom)
        self.assertEqual([component["purl"] for component in bom["components"]],
                         ["pkg:golang/example.dep@v1.0.0", "pkg:golang/std@go9.9.9"])
        self.assertEqual(bom["components"][0]["hashes"][0]["content"], bytes(range(32)).hex())
        notices = (self.dist / "licenses" / "THIRD_PARTY_NOTICES.txt").read_text()
        self.assertIn("example.dep v1.0.0 (MIT)", notices)
        self.assertIn("Example notice text.", notices)
        self.assertEqual((self.dist / "licenses" / "modules.json").read_bytes(), (self.licenses / "modules.json").read_bytes())
        # A hand-edited text, a stale SBOM or a missing file is drift.
        original = (self.licenses / "modules/example.dep/LICENSE").read_bytes()
        (self.licenses / "modules/example.dep/LICENSE").write_bytes(original + b"edited\n")
        _, err = self.run_tool("record", expect=1)
        self.assertIn("modules/example.dep/LICENSE differs", err)
        self.assertIn("tools/build.py gateway notices", err)
        (self.licenses / "modules/example.dep/LICENSE").write_bytes(original)
        (self.sbom / "linux-amd64.cdx.json").write_bytes(sbom.replace(b"example.dep", b"example.dop"))
        _, err = self.run_tool("record", expect=1)
        self.assertIn("linux-amd64.cdx.json differs", err)
        (self.sbom / "linux-amd64.cdx.json").write_bytes(sbom)
        (self.licenses / "modules/example.dep/NOTICE").unlink()
        _, err = self.run_tool("record", expect=1)
        self.assertIn("NOTICE is missing", err)
        self.run_tool("notices")
        self.run_tool("record")

    def test_a_changed_series_or_input_makes_the_notices_stale(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build")
        self.run_tool("notices")
        self.recipe["series"] = self.recipe["series"][:1]
        self.write_recipe()
        self.run_tool("fetch", "vendor", "apply", "build", "--target", "linux-amd64")
        _, err = self.run_tool("record", "--target", "linux-amd64", expect=1)
        self.assertIn("modules.json: upstream differs", err)
        self.assertIn("modules.json: inputs differs", err)

    def test_diagnostic_series_records_no_notices_and_notices_refuse_it(self):
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build", "--patch", str(self.patches / "cli-proxy-api-first.patch"))
        _, err = self.run_tool("record")
        self.assertIn("diagnostic series", err)
        self.assertFalse((self.dist / "licenses").exists())
        _, err = self.run_tool("notices", expect=1)
        self.assertIn("diagnostic series", err)

    def test_a_module_without_a_licence_needs_an_override(self):
        (self.vendor / "example.dep" / "LICENSE").unlink()
        (self.vendor / "example.dep" / "NOTICE").unlink()
        self.recipe["vendor"]["tree_sha256"] = tool.tree_sha256(self.vendor)
        self.write_recipe()
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build")
        _, err = self.run_tool("notices", expect=1)
        self.assertIn("example.dep v1.0.0 ships no licence", err)
        self.licenses.mkdir()
        (self.licenses / "license-overrides.json").write_text(json.dumps(
            {"format": 1, "modules": {"example.dep": {"license": "MIT", "reason": "the upstream README states MIT"}}}))
        self.run_tool("notices")
        module = json.loads((self.licenses / "modules.json").read_text())["modules"][0]
        self.assertEqual((module["license"], module["license_override"], module["files"]),
                         ("MIT", "the upstream README states MIT", []))
        self.assertTrue((self.licenses / "license-overrides.json").is_file())
        self.run_tool("record")
        self.assertIn("no licence file shipped: the upstream README states MIT",
                      (self.dist / "licenses" / "THIRD_PARTY_NOTICES.txt").read_text())

    def test_a_linked_subpackages_own_licence_travels_with_its_module(self):
        # A subpackage with its own copyright and licence, a source file that
        # merely starts like a notice, and a module vendored inside the first
        # one's directory (its licence is its own, never the outer module's).
        sub = self.vendor / "example.dep" / "sub"
        sub.mkdir()
        (sub / "sub.go").write_text("package sub\n")
        (sub / "notice_response.go").write_text("package sub\n")
        (sub / "LICENSE").write_text(SUBPACKAGE_TEXT)
        inner = self.vendor / "example.dep" / "inner"
        inner.mkdir()
        (inner / "inner.go").write_text("package inner\n")
        (inner / "LICENSE").write_text(MIT_TEXT.replace("Example", "Inner Example"))
        (self.vendor / "modules.txt").write_text("# example.dep v1.0.0\nexample.dep\nexample.dep/sub\n"
                                                 "# example.dep/inner v0.1.0\nexample.dep/inner\n")
        self.recipe["vendor"]["tree_sha256"] = tool.tree_sha256(self.vendor)
        self.write_recipe()
        self.seed_vendor_cache()
        self.run_tool("fetch", "vendor", "apply", "build")
        self.run_tool("notices")
        inventory = json.loads((self.licenses / "modules.json").read_text())
        modules = {module["path"]: module for module in inventory["modules"]}
        self.assertEqual([entry["path"] for entry in modules["example.dep"]["files"]],
                         ["modules/example.dep/LICENSE", "modules/example.dep/NOTICE", "modules/example.dep/sub/LICENSE"])
        self.assertEqual(modules["example.dep"]["license"], "MIT AND BSD-3-Clause")
        self.assertEqual([entry["path"] for entry in modules["example.dep/inner"]["files"]],
                         ["modules/example.dep/inner/LICENSE"])
        self.assertEqual((self.licenses / "modules/example.dep/sub/LICENSE").read_text(), SUBPACKAGE_TEXT)
        self.assertEqual((self.licenses / "modules/example.dep/inner/LICENSE").read_text(),
                         MIT_TEXT.replace("Example", "Inner Example"))
        self.run_tool("inspect", "record")
        notices = (self.dist / "licenses" / "THIRD_PARTY_NOTICES.txt").read_text()
        self.assertIn("== sub/LICENSE\n\nCopyright 2026 Subpackage Authors.", notices)
        self.assertNotIn("notice_response", notices)
        # Notices generated without the subpackage's licence no longer
        # describe the build: record refuses them.
        listed = json.loads((self.licenses / "modules.json").read_text())
        for module in listed["modules"]:
            module["files"] = [entry for entry in module["files"] if not entry["path"].endswith("/sub/LICENSE")]
        (self.licenses / "modules.json").write_bytes(tool.inventory_bytes(listed))
        (self.licenses / "modules/example.dep/sub/LICENSE").unlink()
        _, err = self.run_tool("record", expect=1)
        self.assertIn("example.dep v1.0.0 differs", err)
        self.assertIn("modules/example.dep/sub/LICENSE is not checked in", err)

    def test_contract_command_writes_the_admitted_series(self):
        out = self.root / "contract.json"
        self.run_tool("contract", "--out", str(out))
        self.assertEqual(out.read_bytes(), tool.contract_bytes(tool.contract_document(
            self.recipe, [{"basename": entry["basename"], "sha256": entry["sha256"]} for entry in self.recipe["series"]])))
        self.assertFalse(out.read_bytes().endswith(b"\n"))
        (self.patches / "cli-proxy-api-second.patch").write_text("edited\n")
        _, err = self.run_tool("contract", "--out", str(out), expect=1)
        self.assertIn("differs from the pinned", err)

    def test_inspect_catches_a_store_reference(self):
        self.seed_vendor_cache()
        with mock.patch.dict(os.environ, {"FAKE_GO_EXTRA": "/nix/store/" + "a" * 32 + "-glibc/lib"}):
            self.run_tool("fetch", "vendor", "apply", "build", "--target", "linux-amd64")
        _, err = self.run_tool("inspect", expect=1)
        self.assertIn("Nix store", err)


class PatchFileTests(unittest.TestCase):
    def test_changes_paths_and_hunk_bodies(self):
        text = (unified("a/x.go", 1, ["-- removed comment", "--- old"], ["+++ new"], [], [])
                + "diff --git a/new.go b/new.go\nnew file mode 100644\n--- /dev/null\n+++ b/new.go\n"
                  "@@ -0,0 +1 @@\n+package x\n"
                + "diff --git a/gone.go b/gone.go\n--- a/gone.go\n+++ /dev/null\n@@ -1 +0,0 @@\n-package x\n"
                  "\\ No newline at end of file\n")
        self.assertEqual(tool.patch_files(text), [{"change": "modified", "path": "a/x.go"},
                                                  {"change": "added", "path": "new.go"},
                                                  {"change": "deleted", "path": "gone.go"}])

    def test_malformed_patches_are_refused(self):
        for text in ("no files here\n", "--- x.go\n+++ x.go\n@@ -1 +1 @@\n-a\n+b\n",
                     "--- a/x.go\n+++ b/x.go\n@@ -1 +1 @@\n-a\n+b\n+c\n"):
            with self.subTest(text=text), self.assertRaises(tool.BuildError):
                tool.patch_files(text)


def buildinfo_blob(modinfo, version="go1.26.8", inline=True):
    def varint(value):
        out = b""
        while value >= 0x80:
            out, value = out + bytes([value & 0x7F | 0x80]), value >> 7
        return out + bytes([value])

    raw = b"0w\xaf\x0c\x92t\x08\x02A\xe1\xc1\x07\xe6\xd6\x18\xe6" + modinfo.encode() + \
        b"\xf92C1\x86\x18 r\x00\x82B\x10A\x16\xd8\xf2"
    header = b"\xff Go buildinf:" + bytes([8, 2 if inline else 0]) + b"\0" * 16
    return header + varint(len(version)) + version.encode() + varint(len(raw)) + raw


class BuildInfoTests(unittest.TestCase):
    MODINFO = ("path\texample.test/m/cmd/server\nmod\texample.test/m\t(devel)\t\n"
               + "dep\tgithub.com/example/long-module-path-" + "x" * 200 + "\tv1.2.3\t\n"
               + "dep\tgolang.org/x/sys\tv0.30.0\t\nbuild\tCGO_ENABLED=0\n")

    def test_module_information_is_read_like_go_version_m(self):
        data = b"\0" * 100 + b"\xff Go buildinf: decoy" + b"\0" * 64 + buildinfo_blob(self.MODINFO) + b"tail"
        version, modinfo = tool.go_buildinfo(data)
        self.assertEqual(version, "go1.26.8")
        self.assertEqual(tool.linked_modules(modinfo),
                         [("github.com/example/long-module-path-" + "x" * 200, "v1.2.3"), ("golang.org/x/sys", "v0.30.0")])

    def test_missing_old_or_replaced_build_information_is_refused(self):
        for data in (b"plain bytes", buildinfo_blob(self.MODINFO, inline=False)):
            with self.subTest(data=data[:20]), self.assertRaises(tool.BuildError):
                tool.go_buildinfo(data)
        with self.assertRaises(tool.BuildError):
            tool.linked_modules("dep\tgithub.com/a/b\tv1.0.0\t\n=>\t../local\t\t\n")

    def test_go_sum_and_module_hashes(self):
        sums = tool.go_sum_hashes(f"a v1.0.0 {MODULE_SUM}\na v1.0.0/go.mod h1:x\n")
        self.assertEqual(sums, {("a", "v1.0.0"): MODULE_SUM})
        self.assertEqual(tool._h1_hex(MODULE_SUM), bytes(range(32)).hex())
        for value in ("h2:abc", "h1:!!", "h1:" + base64.b64encode(b"short").decode()):
            with self.subTest(value=value), self.assertRaises(tool.BuildError):
                tool._h1_hex(value)


class LicenseClassificationTests(unittest.TestCase):
    APACHE = "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/\n"
    ISC = ("Permission to use, copy, modify, and distribute this software for any\npurpose with or without "
           "fee is hereby granted, provided that the above\ncopyright notice appear in all copies.\n")
    BSD2 = ("Redistribution and use in source and binary forms, with or without\nmodification, are permitted "
            "provided that the following conditions are met:\n1. Redistributions of source code ...\n")
    MPL = "Mozilla Public License Version 2.0\n==================================\n"

    def test_single_families(self):
        for text, spdx in ((MIT_TEXT, "MIT"), (BSD3_TEXT, "BSD-3-Clause"), (self.BSD2, "BSD-2-Clause"),
                           (self.APACHE, "Apache-2.0"), (self.ISC, "ISC"),
                           (self.ISC.replace("and distribute", "and/or distribute"), "ISC"), (self.MPL, "MPL-2.0"),
                           ("Licensed under the Apache License, Version 2.0 (the License)", "Apache-2.0")):
            with self.subTest(spdx=spdx):
                self.assertEqual(tool.classify_license(text), spdx)

    def test_combined_files_list_every_family_in_order(self):
        self.assertEqual(tool.classify_license(BSD3_TEXT + "\n---\n" + self.APACHE + "\n---\n" + MIT_TEXT),
                         "BSD-3-Clause AND Apache-2.0 AND MIT")
        self.assertEqual(tool.classify_license(self.BSD2 + "\nAVL Tree:\n" + self.ISC), "BSD-2-Clause AND ISC")
        self.assertEqual(tool.classify_license(BSD3_TEXT + "\n" + BSD3_TEXT), "BSD-3-Clause")
        files = [{"kind": "license", "path": "a"}, {"kind": "license", "path": "b"}, {"kind": "notice", "path": "c"}]
        texts = {"a": self.APACHE.encode(), "b": BSD3_TEXT.encode(), "c": b"anything"}
        self.assertEqual(tool.license_of(files, texts), "Apache-2.0 AND BSD-3-Clause")

    def test_unknown_texts_are_refused(self):
        with self.assertRaises(tool.BuildError):
            tool.classify_license("All rights reserved. Do not copy.")

    def test_notice_file_names(self):
        for name, kind in (("LICENSE", "license"), ("LICENSE.txt", "license"), ("LICENCE", "license"),
                           ("LICENSE.Golang", "license"), ("COPYING", "license"), ("NOTICE", "notice"),
                           ("PATENTS", "patents"), ("COPYRIGHT", "copyright"), ("license.md", "license"),
                           ("AUTHORS", None), ("CONTRIBUTORS_GUIDE.md", None), ("LICENSES_GO", None),
                           ("README.md", None), ("notice_response.go", None), ("license.go", None),
                           ("COPYRIGHT_amd64.s", None)):
            with self.subTest(name=name):
                self.assertEqual(tool.notice_kind(name), kind)


class CommandLineTests(unittest.TestCase):
    def test_unprepared_env_fails_with_a_remedy(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(BUILD_TOOL), "gateway", "env", "--work", str(Path(tmp) / "w"),
                                     "--cache", str(Path(tmp) / "c")],
                                    capture_output=True, text=True, timeout=60, cwd=tmp,
                                    env={**os.environ, "HOME": tmp})
        self.assertEqual(result.returncode, 1)
        self.assertIn("run the fetch step first", result.stderr)
        self.assertEqual(result.stdout, "")


class BuiltGatewayComparisonTests(unittest.TestCase):
    """The Nix-built linux-amd64 gateway equals a plain build, when both exist."""

    def test_nix_and_plain_builds_are_identical(self):
        nix, dist = os.environ.get(NIX_GATEWAY_ENV), os.environ.get(DIST_GATEWAY_ENV)
        if not nix or not dist:
            self.skipTest(f"BOUNDARY: set {NIX_GATEWAY_ENV} (a Nix gateway output) and {DIST_GATEWAY_ENV} "
                          "(a tools/build.py dist directory) to compare")
        nix_binary = Path(nix) / "bin" / "cli-proxy-api"
        plain = Path(dist) / "linux-amd64" / "cli-proxy-api"
        self.assertEqual(hashlib.sha256(nix_binary.read_bytes()).hexdigest(),
                         hashlib.sha256(plain.read_bytes()).hexdigest())
        nix_record = json.loads((Path(nix) / "share" / "cli-proxy-api" / "BUILD.json").read_text())
        plain_record = json.loads((Path(dist) / "BUILD.json").read_text())
        self.assertEqual(nix_record["targets"]["linux-amd64"], plain_record["targets"]["linux-amd64"])
        for key in ("series", "source", "vendor", "post_apply_tree_sha256", "build"):
            self.assertEqual(nix_record[key], plain_record[key], key)


if __name__ == "__main__":
    unittest.main()
