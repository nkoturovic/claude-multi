"""Release signature verification (``claude_multi.trust``).

RFC 8032 Ed25519 vectors, the ``PROTOCOL.sshsig`` blob rules, allowed-signer
policy (principals, namespaces, rotation windows) and cross-checks against
``ssh-keygen -Y sign``/``-Y verify`` with throwaway keys generated per run
(no private key is stored in the tree).
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from claude_multi import trust
from _fake_release import needs_ssh_keygen

NS = trust.RELEASE_NAMESPACE
WHO = trust.RELEASE_PRINCIPAL

# RFC 8032 section 7.1: TEST 1, TEST 2, TEST 3 and TEST SHA(abc).
RFC8032_VECTORS = (
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
    (
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
    ),
)


# ---------------------------------------------------------------- test signer


def _expand(seed: bytes) -> tuple[int, bytes, bytes]:
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    public = trust._encode(trust._mul(scalar, trust.BASE))
    return scalar, digest[32:], public


def public_key(seed: bytes) -> bytes:
    return _expand(seed)[2]


def sign(seed: bytes, message: bytes) -> bytes:
    """RFC 8032 5.1.6 (test-side only; the product never signs)."""

    scalar, prefix, public = _expand(seed)
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % trust.L
    big_r = trust._encode(trust._mul(r, trust.BASE))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % trust.L
    s = (r + k * scalar) % trust.L
    return big_r + s.to_bytes(32, "little")


def _wire(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def key_blob(public: bytes, key_type: str = "ssh-ed25519") -> bytes:
    return _wire(key_type.encode()) + _wire(public)


def armor(blob: bytes) -> str:
    text = base64.b64encode(blob).decode()
    lines = [text[i:i + 70] for i in range(0, len(text), 70)]
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(lines) + "\n-----END SSH SIGNATURE-----\n"


def sshsig(seed: bytes, message: bytes, *, namespace: str = NS, hash_algorithm: str = "sha512",
           reserved: bytes = b"", version: int = 1, sig_type: str = "ssh-ed25519",
           trailing: bytes = b"", raw_override: bytes | None = None) -> str:
    public = public_key(seed)
    digest = hashlib.new(hash_algorithm, message).digest()
    to_sign = (b"SSHSIG" + _wire(namespace.encode()) + _wire(reserved)
               + _wire(hash_algorithm.encode()) + _wire(digest))
    raw = sign(seed, to_sign) if raw_override is None else raw_override
    blob = (b"SSHSIG" + version.to_bytes(4, "big") + _wire(key_blob(public)) + _wire(namespace.encode())
            + _wire(reserved) + _wire(hash_algorithm.encode()) + _wire(_wire(sig_type.encode()) + _wire(raw)))
    return armor(blob + trailing)


def signer_line(seed: bytes, *, principals: str = WHO, options: str | None = None) -> str:
    encoded = base64.b64encode(key_blob(public_key(seed))).decode()
    head = principals if options is None else f"{principals} {options}"
    return f"{head} ssh-ed25519 {encoded} comment\n"


SEED_A = hashlib.sha256(b"claude-multi test key A").digest()
SEED_B = hashlib.sha256(b"claude-multi test key B").digest()
MESSAGE = b"0" * 64 + b"  claude-multi-1.0.0-linux-x86_64.tar.gz\n"


def _utc(stamp: str) -> int:
    return calendar.timegm(time.strptime(stamp, "%Y%m%d"))


class Rfc8032VectorTests(unittest.TestCase):
    def test_vectors_derive_sign_and_verify(self) -> None:
        for seed_hex, public_hex, message_hex, signature_hex in RFC8032_VECTORS:
            seed, message = bytes.fromhex(seed_hex), bytes.fromhex(message_hex)
            with self.subTest(public=public_hex[:16]):
                self.assertEqual(public_key(seed).hex(), public_hex)
                self.assertEqual(sign(seed, message).hex(), signature_hex)
                self.assertTrue(trust.ed25519_verify(bytes.fromhex(public_hex), message, bytes.fromhex(signature_hex)))

    def test_any_altered_byte_fails(self) -> None:
        _seed, public_hex, message_hex, signature_hex = RFC8032_VECTORS[2]
        public, message, signature = (bytes.fromhex(x) for x in (public_hex, message_hex, signature_hex))
        self.assertFalse(trust.ed25519_verify(public, message + b"\x00", signature))
        self.assertFalse(trust.ed25519_verify(public, b"\xaf\x83", signature))
        for index in (0, 31, 32, 63):
            altered = bytearray(signature)
            altered[index] ^= 0x01
            with self.subTest(index=index):
                self.assertFalse(trust.ed25519_verify(public, message, bytes(altered)))
        self.assertFalse(trust.ed25519_verify(public[:31], message, signature))
        self.assertFalse(trust.ed25519_verify(public, message, signature[:63]))

    def test_non_canonical_s_is_refused(self) -> None:
        _seed, public_hex, message_hex, signature_hex = RFC8032_VECTORS[1]
        public, message, signature = (bytes.fromhex(x) for x in (public_hex, message_hex, signature_hex))
        s = int.from_bytes(signature[32:], "little")
        malleated = signature[:32] + (s + trust.L).to_bytes(32, "little")
        # The malleated S passes the group equation; only the S < L rule refuses it.
        self.assertFalse(trust.ed25519_verify(public, message, malleated))

    def test_non_canonical_and_small_order_points_are_refused(self) -> None:
        _seed, public_hex, message_hex, signature_hex = RFC8032_VECTORS[0]
        message, signature = bytes.fromhex(message_hex), bytes.fromhex(signature_hex)
        identity = (1).to_bytes(32, "little")
        self.assertFalse(trust.ed25519_verify(identity, message, signature))  # small order
        non_canonical_y = (trust.P + 1).to_bytes(32, "little")  # y >= p encodes y = 1
        self.assertFalse(trust.ed25519_verify(non_canonical_y, message, signature))
        minus_zero = (1 | (1 << 255)).to_bytes(32, "little")  # x = 0 with the sign bit set
        self.assertIsNone(trust._decode(minus_zero))
        self.assertFalse(trust.ed25519_verify(bytes.fromhex(public_hex), message, identity + signature[32:]))


class SshsigFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowed = signer_line(SEED_A)

    def test_good_signature_verifies(self) -> None:
        result = trust.verify(MESSAGE, sshsig(SEED_A, MESSAGE), self.allowed)
        self.assertEqual(result.principal, WHO)
        self.assertEqual(result.namespace, NS)
        self.assertEqual(result.fingerprint, trust.fingerprint(key_blob(public_key(SEED_A))))

    def test_tampered_message_is_refused(self) -> None:
        with self.assertRaisesRegex(trust.TrustError, "does not verify"):
            trust.verify(MESSAGE.replace(b"0", b"1", 1), sshsig(SEED_A, MESSAGE), self.allowed)

    def test_wrong_namespace_is_refused(self) -> None:
        with self.assertRaisesRegex(trust.TrustError, "namespace 'file'"):
            trust.verify(MESSAGE, sshsig(SEED_A, MESSAGE, namespace="file"), self.allowed)

    def test_wrong_key_is_refused(self) -> None:
        with self.assertRaisesRegex(trust.TrustError, "not trusted"):
            trust.verify(MESSAGE, sshsig(SEED_B, MESSAGE), self.allowed)

    def test_blob_rules(self) -> None:
        cases = {
            "sha256 prehash": (dict(hash_algorithm="sha256"), "only sha512"),
            "reserved data": (dict(reserved=b"x"), "reserved"),
            "version 2": (dict(version=2), "version 2"),
            "trailing bytes": (dict(trailing=b"\x00"), "trailing"),
            "rsa signature": (dict(sig_type="rsa-sha2-512"), "unsupported signature type"),
            "short signature": (dict(raw_override=b"\x00" * 63), "malformed Ed25519 signature"),
        }
        for label, (kwargs, reason) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(trust.TrustError, reason):
                    trust.verify(MESSAGE, sshsig(SEED_A, MESSAGE, **kwargs), self.allowed)

    def test_armor_rules(self) -> None:
        good = sshsig(SEED_A, MESSAGE)
        for label, text in {
            "no armor": good.replace("-----BEGIN SSH SIGNATURE-----\n", ""),
            "other armor": good.replace("SSH SIGNATURE", "PGP SIGNATURE"),
            "not base64": good.replace("U1NIU0lH", "U1NIU0l!"),
            "empty": "",
            "bad magic": armor(b"SSHSIX" + b"\x00" * 40),
            "truncated": armor(b"SSHSIG\x00\x00\x00\x01\x00\x00\x01\x00"),
        }.items():
            with self.subTest(label):
                with self.assertRaises(trust.TrustError):
                    trust.verify(MESSAGE, text, self.allowed)
        self.assertEqual(trust.verify(MESSAGE, "\n\n" + good.replace("\n", "\r\n"), self.allowed).line, 1)


class AllowedSignerTests(unittest.TestCase):
    def test_parse_options_and_comments(self) -> None:
        text = ("# release keys\n\n"
                + signer_line(SEED_A, options='namespaces="claude-multi-release",valid-after="20260101Z"')
                + signer_line(SEED_B, principals='"a@x,b@y"', options='cert-authority'))
        first, second = trust.parse_allowed_signers(text)
        self.assertEqual((first.line, first.namespaces, first.valid_after), (3, NS, _utc("20260101")))
        self.assertFalse(first.cert_authority)
        self.assertEqual((second.principals, second.cert_authority), ("a@x,b@y", True))

    def test_malformed_lines_refuse_the_document(self) -> None:
        good = signer_line(SEED_A).strip()
        encoded = good.split()[2]
        for label, line in {
            "unknown option": signer_line(SEED_A, options='no-touch-required'),
            "unquoted value": signer_line(SEED_A, options='valid-after=20260101'),
            "bad time": signer_line(SEED_A, options='valid-before="2026"'),
            "type mismatch": f"{WHO} ssh-rsa {encoded}",
            "not base64": f"{WHO} ssh-ed25519 !!!",
            "missing key": f"{WHO} ssh-ed25519",
            "open quote": f'"{WHO} ssh-ed25519 {encoded}',
        }.items():
            with self.subTest(label):
                with self.assertRaisesRegex(trust.TrustError, "allowed signers line"):
                    trust.parse_allowed_signers("# ok\n" + line + "\n")

    def test_principal_patterns(self) -> None:
        signature = sshsig(SEED_A, MESSAGE)
        for principals, accepted in {
            WHO: True, "release@*": True, "*": True, "other@x,release@claude-?ulti": True,
            "*,!release@claude-multi": False, "other@x": False, "RELEASE@claude-multi": False,
        }.items():
            with self.subTest(principals):
                allowed = signer_line(SEED_A, principals=principals)
                if accepted:
                    trust.verify(MESSAGE, signature, allowed)
                else:
                    with self.assertRaises(trust.TrustError):
                        trust.verify(MESSAGE, signature, allowed)

    def test_namespaces_option(self) -> None:
        signature = sshsig(SEED_A, MESSAGE)
        trust.verify(MESSAGE, signature, signer_line(SEED_A, options='namespaces="file,claude-multi-*"'))
        with self.assertRaisesRegex(trust.TrustError, "namespace not allowed"):
            trust.verify(MESSAGE, signature, signer_line(SEED_A, options='namespaces="file"'))

    def test_cert_authority_never_matches_a_plain_key(self) -> None:
        with self.assertRaises(trust.TrustError):
            trust.verify(MESSAGE, sshsig(SEED_A, MESSAGE), signer_line(SEED_A, options="cert-authority"))

    def test_rotation_statements(self) -> None:
        """The old key is valid before the cut-over, the new one after it."""

        allowed = (signer_line(SEED_A, options='valid-before="20270101Z"')
                   + signer_line(SEED_B, options='valid-after="20270101Z"'))
        old, new = sshsig(SEED_A, MESSAGE), sshsig(SEED_B, MESSAGE)
        before, after = _utc("20261215"), _utc("20270115")
        self.assertEqual(trust.verify(MESSAGE, old, allowed, now=before).line, 1)
        self.assertEqual(trust.verify(MESSAGE, new, allowed, now=after).line, 2)
        with self.assertRaisesRegex(trust.TrustError, "expired"):
            trust.verify(MESSAGE, old, allowed, now=after)
        with self.assertRaisesRegex(trust.TrustError, "not yet valid"):
            trust.verify(MESSAGE, new, allowed, now=before)
        # The boundaries are inclusive, as in OpenSSH.
        trust.verify(MESSAGE, old, allowed, now=_utc("20270101"))
        trust.verify(MESSAGE, new, allowed, now=_utc("20270101"))

    def test_parse_time_formats(self) -> None:
        self.assertEqual(trust.parse_time("20270101Z"), _utc("20270101"))
        self.assertEqual(trust.parse_time("202701010130Z"), _utc("20270101") + 5400)
        self.assertEqual(trust.parse_time("20270101013005Z"), _utc("20270101") + 5405)
        self.assertEqual(trust.parse_time("20270101"), int(time.mktime(time.strptime("20270101", "%Y%m%d"))))
        for bad in ("2027", "20271301Z", "2027010101Z", "x0270101"):
            with self.subTest(bad):
                with self.assertRaises(trust.TrustError):
                    trust.parse_time(bad)


class SumsTests(unittest.TestCase):
    def test_parse(self) -> None:
        text = f"{'a' * 64}  claude-multi-1.0.0-linux-x86_64.tar.gz\n{'b' * 64} *MANIFEST.json\n\n"
        self.assertEqual(trust.parse_sums(text), {
            "claude-multi-1.0.0-linux-x86_64.tar.gz": "a" * 64, "MANIFEST.json": "b" * 64})

    def test_refusals(self) -> None:
        for label, text in {
            "empty": "",
            "upper-case hex": f"{'A' * 64}  x.tar.gz\n",
            "path": f"{'a' * 64}  dir/x.tar.gz\n",
            "dot name": f"{'a' * 64}  .hidden\n",
            "duplicate": f"{'a' * 64}  x\n{'b' * 64}  x\n",
            "short hash": f"{'a' * 63}  x\n",
            "binary": b"\xff" * 70,
        }.items():
            with self.subTest(label):
                with self.assertRaises(trust.TrustError):
                    trust.parse_sums(text)

    def test_verify_sums(self) -> None:
        sums = f"{'c' * 64}  claude-multi-1.0.0-linux-x86_64.tar.gz\n".encode()
        entries = trust.verify_sums(sums, sshsig(SEED_A, sums), signer_line(SEED_A))
        self.assertEqual(list(entries), ["claude-multi-1.0.0-linux-x86_64.tar.gz"])
        with self.assertRaises(trust.TrustError):
            trust.verify_sums(sums + b"\n", sshsig(SEED_A, sums), signer_line(SEED_A))


class ReleaseTrustTests(unittest.TestCase):
    """The packaged release trust (``data/release-trust/allowed_signers``)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-trust-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_packaged_file_is_the_release_trust(self) -> None:
        from claude_multi import resources_root

        self.assertEqual(trust.release_trust_path(), resources_root() / "release-trust" / "allowed_signers")
        self.assertTrue(trust.release_trust_path().is_file())

    def test_the_packaged_trust_names_the_production_key(self) -> None:
        (signer,) = trust.release_signers()
        self.assertEqual((signer.principals, signer.key_type, signer.namespaces),
                         ("release@claude-multi", "ssh-ed25519", "claude-multi-release"))
        self.assertEqual((signer.valid_after, signer.valid_before, signer.cert_authority), (None, None, False))

    def test_a_build_without_a_key_refuses(self) -> None:
        # The packaged trust with its key line removed: a build that names
        # no release key.
        from _release import keyless_trust

        with self.assertRaisesRegex(trust.TrustError, "no release signing key"):
            trust.release_signers(keyless_trust(self.tmp))
        for text in ("", "# comments only\n", signer_line(SEED_A, principals="someone@else"),
                     signer_line(SEED_A, options="cert-authority")):
            with self.subTest(text=text[:30]):
                path = self.tmp / "allowed_signers"
                path.write_text(text)
                with self.assertRaisesRegex(trust.TrustError, "no release signing key"):
                    trust.release_signers(path)

    def test_a_key_is_trusted_and_verifies(self) -> None:
        path = self.tmp / "allowed_signers"
        path.write_text("# release key\n" + signer_line(SEED_A, options='namespaces="claude-multi-release"'))
        signers = trust.release_signers(path)
        self.assertEqual(len(signers), 1)
        sums = f"{'d' * 64}  MANIFEST.json\n".encode()
        self.assertEqual(list(trust.verify_sums(sums, sshsig(SEED_A, sums), signers)), ["MANIFEST.json"])
        with self.assertRaises(trust.TrustError):
            trust.verify_sums(sums, sshsig(SEED_B, sums), signers)

    def test_unreadable_or_malformed_files_refuse(self) -> None:
        with self.assertRaisesRegex(trust.TrustError, "cannot be read"):
            trust.release_signers(self.tmp / "missing")
        path = self.tmp / "allowed_signers"
        path.write_text("release@claude-multi ssh-ed25519 not-base64!\n")
        with self.assertRaises(trust.TrustError):
            trust.release_signers(path)


