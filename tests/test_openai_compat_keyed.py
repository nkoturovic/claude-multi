"""Keyed OpenAI-compatible chat providers (gate, render, availability).

Every provider, wire, header and credential name here is fixture-local
(invented); no shipped model id is pinned. Credential values are dummies and
never asserted by value in output: tests compare booleans and counts.

The trusted audit flag (``gateway.json`` ``audits.openai_compat_keyed``) is
explicitly set for each regression on an in-memory docs copy or a temporary
copy of the fixture asset root, independent of the shipped audit default.
"""

from __future__ import annotations

import copy
import dataclasses
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable
from unittest import mock

from claude_multi import catalog, dev, operator, proxy, render, secret_store, state, strict_json
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT

import test_cli
import test_operator as op_tests

KEYED_ID = "chatco"
KEYED_SECRET = "CHATCO_API_KEY"
KEYED_KEY = "custom-chatco-chat"
KEYED_WIRE = "chatco-chat-1"
KEYED_BASE = "https://api.chatco.example/v1"
# A dummy credential (no token shape; never printed by a test).
KEYED_VALUE = "chatco-dummy-credential-value"
REJECTED_MARKER = "Zqrejectedmarker"


# ------------------------------------------------------------------ fixtures
def fixture_docs(*, audited: bool) -> dict:
    """The frozen fixture docs, the keyed audit open only when asked."""

    docs = copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs)
    docs["gateway"][catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: audited}
    return docs


def keyed_document(
    *, base_url: str = KEYED_BASE, efforts: Iterable[str] = ("low", "high"), default: str = "high",
    headers: dict | None = None, secret: str = KEYED_SECRET, display: str = "Chat Co",
    key: str = KEYED_KEY, wire: str = KEYED_WIRE, **provider_extra: Any,
) -> dict:
    """A schema-valid keyed openai-compatible declaration."""

    provider: dict[str, Any] = {
        "display": display, "kind": operator.KEYED_KIND, "base_url": base_url,
        "auth": {"kind": "bearer", "secret_ref": f"env:{secret}"}, "independence_family": "chatco",
    }
    if headers is not None:
        provider["headers"] = headers
    provider.update(provider_extra)
    return {"version": 1, "provider": provider, "lines": {key: {
        "wire_model": wire, "display": "Chat Co chat", "efforts": list(efforts), "default_effort": default,
        "context": {"declared_tokens": 131072, "source": "docs", "source_ref": "https://docs.chatco.example/m"},
    }}}


def raw(document: dict) -> bytes:
    return strict_json.pretty_file_bytes(document)


def layer_of(files: dict[str, dict], *, audited: bool = True, docs: dict | None = None,
             **kwargs: Any) -> operator.OperatorLayer:
    return operator.validate_layer(docs or fixture_docs(audited=audited), {k: raw(v) for k, v in files.items()},
                                   schemas=op_tests._schemas(), **kwargs)


def texts(layer: operator.OperatorLayer) -> list[str]:
    return [problem.text() for problem in layer.problems]


def t1_keyed_provider(base_url: str = KEYED_BASE, secret: str = KEYED_SECRET) -> dict:
    """A trusted (T1) catalog provider with the equivalent keyed shape."""

    return {
        "display": "Chat Co T1", "independence_family": "chatco", "support": "locally-validated-experimental",
        "support_note": "Fixture keyed compat provider.", "adapter": catalog.OPENAI_COMPAT_ADAPTER,
        "transport": {"kind": "direct-openai", "base_url": base_url,
                      "auth": {"kind": "bearer", "secret_ref": f"env:{secret}"}},
        "passthrough_routes": [], "payload_contracts": [],
    }


def audited_root(base: Path, *, audited: bool = True) -> Path:
    """A private copy of the fixture asset root with the audit flag set."""

    root = base / "assets-audited"
    shutil.copytree(FIXTURE_ROOT, root)
    path = root / "catalog" / "gateway.json"
    document = json.loads(path.read_text())
    document[catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: audited}
    path.write_bytes(strict_json.pretty_file_bytes(document))
    return root


class SpyStore:
    """A non-file SecretStore that records every access by operation."""

    path = None

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})
        self.calls: list[tuple[str, str | None]] = []

    def get(self, name: str) -> Any:
        self.calls.append(("get", name))
        return self.values.get(name)

    def is_set(self, name: str) -> bool:
        self.calls.append(("is_set", name))
        return name in self.values

    def set(self, name: str, value: str) -> int:
        self.calls.append(("set", name))
        self.values[name] = value
        return len(value)

    def delete(self, name: str) -> bool:
        self.calls.append(("delete", name))
        return self.values.pop(name, None) is not None

    def description(self) -> str:
        return "fixture in-memory store"

    def scan_values(self) -> frozenset[str]:
        self.calls.append(("scan_values", None))
        return frozenset(value for value in self.values.values() if isinstance(value, str))

    def count(self, op: str, name: str | None = None) -> int:
        return sum(1 for kind, which in self.calls if kind == op and (name is None or which == name))

    def touched(self, name: str) -> int:
        """get/is_set of ``name`` plus every scan_values (which reads all)."""

        return self.count("get", name) + self.count("is_set", name) + self.count("scan_values")


def spy_store(spy: SpyStore):
    return mock.patch.object(secret_store, "default_store", lambda *args, **kwargs: spy)


class KeyedHomeCase(op_tests.OperatorHomeCase):
    """An operator HOME whose asset root has the keyed audit open (or closed)."""

    AUDITED = True

    def setUp(self) -> None:
        super().setUp()
        self.assets = audited_root(self.root, audited=self.AUDITED)
        self.environ["CLAUDE_MULTI_ASSETS"] = str(self.assets)
        state.atomic_write(self.secret_file, (
            f"ACME_API_KEY={op_tests.ACME_SECRET}\nBETA_API_KEY={op_tests.BETA_SECRET}\n"
            f"KIMI_CLAUDE_API_KEY=kimi-dummy-secret-value\n{KEYED_SECRET}={KEYED_VALUE}\n"
            f"{OTHER_SECRET}={OTHER_VALUE}\n").encode())

    def docs(self) -> dict:
        return copy.deepcopy(catalog.load_catalog(self.assets).docs)

    def approve_keyed(self, files: dict[str, dict], *pids: str) -> dict:
        layer = layer_of(files, audited=True)
        return {pid: op_tests._route(layer.providers[pid]) for pid in pids}


