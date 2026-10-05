"""The secret store seam and the shared credential policy.

Values used here are fixture dummies; none is ever expected in a message.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import compiler, proxy, secret_store, state


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-secret-store-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "secrets" / "claude.env"
        self.environ = {"HOME": str(self.root), "CLAUDE_MULTI_SECRET_ENV": str(self.path)}

    def _write(self, text: str) -> None:
        state.ensure_private_dir(self.path.parent)
        state.atomic_write(self.path, text.encode())


class FileBackendTests(_Home):
    def test_absent_file_is_an_empty_store(self) -> None:
        store = secret_store.FileSecretStore(self.path)
        self.assertIsNone(store.get("ACME_API_KEY"))
        self.assertFalse(store.is_set("ACME_API_KEY"))
        self.assertEqual(store.scan_values(), frozenset())
        self.assertFalse(store.delete("ACME_API_KEY"))
        self.assertFalse(os.path.lexists(self.path))

    def test_get_set_delete_round_trip_is_parse_preserving(self) -> None:
        self._write("# keep\nexport OTHER_KEY=dummy-other\n")
        store = secret_store.FileSecretStore(self.path)
        self.assertEqual(store.set("ACME_API_KEY", "dummy-acme-value"), len("dummy-acme-value"))
        self.assertTrue(store.is_set("ACME_API_KEY"))
        self.assertEqual(store.get("ACME_API_KEY"), "dummy-acme-value")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(store.delete("ACME_API_KEY"))
        self.assertEqual(self.path.read_text(), "# keep\nexport OTHER_KEY=dummy-other\n")
        self.assertFalse(store.is_set("ACME_API_KEY"))
        self.assertFalse(store.delete("ACME_API_KEY"))

    def test_delete_collapses_duplicates_and_refuses_bad_names(self) -> None:
        self._write("A_KEY=dummy1\nB_KEY=dummy2\n")
        store = secret_store.FileSecretStore(self.path)
        self.assertTrue(store.delete("A_KEY"))
        self.assertEqual(store.scan_values(), frozenset({"dummy2"}))
        with self.assertRaises(secret_store.SecretStoreError):
            store.delete("lower-case")

    def test_no_process_environment_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"ACME_API_KEY": "dummy-from-process-env"}):
            store = secret_store.default_store(self.environ)
            self.assertIsNone(store.get("ACME_API_KEY"))
            self.assertFalse(store.is_set("ACME_API_KEY"))
            self.assertIsNone(proxy.resolve_secret("ACME_API_KEY", environ=self.environ))

    def test_default_location(self) -> None:
        self.assertEqual(secret_store.secret_env_path({"HOME": "/h"}),
                         Path("/h/.config/claude-multi/secrets/provider-keys.env"))
        self.assertEqual(secret_store.secret_env_path({"HOME": "/h", "CLAUDE_MULTI_SECRET_ENV": "/x/y.env"}), Path("/x/y.env"))
        self.assertEqual(proxy.secret_env_path({"HOME": "/h"}), secret_store.secret_env_path({"HOME": "/h"}))

    def test_description_names_storage_not_values(self) -> None:
        self._write("ACME_API_KEY=dummy-secret-value\n")
        store = secret_store.default_store(self.environ)
        self.assertIn("private env file", store.description())
        self.assertNotIn("dummy-secret-value", store.description())
        self.assertEqual(store.path, self.path)

    def test_unsafe_file_raises_without_values(self) -> None:
        self._write("ACME_API_KEY=dummy-secret-value\n")
        os.chmod(self.path, 0o640)
        store = secret_store.FileSecretStore(self.path)
        with self.assertRaises(secret_store.SecretStoreError) as caught:
            store.get("ACME_API_KEY")
        self.assertNotIn("dummy-secret-value", str(caught.exception))
        with self.assertRaises(proxy.ProxyError):
            proxy.resolve_secret("ACME_API_KEY", environ=self.environ)

    def test_dangling_link_reads_as_empty_like_the_earlier_resolver(self) -> None:
        state.ensure_private_dir(self.path.parent)
        self.path.symlink_to(self.root / "gone.env")
        self.assertIsNone(proxy.resolve_secret("ACME_API_KEY", environ=self.environ))

    def test_malformed_value_message_carries_no_bytes(self) -> None:
        with self.assertRaises(secret_store.SecretStoreError) as caught:
            secret_store.parse_env_bytes(b"ACME_API_KEY=dummy value with spaces\n", Path("/f"))
        self.assertNotIn("dummy", str(caught.exception))
        self.assertIn("/f:1", str(caught.exception))


class _FakeStore:
    """A non-file backend: presence without any pathname."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def is_set(self, name: str) -> bool:
        return name in self.values

    def set(self, name: str, value: str) -> int:
        self.values[name] = value
        return len(value)

    def delete(self, name: str) -> bool:
        return self.values.pop(name, None) is not None

    def description(self) -> str:
        return "fixture in-memory store"

    @property
    def path(self) -> Path | None:
        return None

    def scan_values(self) -> frozenset[str]:
        return frozenset(self.values.values())


