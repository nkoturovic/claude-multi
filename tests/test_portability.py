"""Operator-state portability.

Every test runs on the hermetic operator fixture runtime of ``test_cli``
(temporary HOME/XDG roots, fake smoke transport, instant reload): no
provider call, no live gateway, no operator state of this host.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, portability, profile as profile_mod, served_plan, sessions, state, strict_json
from claude_multi import validate
from claude_multi import operator as operator_mod
from claude_multi.cli import consent as consent_mod
from claude_multi.cli import entry
from claude_multi.cli import gateway_facts
import claude_multi.cli.commands.portability as portability_cmd
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.runtime as runtime_mod
import claude_multi.secret_store

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
import test_cli as cli_tests
import test_served_plan as served_tests
import test_openai_compat_keyed as keyed

SECRET_VALUES = ("acme-dummy-value", "cli-test-dummy")


def inventory(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ``root`` with its mode and (for files) bytes."""

    found: dict[str, tuple[int, bytes | None]] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        found[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), data)
    return found


class _Answers(io.StringIO):
    """Scripted y/N answers; ``on_apply`` runs when the final question is asked."""

    def __init__(self, answers, on_apply=None):
        super().__init__()
        self.answers = list(answers)
        self.asked: list[str] = []
        self.on_apply = on_apply

    def confirm(self, text):
        self.asked.append(text)
        if text == portability_cmd.APPLY_QUESTION and self.on_apply is not None:
            self.on_apply()
        return self.answers.pop(0)


class PortabilityCase(cli_tests.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp(prefix="cm-portability-out-"))
        os.chmod(self.outside, 0o700)
        self.addCleanup(shutil.rmtree, self.outside, True)

    # -- source host ---------------------------------------------------------
    def admitted_source(self) -> None:
        """acme (approved route), custom-acme-small declared and admitted."""

        self.declare_small()
        self.serve_current()
        code, out, err = self.op(["models", "qualify", "custom-acme-small", "--smoke"], "y\n")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.op(["models", "admit", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, out + err)

    def export_json(self, *extra) -> tuple[dict, str, str]:
        code, out, err = self.op(["export", *extra])
        self.assertEqual(code, 0, err)
        return json.loads(out), out, err

    def bundle_file(self, document: dict, name: str = "bundle.json") -> Path:
        path = self.outside / name
        path.write_bytes(strict_json.pretty_file_bytes(document))
        os.chmod(path, 0o600)
        return path

    # -- target host ---------------------------------------------------------
    def fresh_target(self) -> None:
        """Forget this host's operator configuration (a new machine): the
        gateway token and render stay, so the target can still render."""

        for path in (self.pdir, operator_mod.ledger_path(self.env), operator_mod.evidence_path(self.runtime.gateway_environ()),
                     sessions.config_root(self.runtime.environ) / "settings.json",
                     profile_mod.bindings_path(self.runtime.environ), profile_mod.profiles_dir(self.runtime.environ)):
            if path.is_dir():
                shutil.rmtree(path)
            elif os.path.lexists(path):
                path.unlink()

    def importing(self, path: Path, answers=(), *, apply: bool = False, on_apply=None, env=None):
        stream = _Answers(list(answers), on_apply)
        err = io.StringIO()
        out = io.StringIO()
        argv = ["import", str(path)] + (["--apply"] if apply else [])
        with mock.patch.object(consent_mod, "stdio_ttys", return_value=True), \
                mock.patch.dict(self.runtime.environ, env or {}), contextlib.redirect_stderr(err), \
                mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("import ran smoke")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("import ran qualification")):
            code = cli.main(argv, runtime=self.runtime, input_stream=stream, output_stream=out, interactive=True)
        return code, out.getvalue(), err.getvalue(), stream


