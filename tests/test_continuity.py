"""Gateway continuity aliases.

Every test runs on a temp HOME, a temp ``XDG_STATE_HOME`` and an explicit
state root; the asset root is the frozen fixture (or a private copy of it
for catalog mutations). No test reads real state or reaches a gateway: the
reload check is always an injected ``models_get``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claude_multi import catalog, continuity, endpoint, proxy, render, sessions, state, strict_json
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _golden import assertGolden

SEED_GOLDEN = GOLDENS_ROOT / "render" / "continuity-seed.json"
RECORD_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RECORD_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RECORD_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
MUSE_HIGH = "claude-multi-muse-spark-high"
MUSE_XHIGH = "claude-multi-muse-spark-xhigh"


# run/login gained a chdir seam that defaults to "no chdir";
# the direct cmd_run/main(["run"]) call sites here pass none, so the test
# process's working directory must never move.
_MODULE_CWD: str | None = None


def setUpModule() -> None:
    global _MODULE_CWD
    _MODULE_CWD = os.getcwd()


def tearDownModule() -> None:
    moved = os.getcwd()
    if _MODULE_CWD is not None and moved != _MODULE_CWD:
        os.chdir(_MODULE_CWD)
        raise AssertionError(f"test_continuity moved the process cwd: {_MODULE_CWD} -> {moved}")


def _managed_record(stem: str, *selectors: str, event: str | None = None) -> dict:
    lead, *variants = selectors or ("claude-opus-4-8[1m]",)
    return {
        "version": 3,
        "managed_id": stem,
        "session_type": "managed-composition",
        "last_event_source": event,
        "snapshot": {
            "lead": {"model": "opus", "client_selector": lead},
            "variants": [
                {"id": f"v{index}", "client_selector": selector}
                for index, selector in enumerate(variants)
            ],
        },
    }


def _ordinary_record(stem: str, *, event: str | None = None) -> dict:
    return {
        "version": 3,
        "managed_id": stem,
        "session_type": "ordinary-gateway",
        "last_event_source": event,
        "ordinary_model": "kimi-k3",
        "context_profile": "large",
    }


class ContinuityCase(unittest.TestCase):
    """Temp HOME/state root, fixture assets, META + KIMI secrets present."""

    assets: Path = FIXTURE_ROOT

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-continuity-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        state.ensure_private_dir(self.home)
        secrets = state.ensure_private_dir(self.root / "secrets")
        self.secret_file = secrets / "claude.env"
        state.atomic_write(
            self.secret_file,
            b"KIMI_CLAUDE_API_KEY=kimi-dummy-value\nMETA_CLAUDE_API_KEY=meta-dummy-value\n",
        )
        self.state_root = self.root / "state" / "claude-multi"
        self.environ = {
            "HOME": str(self.home),
            # Deliberately elsewhere: continuity.json is HOME-relative.
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "CLAUDE_MULTI_SECRET_ENV": str(self.secret_file),
            "CLAUDE_MULTI_ASSETS": str(self.assets),
        }
        self.config_dir = proxy.config_dir(self.home)
        self.config = self.config_dir / "config.yaml"
        self.file = continuity.path(self.home)

    # -- helpers ---------------------------------------------------------
    def bundle(self) -> catalog.Catalog:
        return catalog.load_catalog(Path(self.environ["CLAUDE_MULTI_ASSETS"]))

    def render(self, state_root: Path | None | str = "default"):
        root = self.state_root if state_root == "default" else state_root
        return proxy.render_runtime_config(
            self.home, environ=self.environ, state_root=root,
        )

    def write_record(self, stem: str, record: dict | bytes, *, mode: int = 0o600) -> Path:
        sessions = state.ensure_private_dir(self.state_root / "sessions")
        path = sessions / f"{stem}.json"
        data = record if isinstance(record, bytes) else strict_json.canonical_file_bytes(record)
        state.atomic_write(path, data)
        os.chmod(path, mode)
        return path

    def write_scope_settings(self, stem: str, settings: dict) -> None:
        scope = state.ensure_private_dir(self.state_root / "scopes" / stem)
        state.atomic_write(scope / "settings.json", strict_json.canonical_file_bytes(settings))

    def models_get(self, _base, _token):
        import re

        yaml = self.config.read_text()
        return 200, set(re.findall(r'alias: "([^"]+)"', yaml))

    def init(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = proxy.cmd_init(list(args), environ=self.environ, models_get=self.models_get)
        return code, out.getvalue(), err.getvalue()

    def copy_assets(self) -> Path:
        target = self.root / "assets"
        shutil.copytree(FIXTURE_ROOT, target)
        self.environ["CLAUDE_MULTI_ASSETS"] = str(target)
        return target


class SeedTests(ContinuityCase):
    """Item 10: a missing file is seeded at HOME, 0600, schema-valid."""

    def test_missing_file_is_seeded_home_relative(self) -> None:
        self.assertFalse(self.file.exists())
        _target, result, report = self.render()
        self.assertEqual(self.file, self.home / ".config" / "claude-multi" / "continuity.json")
        self.assertTrue(self.file.is_file())
        self.assertFalse((self.root / "xdg-config" / "claude-multi" / "continuity.json").exists())
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        document = continuity.read(self.home)
        expected = continuity.seed_only(self.bundle())
        expected["state_root"] = str(self.state_root)
        self.assertEqual(document, expected)
        self.assertEqual(report.size, 2)
        self.assertEqual(report.rendered, (MUSE_HIGH, MUSE_XHIGH))
        self.assertEqual(result.continuity_rendered, (MUSE_HIGH, MUSE_XHIGH))
        self.assertIsNone(report.corrupt)

    def test_seed_bytes_equal_golden_without_state_root(self) -> None:
        _target, _result, report = self.render(state_root=None)
        assertGolden(self, SEED_GOLDEN, self.file.read_bytes())
        self.assertIn("record extension skipped: no state root", report.notices)

    def test_schema_and_key_checks_refuse(self) -> None:
        good = continuity.seed_only(self.bundle())
        state.ensure_private_dir(self.config_dir)
        bad_documents = {
            "schema": {**good, "version": 2},
            "suffix": {**good, "aliases": {MUSE_HIGH + "[1m]": good["aliases"][MUSE_HIGH]}},
            "sentinel": {**good, "pruned": {render.SENTINEL_PREFIX + "0": 3}},
        }
        for label, document in bad_documents.items():
            with self.subTest(label):
                state.atomic_write(self.file, json.dumps(document).encode())
                with self.assertRaises(continuity.ContinuityError):
                    continuity.read(self.home)
        state.atomic_write(self.file, b'{"version": 1, "version": 1}')
        with self.assertRaises(continuity.ContinuityError):
            continuity.read(self.home)
        state.atomic_write(self.file, strict_json.pretty_file_bytes(good))
        os.chmod(self.file, 0o644)
        with self.assertRaises(continuity.ContinuityError):
            continuity.read(self.home)
        self.file.unlink()
        self.file.symlink_to(self.root / "elsewhere.json")
        with self.assertRaises(continuity.ContinuityError):
            continuity.read(self.home)


class MonotonicTests(ContinuityCase):
    """Item 11: entries leave only through prune; init is idempotent."""

    def test_retired_entry_removed_from_catalog_keeps_the_alias(self) -> None:
        self.render()
        assets = self.copy_assets()
        retired = assets / "catalog" / "retired.json"
        retired.write_bytes(strict_json.pretty_file_bytes({"retired": {}, "version": 1}))
        self.assertEqual(self.bundle().retired, {})
        _target, result, _report = self.render()
        self.assertEqual(set(continuity.read(self.home)["aliases"]), {MUSE_HIGH, MUSE_XHIGH})
        self.assertIn(f'alias: "{MUSE_HIGH}"', result.yaml)

    def test_deleting_records_never_shrinks(self) -> None:
        self.render()
        document = continuity.read(self.home)
        document = continuity.apply_prune(
            document, continuity.PrunePlan((MUSE_HIGH,), ()), 31
        )
        state.atomic_write(self.file, strict_json.pretty_file_bytes(document))
        self.write_record(RECORD_A, _managed_record(RECORD_A, MUSE_HIGH + "[1m]"))
        self.render()
        self.assertIn(MUSE_HIGH, continuity.read(self.home)["aliases"])
        shutil.rmtree(self.state_root / "sessions")
        self.render()
        self.assertEqual(set(continuity.read(self.home)["aliases"]), {MUSE_HIGH, MUSE_XHIGH})

    def test_two_inits_are_byte_identical(self) -> None:
        self.write_record(RECORD_A, _managed_record(RECORD_A, "claude-opus-4-8[1m]"))
        self.assertEqual(self.init()[0], 0)
        config, persisted = self.config.read_bytes(), self.file.read_bytes()
        mtime = self.file.stat().st_mtime_ns
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(self.file.read_bytes(), persisted)
        self.assertEqual(self.file.stat().st_mtime_ns, mtime)  # unchanged: not rewritten


class WatermarkTests(ContinuityCase):
    """Item 12: seed watermark plus tombstones."""

    def _pruned(self, alias: str) -> dict:
        document = continuity.seed_only(self.bundle())
        return continuity.apply_prune(document, continuity.PrunePlan((alias,), ()), 31)

    def test_pruned_alias_not_reseeded_at_the_same_catalog(self) -> None:
        state.ensure_private_dir(self.config_dir)
        continuity.write(self.home, self._pruned(MUSE_HIGH))
        _target, result, _report = self.render()
        document = continuity.read(self.home)
        self.assertNotIn(MUSE_HIGH, document["aliases"])
        self.assertEqual(document["pruned"], {MUSE_HIGH: 31})
        self.assertNotIn(f'"{MUSE_HIGH}"', result.yaml)

    def test_new_catalog_seeds_only_new_retired_aliases(self) -> None:
        document = self._pruned(MUSE_HIGH)
        assets = self.copy_assets()
        version_path = assets / "version.json"
        version = strict_json.load(version_path)
        version["catalog_version"] = 32
        version_path.write_bytes(strict_json.pretty_file_bytes(version))
        retired_path = assets / "catalog" / "retired.json"
        retired = strict_json.load(retired_path)
        retired["retired"]["kimi-old"] = {
            "capabilities": ["lead", "agents"],
            "context_tokens": 1000000,
            "display": "Kimi Old",
            "last_wire": "k2",
            "provider": "kimi",
            "reason": "Fixture retired line: frozen test data.",
            "roles": "all",
            "selectors": {"claude-multi-kimi-old[1m]": "output-config-max"},
            "since_catalog": 32,
            "successor": None,
        }
        retired_path.write_bytes(strict_json.pretty_file_bytes(retired))
        merged, changed = continuity.merge_seed(document, self.bundle())
        self.assertTrue(changed)
        self.assertEqual(merged["seeded_through_catalog"], 32)
        self.assertEqual(set(merged["aliases"]), {MUSE_XHIGH, "claude-multi-kimi-old"})
        self.assertEqual(merged["aliases"]["claude-multi-kimi-old"]["source"], "seed:kimi-old")
        again, changed = continuity.merge_seed(merged, self.bundle())
        self.assertFalse(changed)
        self.assertEqual(again, merged)

    def test_ended_record_does_not_revive_a_tombstone_but_a_live_one_does(self) -> None:
        bundle = self.bundle()
        document = self._pruned(MUSE_HIGH)
        aliases = render.catalog_alias_set(bundle.providers, bundle.lines)
        self.write_record(RECORD_A, _managed_record(RECORD_A, MUSE_HIGH + "[1m]", event="end"))
        scan = continuity.scan_records(self.state_root)
        extended, changed, _unservable = continuity.extend_from_records(document, bundle, scan, aliases)
        self.assertFalse(changed)
        self.assertNotIn(MUSE_HIGH, extended["aliases"])
        self.write_record(RECORD_B, _managed_record(RECORD_B, MUSE_HIGH + "[1m]"))
        scan = continuity.scan_records(self.state_root)
        extended, changed, _unservable = continuity.extend_from_records(document, bundle, scan, aliases)
        self.assertTrue(changed)
        self.assertEqual(extended["aliases"][MUSE_HIGH]["source"], "record")
        self.assertNotIn(MUSE_HIGH, extended["pruned"])


class RetiredKeyGrammarTests(ContinuityCase):
    """Every retired key the catalog accepts seeds a persistable
    continuity document (the ``seed:<key>`` source grammar mirrors
    ``catalog.RETIRED_KEY``), so a catalog release cannot pass validation and
    then stop the gateway at render time."""

    def test_uppercase_generation_retired_key_seeds_and_renders(self) -> None:
        assets = self.copy_assets()
        retired_path = assets / "catalog" / "retired.json"
        retired = strict_json.load(retired_path)
        retired["retired"]["fable@4X"] = {
            "capabilities": ["lead", "agents"],
            "context_tokens": 1000000,
            "display": "Fable 4X",
            "generation": "4X",
            "last_wire": "claude-fable-4x",
            "provider": "anthropic",
            "reason": "Review r1 probe: an uppercase generation part.",
            "roles": "all",
            "selectors": {"claude-fable-4x[1m]": None},
            "since_catalog": 31,
            "successor": "fable",
        }
        retired_path.write_bytes(strict_json.pretty_file_bytes(retired))
        bundle = self.bundle()
        document = continuity.seed_only(bundle)
        continuity.validate_document(document)
        self.assertEqual(document["aliases"]["claude-fable-4x"]["source"], "seed:fable@4X")
        self.render()
        self.assertEqual(
            continuity.read(self.home)["aliases"]["claude-fable-4x"]["source"], "seed:fable@4X"
        )

    def test_source_pattern_rejects_keys_outside_the_retired_grammar(self) -> None:
        good = continuity.seed_only(self.bundle())
        for source in ("seed:Fable", "seed:fable@", "seed:a.b", "seed:-x", "seed:"):
            document = json.loads(json.dumps(good))
            document["aliases"][MUSE_HIGH]["source"] = source
            with self.subTest(source=source), self.assertRaises(continuity.ContinuityError):
                continuity.validate_document(document)
        for key in ("muse-spark", "opus@4.8", "fable@4X", "fable@rc-1"):
            self.assertIsNotNone(catalog.RETIRED_KEY.fullmatch(key))
            document = json.loads(json.dumps(good))
            document["aliases"][MUSE_HIGH]["source"] = f"seed:{key}"
            with self.subTest(key=key):
                continuity.validate_document(document)


@uses_shipped_catalog
class ShippedCatalogSeedTests(unittest.TestCase):
    """Every shipped retired entry seeds a schema-valid continuity document."""

    def test_shipped_seed_only_validates(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        document = continuity.seed_only(bundle)
        continuity.validate_document(document)
        self.assertTrue(document["aliases"])


class RecordExtensionTests(ContinuityCase):
    """scan_records on every record shape."""

    def test_pruned_alias_readded_from_a_live_record(self) -> None:
        state.ensure_private_dir(self.config_dir)
        document = continuity.apply_prune(
            continuity.seed_only(self.bundle()), continuity.PrunePlan((MUSE_HIGH,), ()), 31,
        )
        continuity.write(self.home, document)
        self.write_record(
            RECORD_A, _managed_record(RECORD_A, "claude-opus-4-8[1m]", MUSE_HIGH + "[1m]"),
        )
        _target, result, report = self.render()
        entry = continuity.read(self.home)["aliases"][MUSE_HIGH]
        self.assertEqual(entry["source"], "record")
        self.assertEqual(entry["proxy_contract"], "output-config-high")
        self.assertIn(f'alias: "{MUSE_HIGH}"', result.yaml)
        self.assertIn(MUSE_HIGH, report.rendered)

    def test_unknown_selector_is_unservable_never_stored(self) -> None:
        self.write_record(
            RECORD_A, _managed_record(RECORD_A, "claude-multi-grok45-high[1m]"),
        )
        code, out, _err = self.init()
        self.assertEqual(code, 0)
        self.assertIn("unservable selectors: claude-multi-grok45-high", out)
        self.assertIn("continuity: 2 aliases (2 rendered)", out)
        self.assertNotIn("claude-multi-grok45-high", continuity.read(self.home)["aliases"])

    def test_bad_records_are_skipped_by_stem8_and_the_render_succeeds(self) -> None:
        self.write_record(RECORD_A, b"{not json")
        target = self.write_record(RECORD_B, _managed_record(RECORD_B, MUSE_HIGH))
        link_dir = self.state_root / "sessions"
        os.symlink(target, link_dir / f"{RECORD_C}.json")
        loose = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        self.write_record(loose, _managed_record(loose, MUSE_HIGH), mode=0o644)
        scan = continuity.scan_records(self.state_root)
        self.assertEqual(sorted(scan.unreadable), sorted([RECORD_A, RECORD_C, loose]))
        for stem in (RECORD_A, RECORD_C, loose):
            self.assertIn(f"record {stem[:8]}: unreadable, skipped", scan.notices)
        self.assertFalse(any(RECORD_A in notice for notice in scan.notices))
        code, out, err = self.init()
        self.assertEqual(code, 0)
        self.assertIn("records skipped: 3", out)
        self.assertIn(f"record {RECORD_A[:8]}: unreadable, skipped", err)
        self.assertNotIn(RECORD_A, err)

    def test_no_state_root_reads_no_records(self) -> None:
        self.write_record(RECORD_A, _managed_record(RECORD_A, "claude-multi-grok45-high"))

        def refuse(*_args, **_kwargs):
            raise AssertionError("record scan without a state root")

        with mock.patch.object(continuity, "scan_records", refuse):
            _target, _result, report = self.render(state_root=None)
        self.assertEqual(report.unservable, ())
        self.assertIn("record extension skipped: no state root", report.notices)

    def test_ordinary_record_is_readable_and_contributes_its_scope(self) -> None:
        # An ordinary v3 record has no snapshot; it
        # is readable (never "unreadable") and its scope fence and observed
        # model are its references.
        record = _ordinary_record(RECORD_A)
        record["observed_model"] = "claude-multi-kimi-k3[1m]"
        self.write_record(RECORD_A, record)
        self.write_scope_settings(RECORD_A, {
            "availableModels": [MUSE_HIGH + "[1m]", "claude-multi-qwen38-max[1m]"],
            "model": MUSE_HIGH + "[1m]",
        })
        scan = continuity.scan_records(self.state_root)
        self.assertEqual(scan.unreadable, ())
        self.assertEqual(scan.notices, ())
        for base in (MUSE_HIGH, "claude-multi-qwen38-max", "claude-multi-kimi-k3"):
            self.assertEqual(scan.refs[base], frozenset({RECORD_A}))
        self.assertEqual(scan.live, frozenset({RECORD_A}))

    def test_pre_v3_and_partial_records_count_live_and_never_raise(self) -> None:
        self.write_record(RECORD_A, {"version": 1, "session_id": RECORD_A, "snapshot": {}})
        self.write_record(RECORD_B, {"version": 3, "snapshot": {"lead": None, "variants": [1, None]}})
        self.write_record(RECORD_C, {"version": 3, "last_event_source": "end", "snapshot": "x"})
        self.write_scope_settings(RECORD_C, {"availableModels": "not-a-list", "model": 7})
        (self.state_root / "scopes" / RECORD_B).mkdir(mode=0o700)
        os.symlink(self.root / "nowhere", self.state_root / "scopes" / RECORD_B / "settings.json")
        scan = continuity.scan_records(self.state_root)
        self.assertEqual(scan.unreadable, ())
        self.assertEqual(scan.live, frozenset({RECORD_A, RECORD_B}))
        self.assertEqual(scan.refs, {})
        # A structurally malformed fence is unknown, not empty.
        self.assertEqual(scan.notices, tuple(sorted((
            f"record {RECORD_B[:8]}: scope settings unreadable, skipped",
            f"record {RECORD_C[:8]}: scope settings malformed (fence unknown), skipped",
        ), key=lambda notice: notice[7:15])))
        self.assertEqual(set(scan.fence_unreadable), {RECORD_B, RECORD_C})

    def test_missing_sessions_dir_is_empty(self) -> None:
        scan = continuity.scan_records(self.root / "nothing-here")
        self.assertEqual((scan.refs, scan.live, scan.unreadable), ({}, frozenset(), ()))


class RunSurvivesCorruptionTests(ContinuityCase):
    """Run never fails on records or continuity."""

    def setUp(self) -> None:
        super().setUp()
        binary = self.root / "bin" / "cli-proxy-api"
        binary.parent.mkdir()
        binary.write_bytes(b"#!/bin/fake\n")
        binary.chmod(0o755)
        self.environ["CLAUDE_MULTI_PROXY_BIN"] = str(binary)

    def run_proxy(self, *args: str):
        calls = []
        err = io.StringIO()
        with redirect_stderr(err):
            proxy.cmd_run(list(args), environ=self.environ, execve=lambda *a: calls.append(a))
        return calls, err.getvalue()

    def test_corrupt_record_and_corrupt_continuity(self) -> None:
        self.write_record(RECORD_A, b"[1, 2")
        calls, err = self.run_proxy("--state-root", str(self.state_root))
        self.assertEqual(len(calls), 1)
        self.assertIn(f"record {RECORD_A[:8]}: unreadable, skipped", err)
        state.atomic_write(self.file, b'{"version": 1')
        corrupt = self.file.read_bytes()
        calls, err = self.run_proxy()
        self.assertEqual(len(calls), 1)
        self.assertIn("gateway continuity set unreadable", err)
        self.assertEqual(self.file.read_bytes(), corrupt)
        # The single fallback: exactly the seed set, no record extension.
        yaml = self.config.read_text()
        for alias in continuity.seed_only(self.bundle())["aliases"]:
            self.assertIn(f'alias: "{alias}"', yaml)

    def test_corrupt_continuity_renders_the_seed_set_only(self) -> None:
        state.ensure_private_dir(self.config_dir)
        state.atomic_write(self.file, b"garbage")
        self.write_record(RECORD_A, _managed_record(RECORD_A, "claude-multi-kimi-old[1m]"))
        _target, result, report = self.render()
        self.assertIsNotNone(report.corrupt)
        self.assertEqual(report.unservable, ())  # no record extension
        self.assertEqual(result.continuity_rendered, (MUSE_HIGH, MUSE_XHIGH))
        self.assertEqual(self.file.read_bytes(), b"garbage")

    def test_state_root_flag_usage(self) -> None:
        for args in (["--state-root"], ["--state-root", "relative/path"], ["--other"],
                     ["--state-root", "/a", "extra"]):
            with self.subTest(args=args), self.assertRaisesRegex(proxy.ProxyError, "usage"):
                proxy.cmd_run(args, environ=self.environ, execve=lambda *a: None)
        self.write_record(RECORD_A, _managed_record(RECORD_A, "claude-multi-grok45-high"))
        # Default: the passed environ's XDG_STATE_HOME (not the process one).
        _calls, _err = self.run_proxy()
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))
        other = self.root / "other-state"
        self.run_proxy(f"--state-root={other}")
        # Another root never moves the
        # authority; only `init --state-root R --adopt-root` does.
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))

    def test_relative_default_state_root_is_a_usage_error(self) -> None:
        # A relative XDG_STATE_HOME is ignored (as the installer ignores it):
        # the default is the HOME-relative root.
        for value in ("rel/state", "."):
            with self.subTest(value=value):
                self.assertEqual(sessions.state_root({**self.environ, "XDG_STATE_HOME": value}),
                                 Path(self.environ["HOME"]) / ".local" / "state" / "claude-multi")
        # A relative HOME-derived default gets the same usage error as an
        # explicit relative --state-root, before anything renders.
        before = self.file.read_bytes() if self.file.exists() else None
        environ = {key: value for key, value in self.environ.items() if key != "XDG_STATE_HOME"}
        environ["HOME"] = "rel-home"
        with self.assertRaisesRegex(
            proxy.ProxyError,
            r"^usage: claude-multi-proxy run \[--state-root /abs/path\] "
            r"\(the default state root .* is not absolute\)$",
        ):
            proxy.cmd_run([], environ=environ, execve=lambda *a: None)
        self.assertEqual(self.file.read_bytes() if self.file.exists() else None, before)
        # An explicit absolute flag still wins over the relative default.
        with redirect_stderr(io.StringIO()):
            proxy.cmd_run(["--state-root", str(self.state_root)], environ={**self.environ,
                          "XDG_STATE_HOME": "rel/state"}, execve=lambda *a: None)
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))


class RootAuthorityTests(ContinuityCase):
    """The persisted root is
    the authority; renders from another root are additive; unit preparation
    never asks."""

    def managed_with_tombstone(self) -> bytes:
        state.ensure_private_dir(self.config_dir)
        document = continuity.apply_prune(
            continuity.seed_only(self.bundle()), continuity.PrunePlan((MUSE_HIGH,), ()), 31,
        )
        document["state_root"] = str(self.state_root)
        continuity.write(self.home, document)
        return self.file.read_bytes()

    def test_startup_render_other_root_is_additive_and_exit_0(self) -> None:
        before = self.managed_with_tombstone()
        other = self.root / "other-state" / "claude-multi"
        sessions_dir = state.ensure_private_dir(other / "sessions")
        state.atomic_write(sessions_dir / f"{RECORD_A}.json", strict_json.canonical_file_bytes(
            _managed_record(RECORD_A, MUSE_HIGH + "[1m]")))
        code, _out, err = self.init("--prepare-start", "--state-root", str(other))
        self.assertEqual(code, 0, err)
        # Published as a same-root render would (catalog and declarations),
        # the persisted continuity set neither extended, pruned nor moved.
        self.assertEqual(self.file.read_bytes(), before)
        yaml = self.config.read_text()
        self.assertIn(f'alias: "{MUSE_XHIGH}"', yaml)
        self.assertIn('alias: "claude-multi-kimi-k3"', yaml)
        self.assertNotIn(f'alias: "{MUSE_HIGH}"', yaml)
        _target, _result, report = self.render(other)
        self.assertTrue(report.other_root)
        self.assertEqual(report.managed_root, str(self.state_root))
        self.assertEqual(self.file.read_bytes(), before)
        # The same live record under the managed root revives the alias.
        self.write_record(RECORD_A, _managed_record(RECORD_A, MUSE_HIGH + "[1m]"))
        _target, result, report = self.render()
        self.assertFalse(report.other_root)
        self.assertIn(f'alias: "{MUSE_HIGH}"', result.yaml)

    def test_service_preparation_never_prompts(self) -> None:
        code, _out, err = self.init("--state-root", str(self.state_root))
        self.assertEqual(code, 0, err)
        self.assertNotIn("Pending served change", err)  # nothing published yet to compare
        # An out-of-band declaration: the next start publishes it unprompted.
        providers = state.ensure_private_dir(self.config_dir / "providers.d")
        state.atomic_write(providers / "lan.json", strict_json.pretty_file_bytes({
            "version": 1,
            "provider": {"display": "LAN", "kind": "openai-compatible-lan", "base_url": "http://box.lan:8000/v1",
                         "auth": {"kind": "none"}, "independence_family": "local"},
            "lines": {"custom-lan-model": {"wire_model": "lan-model", "display": "LAN model", "efforts": ["high"],
                                           "default_effort": "high",
                                           "context": {"declared_tokens": 32768, "source": "operator"}}},
        }))

        class NoInput:
            def read(self, *_a):
                raise AssertionError("service preparation read stdin")

            readline = read

            def isatty(self):
                return False

        with mock.patch("sys.stdin", NoInput()), \
                mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            code, _out, err = self.init("--prepare-start", "--state-root", str(self.state_root))
            self.assertEqual(code, 0, err)
            self.assertNotIn("Pending served change", err)
            self.assertIn('alias: "custom-lan-model"', self.config.read_text())
            # A manual init shows the pending summary on stderr, never asks.
            providers.joinpath("lan.json").write_bytes(
                providers.joinpath("lan.json").read_bytes().replace(b"/v1", b"/v2"))
            code, out, err = self.init("--state-root", str(self.state_root))
            self.assertEqual(code, 0, err)
        self.assertIn("Pending served change (publishing now; nothing is asked)", err)
        self.assertIn("Retargeted\n  custom-lan-model: lan http://box.lan:8000/v1 · lan-model → "
                      "lan http://box.lan:8000/v2 · lan-model", err)
        self.assertNotIn("Pending served change", out)


class LockingTests(ContinuityCase):
    """Item 15: the api-key lock is a leaf and never re-acquired."""

    def test_render_blocks_on_the_api_key_lock_and_never_writes(self) -> None:
        state.ensure_private_dir(self.config_dir)
        lock = state.FileLock(self.config_dir / "api-key")
        self.assertTrue(lock.acquire(blocking=False))
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                self.render()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        try:
            thread.start()
            thread.join(0.5)
            self.assertTrue(thread.is_alive())
            self.assertFalse(self.file.exists())
            self.assertFalse(self.config.exists())
        finally:
            lock.release()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.file.exists())

    def test_rotation_never_nests_a_lock_acquisition(self) -> None:
        state.ensure_private_dir(self.config_dir)
        state.atomic_write(self.config_dir / "api-key", (("a" * 64) + "\n").encode())
        original_acquire, original_release = state.FileLock.acquire, state.FileLock.release
        held: set[str] = set()
        counts: dict[str, int] = {}

        def acquire(lock, blocking=True):
            name = lock.lock_path.name
            if name in held:
                raise AssertionError(f"nested acquisition of {name}")
            result = original_acquire(lock, blocking)
            if result:
                held.add(name)
                counts[name] = counts.get(name, 0) + 1
            return result

        def release(lock):
            held.discard(lock.lock_path.name)
            return original_release(lock)

        def getter(_base, token):
            import re

            yaml = self.config.read_text()
            block = yaml.split("api-keys:\n", 1)[1].split("debug:", 1)[0]
            keys = re.findall(r'"([0-9a-f]{64})"', block)
            ids = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
            return (200, ids) if token in keys else (401, set())

        clock = [0.0]
        with mock.patch.object(state.FileLock, "acquire", acquire), \
                mock.patch.object(state.FileLock, "release", release):
            outcome = proxy.rotate_token(
                self.home, environ=self.environ, resolver=lambda _: None,
                models_get=getter, clock=lambda: clock[0],
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                live_sessions=lambda: (), state_root=self.state_root,
            )
        self.assertEqual(outcome.status, "reloaded")
        # Four separate api-key sections (render dual, publish, render
        # single, remove previous-key), none nested inside another; each
        # inside the gateway inhibition's writer fence (re-checked per phase).
        self.assertEqual(counts, {"token-rotation.lock": 1, "gateway-inhibition.lock": 4, "api-key.lock": 4})
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))


def _document(bundle, aliases, *, providers=None, resolve=lambda _name: "dummy-key"):
    return render.build_config_document(
        bundle.docs["gateway"],
        providers or bundle.providers,
        bundle.lines,
        home=Path("/home/test"),
        gateway_token="a" * 64,
        resolve_secret=resolve,
        continuity=aliases,
    )


def _entry(provider: str, wire: str, contract: str | None, **extra) -> dict:
    return {
        "provider": provider, "wire": wire, "proxy_contract": contract,
        "display": extra.get("display", "Old line"), "context_tokens": 1000000,
        "source": "record", "since_catalog": 31,
    }


def _section_aliases(document, provider_id, provider) -> set[str]:
    transport = provider["transport"]
    if transport["kind"] == "oauth-pool":
        return {
            entry["alias"]
            for entry in document["oauth-model-alias"].get(transport["pool"], [])
        }
    if transport["kind"] == "direct":
        items = [i for i in document["claude-api-key"] if i["base-url"] == transport["base_url"]]
    else:
        items = [i for i in document["openai-compatibility"] if i["name"] == provider_id]
    return {model["alias"] for item in items for model in item["models"]}


class RenderMergeTests(unittest.TestCase):
    """Item 16: catalog first, continuity after; never a RenderError."""

    def setUp(self) -> None:
        self.bundle = catalog.load_catalog(FIXTURE_ROOT)

    def test_catalog_wins_a_colliding_alias(self) -> None:
        aliases = {
            "claude-multi-kimi-k3": _entry("meta", "other-wire", "output-config-high"),
            "gpt-multi-sol-high": _entry("openai", "gpt-5.5", "reasoning-effort-high"),
            "claude-opus-4-8": _entry("anthropic", "claude-opus-4-8", None),
        }
        document, _a, _u, info = _document(self.bundle, aliases)
        yaml = render.emit_yaml(document)
        self.assertEqual(info["continuity_rendered"], ())
        self.assertEqual(info["notices"], ())
        self.assertNotIn("other-wire", yaml)
        for alias in aliases:
            self.assertEqual(yaml.count(f'alias: "{alias}"'), 1, alias)

    def test_kimi_continuity_alias_gets_override_and_filter(self) -> None:
        aliases = {"claude-multi-kimi-old": _entry("kimi", "k2", "output-config-max")}
        document, _a, _u, info = _document(self.bundle, aliases)
        self.assertEqual(info["continuity_rendered"], ("claude-multi-kimi-old",))
        max_override = next(
            entry for entry in document["payload"]["override"]
            if entry["params"] == {"output_config.effort": "max"}
        )
        self.assertIn({"name": "claude-multi-kimi-old", "protocol": "claude"}, max_override["models"])
        thinking = next(
            entry for entry in document["payload"]["filter"] if entry["params"] == ["thinking"]
        )
        self.assertIn({"name": "claude-multi-kimi-old", "protocol": "claude"}, thinking["models"])
        kimi = self.bundle.providers["kimi"]
        section = next(
            item for item in document["claude-api-key"]
            if item["base-url"] == kimi["transport"]["base_url"]
        )
        self.assertEqual(section["models"][-1], {
            "name": "k2", "alias": "claude-multi-kimi-old", "display-name": "Old line",
            "owned-by": kimi["independence_family"], "context-length": 1000000,
            "force-mapping": True,
        })

    def test_undeclared_contract_keeps_alias_without_override(self) -> None:
        aliases = {"claude-multi-kimi-old": _entry("kimi", "k2", "output-config-low")}
        document, _a, _u, info = _document(self.bundle, aliases)
        self.assertEqual(info["continuity_rendered"], ("claude-multi-kimi-old",))
        self.assertEqual(
            info["notices"],
            ("continuity alias claude-multi-kimi-old: effort rule output-config-low unavailable",),
        )
        overrides = [
            model["name"] for entry in document["payload"]["override"] for model in entry["models"]
        ]
        self.assertNotIn("claude-multi-kimi-old", overrides)

    def test_unconfigured_provider_dropped_with_notice(self) -> None:
        aliases = {"claude-multi-gone": _entry("nowhere", "gone-1", None)}
        document, _a, _u, info = _document(self.bundle, aliases)
        self.assertEqual(info["continuity_rendered"], ())
        self.assertEqual(
            info["notices"], ("continuity alias claude-multi-gone: provider nowhere is not configured",),
        )
        self.assertNotIn("claude-multi-gone", render.emit_yaml(document))

    def test_unavailable_provider_omits_its_continuity_silently(self) -> None:
        document, _a, unavailable, info = _document(
            self.bundle, continuity.seed_only(self.bundle)["aliases"], resolve=lambda _n: None,
        )
        self.assertIn("meta", {item["provider"] for item in unavailable})
        # The captures/overlay report keys (empty without an operator layer).
        self.assertEqual(info, {"continuity_rendered": (), "notices": (), "captures_rendered": (),
                                "overlay_conflicts": ()})

    def test_oauth_continuity_on_a_routed_wire_forks(self) -> None:
        aliases = {
            "claude-multi-opus-old": _entry("anthropic", "claude-opus-4-8", None),
            "gpt-multi-old-high": _entry("openai", "gpt-5.1", "reasoning-effort-high"),
        }
        document, _a, _u, info = _document(self.bundle, aliases)
        claude = document["oauth-model-alias"]["claude"]
        self.assertEqual(claude[-1], {
            "name": "claude-opus-4-8", "alias": "claude-multi-opus-old",
            "force-mapping": True, "fork": True,
        })
        self.assertEqual(document["oauth-model-alias"]["codex"][-1]["fork"], False)
        self.assertEqual(info["continuity_rendered"], ("claude-multi-opus-old", "gpt-multi-old-high"))

    def test_provider_selectors_equal_the_built_sections(self) -> None:
        cases = {
            "none": {},
            "seed": continuity.seed_only(self.bundle)["aliases"],
            "mixed": {
                **continuity.seed_only(self.bundle)["aliases"],
                "claude-multi-kimi-old": _entry("kimi", "k2", "output-config-max"),
                "claude-multi-opus-old": _entry("anthropic", "claude-opus-4-8", None),
                "claude-multi-kimi-k3": _entry("meta", "other-wire", None),
            },
        }
        for label, aliases in cases.items():
            for resolve in (lambda _n: "dummy", lambda _n: None):
                document, available, _u, _i = _document(self.bundle, aliases, resolve=resolve)
                for provider_id, provider in sorted(self.bundle.providers.items()):
                    with self.subTest(case=label, provider=provider_id, keyed=resolve("x")):
                        self.assertEqual(
                            render.provider_selectors(
                                provider_id, provider, self.bundle.lines,
                                available=provider_id in available,
                                continuity=aliases, providers=self.bundle.providers,
                            ),
                            frozenset(_section_aliases(document, provider_id, provider)),
                        )

    def test_malformed_continuity_never_raises(self) -> None:
        aliases = {
            "claude-multi-bad": {"provider": "kimi"},
            render.SENTINEL_PREFIX + "deadbeef": _entry("kimi", "k2", None),
        }
        _document_, _a, _u, info = _document(self.bundle, aliases)
        self.assertEqual(info["continuity_rendered"], ())
        self.assertEqual(len(info["notices"]), 2)


class ZeroModelRestoreAsCustomTests(ContinuityCase):
    """A custom model on a model-less catalog provider still renders."""

    def test_custom_model_on_a_zero_model_provider(self) -> None:
        from claude_multi import custom

        bundle = self.bundle()
        self.assertFalse(any(line["provider"] == "deepseek" for line in bundle.lines.values()))
        state.atomic_write(
            self.secret_file,
            b"KIMI_CLAUDE_API_KEY=k\nMETA_CLAUDE_API_KEY=m\nDEEPSEEK_CLAUDE_API_KEY=d\n",
        )
        custom.add_model(
            self.environ, "restored-pro", wire_model="deepseek-v4-pro", provider="deepseek",
            context_tokens=1000000, created_via="manual",
            catalog_providers=bundle.providers,
            catalog_models=tuple(bundle.lines),
            retired_models=custom.retired_model_ids(bundle.docs),
        )
        _target, result, _report = self.render()
        self.assertIn('alias: "custom-restored-pro"', result.yaml)
        self.assertIn("deepseek", result.available_providers)


class VerifyMinorsTests(ContinuityCase):
    """Never a traceback, never prune blind."""

    def prune(self, *names: str) -> proxy.PruneOutcome:
        return proxy.prune_aliases(
            self.home, self.state_root, list(names) or None,
            environ=self.environ, models_get=self.models_get,
            clock=lambda: 0.0, sleep=lambda _s: None,
        )

    def set_up_gateway(self) -> None:
        """The gateway's port is recorded: the proxy command runs and renders here."""

        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18399))

    def test_deeply_nested_continuity_is_a_continuity_error(self) -> None:
        self.set_up_gateway()
        state.ensure_private_dir(self.config_dir)
        state.atomic_write(self.file, b"[" * 200_000)
        with self.assertRaisesRegex(continuity.ContinuityError, "nesting too deep"):
            continuity.read(self.home)
        # run refuses (the managed root is unknown) without a traceback, and
        # prune refuses in one line.
        binary = self.root / "bin" / "cli-proxy-api"
        binary.parent.mkdir()
        binary.write_bytes(b"#!/bin/fake\n")
        binary.chmod(0o755)
        self.environ["CLAUDE_MULTI_PROXY_BIN"] = str(binary)
        calls, err = [], io.StringIO()
        with redirect_stderr(err):
            code = proxy.main(["run"], environ=self.environ, execve=lambda *a: calls.append(a))
        self.assertEqual((code, len(calls)), (1, 0))
        self.assertIn("gateway continuity set unreadable (continuity.json: nesting too deep)",
                      err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        outcome = self.prune()
        self.assertEqual(outcome.code, 1)
        self.assertIn("gateway continuity set unreadable", outcome.lines[0])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unlistable_sessions_dir_refuses_prune(self) -> None:
        self.assertEqual(self.init()[0], 0)
        self.write_record(RECORD_A, _managed_record(RECORD_A, MUSE_HIGH + "[1m]"))
        sessions = self.state_root / "sessions"
        os.chmod(sessions, 0o000)
        self.addCleanup(os.chmod, sessions, 0o700)
        scan = continuity.scan_records(self.state_root)
        self.assertIsNotNone(scan.directory_error)
        self.assertEqual((scan.refs, scan.live, scan.unreadable), ({}, frozenset(), ()))
        self.assertTrue(scan.notices[0].startswith("sessions directory unreadable"))
        before = self.file.read_bytes()
        outcome = self.prune()
        self.assertEqual(outcome.code, 1)
        self.assertEqual(len(outcome.lines), 1)
        self.assertTrue(outcome.lines[0].startswith(
            "cannot prove liveness: sessions directory unreadable ("
        ), outcome.lines)
        self.assertTrue(outcome.lines[0].endswith("; nothing pruned"))
        self.assertEqual(self.file.read_bytes(), before)
        # A render is still never failed by it: the notice is reported.
        code, _out, err = self.init()
        self.assertEqual(code, 0)
        self.assertIn("continuity: sessions directory unreadable", err)

    def test_null_scope_model_refuses_alias_prune_planning(self) -> None:
        """A present null model is unknown, not permission to prune an alias."""
        self.assertEqual(self.init()[0], 0)
        self.write_record(RECORD_A, _managed_record(RECORD_A, "claude-opus-4-8", event="start"))
        self.write_scope_settings(RECORD_A, {"model": None})
        before = self.file.read_bytes()
        plan = proxy._plan_alias_prune(
            self.home, self.state_root, [MUSE_HIGH], environ=self.environ,
            env=self.environ, is_process_live=None, pinned=None,
        )
        self.assertIsInstance(plan, proxy.PruneOutcome)
        self.assertEqual(plan.code, 1)
        self.assertIn("unreadable scope fences", plan.lines[0])
        self.assertEqual(self.file.read_bytes(), before)

    def test_root_state_root_is_a_usage_error(self) -> None:
        for value in ("/", "//"):
            for args in (["--state-root", value], [f"--state-root={value}"]):
                with self.subTest(args=args), self.assertRaisesRegex(proxy.ProxyError, "usage"):
                    proxy.cmd_init(args, environ=self.environ, models_get=self.models_get)
        err = io.StringIO()
        with redirect_stderr(err):
            code = proxy.main(["init", "--state-root", "/"], environ=self.environ,
                              models_get=self.models_get)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(),
                         "claude-multi-proxy: usage: claude-multi-proxy init "
                         "[--state-root /abs/path]\n")
        self.assertFalse(self.file.exists())

    def test_main_reports_a_continuity_error_in_one_line(self) -> None:
        self.set_up_gateway()
        err = io.StringIO()
        with mock.patch.object(
            continuity, "write", side_effect=continuity.ContinuityError("bad set"),
        ), redirect_stderr(err):
            code = proxy.main(["init"], environ=self.environ, models_get=self.models_get)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "claude-multi-proxy: bad set\n")

    def _seed_route_alias(self) -> None:
        self.assertEqual(self.init()[0], 0)
        document = continuity.read(self.home)
        # claude-fable-5 is an anthropic passthrough route in the fixture
        # (like the shipped fable@5 / fable51 seeds): the catalog serves it.
        document["aliases"]["claude-fable-5"] = _entry("anthropic", "claude-fable-5", None)
        continuity.write(self.home, document)

    def test_all_mode_keeps_a_catalog_served_alias(self) -> None:
        self._seed_route_alias()
        outcome = self.prune()
        self.assertEqual(outcome.code, 0, outcome.lines)
        self.assertIn(f"pruned: {MUSE_HIGH} (meta muse-spark-1.3)", outcome.lines)
        self.assertIn(
            "kept: claude-fable-5 — still served as a catalog route/live selector",
            outcome.lines,
        )
        self.assertFalse(any(line.startswith("pruned: claude-fable-5") for line in outcome.lines))
        document = continuity.read(self.home)
        self.assertEqual(set(document["aliases"]), {"claude-fable-5"})
        self.assertNotIn("claude-fable-5", document["pruned"])

    def test_named_catalog_served_alias_is_pruned_with_a_note(self) -> None:
        self._seed_route_alias()
        outcome = self.prune("claude-fable-5")
        self.assertEqual(outcome.code, 0, outcome.lines)
        self.assertEqual(
            outcome.lines[0],
            "pruned: claude-fable-5 (anthropic claude-fable-5) — "
            "still served as a catalog route/live selector",
        )
        self.assertEqual(continuity.read(self.home)["pruned"], {"claude-fable-5": 31})
        # The route keeps serving it.
        self.assertIn('"claude-fable-5"', self.config.read_text())


