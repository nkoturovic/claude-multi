"""tools/history_scan.py: names, counts and locations only, never a value.

Secret-shaped values are generated at run time, so this file carries no
credential shape and no personal identifier of its own (``test_hygiene``):
the private identifier input is a synthetic one (:data:`SYNTHETIC_INPUT`).
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import string
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import gzip
import hashlib
import time

from _layout import load_tool

history_scan = load_tool("history_scan")


def _real_looking_key() -> str:
    # 40 url-safe characters from the OS CSPRNG: high entropy, no marker.
    while True:
        body = secrets.token_urlsafe(30)
        if not re.search(r"(.)\1{5,}", body) and not any(
            marker.decode() in body.lower() for marker in history_scan.VALUE_MARKERS
        ):
            return "sk-" + body


def _unmarked(make):
    """A draw from ``make`` with no placeholder marker in it by chance
    (``test`` inside random base64, ``000000`` inside random hex), so a
    verdict test never flakes on the marker rule it does not test."""

    for _attempt in range(100):
        value = make()
        if not any(marker.decode() in value.lower() for marker in history_scan.VALUE_MARKERS):
            return value
    raise AssertionError("every draw carried a placeholder marker: the generator's fixed text has one")


def _sk(body: str, prefix: str = "") -> str:
    return "sk" + "-" + prefix + body


def _pem(label: str, der: bytes, headers: str = "") -> str:
    """A PEM block around ``der`` (64-column base64), optional armor headers."""

    text = base64.b64encode(der).decode()
    lines = "\n".join(text[i:i + 64] for i in range(0, len(text), 64))
    label = f"{label} " if label else ""
    return f"-----BEGIN {label}" + "PRIVATE KEY-----\n" + headers + lines + f"\n-----END {label}PRIVATE KEY-----\n"


def _openssh_ed25519_key() -> str:
    return _unmarked(_openssh_ed25519_draw)


def _openssh_ed25519_draw() -> str:
    """The default ``ssh-keygen`` (OpenSSH v1, unencrypted) key layout with
    random key bytes: its fixed header encodes zero length fields as runs of
    ``A``, which a run rule must not take for fill."""

    def blob(data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + data

    public, seed, check = secrets.token_bytes(32), secrets.token_bytes(32), secrets.token_bytes(4)
    private = check + check + blob(b"ssh-ed25519") + blob(public) + blob(seed + public) + blob(b"")
    private += bytes(range(1, 1 + (-len(private) % 8)))
    body = (b"openssh-key-v1\0" + blob(b"none") + blob(b"none") + blob(b"") + struct.pack(">I", 1)
            + blob(blob(b"ssh-ed25519") + blob(public)) + blob(private))
    return _pem("OPENSSH", body)


def _generated_credentials() -> dict[str, str]:
    """One run-time generated, random-looking value per credential class."""

    alnum = string.ascii_letters + string.digits

    def hex_(n: int) -> str:
        return _unmarked(lambda: secrets.token_hex(n))

    def url(n: int) -> str:
        return _unmarked(lambda: secrets.token_urlsafe(n))

    def pick(n: int) -> str:
        return _unmarked(lambda: "".join(secrets.choice(alnum) for _ in range(n)))

    def pem(label: str, headers: str = "") -> str:
        return _unmarked(lambda: _pem(label, secrets.token_bytes(600), headers))

    return {
        "hex sk key (DeepSeek, Qwen)": f"DEEPSEEK_CLAUDE_API_KEY={_sk(hex_(16))}\n",
        "hex sk-or-v1 key (OpenRouter)": f"key: {_sk(hex_(32), 'or-v1-')}\n",
        "hex bearer (the gateway token)": f"Authorization: Bearer {hex_(32)}\n",
        "short base62 bearer": f"Authorization: Bearer {pick(20)}\n",
        "base64url sk key": f"api = '{_real_looking_key()}'\n",
        "prefix-less env key (Meta)": f"META_CLAUDE_API_KEY=LLM|{pick(15)}|{url(27)}\n",
        "gateway config api-keys list": f"api-keys:\n  - \"{hex_(32)}\"\n",
        "auth JSON token": json.dumps({"refresh_token": url(40)}) + "\n",
        "vendor prefix (xAI)": "xai" + "-" + pick(80) + "\n",
        "OpenSSH private key": _openssh_ed25519_key(),
        "PEM with armor headers": pem("RSA", f"Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,{hex_(16).upper()}\n\n"),
        "PEM in a JSON string": json.dumps({"private_key": pem("")}) + "\n",
        "password in a URL": f"proxy: http://operator:{url(16)}@proxy.corp.internal:3128\n",
    }


def _digits(seed: str, count: int) -> str:
    """``count`` decimal digits derived from ``seed`` (deterministic)."""

    out, index = "", 0
    while len(out) < count:
        out += "".join(c for c in hashlib.sha256(f"{seed}:{index}".encode()).hexdigest() if c.isdigit())
        index += 1
    return out[:count]


def _hex(seed: str, count: int) -> str:
    out, index = "", 0
    while len(out) < count:
        out += hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()
        index += 1
    return out[:count]


def _separated_credentials() -> dict[str, str]:
    """Deterministic, separator-heavy credential shapes: a Slack user token
    (digit groups and a hex tail) and a dashed UUID-format API key."""

    slack = "-".join(("xox" + "p", _digits("slack-a", 12), _digits("slack-b", 13), _digits("slack-c", 13),
                      _hex("slack-d", 32)))
    uuid = "-".join((_hex("uuid-a", 8), _hex("uuid-b", 4), "4" + _hex("uuid-c", 3), "a" + _hex("uuid-d", 3),
                     _hex("uuid-e", 12)))
    return {"slack token": f"SLACK_TOKEN={slack}\n", "UUID-format API key": f'api_key = "{uuid}"\n'}


# A private identifier input of the documented format, with synthetic
# identities (a person, a lab host, a repository and a network name).
SYNTHETIC_INPUT = (
    "# synthetic identities\n"
    "\n"
    "repository-name\texample-dotrepo\n"
    "lab-host\tlab-01\\.invalid\n"
    "username\t(?i:sample-person)\n"
    "office-network\t\\bOFX\\b\n"
    "origin\tplan-word\t(?i:\\broadmaps?\\b)\n"
    "identifier-site\tLICENSE\tusername\t1\tkeep\tthe copyright holder\n"
    "origin-site\ttests/example.py\t2\tdocs\ta later pass removes them\n"
)
PRIVATE = history_scan.parse_private_input(SYNTHETIC_INPUT)


def _identifier_samples() -> dict[str, str]:
    """One literal occurrence per identifier: the synthetic private ones and
    a generic home path."""

    return {"repository-name": "example-dotrepo", "lab-host": "lab-01.invalid", "username": "Sample-Person",
            "office-network": "OFX", "home-path": "/home/" + "sampleperson/x",  # split: no site in this file
            "macos-home-path": "/Users/" + "sampleperson"}


class ClassifyTests(unittest.TestCase):
    def test_generated_key_is_a_candidate_and_placeholders_are_synthetic(self) -> None:
        key = _real_looking_key().encode()
        self.assertEqual(history_scan.classify(key[3:], b"token = ")[0], "candidate")
        for value, context, reason in (
            (b"dummy-value-for-the-fixture", b"", "placeholder marker in the value"),
            (b"abcdefabcdefabcdefabcdef", b"", "repeating pattern"),
            (b"0123456789abcdef0123456789abcdef", b"", "repeating pattern"),
            (b"aaaaaaaaaaaaaaaaaaaaaaaa", b"", "fill characters"),
            (b"zzzzzzzzzzzzzzzzzzzzzzQ1", b"", "fill characters"),
            (b"11111111-1111-1111-1111-111111111112", b"", "entropy below"),
            (b"DEEPSEEK_CLAUDE_API_KEY", b"", "words or an identifier"),
            (b"probe-fixture-gateway-token-v2", b"", "placeholder marker in the value"),
            (b"self.resolve_secret_for_provider", b"", "words or an identifier"),
            (b"gateway-token-from-the-key-file", b"", "words or an identifier"),
            (key[3:], b"# fake key for the parser test", "placeholder word beside the value"),
        ):
            with self.subTest(value=value):
                verdict, why = history_scan.classify(value, context)
                self.assertEqual(verdict, "synthetic")
                self.assertTrue(why.startswith(reason), why)

    def test_entropy_is_judged_against_the_value_alphabet(self) -> None:
        # Hex keys can never reach an absolute 4.2 bits/char, and short
        # base62 tokens rarely do; every one of these is a candidate.
        alnum = string.ascii_letters + string.digits
        for label, make in (
            ("32 hex", lambda: secrets.token_hex(16)),
            ("64 hex", lambda: secrets.token_hex(32)),
            ("20 base62", lambda: "".join(secrets.choice(alnum) for _ in range(20))),
            ("24 base36", lambda: "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))),
        ):
            with self.subTest(alphabet=label):
                verdicts = {history_scan.classify(_unmarked(make).encode(), b"")[0] for _ in range(200)}
                self.assertEqual(verdicts, {"candidate"})
        self.assertEqual(history_scan.alphabet_size(b"0123456789abcdef"), 16)
        self.assertEqual(history_scan.alphabet_size(b"Zz9"), 62)
        self.assertEqual(history_scan.alphabet_size(b"a-b_c"), 64)
        self.assertAlmostEqual(history_scan.random_entropy(4096, 16), 4.0, places=2)

    def test_every_generated_credential_class_is_a_candidate(self) -> None:
        for label, text in _generated_credentials().items():
            with self.subTest(credential=label):
                hits = list(history_scan.credential_hits(text.encode()))
                self.assertTrue(hits, label)
                self.assertTrue(all(hit[4] == "candidate" for hit in hits), hits)
                self.assertNotIn(text.strip()[-12:], repr(hits))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "BOUNDARY: ssh-keygen unavailable (the structural key covers it)")
    def test_ssh_keygen_default_format_keys_are_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ("ed25519", "rsa", "ecdsa"):
                path = Path(tmp) / f"id_{kind}"

                def keygen() -> str:
                    for leftover in (path, path.with_suffix(".pub")):
                        leftover.unlink(missing_ok=True)
                    subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", "", "-f", str(path)],
                                   check=True, capture_output=True, timeout=60,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp})
                    return path.read_text()

                hits = list(history_scan.credential_hits(_unmarked(keygen).encode()))
                with self.subTest(kind=kind):
                    self.assertEqual([(hit[0], hit[4]) for hit in hits], [("pem-private-key", "candidate")])

    def test_word_shaped_assignments_and_code_stay_synthetic(self) -> None:
        for text in (
            "DEEPSEEK_CLAUDE_API_KEY=DEEPSEEK_CLAUDE_API_KEY_VALUE\n",
            "token = runtime.gateway_token_for_session\n",
            'Attributes: map[string]string{"api_key": probeSetupTokenAPIKey},\n',  # a bare identifier
            "max_completion_tokens = entry.MaxCompletionTokens\n",  # a count, not a credential
            "Authorization: Bearer ANTHROPIC_AUTH_TOKEN_VALUE\n",
        ):
            with self.subTest(text=text):
                self.assertEqual([hit for hit in history_scan.credential_hits(text.encode())
                                  if hit[4] == "candidate"], [])

    def test_hits_never_carry_the_value(self) -> None:
        key = _real_looking_key()
        data = f"x = 1\nheaders = {{'Authorization': 'Bearer {key[3:]}'}}\napi = '{key}'\n".encode()
        hits = list(history_scan.credential_hits(data))
        self.assertEqual({hit[0] for hit in hits}, {"bearer-value", "sk-key"})
        self.assertTrue(all(hit[4] == "candidate" for hit in hits), hits)
        self.assertEqual({hit[1] for hit in hits}, {2, 3})
        self.assertNotIn(key[3:], repr(hits))

    def test_pem_header_without_body_is_synthetic(self) -> None:
        header = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
        for text in (header, '"' + header.replace("\n", "\\n") + '"\n'):
            hits = list(history_scan.credential_hits(text.encode()))
            self.assertEqual([(hit[0], hit[4]) for hit in hits], [("pem-private-key", "synthetic")])

    def test_identifier_counts_attribute_each_occurrence_once(self) -> None:
        samples = _identifier_samples()
        text = " ".join(samples.values()).encode()
        counts = history_scan.identifier_counts(text, PRIVATE)
        self.assertEqual(dict(counts), {name: 1 for name in samples})
        self.assertEqual(history_scan.identifier_counts(b"nothing personal here", PRIVATE), {})
        # Without the private input only the generic identifiers count.
        self.assertEqual(dict(history_scan.identifier_counts(text)), {"home-path": 1, "macos-home-path": 1})
        # Neutral identities are not personal.
        for name in history_scan.NEUTRAL_USERS:
            self.assertEqual(history_scan.identifier_counts(f"/home/{name}/.config /Users/{name}/x".encode()), {})


class PrivateInputTests(unittest.TestCase):
    """The private identifier input: its format, and refusals that name a
    line, never its content."""

    def test_the_documented_format(self) -> None:
        self.assertEqual([label for label, _pattern in PRIVATE.identifiers],
                         ["repository-name", "lab-host", "username", "office-network"])
        self.assertEqual(PRIVATE.origin, (("plan-word", r"(?i:\broadmaps?\b)"),))
        self.assertEqual(dict(PRIVATE.identifier_sites), {("LICENSE", "username"): (1, "keep", "the copyright holder")})
        self.assertEqual(dict(PRIVATE.origin_sites), {"tests/example.py": (2, "docs", "a later pass removes them")})
        self.assertEqual(history_scan.parse_private_input(""), history_scan.PrivateInput())

    def test_malformed_entries_are_refused_by_line(self) -> None:
        secret_word = "zz-private-value"
        for text, line in ((f"{secret_word}\n", 1), (f"# c\nlabel\t{secret_word}\textra\n", 2),
                           (f"Bad Label\t{secret_word}\n", 1), (f"label\t({secret_word}\n", 1),
                           (f"origin\tlabel\n", 1), (f"identifier-site\tp\tl\tmany\tkeep\twhy\n", 1),
                           (f"a\t{secret_word}\na\tother\n", None)):
            with self.subTest(text=text):
                with self.assertRaises(history_scan.PrivateInputError) as caught:
                    history_scan.parse_private_input(text)
                self.assertNotIn(secret_word, str(caught.exception))
                if line is not None:
                    self.assertIn(f"line {line}:", str(caught.exception))

    def test_a_private_label_may_extend_a_generic_one(self) -> None:
        # The private list may name its own home path under the generic
        # label: both patterns count under it, and a place both match
        # counts once.
        home = "/" + "home/"
        private = history_scan.parse_private_input("home-path\t/srv/people/[a-z]+\n")
        text = f"/srv/people/alice and {home}somebody/x".encode()
        self.assertEqual(dict(history_scan.identifier_counts(text, private)), {"home-path": 2})
        private = history_scan.parse_private_input(f"home-path\t{home}somebody\n")
        self.assertEqual(dict(history_scan.identifier_counts(f"{home}somebody/x".encode(), private)),
                         {"home-path": 1})

    def test_the_environment_names_the_file(self) -> None:
        self.assertIsNone(history_scan.private_input_from_env({}))
        self.assertIsNone(history_scan.private_input_from_env({history_scan.PRIVATE_INPUT_ENV: ""}))
        with tempfile.TemporaryDirectory(prefix="cm-private-input-") as tmp:
            path = Path(tmp) / "identifiers.tsv"
            path.write_text(SYNTHETIC_INPUT)
            self.assertEqual(history_scan.private_input_from_env({history_scan.PRIVATE_INPUT_ENV: str(path)}),
                             PRIVATE)
            with self.assertRaises(history_scan.PrivateInputError):
                history_scan.private_input_from_env({history_scan.PRIVATE_INPUT_ENV: str(path) + ".missing"})


class SeparatorEntropyTests(unittest.TestCase):
    """A separator never enlarges the alphabet a value is judged against."""

    def test_separated_credentials_are_candidates(self) -> None:
        for label, text in _separated_credentials().items():
            with self.subTest(credential=label):
                self.assertFalse(any(marker.decode() in text.lower() for marker in history_scan.VALUE_MARKERS))
                hits = list(history_scan.credential_hits(text.encode()))
                self.assertTrue(hits, label)
                self.assertEqual({hit[4] for hit in hits}, {"candidate"}, hits)

    def test_the_whole_value_against_a_base64_alphabet_would_clear_them(self) -> None:
        # The failure this guards against: with the separators counted, the
        # value is judged against a 64-symbol alphabet and looks far less
        # random than its letters and digits are.
        value = _separated_credentials()["UUID-format API key"].split('"')[1].encode()
        naive = history_scan.entropy(value) / history_scan.random_entropy(len(value), 64)
        self.assertLess(naive, history_scan.relative_entropy(value))
        self.assertEqual(history_scan.alphabet_size(history_scan.alphanumeric_core(value)), 16)

    def test_separated_placeholders_stay_synthetic(self) -> None:
        for value in (b"00000000-0000-0000-0000-000000000000", b"11111111-1111-1111-1111-111111111112",
                      ("xox" + "p-" + "-".join(("1" * 12, "1" * 13, "1" * 13))).encode(),
                      b"key-for-the-gateway-in-tests"):
            with self.subTest(value=value):
                self.assertEqual(history_scan.classify(value, b"")[0], "synthetic")


class CredentialUrlTests(unittest.TestCase):
    """The credential-URL matcher runs in linear time on long scheme-character
    runs and still recognises credential-bearing URLs."""

    def test_long_dotted_dashed_and_plus_runs_scan_in_linear_time(self) -> None:
        # Long scheme-character runs, and long or repeated user information
        # after a scheme (no ":", no "@", many ":"), which the unbounded user
        # name and password must also read in linear time.
        for prefix, unit in ((b"", b"a."), (b"", b"a-"), (b"", b"a+"), (b"", b"ab.1-"), (b"a://", b"x"),
                             (b"a://u:", b"p"), (b"a://", b"x:"), (b"", b"a://_:"), (b"", b"a://a:a"),
                             (b"", b"_a:")):
            with self.subTest(prefix=prefix, unit=unit):
                small, large = prefix + unit * 40_000, prefix + unit * 160_000
                times = []
                for data in (small, large):
                    best = None
                    for _ in range(3):
                        started = time.perf_counter()
                        list(history_scan.credential_hits(data))
                        elapsed = time.perf_counter() - started
                        best = elapsed if best is None else min(best, elapsed)
                    times.append(best)
                # Four times the input: linear is about 4x, quadratic 16x.
                self.assertLess(times[1], 5.0, times)
                self.assertLess(times[1], 10 * times[0] + 0.05, times)

    def test_credential_bearing_urls_are_recognised(self) -> None:
        password = _hex("url-password", 20)
        for text in (f"DATABASE_URL=postgres://app:{password}@db.internal:5432/app",
                     f"remote git+https://deploy:{password}@git.internal/repo.git",
                     f"x.y-z proxy=http://u:{password}@proxy.internal:3128"):
            with self.subTest(text=text[:20]):
                hits = [hit for hit in history_scan.credential_hits(text.encode()) if hit[0] == "credential-url"]
                self.assertEqual([hit[4] for hit in hits], ["candidate"])
        # A new match never starts inside a run of scheme characters.
        pattern = next(shape.pattern for shape in history_scan.SHAPES if shape.name == "credential-url")
        url = b"ab://u:" + password.encode() + b"@h"
        self.assertIsNotNone(pattern.search(url, 0))
        self.assertIsNone(pattern.search(url, 1))  # "b://…" sits inside the run "ab"

    def test_long_user_names_and_passwords_are_recognised(self) -> None:
        # No length limit on either part: a long credential is never dropped.
        for user_length, password_length in ((3, 255), (3, 256), (3, 257), (3, 512), (3, 4096),
                                             (300, 40), (1000, 600)):
            user = _hex("url-user", user_length)
            password = _hex("url-password-long", password_length)
            text = f"DATABASE_URL=postgresql://{user}:{password}@database.invalid/db\n"
            with self.subTest(user=user_length, password=password_length):
                hits = [hit for hit in history_scan.credential_hits(text.encode()) if hit[0] == "credential-url"]
                self.assertEqual([(hit[2], hit[4]) for hit in hits], [(password_length, "candidate")])


class DisclosureTests(unittest.TestCase):
    """What the documentation says the scan prints is what it prints."""

    def result(self) -> object:
        result = history_scan.ScanResult()
        history_scan._note_identifiers(
            result, b"see build.example.lan, ops@example.org and 10.1.2.3 or 192.168.4.5\n", "notes.md", "c" * 40)
        return result

    def test_lan_hosts_and_mail_domains_are_listed_and_addresses_counted(self) -> None:
        result = self.result()
        summary = history_scan.summary(result)
        text = history_scan.render_text(result, max_locations=5)
        self.assertEqual(summary["lan_hosts"], {"build.example.lan": 1})
        self.assertEqual(summary["email_domains"], {"example.org": 1})
        self.assertEqual(summary["private_ipv4_occurrences"], 2)
        produced = json.dumps(summary) + text
        self.assertIn("build.example.lan", produced)
        self.assertIn("example.org", produced)
        for address in ("10.1.2.3", "192.168.4.5"):
            self.assertNotIn(address, produced)
        self.assertNotIn("ops@", produced)

    def test_the_documentation_matches_the_output(self) -> None:
        doc = " ".join(history_scan.__doc__.split())
        self.assertIn("``.lan`` host names and e-mail domains are listed by name with their counts", doc)
        self.assertIn("private IPv4 addresses are counted only, never listed", doc)
        self.assertNotIn("counted, never printed", doc)

    def test_limits_are_stated_in_the_help_and_the_report(self) -> None:
        limits = " ".join(history_scan.LIMITS.split())
        for boundary in ("not that no secret is present", "placeholder word", "is not decoded",
                         "passphrase made only of words"):
            self.assertIn(boundary, limits)
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            history_scan.main(["--help"])
        self.assertIn("Limits: the scan is a heuristic.", out.getvalue())
        report = history_scan.render_text(history_scan.ScanResult(), max_locations=1)
        self.assertIn("a clean result is not proof that no secret is present", report)
        self.assertIn("0 uninspected", report)

    def test_documented_boundaries_hold(self) -> None:
        # The blind spots the limits name are real, not corrected silently.
        key = _real_looking_key().encode()[3:]
        self.assertEqual(history_scan.classify(key, b"# example value below"),
                         ("synthetic", "placeholder word beside the value"))
        self.assertEqual(history_scan.classify(b"correct-horse-battery-staple-orbit", b"")[0], "synthetic")
        self.assertTrue(history_scan.uninspected(gzip.compress(b"token = " + key)))
        self.assertTrue(history_scan.uninspected(b"PK\x03\x04" + key))
        self.assertTrue(history_scan.uninspected(b"\x7fELF\x00\x00" + key))
        self.assertFalse(history_scan.uninspected(b"plain text " + key))
        # Encoded text is neither decoded nor counted: a base64 credential
        # document yields no hit and is not uninspected, as the limits say.
        encoded = base64.b64encode(b"api_key = " + key + b"\n")
        self.assertTrue(list(history_scan.credential_hits(b"api_key = " + key + b"\n")))
        self.assertEqual(list(history_scan.credential_hits(encoded)), [])
        self.assertFalse(history_scan.uninspected(encoded))
        self.assertIn("encoded text (base64, hex dumps) is not decoded and not counted",
                      " ".join(history_scan.LIMITS.split()))


class HistoryScanGitTests(unittest.TestCase):
    """A disposable repository: a key added then deleted stays in history."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("BOUNDARY: git unavailable (the scan reads git objects)")

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-history-scan-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
        }
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.root)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict[str, str | None], message: str) -> str:
        for name, text in files.items():
            path = self.repo / name
            if text is None:
                self.git("rm", "-q", name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_scan(self, *extra: str) -> tuple[int, str, Path]:
        out = self.root / "out"
        if out.exists():
            shutil.rmtree(out)
        stream, errors = io.StringIO(), io.StringIO()
        old = dict(os.environ)
        try:
            # Only the test names a private input (never the caller's environment).
            os.environ.pop(history_scan.PRIVATE_INPUT_ENV, None)
            os.environ.update(self.env)
            with contextlib.redirect_stderr(errors):
                code = history_scan.main(["--repo", str(self.repo), "--out", str(out), *extra], stdout=stream)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, stream.getvalue() + errors.getvalue(), out

    def identifiers_file(self) -> Path:
        path = self.root / "identifiers.tsv"
        path.write_text(SYNTHETIC_INPUT)
        path.chmod(0o600)
        return path

    def test_deleted_key_is_found_by_location_and_never_printed(self) -> None:
        key = _real_looking_key()
        samples = _identifier_samples()
        first = self.commit({"conf/a.txt": f"token: {key}\n", "notes.md": samples["lab-host"] + "\n"}, "add")
        self.commit({"conf/a.txt": None, "b.txt": "sk-" + "dummy-key-for-the-scan-test\n"},
                    "remove the key; " + samples["repository-name"])
        self.git("tag", "-a", "v1", "-m", "release")
        code, text, out = self.run_scan("--identifiers", str(self.identifiers_file()))
        self.assertEqual(code, 1)
        produced = text + "".join(p.read_text() for p in sorted(out.iterdir()))
        self.assertNotIn(key, produced)
        self.assertNotIn(key[3:], produced)
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o700")
        self.assertTrue(all((p.stat().st_mode & 0o777) == 0o600 for p in out.iterdir()))
        hits = json.loads((out / "credentials.json").read_text())
        candidate = [hit for hit in hits if hit["verdict"] == "candidate"]
        self.assertEqual(len(candidate), 1)
        self.assertEqual((candidate[0]["shape"], candidate[0]["commit"], candidate[0]["path"], candidate[0]["line"]),
                         ("sk-key", first, "conf/a.txt", 1))
        self.assertTrue(any(hit["verdict"] == "synthetic" and hit["path"] == "b.txt" for hit in hits))
        self.assertIn("CANDIDATE sk-key blob", text)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual((summary["commits"], summary["annotated_tags"], summary["unattributed_blobs"],
                          summary["uninspected_blobs"]), (2, 1, 0, 0))
        self.assertEqual(summary["credentials"]["candidate"], 1)
        self.assertEqual(summary["identifiers"]["lab-host"], {"occurrences": 1, "blobs": 1, "paths": 1})
        self.assertEqual(summary["message_identifiers"], {"commit-message:repository-name": 1})
        self.assertIn("the generic ones and a private list", text)
        self.assertNotIn("lab-01", text)
        identifiers = json.loads((out / "identifiers.json").read_text())
        self.assertEqual(identifiers["lab-host"]["notes.md"]["first_commit"], first)

    def test_hex_key_exits_one_and_paths_count_identifiers(self) -> None:
        samples = _identifier_samples()
        self.commit({f"{samples['username']}.conf/env": f"QWEN_CLAUDE_API_KEY={_sk(secrets.token_hex(16))}\n"},
                    "add")
        # The environment names the private input when --identifiers does not.
        self.env[history_scan.PRIVATE_INPUT_ENV] = str(self.identifiers_file())
        code, text, out = self.run_scan()
        self.assertEqual(code, 1, text)
        hits = json.loads((out / "credentials.json").read_text())
        self.assertEqual([(hit["shape"], hit["verdict"]) for hit in hits], [("sk-key", "candidate")])
        summary = json.loads((out / "summary.json").read_text())
        # The tree and the blob path both carry the identifier in their names.
        self.assertEqual(summary["path_identifiers"], {"username": 2})
        self.assertEqual(summary["ref_identifiers"], {})
        self.assertIn("path identifiers: username 2", text)

    def test_compressed_blobs_are_counted_as_uninspected(self) -> None:
        (self.repo / "data.gz").write_bytes(gzip.compress(b"token = " + _real_looking_key().encode()))
        self.git("add", "data.gz")
        self.git("commit", "-q", "-m", "data")
        code, text, out = self.run_scan()
        self.assertEqual(code, 0, text)  # not decoded: no candidate, but counted
        self.assertEqual(json.loads((out / "summary.json").read_text())["uninspected_blobs"], 1)
        self.assertIn("1 uninspected (binary, compressed or archived)", text)

    def test_a_pull_request_range_scans_what_it_adds_and_fails_on_a_candidate(self) -> None:
        # The base branch carries an old candidate; the pull request adds a new
        # one on its own branch. The range scan sees exactly the new one.
        old_key, new_key = _real_looking_key(), _real_looking_key()
        self.commit({"old.env": f"token: {old_key}\n"}, "old")
        base = self.commit({"old.env": None, "README": "clean\n"}, "clean main")
        self.git("checkout", "-q", "-b", "feature")
        added = self.commit({"conf/new.env": f"token: {new_key}\n"}, "add a key")
        head = self.commit({"conf/new.env": None, "x.txt": "later\n"}, "remove it again")
        code, text, out = self.run_scan("--range", f"{base}..{head}")
        self.assertEqual(code, 1, text)
        self.assertIn(f"history scan of the range {base}..{head}", text)
        hits = [hit for hit in json.loads((out / "credentials.json").read_text()) if hit["verdict"] == "candidate"]
        self.assertEqual([(hit["path"], hit["commit"]) for hit in hits], [("conf/new.env", added)])
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual((summary["commits"], summary["refs"]), (2, 0))
        self.assertNotIn(new_key, text)
        # A range that adds nothing secret passes; the full scan still finds both.
        self.git("checkout", "-q", "main")
        clean = self.commit({"docs.md": "words\n"}, "docs")
        self.assertEqual(self.run_scan("--range", f"{base}..{clean}")[0], 0)
        code, _text, out = self.run_scan()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((out / "summary.json").read_text())["credentials"]["candidate"], 2)

    def test_an_unavailable_or_malformed_range_is_an_error(self) -> None:
        head = self.commit({"a.txt": "a\n"}, "a")
        for spec in (f"{'0' * 40}..{head}", f"{head}..nosuchref", head, f"{head}...{head}", "--all..HEAD",
                     f"{head}..{head}..{head}"):
            with self.subTest(spec=spec):
                code, text, _out = self.run_scan(f"--range={spec}")
                self.assertEqual(code, 2, text)
                self.assertIn("history scan:", text)

    def test_without_a_private_input_the_generic_identifiers_run(self) -> None:
        samples = _identifier_samples()
        self.commit({"notes.md": f"{samples['lab-host']} {samples['home-path']}\n"}, "notes")
        code, text, out = self.run_scan()
        self.assertEqual(code, 0, text)
        self.assertIn("the generic ones only (no private list given)", text)
        self.assertEqual(json.loads((out / "summary.json").read_text())["identifiers"],
                         {"home-path": {"occurrences": 1, "blobs": 1, "paths": 1}})

    def test_an_unusable_private_input_is_an_error(self) -> None:
        self.commit({"a.txt": "a\n"}, "a")
        broken = self.root / "broken.tsv"
        broken.write_text("zz-private-value\n")
        for extra in (("--identifiers", str(broken)), ("--identifiers", str(self.root / "missing.tsv"))):
            with self.subTest(extra=extra):
                code, text, _out = self.run_scan(*extra)
                self.assertEqual(code, 2, text)
                self.assertIn("history scan: private identifier input:", text)
                self.assertNotIn("zz-private-value", text)

    def test_only_synthetic_sentinels_exit_zero(self) -> None:
        self.commit({"t.py": "KEY = 'sk-" + "fake-value-for-tests-only'\n"}, "fixture")
        code, text, _out = self.run_scan()
        self.assertEqual(code, 0, text)
        self.assertIn("0 candidates", text)


if __name__ == "__main__":
    unittest.main()
