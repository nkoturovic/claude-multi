"""Tests for developer onboarding: drafts, lifecycle, promotion, smoke gate."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

from claude_multi import catalog, dev, sessions, state, strict_json
from claude_multi.dev import DevError
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, fake_checkout


# The isolated fake checkout is the frozen fixture asset root (all that
# load_raw/verify read); the import-hygiene glob reads the package src.
CATALOG_ROOT = FIXTURE_ROOT
# Promotion post-images are checkout-relative (the resources sit under
# src/claude_multi/data in a checkout).
MODELS_IMAGE = str(dev.RESOURCES / "catalog" / "models.json")
PROVIDERS_IMAGE = str(dev.RESOURCES / "catalog" / "providers.json")


def _gateway_baseline() -> str:
    """The catalog's current gateway baseline: a newly onboarded model
    is first tested on the gateway the catalog pins, never a literal."""

    return catalog.load_catalog(CATALOG_ROOT).docs["gateway"]["gateway"][
        "cliproxyapi_baseline"
    ]


def _model_entry(model_id: str = "newmodel") -> dict:
    """A v2 (catalog 33) gateway-effort line draft entry, New · Off."""

    return {
        "id": model_id,
        "provider": "openai",
        "display": "New Model",
        "generation": "1",
        "wire_model": "gpt-new",
        "capabilities": ["agents"],
        "roles": ["cm-reviewer"],
        "context": {
            "client_tokens": 128000,
            "provider_tokens": 128000,
            "scalar_tokens": 128000,
            "ordinary_profile": None,
            "declared_tokens": 128000,
            "validated_tokens": 128000,
            "qualification": "onboarding test fixture",
        },
        "efforts": {
            "high": {
                # The promote shape: <codex prefix><key>-<effort>.
                "selector": f"gpt-multi-{model_id}-high",
                "proxy_contract": "reasoning-effort-high",
            }
        },
        "default_effort": "high",
        "lead": None,
        "routing_note": "Fixture model for onboarding tests.",
        "minimum_tested": {"claude_code": "2.1.216", "cliproxyapi": _gateway_baseline()},
        "status": "new",
        "registry_overlay": None,
    }


def _model_draft(model_id: str = "newmodel") -> dict:
    return dev.make_model_draft(
        name="d1",
        provider="openai",
        entry=_model_entry(model_id),
        now="2026-07-21T00:00:00Z",
    )


def _provider_draft() -> dict:
    profile = {
        "id": "zeta",
        "display": "Zeta",
        "independence_family": "zeta",
        "support": "locally-validated-experimental",
        "support_note": "Fixture provider.",
        "adapter": "cliproxy-claude-compatible-v1",
        "transport": {
            "kind": "direct",
            "base_url": "https://api.example.com/coding",
            "auth": {
                "kind": "header",
                "header": "x-api-key",
                "secret_ref": "env:ZETA_API_KEY",
            },
        },
        "passthrough_routes": [],
        "payload_contracts": ["output-config-max", "filter-thinking"],
    }
    model = _model_entry("zetamodel")
    model["provider"] = "zeta"
    model["wire_model"] = "z1"
    model["efforts"] = {
        "max": {"selector": "claude-multi-zetamodel-max[1m]", "proxy_contract": "output-config-max"}
    }
    model["default_effort"] = "max"
    model["context"] = {
        "client_tokens": 1000000,
        "provider_tokens": 1000000,
        "scalar_tokens": None,
        "ordinary_profile": None,
        "declared_tokens": 1000000,
        "validated_tokens": 100000,
        "qualification": "fixture",
    }
    return dev.make_provider_draft(
        name="d2",
        provider_profile=profile,
        model_entry=model,
        contract_claims=["routing", "streaming"],
        now="2026-07-21T00:00:00Z",
    )


class OnboardingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-dev-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.drafts = dev.DraftStore(self.root / "drafts")
        # Isolated fake checkout: the markers and the fixture resources at
        # the checkout's resource path (what load_raw/verify need).
        self.repo = fake_checkout(self.root / "repo", CATALOG_ROOT)
        self.resources = self.repo / dev.RESOURCES

    def _review(self, draft, name="d1", runner=None):
        return dev.review_draft(
            draft, draft_name=name, repo=self.repo,
            revision="deadbeef", now="2026-07-21T00:00:00Z",
            runner=runner or (lambda c, w: {"cmd": c, "returncode": 0}),
            candidate_parent=Path(tempfile.mkdtemp(dir=self.root)),
        )



class DraftStateMarkerTests(OnboardingTestCase):
    def test_draft_save_refuses_but_load_is_read_only(self):
        draft = _model_draft()
        path = self.drafts.save("d1", draft)
        before = path.read_bytes()
        state.atomic_write(self.root / sessions.STATE_MARKER, b"5\n")
        with self.assertRaises(sessions.StateMarkerError):
            self.drafts.save("d1", {"changed": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.drafts.load("d1"), draft)

    def test_mutating_cli_refuses_before_draft_store_initialization(self):
        state_root = state.ensure_private_dir(self.root / "claude-multi")
        state.atomic_write(state_root / sessions.STATE_MARKER, b"5\n")
        for command in (
            ["model", "add"], ["provider", "add"], ["review", "d1"],
            ["promote", "d1", "--repo", str(self.repo)],
            ["promote", "d1", "--patch-output", str(self.root / "out.diff")],
        ):
            with self.subTest(command=command), mock.patch.dict(
                os.environ, {"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root)}
            ), mock.patch.object(dev, "_draft_store") as store, mock.patch(
                "sys.stderr", io.StringIO()
            ) as error:
                self.assertEqual(dev.main(command), 2)
                self.assertIn("newer claude-multi", error.getvalue())
                store.assert_not_called()
        self.assertFalse((state_root / "drafts").exists())

    def test_promote_refuses_before_source_writes_or_journal_creation(self):
        draft = _model_draft()
        review = self._review(draft)
        path = self.repo / dev.RESOURCES / "catalog" / "models.json"
        before = path.read_bytes()
        state.atomic_write(self.root / sessions.STATE_MARKER, b"5\n")
        with self.assertRaises(sessions.StateMarkerError), mock.patch.object(
            dev, "_repo_atomic_write"
        ) as write:
            dev.promote_draft(
                draft, draft_name="d1", repo=self.repo, review_record=review,
                drafts_root=self.drafts.root,
            )
        write.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.drafts.root / "d1.journal.json").exists())

    def test_review_rechecks_marker_before_writing_output(self):
        state_root = state.ensure_private_dir(self.root / "claude-multi")
        store = dev.DraftStore(state_root / "drafts")
        store.save("d1", _model_draft())

        def review(*args, **kwargs):
            state.atomic_write(state_root / sessions.STATE_MARKER, b"5\n")
            return {"results": {"diff": "fixture"}}

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}), mock.patch.object(
            dev, "review_draft", side_effect=review
        ), mock.patch("sys.stderr", io.StringIO()) as error:
            self.assertEqual(dev.main(["review", "d1", "--repo", str(self.repo)]), 2)
        self.assertIn("newer claude-multi", error.getvalue())
        self.assertFalse((store.root / "d1.review.json").exists())

    def test_check_remains_read_only_under_marker(self):
        state_root = state.ensure_private_dir(self.root / "claude-multi")
        store = dev.DraftStore(state_root / "drafts")
        store.save("d1", _model_draft())
        state.atomic_write(state_root / sessions.STATE_MARKER, b"5\n")
        result = mock.Mock(draft_hash="fixture", builds=[], registry=())
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}), mock.patch.object(
            dev, "check_draft", return_value=result
        ) as check, redirect_stdout(io.StringIO()):
            self.assertEqual(dev.main(["check", "d1", "--repo", str(self.repo)]), 0)
        check.assert_called_once()


class ScaffoldTests(OnboardingTestCase):
    """2.13.1: claude-multi-dev model add --like scaffold."""

    def _docs(self):
        # The scaffold reads RAW catalog docs (models v2 + retired).
        return catalog.load_raw(CATALOG_ROOT)["docs"]

    def test_scaffold_inherits_mechanical_and_marks_judgment(self) -> None:
        entry = dev._scaffold_model_entry(
            self._docs(), like_id="qwen38", new_id="qwen39", wire_model="qwen3.9-max"
        )
        self.assertEqual(entry["wire_model"], "qwen3.9-max")
        self.assertEqual(entry["provider"], "qwen")
        self.assertNotIn("selector", entry)
        self.assertEqual(
            entry["efforts"],
            {"max": {"selector": "claude-multi-qwen39-max[1m]",
                     "proxy_contract": "reasoning-effort-xhigh"}},
        )
        self.assertIn("QUALIFY", entry["display"])
        self.assertIn("QUALIFY", entry["routing_note"])
        self.assertIn("QUALIFY", entry["generation"])
        self.assertIn("QUALIFY", entry["context"]["qualification"])
        self.assertEqual(entry["status"], "new")
        self.assertIsNone(entry["registry_overlay"])
        self.assertEqual(entry["roles"], self._docs()["models"]["models"]["qwen38"]["roles"])
        for removed in ("role_hints", "lanes", "client_selector", "compatible_roles", "default_lane"):
            self.assertNotIn(removed, entry)
        self.assertNotIn("user_reported_tokens", entry["context"])
        self.assertLessEqual(entry["context"]["validated_tokens"], 200000)

    def test_scaffold_records_the_catalog_gateway_baseline(self) -> None:
        # minimum_tested.claude_code is inherited from the sibling, but
        # the gateway half is the catalog's CURRENT baseline — the sibling
        # was first tested on an older gateway than the new model will be.
        docs = copy.deepcopy(self._docs())
        docs["gateway"]["gateway"]["cliproxyapi_baseline"] = "9.8.7"
        like = docs["models"]["models"]["qwen38"]
        entry = dev._scaffold_model_entry(
            docs, like_id="qwen38", new_id="qwen39", wire_model="qwen3.9-max"
        )
        self.assertEqual(
            entry["minimum_tested"],
            {"claude_code": like["minimum_tested"]["claude_code"], "cliproxyapi": "9.8.7"},
        )
        self.assertNotEqual(like["minimum_tested"]["cliproxyapi"], "9.8.7")

    def test_scaffold_rejects_unknown_like_and_collisions(self) -> None:
        with self.assertRaises(dev.DevError):
            dev._scaffold_model_entry(
                self._docs(), like_id="nope", new_id="x1", wire_model="w"
            )
        # An existing catalog key is refused outright.
        with self.assertRaisesRegex(dev.DevError, "already exists"):
            dev._scaffold_model_entry(
                self._docs(), like_id="qwen38", new_id="kimi-k3", wire_model="w"
            )
        # A new key whose derived selectors already exist must fail too:
        # a test-local live line already serving claude-multi-qwen39-max.
        docs = copy.deepcopy(self._docs())
        other = copy.deepcopy(docs["models"]["models"]["qwen38"])
        other["wire_model"] = "other-wire"
        other["efforts"]["max"]["selector"] = "claude-multi-qwen39-max[1m]"
        docs["models"]["models"]["other"] = other
        with self.assertRaisesRegex(dev.DevError, "selector collision"):
            dev._scaffold_model_entry(
                docs, like_id="qwen38", new_id="qwen39", wire_model="w"
            )

    def test_scaffold_refuses_retired_keys_and_retired_selectors(self) -> None:
        docs = copy.deepcopy(self._docs())
        retired = docs["retired"]["retired"]
        # the fixture's retired key, and an @ base
        with self.assertRaisesRegex(dev.DevError, "retired catalog key"):
            dev._scaffold_model_entry(
                docs, like_id="qwen38", new_id="muse-spark", wire_model="w"
            )
        retired["qwen40@1"] = copy.deepcopy(retired["muse-spark"])
        with self.assertRaisesRegex(dev.DevError, "retired catalog key"):
            dev._scaffold_model_entry(docs, like_id="qwen38", new_id="qwen40", wire_model="w")
        # a derived selector colliding with a retired 2.x selector
        retired["qwen-old"] = copy.deepcopy(retired["muse-spark"])
        retired["qwen-old"]["selectors"] = {"claude-multi-qwen39-max[1m]": "output-config-high"}
        with self.assertRaisesRegex(dev.DevError, "selector collision"):
            dev._scaffold_model_entry(docs, like_id="qwen38", new_id="qwen39", wire_model="w")
        with self.assertRaisesRegex(dev.DevError, "must match"):
            dev._scaffold_model_entry(docs, like_id="qwen38", new_id="Qwen_39", wire_model="w")

    def test_scaffold_derives_gateway_and_compat_selectors_by_shape(self) -> None:
        entry = dev._scaffold_model_entry(
            self._docs(), like_id="sol", new_id="terra", wire_model="gpt-6-terra"
        )
        self.assertEqual(
            {level: spec["selector"] for level, spec in entry["efforts"].items()},
            {"high": "gpt-multi-terra-high[1m]", "xhigh": "gpt-multi-terra-xhigh[1m]"},
        )
        local = dev._scaffold_model_entry(
            self._docs(), like_id="qwen-flash-next", new_id="qwen-local2", wire_model="w"
        )
        self.assertEqual(local["selector"], "claude-multi-qwen-local2")
        self.assertEqual(local["efforts"], ["high"])

    def test_scaffold_refuses_a_selector_shape_it_cannot_derive(self) -> None:
        # kimi-k3's selector carries no effort suffix (claude-multi-kimi-k3[1m]):
        # no substring mangling, the --from-json path instead.
        with self.assertRaisesRegex(dev.DevError, "cannot derive selectors.*--from-json"):
            dev._scaffold_model_entry(
                self._docs(), like_id="kimi-k3", new_id="kimi-k4", wire_model="k4"
            )

    def test_scaffold_refuses_anthropic_like_lines(self) -> None:
        # An Anthropic line's wire must be a passthrough
        # route of the pool, which a model draft cannot add.
        with self.assertRaisesRegex(dev.DevError, "Anthropic generations need a route edit"):
            dev._scaffold_model_entry(
                self._docs(), like_id="fable", new_id="fable6", wire_model="claude-fable-6"
            )

    def test_cli_scaffold_writes_validated_draft(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            with redirect_stdout(io.StringIO()) as out:
                code = dev.main(
                    [
                        "model", "add", "--like", "qwen38", "--id", "qwen39",
                        "--wire-id", "qwen3.9-max", "--name", "sc1",
                        "--repo", str(self.repo),
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn("QUALIFY", out.getvalue())
        store = dev.DraftStore(self.root / "claude-multi" / "drafts")
        draft = store.load("sc1")
        self.assertEqual(draft["entry"]["wire_model"], "qwen3.9-max")

    def test_scaffold_draft_entry_carries_the_id(self) -> None:
        # promote derives the catalog key from entry.id (build_post_images);
        # a scaffold without it would fail the advertised next `check` step.
        entry = dev._scaffold_model_entry(
            self._docs(), like_id="qwen38", new_id="qwen39", wire_model="qwen3.9-max"
        )
        self.assertEqual(entry["id"], "qwen39")

    def test_scaffold_errors_when_id_not_in_selector(self) -> None:
        # grok46's selectors lack the [1m] suffix but follow key-effort; a
        # like line whose selector does not read <prefix><like>-<level>
        # (test-local) fails with the --from-json guidance.
        docs = copy.deepcopy(self._docs())
        docs["models"]["models"]["grok46"]["efforts"]["high"]["selector"] = "claude-multi-grok-high"
        with self.assertRaises(dev.DevError) as ctx:
            dev._scaffold_model_entry(docs, like_id="grok46", new_id="grok47", wire_model="w")
        self.assertIn("--from-json", str(ctx.exception))

    def test_check_rejects_qualify_markers(self) -> None:
        draft = _model_draft()
        draft["entry"]["display"] = "QUALIFY: fill me"
        with self.assertRaises(dev.DevError) as ctx:
            dev.check_draft(draft, repo=self.repo)
        self.assertIn("QUALIFY", str(ctx.exception))

    def test_empty_argv_prints_the_same_help_with_usage_exit(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dev.main([]), 2)
        self.assertEqual(out.getvalue(), dev.DEV_HELP)

    def test_help_lists_both_tracks(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            code = dev.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Existing-provider model release", out.getvalue())
        self.assertIn("New provider / provider kind", out.getvalue())


class PrefillTests(OnboardingTestCase):
    """``model add --from-registry`` / ``--from-operator`` drafts.

    A synthetic pinned registry (temp ``CLAUDE_MULTI_REGISTRY_DIR``) and a
    temp operator HOME; invented wires only. Registry/listing values and
    operator evidence are review context; every judgment field stays
    QUALIFY-marked, so ``check`` refuses until a human fills them.
    """

    OPERATOR_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "operator" / "providers.d"

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        registry = self.root / "registry"
        registry.mkdir()
        (registry / "models.json").write_text(json.dumps({
            "claude": [{"id": "claude-fixture-9", "context_length": 1000000, "display_name": "Claude Nine"}],
            "codex-pro": [{"id": "gpt-fixture-7", "context_length": 400000, "max_completion_tokens": 128000,
                           "display_name": "GPT Seven", "created": 1790000000,
                           "thinking": {"levels": ["low", "high", "xhigh"]}}],
            "kimi": [{"id": "kimi-fixture-9", "context_length": 262144, "thinking": {"levels": ["max"]}}],
            "gemini": [{"id": "gemini-fixture"}],
        }))
        (registry / "codex_client_models.json").write_text('{"models": []}')
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.root),
                    "XDG_CONFIG_HOME": str(self.root / "config"), catalog.REGISTRY_DIR_ENV: str(registry)}

    def run_dev(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.env), redirect_stdout(io.StringIO()) as out, \
                mock.patch("sys.stderr", err):
            code = dev.main([*argv, "--repo", str(self.repo)])
        return code, out.getvalue() + err.getvalue()

    def draft(self, name: str) -> dict:
        return dev.DraftStore(self.root / "claude-multi" / "drafts").load(name)

    @staticmethod
    def fill_text_fields(draft: dict) -> dict:
        """Fill the text judgment fields a reviewer writes, leaving the rest."""

        entry = draft["entry"] if draft["kind"] == "model" else draft["entry"]["model"]
        entry.update(generation="9", display="Reviewed", routing_note="Reviewed routing.")
        entry["context"]["qualification"] = "Reviewed; not benchmark-verified."
        return draft

    def declare(self, *names: str) -> None:
        from claude_multi import operator as operator_mod

        directory = state.ensure_private_dir(operator_mod.providers_dir({"HOME": str(self.home)}))
        for name in names:
            state.atomic_write(directory / f"{name}.json", (self.OPERATOR_FIXTURES / f"{name}.json").read_bytes())

    def test_from_registry_prefills_by_catalog_rules_and_says_it_is_review_context(self) -> None:
        code, out = self.run_dev(["model", "add", "--from-registry", "codex-pro:gpt-fixture-7", "--id", "terra",
                                  "--name", "r1"])
        self.assertEqual(code, 0, out)
        draft_path = self.root / "claude-multi" / "drafts" / "r1.json"
        self.assertEqual(out, (
            f"draft: {draft_path}\n"
            "source: registry codex-pro:gpt-fixture-7\n"
            "Registry/listing values and operator qualification are review context, not catalog verification.\n"
            "Fill every QUALIFY field, then:\n"
            "  claude-multi-dev check r1\n"
            "Promotion installs a New · Off catalog line; local admission is a separate operator action.\n"))
        draft = self.draft("r1")
        entry = draft["entry"]
        self.assertEqual((draft["kind"], draft["provider"], entry["id"], entry["status"]),
                         ("model", "openai", "terra", "new"))
        # Only levels with a reviewed contract survive (no low contract here).
        self.assertEqual(entry["efforts"], {
            "high": {"selector": "gpt-multi-terra-high[1m]", "proxy_contract": "reasoning-effort-high"},
            "xhigh": {"selector": "gpt-multi-terra-xhigh[1m]", "proxy_contract": "reasoning-effort-xhigh"}})
        self.assertEqual(entry["default_effort"], "high")
        self.assertEqual((entry["context"]["declared_tokens"], entry["context"]["client_tokens"],
                          entry["context"]["validated_tokens"]), (400000, 1000000, 200000))
        for field in ("display", "generation", "routing_note"):
            self.assertIn("QUALIFY", entry[field])
        self.assertIn("QUALIFY", entry["context"]["qualification"])
        self.assertIn("registry-stated context 400000; max output 128000", draft["notes"])
        with self.assertRaisesRegex(DevError, "QUALIFY"):
            dev.check_draft(draft, repo=self.repo)

    def test_anthropic_candidate_names_the_route_prerequisite(self) -> None:
        code, out = self.run_dev(["model", "add", "--from-registry", "claude:claude-fixture-9", "--id", "nine",
                                  "--name", "r2"])
        self.assertEqual(code, 0, out)
        self.assertIn("prerequisite: this pool wire needs a reviewed passthrough-route change before check can pass\n",
                      out)
        entry = self.draft("r2")["entry"]
        self.assertEqual(entry["selector"], "claude-fixture-9[1m]")
        self.assertIn("QUALIFY", entry["efforts"])
        with self.assertRaisesRegex(DevError, "QUALIFY marker in 'efforts'"):
            dev.check_draft(self.fill_text_fields(self.draft("r2")), repo=self.repo)

    def test_channel_that_names_no_single_provider_needs_provider(self) -> None:
        code, out = self.run_dev(["model", "add", "--from-registry", "kimi:kimi-fixture-9", "--id", "k9"])
        self.assertEqual(code, 2)
        self.assertIn("name it with --provider PROVIDER", out)
        code, out = self.run_dev(["model", "add", "--from-registry", "kimi:kimi-fixture-9", "--id", "k9",
                                  "--provider", "kimi", "--name", "r3"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.draft("r3")["entry"]["efforts"], {
            "max": {"selector": "claude-multi-k9-max[1m]", "proxy_contract": "output-config-max"}})
        for argv in (["--from-registry", "kimi:nope", "--id", "k9"], ["--from-registry", "nochannel", "--id", "k9"],
                     ["--from-registry", "codex-pro:gpt-fixture-7", "--id", "sol"],
                     ["--from-registry", "codex-pro:gpt-fixture-7"]):
            with self.subTest(argv=argv):
                code, _out = self.run_dev(["model", "add", *argv])
                self.assertEqual(code, 2)

    def test_the_four_sources_are_mutually_exclusive(self) -> None:
        for extra in (["--like", "qwen38"], ["--from-json", "x.json"], ["--from-operator", "custom-x"]):
            with self.subTest(extra=extra):
                code, out = self.run_dev(["model", "add", "--from-registry", "codex-pro:gpt-fixture-7",
                                          "--id", "terra", *extra])
                self.assertEqual(code, 2)
                self.assertIn("choose one source", out)

    def test_from_operator_line_on_a_catalog_provider_is_a_model_draft(self) -> None:
        self.declare("kimi")
        code, out = self.run_dev(["model", "add", "--from-operator", "custom-kimi-next", "--id", "kimi-next",
                                  "--name", "o1"])
        self.assertEqual(code, 0, out)
        self.assertIn("source: operator custom-kimi-next\n", out)
        draft = self.draft("o1")
        self.assertEqual((draft["kind"], draft["provider"]), ("model", "kimi"))
        self.assertEqual(draft["entry"]["efforts"], {
            "max": {"selector": "claude-multi-kimi-next-max[1m]", "proxy_contract": "output-config-max"}})
        self.assertIn("operator qualification: none recorded", draft["notes"])
        self.assertNotIn("admission", json.dumps(draft["entry"]))

    def test_list_shaped_line_on_a_claude_compatible_provider_needs_an_efforts_map(self) -> None:
        # The draft is written, efforts QUALIFY-marked, check refuses.
        from claude_multi import operator as operator_mod

        directory = state.ensure_private_dir(operator_mod.providers_dir({"HOME": str(self.home)}))
        state.atomic_write(directory / "kimi.json", json.dumps({"version": 1, "lines": {"custom-kimi-list": {
            "wire_model": "kimi-list-1", "display": "Kimi list", "efforts": ["high"], "default_effort": "high",
            "context": {"declared_tokens": 131072, "source": "operator"}}}}).encode())
        code, out = self.run_dev(["model", "add", "--from-operator", "custom-kimi-list", "--id", "kimi-list",
                                  "--name", "o2"])
        self.assertEqual(code, 0, out)
        self.assertIn("prerequisite: custom-kimi-list is list-shaped but kimi is a claude-compatible "
                      "gateway-effort provider", out)
        draft = self.draft("o2")
        self.assertIn("QUALIFY", draft["entry"]["efforts"])
        self.assertIn("QUALIFY", draft["entry"]["default_effort"])
        with self.assertRaisesRegex(DevError, "QUALIFY marker in 'efforts'"):
            dev.check_draft(self.fill_text_fields(draft), repo=self.repo)

    def test_from_operator_on_a_t2_only_provider_is_a_provider_draft(self) -> None:
        self.declare("acme")
        code, out = self.run_dev(["model", "add", "--from-operator", "custom-acme-large", "--id", "acme-large",
                                  "--name", "o3"])
        self.assertEqual(code, 0, out)
        self.assertIn("kind: provider draft — acme is operator-declared (T2-only)", out)
        draft = self.draft("o3")
        self.assertEqual(draft["kind"], "provider")
        provider = draft["entry"]["provider"]
        self.assertNotIn("origin", provider)
        self.assertEqual(provider["support"], "locally-validated-experimental")
        self.assertIn("QUALIFY", provider["support_note"])
        self.assertEqual(draft["entry"]["contract_claims"], ["output-config-high"])
        self.assertEqual(draft["entry"]["model"]["efforts"], {
            "high": {"selector": "claude-multi-acme-large-high[1m]", "proxy_contract": "output-config-high"}})
        with self.assertRaisesRegex(DevError, "QUALIFY marker in 'support_note'"):
            dev.check_draft(draft, repo=self.repo)
        code, out = self.run_dev(["model", "add", "--from-operator", "custom-missing", "--id", "x1"])
        self.assertEqual(code, 2)
        self.assertIn("no valid operator declaration", out)

    def test_efforts_and_default_effort_are_qualify_checked(self) -> None:
        for field, value in (("efforts", "QUALIFY: efforts"), ("default_effort", "QUALIFY: default")):
            with self.subTest(field=field):
                draft = _model_draft()
                draft["entry"][field] = value
                with self.assertRaisesRegex(DevError, f"QUALIFY marker in '{field}'"):
                    dev.check_draft(draft, repo=self.repo)


class DraftTests(OnboardingTestCase):
    def test_draft_store_roundtrip_mode_0600(self) -> None:
        draft = _model_draft()
        path = self.drafts.save("d1", draft)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertEqual(self.drafts.load("d1"), draft)

    def test_corrupt_draft_rejected(self) -> None:
        path = self.drafts.save("d1", _model_draft())
        state.atomic_write(path, b"{oops")
        with self.assertRaisesRegex(DevError, "corrupt"):
            self.drafts.load("d1")

    def test_model_draft_schema_valid(self) -> None:
        from claude_multi import validate

        draft = _model_draft()
        schema = strict_json.load(CATALOG_ROOT / "schemas" / "draft.schema.json")
        self.assertEqual(validate.validate(draft, schema), [])

    def test_provider_draft_requires_model_and_claims(self) -> None:
        with self.assertRaisesRegex(DevError, "complete model"):
            dev.make_provider_draft(
                name="d2", provider_profile={}, model_entry={},
                contract_claims=["routing"],
            )
        with self.assertRaisesRegex(DevError, "contract claims"):
            dev.make_provider_draft(
                name="d2", provider_profile={}, model_entry={"id": "x"},
                contract_claims=[],
            )

    def test_model_draft_inherits_provider_transport(self) -> None:
        # Model entry carries no transport/auth fields; inheritance is by reference.
        draft = _model_draft()
        self.assertNotIn("transport", draft["entry"])
        self.assertNotIn("auth", draft["entry"])
        self.assertEqual(draft["provider"], "openai")


class PostImageTests(OnboardingTestCase):
    def test_model_post_image_adds_entry_new_off(self) -> None:
        raw = catalog.load_raw(self.resources)
        images = dev.build_post_images(raw["docs"], _model_draft())
        self.assertEqual(
            list(images), [MODELS_IMAGE]
        )
        post = strict_json.loads(images[MODELS_IMAGE])
        self.assertIn("newmodel", post["models"])
        self.assertNotIn("id", post["models"]["newmodel"])
        # The catalog has no 2.x default composition (the New · Off
        # composition half of this test went with dev.py's composition branch).
        self.assertNotIn("compositions/default", raw["docs"])

    def test_duplicate_model_rejected(self) -> None:
        raw = catalog.load_raw(self.resources)
        with self.assertRaisesRegex(DevError, "already exists"):
            dev.build_post_images(raw["docs"], _model_draft("sol"))

    def test_provider_post_images_both_files(self) -> None:
        raw = catalog.load_raw(self.resources)
        images = dev.build_post_images(raw["docs"], _provider_draft())
        self.assertEqual(len(images), 2)
        providers_post = strict_json.loads(
            images[PROVIDERS_IMAGE]
        )
        self.assertIn("zeta", providers_post["providers"])

    def test_provider_post_images_strip_draft_ids(self) -> None:
        raw = catalog.load_raw(self.resources)
        images = dev.build_post_images(raw["docs"], _provider_draft())
        providers_post = strict_json.loads(
            images[PROVIDERS_IMAGE]
        )
        models_post = strict_json.loads(
            images[MODELS_IMAGE]
        )
        self.assertNotIn("id", providers_post["providers"]["zeta"])
        self.assertNotIn("id", models_post["models"]["zetamodel"])

    def test_unknown_candidate_key_rejected_before_review(self) -> None:
        draft = _model_draft()
        draft["entry"]["mystery_field"] = "x"
        with self.assertRaisesRegex(DevError, "trusted schema"):
            dev.check_draft(
                draft, repo=self.repo,
                runner=lambda c, w: {"cmd": c, "returncode": 0},
                candidate_parent=self.root / "cand-x",
            )
        with self.assertRaisesRegex(DevError, "trusted schema"):
            self._review(draft)


class CheckTests(OnboardingTestCase):
    def test_check_runs_exact_offline_commands_and_gates(self) -> None:
        commands: list[list[str]] = []

        def fake_runner(command, cwd):
            commands.append(command)
            return {"cmd": command, "returncode": 0, "stdout": "ok", "stderr": ""}

        parent = self.root / "candidate"
        parent.mkdir()
        result = dev.check_draft(
            _model_draft(), repo=self.repo, runner=fake_runner,
            candidate_parent=parent,
        )
        self.assertTrue(result.bundle_valid)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][:2], ["nix-build", "--no-out-link"])
        self.assertTrue(commands[0][2].endswith("/tree/tests/default.nix"))
        self.assertTrue(commands[1][2].endswith("/tree/nix/package.nix"))
        self.assertIn(str(parent), commands[0][2])
        # candidate tree was materialized, used, and deterministically removed
        self.assertFalse(result.candidate.exists())
        self.assertFalse(parent.exists())
        # the post-image evidence survives in memory
        applied = strict_json.loads(
            result.images[MODELS_IMAGE]
        )
        self.assertIn("newmodel", applied["models"])
        # render used dummy secret, never a real one
        self.assertIn("dummy-onboarding-secret", result.render.yaml)
        # The check renders the v2 lines, so the promoted New · Off
        # line's own selector is exercised; continuity is always empty (the
        # dummy render never reads the operator's continuity.json).
        self.assertIn('alias: "gpt-multi-newmodel-high"', result.render.yaml)
        self.assertEqual(result.render.continuity_rendered, ())

    def test_candidate_cleanup_on_success_and_failure(self) -> None:
        # success path
        ok_parent = self.root / "cand-ok"
        ok_parent.mkdir()
        marker = self.root / "marker.txt"
        marker.write_text("do not touch")
        dev.check_draft(
            _model_draft(), repo=self.repo,
            runner=lambda c, w: {"cmd": c, "returncode": 0},
            candidate_parent=ok_parent,
        )
        self.assertFalse(ok_parent.exists())
        self.assertTrue(marker.is_file())
        # failure path (build gate raises)
        fail_parent = self.root / "cand-fail"
        fail_parent.mkdir()
        with self.assertRaisesRegex(DevError, "candidate build failed"):
            dev.check_draft(
                _model_draft(), repo=self.repo,
                runner=lambda c, w: {"cmd": c, "returncode": 1, "stderr": "boom"},
                candidate_parent=fail_parent,
            )
        self.assertFalse(fail_parent.exists())
        self.assertTrue(marker.is_file())

    def test_candidate_cleanup_on_exception_path(self) -> None:
        parent = self.root / "cand-exc"
        parent.mkdir()

        def exploding_runner(_command, _cwd):
            raise RuntimeError("runner exploded")

        with self.assertRaises(RuntimeError):
            dev.check_draft(
                _model_draft(), repo=self.repo,
                runner=exploding_runner, candidate_parent=parent,
            )
        self.assertFalse(parent.exists())

    def test_no_candidate_accumulation_under_state_root(self) -> None:
        dev.check_draft(
            _model_draft(), repo=self.repo,
            runner=lambda c, w: {"cmd": c, "returncode": 0},
        )
        leftover = list(
            Path(dev.state_root_default()).glob("claude-multi-candidate-*")
        )
        self.assertEqual(leftover, [])

    def test_candidate_copy_exclusions(self) -> None:
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "HEAD").write_text("ref")
        (self.repo / ".slim").mkdir()
        (self.repo / "result").write_text("link")
        (self.repo / "symlink-file").symlink_to(self.repo / "flake.nix")
        (self.repo / ".claude" / "worktrees" / "other").mkdir(parents=True)
        (self.repo / ".claude" / "worktrees" / "other" / "flake.nix").write_text("{}\n")
        observed = {}

        def inspecting_runner(command, cwd):
            observed["cwd"] = Path(cwd)
            return {"cmd": command, "returncode": 0}

        parent = self.root / "cand2"
        parent.mkdir()
        dev.check_draft(
            _model_draft(), repo=self.repo,
            runner=inspecting_runner,
            candidate_parent=parent,
        )
        candidate = observed["cwd"]
        self.assertTrue(str(candidate).startswith(str(parent)))
        self.assertFalse((candidate / ".git").exists())
        self.assertFalse((candidate / ".slim").exists())
        self.assertFalse((candidate / "result").exists())
        self.assertFalse((candidate / "symlink-file").exists())
        self.assertFalse((candidate / ".claude").exists())
        # cleanup removed the transient tree after evidence capture
        self.assertFalse(parent.exists())

    def test_check_rejects_invalid_candidate(self) -> None:
        draft = _model_draft()
        draft["entry"]["roles"] = ["cm-nonexistent"]
        with self.assertRaisesRegex(DevError, "candidate bundle invalid"):
            dev.check_draft(
                draft, repo=self.repo,
                runner=lambda c, w: {"cmd": c, "returncode": 0},
                candidate_parent=self.root / "cand3",
            )

    def test_provider_draft_check_and_review_succeed(self) -> None:
        result = dev.check_draft(
            _provider_draft(), repo=self.repo,
            runner=lambda c, w: {"cmd": c, "returncode": 0},
            candidate_parent=self.root / "cand-p",
        )
        self.assertTrue(result.bundle_valid)
        record = self._review(_provider_draft(), name="d2")
        self.assertEqual(record["draft"], "d2")
        self.assertEqual(len(record["files"]), 2)

    def test_build_failure_blocks_check_and_review(self) -> None:
        targets = ("tests/default.nix", "nix/package.nix")
        for failing_index in (0, 1):
            with self.subTest(failing=failing_index):
                def runner(command, _cwd):
                    if command[-1].endswith(targets[failing_index]):
                        return {
                            "cmd": command,
                            "returncode": 1,
                            "stdout": "",
                            "stderr": "eval error: broken thing",
                        }
                    return {"cmd": command, "returncode": 0}

                with self.assertRaisesRegex(DevError, "candidate build failed"):
                    dev.check_draft(
                        _model_draft(), repo=self.repo, runner=runner,
                        candidate_parent=self.root / f"cand-f{failing_index}",
                    )
                with self.assertRaisesRegex(DevError, "candidate build failed"):
                    dev.review_draft(
                        _model_draft(), draft_name="d1", repo=self.repo,
                        runner=runner,
                        candidate_parent=Path(tempfile.mkdtemp(dir=self.root)),
                    )

    def test_build_failure_output_sanitized(self) -> None:
        secret_value = "super-secret-value-12345"
        os.environ["KIMI_CLAUDE_API_KEY"] = secret_value
        try:
            def runner(_command, _cwd):
                return {
                    "cmd": _command,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": f"leaked {secret_value} here",
                }

            with self.assertRaises(DevError) as raised:
                dev.check_draft(
                    _model_draft(), repo=self.repo, runner=runner,
                    candidate_parent=self.root / "cand-s",
                )
            self.assertNotIn(secret_value, str(raised.exception))
            self.assertIn("***", str(raised.exception))
            self.assertIn("tests/default.nix", str(raised.exception))
        finally:
            del os.environ["KIMI_CLAUDE_API_KEY"]

    def test_binary_unavailable_skip_is_nonfatal_but_reported(self) -> None:
        def runner(command, _cwd):
            return {"cmd": command, "skipped": "nix-build not available"}

        result = dev.check_draft(
            _model_draft(), repo=self.repo, runner=runner,
            candidate_parent=self.root / "cand-k",
        )
        self.assertTrue(result.bundle_valid)
        self.assertTrue(all("skipped" in build for build in result.builds))


class ReviewTests(OnboardingTestCase):
    def test_review_record_exact_and_schema_valid(self) -> None:
        from claude_multi import validate

        record = self._review(_model_draft())
        schema = strict_json.load(CATALOG_ROOT / "schemas" / "review.schema.json")
        self.assertEqual(validate.validate(record, schema), [])
        self.assertEqual(record["repo"]["revision"], "deadbeef")
        self.assertEqual(record["draft_hash"], dev.draft_hash(_model_draft()))
        entry = record["files"][0]
        self.assertEqual(entry["path"], MODELS_IMAGE)
        current = (self.repo / entry["path"]).read_bytes()
        self.assertEqual(
            entry["pre_image_hash"],
            "sha256:" + strict_json.sha256_hex(current),
        )
        images = dev.build_post_images(
            catalog.load_raw(self.resources)["docs"], _model_draft()
        )
        self.assertEqual(
            entry["post_image_hash"],
            "sha256:" + strict_json.sha256_hex(images[entry["path"]]),
        )
        self.assertIn('"newmodel"', record["results"]["diff"])
        self.assertIn("\n+", record["results"]["diff"])

    def test_review_uses_dummy_secrets_only(self) -> None:
        record = self._review(_provider_draft(), name="d2")
        blob = strict_json.canonical_bytes(record).decode("utf-8")
        self.assertNotIn("ZETA_API_KEY-value", blob)
        self.assertNotIn("sk-", blob)


class PromoteTests(OnboardingTestCase):
    def test_promote_applies_reviewed_post_image(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        result = dev.promote_draft(
            draft, draft_name="d1", repo=self.repo, review_record=record,
            drafts_root=self.root / "drafts",
        )
        self.assertEqual(
            result["applied"], [MODELS_IMAGE]
        )
        post = strict_json.load(
            self.resources / "catalog/models.json"
        )
        self.assertIn("newmodel", post["models"])
        self.assertTrue((self.root / "drafts" / "d1.journal.json").exists())

    def test_promoted_catalog_round_trips_through_load_catalog(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        dev.promote_draft(
            draft, draft_name="d1", repo=self.repo, review_record=record,
            drafts_root=self.root / "drafts",
        )
        bundle = catalog.load_catalog(self.resources)
        # A promoted line lands New · Off: in the v2 lines, never
        # offered until a reviewed catalog edit activates it.
        # "Never offered" is the merged-view seam (scope.line_view
        # under the default Settings), not the deleted v1 view.
        from claude_multi import profile, scope, settings

        self.assertIn("newmodel", bundle.lines)
        self.assertEqual(bundle.lines["newmodel"]["status"], "new")
        lcat = profile.LineupCatalog.from_docs(bundle.docs)
        eff = settings.effective(
            {"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines
        )
        self.assertNotIn("newmodel", {line.key for line in scope.line_view(lcat, eff).lines})
        provider_draft = _provider_draft()
        record2 = self._review(provider_draft, name="d2")
        dev.promote_draft(
            provider_draft, draft_name="d2", repo=self.repo,
            review_record=record2, drafts_root=self.root / "drafts",
        )
        bundle = catalog.load_catalog(self.resources)
        self.assertIn("zeta", bundle.providers)
        self.assertIn("zetamodel", bundle.lines)

    def test_promote_rejects_source_drift_after_review(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        target = self.resources / "catalog/models.json"
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaisesRegex(DevError, "pre-image hash mismatch"):
            dev.promote_draft(
                draft, draft_name="d1", repo=self.repo, review_record=record
            )
        self.assertNotIn(
            "newmodel",
            strict_json.load(
                self.resources / "catalog/models.json"
            )["models"],
        )

    def test_promote_rejects_tampered_review_hash(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        record["files"][0]["post_image_hash"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(DevError, "post-image hash mismatch"):
            dev.promote_draft(
                draft, draft_name="d1", repo=self.repo, review_record=record
            )

    def test_promote_rejects_draft_mismatch(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        with self.assertRaisesRegex(DevError, "does not match"):
            dev.promote_draft(
                _model_draft("other"), draft_name="d1", repo=self.repo,
                review_record=record,
            )

    def test_promote_journal_rollback_on_failure(self) -> None:
        draft = _provider_draft()
        record = self._review(draft, name="d2")
        original_providers = (
            self.resources / "catalog/providers.json"
        ).read_bytes()
        calls = {"count": 0}
        real_write = dev._repo_atomic_write

        def flaky_write(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("injected mid-promote failure")
            return real_write(path, data)

        with mock.patch.object(dev, "_repo_atomic_write", side_effect=flaky_write):
            with self.assertRaisesRegex(DevError, "rolled back"):
                dev.promote_draft(
                    draft, draft_name="d2", repo=self.repo, review_record=record
                )
        # first file restored to its exact pre-image
        self.assertEqual(
            (self.resources / "catalog/providers.json").read_bytes(),
            original_providers,
        )
        models_doc = strict_json.load(
            self.resources / "catalog/models.json"
        )
        self.assertNotIn("zetamodel", models_doc["models"])

    def test_patch_output_emit_only(self) -> None:
        draft = _model_draft()
        before = (
            self.resources / "catalog/models.json"
        ).read_bytes()
        output = self.root / "patch.diff"
        dev.promote_patch_output(draft, repo=self.repo, output=output)
        self.assertEqual(
            (self.resources / "catalog/models.json").read_bytes(),
            before,
        )
        content = output.read_text()
        self.assertIn("newmodel", content)
        self.assertIn("---", content)
        self.assertEqual(stat.S_IMODE(os.lstat(output).st_mode), 0o600)
        self.assertNotIn(
            "newmodel",
            strict_json.load(
                self.resources / "catalog/models.json"
            )["models"],
        )

    def test_patch_output_overwrites_regular_target_atomically(self) -> None:
        output = self.root / "patch.diff"
        state.atomic_write(output, b"old content")
        dev.promote_patch_output(_model_draft(), repo=self.repo, output=output)
        self.assertIn("newmodel", output.read_text())
        self.assertEqual(stat.S_IMODE(os.lstat(output).st_mode), 0o600)

    def test_patch_output_rejects_symlink_target(self) -> None:
        real = self.root / "real.diff"
        state.atomic_write(real, b"x")
        link = self.root / "link.diff"
        link.symlink_to(real)
        with self.assertRaisesRegex(DevError, "symlink"):
            dev.promote_patch_output(_model_draft(), repo=self.repo, output=link)
        self.assertEqual(real.read_bytes(), b"x")

    def test_patch_output_rejects_non_private_parent(self) -> None:
        public = self.root / "public"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        with self.assertRaisesRegex(Exception, "state directory"):
            dev.promote_patch_output(
                _model_draft(), repo=self.repo, output=public / "p.diff"
            )

    def test_promote_modes_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(DevError, "never both"):
            dev.resolve_promote_mode(Path("/repo"), "out.diff")
        with self.assertRaisesRegex(DevError, "requires"):
            dev.resolve_promote_mode(None, None)
        self.assertEqual(dev.resolve_promote_mode(Path("/repo"), None), "apply")
        self.assertEqual(dev.resolve_promote_mode(None, "out.diff"), "patch")

    def test_promote_cli_mutual_exclusion(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            code = dev.main(
                ["promote", "d1", "--repo", str(self.repo), "--patch-output", str(self.root / "x.diff")]
            )
        self.assertEqual(code, 2)

    def test_repo_verification(self) -> None:
        with self.assertRaisesRegex(DevError, "Nix store"):
            dev.verify_repo("/nix/store/abcdef")
        with self.assertRaisesRegex(DevError, "missing"):
            dev.verify_repo(self.root)
        link = self.root / "repolink"
        link.symlink_to(self.repo)
        with self.assertRaisesRegex(DevError, "symlink"):
            dev.verify_repo(link)

    def test_a_checkout_keeps_its_resources_in_the_package(self) -> None:
        # Resources at the repository root do not make a checkout.
        flat = fake_checkout(self.root / "flat", CATALOG_ROOT, ())
        shutil.copytree(CATALOG_ROOT / "catalog", flat / "catalog")
        with self.assertRaisesRegex(DevError, "lacks the catalog tree"):
            dev.verify_repo(flat)
        self.assertEqual(dev.verify_repo(self.repo), self.repo.resolve())

    def test_symlinked_resource_directories_are_refused(self) -> None:
        for relative in (dev.RESOURCES, dev.RESOURCES / "catalog", Path("src")):
            with self.subTest(relative=str(relative)):
                repo = fake_checkout(self.root / f"repo-{len(relative.parts)}", CATALOG_ROOT)
                outside = self.root / f"outside-{len(relative.parts)}"
                shutil.move(str(repo / relative), str(outside))
                (repo / relative).symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(DevError, "not a real directory inside the checkout"):
                    dev.verify_repo(repo)

    def test_promote_never_writes_through_a_symlinked_resource_directory(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        outside = self.root / "outside-catalog"
        catalog_dir = self.resources / "catalog"
        shutil.move(str(catalog_dir), str(outside))
        catalog_dir.symlink_to(outside, target_is_directory=True)
        before = (outside / "models.json").read_bytes()
        with self.assertRaisesRegex(DevError, "not a real directory inside the checkout"):
            dev.promote_draft(draft, draft_name="d1", repo=self.repo, review_record=record,
                              drafts_root=self.root / "drafts")
        self.assertEqual((outside / "models.json").read_bytes(), before)
        self.assertFalse((self.root / "drafts" / "d1.journal.json").exists())
        # The destination check alone refuses the same path.
        with self.assertRaisesRegex(DevError, "not a real directory inside the checkout"):
            dev._checkout_target(self.repo, MODELS_IMAGE)

    def test_a_review_of_the_former_layout_needs_a_fresh_review(self) -> None:
        # Reviewed paths are checkout-relative; a record naming a former
        # location never promotes (re-review instead of rewriting approvals).
        draft = _model_draft()
        record = copy.deepcopy(self._review(draft))
        self.assertEqual([entry["path"] for entry in record["files"]], [MODELS_IMAGE])
        for entry in record["files"]:
            entry["path"] = "catalog/models.json"
        before = (self.resources / "catalog/models.json").read_bytes()
        with self.assertRaisesRegex(DevError, "reviewed file set differs"):
            dev.promote_draft(draft, draft_name="d1", repo=self.repo, review_record=record,
                              drafts_root=self.root / "drafts")
        self.assertEqual((self.resources / "catalog/models.json").read_bytes(), before)


class SmokeTests(OnboardingTestCase):
    def test_smoke_without_consent_zero_requests(self) -> None:
        outcome = dev.smoke_test(
            "gpt-multi-sol-high",
            allow_provider_call=False,
        )
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["requests"], 0)
        self.assertIn("--allow-provider-call", outcome["guidance"])

    def test_smoke_with_consent_fails_closed_without_transport(self) -> None:
        with self.assertRaisesRegex(DevError, "no provider transport"):
            dev.smoke_test(
                "gpt-multi-sol-high",
                allow_provider_call=True,
            )


class ImportHygieneTests(unittest.TestCase):
    def test_runtime_modules_never_import_dev(self) -> None:
        import ast

        src = REPO_ROOT / "src" / "claude_multi"
        offenders: list[str] = []
        modules = sorted(src.rglob("*.py"))
        self.assertTrue(any("cli" in p.relative_to(src).parts[:-1] for p in modules))
        for module in modules:
            module_name = "claude_multi." + ".".join(module.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
            relative = module.relative_to(src).as_posix()
            if relative == "dev.py":
                continue
            tree = ast.parse(module.read_text())
            # The entry points load dev.py inside the claude-multi-dev
            # command only, so their module-level imports are what counts.
            nodes = tree.body if relative == "entrypoints.py" else ast.walk(tree)
            for node in nodes:
                if isinstance(node, ast.ImportFrom) and node.module and "dev" in node.module.split("."):
                    offenders.append(f"{module_name}: from {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "dev" in alias.name.split("."):
                            offenders.append(f"{module_name}: import {alias.name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()


class DevCLIErrorTests(OnboardingTestCase):
    def test_model_add_outside_checkout_exits_2_clean(self) -> None:
        spec = self.root / "spec.json"
        spec.write_text('{"provider": "openai", "entry": {}}')
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            with mock.patch("sys.argv", ["claude-multi-dev"]):
                code = dev.main(
                    ["model", "add", "--from-json", str(spec), "--repo", str(self.root / "nowhere")]
                )
        self.assertEqual(code, 2)

    def test_malformed_draft_exits_2_without_traceback(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("{not json")
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            with mock.patch("sys.stderr", stderr):
                code = dev.main(
                    ["model", "add", "--from-json", str(bad), "--repo", str(self.repo)]
                )
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_missing_spec_field_exits_2(self) -> None:
        spec = self.root / "spec.json"
        spec.write_text('{"entry": {}}')
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            code = dev.main(
                ["model", "add", "--from-json", str(spec), "--repo", str(self.repo)]
            )
        self.assertEqual(code, 2)

    def test_unsafe_patch_output_parent_exits_2(self) -> None:
        self.drafts.save("d1", _model_draft())
        public = self.root / "public"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            code = dev.main(
                [
                    "promote", "d1", "--patch-output", str(public / "x.diff"),
                ]
            )
        self.assertEqual(code, 2)


class RedactionTests(OnboardingTestCase):
    def test_short_values_redacted(self) -> None:
        with mock.patch.dict(os.environ, {"KIMI_CLAUDE_API_KEY": "abc12"}):
            self.assertEqual(dev._sanitize_output("token abc12 tail"), "token *** tail")

    def test_overlapping_values_fully_redacted(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"A_KEY": "secretkey123", "B_TOKEN": "key123"},
        ):
            text = dev._sanitize_output("leak secretkey123 and key123 done")
        self.assertNotIn("secretkey123", text)
        self.assertNotIn("key123", text)
        self.assertIn("leak", text)
        self.assertIn("done", text)

    def test_nonsecret_diagnostics_retained(self) -> None:
        self.assertEqual(
            dev._sanitize_output("eval error: broken thing"),
            "eval error: broken thing",
        )

    def test_empty_values_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"EMPTY_KEY": ""}):
            self.assertEqual(dev._sanitize_output("plain"), "plain")


class ReviewRecordValidationTests(OnboardingTestCase):
    def _valid_record(self):
        return self._review(_model_draft())

    def test_missing_fields_rejected_deterministically(self) -> None:
        record = self._valid_record()
        del record["files"]
        with self.assertRaisesRegex(DevError, "review record invalid.*files"):
            dev.promote_draft(
                _model_draft(), draft_name="d1", repo=self.repo,
                review_record=record,
            )

    def test_unknown_field_rejected(self) -> None:
        record = self._valid_record()
        record["surprise"] = True
        with self.assertRaisesRegex(DevError, "review record invalid"):
            dev.promote_draft(
                _model_draft(), draft_name="d1", repo=self.repo,
                review_record=record,
            )

    def test_wrong_types_rejected(self) -> None:
        record = self._valid_record()
        record["draft_hash"] = 12345
        with self.assertRaisesRegex(DevError, "review record invalid"):
            dev.promote_draft(
                _model_draft(), draft_name="d1", repo=self.repo,
                review_record=record,
            )

    def test_non_object_record_rejected(self) -> None:
        with self.assertRaisesRegex(DevError, "not a JSON object"):
            dev.promote_draft(
                _model_draft(), draft_name="d1", repo=self.repo,
                review_record=["not", "a", "dict"],
            )

    def test_valid_record_happy_path(self) -> None:
        draft = _model_draft()
        result = dev.promote_draft(
            draft, draft_name="d1", repo=self.repo,
            review_record=self._review(draft),
        )
        self.assertEqual(
            result["applied"], [MODELS_IMAGE]
        )

    def test_cli_corrupt_review_json_exits_2_clean(self) -> None:
        self.drafts.save("d1", _model_draft())
        state.atomic_write(self.root / "drafts" / "d1.review.json", b"{oops")
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            with mock.patch("sys.stderr", stderr):
                code = dev.main(["promote", "d1", "--repo", str(self.repo)])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_unknown_field_review_exits_2(self) -> None:
        draft = _model_draft()
        self.drafts.save("d1", draft)
        record = self._review(draft)
        record["surprise"] = True
        state.atomic_write(
            self.root / "drafts" / "d1.review.json",
            strict_json.canonical_file_bytes(record),
        )
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            with mock.patch("sys.stderr", stderr):
                code = dev.main(["promote", "d1", "--repo", str(self.repo)])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr.getvalue())


class CandidateFailureCleanupTests(OnboardingTestCase):
    def test_copy_failure_removes_partial_candidate(self) -> None:
        parent = self.root / "claude-multi-candidate-x"
        parent.mkdir()
        sentinel_dir = self.root / "outside"
        sentinel_dir.mkdir()
        (sentinel_dir / "keep.txt").write_text("untouched")
        with mock.patch.object(
            dev.shutil, "copytree", side_effect=OSError("injected copy failure")
        ):
            with self.assertRaises(OSError):
                dev.materialize_candidate(self.repo, {}, parent)
        self.assertFalse(parent.exists())
        self.assertEqual(
            list(self.root.glob("claude-multi-candidate-*")), []
        )
        self.assertEqual((sentinel_dir / "keep.txt").read_text(), "untouched")

    def test_image_apply_failure_removes_candidate(self) -> None:
        parent = self.root / "claude-multi-candidate-y"
        parent.mkdir()
        images = {MODELS_IMAGE: b"{}\n"}
        with mock.patch.object(
            dev.state, "atomic_write", side_effect=OSError("injected write failure")
        ):
            with self.assertRaises(OSError):
                dev.materialize_candidate(self.repo, images, parent)
        self.assertFalse(parent.exists())
        self.assertEqual(
            list(self.root.glob("claude-multi-candidate-*")), []
        )

    def test_default_location_no_leftovers(self) -> None:
        with mock.patch.object(
            dev.shutil, "copytree", side_effect=OSError("injected copy failure")
        ):
            with self.assertRaises(OSError):
                dev.materialize_candidate(self.repo, {}, None)
        leftover = list(
            Path(dev.state_root_default()).glob("claude-multi-candidate-*")
        )
        self.assertEqual(leftover, [])


class NewEntryPolicyTests(OnboardingTestCase):
    """New · Off applies to provider-kind drafts too (not just model-kind)."""

    # Seed profiles never bind a New line or provider.
    def _candidate_docs(self, draft: dict) -> dict:
        docs = copy.deepcopy(catalog.load_raw(CATALOG_ROOT)["docs"])
        if draft["kind"] == "model":
            entry = copy.deepcopy(draft["entry"])
        else:
            entry = copy.deepcopy(draft["entry"]["model"])
            provider = copy.deepcopy(draft["entry"]["provider"])
            docs["providers"]["providers"][provider.pop("id")] = provider
        docs["models"]["models"][entry.pop("id")] = entry
        return docs

    def test_seed_policy_passes_when_nothing_is_bound(self) -> None:
        for draft in (_model_draft(), _provider_draft()):
            with self.subTest(kind=draft["kind"]):
                dev._check_new_entry_policy(self._candidate_docs(draft), draft)

    def test_model_bound_in_seed_is_new_off(self) -> None:
        draft = _model_draft()
        docs = self._candidate_docs(draft)
        docs["profiles/balanced"]["agents"]["cm-reviewer"] = {"effort": "high", "model": "newmodel"}
        with self.assertRaisesRegex(
            DevError, r"new model 'newmodel' must not be bound in seed profile 'balanced'; it is New · Off"
        ):
            dev._check_new_entry_policy(docs, draft)

    def test_provider_bound_in_seed_is_new_off(self) -> None:
        draft = _provider_draft()
        docs = self._candidate_docs(draft)
        docs["profiles/openai"]["primary_provider"] = "zeta"
        with self.assertRaisesRegex(
            DevError, r"new provider 'zeta' must not be bound in seed profile 'openai'; it is New · Off"
        ):
            dev._check_new_entry_policy(docs, draft)
        docs = self._candidate_docs(draft)
        docs["profiles/direct"]["lead_providers"] = ["anthropic", "zeta"]
        with self.assertRaisesRegex(DevError, "New · Off"):
            dev._check_new_entry_policy(docs, draft)


# --------------------------------------------------------------- promote


def _check(case: "OnboardingTestCase", draft: dict, tag: str):
    return dev.check_draft(
        draft, repo=case.repo,
        runner=lambda c, w: {"cmd": c, "returncode": 0},
        candidate_parent=case.root / f"cand-{tag}",
    )


class PromoteStatusTests(OnboardingTestCase):
    """A promoted line always lands New · Off (status "new")."""

    def _raw_docs(self):
        return catalog.load_raw(self.resources)["docs"]

    def test_missing_status_defaults_to_new(self) -> None:
        draft = _model_draft()
        del draft["entry"]["status"]
        images = dev.build_post_images(self._raw_docs(), draft)
        post = strict_json.loads(images[MODELS_IMAGE])
        self.assertEqual(post["models"]["newmodel"]["status"], "new")
        # The draft itself is never mutated (its hash is the review contract).
        self.assertNotIn("status", draft["entry"])

    def test_active_status_refused_for_model_and_provider_drafts(self) -> None:
        draft = _model_draft()
        draft["entry"]["status"] = "active"
        with self.assertRaisesRegex(DevError, 'promoted lines start New · Off \\(status "new"\\)'):
            dev.build_post_images(self._raw_docs(), draft)
        provider = _provider_draft()
        provider["entry"]["model"]["status"] = "active"
        with self.assertRaisesRegex(DevError, "New · Off .*local admission is a separate operator action"):
            dev.build_post_images(self._raw_docs(), provider)
        with self.assertRaisesRegex(DevError, "New · Off"):
            _check(self, draft, "active")

    def test_at_retired_and_live_keys_refused(self) -> None:
        docs = self._raw_docs()
        for key, message in (
            ("newmodel@1", "'@' keys are retired generation entries"),
            ("muse-spark", "retired catalog key"),
            ("sol", "already exists"),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(DevError, message):
                    dev.build_post_images(docs, _model_draft(key))
        provider = _provider_draft()
        provider["entry"]["model"]["id"] = "muse-spark"
        with self.assertRaisesRegex(DevError, "retired catalog key"):
            dev.build_post_images(docs, provider)

    def test_entry_policy_requires_status_new(self) -> None:
        # _check_new_entry_policy re-checks the candidate line itself.
        docs = copy.deepcopy(self._raw_docs())
        entry = {k: v for k, v in _model_entry().items() if k != "id"}
        entry["status"] = "active"
        docs["models"]["models"]["newmodel"] = entry
        with self.assertRaisesRegex(DevError, "New · Off"):
            dev._check_new_entry_policy(docs, _model_draft())

    def test_promote_writes_status_new(self) -> None:
        draft = _model_draft()
        del draft["entry"]["status"]
        record = self._review(draft)
        dev.promote_draft(draft, draft_name="d1", repo=self.repo, review_record=record)
        post = strict_json.load(self.repo / dev.RESOURCES / "catalog" / "models.json")
        self.assertEqual(post["models"]["newmodel"]["status"], "new")


class PromoteSelectorShapeTests(OnboardingTestCase):
    """The catalog-33 selector shape is a promote gate."""

    def _policy_docs(self, key: str, entry: dict, providers: dict | None = None) -> dict:
        docs = copy.deepcopy(catalog.load_raw(self.resources)["docs"])
        docs["models"]["models"][key] = {k: v for k, v in entry.items() if k != "id"}
        if providers:
            docs["providers"]["providers"].update(providers)
        return docs

    def _refused(self, key: str, entry: dict, pattern: str) -> None:
        draft = dev.make_model_draft(name="d1", provider=entry["provider"], entry=entry)
        with self.assertRaisesRegex(DevError, pattern):
            dev._check_new_entry_policy(self._policy_docs(key, entry), draft)

    def test_compliant_entries_pass(self) -> None:
        for draft in (_model_draft(), _provider_draft()):
            with self.subTest(kind=draft["kind"]):
                self.assertTrue(_check(self, draft, draft["kind"]).bundle_valid)

    def test_gateway_selector_must_be_key_plus_effort(self) -> None:
        cases = {
            # the generation in the selector (never the generation)
            "gpt-multi-newmodel-1-high": "key \\+ effort, never the generation",
            # the claude prefix on the codex pool
            "claude-multi-newmodel-high": "must be 'gpt-multi-newmodel-high'",
            # [1m] on a line below 1M client tokens
            "gpt-multi-newmodel-high[1m]": "must be 'gpt-multi-newmodel-high'",
            # another key
            "gpt-multi-other-high": "must be 'gpt-multi-newmodel-high'",
        }
        for selector, pattern in cases.items():
            with self.subTest(selector=selector):
                entry = _model_entry()
                entry["efforts"]["high"]["selector"] = selector
                self._refused("newmodel", entry, pattern)
        # The same refusal through the real check pipeline (a --from-json
        # draft skips the scaffold that would derive the right shape).
        draft = _model_draft()
        draft["entry"]["efforts"]["high"]["selector"] = "gpt-multi-newmodel-1-high"
        with self.assertRaisesRegex(DevError, "never the generation"):
            _check(self, draft, "gen")

    def test_gateway_selector_needs_1m_suffix_at_1m(self) -> None:
        draft = _provider_draft()
        entry = draft["entry"]["model"]
        entry["efforts"]["max"]["selector"] = "claude-multi-zetamodel-max"
        # validate_catalog already refuses the [1m]/client_tokens mismatch in
        # the pipeline; the promote shape gate refuses it on its own too.
        with self.assertRaisesRegex(DevError, "selector classification"):
            _check(self, draft, "1m")
        docs = self._policy_docs(
            "zetamodel", entry,
            providers={"zeta": {k: v for k, v in draft["entry"]["provider"].items() if k != "id"}},
        )
        with self.assertRaisesRegex(DevError, "must be 'claude-multi-zetamodel-max\\[1m\\]'"):
            dev._check_new_entry_policy(docs, draft)

    def test_contract_level_must_equal_effort(self) -> None:
        entry = _model_entry()
        entry["efforts"]["high"]["proxy_contract"] = "reasoning-effort-xhigh"
        self._refused("newmodel", entry, "proxy_contract 'reasoning-effort-xhigh' must carry the same level")
        draft = _model_draft()
        draft["entry"]["efforts"]["high"]["proxy_contract"] = "reasoning-effort-xhigh"
        with self.assertRaisesRegex(DevError, "must carry the same level"):
            _check(self, draft, "level")

    def test_anthropic_selector_must_be_canonical_wire(self) -> None:
        # A --from-json Anthropic line on an existing route (the fixture pool
        # routes claude-fable-5-1, which no fixture line uses).
        docs = catalog.load_raw(self.resources)["docs"]
        entry = copy.deepcopy(docs["models"]["models"]["fable"])
        entry.update(
            id="fable-next", wire_model="claude-fable-5-1",
            selector="claude-multi-fable-next[1m]", status="new",
        )
        draft = dev.make_model_draft(name="d1", provider="anthropic", entry=entry)
        with self.assertRaisesRegex(DevError, "canonical wire id 'claude-fable-5-1\\[1m\\]', not"):
            _check(self, draft, "anthropic")
        for selector in ("claude-fable-5-1", "claude-fable-5[1m]"):
            with self.subTest(selector=selector):
                entry["selector"] = selector
                self._refused("fable-next", entry, "canonical wire id")
        entry["selector"] = "claude-fable-5-1[1m]"
        dev._check_new_entry_policy(
            self._policy_docs("fable-next", entry),
            dev.make_model_draft(name="d1", provider="anthropic", entry=entry),
        )

    def test_openai_compatible_selector_is_the_key(self) -> None:
        docs = catalog.load_raw(self.resources)["docs"]
        entry = copy.deepcopy(docs["models"]["models"]["qwen-flash-next"])
        entry.update(id="local2", wire_model="w", selector="claude-multi-local-2", status="new")
        self._refused("local2", entry, "must be 'claude-multi-local2'")
        entry["selector"] = "claude-multi-local2"
        dev._check_new_entry_policy(
            self._policy_docs("local2", entry),
            dev.make_model_draft(name="d1", provider="llm-local", entry=entry),
        )


class DraftVersionTests(OnboardingTestCase):
    """Draft and review data version 2; 2.x drafts point at migrate."""

    def _v1_draft(self) -> dict:
        draft = _model_draft()
        draft["version"] = 1
        return draft

    def test_drafts_and_reviews_are_version_2(self) -> None:
        from claude_multi import validate

        schema = strict_json.load(CATALOG_ROOT / "schemas" / "draft.schema.json")
        self.assertEqual(schema["version"], 1)  # schema-meta stays 1
        self.assertEqual(schema["properties"]["version"], {"const": 2})
        for draft in (_model_draft(), _provider_draft()):
            self.assertEqual(draft["version"], 2)
            self.assertEqual(validate.validate(draft, schema), [])
        self.assertTrue(validate.validate(self._v1_draft(), schema))
        record = self._review(_model_draft())
        self.assertEqual(record["version"], 2)
        review_schema = strict_json.load(CATALOG_ROOT / "schemas" / "review.schema.json")
        self.assertEqual(review_schema["version"], 1)
        record["version"] = 1
        self.assertTrue(validate.validate(record, review_schema))

    def test_v1_draft_load_names_drafts_migrate(self) -> None:
        state.atomic_write(
            self.drafts.root / "old.json", strict_json.canonical_file_bytes(self._v1_draft())
        )
        with self.assertRaisesRegex(
            DevError, "draft old is a version-1 draft; run claude-multi-dev drafts migrate"
        ):
            self.drafts.load("old")

    def test_cli_check_on_v1_draft_exits_2_with_migrate_pointer(self) -> None:
        store = dev.DraftStore(self.root / "claude-multi" / "drafts")
        state.atomic_write(
            store.root / "old.json", strict_json.canonical_file_bytes(self._v1_draft())
        )
        error = io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}), mock.patch(
            "sys.stderr", error
        ):
            self.assertEqual(dev.main(["check", "old", "--repo", str(self.repo)]), 2)
        self.assertIn("run claude-multi-dev drafts migrate", error.getvalue())
        self.assertNotIn("must equal", error.getvalue())

    def test_v1_review_record_refused_by_promote(self) -> None:
        draft = _model_draft()
        record = self._review(draft)
        record["version"] = 1
        with self.assertRaisesRegex(DevError, "review record invalid"):
            dev.promote_draft(draft, draft_name="d1", repo=self.repo, review_record=record)

    def test_journal_stays_version_1(self) -> None:
        draft = _model_draft()
        dev.promote_draft(
            draft, draft_name="d1", repo=self.repo, review_record=self._review(draft),
            drafts_root=self.drafts.root,
        )
        journal = strict_json.loads(state.read_private(self.drafts.root / "d1.journal.json"))
        self.assertEqual(journal["version"], 1)


def _catalog32_entry(key: str) -> dict:
    frozen = strict_json.load(FIXTURE_ROOT.parent / "catalog32-models.json")
    return copy.deepcopy(frozen["models"][key])


def _v1_model_draft(new_id: str = "qwen39", like: str = "qwen38") -> dict:
    entry = _catalog32_entry(like)
    entry["id"] = new_id
    entry["wire_model"] = "qwen3.9-max"
    return {
        "version": 1,
        "kind": "model",
        "provider": entry["provider"],
        "entry": entry,
        "notes": "a 2.x scaffold",
        "created_at": "2026-07-01T00:00:00Z",
    }


def _v1_provider_draft() -> dict:
    draft = _provider_draft()
    draft["version"] = 1
    draft["created_at"] = "2026-07-01T00:00:00Z"
    return draft


class DraftsMigrateTests(OnboardingTestCase):
    """`claude-multi-dev drafts migrate` on temp roots only."""

    def setUp(self) -> None:
        super().setUp()
        self.state_home = self.root / "xdg-state"
        self.store = dev.DraftStore(self.state_home / "claude-multi" / "drafts")
        self.droot = self.store.root

    def _put(self, name: str, document) -> None:
        state.atomic_write(self.droot / name, strict_json.canonical_file_bytes(document))

    def _seed_three(self) -> None:
        # (a) promoted in catalog <=32: draft + review + journal
        self._put("promoted.json", _v1_model_draft("llm2", "qwen-flash-next"))
        self._put("promoted.review.json", {"version": 1, "draft": "promoted"})
        self._put("promoted.journal.json", {"version": 1, "draft": "promoted", "applied": []})
        # (b) a convertible v1 model draft (unreviewed)
        self._put("conv.json", _v1_model_draft())
        # (c) a v1 provider draft: re-draft against catalog 33
        self._put("prov.json", _v1_provider_draft())

    def _tree(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.droot)): path.read_bytes()
            for path in sorted(self.droot.rglob("*"))
            if path.is_file()
        }

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": str(self.state_home), "HOME": str(self.root / "home")}
        ), redirect_stdout(out), mock.patch("sys.stderr", err):
            code = dev.main(["drafts", "migrate", *args, "--repo", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_classifies_and_changes_nothing(self) -> None:
        self._seed_three()
        before = self._tree()
        code, out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertIn(
            "promoted: promoted in catalog <=32: archive "
            "(promoted.json, promoted.review.json, promoted.journal.json)",
            out,
        )
        self.assertIn("conv: migrate to v2 (status new, generation QUALIFY) (conv.json)", out)
        self.assertIn("prov: discard: re-draft against catalog 33 (prov.json)", out)
        self.assertIn("drafts: 3 (files: 5)", out)
        self.assertIn("dry run: nothing was changed", out)
        self.assertEqual(self._tree(), before)
        self.assertFalse((self.droot / dev.DRAFTS_ARCHIVE).exists())

    def test_apply_renames_never_deletes_and_converts(self) -> None:
        self._seed_three()
        before = self._tree()
        code, out, err = self._run("--apply")
        self.assertEqual(code, 0, err)
        archive = self.droot / dev.DRAFTS_ARCHIVE
        self.assertEqual(stat.S_IMODE(os.lstat(archive).st_mode), 0o700)
        after = self._tree()
        # Every original byte-for-byte under archive-2x/, nothing deleted.
        for name, data in before.items():
            self.assertEqual(after[f"{dev.DRAFTS_ARCHIVE}/{name}"], data)
        self.assertEqual(sorted(p.name for p in self.droot.glob("*.json")), ["conv.json"])
        self.assertEqual(len(after), len(before) + 1)
        self.assertIn(
            "applied: archived 1, migrated 1, discarded 1, kept 0", out
        )
        migrated = self.store.load("conv")  # version 2: loads without the pointer
        self.assertEqual(migrated["version"], 2)
        self.assertNotEqual(migrated["created_at"], "2026-07-01T00:00:00Z")
        entry = migrated["entry"]
        self.assertEqual(entry["id"], "qwen39")
        self.assertEqual(entry["status"], "new")
        self.assertEqual(entry["generation"], "QUALIFY: generation")
        self.assertIsNone(entry["registry_overlay"])
        self.assertEqual(
            entry["efforts"],
            {"max": {"selector": "claude-multi-qwen38-max[1m]",
                     "proxy_contract": "reasoning-effort-xhigh"}},
        )
        self.assertEqual(entry["roles"], "all")
        for removed in ("lanes", "default_lane", "client_selector", "compatible_roles", "role_hints"):
            self.assertNotIn(removed, entry)
        self.assertIn("a 2.x scaffold", migrated["notes"])
        # The converted draft still has to pass the human gate before check.
        with self.assertRaisesRegex(DevError, "QUALIFY"):
            dev._reject_qualify_markers(migrated)
        # A second run finds only the migrated v2 draft and keeps it.
        code, out, _err = self._run("--apply")
        self.assertEqual(code, 0)
        self.assertIn("conv: already a v2 draft: untouched", out)
        self.assertIn("drafts: 1 (files: 1)", out)

    def test_client_effort_v1_draft_converts_to_a_selector_line(self) -> None:
        self._put("local.json", _v1_model_draft("local2", "qwen-flash-next"))
        code, _out, err = self._run("--apply")
        self.assertEqual(code, 0, err)
        entry = self.store.load("local")["entry"]
        self.assertEqual(entry["efforts"], ["high"])
        self.assertEqual(entry["selector"], "claude-multi-qwen-flash-next")
        self.assertEqual(entry["status"], "new")

    def test_unconvertible_and_unknown_provider_drafts_are_discarded(self) -> None:
        broken = _v1_model_draft()
        del broken["entry"]["lanes"]
        self._put("broken.json", broken)
        foreign = _v1_model_draft("zz")
        foreign["entry"]["provider"] = "zeta"
        self._put("foreign.json", foreign)
        state.atomic_write(self.droot / "garbage.json", b"{oops")
        code, out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertIn("broken: discard: not convertible", out)
        self.assertIn("foreign: discard: provider 'zeta' is not in the catalog", out)
        self.assertIn("garbage: unreadable", out)
        self.assertIn("drafts: 3 (files: 3)", out)

    def test_orphan_companions_are_reported_and_left(self) -> None:
        self._put("gone.review.json", {"version": 1})
        before = self._tree()
        code, out, _err = self._run("--apply")
        self.assertEqual(code, 0)
        self.assertIn("gone.review.json: no matching draft; left in place", out)
        self.assertEqual(self._tree(), before)

    def test_apply_refused_under_newer_state_marker(self) -> None:
        self._seed_three()
        before = self._tree()
        state.atomic_write(self.droot.parent / sessions.STATE_MARKER, b"5\n")
        code, _out, err = self._run("--apply")
        self.assertEqual(code, 2)
        self.assertIn("newer claude-multi", err)
        self.assertEqual(self._tree(), before)
        # The dry run stays read-only and available.
        self.assertEqual(self._run()[0], 0)

    def test_archive_collision_refuses_before_any_change(self) -> None:
        self._seed_three()
        archive = state.ensure_private_dir(self.droot / dev.DRAFTS_ARCHIVE)
        state.atomic_write(archive / "prov.json", b"{}")
        before = self._tree()
        code, _out, err = self._run("--apply")
        self.assertEqual(code, 2)
        self.assertIn("already holds prov.json", err)
        self.assertEqual(self._tree(), before)

    def test_absent_drafts_root_is_never_created(self) -> None:
        shutil.rmtree(self.droot)
        code, out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertIn("drafts: 0 (files: 0)", out)
        self.assertFalse(self.droot.exists())

    def test_usage_errors(self) -> None:
        for argv in (["drafts"], ["drafts", "list"], ["drafts", "migrate", "--force"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(dev.main(argv), 2)
                self.assertIn("drafts", err.getvalue())