class InterfaceTests(unittest.TestCase):
    def test_non_file_fake_satisfies_the_interface(self) -> None:
        fake = _FakeStore()
        self.assertIsInstance(fake, secret_store.SecretStore)
        self.assertIsInstance(secret_store.FileSecretStore("/nonexistent/x.env"), secret_store.SecretStore)
        fake.set("ACME_API_KEY", "dummy-acme")
        self.assertTrue(fake.is_set("ACME_API_KEY"))
        self.assertIsNone(fake.path)

    def test_the_proxy_resolver_goes_through_the_store_seam(self) -> None:
        fake = _FakeStore()
        fake.set("ACME_API_KEY", "dummy-from-fake-store")
        with mock.patch.object(secret_store, "default_store", return_value=fake):
            self.assertEqual(proxy.resolve_secret("ACME_API_KEY", environ={"HOME": "/nonexistent"}), "dummy-from-fake-store")
            self.assertIsNone(proxy.resolve_secret("OTHER_KEY", environ={"HOME": "/nonexistent"}))


class CredentialPolicyTests(unittest.TestCase):
    def test_constants_are_shared_by_reference(self) -> None:
        self.assertIs(proxy.GATEWAY_ENV_DENY_NAMES, secret_store.GATEWAY_ENV_DENY_NAMES)
        self.assertIs(proxy.GATEWAY_ENV_DENY_PREFIXES, secret_store.GATEWAY_ENV_DENY_PREFIXES)
        self.assertIs(compiler.ENV_UNSET_KEEP, secret_store.ENV_UNSET_KEEP)
        self.assertIs(proxy._ASSIGNMENT, secret_store.ASSIGNMENT)
        self.assertIs(proxy._VALUE_SHAPE, secret_store.VALUE_SHAPE)
        self.assertTrue(proxy.gateway_env_denied("pgstore_dsn"))
        self.assertFalse(proxy.gateway_env_denied("ACME_API_KEY"))

    def test_secret_name_policy(self) -> None:
        refused = (
            "9KEY", "_TOKEN", "AB", "lower", "A" * 65,
            "ANTHROPIC_EXTRA", "CLAUDE_EXTRA", "X_FILE_DESCRIPTOR",
            "GITHUB_TOKEN", "MANAGEMENT_PASSWORD", "PGSTORE_DSN", "OBJECTSTORE_KEY",
            "CLAUDE_MULTI_SECRET_ENV", "CLAUDE_CODE_MESSAGING_TOKEN",
        )
        for name in refused:
            with self.subTest(name=name):
                self.assertIsNotNone(secret_store.secret_name_problem(name))
        for name in ("ACME_API_KEY", "GROQ_KEY", "Z9_TOKEN"):
            with self.subTest(name=name):
                self.assertIsNone(secret_store.secret_name_problem(name))

    def test_legacy_names_skip_only_the_new_grammar(self) -> None:
        self.assertIsNone(secret_store.secret_name_problem("9KEY", grammar=False))
        self.assertIsNotNone(secret_store.secret_name_problem("ANTHROPIC_X", grammar=False))
        self.assertIsNotNone(secret_store.secret_name_problem("CLAUDE_CODE_MESSAGING_TOKEN", grammar=False))
        # An MCP server's key is an ordinary name (no operator keep entry).
        self.assertIsNone(secret_store.secret_name_problem("EXAMPLE_MCP_API_KEY"))


