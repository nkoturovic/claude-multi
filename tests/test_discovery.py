"""Discovery: candidates, served extras, listing plans, the
unified consent guard, the bounded transport, marks, drift, declaration
from a listing, the public feed peek and the LAN reachability observation.

Every observation is injected: a synthetic pinned registry under a temp
``CLAUDE_MULTI_REGISTRY_DIR``, a served snapshot through the Runtime seam,
listing bodies through ``Runtime.listing_transport``. The bounded transport
itself runs only against local fake servers on port 0 (never 8317/8316).
No test pins a shipped model id: wires are invented or derived from the
frozen fixture catalog.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import test_cli  # module import: no test classes re-exported
from _catalog import FIXTURE_ROOT
from claude_multi import catalog, discovery, launch, operator as operator_mod, proxy
from claude_multi.cli import gateway_facts
from claude_multi.cli import parser as parser_mod

ACME_ADD = test_cli.ACME_ADD
LAN_ADD = ["providers", "add", "lanbox", "--kind", "openai-compatible-lan", "--base-url",
           "http://box.lan:8000/v1", "--auth", "none", "--family", "local"]
DAY = 86400


def _registry_dir(testcase: unittest.TestCase, models: dict, codex: list | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="claude-multi-discovery-registry-"))
    testcase.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
    (root / "models.json").write_text(json.dumps(models))
    (root / "codex_client_models.json").write_text(json.dumps({"models": codex or []}))
    return root


def _fixture_docs() -> dict:
    return catalog.load_catalog(FIXTURE_ROOT).docs


def _wire(provider: str) -> str:
    lines = catalog.load_catalog(FIXTURE_ROOT).lines
    return sorted(line["wire_model"] for line in lines.values() if line["provider"] == provider)[0]


# ------------------------------------------------------------ candidates
class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = _fixture_docs()
        self.claude_known = _wire("anthropic")
        self.codex_known = _wire("openai")
        self.foreign = _wire("kimi")  # a kimi wire spelled again on the claude channel

    def registry(self, extra_claude=(), extra_codex=(), other=None) -> catalog.PinnedRegistry:
        models = {
            "claude": [{"id": self.claude_known, "created": 1000 * DAY},
                       {"id": "claude-fixture-new", "created": 1100 * DAY, "context_length": 400000,
                        "max_completion_tokens": 32000, "display_name": "Fixture New"},
                       {"id": "claude-fixture-old", "created": 900 * DAY},
                       {"id": "claude-fixture-undated"},
                       {"id": self.foreign, "created": 1200 * DAY},
                       *extra_claude],
            "codex-pro": [{"id": self.codex_known, "created": 1000 * DAY},
                          {"id": "gpt-fixture-next", "created": 1050 * DAY}, *extra_codex],
            "codex-free": [{"id": "gpt-fixture-free", "created": 1300 * DAY}],
            "gemini": [{"id": "gemini-fixture", "created": 1300 * DAY}],
            **(other or {}),
        }
        codex = [{"slug": "gpt-fixture-next", "visibility": "hide"}]
        return catalog.load_pinned_registry(_registry_dir(self, models, codex))

    def test_known_aliases_retired_continuity_and_unattributed_ids(self) -> None:
        continuity = {"cont-alias-1": {"provider": "anthropic", "wire": "claude-fixture-cont"}}
        captures = {"custom-captured-high": {"provider": "anthropic", "wire": "claude-fixture-captured"}}
        retired = next(iter(self.docs["retired"]["retired"].values()))
        registry = self.registry(extra_claude=[
            {"id": "claude-fixture-cont", "created": 950 * DAY},
            {"id": "claude-fixture-captured", "created": 950 * DAY},
            {"id": catalog.REFUSAL_FALLBACK_WIRES[0], "created": 950 * DAY},
        ], extra_codex=[{"id": retired["last_wire"], "created": 950 * DAY}])
        known = discovery.known_index(self.docs, continuity=continuity, captures=captures,
                                      expected={"claude-multi-render-0123abcd"})
        served = (launch.ServedModel("claude-fixture-new", "anthropic", 1100 * DAY),
                  launch.ServedModel("mystery-model", "someone", None),
                  launch.ServedModel("claude-multi-stale-high"), launch.ServedModel("custom-old-x"),
                  launch.ServedModel("claude-multi-render-0123abcd"), launch.ServedModel("cont-alias-1"))
        report = discovery.candidates(registry, served, known)
        rows = {(row.channel, row.wire): row for row in report.rows}
        self.assertNotIn(("claude", self.claude_known), rows)
        self.assertNotIn(("claude", "claude-fixture-cont"), rows)
        self.assertNotIn(("claude", "claude-fixture-captured"), rows)
        self.assertNotIn(("claude", catalog.REFUSAL_FALLBACK_WIRES[0]), rows)
        self.assertNotIn(("codex-pro", self.codex_known), rows)
        # Provider/channel scoped: a kimi wire never suppresses a claude candidate.
        self.assertIn(("claude", self.foreign), rows)
        self.assertEqual(rows[("claude", "claude-fixture-new")].mark, "served candidate")
        self.assertEqual(rows[("claude", "claude-fixture-new")].provider_hint, "anthropic")
        self.assertEqual(rows[("claude", "claude-fixture-undated")].mark, "candidate")
        self.assertEqual(rows[("codex-pro", "gpt-fixture-next")].visibility, "hide")
        self.assertEqual([row.wire for row in report.unattributed], ["mystery-model"])
        self.assertEqual(report.unattributed[0].owned_by, "someone")
        self.assertIsNone(report.unattributed[0].provider_hint)

    def test_created_cutoff_unknown_dates_and_all(self) -> None:
        registry = self.registry()
        known = discovery.known_index(self.docs)
        default = discovery.candidates(registry, (), known)
        wires = {(row.channel, row.wire) for row in default.rows}
        self.assertIn(("claude", "claude-fixture-new"), wires)
        self.assertIn(("claude", "claude-fixture-undated"), wires)  # unknown date stays visible
        self.assertNotIn(("claude", "claude-fixture-old"), wires)  # older than the onboarded wire
        self.assertEqual(default.hidden_older, {"claude": 1})
        self.assertEqual(default.hidden_channels, ("codex-free", "gemini"))
        self.assertFalse({row.channel for row in default.rows} - set(discovery.DEFAULT_SECTIONS))
        everything = discovery.candidates(registry, (), known, include_all=True)
        wires = {(row.channel, row.wire) for row in everything.rows}
        self.assertIn(("claude", "claude-fixture-old"), wires)
        self.assertIn(("gemini", "gemini-fixture"), wires)
        self.assertIsNone(next(r for r in everything.rows if r.channel == "gemini").provider_hint)
        self.assertEqual(everything.hidden_older, {})

    def test_raw_wires_and_unmapped_channels_stay_provider_scoped(self) -> None:
        # The anthropic wire is also its own raw passthrough selector and a
        # rendered selector; neither suppresses the same spelling on another
        # provider's channel. A kimi-named channel is scoped to kimi.
        registry = self.registry(extra_codex=[{"id": self.claude_known}],
                                 other={"gemini": [{"id": self.claude_known}], "kimi": [{"id": self.foreign}]})
        known = discovery.known_index(self.docs, expected={self.claude_known, f"{self.claude_known}[1m]"})
        self.assertNotIn(self.claude_known, known.selectors)
        self.assertNotIn(self.claude_known, known.wires.get("openai", frozenset()))
        report = discovery.candidates(registry, (), known, include_all=True)
        rows = {(row.channel, row.wire) for row in report.rows}
        self.assertIn(("codex-pro", self.claude_known), rows)
        self.assertIn(("gemini", self.claude_known), rows)
        self.assertNotIn(("claude", self.claude_known), rows)
        self.assertNotIn(("kimi", self.foreign), rows)
        # A served raw wire of a known line is still known (not unattributed).
        served = (launch.ServedModel(self.claude_known),)
        self.assertEqual(discovery.candidates(None, served, known).unattributed, ())

    def test_ambiguous_served_id_stays_unattributed_like_doctor(self) -> None:
        registry = self.registry(extra_claude=[{"id": "shared-wire"}], other={"kimi": [{"id": "shared-wire"}]})
        known = discovery.known_index(self.docs)
        served = (launch.ServedModel("shared-wire", "someone"), launch.ServedModel("claude-fixture-new"))
        report = discovery.candidates(registry, served, known, include_all=True)
        rows = {(row.channel, row.wire): row for row in report.rows}
        self.assertEqual(rows[("claude", "shared-wire")].mark, "candidate")
        self.assertEqual(rows[("kimi", "shared-wire")].mark, "candidate")
        self.assertIsNone(rows[("claude", "shared-wire")].owned_by)
        self.assertEqual(rows[("claude", "claude-fixture-new")].mark, "served candidate")
        self.assertEqual([row.wire for row in report.unattributed], ["shared-wire"])
        self.assertEqual(report.unattributed[0].owned_by, "someone")
        self.assertEqual(discovery.served_extras(registry, served, known),
                         ({"claude": ["claude-fixture-new"]}, ["shared-wire"]))

    def test_served_extras_count_unique_ids_and_ambiguity(self) -> None:
        registry = self.registry(other={"kimi": [{"id": "shared-wire"}], "codex-team": [{"id": "gpt-fixture-free"}]},
                                 extra_claude=[{"id": "shared-wire"}])
        known = discovery.known_index(self.docs)
        served = [launch.ServedModel(wire) for wire in (
            "gpt-fixture-free", "claude-fixture-new", "shared-wire", "loose-id", self.claude_known,
            "claude-multi-render-00000000", "gpt-multi-anything-high")]
        by_channel, unattributed = discovery.served_extras(registry, served, known)
        # gpt-fixture-free is in codex-free and codex-team: one id, one count.
        self.assertEqual(by_channel, {"claude": ["claude-fixture-new"], "codex-free": ["gpt-fixture-free"]})
        self.assertEqual(unattributed, ["loose-id", "shared-wire"])
        self.assertEqual(discovery.doctor_info_lines(by_channel, unattributed), [
            "discovery: 4 routable, uncataloged model IDs — claude-multi models --candidates",
            "discovery claude: 1 routable, uncataloged; locally registered only, upstream access unverified",
            "discovery codex-free: 1 routable, uncataloged; locally registered only, upstream access unverified",
            "discovery unattributed: 2 routable, uncataloged; locally registered only, upstream access unverified",
        ])
        self.assertEqual(discovery.doctor_info_lines({}, []), [])


# ------------------------------------------------------------ CLI base
class DiscoveryCLICase(test_cli.OperatorCommandCase):
    """The operator CLI fixture plus an injected listing transport."""

    def setUp(self) -> None:
        super().setUp()
        self.sent: list[tuple[str, dict]] = []
        self.bodies: dict[str, bytes] = {}
        self.runtime.listing_transport = self.transport
        self.today = discovery._date(int(time.time())) or ""

    def transport(self, url, headers, *, deadline, max_bytes):
        self.assertEqual((deadline, max_bytes), (20.0, 4 * 1024 * 1024))
        self.sent.append((url, dict(headers)))
        body = self.bodies.get(url)
        if body is None:
            raise proxy.ListingFailure("http", 404)
        return body

    def listing(self, url: str, entries: list[dict], **envelope) -> None:
        self.bodies[url] = json.dumps({"data": entries, **envelope}).encode()

    def approve_acme(self) -> None:
        code, _out, err = self.op(ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)


# ------------------------------------------------------------ consent
class ConsentTests(DiscoveryCLICase):
    def test_decline_sends_zero_requests(self) -> None:
        self.approve_acme()
        before = self.state_bytes()
        for argv in (["discover", "kimi"], ["discover", "acme"], ["discover", "--all"], ["discover", "--feed"],
                     ["discover", "openrouter", "--add", "x-ai/fresh"]):
            for answer in ("n\n", "\n", ""):
                with self.subTest(argv=argv, answer=answer):
                    code, out, err = self.op(argv, answer)
                    self.assertEqual(code, 0, err)
                    self.assertIn("declined — nothing sent", err)
                    self.assertEqual(out, "")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state_bytes(), before)

    def test_session_or_non_tty_refuses_before_secret_access(self) -> None:
        self.approve_acme()
        before = self.state_bytes()
        cases = [({"CLAUDE_MULTI_MANAGED_ID": test_cli.FIXED_ID}, True, "CLAUDE_MULTI_MANAGED_ID set"),
                 ({"CLAUDECODE": ""}, True, "CLAUDECODE set"),
                 ({}, False, "stdin/stdout is not a terminal")]
        for env, tty, reason in cases:
            for argv in (["discover", "acme"], ["discover", "--all"], ["discover", "--feed"],
                         ["discover", "kimi", "--add", "k9"]):
                with self.subTest(argv=argv, reason=reason), \
                        mock.patch("claude_multi.secret_store.FileSecretStore._values",
                                   side_effect=AssertionError("secret read before the guard")), \
                        mock.patch.object(operator_mod, "read_providers_dir",
                                          side_effect=AssertionError("plan before the guard")):
                    code, out, err = self.op(argv, "y\n", tty=tty, env=env)
                    self.assertEqual(code, 1, out + err)
                    self.assertIn(f"{' '.join(argv[:2])}: needs a terminal outside Claude Code sessions "
                                  f"({reason}) — run it in a separate shell", err)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state_bytes(), before)

    def test_all_plan_names_every_call_and_skip_before_one_consent(self) -> None:
        self.approve_acme()
        code, _out, err = self.op(LAN_ADD)
        self.assertEqual(code, 0, err)
        code, out, err = self.op(["discover", "--all"], "n\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [])
        # Deterministic, provider-id sorted; the codex pool is excluded by name.
        self.assertIn("skipped anthropic: an OAuth pool has no model listing", err)
        self.assertIn("skipped deepseek: credential DEEPSEEK_CLAUDE_API_KEY is not set — "
                      "claude-multi providers set-key deepseek", err)
        self.assertIn("skipped qwen: model listing unsupported", err)
        self.assertIn("openai: not included; its plan listing requires separate consent:\n"
                      "  claude-multi discover openai", err)
        block = err[err.index("claude-multi will make "):]
        self.assertTrue(block.startswith("claude-multi will make 4 requests to providers:\n"), block)
        urls = [line.split("GET ", 1)[1] for line in block.splitlines() if "GET " in line]
        self.assertEqual(urls, ["https://api.acme.example/anthropic/v1/models",
                                "https://api.kimi.com/coding/v1/models",
                                "http://box.lan:8000/v1/models",
                                "https://openrouter.ai/api/v1/models"])
        self.assertIn("     auth: bearer from ACME_API_KEY (value not shown)\n"
                      "     endpoint: unverified (an attempt; a failure here is not a refusal by the provider)\n"
                      "     why: list models to declare; nothing is admitted\n"
                      "     caps: 20 s wall-clock; 4 MiB response; no redirects or retries\n", block)
        self.assertIn("     auth: header x-api-key from KIMI_CLAUDE_API_KEY (value not shown)", block)
        self.assertIn("  4. GET https://openrouter.ai/api/v1/models\n     auth: none; no credential is sent\n"
                      "     why:", block)
        self.assertIn("\nProceed with these listed requests? [y/N] discover --all: declined — nothing sent\n", block)
        self.assertNotIn("dummy", err)

    def test_all_continues_past_ordinary_failures_and_exits_one(self) -> None:
        self.listing("https://openrouter.ai/api/v1/models", [{"id": "x-ai/fresh", "name": "Fresh"}])
        code, out, err = self.op(["discover", "--all"], "y\n")
        self.assertEqual(code, 1, err)
        self.assertEqual([url for url, _h in self.sent], ["https://api.kimi.com/coding/v1/models",
                                                         "https://openrouter.ai/api/v1/models"])
        self.assertIn("== kimi (GET https://api.kimi.com/coding/v1/models)\nkimi: listing failed (HTTP 404)", out)
        self.assertIn("== openrouter (GET https://openrouter.ai/api/v1/models)\nx-ai/fresh\tcandidate", out)

    def test_all_continues_past_a_malformed_response_from_the_real_transport(self) -> None:
        bounds = TransportBoundsTests("test_success_and_scheme_refusal")
        local, hits = bounds.serve("bad-status")
        self.addCleanup(bounds.doCleanups)
        self.listing("https://openrouter.ai/api/v1/models", [{"id": "x-ai/fresh", "name": "Fresh"}])
        fake = self.transport

        def transport(url, headers, *, deadline, max_bytes):
            if url == "https://api.kimi.com/coding/v1/models":
                self.sent.append((url, dict(headers)))
                return proxy.bounded_get(local, headers, deadline=deadline, max_bytes=max_bytes)
            return fake(url, headers, deadline=deadline, max_bytes=max_bytes)

        self.runtime.listing_transport = transport
        code, out, err = self.op(["discover", "--all"], "y\n")
        self.assertEqual(code, 1, err)
        self.assertEqual(len(hits), 1)
        self.assertIn("kimi: listing failed (malformed HTTP response)", out)
        self.assertIn("== openrouter (GET https://openrouter.ai/api/v1/models)\nx-ai/fresh\tcandidate", out)
        self.assertNotIn("Traceback", out + err)

    def test_disabled_provider_is_skipped_in_all_but_inspectable_alone(self) -> None:
        self.listing("https://api.kimi.com/coding/v1/models", [{"id": "k9"}])
        with contextlib.redirect_stderr(io.StringIO()):
            self.runtime.settings_store.update(
                lambda doc: doc.setdefault("providers", {}).__setitem__("kimi", {"enabled": False}),
                catalog=self.runtime.lineup_catalog())
        code, _out, err = self.op(["discover", "--all"], "n\n")
        self.assertIn("skipped kimi: disabled in Settings", err)
        code, out, err = self.op(["discover", "kimi"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("k9\tcandidate", out)


# ------------------------------------------------------------ routes
class RouteTests(DiscoveryCLICase):
    def test_unapproved_changed_and_raced_routes_send_nothing(self) -> None:
        refusal = "discover acme: route unapproved or changed — claude-multi providers approve acme"
        code, _out, err = self.op([*ACME_ADD, "--declare-only"])
        self.assertEqual(code, 0, err)
        code, _out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn(refusal, err)
        code, _out, err = self.op(["providers", "approve", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        # Changed: the header route binds the credential route digest; a
        # secret-name edit after approval makes the route "changed".
        path = operator_mod.providers_dir(self.env) / "acme.json"
        document = json.loads(path.read_text())
        document["provider"]["auth"]["secret_ref"] = "env:ACME_OTHER_KEY"
        path.write_text(json.dumps(document))
        code, _out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn(refusal, err)
        self.assertEqual(self.sent, [])
        document["provider"]["auth"]["secret_ref"] = "env:ACME_API_KEY"
        path.write_text(json.dumps(document))
        # Raced: the declaration changes while the consent is open.
        url = "https://api.acme.example/anthropic/v1/models"
        self.listing(url, [{"id": "acme-new"}])
        original = test_cli.consent_mod.confirm

        def edit_during_prompt(text, *, input_stream):
            raced = dict(document, provider={**document["provider"], "display": "Acme renamed"})
            path.write_text(json.dumps(raced))
            return original(text, input_stream=input_stream)

        with mock.patch.object(test_cli.consent_mod, "confirm", side_effect=edit_during_prompt):
            code, out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("discover acme: configuration changed while awaiting confirmation — nothing sent; retry", err)
        self.assertEqual(self.sent, [])
        self.assertEqual(out, "")

    def test_approved_t2_listing_sends_one_bearer_request_to_the_attempt_url(self) -> None:
        self.approve_acme()
        url = "https://api.acme.example/anthropic/v1/models"
        self.listing(url, [{"id": "acme-new", "display_name": "Acme New", "context_length": 200000}])
        code, out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.sent), 1)
        sent_url, headers = self.sent[0]
        self.assertEqual(sent_url, url)
        self.assertEqual(headers, {"Authorization": "Bearer acme-dummy-value", "anthropic-version": "2023-06-01"})
        self.assertNotIn("acme-dummy-value", out + err)
        self.assertEqual(out, "acme-new\tcandidate context=200000\n")

    def test_attempt_descriptors_follow_the_provider_kind(self) -> None:
        # anthropic-compatible {base}/v1/models (anthropic shape);
        # the keyless LAN kind {base}/models (openai shape, no credential).
        self.approve_acme()
        code, _out, err = self.op(LAN_ADD)
        self.assertEqual(code, 0, err)
        layer = self.runtime.operator_snapshot().layer
        docs = operator_mod.merge_docs(self.runtime.catalog.docs, layer)
        acme = discovery.plan_provider("acme", docs=docs, layer=layer, descriptors=proxy._LISTING_SUPPORT)
        lan = discovery.plan_provider("lanbox", docs=docs, layer=layer, descriptors=proxy._LISTING_SUPPORT)
        self.assertEqual((acme.url, acme.shape, acme.auth, acme.secret_name, acme.verified, acme.tier),
                         ("https://api.acme.example/anthropic/v1/models", "anthropic", "bearer", "ACME_API_KEY",
                          False, "T2"))
        self.assertEqual((lan.url, lan.shape, lan.auth, lan.secret_name, lan.route),
                         ("http://box.lan:8000/v1/models", "openai", "none", None, "keyless"))
        self.listing(lan.url, [{"id": "lan-model-2"}])
        code, out, err = self.op(["discover", "lanbox"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent[-1], ("http://box.lan:8000/v1/models", {}))
        self.assertIn("auth: none; no credential is sent", err)

    def test_credentialed_listing_stays_on_the_approved_origin(self) -> None:
        self.approve_acme()
        layer = self.runtime.operator_snapshot().layer
        docs = operator_mod.merge_docs(self.runtime.catalog.docs, layer)
        import dataclasses

        forged = dataclasses.replace(layer.providers["acme"], listing={
            "url": "https://elsewhere.example/v1/models", "shape": "openai", "auth": "bearer"})
        layer = dataclasses.replace(layer, providers={**layer.providers, "acme": forged})
        with self.assertRaisesRegex(discovery.PlanRefusal, "approved origin https://api.acme.example"):
            discovery.plan_provider("acme", docs=docs, layer=layer, descriptors=proxy._LISTING_SUPPORT)
        public = dataclasses.replace(forged, listing={
            "url": "https://elsewhere.example/v1/models", "shape": "openai", "auth": "none"})
        layer = dataclasses.replace(layer, providers={**layer.providers, "acme": public})
        call = discovery.plan_provider("acme", docs=docs, layer=layer, descriptors=proxy._LISTING_SUPPORT)
        self.assertEqual((call.auth, call.secret_name, call.verified), ("none", None, True))

    def test_a_selected_api_key_transport_is_not_described_as_the_pool(self) -> None:
        layer = self.runtime.operator_snapshot().layer
        docs = operator_mod.merge_docs(self.runtime.catalog.docs, layer)
        with self.assertRaisesRegex(discovery.PlanRefusal, "its selected api-key transport has no reviewed "
                                                          "model listing — declare manually"):
            discovery.plan_provider("anthropic", docs=docs, layer=layer, descriptors=proxy._LISTING_SUPPORT,
                                    transports={"anthropic": "api-key"})
        plan = discovery.plan_all(docs=docs, layer=layer, descriptors=proxy._LISTING_SUPPORT,
                                  enabled=lambda _p: True, secret_present=lambda _n: True,
                                  transports={"openai": "api-key"})
        self.assertFalse(plan.codex_excluded)
        self.assertIn("openai", {item.provider_id for item in plan.skipped})

    def test_anthropic_pool_and_unknown_providers_invent_no_listing(self) -> None:
        for provider, text in (("anthropic", "an OAuth pool has no model listing"),
                               ("nope", "unknown provider 'nope'")):
            with self.subTest(provider=provider):
                code, _out, err = self.op(["discover", provider], "y\n")
                self.assertEqual(code, 1)
                self.assertIn(text, err)
        self.assertEqual(self.sent, [])


# ------------------------------------------------------------ bounded transport
class _SlowHandler(BaseHTTPRequestHandler):
    mode = "ok"
    hits: list = []

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        mode = type(self).mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "stall":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            time.sleep(1.5)
            return
        if mode == "slow-headers":
            self.send_response(200)
            try:
                for index in range(60):
                    self.send_header(f"X-Slow-{index}", "x")
                    self.flush_headers()
                    time.sleep(0.05)
                self.end_headers()
            except OSError:
                pass
            return
        if mode in ("bad-status", "bad-chunk"):
            echo = (self.headers.get("Authorization") or "").encode()
            if mode == "bad-status":
                self.wfile.write(b"HTTP/1.1 2x0 " + echo + b"\r\n\r\n")
            else:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                                 b"zz-" + echo.replace(b" ", b"-") + b"\r\n")
            self.wfile.flush()
            self.close_connection = True
            return
        if mode == "dribble":
            self.send_response(200)
            self.send_header("Content-Length", "400")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b" " * 10)
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return
        body = b"x" * 5000 if mode == "oversize" else b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TransportBoundsTests(unittest.TestCase):
    """The listing transport: total wall-clock and cumulative byte caps, no
    redirects (a credential header never reaches a second request)."""

    def serve(self, mode: str, tls: "ssl.SSLContext | None" = None) -> tuple[str, list]:
        hits: list = []
        handler = type("Handler", (_SlowHandler,), {"mode": mode, "hits": hits})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        if tls is not None:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        scheme = "https" if tls is not None else "http"
        return f"{scheme}://127.0.0.1:{server.server_port}/v1/models", hits

    def tls(self) -> "tuple[ssl.SSLContext, Path]":
        """A disposable self-signed loopback certificate, trusted only
        through SSL_CERT_FILE for the duration of one test."""

        if not shutil.which("openssl"):
            self.skipTest("openssl is needed for a disposable TLS fake")
        root = Path(tempfile.mkdtemp(prefix="claude-multi-listing-tls-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=30)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context, cert

    def test_redirect_receives_no_second_request(self) -> None:
        url, hits = self.serve("redirect")
        with self.assertRaises(proxy.ListingFailure) as raised:
            proxy.bounded_get(url, {"Authorization": "Bearer dummy-secret"}, deadline=5)
        self.assertEqual((raised.exception.kind, raised.exception.status), ("redirect", 302))
        self.assertEqual(hits, [("/v1/models", "Bearer dummy-secret")])
        self.assertNotIn("dummy-secret", str(raised.exception))

    def test_dribble_stall_and_oversize_are_capped(self) -> None:
        for mode, kind, deadline in (("dribble", "timeout", 0.5), ("stall", "timeout", 0.4),
                                     ("oversize", "oversize", 5)):
            with self.subTest(mode=mode):
                url, _hits = self.serve(mode)
                started = time.monotonic()
                with self.assertRaises(proxy.ListingFailure) as raised:
                    proxy.bounded_get(url, {}, deadline=deadline, max_bytes=1024)
                self.assertEqual(raised.exception.kind, kind)
                self.assertLess(time.monotonic() - started, deadline + 1.0)

    def test_success_and_scheme_refusal(self) -> None:
        url, hits = self.serve("ok")
        self.assertEqual(proxy.bounded_get(url, {}), b'{"data": []}')
        self.assertEqual(len(hits), 1)
        for bad in ("file:///etc/passwd", "ftp://example.invalid/x"):
            with self.subTest(url=bad), self.assertRaises(proxy.ListingFailure) as raised:
                proxy.bounded_get(bad, {})
            self.assertEqual(raised.exception.kind, "scheme")

    def test_https_success_and_dribble_use_the_default_transport(self) -> None:
        context, cert = self.tls()
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
            url, hits = self.serve("ok", tls=context)
            self.assertEqual(proxy.bounded_get(url, {"Authorization": "Bearer dummy-secret"}), b'{"data": []}')
            self.assertEqual(hits, [("/v1/models", "Bearer dummy-secret")])
            for mode, deadline in (("dribble", 0.5), ("stall", 0.4)):
                with self.subTest(mode=mode):
                    url, _hits = self.serve(mode, tls=context)
                    started = time.monotonic()
                    with self.assertRaises(proxy.ListingFailure) as raised:
                        proxy.bounded_get(url, {}, deadline=deadline, max_bytes=1024)
                    self.assertEqual(raised.exception.kind, "timeout")
                    self.assertLess(time.monotonic() - started, deadline + 0.5)

    def test_slow_headers_are_inside_the_total_deadline(self) -> None:
        url, _hits = self.serve("slow-headers")
        started = time.monotonic()
        with self.assertRaises(proxy.ListingFailure) as raised:
            proxy.bounded_get(url, {}, deadline=0.3)
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 0.3 + 0.3)

    def test_stalled_resolution_times_out_and_never_sends_later(self) -> None:
        url, hits = self.serve("ok")
        port = int(url.split(":")[2].split("/")[0])
        original = socket.getaddrinfo

        def slow(host, *args, **kwargs):
            if host == "listing.invalid":
                time.sleep(0.6)
                return original("127.0.0.1", port, 0, socket.SOCK_STREAM)
            return original(host, *args, **kwargs)

        with mock.patch("socket.getaddrinfo", side_effect=slow):
            started = time.monotonic()
            with self.assertRaises(proxy.ListingFailure) as raised:
                proxy.bounded_get(f"http://listing.invalid:{port}/v1/models", {}, deadline=0.2)
            self.assertEqual(raised.exception.kind, "timeout")
            self.assertLess(time.monotonic() - started, 0.2 + 0.3)
            time.sleep(0.8)  # the resolver finished; nothing may follow it
        self.assertEqual(hits, [])

    def test_malformed_responses_are_value_free_protocol_failures(self) -> None:
        for mode in ("bad-status", "bad-chunk"):
            with self.subTest(mode=mode):
                url, hits = self.serve(mode)
                with self.assertRaises(proxy.ListingFailure) as raised:
                    proxy.bounded_get(url, {"Authorization": "Bearer dummy-secret"}, deadline=5)
                self.assertEqual(raised.exception.kind, "protocol")
                self.assertEqual(len(hits), 1)
                text = "".join(traceback.format_exception(raised.exception))
                self.assertNotIn("dummy-secret", text)
                self.assertNotIn("dummy-secret", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_parser_completeness_and_shapes(self) -> None:
        body = json.dumps({"data": [{"id": "a", "context_length": 9000, "think_efforts": {"valid_efforts": ["high"]}},
                                    {"id": "b", "created_at": "2026-09-01T00:00:00Z"}, {"nope": 1}],
                           "has_more": True}).encode()
        result = proxy.parse_listing(body, "anthropic")
        self.assertFalse(result.complete)
        self.assertEqual([e["id"] for e in result.entries], ["a", "b"])
        self.assertEqual(result.entries[0]["think_efforts"], ["high"])
        openai = json.dumps({"data": [{"id": "c", "name": "C", "context_length": 128000,
                                       "top_provider": {"max_completion_tokens": 8000},
                                       "reasoning": {"supported_efforts": ["low", "high"]}}]}).encode()
        result = proxy.parse_listing(openai, "openai")
        self.assertTrue(result.complete)
        self.assertEqual(result.entries[0], {"id": "c", "display_name": "C", "context_length": 128000,
                                             "max_completion_tokens": 8000, "think_efforts": ["low", "high"]})
        for raw in (b"{}", b'{"data": {}}', b"[" * 5000, b"not json"):
            with self.subTest(raw=raw[:10]), self.assertRaises(proxy.ListingShapeError):
                proxy.parse_listing(raw, "anthropic")


# ------------------------------------------------------------ marks and drift
class DriftTests(DiscoveryCLICase):
    URL = "https://api.acme.example/anthropic/v1/models"

    def declare(self, argv_tail: list[str]) -> None:
        code, _out, err = self.op(["models", "add", "acme", *argv_tail])
        self.assertEqual(code, 0, err)

    def test_incomplete_listing_cannot_mark_missing(self) -> None:
        self.approve_acme()
        self.declare(["acme-one", "--as", "custom-acme-one", "--context", "131072", "--source", "operator"])
        self.listing(self.URL, [{"id": "acme-other"}], has_more=True)
        code, out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertNotIn("not advertised", out)
        self.assertIn("acme: listing incomplete (the provider reported more pages); absence is not concluded", out)
        # A failed listing concludes nothing either.
        self.bodies.pop(self.URL)
        code, out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("discover acme: listing failed (HTTP 404)", err)
        self.assertEqual(out, "")
        self.listing(self.URL, [{"id": "acme-other"}])
        code, out, _err = self.op(["discover", "acme"], "y\n")
        self.assertIn(f"custom-acme-one: not advertised on {self.today} (Info only; not a retirement)\n", out)

    def test_marks_and_drift_against_the_effective_view(self) -> None:
        self.approve_acme()
        self.declare(["acme-one", "--as", "custom-acme-one", "--context", "131072", "--source", "operator",
                      "--effort", "high"])
        self.listing(self.URL, [
            {"id": "acme-one", "context_length": 262144, "think_efforts": {"valid_efforts": ["high", "max"]},
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "acme-later", "created_at": "2026-06-01T00:00:00Z"},
            {"id": "acme-earlier", "created_at": "2025-06-01T00:00:00Z"},
            {"id": "acme-undated"},
        ])
        code, out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[:4], [
            "acme-one\toperator custom-acme-one (off) context=262144 efforts=high,max created=2026-01-01T00:00:00Z",
            "acme-later\tnew created=2026-06-01T00:00:00Z",
            "acme-earlier\tcandidate created=2025-06-01T00:00:00Z",
            "acme-undated\tcandidate",
        ])
        self.assertEqual(lines[4:], [
            "custom-acme-one: listed context 262144 != declared 131072",
            "  edit: claude-multi models edit custom-acme-one",
            "  change: context.declared_tokens = 262144",
            "  consequence: new class; admission and evidence lapse; running sessions keep their fence until relaunch",
            "custom-acme-one: listing efforts now include max",
            "  edit: claude-multi models edit custom-acme-one",
            "  consequence: only reviewed contracts are eligible; re-admission and qualification are required "
            "after a definition change",
        ])

    def test_operator_states_cover_admitted_changed_and_route(self) -> None:
        layer = mock.Mock()
        line = mock.Mock(provider_id="p", definition_digest="d1",
                         core_entry={"wire_model": "w", "efforts": ["high"], "context": {"declared_tokens": 9000}})
        layer.lines = {"custom-a": line, "custom-b": line, "custom-c": line, "custom-d": line}
        layer.route_status = {"p": "approved"}
        ledger = operator_mod.OperatorLedger.empty()
        ledger = operator_mod.OperatorLedger(ledger.routes, {"custom-a": {"digest": "d1"}, "custom-b": {"digest": "d0"}},
                                             {}, {}, {}, {}, None)
        states = discovery.operator_states(layer, ledger, {"custom-a", "custom-b"})
        self.assertEqual({k: v.status for k, v in states.items()},
                         {"custom-a": "admitted", "custom-b": "changed — re-admit", "custom-c": "off",
                          "custom-d": "off"})
        layer.route_status = {"p": "unapproved"}
        self.assertEqual(discovery.operator_states(layer, ledger, {"custom-a"})["custom-a"].status, "route unapproved")
        marked = discovery.mark_listing("p", [{"id": "w"}, {"id": "leg"}, {"id": "old"}, {"id": "cat"}],
                                        operator={"custom-a": states["custom-a"]},
                                        catalog_lines={"catline": {"provider": "p", "wire_model": "cat"}},
                                        legacy_models={"legacy-x": {"provider": "p", "wire_model": "leg"}},
                                        retired={"gone": {"provider": "p", "last_wire": "old"}})
        self.assertEqual([m.mark for m in marked], ["operator custom-a (admitted)", "legacy custom legacy-x",
                                                    "retired gone (continuity retained)", "cataloged as catline"])


# ------------------------------------------------------------ declaration from a listing
class DeclarationTests(DiscoveryCLICase):
    URL = "https://api.acme.example/anthropic/v1/models"

    def test_add_declares_new_off_with_listing_provenance(self) -> None:
        self.approve_acme()
        self.listing(self.URL, [
            {"id": "acme-fresh", "display_name": "Acme Fresh", "context_length": 262144,
             "think_efforts": {"valid_efforts": ["low", "high", "max"]}},
            {"id": "acme-bare"},
        ])
        code, out, err = self.op(["discover", "acme", "--add", "acme-fresh"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("declared custom-acme-fresh — New · Off · selectors custom-acme-fresh-high[1m]", out)
        line = json.loads((operator_mod.providers_dir(self.env) / "acme.json").read_text())["lines"][
            "custom-acme-fresh"]
        self.assertEqual(line, {
            "wire_model": "acme-fresh", "display": "Acme Fresh",
            "efforts": {"high": "output-config-high"}, "default_effort": "high",
            "context": {"declared_tokens": 262144, "source": "listing", "source_ref": f"{self.URL} {self.today}"},
        })
        self.assertNotIn("custom-acme-fresh", self.runtime.current_effective().admitted_lines)
        ledger = self.ledger()
        self.assertNotIn("custom-acme-fresh", ledger.admissions)
        # Existing lines are never edited; a second add of it is refused.
        code, _out, err = self.op(["discover", "acme", "--add", "acme-fresh"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("acme-fresh is operator custom-acme-fresh (off) — declaration never edits an existing line",
                      err)

    def test_context_rules_and_over_listed(self) -> None:
        self.approve_acme()
        self.listing(self.URL, [{"id": "acme-ctx", "context_length": 100000}, {"id": "acme-none"}])
        code, _out, err = self.op(["discover", "acme", "--add", "acme-none"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("discover acme --add acme-none: the listing states no context — claude-multi models add acme acme-none --context N --source docs", err)
        code, _out, err = self.op(["discover", "acme", "--add", "acme-ctx", "--context", "200000"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("exceeds the listed context 100000; give --over-listed REASON", err)
        code, out, err = self.op(["discover", "acme", "--add", "acme-ctx", "--as", "custom-acme-wide",
                                  "--context", "200000", "--over-listed", "provider docs state 200K"], "y\n")
        self.assertEqual(code, 0, err)
        line = json.loads((operator_mod.providers_dir(self.env) / "acme.json").read_text())["lines"][
            "custom-acme-wide"]
        self.assertEqual(line["context"]["source"], "operator")
        self.assertEqual(line["notes"], "over-listed: provider docs state 200K")
        code, out, err = self.op(["discover", "acme", "--add", "acme-missing"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("acme-missing is not in the listing — declare it manually", err)

    def test_usage_errors_exit_two_before_the_guard(self) -> None:
        for argv in (["discover"], ["discover", "kimi", "--all"], ["discover", "--all", "--add", "x"],
                     ["discover", "kimi", "--as", "custom-x"], ["discover", "kimi", "--add", "a", "b", "--as", "custom-x"],
                     ["discover", "kimi", "--add", "a", "--as", "Bad"],
                     ["discover", "kimi", "--add", "a", "--over-listed", "x" * 201]):
            with self.subTest(argv=argv):
                code, _out, _err = self.op(argv, "y\n", tty=False)
                self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])

    def test_declaration_efforts_and_key_rules(self) -> None:
        contracts = {"payload_contracts": ["output-config-high", "output-config-xhigh"]}
        native = ("low", "medium", "high", "xhigh", "max", "ultracode")
        self.assertEqual(discovery.declaration_efforts(contracts, ["xhigh", "max"], None, native),
                         ({"xhigh": "output-config-xhigh"}, "xhigh"))
        self.assertEqual(discovery.declaration_efforts(contracts, None, None, native),
                         ({"high": "output-config-high"}, "high"))
        self.assertEqual(discovery.declaration_efforts({"payload_contracts": []}, ["max"], None, native),
                         (["high"], "high"))
        with self.assertRaisesRegex(discovery.DeclarationRefusal, "declare the line manually"):
            discovery.declaration_efforts(contracts, ["max"], None, native)
        self.assertEqual(discovery.derived_key("X-AI/Grok_5.0", ()), "custom-x-ai-grok-5-0")

    def test_nothing_advertised_without_a_high_contract_is_the_plain_line(self) -> None:
        # A provider whose reviewed contracts have no high level: a listing
        # that states no level and a declaration by hand get the line
        # `models add` declares by default, never a refusal; advertised levels
        # no contract matches are still refused.
        native = ("low", "medium", "high", "xhigh", "max", "ultracode")
        for contracts in (["output-config-max", "filter-thinking"], ["reasoning-effort-xhigh", "reasoning-effort-max"]):
            provider = {"payload_contracts": contracts}
            with self.subTest(contracts=contracts):
                self.assertEqual(discovery.declaration_efforts(provider, None, None, native), (["high"], "high"))
                self.assertEqual(discovery.declaration_efforts(provider, [], [], native), (["high"], "high"))
                with self.assertRaisesRegex(discovery.DeclarationRefusal, "declare the line manually"):
                    discovery.declaration_efforts(provider, ["low"], None, native)
        self.assertEqual(discovery.derived_key("w", {"custom-w", "custom-w-2"}), "custom-w-3")


# ------------------------------------------------------------ purity and candidates report
class PurityTests(DiscoveryCLICase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = _registry_dir(self, {"claude": [{"id": "claude-fixture-cand", "context_length": 400000,
                                                          "max_completion_tokens": 64000, "created": 2000 * DAY}],
                                             "codex-pro": [{"id": "gpt-fixture-cand"}],
                                             "gemini": [{"id": "gemini-fixture"}]},
                                      [{"slug": "gpt-fixture-cand", "visibility": "hide"}])
        self.runtime.environ[catalog.REGISTRY_DIR_ENV] = str(self.registry)

    def test_observations_leave_authority_files_byte_identical(self) -> None:
        self.approve_acme()
        self.listing("https://api.acme.example/anthropic/v1/models", [{"id": "acme-x"}])
        self.listing(discovery.FEED_URL, [])
        self.bodies[discovery.FEED_URL] = json.dumps({"claude": [{"id": "claude-feed-only"}]}).encode()
        before = self.state_bytes()
        for argv, text in ((["models", "--candidates"], ""), (["models", "--candidates", "--all"], ""),
                           (["discover", "acme"], "y\n"), (["discover", "--feed"], "y\n"),
                           (["discover", "--all"], "y\n")):
            with self.subTest(argv=argv):
                code, _out, err = self.op(argv, text)
                self.assertIn(code, (0, 1), err)
        self.assertEqual(self.state_bytes(), before)

    def test_candidates_report_text_and_no_provider_request(self) -> None:
        self.runtime.served_models_callback = lambda gateway, token: (
            (launch.ServedModel("claude-fixture-cand", "anthropic", 2000 * DAY),
             launch.ServedModel("loose-served", "someone", None)), 200)
        code, out, err = self.op(["models", "--candidates"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "Candidates — advisory only; nothing declared or admitted",
            "claude  claude-fixture-cand  registry-stated context 400000  max output 64000",
            f"  served candidate · visibility: n/a · created {discovery._date(2000 * DAY)}",
            "codex-pro  gpt-fixture-cand  registry-stated context unknown  max output unknown",
            "  candidate · visibility: hide · created unknown",
            "unattributed  loose-served  registry-stated context unknown  max output unknown",
            "  served candidate · owned_by someone (advisory) · created unknown",
            "other registry channels hidden: gemini — --all shows them",
            "next: claude-multi models add PROVIDER WIRE --context N --source registry",
        ])
        self.assertEqual(self.sent, [])
        code, out, _err = self.op(["models", "--candidates", "--all"])
        self.assertIn("gemini  gemini-fixture  registry-stated context unknown  max output unknown", out)

    def test_unavailable_observations_say_so(self) -> None:
        self.served = None  # the fixture gateway is down
        code, out, _err = self.op(["models", "--candidates"])
        self.assertEqual(code, 0)
        self.assertIn("Gateway observation unavailable; showing pinned-registry candidates only.\n", out)
        self.assertNotIn("served candidate", out)
        self.runtime.environ[catalog.REGISTRY_DIR_ENV] = str(self.registry / "missing")
        self.served = set()
        code, out, _err = self.op(["models", "--candidates"])
        self.assertIn("Pinned registry unavailable; showing served candidates only.\n", out)
        self.assertIn("No candidates.\n", out)

    def test_candidate_flag_combinations_are_usage_errors(self) -> None:
        for argv in (["models", "--all"], ["models", "--candidates", "show", "custom-x"]):
            with self.subTest(argv=argv):
                code, out, _err = self.op(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")

    def test_parser_classifies_discover_add_as_a_write(self) -> None:
        parser = parser_mod.build_parser()
        self.assertFalse(parser_mod._writes_versioned_state(parser.parse_args(["discover", "kimi"])))
        self.assertFalse(parser_mod._writes_versioned_state(parser.parse_args(["discover", "--all"])))
        self.assertTrue(parser_mod._writes_versioned_state(parser.parse_args(["discover", "kimi", "--add", "x"])))
        self.assertFalse(parser_mod._writes_versioned_state(parser.parse_args(["models", "--candidates"])))


# ------------------------------------------------------------ feed peek
class FeedTests(DiscoveryCLICase):
    def test_feed_consent_and_advisory_difference(self) -> None:
        registry = _registry_dir(self, {"claude": [{"id": "claude-pinned"}]})
        self.runtime.environ[catalog.REGISTRY_DIR_ENV] = str(registry)
        self.bodies[discovery.FEED_URL] = json.dumps({
            "claude": [{"id": "claude-pinned"}, {"id": "claude-feed-new"}, {"id": "../bad"}],
            "codex-pro": [{"id": "gpt-feed-new"}], "junk": "x"}).encode()
        code, out, err = self.op(["discover", "--feed"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("claude-multi will make ONE credential-free third-party request:\n"
                      f"  GET {discovery.FEED_URL}\n"
                      "  why: compare the public feed with the pinned registry\n"
                      "  caps: 20 s wall-clock; 4 MiB response; no redirects or retries\n"
                      "Nothing is downloaded into the gateway or stored.\nProceed? [y/N]", err)
        self.assertEqual(out, "Public feed difference — advisory, not a pin update\n"
                              "A future pin bump may add:\n"
                              "  claude: claude-feed-new\n"
                              "  codex-pro: gpt-feed-new\n"
                              "No catalog, registry, provider, admission, or evidence file changed.\n")
        self.assertEqual(self.sent, [(discovery.FEED_URL, {"Accept": "application/json"})])


# ------------------------------------------------------------ LAN reachability
class LanReachabilityTests(unittest.TestCase):
    URL = "http://box.lan:8000/v1"

    def test_fixtures_nxdomain_refused_timeout_reachable(self) -> None:
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8000))]

        def hang(_host, _port):
            time.sleep(1.0)
            return addresses

        closed = []
        conn = mock.Mock(close=lambda: closed.append(True))
        cases = {
            "nxdomain": dict(resolve=mock.Mock(side_effect=socket.gaierror(-2, "Name or service not known"))),
            "refused": dict(resolve=lambda *_: addresses, connect=mock.Mock(side_effect=ConnectionRefusedError())),
            "timeout": dict(resolve=lambda *_: addresses, connect=mock.Mock(side_effect=socket.timeout())),
            "resolver-timeout": dict(resolve=hang),
            "reachable": dict(resolve=lambda *_: addresses, connect=mock.Mock(return_value=conn)),
        }
        texts = {
            "nxdomain": "not reachable from this network: box.lan does not resolve",
            "refused": "not reachable from this network: box.lan refused the connection",
            "timeout": "not reachable from this network: box.lan did not answer in time",
            "resolver-timeout": "not reachable from this network: box.lan did not answer in time",
            "reachable": "reachable from this network: box.lan",
        }
        for name, seams in cases.items():
            with self.subTest(case=name):
                started = time.monotonic()
                observation = gateway_facts.probe_lan(self.URL, timeout=0.2, **seams)
                self.assertLess(time.monotonic() - started, 0.9)
                self.assertEqual(observation.text(), texts[name])
                self.assertEqual(observation.state, "reachable" if name == "reachable" else "unreachable")
        self.assertEqual(closed, [True])
        cases["refused"]["connect"].assert_called_once_with(addresses[0], mock.ANY)

    V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::10", 8000, 0, 0))
    V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8000))

    def test_every_resolved_address_is_tried_before_a_verdict(self) -> None:
        """A refused first address (IPv6 without a listener)
        is no verdict while a later one (IPv4) connects; each attempt keeps
        its own family and socket address, within one shared deadline."""

        attempts: list[tuple] = []

        def connect(entry, seconds):
            attempts.append((entry, seconds))
            if entry[0] == socket.AF_INET6:
                raise ConnectionRefusedError()
            return mock.Mock()

        observation = gateway_facts.probe_lan(self.URL, timeout=0.2, resolve=lambda *_: [self.V6, self.V4],
                                              connect=connect)
        self.assertEqual((observation.state, observation.reason), ("reachable", None))
        self.assertEqual([entry for entry, _ in attempts], [self.V6, self.V4])
        self.assertTrue(all(0 < seconds <= 0.2 for _, seconds in attempts))
        # Only when every candidate fails is the host unreachable; refused wins.
        failures = iter([socket.timeout(), ConnectionRefusedError()])

        def fail(entry, seconds):
            raise next(failures)

        observation = gateway_facts.probe_lan(self.URL, timeout=0.2, resolve=lambda *_: [self.V6, self.V4, self.V4],
                                              connect=fail)
        self.assertEqual((observation.state, observation.reason), ("unreachable", "refused"))

    def test_the_shared_deadline_bounds_every_candidate(self) -> None:
        attempts = []

        def slow(entry, seconds):
            attempts.append(entry)
            time.sleep(seconds)
            raise socket.timeout()

        started = time.monotonic()
        observation = gateway_facts.probe_lan(self.URL, timeout=0.2, resolve=lambda *_: [self.V6, self.V4],
                                              connect=slow)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual((observation.state, observation.reason), ("unreachable", "timeout"))
        self.assertEqual(attempts, [self.V6, self.V4])

    def test_a_timing_out_first_address_leaves_the_next_its_turn(self) -> None:
        """A first address that drops packets uses only its
        share of the deadline; the next address still connects in time."""

        shares: list[tuple] = []

        def connect(entry, seconds):
            shares.append((entry, seconds))
            if entry[0] == socket.AF_INET6:
                time.sleep(seconds)
                raise socket.timeout()
            return mock.Mock()

        started = time.monotonic()
        observation = gateway_facts.probe_lan(self.URL, timeout=0.2, resolve=lambda *_: [self.V6, self.V4],
                                              connect=connect)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual((observation.state, observation.reason), ("reachable", None))
        self.assertEqual([entry for entry, _ in shares], [self.V6, self.V4])
        self.assertLessEqual(shares[0][1], 0.1 + 1e-6)
        self.assertGreater(shares[1][1], 0)

    def test_a_resolver_that_used_the_budget_keeps_the_whole_grace_attempt(self) -> None:
        """When resolution used the whole deadline, the one
        minimal attempt keeps its 50 ms; it is not divided."""

        clock = iter([100.0])  # the deadline is set at 100.0; every later read is past it
        attempts: list[tuple] = []

        def connect(entry, seconds):
            attempts.append((entry, seconds))
            raise socket.timeout()

        with mock.patch.object(time, "monotonic", lambda: next(clock, 101.0)):
            observation = gateway_facts.probe_lan(self.URL, timeout=0.2, resolve=lambda *_: [self.V6, self.V4],
                                                  connect=connect)
        self.assertEqual(attempts, [(self.V6, 0.05)])
        self.assertEqual((observation.state, observation.reason), ("unreachable", "timeout"))

    def test_runtime_seam_never_dials_under_the_test_seams(self) -> None:
        runtime = mock.Mock(lan_probe=None, health_get=lambda *_: 200, served_models_callback=None)
        with mock.patch.object(gateway_facts, "probe_lan", side_effect=AssertionError("dialed")):
            observation = gateway_facts.lan_reachability(runtime, self.URL)
        self.assertEqual((observation.state, observation.host), ("unknown", "box.lan"))
        injected = gateway_facts.LanReachability("unreachable", "box.lan", "nxdomain")
        runtime.lan_probe = mock.Mock(return_value=injected)
        self.assertIs(gateway_facts.lan_reachability(runtime, self.URL), injected)


class LanDoctorTests(DiscoveryCLICase):
    def test_unreachable_lan_provider_is_doctor_info_never_block(self) -> None:
        from claude_multi.cli import doctor as doctor_mod

        code, _out, err = self.op(LAN_ADD)
        self.assertEqual(code, 0, err)
        code, _out, err = self.op(["models", "add", "lanbox", "lan-one", "--as", "custom-lan-one", "--context",
                                   "32768", "--source", "operator"])
        self.assertEqual(code, 0, err)
        probes = []
        self.runtime.lan_probe = lambda url: probes.append(url) or gateway_facts.LanReachability(
            "unreachable", "box.lan", "nxdomain")
        self.assertEqual(doctor_mod._doctor_lan_report(self.runtime), [
            "provider lanbox: not reachable from this network: box.lan does not resolve — connect to the host's "
            "network, or bind another model (network-scoped; its lines are unusable from here)"])
        self.assertEqual(probes, ["http://box.lan:8000/v1"])
        problems, info, attention = doctor_mod._collect_doctor_reports(self.runtime)
        self.assertFalse([p for p in problems if "box.lan" in p])
        self.assertFalse([a for a in attention if "box.lan" in a])
        self.assertTrue([i for i in info if "box.lan does not resolve" in i])
        self.runtime.lan_probe = lambda url: gateway_facts.LanReachability("reachable", "box.lan")
        self.assertEqual(doctor_mod._doctor_lan_report(self.runtime), [])





class KeyedDiscoveryTests(unittest.TestCase):
    def test_keyed_listing_descriptor_no_v1_insertion_or_fallback(self):
        import test_openai_compat_keyed as keyed
        for base in ("https://api.chatco.example", "https://api.chatco.example/v1", "https://api.chatco.example/cm/v2"):
            plan, _ = keyed.keyed_plan({keyed.KEYED_ID: keyed.keyed_document(base_url=base)})
            call = discovery.plan_provider(keyed.KEYED_ID, docs=plan.docs, layer=plan.layer,
                                           descriptors=proxy._LISTING_SUPPORT)
            self.assertEqual((call.url, call.shape, call.auth, call.secret_name, call.verified),
                             (base + "/models", "openai", "bearer", keyed.KEYED_SECRET, False))
            self.assertEqual(discovery.ATTEMPT_DESCRIPTORS[operator_mod.KEYED_KIND],
                             ("/models", "openai", "provider"))
            closed = __import__("copy").deepcopy(plan.docs)
            closed["gateway"].pop("audits", None)
            with self.assertRaises(discovery.PlanRefusal):
                discovery.plan_provider(keyed.KEYED_ID, docs=closed, layer=plan.layer, descriptors={})

    def test_keyed_declared_anthropic_listing_shape_refused(self):
        import test_openai_compat_keyed as keyed
        doc = keyed.keyed_document(listing={"url": keyed.KEYED_BASE + "/models", "auth": "provider",
                                           "shape": "anthropic"})
        layer = keyed.layer_of({keyed.KEYED_ID: doc})
        self.assertNotIn(keyed.KEYED_ID, layer.providers)
        self.assertTrue(any("openai shape" in p.text() for p in layer.problems))

    def test_openrouter_messages_public_listing_unchanged(self):
        docs = _fixture_docs()
        before = discovery.plan_provider("openrouter", docs=docs, layer=operator_mod.empty_layer(),
                                         descriptors=proxy._LISTING_SUPPORT)
        docs["gateway"]["audits"] = {"openai_compat_keyed": True}
        after = discovery.plan_provider("openrouter", docs=docs, layer=operator_mod.empty_layer(),
                                        descriptors=proxy._LISTING_SUPPORT)
        self.assertEqual(before, after)
        self.assertEqual((after.auth, after.shape, after.secret_name), ("none", "openai", None))
        self.assertEqual(docs["providers"]["providers"]["openrouter"]["adapter"], "cliproxy-claude-compatible-v1")


if __name__ == "__main__":
    unittest.main()