# ================================================================== E1a
class KeyedValidationTests(unittest.TestCase):
    """E1a: the trusted shape gate, metadata validation and the CLI gate."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="keyedtest-keyed-")).resolve()
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _home_env(self, assets: Path) -> tuple[dict[str, str], Path]:
        home = state.ensure_private_dir(self.tmp / "home")
        environ = {"HOME": str(home), "CLAUDE_MULTI_ASSETS": str(assets),
                   "CLAUDE_MULTI_SECRET_ENV": str(self.tmp / "secrets" / "claude.env"),
                   "CLAUDE_MULTI_REGISTRY_DIR": str(self.tmp / "no-registry")}
        return environ, home

    def _install(self, environ: dict[str, str], files: dict[str, dict]) -> None:
        directory = state.ensure_private_dir(operator.providers_dir(environ))
        for name, document in files.items():
            state.atomic_write(directory / f"{name}.json", raw(document))

    # -------------------------------------------------------------- gate
    def test_audit_gate_precedes_secret_resolution(self) -> None:
        closed = audited_root(self.tmp, audited=False)
        environ, home = self._home_env(closed)
        self._install(environ, {KEYED_ID: keyed_document()})
        spy = SpyStore({KEYED_SECRET: KEYED_VALUE})
        with spy_store(spy), self.assertRaises(proxy.ProxyError) as caught:
            proxy.render_runtime_config(home, environ=environ, policy="explicit")
        self.assertIn(catalog.KEYED_AUDIT_CLOSED, str(caught.exception))
        self.assertEqual(spy.touched(KEYED_SECRET), 0)
        self.assertFalse((proxy.config_dir(home) / "config.yaml").exists())
        # Start policy degrades the refused declaration and still never
        # resolves its credential (catalog direct secrets are other names).
        with spy_store(spy):
            _target, result, report = proxy.render_runtime_config(home, environ=environ, policy="start")
        self.assertEqual(spy.touched(KEYED_SECRET), 0)
        self.assertNotIn(KEYED_KEY, result.served)
        self.assertNotIn(KEYED_VALUE, result.yaml)
        self.assertTrue(any(f"providers.d/{KEYED_ID}.json not rendered" in note for note in report.operator))
        # Never a keyless downgrade: the provider is absent, not a LAN section.
        self.assertNotIn(f'"name": "{KEYED_ID}"', result.yaml)

    def test_catalog_keyed_shape_gate_precedes_store_access(self) -> None:
        spy = SpyStore({KEYED_SECRET: KEYED_VALUE})
        with spy_store(spy):
            for audited in (False, True):
                with self.subTest(audited=audited):
                    bundle = catalog.load_raw(FIXTURE_ROOT)
                    bundle["docs"]["providers"]["providers"]["chatco"] = t1_keyed_provider()
                    bundle["docs"]["gateway"][catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: audited}
                    errors = catalog.validate_catalog(bundle)
                    gate = [error for error in errors if error.startswith("providers.chatco:")]
                    self.assertEqual(bool(gate), not audited, errors)
                    if gate:
                        self.assertIn(catalog.KEYED_AUDIT_CLOSED, gate[0])
            # The flag is a literal true; any other value is closed.
            for value in (False, "true", 1, None):
                self.assertFalse(catalog.keyed_compat_audited({catalog.AUDITS_KEY: {catalog.KEYED_COMPAT_AUDIT: value}}))
            self.assertFalse(catalog.keyed_compat_audited({}))
            gateway = {catalog.AUDITS_KEY: {catalog.KEYED_COMPAT_AUDIT: True}, "gateway": {"base_url": "http://127.0.0.1:8317"}}
            bad = {
                "header auth": dict(t1_keyed_provider(), transport={
                    "kind": "direct-openai", "base_url": KEYED_BASE,
                    "auth": {"kind": "header", "header": "x-api-key", "secret_ref": f"env:{KEYED_SECRET}"}}),
                "wrong adapter": dict(t1_keyed_provider(), adapter="cliproxy-claude-compatible-v1"),
                "payload": dict(t1_keyed_provider(), payload_contracts=["output-config-high"]),
                "http": t1_keyed_provider(base_url="http://api.chatco.example/v1"),
                "platform": t1_keyed_provider(base_url="https://api.openai.com/v1"),
                "missing auth": dict(t1_keyed_provider(), transport={"kind": "direct-openai", "base_url": KEYED_BASE}),
            }
            for label, provider in bad.items():
                with self.subTest(label=label):
                    self.assertIsNotNone(catalog.keyed_compat_problem(provider, gateway))
            self.assertIsNone(catalog.keyed_compat_problem(t1_keyed_provider(), gateway))
            # A keyless LAN route is not keyed and never gated.
            lan = fixture_docs(audited=False)["providers"]["providers"]["llm-local"]
            self.assertFalse(catalog.is_keyed_compat(lan))
            self.assertIsNone(catalog.keyed_compat_problem(lan, {}))
            # A copied tree with the T1 shape refuses at load while closed.
            root = audited_root(self.tmp, audited=False)
            path = root / "catalog" / "providers.json"
            document = json.loads(path.read_text())
            document["providers"]["chatco"] = t1_keyed_provider()
            path.write_bytes(strict_json.pretty_file_bytes(document))
            with self.assertRaisesRegex(catalog.CatalogError, "audit gate is closed"):
                catalog.load_catalog(root)
        self.assertEqual(spy.calls, [])

    def test_closed_keyed_snapshot_never_gets_checks_or_scans_store(self) -> None:
        closed = audited_root(self.tmp, audited=False)
        environ, _home = self._home_env(closed)
        document = keyed_document(display=f"Chat {REJECTED_MARKER}")
        self._install(environ, {KEYED_ID: document})
        spy = SpyStore({KEYED_SECRET: KEYED_VALUE})
        docs = catalog.load_catalog(closed).docs
        with spy_store(spy):
            snapshot = proxy.operator_snapshot(environ, docs, {}, asset_root=closed)
            layer = snapshot.layer
            self.assertEqual(layer.providers, {})
            self.assertEqual(layer.lines, {})
            found = layer.problems_by_file[f"providers.d/{KEYED_ID}.json"]
            self.assertEqual([(p.code, p.subject) for p in found], [("kind-gated", catalog.KEYED_AUDIT_CLOSED)])
            # The conservative scrub of declared names is preserved.
            self.assertIn(KEYED_SECRET, layer.secret_names)
            # Validation of a proposed (still solely rejected) layer is lazy.
            read = snapshot.read
            called: list[int] = []
            proposed = operator.proposed_layer(docs, read, {KEYED_ID: raw(keyed_document(efforts=["max"], default="max"))},
                                               schemas=snapshot.schemas or op_tests._schemas(), ledger=None,
                                               secret_values=lambda: called.append(1) or frozenset())
            self.assertEqual(called, [])
            self.assertEqual(proposed.providers, {})
        self.assertEqual(spy.calls, [])
        self.assertFalse(operator.store_scan_needed(docs, {KEYED_ID: raw(document)}))
        for text in texts(layer):
            self.assertNotIn(REJECTED_MARKER, text)

    def test_mixed_valid_contribution_keeps_defensive_literal_scan(self) -> None:
        closed = audited_root(self.tmp, audited=False)
        environ, _home = self._home_env(closed)
        stored = "stored-literal-credential-1"
        acme = op_tests._fixture_files()["acme"]
        acme["lines"]["custom-acme-small"]["display"] = stored  # a stored-value literal
        self._install(environ, {KEYED_ID: keyed_document(display=f"Chat {REJECTED_MARKER}"), "acme": acme,
                                "beta": op_tests._fixture_files()["beta"]})
        spy = SpyStore({"ACME_API_KEY": stored})
        with spy_store(spy):
            snapshot = proxy.operator_snapshot(environ, catalog.load_catalog(closed).docs, {}, asset_root=closed)
        self.assertEqual(spy.count("scan_values"), 1)
        layer = snapshot.layer
        acme_problems = layer.problems_by_file.get("providers.d/acme.json", ())
        self.assertTrue(any(problem.code == "secret-literal" for problem in acme_problems))
        self.assertIn("beta", layer.providers)
        keyed = layer.problems_by_file[f"providers.d/{KEYED_ID}.json"]
        self.assertEqual([problem.code for problem in keyed], ["kind-gated"])
        for text in texts(layer):
            self.assertNotIn(REJECTED_MARKER, text)
            self.assertNotIn(stored, text)
        self.assertTrue(operator.store_scan_needed(catalog.load_catalog(closed).docs,
                                                   {KEYED_ID: raw(keyed_document()), "beta": raw(op_tests._fixture_files()["beta"])}))

    # -------------------------------------------------------------- endpoint
    def test_keyed_https_origin_and_api_base_policy(self) -> None:
        layer = layer_of({KEYED_ID: keyed_document(base_url="https://API.ChatCo.example:443/v1/chat/")})
        provider = layer.providers[KEYED_ID]
        self.assertEqual((provider.origin, provider.base_url),
                         ("https://api.chatco.example", "https://api.chatco.example/v1/chat"))
        self.assertEqual(provider.entry["transport"]["base_url"], "https://api.chatco.example/v1/chat")
        self.assertEqual(provider.entry["transport"]["auth"], {"kind": "bearer", "secret_ref": f"env:{KEYED_SECRET}"})
        self.assertEqual(provider.entry["adapter"], catalog.OPENAI_COMPAT_ADAPTER)
        self.assertEqual(provider.entry["transport"]["kind"], "direct-openai")
        self.assertEqual(provider.kind, operator.KEYED_KIND)
        refused = {
            "http": "http://api.chatco.example/v1",
            "userinfo": "https://user@api.chatco.example/v1",
            "query": "https://api.chatco.example/v1?x=1",
            "fragment": "https://api.chatco.example/v1#f",
            "metadata": "https://169.254.169.254/v1",
            "metadata host": "https://metadata.google.internal/v1",
            "gateway port": "https://127.0.0.1:8317/v1",
            "numeric": "https://0x7f.1/v1",
        }
        for label, url in refused.items():
            with self.subTest(label=label):
                found = layer_of({KEYED_ID: keyed_document(base_url=url)})
                self.assertNotIn(KEYED_ID, found.providers)
                self.assertTrue(any(p.code == "endpoint" for p in found.problems), texts(found))
        # A secret-bearing listing must stay on the approved origin.
        listing = layer_of({KEYED_ID: keyed_document(listing={
            "url": "https://other.chatco.example/v1/models", "shape": "openai", "auth": "provider"})})
        self.assertTrue(any(p.code == "listing-origin" for p in listing.problems))
        same = layer_of({KEYED_ID: keyed_document(listing={
            "url": "https://api.chatco.example/v1/models", "shape": "openai", "auth": "provider"})})
        self.assertIn(KEYED_ID, same.providers)
        # Path-only edits keep the bearer route digest but move the
        # definition (and the served identity).
        plain = layer_of({KEYED_ID: keyed_document()})
        moved = layer_of({KEYED_ID: keyed_document(base_url="https://api.chatco.example/v2")})
        self.assertEqual(moved.providers[KEYED_ID].route_digest, plain.providers[KEYED_ID].route_digest)
        self.assertNotEqual(moved.lines[KEYED_KEY].definition_digest, plain.lines[KEYED_KEY].definition_digest)

    def test_api_openai_com_refused_for_canonical_host_variants(self) -> None:
        for url in ("https://api.openai.com/v1", "https://API.OpenAI.COM/v1/", "https://api.openai.com:443/v1",
                    "https://api.openai.com", "https://api.openai.com/", "https://api.openai.com:8443/other/path"):
            with self.subTest(url=url):
                layer = layer_of({KEYED_ID: keyed_document(base_url=url)})
                self.assertNotIn(KEYED_ID, layer.providers)
                self.assertTrue(any(catalog.OPENAI_PLATFORM_REFUSAL in t for t in texts(layer)), texts(layer))
                gateway = {catalog.AUDITS_KEY: {catalog.KEYED_COMPAT_AUDIT: True}}
                self.assertEqual(catalog.keyed_compat_problem(t1_keyed_provider(base_url=url), gateway),
                                 catalog.OPENAI_PLATFORM_REFUSAL)
        # Lexical policy only: a different host that merely contains the name.
        allowed = layer_of({KEYED_ID: keyed_document(base_url="https://api.openai.com.chatco.example/v1")})
        self.assertIn(KEYED_ID, allowed.providers)
        # The Messages-based kinds keep their own (unchanged) rules.
        self.assertIsNone(catalog.platform_host_problem("https://api.chatco.example"))

    def test_keyed_headers_are_closed_static_and_redacted(self) -> None:
        ok = layer_of({KEYED_ID: keyed_document(headers={"x-client": "claude-multi"})})
        self.assertEqual(ok.providers[KEYED_ID].headers, {"X-Client": "claude-multi"})
        marker_name = f"X-{REJECTED_MARKER}"
        refused = {
            "authorization": {"Authorization": "fixed-value"},
            "other name": {marker_name: "fixed-value"},
            "title": {"X-Title": "fixed-value"},
            "host": {"Host": "api.chatco.example"},
            "case duplicate": {"X-Client": "one", "x-client": "two"},
            "dollar": {"X-Client": "$HOME"},
            "whitespace": {"X-Client": f" {REJECTED_MARKER}"},
            "control": {"X-Client": "a\u0001b"},
            "non-ascii": {"X-Client": "café"},
        }
        for label, headers in refused.items():
            with self.subTest(label=label):
                layer = layer_of({KEYED_ID: keyed_document(headers=headers)})
                self.assertNotIn(KEYED_ID, layer.providers)
                found = texts(layer)
                self.assertTrue(found)
                for text in found:
                    self.assertNotIn(REJECTED_MARKER, text)
                    self.assertNotIn("fixed-value", text)
                    self.assertNotIn("$HOME", text)
        secretish = layer_of({KEYED_ID: keyed_document(headers={"X-Client": "sk-" + "a" * 24})})
        self.assertNotIn(KEYED_ID, secretish.providers)
        self.assertTrue(all(p.code == "secret-literal" for p in secretish.problems))
        self.assertFalse(any("sk-" in t for t in texts(secretish)))

    def test_casefold_alias_and_normalized_provider_key_collisions(self) -> None:
        # Gateway key normalization: vendor == openai-compatible-vendor.
        twin = keyed_document(secret="TWIN_API_KEY", key="custom-twin-chat", wire="twin-chat-1",
                              base_url="https://api.twin.example/v1")
        layer = layer_of({KEYED_ID: keyed_document(), f"openai-compatible-{KEYED_ID}": twin})
        self.assertEqual(layer.providers, {})
        self.assertEqual(layer.lines, {})
        both = [p for p in layer.problems if p.code == "collision"]
        self.assertEqual(sorted(p.file for p in both),
                         [f"providers.d/{KEYED_ID}.json", f"providers.d/openai-compatible-{KEYED_ID}.json"])
        self.assertEqual(operator.compat_provider_key(KEYED_ID), f"openai-compatible-{KEYED_ID}")
        self.assertEqual(operator.compat_provider_key("openai-compatibility"), "openai-compatibility")
        # The normalized render sentinel key and a T1 compat key win.
        for pid in ("openai-compatible-claude-multi-render", "openai-compatible-llm-local"):
            with self.subTest(pid=pid):
                found = layer_of({pid: keyed_document()})
                self.assertNotIn(pid, found.providers)
                self.assertTrue(any(p.code == "collision" for p in found.problems), texts(found))
        # Case-insensitive effective selector bases (trusted catalog data
        # spelled in upper case still collides; the client suffix is removed).
        docs = fixture_docs(audited=True)
        lines = docs["models"]["models"]
        mixed = copy.deepcopy(next(entry for entry in lines.values() if isinstance(entry["efforts"], list)))
        mixed["selector"] = KEYED_KEY.upper() + "[1m]"
        lines["mixedcase"] = mixed
        found = layer_of({KEYED_ID: keyed_document()}, docs=docs)
        self.assertNotIn(KEYED_KEY, found.lines)
        self.assertTrue(any("collides with catalog line mixedcase" in t for t in texts(found)), texts(found))
        # Two schema-valid T2 lines with one selector base: both refused.
        other = keyed_document(secret="OTHER_API_KEY", wire="other-chat-1", base_url="https://api.other.example/v1")
        dup = layer_of({KEYED_ID: keyed_document(), "otherco": other})
        self.assertNotIn(KEYED_KEY, dup.lines)

    def test_no_payload_pool_proxy_or_model_option_escape_hatch(self) -> None:
        self.assertEqual(render.ADAPTER_PAYLOAD_CONTRACTS[catalog.OPENAI_COMPAT_ADAPTER], {})
        contracts = layer_of({KEYED_ID: keyed_document(payload_contracts=["output-config-high"])})
        self.assertNotIn(KEYED_ID, contracts.providers)
        self.assertTrue(any("takes no payload contracts" in t for t in texts(contracts)))
        provider_fields = {"proxy_url": "http://proxy.example:8080", "prefix": "x", "priority": 1,
                           "api_key_entries": [], "is_compat": True, "models": [], "weight": 2, "retry": 3,
                           "use_max_completion_tokens": True, "image": True, "payload": {}}
        for name, value in provider_fields.items():
            with self.subTest(provider_field=name):
                found = layer_of({KEYED_ID: keyed_document(**{name: value})})
                self.assertNotIn(KEYED_ID, found.providers)
        line_fields = {"force_mapping": True, "thinking": {"levels": ["high"]}, "is_compat": True,
                       "use_max_completion_tokens": True, "image": True, "modalities": ["text"],
                       "prompt_cache_key": "x", "payload": {}, "api_key": "x"}
        for name, value in line_fields.items():
            with self.subTest(line_field=name):
                document = keyed_document()
                document["lines"][KEYED_KEY][name] = value
                found = layer_of({KEYED_ID: document})
                self.assertNotIn(KEYED_KEY, found.lines)
        for auth in ({"kind": "header", "header": "x-api-key", "secret_ref": f"env:{KEYED_SECRET}"},
                     {"kind": "none"}):
            with self.subTest(auth=auth["kind"]):
                found = layer_of({KEYED_ID: keyed_document(auth=auth)})
                self.assertNotIn(KEYED_ID, found.providers)
        # A second credential cannot be authored anywhere (closed schema).
        self.assertNotIn(KEYED_ID, layer_of({KEYED_ID: keyed_document(secondary_secret_ref="env:B")}).providers)

    # -------------------------------------------------------------- levels
    def test_keyed_levels_exact_and_digest_sensitive(self) -> None:
        base = layer_of({KEYED_ID: keyed_document(efforts=["high", "low"], default="high")})
        line = base.lines[KEYED_KEY]
        self.assertEqual(line.core_entry["efforts"], ["low", "high"])
        self.assertEqual(catalog.keyed_compat_levels(line.core_entry), ("low", "high"))
        variants = {
            "set": keyed_document(efforts=["low", "high", "max"], default="high"),
            "default": keyed_document(efforts=["low", "high"], default="low"),
            "context": copy.deepcopy(keyed_document()),
        }
        variants["context"]["lines"][KEYED_KEY]["context"]["declared_tokens"] = 65536
        for label, document in variants.items():
            with self.subTest(label=label):
                other = layer_of({KEYED_ID: document}).lines[KEYED_KEY]
                self.assertNotEqual(other.definition_digest, line.definition_digest)
        for bad in (["ultracode"], ["minimal"], ["none"], ["auto"], ["high", "turbo"]):
            with self.subTest(bad=bad):
                found = layer_of({KEYED_ID: keyed_document(efforts=bad, default=bad[0])})
                self.assertNotIn(KEYED_KEY, found.lines)
        mapped = keyed_document()
        mapped["lines"][KEYED_KEY]["efforts"] = {"high": "reasoning-effort-high"}
        self.assertNotIn(KEYED_KEY, layer_of({KEYED_ID: mapped}).lines)
        not_member = layer_of({KEYED_ID: keyed_document(efforts=["low"], default="high")})
        self.assertNotIn(KEYED_KEY, not_member.lines)

    def test_keyed_levels_canonical_order_and_digest_stability(self) -> None:
        orders = (["max", "low", "high"], ["low", "high", "max"], ["high", "max", "low"])
        seen = [layer_of({KEYED_ID: keyed_document(efforts=order, default="high")}).lines[KEYED_KEY]
                for order in orders]
        self.assertEqual({line.definition_digest for line in seen}, {seen[0].definition_digest})
        self.assertEqual({json.dumps(line.core_entry, sort_keys=True) for line in seen},
                         {json.dumps(seen[0].core_entry, sort_keys=True)})
        self.assertEqual(seen[0].core_entry["efforts"], ["low", "high", "max"])
        self.assertEqual(catalog.KEYED_COMPAT_LEVELS, ("low", "medium", "high", "xhigh", "max"))
        metadata = layer_of({KEYED_ID: keyed_document(efforts=orders[0], default="high")}).metadata[KEYED_KEY]
        self.assertIn("thinking_levels", metadata["field_hashes"])

    # -------------------------------------------------------------- origins
    def test_keyed_t1_secret_origin_accounting(self) -> None:
        docs = fixture_docs(audited=True)
        docs["providers"]["providers"]["chatco-t1"] = t1_keyed_provider()
        bundle = catalog.load_raw(FIXTURE_ROOT)
        bundle["docs"]["providers"]["providers"]["chatco-t1"] = t1_keyed_provider()
        bundle["docs"]["gateway"][catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: True}
        self.assertEqual([e for e in catalog.validate_catalog(bundle) if "chatco" in e], [])
        rival = op_tests._fixture_files()["acme"]
        rival["provider"]["auth"]["secret_ref"] = f"env:{KEYED_SECRET}"
        layer = layer_of({"acme": rival}, docs=docs)
        self.assertNotIn("acme", layer.providers)
        self.assertTrue(any(p.code == "secret-origin" and "chatco-t1" in p.subject for p in layer.problems))
        same_origin = layer_of({KEYED_ID: keyed_document()}, docs=docs)
        self.assertIn(KEYED_ID, same_origin.providers)
        moved = layer_of({KEYED_ID: keyed_document(base_url="https://elsewhere.example/v1")}, docs=docs)
        self.assertNotIn(KEYED_ID, moved.providers)
        self.assertTrue(any(p.code == "secret-origin" for p in moved.problems))

    def test_output_reuses_the_overlay_bounds_and_provenance(self) -> None:
        def with_output(output: Any) -> operator.OperatorLayer:
            document = keyed_document()
            document["lines"][KEYED_KEY]["output"] = output
            return layer_of({KEYED_ID: document})

        top = with_output({"declared_tokens": 2097152, "source": "docs", "source_ref": "https://docs.chatco.example"})
        self.assertEqual(top.lines[KEYED_KEY].output_tokens, 2097152)
        self.assertEqual(with_output({"declared_tokens": 1, "source": "operator"}).lines[KEYED_KEY].output_tokens, 1)
        for bad in ({"declared_tokens": 0, "source": "operator"},
                    {"declared_tokens": 2097153, "source": "operator"},
                    {"declared_tokens": 2**63 - 1, "source": "operator"},
                    {"declared_tokens": True, "source": "operator"},
                    {"declared_tokens": None, "source": "operator"},
                    {"declared_tokens": 4096, "source": "measured"},
                    {"declared_tokens": 4096, "source": "docs", "source_ref": ""},
                    {"declared_tokens": 4096, "source": "docs", "source_ref": "x" * 257},
                    {"declared_tokens": 4096, "source": "docs"}):
            with self.subTest(output=bad):
                self.assertNotIn(KEYED_KEY, with_output(bad).lines)
        provenance = with_output({"declared_tokens": 4096, "source": "listing"})
        self.assertTrue(any(p.code == "provenance" for p in provenance.problems))

    def test_from_operator_draft_preserves_keyed_auth(self) -> None:
        environ, _home = self._home_env(FIXTURE_ROOT)
        self._install(environ, {KEYED_ID: keyed_document()})
        docs = fixture_docs(audited=True)
        with spy_store(SpyStore()):
            kind, payload, prerequisites, _notes = dev._operator_prefill(
                docs, environ, FIXTURE_ROOT, KEYED_KEY, "chatco-chat")
        self.assertEqual(kind, "provider")
        transport = payload["provider"]["transport"]
        self.assertEqual(transport["kind"], "direct-openai")
        self.assertEqual(transport["auth"], {"kind": "bearer", "secret_ref": f"env:{KEYED_SECRET}"})
        self.assertFalse(any("T1 projection needs review" in item for item in prerequisites), prerequisites)
        # The keyless LAN projection still resets to auth none.
        lan = op_tests._fixture_files()["lanbox"]
        self._install(environ, {"lanbox": lan})
        _kind, lan_payload, _pre, _notes = dev._operator_prefill(
            docs, environ, FIXTURE_ROOT, "custom-lan-model", "lan-model")
        self.assertEqual(lan_payload["provider"]["transport"]["auth"], {"kind": "none"})

    # -------------------------------------------------------------- CLI
    def test_cli_validate_add_edit_template_closed_keyed_touch_no_store(self) -> None:
        case = _ClosedCommandCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        environ = {"HOME": str(case.runtime.home)}
        directory = state.ensure_private_dir(operator.providers_dir(environ))
        document = keyed_document(display=f"Chat {REJECTED_MARKER}")
        state.atomic_write(directory / f"{KEYED_ID}.json", raw(document))
        candidate = case.root / f"{KEYED_ID}.json"
        candidate.write_bytes(raw(document))
        os.chmod(candidate, 0o600)
        spy = SpyStore({KEYED_SECRET: KEYED_VALUE})
        before = case.state_bytes()
        add = ["providers", "add", "gen", "--kind", operator.KEYED_KIND, "--base-url", KEYED_BASE,
               "--auth", "bearer", "--secret-ref", f"env:{KEYED_SECRET}", "--family", "gen"]
        edited = raw(keyed_document(efforts=["max"], default="max", display=f"Chat {REJECTED_MARKER}"))
        import claude_multi.cli.commands.providers as providers_cmd
        with spy_store(spy):
            for argv in (["providers", "validate"], ["providers", "validate", str(candidate)],
                         ["providers", "template", "--kind", operator.KEYED_KIND], add, [*add, "--declare-only"]):
                with self.subTest(argv=argv[:3]):
                    code, out, err = case.op(argv)
                    self.assertEqual(code, 1, out + err)
                    self.assertIn(catalog.KEYED_AUDIT_CLOSED, out + err)
                    self.assertNotIn(REJECTED_MARKER, out + err)
            kept = case.root / "kept.json"
            kept.write_bytes(edited)
            with mock.patch.object(providers_cmd, "run_editor", return_value=(edited, kept, None)):
                code, out, err = case.op(["providers", "edit", KEYED_ID], "y\n")
            self.assertEqual(code, 1, out + err)
            self.assertIn(catalog.KEYED_AUDIT_CLOSED, err)
            self.assertNotIn(REJECTED_MARKER, out + err)
            # A schema-valid but token-shaped candidate file
            # name is scrubbed from the early refusal (metadata only).
            token_name = "sk-" + "zqtokenshapedfilemarker" + "x"
            shaped = case.root / f"{token_name}.json"
            shaped.write_bytes(raw(document))
            os.chmod(shaped, 0o600)
            code, out, err = case.op(["providers", "validate", str(shaped)])
            self.assertEqual(code, 1, out + err)
            self.assertIn(catalog.KEYED_AUDIT_CLOSED, out + err)
            self.assertIn("providers.d/<redacted>.json", out + err)
            self.assertNotIn(token_name, out + err)
        self.assertEqual(spy.calls, [])
        self.assertEqual(case.state_bytes(), before)
        # Replacing the sole valid (anthropic-compatible)
        # declaration with a closed-keyed one is refused before any store read.
        valid = _ClosedCommandCase()
        valid.setUp()
        self.addCleanup(valid.doCleanups)
        environ = {"HOME": str(valid.runtime.home)}
        directory = state.ensure_private_dir(operator.providers_dir(environ))
        state.atomic_write(directory / "acme.json", raw(op_tests._fixture_files()["acme"]))
        self.assertIn("acme", valid.runtime.operator_snapshot().layer.providers)
        before = valid.state_bytes()
        replacement = raw(keyed_document(secret="ACME_API_KEY", display=f"Chat {REJECTED_MARKER}"))
        kept = valid.root / "kept-acme.json"
        kept.write_bytes(replacement)
        spy = SpyStore({"ACME_API_KEY": "acme-dummy-value", KEYED_SECRET: KEYED_VALUE})
        with spy_store(spy), mock.patch.object(providers_cmd, "run_editor", return_value=(replacement, kept, None)):
            code, out, err = valid.op(["providers", "edit", "acme"], "y\n")
        self.assertEqual(code, 1, out + err)
        self.assertIn(catalog.KEYED_AUDIT_CLOSED, err)
        self.assertIn("your edit is kept at", err)
        self.assertNotIn(REJECTED_MARKER, out + err)
        self.assertEqual(spy.calls, [])
        self.assertEqual(valid.state_bytes(), before)

    def test_cli_add_and_template_keyed_kind_follow_trusted_accessor(self) -> None:
        case = _AuditedCommandCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        code, out, err = case.op(["providers", "template", "--kind", operator.KEYED_KIND])
        self.assertEqual(code, 0, err)
        template = json.loads(out)
        self.assertEqual(template["provider"]["auth"]["kind"], "bearer")
        self.assertTrue(template["provider"]["auth"]["secret_ref"].startswith("env:"))
        self.assertNotIn("headers", template["provider"])
        candidate = case.root / "example.json"
        candidate.write_text(out)
        os.chmod(candidate, 0o600)
        code, report, err = case.op(["providers", "validate", str(candidate)])
        self.assertEqual(code, 0, report + err)
        self.assertIn("providers.d/example.json: valid (1 line(s))", report)
        code, out, err = case.op(["providers", "add", KEYED_ID, "--kind", operator.KEYED_KIND, "--base-url", KEYED_BASE,
                                  "--auth", "bearer", "--secret-ref", f"env:{KEYED_SECRET}", "--family", "chatco",
                                  "--declare-only"])
        self.assertEqual(code, 0, out + err)
        layer = case.runtime.operator_snapshot().layer
        self.assertEqual(layer.providers[KEYED_ID].kind, operator.KEYED_KIND)
        self.assertEqual(layer.route_status[KEYED_ID], "unapproved")
        # The keyed kind never falls back to another auth: a header route is refused.
        code, out, err = case.op(["providers", "add", "headerco", "--kind", operator.KEYED_KIND, "--base-url",
                                  "https://api.headerco.example/v1", "--auth", "header", "--header", "x-api-key",
                                  "--secret-ref", "env:HEADERCO_API_KEY", "--family", "headerco", "--declare-only"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("takes bearer auth", err)


# ================================================================== E1b
OTHER_ID = "otherco"
OTHER_SECRET = "OTHERCO_API_KEY"
OTHER_KEY = "custom-otherco-chat"
OTHER_VALUE = "otherco-dummy-credential-value"
OLD_KEY = "custom-chatco-old"
KEYED_RENDER_GOLDEN = GOLDENS_ROOT / "render" / "gateway-operator-compat.yaml"


def other_document() -> dict:
    return keyed_document(secret=OTHER_SECRET, key=OTHER_KEY, wire="otherco-chat-1",
                          base_url="https://llm.otherco.example/api/v1", efforts=["medium"], default="medium",
                          display="Other Co")


def keyed_files(*, headers: dict | None = None, efforts: Iterable[str] = ("high", "low", "max"),
                default: str = "high") -> dict[str, dict]:
    return {KEYED_ID: keyed_document(headers=headers, efforts=efforts, default=default), OTHER_ID: other_document(),
            "lanbox": op_tests._fixture_files()["lanbox"]}


def capture_record(layer: operator.OperatorLayer, pid: str, *, wire: str, levels: Any = ("medium",),
                   key: str = OLD_KEY) -> dict:
    record = {"provider": pid, "rd": layer.providers[pid].route_digest, "wire": wire, "proxy_contract": None,
              "display": "Chat Co old", "context_tokens": 65536, "source": f"operator:{key}",
              "since_catalog": 36, "overlay": None}
    if levels is not None:
        record["thinking_levels"] = list(levels)
    return record


def keyed_plan(files: dict[str, dict] | None = None, *, captures: dict[str, dict] | None = None,
               docs: dict | None = None) -> tuple[operator.RenderPlan, operator.OperatorLedger]:
    """Validated, route-approved keyed declarations through the real render plan."""

    docs = docs or fixture_docs(audited=True)
    files = files if files is not None else keyed_files()
    first = layer_of(files, docs=docs)
    routes = {pid: op_tests._route(provider) for pid, provider in first.providers.items()
              if provider.auth_kind != "none"}
    aliases = {alias: (capture_record(first, **spec) if "rd" not in spec else spec)
               for alias, spec in (captures or {}).items()}
    ledger = op_tests._ledger(routes=routes, aliases=aliases)
    layer = layer_of(files, docs=docs, ledger=ledger)
    return operator.render_plan(docs, layer, ledger), ledger


def dummy_resolver(values: dict[str, Any] | None = None, calls: list[str] | None = None):
    table = {KEYED_SECRET: KEYED_VALUE, OTHER_SECRET: OTHER_VALUE, **(values or {})}

    def resolve(name: str) -> Any:
        if calls is not None:
            calls.append(name)
        if name in table:
            return table[name]
        return f"dummy-{name.lower().replace('_', '-')}"

    return resolve


def render_plan_config(plan: operator.RenderPlan, resolve, continuity: dict | None = None) -> render.RenderResult:
    return render.render_config(
        plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/fixture/home"), gateway_tokens=("t" * 64,), resolve_secret=resolve,
        continuity=continuity or {}, captures=plan.captures, provider_headers=plan.headers,
        oauth_overlay=plan.overlay, quiet_providers=plan.quiet_providers,
    )


def compat_sections(result: render.RenderResult) -> dict[str, dict]:
    from claude_multi import served_plan

    document = served_plan.parse_restricted_yaml(result.yaml)
    return {section["name"]: section for section in document["openai-compatibility"]}


def keyed_render_golden_bytes() -> bytes:
    """The keyed render golden (bless.py writes it from this exact builder):
    two keyed providers (one with the closed X-Client header and a
    captured-only alias), the keyless LAN fixture provider, dummy values."""

    plan, _ledger = keyed_plan(keyed_files(headers={"X-Client": "claude-multi"}),
                               captures={"custom-chatco-old": {"pid": KEYED_ID, "wire": "chatco-old-1"}})
    resolve = lambda name: f"golden-dummy-{name.lower().replace('_', '-')}"  # noqa: E731
    return render_plan_config(plan, resolve).yaml.encode("utf-8")


class KeyedRenderTests(unittest.TestCase):
    """E1b: one usable key, the keyed model item, availability parity, captures."""

    def test_keyed_render_one_nonblank_key_and_per_model_force_mapping(self) -> None:
        plan, _ledger = keyed_plan(keyed_files(headers={"x-client": "claude-multi"}))
        result = render_plan_config(plan, dummy_resolver())
        sections = compat_sections(result)
        chat, other = sections[KEYED_ID], sections[OTHER_ID]
        self.assertEqual(list(chat), ["name", "base-url", "api-key-entries", "headers", "models"])
        self.assertEqual(list(other), ["name", "base-url", "api-key-entries", "models"])
        self.assertEqual(chat["base-url"], KEYED_BASE)
        self.assertEqual(chat["headers"], {"X-Client": "claude-multi"})
        for section, value in ((chat, KEYED_VALUE), (other, OTHER_VALUE)):
            entries = section["api-key-entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(list(entries[0]), ["api-key"])
            # Key/wire pairing: each section carries its own credential
            # (compared in memory as a boolean, never shown).
            self.assertTrue(entries[0]["api-key"] == value)
        model = chat["models"]
        self.assertEqual(len(model), 1)  # one list-shaped effort line -> one item
        item = model[0]
        self.assertEqual(list(item), ["name", "alias", "display-name", "max-context-length", "thinking",
                                      "force-mapping"])
        line = plan.layer.lines[KEYED_KEY].core_entry
        self.assertEqual((item["name"], item["alias"], item["display-name"]),
                         (KEYED_WIRE, KEYED_KEY, line["display"]))
        self.assertEqual(item["max-context-length"], line["context"]["provider_tokens"])
        self.assertEqual(item["thinking"], {"levels": ["low", "high", "max"]})
        self.assertIs(item["force-mapping"], True)
        self.assertEqual(other["models"][0]["thinking"], {"levels": ["medium"]})
        # Keyless LAN keeps its pinned 7.2.80 shape.
        lan = sections["lanbox"]
        self.assertEqual(list(lan), ["name", "base-url", "models"])
        self.assertEqual(list(lan["models"][0]), ["name", "alias", "display-name", "force-mapping"])
        # No payload rule for a keyed alias; static settings unchanged.
        document = __import__("claude_multi.served_plan", fromlist=["x"]).parse_restricted_yaml(result.yaml)
        payload_models = {m["name"] for kind in ("override", "filter") for entry in document["payload"][kind]
                          for m in entry["models"]}
        self.assertNotIn(KEYED_KEY, payload_models)
        static = plan.docs["gateway"]["gateway"]["cliproxy_static"]
        for key in ("plugins", "discovery", "commercial-mode", "debug", "request-retry", "proxy-url"):
            self.assertEqual(document[key], static[key])
        self.assertIn(KEYED_KEY, result.served)

    def test_missing_blank_or_invalid_secret_omits_current_and_captured_aliases(self) -> None:
        plan, _ledger = keyed_plan(captures={OLD_KEY: {"pid": KEYED_ID, "wire": "chatco-old-1"}})
        served = render_plan_config(plan, dummy_resolver()).served
        self.assertTrue({KEYED_KEY, OLD_KEY} <= served)
        for label, value in (("missing", None), ("blank", ""), ("spaces", "   "), ("inner space", "a b"),
                             ("newline", "value\n"), ("non-string", 12345), ("bytes", b"bytes-value")):
            with self.subTest(label=label):
                calls: list[str] = []
                result = render_plan_config(plan, dummy_resolver({KEYED_SECRET: value}, calls))
                self.assertEqual(calls.count(KEYED_SECRET), 1)  # resolved once per render plan
                self.assertFalse({KEYED_KEY, OLD_KEY} & result.served)
                sections = compat_sections(result)
                self.assertNotIn(KEYED_ID, sections)  # never a keyless section
                self.assertIn(OTHER_ID, sections)
                self.assertIn(KEYED_ID, {entry["provider"] for entry in result.unavailable})
                for section in sections.values():
                    for entry in section.get("api-key-entries", ()):
                        self.assertTrue(render.usable_secret(entry["api-key"]))
                self.assertNotIn(OLD_KEY, result.captures_rendered)

    def test_provider_selectors_matches_keyed_availability(self) -> None:
        plan, _ledger = keyed_plan()
        providers = plan.docs["providers"]["providers"]
        lines = plan.docs["models-v2"]["models"]
        for value in (KEYED_VALUE, None, "", "bad value"):
            with self.subTest(value=value):
                resolve = dummy_resolver({KEYED_SECRET: value})
                result = render_plan_config(plan, resolve)
                reason = render.provider_secret_reason(providers[KEYED_ID], resolve)
                expected = render.provider_selectors(KEYED_ID, providers[KEYED_ID], lines, available=reason is None,
                                                     continuity={}, providers=providers)
                section = compat_sections(result).get(KEYED_ID, {"models": []})
                self.assertEqual(expected, frozenset(item["alias"] for item in section["models"]))
                self.assertEqual(bool(expected), value == KEYED_VALUE)
        # The keyless LAN route is available by construction.
        lan = render.provider_selectors("lanbox", providers["lanbox"], lines, available=False, continuity={},
                                        providers=providers)
        self.assertTrue(lan)

    def test_unavailable_providers_matches_renderer_report(self) -> None:
        plan, _ledger = keyed_plan()
        providers = plan.docs["providers"]["providers"]
        for values in ({}, {KEYED_SECRET: None}, {KEYED_SECRET: ""}, {KEYED_SECRET: 7, OTHER_SECRET: None},
                       {OTHER_SECRET: "x y"}):
            with self.subTest(values=sorted(values)):
                resolve = dummy_resolver(values)
                result = render_plan_config(plan, resolve)
                self.assertEqual(render.unavailable_providers(providers, resolve_secret=resolve), result.unavailable)
                for entry in result.unavailable:
                    self.assertNotIn(KEYED_VALUE, entry["reason"])

    def test_capture_preserves_keyed_levels_before_config_publish(self) -> None:
        case = _KeyedHome()
        case.setUp()
        self.addCleanup(case.doCleanups)
        files = keyed_files(efforts=("max", "low"), default="low")
        case.install(files)
        case.write_ledger(routes=case.approve_keyed(files, KEYED_ID, OTHER_ID))
        order: list[str] = []
        original = state.atomic_write

        def write(path, data):
            order.append(Path(path).name)
            return original(path, data)

        with mock.patch.object(state, "atomic_write", side_effect=write):
            code, _out, err = case.init()
        self.assertEqual(code, 0, err)
        self.assertLess(order.index(operator.LEDGER_NAME), order.index("config.yaml"))
        captured = case.ledger().aliases[KEYED_KEY]
        self.assertEqual(captured["thinking_levels"], ["low", "max"])
        self.assertIsNone(captured["overlay"])  # alias-level levels, not an OAuth overlay
        self.assertEqual(case.ledger().aliases[OTHER_KEY]["thinking_levels"], ["medium"])
        self.assertIn(KEYED_KEY, case.aliases())

    def test_keyed_capture_failure_keeps_previous_config(self) -> None:
        case = _KeyedHome()
        case.setUp()
        self.addCleanup(case.doCleanups)
        files = keyed_files()
        case.install(files)
        case.write_ledger(routes=case.approve_keyed(files, KEYED_ID, OTHER_ID))
        self.assertEqual(case.init()[0], 0)
        before = (case.config / "config.yaml").read_bytes()
        case.install(keyed_files(efforts=("low",), default="low"))
        with mock.patch.object(operator, "write_ledger", side_effect=OSError(28, "No space left")), \
                self.assertRaisesRegex(proxy.ProxyError, "capture could not be committed"):
            case.init()
        self.assertEqual((case.config / "config.yaml").read_bytes(), before)
        self.assertEqual(case.ledger().aliases[KEYED_KEY]["thinking_levels"], ["low", "high", "max"])

    def test_current_definition_wins_and_captured_only_keeps_levels(self) -> None:
        plan, _ledger = keyed_plan(captures={
            KEYED_KEY: {"pid": KEYED_ID, "wire": KEYED_WIRE, "levels": ["max"], "key": KEYED_KEY},
            OLD_KEY: {"pid": KEYED_ID, "wire": "chatco-old-1", "levels": ["high", "medium"]},
        })
        self.assertNotIn(KEYED_KEY, plan.captures)  # the current definition wins its own capture
        self.assertEqual(plan.captures[OLD_KEY]["thinking_levels"], ["high", "medium"])
        models = {item["alias"]: item for item in compat_sections(render_plan_config(plan, dummy_resolver()))
                  [KEYED_ID]["models"]}
        self.assertEqual(models[KEYED_KEY]["thinking"], {"levels": ["low", "high", "max"]})
        self.assertEqual(models[OLD_KEY]["thinking"], {"levels": ["medium", "high"]})
        self.assertEqual(models[OLD_KEY]["max-context-length"], 65536)
        # A captured-only alias needs the same approved route: a changed
        # route identity never retargets it.
        docs = fixture_docs(audited=True)
        files = keyed_files()
        first = layer_of(files, docs=docs)
        stale = capture_record(first, KEYED_ID, wire="chatco-old-1")
        stale["rd"] = "0" * 64
        ledger = op_tests._ledger(routes={pid: op_tests._route(first.providers[pid]) for pid in (KEYED_ID, OTHER_ID)},
                                  aliases={OLD_KEY: stale})
        plan = operator.render_plan(docs, layer_of(files, docs=docs, ledger=ledger), ledger)
        self.assertNotIn(OLD_KEY, plan.captures)

    def test_keyed_capture_missing_or_invalid_levels_is_unservable(self) -> None:
        plan, _ledger = keyed_plan()
        base = {"provider": KEYED_ID, "wire": "chatco-old-1", "proxy_contract": None, "display": "Old",
                "context_tokens": 65536}
        for label, levels in (("missing", None), ("empty", []), ("outside", ["none"]), ("dup", ["high", "high"]),
                              ("auto", ["auto"]), ("string", "high")):
            with self.subTest(label=label):
                capture = dict(base) if levels is None else dict(base, thinking_levels=levels)
                result = render.render_config(
                    plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
                    home=Path("/fixture/home"), gateway_tokens=("t" * 64,), resolve_secret=dummy_resolver(),
                    continuity={}, captures={OLD_KEY: capture}, provider_headers=plan.headers)
                self.assertNotIn(OLD_KEY, result.served)
                self.assertTrue(any(OLD_KEY in note and "unservable" in note for note in result.notices))
        # The ledger itself refuses levels outside the keyed vocabulary.
        first = layer_of(keyed_files())
        record = capture_record(first, KEYED_ID, wire="chatco-old-1", levels=["minimal"])
        with self.assertRaises(operator.OperatorError):
            op_tests._ledger(aliases={OLD_KEY: record})
        # A non-keyed capture without levels is unchanged (no levels needed).
        acme = op_tests._fixture_files()
        acme_layer = op_tests._layer({"acme": acme["acme"]})
        plain = capture_record(acme_layer, "acme", wire="acme-old-1", levels=None)
        self.assertNotIn("thinking_levels", op_tests._ledger(aliases={"custom-acme-old": plain}).aliases["custom-acme-old"])

    def test_keyed_continuity_without_levels_is_unservable(self) -> None:
        plan, _ledger = keyed_plan()
        continuity = {
            "custom-chatco-retired": {"provider": KEYED_ID, "wire": "chatco-retired-1", "display": "Retired",
                                      "context_tokens": 65536, "proxy_contract": None},
            "custom-lan-retired": {"provider": "lanbox", "wire": "lan-retired-1", "display": "LAN retired",
                                   "context_tokens": 32768, "proxy_contract": None},
        }
        result = render_plan_config(plan, dummy_resolver(), continuity)
        self.assertNotIn("custom-chatco-retired", result.served)
        self.assertIn("custom-lan-retired", result.served)
        self.assertTrue(any("custom-chatco-retired" in note and "unservable" in note for note in result.notices))
        providers = plan.docs["providers"]["providers"]
        selectors = render.provider_selectors(KEYED_ID, providers[KEYED_ID], plan.docs["models-v2"]["models"],
                                              continuity=continuity, providers=providers)
        self.assertNotIn("custom-chatco-retired", selectors)

    def test_legacy_lan_and_empty_operator_render_unchanged(self) -> None:
        import test_render

        self.assertEqual(test_render._render().yaml.encode("utf-8"), test_render.GOLDEN.read_bytes())
        closed = fixture_docs(audited=False)
        opened = fixture_docs(audited=True)
        for docs in (closed, opened):
            empty = operator.render_plan(docs, operator.empty_layer(), None)
            result = render_plan_config(empty, dummy_resolver())
            self.assertNotIn("api-key-entries", result.yaml)
        self.assertEqual(render_plan_config(operator.render_plan(closed, operator.empty_layer(), None),
                                            dummy_resolver()).yaml,
                         render_plan_config(operator.render_plan(opened, operator.empty_layer(), None),
                                            dummy_resolver()).yaml)
        lan_only = {"lanbox": op_tests._fixture_files()["lanbox"]}
        for docs in (closed, opened):
            plan = operator.render_plan(docs, layer_of(lan_only, docs=docs), None)
            lan = compat_sections(render_plan_config(plan, dummy_resolver()))["lanbox"]
            self.assertEqual(list(lan), ["name", "base-url", "models"])
        # Catalog-direct emission is unchanged (#12 partial: new keyed only).
        direct = next(section for section in __import__("claude_multi.served_plan", fromlist=["x"])
                      .parse_restricted_yaml(test_render._render().yaml)["claude-api-key"])
        self.assertNotIn("thinking", direct["models"][0])
        self.assertIn("owned-by", direct["models"][0])

    def test_nonfile_store_and_all_declared_name_scrub(self) -> None:
        case = _KeyedHome()
        case.setUp()
        self.addCleanup(case.doCleanups)
        files = keyed_files()
        case.install(files)
        case.write_ledger(routes=case.approve_keyed(files, KEYED_ID, OTHER_ID))
        spy = SpyStore({KEYED_SECRET: KEYED_VALUE, OTHER_SECRET: 12345, "KIMI_CLAUDE_API_KEY": "kimi-dummy-value"})
        with spy_store(spy):
            _target, result, _report = proxy.render_runtime_config(case.home, environ=case.environ)
        self.assertIsNone(spy.path)
        self.assertEqual(spy.count("get", KEYED_SECRET), 1)
        self.assertIn(KEYED_KEY, result.served)
        self.assertNotIn(OTHER_KEY, result.served)  # a non-string fake value is unusable
        self.assertIn(OTHER_ID, {entry["provider"] for entry in result.unavailable})
        # Conservative scrub: every declared name, refused or unapproved included.
        docs = fixture_docs(audited=False)
        closed_layer = layer_of({KEYED_ID: keyed_document()}, docs=docs)
        unapproved = layer_of({OTHER_ID: other_document()})
        for layer, the_docs, name in ((closed_layer, docs, KEYED_SECRET),
                                      (unapproved, fixture_docs(audited=True), OTHER_SECRET)):
            merged = operator.merge_docs(the_docs, layer)
            self.assertIn(name, merged[operator.SECRET_NAMES_KEY])
            from claude_multi import profile

            self.assertIn(name, profile.LineupCatalog.from_docs(merged).extra_secret_names)

    def test_keyed_renderer_precondition_precedes_resolver(self) -> None:
        plan, _ledger = keyed_plan()
        providers = copy.deepcopy(plan.docs["providers"]["providers"])
        lines = plan.docs["models-v2"]["models"]
        closed_gateway = fixture_docs(audited=False)["gateway"]
        opened_gateway = plan.docs["gateway"]
        header_auth = copy.deepcopy(providers)
        header_auth[KEYED_ID]["transport"]["auth"] = {"kind": "header", "header": "x-api-key",
                                                      "secret_ref": f"env:{KEYED_SECRET}"}
        platform = copy.deepcopy(providers)
        platform[KEYED_ID]["transport"]["base_url"] = "https://api.openai.com/v1"
        for label, gateway, the_providers in (("closed", closed_gateway, providers),
                                              ("header auth", opened_gateway, header_auth),
                                              ("platform", opened_gateway, platform)):
            with self.subTest(label=label):
                calls: list[str] = []
                with self.assertRaises(render.RenderError):
                    render.build_config_document(
                        gateway, the_providers, lines, home=Path("/fixture/home"), gateway_tokens=("t" * 64,),
                        resolve_secret=dummy_resolver(calls=calls), continuity={})
                self.assertEqual(calls, [])

    def test_keyed_render_golden(self) -> None:
        from _golden import assertGolden

        data = keyed_render_golden_bytes()
        assertGolden(self, KEYED_RENDER_GOLDEN, data)
        text = data.decode("utf-8")
        self.assertIn("api-key-entries", text)
        self.assertIn('X-Client: "claude-multi"', text)
        self.assertIn("custom-chatco-old", text)


class _KeyedHome(KeyedHomeCase):
    """A KeyedHomeCase driven from another test (no test methods of its own)."""


# ================================================================== E1c
SECRET_MARKER = "keyed-secret-marker-zq9"
HEADER_MARKER = "hdr-value-zq1"
KEYED_CELLS_GOLDEN = GOLDENS_ROOT / "tui" / "keyed-provider-credentials-80x24.txt"


def install_keyed_state(environ: dict[str, str], files: dict[str, dict]) -> None:
    """Write providers.d declarations and approve their keyed routes (ledger)."""

    directory = state.ensure_private_dir(operator.providers_dir(environ))
    for name, document in files.items():
        state.atomic_write(directory / f"{name}.json", raw(document))
    layer = layer_of(files)
    document = operator.ledger_document(None)
    document["routes"] = {pid: op_tests._route(provider) for pid, provider in layer.providers.items()
                          if provider.auth_kind != "none"}
    path = operator.ledger_path(environ)
    state.ensure_private_dir(path.parent)
    state.atomic_write(path, strict_json.canonical_file_bytes(document))


def keyed_cells_golden(root: Path) -> str:
    """The Providers pane with keyed compat rows (key set, key missing) and
    the keyless LAN row (tests/_tui_render.py registers it)."""

    import _tui_fixture as fx
    import _tui_render as tr
    from claude_multi import cli, tui

    assets = audited_root(Path(root))
    with tr.golden_runtime(root, asset_root=assets, secrets=[*fx._secret_names(assets), KEYED_SECRET]) as runtime:
        install_keyed_state(runtime.gateway_environ(), keyed_files())
        journal = fx.journal_text(dead_pool=tr.first_oauth_pool(runtime))

        def screen():
            widget = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda: journal)
            widget.selected = [row.id for row in widget.rows].index(KEYED_ID)
            return widget

        return tr.render(screen)


@dataclasses.dataclass(frozen=True)
class _Prepared:
    lineup: Any
    secret_problems: tuple


def _binding(key: str, provider: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(binding=SimpleNamespace(key=key, provider=provider))


class KeyedServedCase(test_cli.OperatorCommandCase):
    """The fixture CLI harness on an audited asset root with keyed declarations."""

    def setUp(self) -> None:
        self._assets_tmp = Path(tempfile.mkdtemp(prefix="keyedtest-assets-")).resolve()
        self.addCleanup(shutil.rmtree, self._assets_tmp, True)
        self.assets = audited_root(self._assets_tmp)
        with mock.patch.object(test_cli, "CATALOG_ROOT", self.assets):
            super().setUp()

    def declare(self, files: dict[str, dict] | None = None, *, value: str | None = SECRET_MARKER) -> None:
        install_keyed_state(self.runtime.gateway_environ(), files or {
            KEYED_ID: keyed_document(headers={"X-Client": HEADER_MARKER}), "lanbox": op_tests._fixture_files()["lanbox"]})
        lines = [f"KIMI_CLAUDE_API_KEY=cli-test-dummy", "ACME_API_KEY=acme-dummy-value"]
        if value is not None:
            lines.append(f"{KEYED_SECRET}={value}")
        state.atomic_write(self.secret_file, ("\n".join(lines) + "\n").encode())

    def publish(self) -> str:
        code, out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, out + err)
        return out + err

    def config(self) -> Path:
        return proxy.config_dir(self.runtime.home) / "config.yaml"

    def record(self, stem: str, selector: str, *, event: str | None = None) -> Path:
        import test_served_plan

        directory = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        path = directory / f"{stem}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(
            test_served_plan.lenient_record(stem, selector, event=event)))
        return path

    def hand_edit(self, mutate) -> None:
        path = operator.providers_dir(self.env) / f"{KEYED_ID}.json"
        document = strict_json.loads(path.read_bytes())
        mutate(document)
        state.atomic_write(path, strict_json.pretty_file_bytes(document))

    def assertNoValues(self, *texts: str) -> None:
        for text in texts:
            self.assertNotIn(SECRET_MARKER, text)
            self.assertNotIn(HEADER_MARKER, text)


def _add_max(document: dict) -> None:
    line = document["lines"][KEYED_KEY]
    line["efforts"] = [*line["efforts"], "max"]


class KeyedAvailabilityTests(KeyedServedCase):
    """Secret readiness over the SecretStore, UI parity and the guarded key action."""

    def _resolved(self, *keys: str):
        from types import SimpleNamespace

        lead, *agents = keys
        return SimpleNamespace(lead=SimpleNamespace(model=lead),
                               variants=tuple(SimpleNamespace(model=key) for key in agents))

    def _lineup(self, lead: str, *agents: str):
        from types import SimpleNamespace

        return SimpleNamespace(lead=_binding(lead), agents={
            role: _binding(key) for role, key in zip(("cm-explorer", "cm-analyst"), agents)})

    def _direct_key(self) -> str:
        lines = self.runtime.catalog.docs["models"]["models"]
        return next(key for key, entry in sorted(lines.items()) if entry["provider"] == "kimi")

    def test_d28_refuses_keyed_compat_missing_secret(self) -> None:
        self.declare(value=None)
        docs = self.runtime.ordinary_docs
        models = docs["models"]["models"]
        providers = docs["providers"]["providers"]
        for value, expected in ((None, "missing from"), ("", "blank or invalid"), ("bad value", "blank or invalid"),
                                (12345, "blank or invalid"), (SECRET_MARKER, None)):
            with self.subTest(value=value):
                spy = SpyStore({} if value is None else {KEYED_SECRET: value})
                problems = proxy.selected_secret_problems(self._resolved(KEYED_KEY), models, providers, store=spy)
                if expected is None:
                    self.assertEqual(problems, [])
                else:
                    self.assertEqual(len(problems), 1)
                    self.assertIn(f"({KEYED_ID})", problems[0])
                    self.assertIn(expected, problems[0])
                for text in problems:
                    self.assertNotIn(SECRET_MARKER, text)
        # The default file store: its path-bearing texts are unchanged.
        problems = self.runtime.lineup_secret_problems(self._lineup(KEYED_KEY))
        self.assertEqual(len(problems), 1)
        self.assertIn(f"required variable {KEYED_SECRET} missing from the secret env file", problems[0])

    def test_d28_checks_every_bound_agent_with_nonfile_store(self) -> None:
        self.declare(value=None)
        direct = self._direct_key()
        anthropic = next(key for key, entry in sorted(self.runtime.catalog.docs["models"]["models"].items())
                         if entry["provider"] == "anthropic")
        spy = SpyStore({})
        with spy_store(spy):
            problems = self.runtime.lineup_secret_problems(self._lineup(anthropic, KEYED_KEY, direct))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any(f"({KEYED_ID})" in text for text in problems))
        self.assertTrue(any("(kimi)" in text for text in problems))
        self.assertTrue(all("fixture in-memory store" in text for text in problems))
        self.assertEqual(spy.count("get", KEYED_SECRET), 1)
        self.assertEqual(spy.count("is_set", "KIMI_CLAUDE_API_KEY"), 1)
        self.assertEqual(spy.count("scan_values"), spy.count("scan_values"))  # literal scan is separate
        spy.values.update({KEYED_SECRET: SECRET_MARKER, "KIMI_CLAUDE_API_KEY": "kimi-dummy"})
        with spy_store(spy):
            self.assertEqual(self.runtime.lineup_secret_problems(self._lineup(KEYED_KEY, direct)), [])
            # The lead is checked too (not only agents).
            spy.values.pop(KEYED_SECRET)
            self.assertEqual(len(self.runtime.lineup_secret_problems(self._lineup(KEYED_KEY))), 1)

    def test_keyed_credential_cells_golden(self) -> None:
        from _golden import assertGolden
        from claude_multi import views
        import _tui_render

        frame = _tui_render.build("keyed-provider-credentials")
        assertGolden(self, KEYED_CELLS_GOLDEN, frame.encode("utf-8"))
        self.assertIn("key set", frame)
        self.assertIn("key missing", frame)
        self.assertIn("keyless", frame)
        cell = views._credential_cell
        self.assertEqual(cell({"kind": "direct-openai", "auth": "bearer", "credential": "X present"}, None), "key set")
        self.assertEqual(cell({"kind": "direct-openai", "auth": "bearer", "credential": "X missing"}, None),
                         "key missing")
        self.assertEqual(cell({"kind": "direct-openai", "auth": "bearer", "credential": "X invalid"}, None),
                         "key invalid")
        self.assertEqual(cell({"kind": "direct-openai", "auth": "none", "credential": "keyless LAN"}, None), "keyless")
        self.assertEqual(cell({"kind": "direct-openai", "credential": "keyless LAN"}, None), "keyless")

    def test_keyed_provider_key_action_opens_the_key_modal(self) -> None:
        from claude_multi import cli, tui
        import claude_multi.cli.screens.providers as providers_screen
        from claude_multi.setup import texts
        from test_tui import FakeWindow

        self.declare(value=None)
        screen = cli._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        ids = [row.id for row in screen.rows]
        stored = providers_screen.Outcome("chatco API key saved (9 chars) — gateway reloaded")
        with mock.patch.object(providers_screen.ConnectActions, "set_key", return_value=stored) as set_key:
            screen.selected = ids.index(KEYED_ID)
            screen._key(FakeWindow([], height=24, width=80))
        set_key.assert_called_once_with(KEYED_ID)
        self.assertIn("API key saved", screen.message)
        # A keyless route has no key to set: the layer refuses before any modal.
        with mock.patch.object(tui, "Modal", side_effect=AssertionError("no key modal for a keyless route")):
            screen.selected = ids.index("lanbox")
            screen._key(FakeWindow([], height=24, width=80))
        self.assertIn(texts.KEY_NOT_NEEDED.format(display="lanbox").split(" needs")[1], screen.message)

    def test_direct_warning_and_detail_reserve_are_auth_aware(self) -> None:
        import argparse
        import claude_multi.cli.launch_flow as launch_flow
        import claude_multi.cli.screens.direct as direct_screen
        from claude_multi.cli import gateway_facts

        self.declare(value=None)
        unavailable = gateway_facts._ordinary_unavailable(self.runtime)
        self.assertEqual(unavailable[KEYED_ID], f"missing required secret env:{KEYED_SECRET}")
        self.assertNotIn("lanbox", unavailable)
        self.assertIn(f"set {KEYED_SECRET}", gateway_facts._connect_hint(self.runtime, KEYED_ID))
        self.assertIn("keyless LAN route", gateway_facts._connect_hint(self.runtime, "lanbox"))
        # The Direct detail reserve budgets the keyed reasons too.
        wrapped: list[str] = []
        real_wrap = direct_screen.textwrap.wrap
        with mock.patch.object(direct_screen.textwrap, "wrap",
                               side_effect=lambda text, width: wrapped.append(text) or real_wrap(text, width)):
            direct_screen._direct_detail_reserve(self.runtime, 80)
        import claude_multi.cli.text as cli_text
        display = self.runtime.lineup_catalog().providers[KEYED_ID].get("display", KEYED_ID)
        self.assertIn(cli_text.DIRECT_REASON_KEY.format(display=display), wrapped)
        self.assertIn(cli_text.DIRECT_REASON_KEY_INVALID.format(display=display), wrapped)
        # The ad-hoc direct warning names the keyed route and stays warn-only.
        prepared = _Prepared(lineup=SimpleNamespaceLead(KEYED_ID), secret_problems=("provider problem text",))
        args = argparse.Namespace(direct_resume=None, force=False, no_subagents=None, direct_continue=False,
                                  direct_model=KEYED_KEY, print_launch=False)
        output = io.StringIO()
        performed: list[Any] = []
        with mock.patch.object(self.runtime, "prepare", return_value=prepared), \
                mock.patch.object(self.runtime, "perform", side_effect=lambda p, **kw: performed.append(p) or 0), \
                mock.patch.object(launch_flow, "_skill_policy_notices", lambda prepared: None):
            code = launch_flow._direct_command(self.runtime, args, input_stream=io.StringIO(), output_stream=output,
                                               interactive=False, passthrough=[])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn(f"warning: missing required secret env:{KEYED_SECRET}", text)
        self.assertIn(f"fix: set {KEYED_SECRET}", text)
        self.assertIn("warning: provider problem text", text)
        self.assertEqual(performed[0].secret_problems, ())

    def test_keyed_readiness_selectors_and_doctor_agree(self) -> None:
        from claude_multi import readiness
        from claude_multi.cli import gateway_facts

        for value in (SECRET_MARKER, None):
            with self.subTest(present=value is not None):
                self.declare(value=value)
                facts = {fact["id"]: fact for fact in gateway_facts._provider_facts(self.runtime).facts}
                fact = facts[KEYED_ID]
                self.assertEqual(fact["auth"], "bearer")
                self.assertEqual(fact["credential"], f"{KEYED_SECRET} {'present' if value else 'missing'}")
                self.assertEqual(fact["expected"], 1 if value else 0)
                self.assertEqual(facts["lanbox"]["credential"], "keyless LAN")
                problems = self.runtime.lineup_secret_problems(self._lineup(KEYED_KEY))
                self.assertEqual(bool(problems), value is None)
                snapshot = gateway_facts._gateway_snapshot(self.runtime, self.runtime.gateway_token())
                self.assertEqual(KEYED_KEY in snapshot.expected, value is not None)
                provider = self.runtime.ordinary_docs["providers"]["providers"][KEYED_ID]
                self.assertFalse(readiness.is_lan(provider))
                binding = SimpleNamespaceBinding(KEYED_KEY, KEYED_ID)
                observed = readiness.Observations(
                    served=frozenset({KEYED_KEY}),
                    credential_problems={KEYED_ID: problems[0]} if problems else {})
                row = readiness.slot_readiness("cm-lead", binding, {KEYED_ID: provider}, observed)
                self.assertEqual(row.ready, value is not None)
                if problems:
                    self.assertTrue(row.first.authority)
                self.assertNoValues(*problems, json.dumps(fact))

    def test_keyed_captured_only_provider_facts_match_render(self) -> None:
        """A keyed provider whose last current line was
        removed keeps its captured alias; provider facts, the render plan and
        the doctor snapshot agree, with a usable and with a missing key."""

        from claude_multi.cli import gateway_facts

        for value in (SECRET_MARKER, None):
            with self.subTest(present=value is not None):
                self.declare(value=SECRET_MARKER)
                self.publish()
                self.hand_edit(lambda document: document.__setitem__("lines", {}))
                if value is None:
                    # Drop the credential only (the ledger keeps the capture).
                    state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\n")
                plan = self.runtime.operator_render_plan()
                self.assertIn(KEYED_KEY, plan.captures)
                self.assertFalse(any(line.provider_id == KEYED_ID for line in plan.layer.lines.values()))
                snapshot = gateway_facts._gateway_snapshot(self.runtime, self.runtime.gateway_token())
                self.assertEqual(KEYED_KEY in snapshot.expected, value is not None)
                fact = {item["id"]: item for item in gateway_facts._provider_facts(self.runtime).facts}[KEYED_ID]
                self.assertEqual(fact["selectors"], [KEYED_KEY] if value else [])
                self.assertEqual(fact["expected"], 1 if value else 0)
                self.assertEqual(set(fact["selectors"]), {s for s in snapshot.expected if s == KEYED_KEY})
                self.assertEqual(fact["credential"], f"{KEYED_SECRET} {'present' if value else 'missing'}")
                self.assertEqual(fact["models_note"], "configured · no models · 1 captured aliases")
                self.assertNoValues(json.dumps(fact))


class KeyedServedPlanTests(KeyedServedCase):
    """E1c: secret-free served-plan identity with keyed metadata."""

    def test_keyed_secret_absent_from_published_projection_plan_and_doctor(self) -> None:
        import claude_multi.cli.doctor as doctor_mod

        self.declare()
        preview = self.publish()
        self.assertNoValues(preview)
        config = self.config().read_text()
        self.assertIn(SECRET_MARKER, config)  # the private config needs it; nothing else may
        published = proxy.published_identity(self.runtime.home)
        self.assertIsNone(published.unknown)
        identity = published.routes[KEYED_KEY]
        self.assertEqual((identity.auth_header, identity.header_names), ("bearer", ("X-Client",)))
        self.assertNoValues(json.dumps({k: v.as_document() for k, v in published.routes.items()}))
        self.hand_edit(_add_max)
        plan = doctor_mod.pending_served_plan(self.runtime)
        self.assertIsNotNone(plan)
        self.assertNoValues(plan.text(), json.dumps(plan.as_document()), plan.doctor_fact() or "")
        for argv in (["plan"], ["plan", "--json"], ["doctor"], ["doctor", "--json"]):
            with self.subTest(argv=argv):
                code, out = self.run_cli(argv)
                self.assertNoValues(out)
        code, out = self.run_cli(["doctor", "--json"])
        document = json.loads(out)
        self.assertEqual(set(document), {"schema_version", "report", "generated_at", "status", "coverage", "facts",
                                         "diagnostics", "environment"})
        # The shared doctor producer (human Attention line) names the change.
        problems, info, attention = doctor_mod._doctor_served_checks(self.runtime, self.runtime.gateway_token())
        self.assertTrue(any(line.startswith("Pending served change") for line in attention), attention)
        self.assertNoValues(*problems, *info, *attention)

    def test_keyed_levels_only_change_is_live_protected_remap(self) -> None:
        import claude_multi.cli.doctor as doctor_mod
        from claude_multi import served_plan

        self.declare()
        self.publish()
        live = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        self.record(live, KEYED_KEY, event="start")
        self.hand_edit(_add_max)
        plan = doctor_mod.pending_served_plan(self.runtime)
        self.assertEqual([selector for selector, _o, _n in plan.retargeted], [KEYED_KEY])
        before, after = plan.retargeted[0][1:]
        self.assertEqual(before.changed_parts(after), ("contract",))
        self.assertEqual(plan.added, ())
        self.assertEqual(plan.removed, ())
        self.assertIn((live[:8], "lead", KEYED_KEY, "retargets"), plan.impacts)
        self.assertTrue(plan.destructive)
        self.assertIn("1 retargeted", plan.doctor_fact())
        self.assertIn("1 live session(s)", plan.doctor_fact())
        # Unknown coverage refuses the remap, exactly as any served retarget.
        published = proxy.published_identity(self.runtime.home)
        candidate = proxy.candidate_document(self.runtime.home, environ=self.runtime.gateway_environ())
        unknown = served_plan.build_plan(
            published.routes, served_plan.routes_from_document(candidate),
            references=served_plan.References({KEYED_KEY: frozenset({live})}, frozenset({live}),
                                              unknown="unreadable records cccccccc"),
            state_root="/state", gateway="g")
        self.assertIsNotNone(unknown.refusal())
        # The verb shows the retarget and its live impact in the preview.
        code, out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Retargeted", err)
        self.assertIn(f"{live[:8]} lead: {KEYED_KEY} retargets", err)

    def test_keyed_auth_headers_context_change_plan_identity(self) -> None:
        from claude_multi import served_plan

        def routes(files: dict[str, dict], values: dict | None = None) -> dict:
            plan, _ledger = keyed_plan(files)
            return served_plan.routes_from_document(
                served_plan.parse_restricted_yaml(render_plan_config(plan, dummy_resolver(values)).yaml))

        base = routes({KEYED_ID: keyed_document(headers={"X-Client": "one"}), "lanbox": op_tests._fixture_files()["lanbox"]})
        identity = base[KEYED_KEY]
        self.assertEqual((identity.section, identity.auth_header, identity.header_names),
                         (served_plan.SECTION_COMPAT, "bearer", ("X-Client",)))
        self.assertTrue(identity.contract.startswith("contract "))
        lan = base["custom-lan-model"]
        self.assertEqual((lan.auth_header, lan.header_names, lan.contract), (None, (), ""))
        self.assertIn("(bearer)", identity.label())
        same = {
            "header value": routes({KEYED_ID: keyed_document(headers={"X-Client": "two"})}),
            "key value": routes({KEYED_ID: keyed_document(headers={"X-Client": "one"})},
                                {KEYED_SECRET: "another-dummy-value"}),
        }
        for label, other in same.items():
            with self.subTest(same=label):
                self.assertEqual(other[KEYED_KEY], identity)
                plan = served_plan.build_plan(base, {**base, **other}, references=served_plan.References({}, frozenset()),
                                              state_root="/s", gateway="g")
                self.assertFalse(plan.served_changed)
        context = keyed_document(headers={"X-Client": "one"})
        context["lines"][KEYED_KEY]["context"]["declared_tokens"] = 65536
        changed = {
            "no header": (routes({KEYED_ID: keyed_document()}), "route"),
            "levels": (routes({KEYED_ID: keyed_document(headers={"X-Client": "one"}, efforts=["low"],
                                                        default="low")}), "contract"),
            "context": (routes({KEYED_ID: context}), "contract"),
        }
        for label, (other, part) in changed.items():
            with self.subTest(changed=label):
                self.assertIn(part, identity.changed_parts(other[KEYED_KEY]))
        # Keyed versus keyless auth mode on an otherwise identical section.
        plan, _ledger = keyed_plan({KEYED_ID: keyed_document()})
        document = served_plan.parse_restricted_yaml(render_plan_config(plan, dummy_resolver()).yaml)
        for section in document["openai-compatibility"]:
            if section["name"] == KEYED_ID:
                del section["api-key-entries"]
        keyless = served_plan.routes_from_document(document)[KEYED_KEY]
        self.assertIsNone(keyless.auth_header)
        self.assertIn("route", routes({KEYED_ID: keyed_document()})[KEYED_KEY].changed_parts(keyless))

    def test_keyed_malformed_published_config_is_fixed_unknown(self) -> None:
        self.declare()
        self.publish()
        text = self.config().read_text()
        broken = text.replace("    api-key-entries:\n      - api-key: ", "    api-key-entries: [{api-key: ", 1)
        self.assertNotEqual(broken, text)
        state.atomic_write(self.config(), broken.encode("utf-8"))
        published = proxy.published_identity(self.runtime.home)
        self.assertIsNone(published.routes)
        self.assertEqual(published.unknown, "published gateway config is not in the rendered form")
        code, out, err = self.op(["providers", "apply"])
        self.assertIn("Published render: unknown — published gateway config is not in the rendered form", err)
        self.assertNoValues(out, err)
        code, out = self.run_cli(["plan", "--json"])
        self.assertNoValues(out)

    def test_keyed_capture_barrier_cas_and_root_checks_preserved(self) -> None:
        import claude_multi.cli.commands.providers as providers_cmd
        from claude_multi import continuity, served_plan

        self.declare()
        barriers: list[Any] = []
        order: list[str] = []
        real_render = proxy.render_runtime_config
        real_write = state.atomic_write

        def render_spy(*args, **kwargs):
            barriers.append(kwargs.get("barrier"))
            return real_render(*args, **kwargs)

        def write_spy(path, data):
            order.append(Path(path).name)
            return real_write(path, data)

        with mock.patch.object(proxy, "render_runtime_config", side_effect=render_spy), \
                mock.patch.object(state, "atomic_write", side_effect=write_spy):
            self.publish()
        self.assertTrue(barriers and all(barrier is not None for barrier in barriers))
        self.assertLess(order.index(operator.LEDGER_NAME), order.index("config.yaml"))
        self.assertEqual(self.ledger().aliases[KEYED_KEY]["thinking_levels"], ["low", "high"])
        # CAS: a change after the shown plan refuses with nothing applied.
        self.hand_edit(_add_max)
        before = self.state_bytes()
        real_preflight = providers_cmd.served_preflight

        def preflight_then_change(runtime, verb, **kwargs):
            planned = real_preflight(runtime, verb, **kwargs)
            self.hand_edit(lambda document: document["lines"][KEYED_KEY].__setitem__("display", "Changed"))
            return planned

        with mock.patch.object(providers_cmd, "served_preflight", side_effect=preflight_then_change):
            code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 1)
        self.assertIn(served_plan.CHANGED_REFUSAL, err)
        after = self.state_bytes()
        changed_file = str(operator.providers_dir(self.env) / f"{KEYED_ID}.json")
        self.assertEqual({k: v for k, v in after.items() if k != changed_file},
                         {k: v for k, v in before.items() if k != changed_file})
        # Root authority: another managed root refuses before any write.
        other = str(self.root / "other-state")
        document = continuity.read(self.runtime.home)
        document["state_root"] = other
        continuity.write(self.runtime.home, document)
        before = self.state_bytes()
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 1)
        self.assertIn(served_plan.ROOT_REFUSAL.format(managed=other, requested=self.runtime.session_store.root), err)
        self.assertEqual(self.state_bytes(), before)
        self.assertNoValues(err)


class SimpleNamespaceLead:
    """A prepared lineup whose lead is bound on ``provider``."""

    def __init__(self, provider: str) -> None:
        self.lead = _binding("", provider)


def SimpleNamespaceBinding(key: str, provider: str):  # noqa: N802 - a tiny readiness binding
    from types import SimpleNamespace

    return SimpleNamespace(key=key, provider=provider, selector=key)


class _ClosedCommandCase(test_cli.OperatorCommandCase):
    """The fixture CLI harness with an explicitly closed audit."""

    def setUp(self) -> None:
        self._assets_tmp = Path(tempfile.mkdtemp(prefix="keyedtest-closed-assets-")).resolve()
        self.addCleanup(shutil.rmtree, self._assets_tmp, True)
        assets = audited_root(self._assets_tmp, audited=False)
        with mock.patch.object(test_cli, "CATALOG_ROOT", assets):
            super().setUp()


class _AuditedCommandCase(test_cli.OperatorCommandCase):
    """The fixture CLI harness on a temporary asset root with the audit open."""

    def setUp(self) -> None:
        self._assets_tmp = Path(tempfile.mkdtemp(prefix="keyedtest-assets-")).resolve()
        self.addCleanup(shutil.rmtree, self._assets_tmp, True)
        assets = audited_root(self._assets_tmp)
        with mock.patch.object(test_cli, "CATALOG_ROOT", assets):
            super().setUp()





class KeyedAuthorityTests(KeyedServedCase):
    def test_enabling_audit_creates_no_operator_or_admission_state(self):
        self.runtime.catalog.docs["gateway"]["audits"] = {"openai_compat_keyed": False}
        before = self.state_bytes()
        self.runtime.catalog.docs["gateway"]["audits"]["openai_compat_keyed"] = True
        snapshot = self.runtime.operator_snapshot()
        self.assertFalse(snapshot.layer.providers)
        self.assertFalse(snapshot.layer.lines)
        self.assertIsNone(snapshot.ledger)
        self.assertFalse(self.runtime.current_effective().admitted_lines)
        self.assertIsNone(operator.load_evidence(self.runtime.gateway_environ(), snapshot.schemas))
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.http_calls, [])

    def prepare_keyed(self, *, agents=False):
        document = keyed_document()
        if agents:
            line = document["lines"][KEYED_KEY]
            line.update(capabilities=["lead", "agents"], roles=["cm-reviewer"], family="unknown")
            line["context"]["declared_tokens"] = 200000
        self.declare({KEYED_ID: document})
        self.publish()
        self.serve_current()

    def test_keyed_admission_preflight_fails_before_secret_or_network(self):
        """The audit wrapper keeps this name; request authority belongs to qualify."""
        from claude_multi.cli.commands import models as commands

        self.declare({KEYED_ID: keyed_document()})
        approved = self.ledger_file.read_bytes()
        ledger = operator.ledger_document(None)
        state.atomic_write(self.ledger_file, strict_json.canonical_file_bytes(ledger))
        spy = SpyStore({KEYED_SECRET: KEYED_VALUE})
        # Unapproved and changed credential destinations refuse the explicit
        # diagnostic before even the defensive scan of unrelated keys.
        for route in ("unapproved", "changed"):
            if route == "changed":
                state.atomic_write(self.ledger_file, approved)
                self.hand_edit(lambda doc: doc["provider"].update(base_url="https://changed.chatco.example/v1"))
            before = self.state_bytes()
            with spy_store(spy), mock.patch.object(commands, "render_identity", side_effect=AssertionError("network")):
                code, out, err = self.op(["models", "qualify", KEYED_KEY, "--smoke"], "y\n")
            self.assertNotEqual(code, 0, out + err)
            self.assertIn(f"route {route}", err)
            self.assertEqual(spy.calls, [])
            self.assertEqual(self.state_bytes(), before)
        self.runtime.catalog.docs["gateway"].pop("audits", None)
        # A closed security audit makes the declaration invalid for either
        # metadata admission or a diagnostic; it must not read credentials.
        for command in (["models", "admit", KEYED_KEY], ["models", "qualify", KEYED_KEY, "--smoke"]):
            before = self.state_bytes()
            with spy_store(spy), mock.patch.object(commands, "render_identity", side_effect=AssertionError("network")):
                code, out, err = self.op(command, "y\n")
            self.assertNotEqual(code, 0, out + err)
            self.assertIn(catalog.KEYED_AUDIT_CLOSED, err)
            self.assertEqual(spy.calls, [])
            self.assertEqual(self.state_bytes(), before)
        self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_keyed_optional_smoke_preserves_failure_reasons(self):
        from claude_multi import qualify

        self.prepare_keyed()
        for response, verdict, reason in (
            (qualify.HttpResult(429, b"{}"), "inconclusive", "rate-limited"),
            (qualify.HttpResult(503, b"{}"), "inconclusive", "upstream-error"),
            (qualify.HttpResult(None, b"", "connection"), "inconclusive", "connection"),
            (qualify.HttpResult(200, b"", "oversize"), "failed", "oversize"),
        ):
            with self.subTest(reason=reason), mock.patch.object(
                    self.runtime, "qualify_post", return_value=response) as post:
                code, out, err = self.op(["models", "qualify", KEYED_KEY, "--smoke"], "y\n")
                self.assertNotEqual(code, 0, out + err)
                post.assert_called_once()
                self.assertIn(f"smoke {KEYED_KEY}: {verdict}", out)
                self.assertNotIn("degenerate", out)
                evidence = operator.load_evidence(self.runtime.gateway_environ(), operator.load_schemas(self.assets))
                record = evidence.lines[KEYED_KEY]["checks"]["smoke"]
                self.assertEqual((record["result"], record["http"], record["reason"]),
                                 (verdict, response.status, reason))
                self.assertEqual(record["contracts"], self.runtime.contract_identity().as_document())
                self.assertNotIn(KEYED_KEY, self.ledger().admissions)

    def test_keyed_admission_race_rejects_stale_render_evidence(self):
        """The audit wrapper keeps this name; only explicit qualification sends."""
        self.prepare_keyed()
        # Sentinel remains but one alias disappears during the actual request.
        self.during_smoke = lambda: self.served.discard(KEYED_KEY)
        code, out, err = self.op(["models", "qualify", KEYED_KEY, "--smoke"], "y\n")
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(len(self.http_calls), 1)
        self.assertNotIn(KEYED_KEY, self.ledger().admissions)
        evidence = operator.load_evidence(self.runtime.gateway_environ(), operator.load_schemas(self.assets))
        self.assertTrue(evidence is None or KEYED_KEY not in evidence.lines)
        self.during_smoke = None
        self.serve_current()
        # Alias remains but the current sentinel disappears: equally insufficient.
        from claude_multi.cli.commands import models as commands
        sentinel, _, problem = commands.render_identity(self.runtime)
        self.assertFalse(problem)
        self.during_smoke = lambda: self.served.discard(sentinel)
        code, out, err = self.op(["models", "qualify", KEYED_KEY, "--agents"], "y\n")
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(len(self.http_calls), 2)  # stops before effort request
        self.assertNotIn(KEYED_KEY, self.ledger().admissions)
        evidence = operator.load_evidence(self.runtime.gateway_environ(), operator.load_schemas(self.assets))
        self.assertTrue(evidence is None or KEYED_KEY not in evidence.lines)

    def test_keyed_admit_revoke_are_local_badges_despite_failed_evidence(self):
        from claude_multi import qualify
        from claude_multi.cli.commands import models as commands
        from claude_multi.cli.commands import providers as providers_cmd

        self.prepare_keyed()
        with mock.patch.object(self.runtime, "qualify_post", return_value=qualify.HttpResult(200, b"", "oversize")):
            code, out, err = self.op(["models", "qualify", KEYED_KEY, "--smoke"], "y\n")
        self.assertEqual(code, 1, out + err)
        evidence_path = operator.evidence_path(self.runtime.gateway_environ())
        before_evidence = evidence_path.read_bytes()
        # Admission neither approves this route nor requires a usable provider,
        # key, served alias or current render. The stored failed verdict stays.
        state.atomic_write(self.ledger_file, strict_json.canonical_file_bytes(operator.ledger_document(None)))
        state.atomic_write(self.secret_file, b"")
        self.runtime.settings_store.set_provider_enabled(KEYED_ID, False, catalog=self.runtime.lineup_catalog())
        self.served.clear()
        before_config = self.config().read_bytes()
        with mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("badge inferred")), \
                mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("badge smoked")), \
                mock.patch.object(self.runtime, "render_gateway", side_effect=AssertionError("badge rendered")), \
                mock.patch.object(commands, "render_identity", side_effect=AssertionError("badge observed gateway")), \
                mock.patch.object(providers_cmd, "served_preflight", side_effect=AssertionError("badge planned render")):
            code, out, err = self.op(["models", "admit", KEYED_KEY], "y\n")
            self.assertEqual(code, 0, out + err)
            self.assertIn(KEYED_KEY, self.ledger().admissions)
            self.assertIn(KEYED_KEY, self.runtime.current_effective().admitted_lines)
            self.assertFalse(self.ledger().routes)
            self.assertEqual(evidence_path.read_bytes(), before_evidence)
            code, out, err = self.op(["models", "revoke", KEYED_KEY, "--yes"])
            self.assertEqual(code, 0, out + err)
        self.assertNotIn(KEYED_KEY, self.ledger().admissions)
        self.assertNotIn(KEYED_KEY, self.runtime.current_effective().admitted_lines)
        self.assertFalse(self.runtime.settings_store.load()["providers"][KEYED_ID]["enabled"])
        self.assertEqual(evidence_path.read_bytes(), before_evidence)
        self.assertEqual(self.config().read_bytes(), before_config)
        self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_keyed_qualification_writes_evidence_not_grants(self):
        self.prepare_keyed()
        before = self.ledger_file.read_bytes()
        declaration = (self.pdir / f"{KEYED_ID}.json").read_bytes()
        code, out, err = self.op(["models", "qualify", KEYED_KEY, "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ledger_file.read_bytes(), before)
        self.assertEqual((self.pdir / f"{KEYED_ID}.json").read_bytes(), declaration)
        self.assertNotIn(KEYED_KEY, self.runtime.current_effective().admitted_lines)
        evidence = operator.load_evidence(self.runtime.gateway_environ(), operator.load_schemas(self.assets))
        self.assertIn(KEYED_KEY, evidence.lines)
        self.assertEqual(len(self.http_calls), 6)
        first = json.loads(self.http_calls[0][1])
        self.assertEqual(first["thinking"], {"type": "adaptive"})
        self.assertEqual(first["output_config"], {"effort": "high"})

    def test_keyed_audit_preserves_platform_refusal_and_keyless_lan_rules(self):
        for audited in (False, True):
            platform = layer_of({KEYED_ID: keyed_document(base_url="https://API.OPENAI.COM:443/v1")},
                                audited=audited)
            self.assertNotIn(KEYED_ID, platform.providers)
            lan = copy.deepcopy(op_tests._fixture_files()["lanbox"])
            for line in lan["lines"].values():
                line.update(capabilities=["lead", "agents"], roles=["cm-reviewer"])
            layer = layer_of({"lanbox": lan}, audited=audited)
            self.assertEqual(layer.problems, ())
            self.assertEqual(layer.route_status["lanbox"], "keyless")
            for key in lan["lines"]:
                self.assertTrue(operator.operator_line_offered(key, layer=layer, provider_enabled=True))
            lan["provider"]["auth"] = {"kind": "bearer", "secret_ref": "env:LAN_FIXTURE_API_KEY"}
            self.assertNotIn("lanbox", layer_of({"lanbox": lan}, audited=audited).providers)

    def facts(self):
        from claude_multi import profile
        snapshot = self.runtime.operator_snapshot()
        fields = operator.agent_fact_fields(KEYED_KEY, layer=snapshot.layer, ledger=snapshot.ledger,
            evidence=operator.load_evidence(self.runtime.gateway_environ(), snapshot.schemas), admitted_lines=(KEYED_KEY,),
            provider_enabled=True, contracts=self.runtime.contract_identity().as_document(),
            docs=self.runtime.catalog.docs, trusted_docs=self.runtime.catalog.docs)
        return snapshot.layer.lines[KEYED_KEY].core_entry, profile.AgentFacts(**fields)

    def eligibility(self, entry, facts, **kw):
        from claude_multi import profile
        defaults = dict(key=KEYED_KEY, slot="cm-reviewer", effort="high", facts=facts,
                        agent_efforts=catalog.KEYED_COMPAT_LEVELS)
        defaults.update(kw)
        return profile.agent_eligibility(entry, **defaults)

    def test_keyed_contract_stale_attention_and_record_authority(self):
        self.prepare_keyed(agents=True)
        code, out, err = self.op(["models", "admit", KEYED_KEY], "y\n")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.op(["models", "qualify", KEYED_KEY, "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        entry, facts = self.facts()
        self.assertTrue(self.eligibility(entry, facts).eligible, self.eligibility(entry, facts).reasons)
        stale = dataclasses.replace(facts, evidence="contract-stale")
        verdict = self.eligibility(entry, stale)
        self.assertTrue(verdict.eligible)
        self.assertEqual(verdict.evidence, "contract-stale")
        self.assertTrue(any(w.code == "qualification" and "predates" in w.message for w in verdict.warnings))
        for evidence in ("missing", "definition-stale", "failed"):
            diagnostic = dataclasses.replace(facts, evidence=evidence, admitted=False, evidence_gaps=("tools",))
            for mode, recorded in (("current", False), ("record", True), ("record", False)):
                with self.subTest(evidence=evidence, mode=mode, recorded=recorded):
                    verdict = self.eligibility(entry, diagnostic, mode=mode, recorded=recorded)
                    self.assertTrue(verdict.eligible, verdict.reasons)
                    self.assertEqual(verdict.evidence, evidence)
                    self.assertTrue({"admission", "qualification"} <= {w.code for w in verdict.warnings})
                    if evidence == "failed":
                        self.assertTrue(any(w.code == "qualification" and "failed tools" in w.message
                                            for w in verdict.warnings))

    def test_keyed_workflow_warns_without_forced_variant(self):
        from claude_multi import profile
        self.prepare_keyed(agents=True)
        code, out, err = self.op(["models", "admit", KEYED_KEY], "y\n")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.op(["models", "qualify", KEYED_KEY, "--agents", "--tool-choice", "auto"], "y\n")
        self.assertEqual(code, 0, out + err)
        entry, facts = self.facts()
        self.assertTrue(self.eligibility(entry, facts).eligible, self.eligibility(entry, facts).reasons)
        verdict = self.eligibility(entry, facts, slot=None, use=profile.WORKFLOW_USE)
        self.assertTrue(verdict.eligible, verdict.reasons)
        self.assertEqual(facts.tools_variants, frozenset({"auto"}))
        self.assertTrue(any(w.code == "tool-evidence" and "forced" in w.message for w in verdict.warnings))
        code, out, err = self.op(["models", "qualify", KEYED_KEY, "--tools", "--tool-choice", "forced"], "y\n")
        self.assertEqual(code, 0, out + err)
        entry, facts = self.facts()
        verdict = self.eligibility(entry, facts, slot=None, use=profile.WORKFLOW_USE)
        self.assertTrue(verdict.eligible, verdict.reasons)
        self.assertEqual(facts.tools_variants, frozenset({"auto", "forced"}))
        self.assertNotIn("tool-evidence", {w.code for w in verdict.warnings})

    def test_keyed_agents_route_eligible_only_with_trusted_flag(self):
        self.prepare_keyed(agents=True)
        entry, facts = self.facts()
        self.assertTrue(facts.d60)
        verdict = self.eligibility(entry, facts)
        self.assertTrue(verdict.eligible, verdict.reasons)  # approved route; no badge or qualification needed
        self.assertTrue({"admission", "qualification"} <= {w.code for w in verdict.warnings})
        for route in ("unapproved", "changed"):
            refused = self.eligibility(entry, dataclasses.replace(facts, route=route))
            self.assertFalse(refused.eligible)
            self.assertTrue(any(route in reason for reason in refused.reasons))
        provider = self.runtime.operator_snapshot().layer.providers[KEYED_ID].entry
        self.assertIsNone(operator.agent_route_kind(provider, gateway=self.runtime.catalog.docs["gateway"]))
        self.assertIsNotNone(operator.agent_route_kind(provider, gateway={}))
        self.runtime.catalog.docs["gateway"].pop("audits", None)
        spy = SpyStore()
        with spy_store(spy):
            snapshot = self.runtime.operator_snapshot()
        self.assertNotIn(KEYED_KEY, snapshot.layer.lines)
        self.assertEqual(spy.calls, [])
        self.assertNotIn(catalog.OPENAI_COMPAT_ADAPTER, operator.RETENTION_AUDITED_ADAPTERS)


if __name__ == "__main__":
    unittest.main()