class NameSafetyTests(unittest.TestCase):
    """A pasted key is refused without being echoed; names stay names."""

    @staticmethod
    def pasted() -> tuple[str, ...]:
        """Key-shaped values built at run time: the tree carries no credential."""

        body = secrets.token_hex(12)
        return ("sk-" + "ant-api03-" + body, "gh" + "p_" + body + "0123", "AK" + "IA" + body[:16].upper(),
                "a1b2c3d4e5f6g7h8i9j0k1l2", "ey" + "J" + body + ".e30.sig-value")

    def test_a_key_shaped_name_is_refused_and_never_echoed(self) -> None:
        for value in self.pasted():
            with self.subTest(value=value[:6]):
                self.assertTrue(secret_store.looks_like_key(value))
                for problem in (secret_store.secret_name_problem(value),
                                secret_store.secret_name_problem(value, grammar=False),
                                secret_store.env_keep_problem(value)):
                    self.assertIsNotNone(problem)
                    self.assertNotIn(value, problem)
                    self.assertNotIn(value[:12], problem)

    def test_an_invalid_name_is_not_repeated(self) -> None:
        for value in ("lower-case-name", "with space", "x" * 70):
            with self.subTest(value=value[:10]):
                problem = secret_store.secret_name_problem(value)
                self.assertIsNotNone(problem)
                self.assertNotIn(value, problem)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "keys.env"
            for call in (lambda: secret_store.write_env_value(target, "sk-live-value", "v"),
                         lambda: secret_store.delete_env_value(target, "sk-live-value")):
                with self.assertRaises(secret_store.SecretStoreError) as caught:
                    call()
                self.assertNotIn("sk-live-value", str(caught.exception))

    def test_ordinary_names_are_not_mistaken_for_keys(self) -> None:
        for name in ("OPENROUTER_API_KEY", "DOCS_TOOL_API_KEY", "GROQ_KEY", "Z9_TOKEN",
                     "OPENROUTER_API_KEY_2024_BACKUP", "USER_ACME_API_KEY"):
            with self.subTest(name=name):
                self.assertFalse(secret_store.looks_like_key(name))
                self.assertIsNone(secret_store.secret_name_problem(name))

    def test_the_product_keep_list_is_covered_by_the_reserved_prefixes(self) -> None:
        # No rule of its own: every product keep name is reserved, so no
        # provider credential can take one.
        for name in secret_store.ENV_UNSET_KEEP:
            with self.subTest(name=name):
                self.assertTrue(name.startswith(secret_store.RESERVED_SECRET_PREFIXES))
                self.assertIn("reserved prefix", secret_store.secret_name_problem(name, grammar=False))


class EnvKeepPolicyTests(unittest.TestCase):
    def test_only_a_users_own_api_key_names_can_be_kept(self) -> None:
        self.assertIsNone(secret_store.env_keep_problem("DOCS_TOOL_API_KEY"))
        refused = {
            "PATH": "needs no exception",
            "ANTHROPIC_EXTRA_API_KEY": "reserved prefix",
            "CLAUDE_TOOL_API_KEY": "reserved prefix",
            "PGSTORE_X_API_KEY": "gateway secret",
            "lower_api_key": "not an environment variable name",
        }
        for name, why in refused.items():
            with self.subTest(name=name):
                self.assertIn(why, secret_store.env_keep_problem(name))
        self.assertIn("provider's API-key name",
                      secret_store.env_keep_problem("ACME_API_KEY", credential_names={"ACME_API_KEY"}))