class ExportTests(PortabilityCase):
    def test_export_schema_excludes_secrets_records_and_evidence(self) -> None:
        self.admitted_source()
        self.save_session(mode="durable", scope_generation=1)
        evidence = operator_mod.evidence_path(self.runtime.gateway_environ())
        self.assertTrue(evidence.exists())
        # Never a credential read: the screen is shape-only.
        with mock.patch.object(claude_multi.secret_store, "default_store",
                               side_effect=AssertionError("export read the credential store")):
            document, raw, err = self.export_json()
        self.assertEqual(err.splitlines(), list(portability.EXPORT_SUMMARY))
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / portability.SCHEMA_NAME)
        self.assertEqual(validate.validate(document, schema, "$"), [])
        self.assertEqual(set(document), {"format", "version", "exported_by", "profiles", "bindings", "settings",
                                         "providers", "trust_requests", "reconnect"})
        # What to connect again elsewhere: key names and signed-in providers only.
        self.assertEqual(document["reconnect"], {"api_keys": ["ACME_API_KEY"], "sign_ins": []})
        for value in (*SECRET_VALUES, cli_tests.FIXTURE_GATEWAY_TOKEN, cli_tests.FIXED_ID):
            self.assertNotIn(value, raw)
        for excluded in ('"rd"', "approved_at", '"digest"', "aliases", "tombstones", "removed", "pruned",
                         "checks", "lineup_generation", "managed_id", "admitted_lines", "native-contract",
                         "claude_feedback_drafts", "config.yaml"):
            self.assertNotIn(excluded, raw)
        self.assertEqual(document["trust_requests"], {
            "routes": ["acme"], "admissions": ["custom-acme-small"], "transport_choices": {}})
        self.assertEqual(set(document["providers"]), {"acme"})
        self.assertEqual(document["providers"]["acme"]["provider"]["auth"]["secret_ref"], "env:ACME_API_KEY")
        # Virtual seeds: only the selected default travels, as a reference.
        self.assertEqual(document["profiles"], {catalog.DEFAULT_SEED: {"seed": {
            "id": catalog.DEFAULT_SEED, "version": self.runtime.catalog.seed_profiles[catalog.DEFAULT_SEED]["seed"]["version"]}}})
        self.assertEqual(portability.load_export(raw.encode(), asset_root=FIXTURE_ROOT).document, document)
        # The closed schema: no ledger, evidence or secret member is accepted.
        for member, value in (("ledger", {}), ("evidence", {}), ("secrets", {"ACME_API_KEY": "x"})):
            with self.subTest(member=member), self.assertRaisesRegex(portability.PortabilityError, "invalid"):
                portability.load_export(json.dumps({**document, member: value}).encode(), asset_root=FIXTURE_ROOT)
        # A secret-shaped literal anywhere refuses (never exported, never imported).
        leaked = copy.deepcopy(document)
        leaked["providers"]["acme"]["notes"] = "key sk-ant-api03-" + "A" * 40
        with self.assertRaisesRegex(portability.PortabilityError, "secret-like literal"):
            portability.load_export(json.dumps(leaked).encode(), asset_root=FIXTURE_ROOT)
        # An invalid source refuses the whole export (never a partial bundle).
        (self.pdir / "broken.json").write_text("{not json")
        os.chmod(self.pdir / "broken.json", 0o600)
        code, out, err = self.op(["export"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("export refused: providers.d has problems", err)

    def test_export_stdout_has_no_side_effects(self) -> None:
        self.admitted_source()
        before = inventory(self.root)
        for argv in (["export"],):
            with self.subTest(argv=argv):
                code, out, _err = self.op(argv)
                self.assertEqual(code, 0)
                self.assertTrue(out)
                self.assertEqual(inventory(self.root), before)
        self.assertFalse(portability.receipt_path(self.runtime.session_store.root).exists())
        # The read-only Runtime branch: no shim refresh, no state writes.
        bundle = self.bundle_file(self.export_json()[0])
        for argv, read_only in ((["export"], True), (["import", str(bundle)], True),
                                (["export", "--out", str(self.outside / "x.json")], False),
                                (["import", str(bundle), "--apply"], False)):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                    mock.patch.object(runtime_mod, "Runtime", side_effect=RuntimeError("intercept")) as factory:
                with self.assertRaisesRegex(RuntimeError, "intercept"):
                    entry.main(argv, output_stream=io.StringIO(), interactive=False)
                kwargs = factory.call_args.kwargs
                self.assertIs(kwargs["refresh_shims"], not read_only)
                self.assertIs(kwargs["allow_state_writes"], not read_only)

    def test_export_receipt_requires_successful_file_write(self) -> None:
        receipt = portability.receipt_path(self.runtime.session_store.root)
        now = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)

        def doctor_info(at: datetime.datetime) -> list[str]:
            with mock.patch.object(gateway_facts, "_doctor_now", return_value=at):
                _problems, info, _attention = cli.doctor._collect_doctor_reports(self.runtime)
            return [line for line in info if line.startswith("Operator state:")]

        self.assertEqual(doctor_info(now), ["Operator state: no confirmed file export recorded."])
        target = self.outside / "bundle.json"
        # A failed write (fsync, or the rename) records nothing.
        real_fsync = os.fsync
        for patched, call in (("fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "fixture EIO"))),
                              ("replace", lambda *a: (_ for _ in ()).throw(OSError(28, "fixture ENOSPC")))):
            with self.subTest(fails=patched), mock.patch.object(portability.os, patched, side_effect=call):
                code, _out, err = self.op(["export", "--out", str(target)])
                self.assertEqual(code, 1, err)
                self.assertFalse(receipt.exists())
                self.assertFalse(target.exists())
                self.assertEqual([p.name for p in self.outside.iterdir()], [])
        del real_fsync
        code, out, err = self.op(["export", "--out", str(target)])
        self.assertEqual(code, 0, err)
        data = target.read_bytes()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(data), self.export_json()[0])
        written = json.loads(receipt.read_bytes())
        self.assertEqual(set(written), {"version", "exported_at", "sha256"})
        self.assertEqual(written["sha256"], portability.digest(data))
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        self.assertIn(f"exported the operator configuration to {target}", out)
        stamp = datetime.datetime.strptime(written["exported_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        self.assertEqual(doctor_info(stamp + datetime.timedelta(days=3)),
                         ["Operator state: last confirmed export 3 days ago."])
        self.assertEqual(doctor_info(stamp + datetime.timedelta(days=31)),
                         ["Operator state: last confirmed export 31 days ago — claude-multi export --out FILE"])
        # Stdout exports never refresh the receipt.
        before = receipt.read_bytes()
        self.export_json()
        self.assertEqual(receipt.read_bytes(), before)
        # A corrupt receipt is reported, never repaired by doctor.
        state.atomic_write(receipt, b"{}\n")
        self.assertIn("is invalid", doctor_info(now)[0])
        self.assertEqual(receipt.read_bytes(), b"{}\n")

    def test_export_unreadable_profiles_directory_refuses(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("BOUNDARY: root reads a mode-000 directory")
        store = self.runtime.profiles
        seed = copy.deepcopy(store.load(catalog.DEFAULT_SEED))
        seed.pop("seed", None)
        store.create({**seed, "name": "important-custom"})
        self.assertIn("important-custom", self.export_json()[0]["profiles"])
        receipt = portability.receipt_path(self.runtime.session_store.root)
        os.chmod(store.root, 0)
        self.addCleanup(lambda: store.root.exists() and os.chmod(store.root, 0o700))
        code, out, err = self.op(["export"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"export refused: the profiles directory {store.root} cannot be read", err)
        target = self.outside / "bundle.json"
        code, out, err = self.op(["export", "--out", str(target)])
        self.assertEqual(code, 1, out)
        self.assertIn("export refused: the profiles directory", err)
        self.assertFalse(target.exists())
        self.assertFalse(receipt.exists())
        # The import target refuses the same way (never "no user profiles").
        os.chmod(store.root, 0o700)
        bundle = self.bundle_file(self.export_json()[0])
        os.chmod(store.root, 0)
        code, out, err, _stream = self.importing(bundle)
        self.assertEqual(code, 1, out)
        self.assertIn(f"import: the profiles directory {store.root} cannot be read", err)
        # A genuinely absent directory is still "no user profiles".
        os.chmod(store.root, 0o700)
        shutil.rmtree(store.root)
        document = self.export_json()[0]
        self.assertNotIn("important-custom", document["profiles"])

    def test_export_non_searchable_profiles_directory_refuses(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("BOUNDARY: root searches a mode-400 directory")
        store = self.runtime.profiles
        seed = copy.deepcopy(store.load(catalog.DEFAULT_SEED))
        seed.pop("seed", None)
        store.create({**seed, "name": "important-custom"})
        receipt = portability.receipt_path(self.runtime.session_store.root)
        code, out, err = self.op(["export", "--out", str(self.outside / "previous.json")])
        self.assertEqual(code, 0, out + err)
        old_receipt = receipt.read_bytes()
        target = self.outside / "bundle.json"
        os.chmod(store.root, 0o400)
        self.addCleanup(lambda: store.root.exists() and os.chmod(store.root, 0o700))
        code, out, err = self.op(["export", "--out", str(target)])
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"export refused: the profiles directory {store.root} cannot be read", err)
        self.assertFalse(target.exists())
        self.assertEqual(receipt.read_bytes(), old_receipt)

    def test_export_out_refuses_symlink_and_private_roots(self) -> None:
        home = self.runtime.home
        link = self.outside / "link.json"
        link.symlink_to(self.outside / "elsewhere.json")
        (self.outside / "dir.json").mkdir()
        state.ensure_private_dir(home / ".claude")
        refused = {
            link: "refusing a symlink",
            self.outside / "dir.json": "not a regular file",
            self.outside / "missing" / "x.json": "the directory does not exist",
            self.runtime.session_store.root / "x.json": "refusing a path under",
            sessions.config_root(self.runtime.environ) / "x.json": "refusing a path under",
            home / ".config" / "claude-multi" / "x.json": "refusing a path under",
            home / ".claude" / "x.json": "refusing a path under",
        }
        for path, message in refused.items():
            path.parent.mkdir(parents=True, exist_ok=True) if "missing" not in str(path) else None
            before = inventory(self.root), inventory(self.outside)
            with self.subTest(path=str(path)):
                code, out, err = self.op(["export", "--out", str(path)])
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual((inventory(self.root), inventory(self.outside)), before)
        # There is no Nix module export: the flag is refused as unknown.
        with self.assertRaises(SystemExit) as caught:
            self.op(["export", "--nix"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(os.path.lexists(self.outside / "elsewhere.json"))


# ------------------------------------------------------------------ import
class ImportReaderTests(PortabilityCase):
    def test_invalid_import_diagnostics_never_echo_secret_literals(self) -> None:
        document, _, _ = self.export_json()
        sentinel = "sk-ant-api03-" + "SYNTHETIC" * 6
        encoded_key = json.dumps(sentinel)
        cases = {
            "unknown key": json.dumps({**document, sentinel: None}),
            "version": json.dumps({**document, "version": sentinel}),
            "enum": json.dumps({**document, "trust_requests": {
                **document["trust_requests"], "transport_choices": {"acme": sentinel}}}),
            "duplicate key": "{" + encoded_key + ": 1, " + encoded_key + ": 2}",
            "escaped duplicate key": "{" + encoded_key.replace("s", "\\u0073") + ": 1, " + encoded_key + ": 2}",
            "nested key": json.dumps({**document, "profiles": {sentinel: {"seed": {"id": sentinel}}}}),
        }
        path = self.outside / "invalid.json"
        for name, raw in cases.items():
            with self.subTest(name):
                path.write_text(raw)
                path.chmod(0o600)
                code, out, err, _ = self.importing(path)
                self.assertEqual(code, 1)
                self.assertNotIn(sentinel, out)
                self.assertNotIn(sentinel, err)
                self.assertIn("not strict JSON" if "duplicate" in name else "secret-like literal", err)


    def test_import_reader_accepts_safe_store_bundle_symlink(self) -> None:
        document, raw, _err = self.export_json()
        store = self.outside / "store"
        store.mkdir()
        data = store / "abc-operator-export.json"
        data.write_text(raw)
        os.chmod(data, 0o444)
        os.chmod(store, 0o555)
        self.addCleanup(os.chmod, store, 0o755)
        link = self.outside / "operator-export.json"
        link.symlink_to(data)
        self.assertEqual(portability.load_export(portability.read_bundle(link), asset_root=FIXTURE_ROOT).document,
                         document)
        code, out, err, _stream = self.importing(link)
        self.assertEqual(code, 0, err)
        self.assertIn(portability.PREVIEW_HEADER, out)
        # Unsafe inputs are refused before parsing.
        writable = self.outside / "writable.json"
        writable.write_text(raw)
        os.chmod(writable, 0o662)
        fifo = self.outside / "fifo.json"
        os.mkfifo(fifo, 0o600)
        big = self.outside / "big.json"
        big.write_bytes(b" " * (portability.MAX_BYTES + 1))
        os.chmod(big, 0o600)
        dangling = self.outside / "dangling.json"
        dangling.symlink_to(self.outside / "absent.json")
        for path, message in ((writable, "refused"), (fifo, "refused"), (big, "refused"),
                              (dangling, "no such file")):
            with self.subTest(path=path.name), self.assertRaisesRegex(portability.PortabilityError, message):
                portability.read_bundle(path)

    def test_import_preview_is_read_only(self) -> None:
        self.admitted_source()
        document, _raw, _err = self.export_json()
        self.fresh_target()
        bundle = self.bundle_file(document)
        before = inventory(self.root)
        code, out, err, _stream = self.importing(bundle)
        self.assertEqual(code, 0, err)
        self.assertEqual(inventory(self.root), before)
        self.assertTrue(out.startswith(portability.PREVIEW_HEADER + "\n"))
        self.assertIn("  providers: 1 new, 0 unchanged, 0 conflicting, 0 blocked", out)
        self.assertIn("  route re-approvals required: 1", out)
        self.assertNotIn("model admissions required", out)
        self.assertNotIn("pending admission", out)
        self.assertNotIn("models admit", out)
        self.assertIn("Imported trust is not active.\n  claude-multi providers approve acme\n", out)
        self.assertIn("Nothing was written.", out)
        self.assertEqual(err, "")  # no prompt in a preview


class ImportTrustTests(PortabilityCase):
    def test_imported_trust_never_becomes_ledger_grant(self) -> None:
        self.admitted_source()
        document, _raw, _err = self.export_json()
        self.fresh_target()
        code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn(portability.APPLIED_HEADER, out)
        self.assertIn(portability.APPLIED_TRUST, out)
        self.assertTrue((self.pdir / "acme.json").exists())
        ledger = self.ledger() or operator_mod.OperatorLedger.empty()
        self.assertEqual((ledger.routes, ledger.admissions, ledger.transport_choices), ({}, {}, {}))
        self.assertNotIn("custom-acme-small", self.runtime.settings_store.load().get("admitted_lines", []))
        self.assertNotIn("custom-acme-small", self.runtime.current_effective().admitted_lines)
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(layer.route_status["acme"], "unapproved")
        config = (cli_tests.claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertNotIn("custom-acme-small", config)  # an unapproved keyed route is never rendered
        # A bundle cannot smuggle a grant: trust requests are names only.
        forged = copy.deepcopy(document)
        forged["trust_requests"]["routes"] = [{"acme": {"rd": "0" * 64, "approved_at": "2026-01-01T00:00:00Z"}}]
        with self.assertRaisesRegex(portability.PortabilityError, "invalid"):
            portability.load_export(json.dumps(forged).encode(), asset_root=FIXTURE_ROOT)
        # Rerunning the import is a no-op for what it applied.
        code, out, err, _stream = self.importing(self.bundle_file(document, "again.json"))
        self.assertIn("  providers: 0 new, 1 unchanged, 0 conflicting, 0 blocked", out)

    def test_import_uses_target_digest_and_current_evidence(self) -> None:
        self.admitted_source()
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.http_calls), 1)
        source_grant = self.ledger().routes["acme"]
        document, _raw, _err = self.export_json()
        self.fresh_target()
        code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        # The existing command approves the TARGET's current route.
        code, out, err = self.op(["providers", "approve", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        layer = self.runtime.operator_snapshot().layer
        grant = self.ledger().routes["acme"]
        self.assertEqual(grant["rd"], layer.providers["acme"].route_digest)
        self.assertGreaterEqual(grant["approved_at"], source_grant["approved_at"])
        # Optional diagnostics need this host's own requests: nothing was
        # carried over, and import never sends one on the operator's behalf.
        self.assertIsNone(operator_mod.load_evidence(self.runtime.gateway_environ(),
                                                     operator_mod.load_schemas(FIXTURE_ROOT)))
        self.assertEqual(len(self.http_calls), 1)
        self.serve_current()
        code, out, err = self.op(["models", "qualify", "custom-acme-small", "--smoke"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.http_calls), 2)
        evidence = operator_mod.load_evidence(self.runtime.gateway_environ(), operator_mod.load_schemas(FIXTURE_ROOT))
        self.assertTrue(operator_mod.smoke_current(
            evidence, "custom-acme-small", self.runtime.operator_snapshot().layer.lines["custom-acme-small"].definition_digest))
        self.assertEqual(self.ledger().admissions, {})
        # An existing grant on the target is kept as is, never overwritten.
        before = self.ledger()
        code, out, err, _stream = self.importing(self.bundle_file(document, "again.json"), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Nothing to apply on this host.", out)
        self.assertEqual((self.ledger().routes, self.ledger().admissions), (before.routes, before.admissions))

    def test_import_keyless_origin_requires_per_provider_confirm(self) -> None:
        self.declare_small()
        lan = (REPO_ROOT / "tests/fixtures/operator/providers.d/lanbox.json").read_bytes()
        state.atomic_write(self.pdir / "lanbox.json", lan)
        document, _raw, _err = self.export_json()
        self.fresh_target()
        bundle = self.bundle_file(document)
        line = portability.KEYLESS_TEXT.format(provider="lanbox", origin="http://box.lan:8010")
        code, out, err, _stream = self.importing(bundle)
        self.assertIn(line, out)
        self.assertIn("custom-lan-model", out)  # the served preview lists the alias it adds
        code, out, err, stream = self.importing(bundle, [False, True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(stream.asked[0], portability.KEYLESS_QUESTION.format(provider="lanbox",
                                                                              origin="http://box.lan:8010"))
        self.assertEqual(stream.asked[1], portability_cmd.APPLY_QUESTION)
        self.assertIn("excluded (declined): lanbox", out)
        self.assertFalse((self.pdir / "lanbox.json").exists())
        self.assertTrue((self.pdir / "acme.json").exists())
        code, out, err, stream = self.importing(bundle, [True, True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.pdir / "lanbox.json").exists())
        config = (cli_tests.claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertIn("custom-lan-model", config)

    def test_import_transport_choices_not_transferred_uses_real_remedy(self) -> None:
        document, _raw, _err = self.export_json()
        document["trust_requests"]["transport_choices"] = {"anthropic": "api-key"}
        code, out, err, _stream = self.importing(self.bundle_file(document))
        line = ("Not transferred: transport choice anthropic api-key — "
                "claude-multi providers transport anthropic api-key")
        self.assertIn(line, out)
        self.assertNotIn("providers enable", out)
        args = cli.build_parser().parse_args(line.split(" — ", 1)[1].split()[1:])
        self.assertEqual((args.command, args.providers_command, args.provider_id, args.transport_choice),
                         ("providers", "transport", "anthropic", "api-key"))
        code, out, err, _stream = self.importing(self.bundle_file(document, "b.json"), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        ledger = self.ledger() if operator_mod.ledger_path(self.env).exists() else None
        self.assertEqual(ledger.transport_choices if ledger is not None else {}, {})


class ImportValidationTests(PortabilityCase):
    def base_bundle(self) -> dict:
        self.declare_small()
        document, _raw, _err = self.export_json()
        self.fresh_target()
        return document

    def test_import_cannot_bypass_normal_profile_validation(self) -> None:
        document = self.base_bundle()
        lead = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        lead.pop("seed", None)
        lead.update(name="on-acme", lead={"model": "custom-acme-small", "effort": "high"})
        broken = copy.deepcopy(lead)
        broken.update(name="bad-effort", lead={**self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"],
                                               "effort": "unsupported"})
        document["profiles"].update({"on-acme": {"document": lead}, "bad-effort": {"document": broken}})
        document["bindings"]["fast"] = {"model": "custom-acme-small", "effort": "high"}
        calls = []
        real = profile_mod.evaluate
        with mock.patch.object(profile_mod, "evaluate", side_effect=lambda *a, **k: calls.append(a[0]["name"]) or real(*a, **k)):
            code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("on-acme", calls)  # the ordinary validator decided
        self.assertRegex(out, r"blocked\s+profile on-acme: invalid here — .*unknown model")
        self.assertRegex(out, r"blocked\s+profile bad-effort: invalid here")
        self.assertRegex(out, r"blocked\s+binding fast: invalid here — .*unknown model")
        self.assertNotIn("pending admission", out)
        self.assertNotIn("models admit", out)
        self.assertFalse(self.runtime.profiles.has_user("on-acme"))
        self.assertFalse(self.runtime.profiles.has_user("bad-effort"))
        self.assertNotIn("fast", self.runtime.bindings.bindings())
        # The declaration is now local, but an unapproved route still blocks.
        code, out, err, _stream = self.importing(self.bundle_file(document, "unapproved.json"))
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"blocked\s+profile on-acme: invalid here — .*route")
        self.assertRegex(out, r"blocked\s+binding fast: invalid here — .*route")
        self.assertNotIn("pending admission", out)
        # Target-host route approval is sufficient; no admission or diagnostic
        # is required. New declarations still require the ordinary import rerun.
        code, out, err = self.op(["providers", "approve", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        self.serve_current()
        code, out, err, _stream = self.importing(self.bundle_file(document, "again.json"), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.runtime.profiles.has_user("on-acme"))
        self.assertEqual(self.runtime.bindings.bindings()["fast"], {"model": "custom-acme-small", "effort": "high"})
        self.assertFalse(self.runtime.profiles.has_user("bad-effort"))
        self.assertEqual(self.runtime.current_effective().admitted_lines, frozenset())
        self.assertEqual((self.calls, self.http_calls), ([], []))
        self.assertFalse(operator_mod.evidence_path(self.runtime.gateway_environ()).exists())

    def test_import_local_unadmitted_lines_as_leads_and_agents(self) -> None:
        self.declare_small()
        state.atomic_write(self.pdir / "lanbox.json",
                           (REPO_ROOT / "tests/fixtures/operator/providers.d/lanbox.json").read_bytes())
        document, _raw, _err = self.export_json()
        seed = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        seed.pop("seed", None)
        for name, key in (("on-acme", "custom-acme-small"), ("on-lan", "custom-lan-model")):
            binding = {"model": key, "effort": "high"}
            profile = copy.deepcopy(seed)
            profile.update(name=name, lead=binding)
            profile["agents"]["cm-implementer"] = binding
            document["profiles"][name] = {"document": profile}
            document["bindings"][name] = binding
        # Even a source admission request is inert, never a required remedy.
        document["trust_requests"]["admissions"] = ["custom-acme-small", "custom-lan-model"]
        code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        for name in ("on-acme", "on-lan"):
            self.assertTrue(self.runtime.profiles.has_user(name), out)
            self.assertEqual(self.runtime.bindings.bindings()[name], document["bindings"][name])
            self.assertEqual(self.runtime.profiles.load(name)["agents"]["cm-implementer"],
                             document["bindings"][name])
        self.assertNotIn("pending admission", out)
        self.assertNotIn("models admit", out)
        self.assertNotIn(portability.APPLIED_RERUN, out)
        self.assertEqual(self.runtime.current_effective().admitted_lines, frozenset())
        self.assertEqual(self.ledger().admissions, {})
        self.assertFalse(operator_mod.evidence_path(self.runtime.gateway_environ()).exists())
        self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_import_disabled_local_provider_remains_blocked(self) -> None:
        self.declare_small()
        self.runtime.settings_store.set_provider_enabled("acme", False, catalog=self.runtime.lineup_catalog())
        document, _raw, _err = self.export_json()
        seed = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        seed.pop("seed", None)
        binding = {"model": "custom-acme-small", "effort": "high"}
        document["bindings"]["on-acme"] = binding
        for name in ("lead-on-acme", "agent-on-acme"):
            profile = copy.deepcopy(seed)
            profile["name"] = name
            if name == "lead-on-acme":
                profile["lead"] = binding
            else:
                profile["agents"]["cm-implementer"] = binding
            document["profiles"][name] = {"document": profile}
        code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        # Named bindings save metadata; only actual profile use is blocked.
        self.assertRegex(out, r"ready\s+binding on-acme")
        for name in ("lead-on-acme", "agent-on-acme"):
            self.assertRegex(out, rf"blocked\s+profile {name}: invalid here — .*provider.*disabled")
            self.assertFalse(self.runtime.profiles.has_user(name))
        self.assertEqual(self.runtime.bindings.bindings()["on-acme"], binding)
        self.assertNotIn("pending admission", out)
        self.assertNotIn("models admit", out)

    def test_import_refuses_hm_managed_target(self) -> None:
        document = self.base_bundle()
        managed = self.outside / "hm-providers.d"
        managed.mkdir(mode=0o700)
        self.pdir.parent.mkdir(parents=True, exist_ok=True)
        if self.pdir.exists():
            shutil.rmtree(self.pdir)
        self.pdir.symlink_to(managed)
        before = inventory(managed)
        code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertRegex(out, r"blocked\s+provider acme: providers.d/acme.json is read-only here \(managed elsewhere\)")
        self.assertIn('"secret_ref": "env:ACME_API_KEY"', out)  # the source fragment to add instead
        self.assertEqual(inventory(managed), before)
        # Settings, bindings and profiles are never managed-elsewhere targets: a symlinked
        # store refuses the import as a whole, writing nothing.
        settings_file = sessions.config_root(self.runtime.environ) / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.symlink_to(self.bundle_file({"version": 1}, "settings-src.json"))
        before = inventory(self.root)
        code, out, err, _stream = self.importing(self.bundle_file(document, "b.json"), [True], apply=True)
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertEqual(inventory(self.root), before)

    def test_import_stale_plan_and_committed_failure_reporting(self) -> None:
        document = self.base_bundle()
        seed = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        seed.pop("seed", None)
        for name in ("alpha", "beta"):
            document["profiles"][name] = {"document": {**seed, "name": name}}
        document["bindings"]["deep"] = {"model": seed["lead"]["model"], "effort": "xhigh"}
        bundle = self.bundle_file(document)

        # (a) Stale: the target moves between the confirmation and the commit.
        def race() -> None:
            self.runtime.bindings.set("deep", seed["lead"]["model"], "ultracode", cat=self.runtime.lineup_catalog())

        before = inventory(self.pdir.parent)
        code, out, err, _stream = self.importing(bundle, [True], apply=True, on_apply=race)
        self.assertEqual(code, 1)
        self.assertIn(portability.STALE_PLAN, err)
        self.assertEqual(inventory(self.pdir.parent), before)
        self.assertFalse(self.runtime.profiles.has_user("alpha"))
        self.runtime.bindings.update(lambda doc: doc["bindings"].pop("deep"), cat=self.runtime.lineup_catalog())

        # (b) A committed failure is reported item by item, never "nothing changed".
        real_write = state.atomic_write

        def flaky(path, data):
            real_write(path, data)
            if Path(path).name == "alpha.json":
                raise state.CommittedStateError(5, f"state file {path} was replaced but directory fsync failed")
            if Path(path).name == "beta.json":
                raise AssertionError("beta must not be attempted after the stop")

        with mock.patch.object(state, "atomic_write", side_effect=flaky):
            code, out, err, _stream = self.importing(bundle, [True], apply=True)
        self.assertEqual(code, 1, out + err)
        self.assertIn("Import stopped at profile alpha:", out)
        self.assertIn("Applied: providers.d acme, binding deep.", out)
        self.assertIn("Written, durability unconfirmed: profile alpha (written; durability unconfirmed:", out)
        self.assertIn("Not applied: profile beta.", out)
        self.assertIn(portability.APPLIED_TRUST, out)
        self.assertTrue(self.runtime.profiles.has_user("alpha"))
        self.assertFalse(self.runtime.profiles.has_user("beta"))
        # The rerun completes the remainder only.
        code, out, err, _stream = self.importing(self.bundle_file(document, "again.json"), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.runtime.profiles.has_user("beta"))
        self.assertRegex(out, r"unchanged|Applied")

    def test_import_virtual_seed_reference_never_written(self) -> None:
        store = self.runtime.profiles
        self.assertEqual(sorted(store.install_seeds()), sorted(self.runtime.catalog.seed_profiles))
        modified = "quality"
        store.update(modified, lambda doc: doc.update(description="tuned on the source host"))
        document, _raw, _err = self.export_json()
        for name in self.runtime.catalog.seed_profiles:
            entry = document["profiles"][name]
            if name == modified:
                self.assertEqual(entry["document"]["description"], "tuned on the source host")
            else:
                self.assertEqual(set(entry), {"seed"})
                self.assertEqual(entry["seed"]["id"], name)
        document["profiles"]["retired-seed"] = {"seed": {"id": "retired-seed", "version": 1}}
        self.fresh_target()
        code, out, err, _stream = self.importing(self.bundle_file(document), [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"reference\s+profile balanced: seed reference")
        self.assertRegex(out, r"blocked\s+profile retired-seed: seed retired-seed \(version 1\) is not shipped")
        for name in self.runtime.catalog.seed_profiles:
            self.assertEqual(store.has_user(name), name == modified, name)
        self.assertEqual(store.load(modified)["description"], "tuned on the source host")
        self.assertFalse(store.has_user("retired-seed"))


class ImportCommitTests(PortabilityCase):
    def config_text(self) -> str:
        return (cli_tests.claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()

    def test_import_commits_against_the_displayed_served_preview(self) -> None:
        # An unpublished LAN retarget plus a new provider to import: a session
        # that starts on the alias after the preview was shown refuses the
        # commit (never a silently refreshed confirmation baseline).
        self.declare_small()
        document, _raw, _err = self.export_json()
        self.fresh_target()
        for argv in (served_tests.LAN_ADD, served_tests.LAN_MODEL):
            code, _out, err = self.op(argv)
            self.assertEqual(code, 0, err)
        lan = self.pdir / "lan.json"
        declaration = strict_json.loads(lan.read_bytes())
        declaration["provider"]["base_url"] = "http://box.lan:8000/v2"
        state.atomic_write(lan, strict_json.pretty_file_bytes(declaration))
        bundle = self.bundle_file(document)
        record = state.ensure_private_dir(self.runtime.session_store.root / "sessions") / f"{served_tests.LIVE}.json"
        real = providers_cmd.served_preflight
        shown: list = []

        def preflight_then_new_session(runtime, verb, **kwargs):
            planned = real(runtime, verb, **kwargs)
            if not shown:
                shown.append(planned)
                state.atomic_write(record, strict_json.canonical_file_bytes(
                    served_tests.lenient_record(served_tests.LIVE, served_tests.LAN_ALIAS)))
            return planned

        with mock.patch.object(providers_cmd, "served_preflight", side_effect=preflight_then_new_session):
            code, out, err, stream = self.importing(bundle, [True], apply=True)
        self.assertEqual(len(shown), 1)  # one served plan: the displayed one
        self.assertTrue(shown[0].plan.destructive)
        self.assertEqual(shown[0].plan.impact_summary(), "none")
        self.assertIn("Live session impact: none", out)
        self.assertEqual(stream.asked, [portability_cmd.APPLY_QUESTION])
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"import: {served_plan.CHANGED_REFUSAL}", err)
        self.assertFalse((self.pdir / "acme.json").exists())
        self.assertIn('"http://box.lan:8000/v1"', self.config_text())

    def keyless_bundle(self, names) -> Path:
        document, _raw, _err = self.export_json()
        base = strict_json.loads(
            (REPO_ROOT / "tests/fixtures/operator/providers.d/lanbox.json").read_bytes())
        for name in names:
            declaration = copy.deepcopy(base)
            declaration["provider"]["base_url"] = f"http://{name}.lan:8010/v1"
            declaration["lines"] = {f"custom-{name}-model": declaration["lines"]["custom-lan-model"]}
            document["providers"][name] = declaration
        return self.bundle_file(document)

    def test_provider_rollback_after_directory_access_loss_reports_retained(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("BOUNDARY: root searches a mode-600 directory")
        bundle = self.keyless_bundle(("alpha", "beta"))
        real_write = operator_mod.write_provider_bytes
        calls = 0
        self.addCleanup(lambda: self.pdir.exists() and os.chmod(self.pdir, 0o700))

        def lose_access(env, file_id, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                os.chmod(self.pdir, 0o600)
            return real_write(env, file_id, data)

        with mock.patch.object(operator_mod, "write_provider_bytes", side_effect=lose_access):
            code, out, err, _stream = self.importing(bundle, [True, True, True], apply=True)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(calls, 2)
        self.assertIn("Written and kept: providers.d alpha", out)
        self.assertIn("its removal is unconfirmed", out)
        self.assertIn("claude-multi providers rm alpha", out)
        self.assertIn("Not applied: providers.d beta", out)
        self.assertNotIn("Not applied: providers.d alpha", out)
        os.chmod(self.pdir, 0o700)
        self.assertTrue((self.pdir / "alpha.json").is_file())

    def test_provider_rollback_noop_removal_reports_retained(self) -> None:
        bundle = self.keyless_bundle(("alpha", "beta"))
        real_write = operator_mod.write_provider_bytes

        def fail_beta(env, file_id, data):
            if file_id == "beta":
                raise OSError(28, "fixture ENOSPC")
            return real_write(env, file_id, data)

        with mock.patch.object(operator_mod, "write_provider_bytes", side_effect=fail_beta), \
                mock.patch.object(providers_cmd, "restore_file", return_value=None) as remove:
            code, out, err, _stream = self.importing(bundle, [True, True, True], apply=True)
        self.assertEqual(code, 1, out + err)
        remove.assert_called_once_with(self.runtime.gateway_environ(), "alpha", None)
        self.assertIn("Written and kept: providers.d alpha (not published: the removal failed:", out)
        self.assertIn("claude-multi providers rm alpha", out)
        self.assertIn("Not applied: providers.d beta", out)
        self.assertNotIn("Not applied: providers.d alpha", out)
        self.assertIn("Applied: nothing.", out)
        self.assertTrue((self.pdir / "alpha.json").is_file())
        self.assertFalse((self.pdir / "beta.json").exists())
        self.assertFalse((cli_tests.claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").exists())

    def test_import_provider_batch_failure_is_rolled_back_per_declaration(self) -> None:
        names = ("alpha", "beta", "gamma")
        bundle = self.keyless_bundle(names)
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        config_before = self.config_text()
        real_write = state.atomic_write
        for position, failing in enumerate(names):
            for when in ("before rename", "after rename"):
                def flaky(path, data, failing=failing, when=when):
                    if Path(path).parent == self.pdir and Path(path).stem == failing:
                        if when == "before rename":
                            raise OSError(28, "fixture ENOSPC")
                        real_write(path, data)
                        raise state.CommittedStateError(5, f"state file {path} was replaced but directory fsync failed")
                    real_write(path, data)

                with self.subTest(failing=failing, when=when), mock.patch.object(state, "atomic_write", side_effect=flaky):
                    code, out, err, _stream = self.importing(bundle, [True, True, True, True], apply=True)
                    self.assertEqual(code, 1, out + err)
                    self.assertIn(f"Import stopped at providers.d {failing}:", out)
                    self.assertIn("Applied: nothing.", out)
                    self.assertNotIn("Written and kept", out)
                    self.assertIn("Not applied: providers.d alpha, providers.d beta, providers.d gamma", out)
                    self.assertEqual(sorted(p.name for p in self.pdir.glob("*.json")), [], position)
                    self.assertEqual(self.config_text(), config_before)  # nothing published
        # A rollback that cannot remove a written declaration says so, with
        # its recovery command; it is never published.
        def fail_beta(path, data):
            if Path(path).parent == self.pdir and Path(path).stem == "beta":
                raise OSError(28, "fixture ENOSPC")
            real_write(path, data)

        with mock.patch.object(state, "atomic_write", side_effect=fail_beta), \
                mock.patch.object(providers_cmd, "restore_file",
                                  side_effect=operator_mod.OperatorError("fixture removal refused")):
            code, out, err, _stream = self.importing(bundle, [True, True, True, True], apply=True)
        self.assertEqual(code, 1, out + err)
        self.assertIn("Applied: nothing.", out)
        self.assertIn("Written and kept: providers.d alpha (not published: the removal failed: fixture removal "
                      "refused; remove it with: claude-multi providers rm alpha).", out)
        self.assertIn("Not applied: providers.d beta, providers.d gamma", out)
        self.assertEqual(sorted(p.stem for p in self.pdir.glob("*.json")), ["alpha"])
        self.assertNotIn("custom-alpha-model", self.config_text())
        code, out, err = self.op(["providers", "rm", "alpha"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.pdir / "alpha.json").exists())
        # Without a fault the whole batch applies and is published once.
        code, out, err, _stream = self.importing(bundle, [True, True, True, True], apply=True)
        self.assertEqual(code, 0, out + err)
        for name in names:
            self.assertIn(f"custom-{name}-model", self.config_text())


class ImportBoundaryTests(PortabilityCase):
    def test_import_non_searchable_profiles_directory_refuses(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("BOUNDARY: root searches a mode-400 directory")
        store = self.runtime.profiles
        seed = copy.deepcopy(store.load(catalog.DEFAULT_SEED))
        seed.pop("seed", None)
        store.create({**seed, "name": "important-custom"})
        bundle = self.bundle_file(self.export_json()[0])
        before = inventory(self.root)
        os.chmod(store.root, 0o400)
        self.addCleanup(lambda: store.root.exists() and os.chmod(store.root, 0o700))
        code, out, err, _stream = self.importing(bundle)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"import: the profiles directory {store.root} cannot be read", err)
        self.assertIn("— nothing written", err)
        os.chmod(store.root, 0o700)
        self.assertEqual(inventory(self.root), before)

    def test_load_export_bounds_and_names(self) -> None:
        document, raw, _err = self.export_json()
        self.assertEqual(portability.load_export(raw.encode(), asset_root=FIXTURE_ROOT).sha256,
                         portability.digest(raw.encode()))
        for mutate, message in (
            (lambda d: d.update(format="other"), "format"),
            (lambda d: d.update(version=2), "unsupported"),
            (lambda d: d["profiles"].update({"../x": {"seed": {"id": "../x", "version": 1}}}), "invalid|unsafe"),
            (lambda d: d["providers"].update({"../../etc": {}}), "unsafe"),
            (lambda d: d["bindings"].update({"Bad Name": {"model": "x", "effort": "high"}}), "bindings"),
            (lambda d: d["settings"].update(compaction_percent=10), "invalid"),
            (lambda d: d["profiles"].update({f"p{i}": {"seed": {"id": f"p{i}", "version": 1}}
                                             for i in range(portability.MAX_PROFILES + 1)}), "more than"),
        ):
            changed = copy.deepcopy(document)
            mutate(changed)
            with self.subTest(message=message), self.assertRaisesRegex(portability.PortabilityError, message):
                portability.load_export(json.dumps(changed).encode(), asset_root=FIXTURE_ROOT)
        for raw_bad in (b"[]", b'{"format": 1, "format": 2}', b"\xff"):
            with self.assertRaises(portability.PortabilityError):
                portability.load_export(raw_bad, asset_root=FIXTURE_ROOT)

    def test_import_apply_needs_the_human_guard(self) -> None:
        document, _raw, _err = self.export_json()
        bundle = self.bundle_file(document)
        before = inventory(self.root)
        code, out, err, _stream = self.importing(bundle, [True, True], apply=True, env={"CLAUDECODE": "1"})
        self.assertEqual(code, 1)
        self.assertIn("import --apply: needs a terminal outside Claude Code sessions (CLAUDECODE set)", err)
        self.assertEqual(out, "")
        self.assertEqual(inventory(self.root), before)




class KeyedPortabilityTests(keyed.KeyedServedCase, PortabilityCase):
    """Transfer carries declarations, never source-host credentials or authority."""

    def source(self):
        self.declare({keyed.KEYED_ID: keyed.keyed_document(headers={"X-Client": "fixture-client"})},
                     value=keyed.KEYED_VALUE)
        self.publish()
        self.serve_current()
        code, out, err = self.op(["models", "qualify", keyed.KEYED_KEY, "--smoke"], "y\n")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.op(["models", "admit", keyed.KEYED_KEY], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.ledger().aliases)
        self.assertTrue(operator_mod.evidence_path(self.runtime.gateway_environ()).exists())
        return self.export_json()[0]

    def no_trust(self, document, text):
        self.assertEqual(document["providers"][keyed.KEYED_ID]["provider"]["auth"],
                         {"kind": "bearer", "secret_ref": "env:" + keyed.KEYED_SECRET})
        self.assertEqual(document["trust_requests"]["routes"], [keyed.KEYED_ID])
        self.assertEqual(document["trust_requests"]["admissions"], [keyed.KEYED_KEY])
        for forbidden in (keyed.KEYED_VALUE, '"aliases"', '"approved_at"', '"checks"',
                          '"admitted_lines"', "api-key-entries", "config.yaml", "operator-evidence"):
            self.assertNotIn(forbidden, text)

    def test_keyed_json_export_has_logical_refs_and_no_store_reads(self):
        self.source()
        spy = keyed.SpyStore({keyed.KEYED_SECRET: keyed.KEYED_VALUE})
        with keyed.spy_store(spy):
            document, text, _ = self.export_json()
        self.assertEqual(spy.calls, [])
        self.no_trust(document, text)
        self.assertEqual(portability.load_export(text.encode(), asset_root=self.assets).document, document)

    def test_keyed_import_closed_target_refuses_audit(self):
        bundle = self.bundle_file(self.source())
        self.fresh_target()
        self.runtime.catalog.docs["gateway"].pop("audits", None)
        before = inventory(self.root)
        code, out, err, _ = self.importing(bundle)
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"blocked\s+provider chatco:")
        self.assertIn("audit", out)
        self.assertEqual(inventory(self.root), before)
        code, out, err, _ = self.importing(bundle, [False], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Nothing to apply on this host.", out)
        self.assertEqual(inventory(self.root), before)
        self.assertFalse((self.pdir / f"{keyed.KEYED_ID}.json").exists())

    def test_keyed_import_open_target_requires_local_route_and_key_not_admission(self):
        bundle = self.bundle_file(self.source())
        self.fresh_target()
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\n")
        code, out, err, _ = self.importing(bundle, [True], apply=True)
        self.assertEqual(code, 0, out + err)
        ledger = self.ledger() or operator_mod.OperatorLedger.empty()
        self.assertEqual((ledger.routes, ledger.admissions, ledger.aliases), ({}, {}, {}))
        self.assertEqual(self.runtime.operator_snapshot().layer.route_status[keyed.KEYED_ID], "unapproved")
        self.assertNotIn(keyed.KEYED_KEY, self.runtime.current_effective().admitted_lines)
        self.assertNotIn(keyed.KEYED_KEY, self.config().read_text())
        self.assertIn(keyed.KEYED_KEY, self.runtime.current_effective().unavailable_lines)
        code, out, err = self.op(["providers", "approve", keyed.KEYED_ID], "y\n")
        self.assertEqual(code, 0, out + err)
        self.publish()
        self.assertNotIn(keyed.KEYED_KEY, self.config().read_text())  # no local key yet
        state.atomic_write(self.secret_file, self.secret_file.read_bytes() +
                           f"{keyed.KEYED_SECRET}=target-fixture-key\n".encode())
        self.publish()
        self.serve_current()
        self.assertIn(keyed.KEYED_KEY, self.config().read_text())
        self.assertNotIn(keyed.KEYED_KEY, self.runtime.current_effective().admitted_lines)
        self.assertNotIn(keyed.KEYED_KEY, self.runtime.current_effective().unavailable_lines)
        self.assertFalse(operator_mod.evidence_path(self.runtime.gateway_environ()).exists())

    def test_keyed_import_rechecks_headers_origins_collisions(self):
        document = self.source()
        self.fresh_target()
        for case in ("header", "origin", "collision", "schema"):
            with self.subTest(case=case):
                changed = copy.deepcopy(document)
                provider = changed["providers"][keyed.KEYED_ID]["provider"]
                if case == "header":
                    provider["headers"] = {"Authorization": "forbidden-fixture"}
                elif case == "origin":
                    provider["listing"] = {"url": "https://elsewhere.example/models", "auth": "provider", "shape": "openai"}
                elif case == "collision":
                    changed["providers"]["otherchat"] = copy.deepcopy(changed["providers"][keyed.KEYED_ID])
                else:
                    provider["use-max-completion-tokens"] = True
                before = inventory(self.root)
                code, out, err, _ = self.importing(self.bundle_file(changed, case + ".json"))
                self.assertTrue(code != 0 or "blocked" in out, out + err)
                if code == 0:
                    self.assertRegex(out, r"blocked\s+provider")
                self.assertEqual(inventory(self.root), before)

    def test_keyed_import_uses_existing_single_commit_barrier(self):
        bundle = self.bundle_file(self.source())
        self.fresh_target()
        real = sessions.served_change_phase
        active = []
        entered = []

        @contextlib.contextmanager
        def phase(*args, **kwargs):
            self.assertFalse(active, "nested import barrier")
            with real(*args, **kwargs) as token:
                entered.append(token)
                active.append(token)
                try:
                    yield token
                finally:
                    active.pop()

        real_write = operator_mod.write_provider_bytes

        def write(*args, **kwargs):
            self.assertEqual(len(active), 1)
            return real_write(*args, **kwargs)

        with mock.patch.object(sessions, "served_change_phase", phase), \
                mock.patch.object(operator_mod, "write_provider_bytes", side_effect=write) as writer:
            code, out, err, _ = self.importing(bundle, [True], apply=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(entered), 1)
        writer.assert_called_once()
        self.assertFalse(active)