class PrunePlanTests(unittest.TestCase):
    """Pure plan rules (the CLI flow is in test_cli PruneAliasesTests)."""

    def setUp(self) -> None:
        self.bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.document = continuity.seed_only(self.bundle)
        self.document["aliases"]["claude-opus-5"] = _entry("anthropic", "claude-opus-5", None)

    def scan(self, refs=None, live=()):
        return continuity.RecordScan(
            refs={k: frozenset(v) for k, v in (refs or {}).items()},
            live=frozenset(live), unreadable=(), notices=(),
        )

    def test_all_mode_keeps_live_and_refusal_targets(self) -> None:
        plan = continuity.plan_prune(
            self.document, self.bundle, self.scan({MUSE_HIGH: {RECORD_A}}, {RECORD_A}), None,
        )
        self.assertEqual(plan.remove, (MUSE_XHIGH,))
        self.assertEqual(plan.keep, (
            (MUSE_HIGH, f"live session {RECORD_A[:8]}"),
            ("claude-opus-5", "refusal-fallback target"),
        ))
        self.assertIsNone(plan.refusal)

    def test_named_live_alias_refuses_the_whole_operation(self) -> None:
        plan = continuity.plan_prune(
            self.document, self.bundle, self.scan({MUSE_HIGH: {RECORD_A}}, {RECORD_A}),
            frozenset({MUSE_HIGH + "[1m]", MUSE_XHIGH}),
        )
        self.assertEqual(plan.remove, ())
        self.assertIn(f"{MUSE_HIGH} -> {RECORD_A[:8]}", plan.refusal)
        self.assertEqual(plan.holders, (RECORD_A,))  # The full id the remedy names

    def test_ended_references_allow_removal_and_unknown_names_refuse(self) -> None:
        plan = continuity.plan_prune(
            self.document, self.bundle, self.scan({MUSE_HIGH: {RECORD_A}}), frozenset({MUSE_HIGH}),
        )
        self.assertEqual(plan.remove, (MUSE_HIGH,))
        plan = continuity.plan_prune(self.document, self.bundle, self.scan(), frozenset({"nope"}))
        self.assertIn("unknown continuity alias", plan.refusal)
        pruned = continuity.apply_prune(self.document, continuity.PrunePlan((MUSE_HIGH,), ()), 33)
        self.assertNotIn(MUSE_HIGH, pruned["aliases"])
        self.assertEqual(pruned["pruned"], {MUSE_HIGH: 33})
        self.assertIn(MUSE_HIGH, self.document["aliases"])  # input untouched


if __name__ == "__main__":
    unittest.main()