@needs_ssh_keygen()
class SshKeygenCrossCheckTests(unittest.TestCase):
    """Throwaway keys from ``ssh-keygen``; both verifiers must agree."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp(prefix="claude-multi-trust-"))
        os.chmod(cls.dir, 0o700)
        cls.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cls.dir), "LC_ALL": "C"}
        for name in ("release", "other"):
            cls._run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{name} test key", "-f", str(cls.dir / name))
        cls.message = cls.dir / "SHA256SUMS"
        cls.message.write_bytes(MESSAGE)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def _run(cls, *argv: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(argv, input=stdin if stdin is not None else b"", capture_output=True, env=cls.env, cwd=cls.dir, check=check, timeout=60)

    def _allowed(self, *lines: str) -> Path:
        path = self.dir / f"allowed-{len(lines)}-{abs(hash(lines))}"
        path.write_text("".join(lines))
        return path

    def _line(self, key: str, options: str | None = None) -> str:
        key_type, encoded = (self.dir / f"{key}.pub").read_text().split()[:2]
        head = WHO if options is None else f"{WHO} {options}"
        return f"{head} {key_type} {encoded}\n"

    def _sign(self, key: str, namespace: str = NS, data: bytes = MESSAGE) -> str:
        target = self.dir / f"data-{key}-{namespace}"
        target.write_bytes(data)
        self._run("ssh-keygen", "-Y", "sign", "-f", str(self.dir / key), "-n", namespace, str(target))
        return (self.dir / f"{target.name}.sig").read_text()

    def _keygen_accepts(self, signature: str, allowed: Path, data: bytes = MESSAGE,
                        verify_time: str | None = None) -> bool:
        sig_path = self.dir / "candidate.sig"
        sig_path.write_text(signature)
        argv = ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", WHO, "-n", NS, "-s", str(sig_path)]
        if verify_time:
            argv[3:3] = ["-O", f"verify-time={verify_time}"]
        return self._run(*argv, stdin=data, check=False).returncode == 0

    def _trust_accepts(self, signature: str, allowed: Path, data: bytes = MESSAGE, now: float | None = None) -> bool:
        try:
            trust.verify(data, signature, allowed.read_text(), now=now)
        except trust.TrustError:
            return False
        return True

    def test_ssh_keygen_signature_verifies(self) -> None:
        allowed = self._allowed(self._line("release", 'namespaces="claude-multi-release"'))
        signature = self._sign("release")
        self.assertTrue(self._keygen_accepts(signature, allowed))
        self.assertTrue(self._trust_accepts(signature, allowed))
        fp = self._run("ssh-keygen", "-l", "-f", str(self.dir / "release.pub")).stdout.decode().split()[1]
        self.assertEqual(trust.verify(MESSAGE, signature, allowed.read_text()).fingerprint, fp)

    def test_negatives_agree(self) -> None:
        allowed = self._allowed(self._line("release"))
        good = self._sign("release")
        cases = {
            "tampered file": (good, MESSAGE + b"x"),
            "wrong namespace": (self._sign("release", namespace="file"), MESSAGE),
            "wrong key": (self._sign("other"), MESSAGE),
        }
        for label, (signature, data) in cases.items():
            with self.subTest(label):
                self.assertFalse(self._keygen_accepts(signature, allowed, data))
                self.assertFalse(self._trust_accepts(signature, allowed, data))

    def test_rotation_agrees(self) -> None:
        allowed = self._allowed(self._line("release", 'valid-before="20270101Z"'),
                                self._line("other", 'valid-after="20270101Z"'))
        old, new = self._sign("release"), self._sign("other")
        for stamp, old_ok, new_ok in (("20261215Z", True, False), ("20270115Z", False, True)):
            now = trust.parse_time(stamp)
            with self.subTest(stamp):
                self.assertEqual(self._keygen_accepts(old, allowed, verify_time=stamp), old_ok)
                self.assertEqual(self._trust_accepts(old, allowed, now=now), old_ok)
                self.assertEqual(self._keygen_accepts(new, allowed, verify_time=stamp), new_ok)
                self.assertEqual(self._trust_accepts(new, allowed, now=now), new_ok)

    def test_ssh_keygen_accepts_the_test_signer(self) -> None:
        """The test-side blob builder matches the format ssh-keygen verifies."""

        allowed = self._allowed(signer_line(SEED_A))
        self.assertTrue(self._keygen_accepts(sshsig(SEED_A, MESSAGE), allowed))
        self.assertFalse(self._keygen_accepts(sshsig(SEED_A, MESSAGE, namespace="file"), allowed))


if __name__ == "__main__":
    unittest.main()
