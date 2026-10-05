"""Signed public release fixtures, no network or operator keyring."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from claude_multi import release_manifest as rm
from _layout import REPO_ROOT


class ManifestTests(unittest.TestCase):
    def document(self):
        return {"version": "1.2.3", "manifestSignatureEnforcement": "off",
                "platforms": {"linux-x64": {"checksum": "a" * 64, "size": 123}}}

    def check(self, doc):
        rm.check_manifest(json.dumps(doc).encode(), version="1.2.3",
                          platform="linux-x64", sha256="a" * 64, size=123)

    def test_checksum_size_version_and_platform(self):
        self.check(self.document())  # enforcement flag is intentionally ignored
        for field, value, message in (
            ("version", "1.2.4", "manifest names version 1.2.4"),
            ("platforms", {}, "no linux-x64 entry"),
            ("checksum", "A" * 64, "sha256 differs"),
            ("checksum", "b" * 64, "sha256 differs"),
            ("checksum", None, "sha256 differs"),
            ("size", 124, "size differs"),
            ("size", "123", "size differs"),
            ("size", 123.0, "size differs"),
            ("size", True, "size differs"),
        ):
            with self.subTest(field=field, value=value):
                doc = self.document()
                target = doc if field in ("version", "platforms") else doc["platforms"]["linux-x64"]
                target[field] = value
                with self.assertRaisesRegex(rm.ReleaseManifestMismatch, message):
                    self.check(doc)

    def test_manifest_platforms_lists_every_known_build_strictly(self):
        manifest, _signature = rm.load_local(REPO_ROOT / "tests/fixtures/release-manifest", "2.1.281")
        platforms = rm.manifest_platforms(manifest, version="2.1.281")
        self.assertEqual(len(platforms), 8)
        self.assertEqual(platforms["win32-x64"]["size"], 240767648)
        doc = self.document()
        doc["platforms"]["future-os-x64"] = {"checksum": "b" * 64, "size": 1}
        self.assertEqual(set(rm.manifest_platforms(json.dumps(doc).encode(), version="1.2.3")), {"linux-x64"})
        for field, value, message in (("checksum", "A" * 64, "invalid linux-x64 checksum"),
                                      ("size", 0, "invalid linux-x64 size"),
                                      ("size", 1.5, "invalid linux-x64 size"),
                                      ("binary", "claude.exe", "unexpected linux-x64 binary name")):
            with self.subTest(field=field, value=value):
                doc = self.document()
                doc["platforms"]["linux-x64"][field] = value
                with self.assertRaisesRegex(rm.ReleaseManifestMismatch, message):
                    rm.manifest_platforms(json.dumps(doc).encode(), version="1.2.3")
        with self.assertRaisesRegex(rm.ReleaseManifestMismatch, "no platform entries"):
            rm.manifest_platforms(json.dumps({"version": "1.2.3", "platforms": {}}).encode(), version="1.2.3")

    def test_strict_json(self):
        for raw in (b'[]', b'{"version":"1.2.3","version":"1.2.3"}', b'no json', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(rm.ReleaseManifestMismatch):
                rm.check_manifest(raw, version="1.2.3", platform="linux-x64", sha256="a" * 64, size=123)

    def test_platform_table(self):
        for arch, suffix in (("x86_64", "x64"), ("aarch64", "arm64"), ("arm64", "arm64")):
            for musl in (False, True):
                with self.subTest(arch=arch, musl=musl):
                    self.assertEqual(rm.platform_key(system="Linux", machine=arch, musl=musl),
                                     f"linux-{suffix}" + ("-musl" if musl else ""))
        for system, machine, key in (("Darwin", "arm64", "darwin-arm64"), ("Darwin", "x86_64", "darwin-x64"),
                                     ("Windows", "AMD64", "win32-x64"), ("Windows", "ARM64", "win32-arm64")):
            with self.subTest(system=system, machine=machine):
                # musl only applies on Linux
                self.assertEqual(rm.platform_key(system=system, machine=machine, musl=True), key)
        for system, machine in (("Linux", "riscv64"), ("SunOS", "x86_64")):
            with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "unsupported platform"):
                rm.platform_key(system=system, machine=machine, musl=False)
        with mock.patch.object(Path, "glob", return_value=iter([Path("/lib/ld-musl-fixture.so.1")])):
            self.assertEqual(rm.platform_key(system="linux", machine="x86_64"), "linux-x64-musl")


class SourceTests(unittest.TestCase):
    def test_local_layouts_and_missing_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "1.2.3"
            nested.mkdir()
            for path in (nested / "manifest.json", root / "manifest-1.2.4.json"):
                path.write_bytes(b"manifest")
                Path(str(path) + ".sig").write_bytes(b"signature")
            self.assertEqual(rm.load_local(root, "1.2.3"), (b"manifest", b"signature"))
            self.assertEqual(rm.load_local(root, "1.2.4"), (b"manifest", b"signature"))
            (nested / "manifest.json.sig").unlink()
            with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "offline files"):
                rm.load_local(root, "1.2.3")
            (root / "manifest-1.2.4.json").write_bytes(b"x" * (rm.MAX_BYTES + 1))
            with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "file too large"):
                rm.load_local(root, "1.2.4")

    def opener(self, payload=b"fixture", returned_url=None):
        def open_url(url, **kwargs):
            stream = io.BytesIO(payload)
            stream.geturl = lambda: returned_url or url
            return stream
        return mock.Mock(side_effect=open_url)

    def test_fetch_uses_only_expected_https_pair(self):
        opener = self.opener()
        self.assertEqual(rm.fetch("1.2.3", opener=opener), (b"fixture", b"fixture"))
        url = rm.MANIFEST_URL.format(version="1.2.3")
        self.assertEqual(opener.call_args_list, [mock.call(url, timeout=30.0), mock.call(url + ".sig", timeout=30.0)])

    def test_fetch_rejects_bad_url_before_open_and_bad_redirect_before_follow(self):
        for url in ("http://downloads.claude.ai/a", "https://example.org/a",
                    "https://downloads.claude.ai.evil/a", "https://user@downloads.claude.ai/a",
                    "https://downloads.claude.ai:443/a"):
            with self.subTest(url=url):
                opener = self.opener()
                with mock.patch.object(rm, "MANIFEST_URL", url), self.assertRaises(rm.ReleaseManifestUnavailable):
                    rm.fetch("1.2.3", opener=opener)
                opener.assert_not_called()
                with self.assertRaises(rm.ReleaseManifestUnavailable):
                    rm._ReleaseRedirect().redirect_request(None, None, 302, "", {}, url)
                with self.assertRaises(rm.ReleaseManifestUnavailable):
                    rm.fetch("1.2.3", opener=self.opener(returned_url=url))

    def test_fetch_maps_http_read_failures_without_response_contents(self):
        for error in (http.client.IncompleteRead(b"private response"),
                      http.client.LineTooLong("private response")):
            for failed_read in (0, 1):
                with self.subTest(error=type(error).__name__, failed_read=failed_read):
                    def open_url(url, **kwargs):
                        stream = io.BytesIO(b"fixture")
                        stream.geturl = lambda: url
                        if url.endswith(".sig") == bool(failed_read):
                            stream.read = mock.Mock(side_effect=error)
                        return stream
                    with self.assertRaises(rm.ReleaseManifestUnavailable) as caught:
                        rm.fetch("1.2.3", opener=open_url)
                    self.assertEqual(str(caught.exception), f"network: {type(error).__name__}")

    def test_fetch_size_cap_errors_and_path_traversal(self):
        with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "file too large"):
            rm.fetch("1.2.3", opener=self.opener(b"x" * (rm.MAX_BYTES + 1)))
        with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "network: OSError"):
            rm.fetch("1.2.3", opener=mock.Mock(side_effect=OSError("private URL")))
        opener = self.opener()
        with self.assertRaises(rm.ReleaseManifestUnavailable):
            rm.fetch("../1.2.3", opener=opener)
        opener.assert_not_called()


class SignatureTests(unittest.TestCase):
    def status(self, fingerprint=rm.KEY_FINGERPRINT):
        return ("[GNUPG:] GOODSIG BAA929FF1A7ECACE Fixture signing key\n"
                f"[GNUPG:] VALIDSIG {fingerprint} 2026-09-23 1790123456 0 4 0 1 10 00 {fingerprint}\n")

    def runner(self, status=None, returncode=0):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            root = Path(kwargs["env"]["GNUPGHOME"])
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(kwargs["timeout"], 30)
            self.assertIn("--no-options", argv)
            self.assertIn("--no-auto-key-retrieve", argv)
            self.assertEqual((root / "release.asc").read_bytes(), rm.RELEASE_KEY.encode("ascii"))
            if "--import" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, returncode, self.status() if status is None else status, "")
        return run, calls

    def test_requires_good_and_valid_signature_for_pinned_primary(self):
        runner, calls = self.runner()
        self.assertEqual(rm.verify_signature(b"manifest", b"signature", gpg="gpg", runner=runner), rm.KEY_FINGERPRINT)
        self.assertEqual(len(calls), 2)
        self.assertFalse(Path(calls[0][-1]).parent.exists())
        for status, code, message in (
            (self.status("F" * 40), 0, "signed by another key"),
            (self.status().split("\n", 1)[1], 0, "bad signature"),
            (self.status().split("\n", 1)[0], 0, "bad signature"),
            (self.status(), 1, "bad signature"),
            ("", 0, "bad signature"),
        ):
            runner, _ = self.runner(status, code)
            with self.subTest(status=status, code=code), self.assertRaisesRegex(rm.ReleaseManifestMismatch, message):
                rm.verify_signature(b"m", b"s", gpg="gpg", runner=runner)

    def test_missing_gpg_names_both_remedies(self):
        with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "install GnuPG or set CLAUDE_MULTI_GPG"):
            rm.verify_signature(b"m", b"s", gpg="missing", runner=mock.Mock(side_effect=FileNotFoundError()))
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "install GnuPG or set CLAUDE_MULTI_GPG"):
                rm.gpg_executable({"PATH": "/empty"})
        with mock.patch.object(shutil, "which", return_value="/fixture/gpg") as which:
            self.assertEqual(rm.gpg_executable({"CLAUDE_MULTI_GPG": "/fixture/gpg", "PATH": "/empty"}), "/fixture/gpg")
            which.assert_called_once_with("/fixture/gpg", path="/empty")
        with mock.patch.object(shutil, "which", return_value="/fixture/gpg") as which:
            rm.gpg_executable({"PATH": "/fixture"})
            which.assert_called_once_with("gpg", path="/fixture")

    def test_verify_release_binds_proof_to_inputs(self):
        manifest = json.dumps(ManifestTests().document()).encode()
        runner, _ = self.runner()
        proof = rm.verify_release("1.2.3", "a" * 64, 123, source=lambda _: (manifest, b"sig"),
                                  gpg="gpg", platform="linux-x64", runner=runner)
        self.assertEqual(proof, rm.ReleaseProof("1.2.3", "linux-x64", "a" * 64, 123,
                                               hashlib.sha256(manifest).hexdigest(),
                                               hashlib.sha256(b"sig").hexdigest(), rm.KEY_FINGERPRINT,
                                               {"linux-x64": {"sha256": "a" * 64, "size": 123}}))

    def test_real_public_signature_and_manifest(self):
        gpg = shutil.which("gpg")
        if gpg is None:
            self.skipTest("BOUNDARY: gpg absent; sandbox provides GnuPG")
        manifest, signature = rm.load_local(REPO_ROOT / "tests/fixtures/release-manifest", "2.1.281")
        doc = json.loads(manifest)
        entry = doc["platforms"]["linux-x64"]
        proof = rm.verify_release(doc["version"], entry["checksum"], entry["size"],
                                  source=lambda _: (manifest, signature), gpg=gpg, platform="linux-x64")
        self.assertEqual(proof.fingerprint, rm.KEY_FINGERPRINT)
        with self.assertRaisesRegex(rm.ReleaseManifestMismatch, "bad signature"):
            rm.verify_signature(manifest + b" ", signature, gpg=gpg)


class _Stalled:
    """A response whose body never arrives until it is closed (or released)."""

    def __init__(self, url, release):
        self.url, self.release, self.closed = url, release, False

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def read(self, _n=-1):
        self.release.wait(10)
        return b""

    def close(self):
        self.closed = True
        self.release.set()


class _BlockingRaw(io.RawIOBase):
    """Response bytes: the head at once, then a body read that blocks."""

    def __init__(self, head: bytes, release: threading.Event):
        self.head, self.release, self.reading = head, release, threading.Event()

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.head:
            count = min(len(buffer), len(self.head))
            buffer[:count], self.head = self.head[:count], self.head[count:]
            return count
        self.reading.set()
        self.release.wait(10)
        return 0


class UpdateTrustTests(unittest.TestCase):
    """Bounded, consent-shaped fetches; no key retrieval."""

    def test_update_fetch_total_deadline_is_bounded(self):
        # A stalled first response: the deadline abandons it (closed) and
        # the signature is never requested.
        release = threading.Event()
        self.addCleanup(release.set)
        opened = []

        def stalled(url, **kwargs):
            opened.append((url, kwargs["timeout"]))
            response = _Stalled(url, release)
            opened.append(response)
            return response

        started = time.monotonic()
        with self.assertRaises(rm.ReleaseManifestUnavailable) as caught:
            rm.fetch("1.2.3", opener=stalled, deadline=0.3)
        self.assertEqual(str(caught.exception), "network: total deadline exceeded")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([item[0] for item in opened if isinstance(item, tuple)],
                         [rm.MANIFEST_URL.format(version="1.2.3")])
        closed_by = time.monotonic() + 5
        while not opened[1].closed and time.monotonic() < closed_by:
            time.sleep(0.01)  # closed off the caller's thread
        self.assertTrue(opened[1].closed)
        # One deadline covers both requests together: a slow first answer
        # leaves the second only the remainder, then the fetch stops.
        second = threading.Event()
        self.addCleanup(second.set)
        calls = []

        def slow_then_stalled(url, **kwargs):
            calls.append((url, kwargs["timeout"]))
            if url.endswith(".sig"):
                return _Stalled(url, second)
            time.sleep(0.2)
            stream = io.BytesIO(b"manifest")
            stream.geturl = lambda: url
            return stream

        with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "^network: total deadline exceeded$"):
            rm.fetch("1.2.3", opener=slow_then_stalled, deadline=0.45)
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1][1], 0.45)
        # An already exhausted deadline (fake clock) sends nothing more.
        ticks = iter([0.0, 0.0, 100.0])
        fast = []

        def quick(url, **kwargs):
            fast.append(url)
            stream = io.BytesIO(b"x")
            stream.geturl = lambda: url
            return stream

        with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "total deadline exceeded"):
            rm.fetch("1.2.3", opener=quick, deadline=60.0, clock=lambda: next(ticks, 100.0))
        self.assertEqual(fast, [rm.MANIFEST_URL.format(version="1.2.3")])

    def test_update_fetch_deadline_never_waits_on_a_buffered_close(self):
        # A real HTTPResponse over a BufferedReader: its close() takes the
        # lock the worker's blocked read holds. The caller still returns by
        # the total deadline while that read stays blocked.
        release = threading.Event()
        self.addCleanup(release.set)
        raws = []

        class Sock:
            def makefile(self, _mode):
                raw = _BlockingRaw(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", release)
                raws.append(raw)
                return io.BufferedReader(raw)

        def opener(url, **_kwargs):
            response = http.client.HTTPResponse(Sock())
            response.begin()
            response.url = url
            return response

        started = time.monotonic()
        with self.assertRaisesRegex(rm.ReleaseManifestUnavailable, "^network: total deadline exceeded$"):
            rm.fetch("1.2.3", opener=opener, deadline=0.1)
        elapsed = time.monotonic() - started
        self.assertTrue(raws and raws[0].reading.is_set())
        self.assertFalse(release.is_set())  # the read is still blocked
        self.assertLess(elapsed, 0.5)

    def test_gpg_never_retrieves_keys(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), dict(kwargs["env"])))
            home = kwargs["env"]["GNUPGHOME"]
            self.assertEqual(kwargs["env"], {"PATH": os.defpath, "HOME": home, "GNUPGHOME": home})
            if "--import" in argv:
                # Only the vendored key, from the private ephemeral keyring.
                self.assertEqual(Path(argv[-1]).read_bytes(), rm.RELEASE_KEY.encode("ascii"))
                self.assertEqual(Path(argv[-1]).parent, Path(home))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, SignatureTests().status(), "")

        self.assertEqual(rm.verify_signature(b"manifest", b"signature", gpg="gpg", runner=run), rm.KEY_FINGERPRINT)
        self.assertEqual(len(calls), 2)
        retrieval = {"--recv-keys", "--receive-keys", "--fetch-keys", "--search-keys", "--refresh-keys",
                     "--locate-keys", "--locate-external-keys", "--keyserver", "--auto-key-retrieve",
                     "--auto-key-locate", "--auto-key-import", "--keyserver-options", "--dirmngr-program"}
        for argv, _env in calls:
            self.assertFalse(retrieval & set(argv), argv)
            for required in ("--no-options", "--batch", "--no-tty", "--no-auto-key-retrieve", "--no-autostart"):
                self.assertIn(required, argv)
            self.assertEqual(argv[argv.index("--homedir") + 1], _env["GNUPGHOME"])
        # The keyring is ephemeral: nothing remains for a later run to trust.
        self.assertFalse(Path(calls[0][1]["GNUPGHOME"]).exists())
        # A signature by a key the vendored keyring lacks is refused, never fetched.
        def unknown_key(argv, **kwargs):
            calls.append((list(argv), dict(kwargs["env"])))
            if "--import" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 2, "[GNUPG:] NO_PUBKEY BAA929FF1A7ECACE\n", "")

        with self.assertRaisesRegex(rm.ReleaseManifestMismatch, "bad signature"):
            rm.verify_signature(b"manifest", b"signature", gpg="gpg", runner=unknown_key)
        self.assertFalse(retrieval & set(calls[-1][0]))

    def test_request_plan_is_exact_and_redirects_are_same_host_only(self):
        plan = rm.request_plan("1.2.3")
        url = rm.MANIFEST_URL.format(version="1.2.3")
        self.assertEqual(plan.urls, (url, url + ".sig"))
        self.assertEqual(plan.text().splitlines(), [
            "Re-pin release-evidence request plan",
            f"  1. GET {url}",
            f"  2. GET {url}.sig",
            "Authentication: none",
            "Purpose: verify candidate 1.2.3 before executing it",
            "Same-host HTTPS redirects on downloads.claude.ai may be followed; any other "
            "destination is refused before a request; a configured HTTPS proxy is used.",
            "No keyserver access and no automatic retries.",
            "Caps: 1024 KiB per file; 60 s for both requests together.",
            "Proceed with these listed requests? [y/N] ",
        ])
        self.assertNotIn("No redirects", plan.text())
        # The stated behaviour is the opener's: a same-host HTTPS redirect is
        # followed, any other target refused before the request, and urllib's
        # proxy handler (a configured https_proxy) stays installed.
        request = urllib.request.Request(url)
        followed = rm._ReleaseRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://downloads.claude.ai/elsewhere/manifest.json")
        self.assertEqual(followed.full_url, "https://downloads.claude.ai/elsewhere/manifest.json")
        with self.assertRaises(rm.ReleaseManifestUnavailable):
            rm._ReleaseRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.example/x")
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy.invalid:3128"}):
            handlers = urllib.request.build_opener(rm._ReleaseRedirect()).handlers
        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler) for handler in handlers))
        for bad in ("../1.2.3", "1.2"):
            with self.assertRaises(rm.ReleaseManifestUnavailable):
                rm.request_plan(bad)