class LogRedactionTests(unittest.TestCase):
    """The one redactor of the public gateway-log surfaces."""

    def test_every_secret_shape_and_account_identifier_is_redacted(self) -> None:
        import _log_secrets

        for secret, line in _log_secrets.secrets_and_lines():
            with self.subTest(line=line[40:100]):
                shown = secret_store.redact(line)
                self.assertNotIn(secret, shown)
                self.assertIn(secret_store.REDACTED, shown)
                self.assertTrue(shown.startswith("[2026-10-02 12:00:00] [a1b2c3d4] [info ]"), shown)

    def test_ordinary_lines_are_unchanged(self) -> None:
        import _log_secrets

        for line in _log_secrets.ORDINARY:
            with self.subTest(line=line[:60]):
                self.assertEqual(secret_store.redact(line), line)

    def test_what_stays_around_a_redaction(self) -> None:
        import _log_secrets

        lines = dict((line.split("] ", 4)[-1][:24], secret_store.redact(line))
                     for _secret, line in _log_secrets.secrets_and_lines())
        shown = "\n".join(lines.values())
        for kept in ("model=claude-multi-opus provider=claude auth=<redacted>", "Authorization: <redacted>",
                     "X-Management-Key: <redacted>", "?key=<redacted>&alt=sse", "api_key=<redacted> status=401",
                     "Cookie: <redacted>", "http://<redacted>@proxy.invalid:3128"):
            self.assertIn(kept, shown)

    def test_a_structured_value_is_redacted_whole(self) -> None:
        # Quoted strings with spaces (escaped ones too), arrays of keys,
        # JSON account fields and a whole cookie header leave nothing.
        import _log_secrets

        for secrets, line in _log_secrets.structured_cases():
            with self.subTest(line=line[40:110]):
                shown = secret_store.redact(line)
                self.assertIn(secret_store.REDACTED, shown)
                for secret in secrets:
                    self.assertNotIn(secret, shown)
                self.assertTrue(shown.startswith(_log_secrets.STAMP), shown)
        # What stays around a structured value.
        shown = secret_store.redact(f'{_log_secrets.STAMP} {{"account_id": "dummy-account-7", "type": "claude"}}')
        self.assertIn('"type": "claude"', shown)
        shown = secret_store.redact(f'{_log_secrets.STAMP} {{"api_keys": [["a"], ["b"]], "port": 1}}')
        self.assertTrue(shown.endswith('"api_keys": [<redacted>], "port": 1}'), shown)
        shown = secret_store.redact(f'{_log_secrets.STAMP} proxy password="dummy pass phrase" refused')
        self.assertTrue(shown.endswith('password="<redacted>" refused'), shown)

    def test_terminal_escapes_are_removed_before_matching(self) -> None:
        # A decorated header name and escapes between a key's chunks are
        # matched as the plain text; only sanitized text comes back.
        import _log_secrets

        for secrets, line in _log_secrets.escape_cases():
            with self.subTest(line=repr(line[40:100])):
                shown = secret_store.redact(line)
                for secret in secrets:
                    self.assertNotIn(secret, shown)
                self.assertTrue(shown.startswith(_log_secrets.STAMP), shown)
                self.assertFalse(any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch == "\u200b" for ch in shown),
                                 repr(shown))
        self.assertEqual(secret_store.redact(_log_secrets.escape_cases()[2][1]),
                         f"{_log_secrets.STAMP} request headers Authorization: <redacted>")
        self.assertEqual(secret_store.redact("plain\ttext \x1b[1mbold\x1b[0m"), "plain text bold")

    def test_a_private_key_across_lines_is_redacted_whole(self) -> None:
        import _log_secrets

        body, lines = _log_secrets.private_key_lines()
        for start in range(len(lines)):
            with self.subTest(tail_from=start):
                shown = secret_store.redact_lines(lines[start:])
                self.assertEqual(len(shown), len(lines) - start)
                for piece in body:
                    self.assertNotIn(piece, "\n".join(shown))
                    self.assertNotIn(piece[:8], "\n".join(shown))
                kept = [line for line in lines[start:] if line in _log_secrets.ORDINARY]
                self.assertEqual(shown[len(shown) - len(kept):], kept)
        # One text with its line breaks reads the same way.
        joined = secret_store.redact("\n".join(lines))
        self.assertEqual(joined.split("\n"), secret_store.redact_lines(lines))
        self.assertNotIn(body[0][:8], joined)

    def test_a_value_open_at_a_line_end_continues_on_the_next_lines(self) -> None:
        import _log_secrets

        lines = [f'{_log_secrets.STAMP} body {{"api_keys": [', '    "dummy-key-one",', '    "dummy-key-two"',
                 '  ], "port": 1}', f'{_log_secrets.STAMP} token="dummy first half', 'dummy second half" ok',
                 *_log_secrets.ORDINARY[:1]]
        shown = secret_store.redact_lines(lines)
        text = "\n".join(shown)
        for secret in ("dummy-key-one", "dummy-key-two", "first half", "second half"):
            self.assertNotIn(secret, text)
        self.assertEqual(shown[3], '<redacted>, "port": 1}')
        self.assertEqual(shown[5], "<redacted> ok")
        self.assertEqual(shown[-1], _log_secrets.ORDINARY[0])

    def test_a_private_key_after_a_field_name_is_redacted_to_its_end(self) -> None:
        # The field's name stands before the key's BEGIN marker: the body
        # and the END line are redacted too, also when the key's line breaks
        # are escaped within one line, whichever line a tail starts at.
        import _log_secrets

        for lines in _log_secrets.field_key_cases():
            for start in range(len(lines)):
                with self.subTest(case=lines[0][len(_log_secrets.STAMP):60], tail_from=start):
                    shown = secret_store.redact_lines(lines[start:])
                    self.assertEqual(len(shown), len(lines) - start)
                    for piece in _log_secrets.SHORT_KEY_PIECES:
                        self.assertNotIn(piece, "\n".join(shown))
                    kept = [line for line in lines[start:] if line in _log_secrets.ORDINARY]
                    self.assertEqual(shown[len(shown) - len(kept):], kept)
        shown = secret_store.redact_lines(_log_secrets.field_key_cases()[0])
        self.assertEqual(shown[0], f"{_log_secrets.STAMP} loading private_key: {secret_store.REDACTED}")

    def test_a_tail_inside_an_unfinished_private_key_is_redacted_with_its_context(self) -> None:
        # The log ends inside a key (BEGIN and a body, no END yet): the
        # lines above a short tail are its context, so even its last line
        # alone shows nothing of the key, and from BEGIN on nothing survives.
        import _log_secrets

        for field in (False, True):
            lines = _log_secrets.unfinished_key_lines(field=field)
            for count in range(len(lines) + 2):
                with self.subTest(field=field, count=count):
                    shown = secret_store.redact_tail(lines, count)
                    self.assertEqual(len(shown), min(count, len(lines)))
                    self.assertEqual(shown, secret_store.redact_lines(lines)[len(lines) - len(shown):])
                    for piece in _log_secrets.SHORT_KEY_PIECES:
                        self.assertNotIn(piece, "\n".join(shown))
            self.assertEqual(secret_store.redact_lines(lines)[1:],
                             [secret_store.REDACTED if not field else
                              f"{_log_secrets.STAMP} loading private_key: {secret_store.REDACTED}",
                              *[secret_store.REDACTED] * (len(lines) - 2)])
            self.assertEqual(secret_store.redact("\n".join(lines)).split("\n"), secret_store.redact_lines(lines))

    def test_redaction_is_idempotent(self) -> None:
        import _log_secrets

        lines = [line for _secret, line in _log_secrets.secrets_and_lines()]
        lines += [line for _secrets, line in _log_secrets.structured_cases() + _log_secrets.escape_cases()]
        lines += _log_secrets.private_key_lines()[1] + list(_log_secrets.ORDINARY)
        lines += [line for case in _log_secrets.field_key_cases() for line in case]
        lines += [line for _secrets, line in _log_secrets.line_break_cases()]
        once = secret_store.redact_lines(lines)
        self.assertEqual(secret_store.redact_lines(once), once)



if __name__ == "__main__":
    unittest.main()
