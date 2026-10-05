"""Tests for the pure deterministic CLIProxyAPI renderer."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, continuity, management, proxy, render, state, validate
from claude_multi.render import RenderError
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _layout import PATCH_DIR
from _golden import assertGolden


CATALOG_ROOT = FIXTURE_ROOT
GOLDEN = GOLDENS_ROOT / "render" / "gateway-default.yaml"


def _seed(root=None):
    """The fixture continuity seed alias map."""

    return continuity.seed_only(catalog.load_catalog(root or CATALOG_ROOT))["aliases"]


def _render(resolve_secret=None, models=None, providers=None, continuity_aliases=None):
    """The golden render: v2 lines plus the continuity seed.

    ``bless.py`` writes ``render/gateway-default.yaml`` through this helper,
    so test and golden share inputs.
    """

    bundle = catalog.load_catalog(CATALOG_ROOT)
    return render.render_config(
        bundle.docs["gateway"],
        providers or bundle.docs["providers"]["providers"],
        models or bundle.lines,
        home=Path("/home/test"),
        gateway_token="a" * 64,
        resolve_secret=resolve_secret or (lambda name: "dummy-kimi-key"),
        continuity=_seed() if continuity_aliases is None else continuity_aliases,
    )


def _raw_with_models(models):
    """The fixture raw catalog with the v2 line map ``models`` swapped in.

    Validation and render both read raw v2 lines.
    """

    raw = catalog.load_raw(CATALOG_ROOT)
    raw["docs"]["models"]["models"] = models
    return raw


class SentinelTests(unittest.TestCase):
    def document(self, tokens=("a" * 64,)):
        bundle = catalog.load_catalog(CATALOG_ROOT)
        return render.build_config_document(
            bundle.docs["gateway"], bundle.providers, bundle.lines,
            home=Path("/home/test"), gateway_tokens=tokens,
            resolve_secret=lambda _: "dummy-kimi-key", continuity=_seed(),
        )[0]

    def test_last_unique_sentinel_hashes_sentinelless_emit(self):
        import hashlib

        document = self.document()
        without = copy.deepcopy(document)
        sentinel = without["openai-compatibility"].pop()
        digest = hashlib.sha256(render.emit_yaml(without).encode()).hexdigest()[:8]
        self.assertEqual(sentinel, {
            "name": "claude-multi-render", "base-url": "http://127.0.0.1:9/v1",
            "models": [{"name": "claude-multi-render",
                        "alias": render.SENTINEL_PREFIX + digest, "force-mapping": True}],
        })
        self.assertEqual(_render().sentinel, render.SENTINEL_PREFIX + digest)
        self.assertEqual(_render().yaml.count('alias: "' + render.SENTINEL_PREFIX), 1)
        with self.assertRaises(RenderError):
            render.finalize_document(document)  # input must be sentinel-less
        self.assertEqual(document, self.document())

    def test_sentinel_excluded_from_selector_sets(self):
        document = self.document()
        self.assertFalse(any(s.startswith(render.SENTINEL_PREFIX)
                             for s in render.rendered_selectors(document)))
        bundle = catalog.load_catalog(CATALOG_ROOT)
        for name, provider in bundle.providers.items():
            self.assertFalse(any(s.startswith(render.SENTINEL_PREFIX)
                                 for s in render.provider_selectors(
                                     name, provider, bundle.lines, continuity=_seed())))

    def test_provider_selectors_defensively_exclude_reserved_prefix(self):
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.lines)
        for model in models.values():
            if isinstance(model["efforts"], list):
                model["selector"] = render.SENTINEL_PREFIX + "01234567"
            else:
                for spec in model["efforts"].values():
                    spec["selector"] = render.SENTINEL_PREFIX + "01234567"
        hostile = {render.SENTINEL_PREFIX + "89abcdef": dict(next(iter(_seed().values())))}
        for name, provider in bundle.providers.items():
            self.assertFalse(any(s.startswith(render.SENTINEL_PREFIX)
                                 for s in render.provider_selectors(
                                     name, provider, models, continuity=hostile)))

    def test_ordered_keys_change_the_sentinel(self):
        old, new = "a" * 64, "b" * 64
        docs = [self.document(keys) for keys in ((old,), (old, new), (new,), (new, old))]
        aliases = {d["openai-compatibility"][-1]["models"][0]["alias"] for d in docs}
        self.assertEqual(len(aliases), 4)
        self.assertEqual(docs[1]["api-keys"], [old, new])

    def test_reserved_provider_names_refused_without_dropping_routes(self):
        bundle = catalog.load_catalog(CATALOG_ROOT)
        for name in ("claude-multi-render", render.SENTINEL_PREFIX + "custom"):
            with self.subTest(name=name):
                providers = copy.deepcopy(bundle.providers)
                # Admission refuses even credentialed/unavailable providers,
                # not just rendered keyless sections.
                providers[name] = copy.deepcopy(next(iter(providers.values())))
                with self.assertRaisesRegex(RenderError, "reserved"):
                    render.build_config_document(
                        bundle.docs["gateway"], providers, bundle.lines,
                        home=Path("/home/test"), gateway_tokens=("a" * 64,),
                        resolve_secret=lambda _: None, continuity={},
                    )
                document = {"openai-compatibility": [{"name": name, "models": []}]}
                before = copy.deepcopy(document)
                with self.assertRaisesRegex(RenderError, "reserved"):
                    render.finalize_document(document)
                self.assertEqual(document, before)

    def test_empty_keys_refused(self):
        for keys in ((), ("",), (" ",), (None,), "a" * 64, b"a" * 64):
            with self.subTest(keys=keys), self.assertRaises(RenderError):
                self.document(keys)


class OutboundProxyPolicyTests(unittest.TestCase):
    """The outbound proxy policy: credential-free proxy URLs only,
    value-free diagnostics; gateway.json ``proxy-url`` goes through it."""

    SECRET = "proxy-dummy-secret-value"

    def test_accepted_and_refused_shapes(self):
        for url in ("", "http://127.0.0.1:3128", "https://proxy.example", "socks5://10.0.0.2:1080",
                    "socks5h://proxy.lan:1080/", "HTTP://Proxy.Example:8080"):
            with self.subTest(url=url):
                self.assertIsNone(render.outbound_proxy_problem(url))
        for url, needle in (
                (f"http://user:{self.SECRET}@proxy.example:3128", "userinfo"),
                (f"http://{self.SECRET}@proxy.example", "userinfo"),
                ("ftp://proxy.example", "scheme"),
                ("proxy.example:3128", "scheme"),
                ("http://", "no host"),
                (f"http://proxy.example/{self.SECRET}", "path, query or fragment"),
                (f"http://proxy.example?token={self.SECRET}", "path, query or fragment"),
                (f"http://proxy.example#{self.SECRET}", "path, query or fragment"),
                ("http://proxy.example:99999", "parse"),
                (f"http://proxy.example/ {self.SECRET}", "control"),
                (None, "string")):
            with self.subTest(url=url):
                problem = render.outbound_proxy_problem(url)
                self.assertIsNotNone(problem)
                self.assertIn(needle, problem)
                self.assertNotIn(self.SECRET, problem)

    def test_gateway_proxy_url_is_validated_at_render(self):
        bundle = catalog.load_catalog(CATALOG_ROOT)
        gateway = copy.deepcopy(bundle.docs["gateway"])
        gateway["gateway"]["cliproxy_static"]["proxy-url"] = f"http://user:{self.SECRET}@proxy.example:3128"
        with self.assertRaises(RenderError) as caught:
            render.build_config_document(
                gateway, bundle.providers, bundle.lines, home=Path("/home/test"), gateway_tokens=("a" * 64,),
                resolve_secret=lambda _: "dummy-kimi-key", continuity=_seed(),
            )
        self.assertIn("gateway.json proxy-url refused", str(caught.exception))
        self.assertNotIn(self.SECRET, str(caught.exception))
        gateway["gateway"]["cliproxy_static"]["proxy-url"] = "http://127.0.0.1:3128"
        document = render.build_config_document(
            gateway, bundle.providers, bundle.lines, home=Path("/home/test"), gateway_tokens=("a" * 64,),
            resolve_secret=lambda _: "dummy-kimi-key", continuity=_seed(),
        )[0]
        self.assertEqual(document["proxy-url"], "http://127.0.0.1:3128")


class GoldenTests(unittest.TestCase):
    def test_default_render_matches_golden(self) -> None:
        result = _render()
        assertGolden(self, GOLDEN, result.yaml.encode("utf-8"))
        self.assertTrue(result.yaml.endswith("\n"))

    def test_shipped_catalog_renders_every_lane_alias(self) -> None:
        # Goldens render the frozen fixture; this invariant
        # keeps one render of the SHIPPED catalog in the suite (no bytes
        # pinned, so model additions stay isolated): it renders without a
        # RenderError and serves every shipped lane alias.
        # v2 lines plus the shipped continuity seed; every live
        # selector base and every continuity alias is served.
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        seed = continuity.seed_only(bundle)["aliases"]
        # externalized-provider retirements are quiet by design.
        quiet = frozenset(catalog.externalized_providers(bundle.docs))
        result = render.render_config(
            bundle.docs["gateway"],
            bundle.docs["providers"]["providers"],
            bundle.lines,
            home=Path("/home/test"),
            gateway_token="a" * 64,
            resolve_secret=lambda name: "dummy-secret",
            continuity=seed,
            quiet_providers=quiet,
        )
        for model_id, model in bundle.lines.items():
            for level, selector, _contract in catalog.line_selectors(model):
                base = selector.removesuffix("[1m]")
                with self.subTest(line=f"{model_id}.{level}"):
                    self.assertIn(f'"{base}"', result.yaml)
        self.assertTrue(seed)
        for alias, entry in seed.items():
            with self.subTest(continuity=alias):
                if entry["provider"] in quiet:
                    self.assertNotIn(f'alias: "{alias}"', result.yaml)
                else:
                    self.assertIn(f'alias: "{alias}"', result.yaml)
        self.assertEqual(result.notices, ())

    def test_render_deterministic(self) -> None:
        self.assertEqual(_render().yaml, _render().yaml)

    def test_v1_static_settings_preserved(self) -> None:
        yaml = _render().yaml
        for needle in (
            'host: "127.0.0.1"',
            "port: 8317",
            "tls:",
            "request-retry: 0",
            "disable-cooling: true",
            'strategy: "fill-first"',
            "session-affinity: true",
            "ws-auth: true",
            "disable-control-panel: true",
        ):
            self.assertIn(needle, yaml)

    def test_lan_discovery_advertisement_pinned_off(self) -> None:
        # mDNS / DNS-SD advertisement is stated off in
        # every rendered config, and the gateway schema admits only false.
        document = render.build_config_document(
            catalog.load_catalog(CATALOG_ROOT).docs["gateway"],
            catalog.load_catalog(CATALOG_ROOT).providers,
            catalog.load_catalog(CATALOG_ROOT).lines,
            home=Path("/home/test"), gateway_tokens=("a" * 64,),
            resolve_secret=lambda _: "dummy-kimi-key", continuity=_seed(),
        )[0]
        self.assertEqual(document["discovery"], {"enabled": False})
        self.assertIn("discovery:\n  enabled: false\n", _render().yaml)
        for root in (CATALOG_ROOT, SHIPPED_ROOT):
            raw = catalog.load_raw(root)
            static = raw["docs"]["gateway"]["gateway"]["cliproxy_static"]
            self.assertEqual(static["discovery"], {"enabled": False})
            from claude_multi import validate

            for bad in ({"enabled": True}, {}, {"enabled": False, "service-name": "x"}):
                with self.subTest(root=str(root), bad=bad):
                    gateway = copy.deepcopy(raw["docs"]["gateway"])
                    gateway["gateway"]["cliproxy_static"]["discovery"] = bad
                    self.assertTrue(
                        validate.validate(gateway, raw["schemas"]["gateway"], "$")
                    )

    def test_retained_and_removed_aliases(self) -> None:
        yaml = _render().yaml
        for retained in (
            "claude-multi-kimi-k3",
            "claude-multi-opus-4-8",
            "gpt-multi-sol-high",
            "gpt-multi-sol-xhigh",
            "gpt-multi-gpt55-high",
            # the fixture continuity seed (retired muse-spark, meta)
            "claude-multi-muse-spark-high",
            "claude-multi-muse-spark-xhigh",
        ):
            self.assertIn(retained, yaml)
        for removed in (
            "claude-multi-fable-5",
            "claude-multi-sol-",
            "claude-multi-gpt55",
            "conserve-",
        ):
            self.assertNotIn(removed, yaml)

    def test_kimi_metadata_and_contracts(self) -> None:
        yaml = _render().yaml
        self.assertIn('"output_config.effort": "max"', yaml)
        self.assertIn('- "thinking"', yaml)
        self.assertIn('"reasoning.effort": "high"', yaml)
        self.assertIn('"reasoning.effort": "xhigh"', yaml)
        self.assertIn('owned-by: "moonshot"', yaml)
        self.assertIn("context-length: 1000000", yaml)
        self.assertIn('auth-header: "x-api-key"', yaml)


class ManagementRenderPinTests(unittest.TestCase):
    BLOCK = (
        'remote-management:\n'
        '  allow-remote: false\n'
        '  secret-key: ""\n'
        '  disable-control-panel: true\n'
        '  disable-auto-update-panel: true\n'
    )

    def test_fixture_and_shipped_security_pins(self):
        for root in (FIXTURE_ROOT, SHIPPED_ROOT):
            with self.subTest(root=str(root)):
                bundle = catalog.load_catalog(root)
                yaml = render.render_config(
                    bundle.docs["gateway"], bundle.providers, bundle.lines,
                    home=Path("/home/test"), gateway_token="a" * 64,
                    resolve_secret=lambda _: "dummy-secret",
                    continuity=continuity.seed_only(bundle)["aliases"],
                ).yaml
                self.assertIn(self.BLOCK, yaml)
                self.assertIn("usage-statistics-enabled: false\n", yaml)
                self.assertEqual(yaml.count('secret-key: ""'), 1)
                for prefix in ("$2a$", "$2b$", "$2y$"):
                    self.assertNotIn(prefix, yaml)
        self.assertIn(self.BLOCK, _render().yaml)

    def test_schema_rejects_each_unsafe_management_value(self):
        mutations = (
            ("allow-remote", True), ("secret-key", "x"),
            ("disable-control-panel", False), ("disable-auto-update-panel", False),
            ("usage-statistics-enabled", True),
        )
        for root in (FIXTURE_ROOT, SHIPPED_ROOT):
            raw = catalog.load_raw(root)
            schema = raw["schemas"]["gateway"]
            self.assertEqual(validate.validate(raw["docs"]["gateway"], schema, "$"), [])
            for field, value in mutations:
                with self.subTest(root=str(root), field=field):
                    doc = copy.deepcopy(raw["docs"]["gateway"])
                    static = doc["gateway"]["cliproxy_static"]
                    target = static if field == "usage-statistics-enabled" else static["remote-management"]
                    target[field] = value
                    errors = validate.validate(doc, schema, "$")
                    self.assertTrue(errors)
                    self.assertTrue(any(field in error and "must equal" in error for error in errors), errors)

    def test_runtime_render_never_reads_any_management_slot(self):
        with tempfile.TemporaryDirectory(prefix="cm-management-render-") as tmp:
            home = Path(tmp)
            env = {"HOME": str(home), "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}

            def rendered():
                path, result, _ = proxy.render_runtime_config(
                    home, environ=env, resolver=lambda _: "dummy-secret",
                )
                self.assertEqual(path.read_bytes(), result.yaml.encode())
                return path.read_bytes()

            before = rendered()
            directory = management.key_dir(home)
            slots = (management.KEY_FILE, management.STAGED_FILE,
                     management.DISABLED_FILE, management.PREPARED_FILE,
                     management.KEY_FILE + ".lock")
            sentinels = {name: (f"management-render-sentinel-{name}\n").encode() for name in slots}
            original_read = state.read_private

            def read_private(path, *args, **kwargs):
                self.assertNotIn(Path(path).name, slots, "render read management state")
                return original_read(path, *args, **kwargs)

            with mock.patch.object(state, "read_private", side_effect=read_private):
                for name, value in sentinels.items():
                    with self.subTest(slot=name):
                        state.atomic_write(directory / name, value)
                        self.assertEqual(rendered(), before)
                        (directory / name).unlink()
                # All slots/markers together (including unusable contents)
                # remain irrelevant to rendering and its sentinel.
                for name, value in sentinels.items():
                    state.atomic_write(directory / name, value)
                self.assertEqual(rendered(), before)
            for name, value in sentinels.items():
                self.assertEqual((directory / name).read_bytes(), value)
                self.assertNotIn(value.strip(), before)


class ForkRuleTests(unittest.TestCase):
    def test_validated_fork_routes_render_true(self) -> None:
        yaml = _render().yaml
        self.assertIn("fork: true", yaml)
        # codex pool has no fork policy: aliases render fork: false
        self.assertIn("fork: false", yaml)

    def test_mutated_fork_false_renders_false(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        route = providers["anthropic"]["passthrough_routes"][0]
        route["fork"] = False
        yaml = _render(providers=providers).yaml
        route_block = yaml.split(f'alias: "{route["name"]}"\n')[1].split("- name:")[0]
        self.assertIn("fork: false", route_block)


class CustomRegistryRenderTests(unittest.TestCase):
    """Custom providers/models render as ordinary direct routes."""

    def _merged(self):
        from claude_multi import custom

        registry = {
            "version": 1,
            "providers": {
                "my-lab": {
                    "base_url": "https://lab.example.com/apps/anthropic",
                    "auth_kind": "bearer",
                    "secret_env": "MY_LAB_API_KEY",
                }
            },
            "models": {
                "lab-model": {
                    "wire_model": "lab-1",
                    "provider": "my-lab",
                    "context_tokens": 262144,
                    "created_via": "manual",
                }
            },
        }
        bundle = catalog.load_catalog(CATALOG_ROOT)
        return custom.merge_docs(bundle.docs, registry)

    def test_custom_entry_minimum_tested_is_the_gateway_baseline(self) -> None:
        # A custom model is first served by the gateway the catalog
        # pins, so its synthetic evidence names that baseline — never a
        # stale literal.
        docs = self._merged()
        baseline = docs["gateway"]["gateway"]["cliproxyapi_baseline"]
        self.assertEqual(
            docs["models"]["models"]["lab-model"]["minimum_tested"]["cliproxyapi"],
            baseline,
        )

    def test_custom_provider_and_model_render(self) -> None:
        docs = self._merged()
        yaml = _render(
            models=docs["models-v2"]["models"],
            providers=docs["providers"]["providers"],
        ).yaml
        self.assertIn('name: "lab-1"', yaml)
        self.assertIn('alias: "custom-lab-model"', yaml)
        self.assertIn("https://lab.example.com/apps/anthropic", yaml)
        self.assertIn("context-length: 262144", yaml)

    def test_rendered_selectors_cover_customs(self) -> None:
        docs = self._merged()
        result = _render(
            models=docs["models-v2"]["models"],
            providers=docs["providers"]["providers"],
        )
        document, _avail, _unavail, _info = render.build_config_document(
            docs["gateway"],
            docs["providers"]["providers"],
            docs["models-v2"]["models"],
            home=Path("/home/test"),
            gateway_token="a" * 64,
            resolve_secret=lambda name: "dummy",
            continuity={},
        )
        selectors = render.rendered_selectors(document)
        self.assertIn("custom-lab-model", selectors)
        self.assertIn("claude-multi-kimi-k3", selectors)


class DirectProviderLaneTests(unittest.TestCase):
    def test_every_lane_selector_rendered_for_direct_provider(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.lines)
        models["kimi-k3"]["efforts"]["high"] = {
            "selector": "claude-multi-kimi-k3-turbo[1m]",
            "proxy_contract": "output-config-max",
        }
        yaml = _render(models=models).yaml
        self.assertIn('alias: "claude-multi-kimi-k3"', yaml)
        self.assertIn('alias: "claude-multi-kimi-k3-turbo"', yaml)

    def test_kimi_output_single_lane_unchanged(self) -> None:
        yaml = _render().yaml
        self.assertEqual(yaml.count('alias: "claude-multi-kimi-k3"'), 1)

    def test_qwen_1m_client_suffix_is_stripped_from_exact_wire_mapping(self) -> None:
        yaml = _render().yaml
        block = yaml.split('name: "qwen3.8-max"', 1)[1]
        self.assertIn('alias: "claude-multi-qwen38-max"', block)
        self.assertNotIn('alias: "claude-multi-qwen38-max[1m]"', block)
        self.assertIn("context-length: 983616", block)

    def test_glm52_alias_and_max_reasoning_override_rendered(self) -> None:
        # The shipped glm52 line is catalog data; the render behaviour it
        # guarded (a second model on the same direct provider whose [1m] lane
        # declares reasoning-effort-max renders a suffix-stripped alias, the
        # provider context and family, and its own max override) is pinned
        # on a test-local sibling of the fixture's direct qwen38 line.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        lines = copy.deepcopy(bundle.lines)
        sibling = copy.deepcopy(lines["qwen38"])
        sibling["wire_model"] = "fixture-max-wire"
        sibling["display"] = "Fixture Max Sibling"
        sibling["context"]["provider_tokens"] = 1000000
        sibling["context"]["declared_tokens"] = 1000000
        sibling["efforts"] = {
            "max": {
                "selector": "claude-multi-fixture-max[1m]",
                "proxy_contract": "reasoning-effort-max",
            }
        }
        sibling["default_effort"] = "max"
        lines["fixture-max"] = sibling
        provider = bundle.providers[sibling["provider"]]
        self.assertIn("reasoning-effort-max", provider["payload_contracts"])
        raw = _raw_with_models(lines)
        self.assertEqual(catalog.validate_catalog(raw), [])
        yaml = _render(models=lines).yaml
        block = yaml.split('name: "fixture-max-wire"', 1)[1]
        self.assertIn('alias: "claude-multi-fixture-max"', block)
        self.assertNotIn('alias: "claude-multi-fixture-max[1m]"', block)
        self.assertIn("context-length: 1000000", block)
        self.assertIn(f'owned-by: "{provider["independence_family"]}"', block)
        override = (
            '- models:\n'
            '        - name: "claude-multi-fixture-max"\n'
            '          protocol: "claude"\n'
            '      params:\n'
            '        reasoning_effort: "max"'
        )
        self.assertIn(override, yaml)


class SecretBoundaryTests(unittest.TestCase):
    def test_missing_secret_omits_provider_atomically(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = bundle.docs["providers"]["providers"]
        models = bundle.lines
        direct = {
            provider_id: provider
            for provider_id, provider in providers.items()
            if provider["transport"]["kind"] == "direct"
        }
        secret_names = {
            provider["transport"]["auth"]["secret_ref"].removeprefix("env:")
            for provider in direct.values()
        }
        # The fixture must exercise both sides of the boundary.
        self.assertTrue(direct)
        self.assertTrue(set(providers) - set(direct))

        def resolver(name: str):
            assert name in secret_names, name
            return None

        result = _render(resolve_secret=resolver)
        self.assertEqual(
            result.available_providers, tuple(sorted(set(providers) - set(direct)))
        )
        self.assertEqual(len(result.unavailable), len(direct))
        self.assertEqual({entry["provider"] for entry in result.unavailable}, set(direct))
        seed = _seed()
        self.assertTrue(seed)  # the fixture continuity rides a direct provider
        aliases = [
            (model_id, model["provider"], selector.removesuffix("[1m]"))
            for model_id, model in models.items()
            for _level, selector, _contract in catalog.line_selectors(model)
        ] + [(f"continuity:{alias}", entry["provider"], alias) for alias, entry in seed.items()]
        for model_id, provider_id, alias in aliases:
            model = {"provider": provider_id}
            with self.subTest(model=model_id, alias=alias):
                    if model["provider"] in direct:
                        self.assertNotIn(f'"{alias}"', result.yaml)
                    else:
                        self.assertIn(f'"{alias}"', result.yaml)
        # Every claude-protocol direct contract disappears with its provider.
        self.assertNotIn("output_config.effort", result.yaml)
        self.assertNotIn("reasoning_effort", result.yaml)
        self.assertNotIn('"thinking"', result.yaml)
        for route in providers["anthropic"]["passthrough_routes"]:
            self.assertIn(f'alias: "{route["name"]}"', result.yaml)

    def test_renderer_never_reads_process_env(self) -> None:
        prior = os.environ.get("KIMI_CLAUDE_API_KEY")
        os.environ["KIMI_CLAUDE_API_KEY"] = "env-value-must-be-ignored"
        try:
            result = _render(resolve_secret=lambda name: None)
        finally:
            # Restore, not delete: a pre-existing value must survive the test.
            if prior is None:
                del os.environ["KIMI_CLAUDE_API_KEY"]
            else:
                os.environ["KIMI_CLAUDE_API_KEY"] = prior
        self.assertNotIn("env-value-must-be-ignored", result.yaml)
        providers = catalog.load_catalog(CATALOG_ROOT).providers
        self.assertEqual(
            result.available_providers,
            tuple(
                sorted(
                    provider_id
                    for provider_id, provider in providers.items()
                    if provider["transport"]["kind"] != "direct"
                )
            ),
        )
        self.assertNotIn("kimi", result.available_providers)

    def test_resolver_value_used_verbatim(self) -> None:
        result = _render(resolve_secret=lambda name: "resolved-dummy-value")
        self.assertIn('api-key: "resolved-dummy-value"', result.yaml)

    def test_unavailable_providers_matches_renderer_report(self) -> None:
        # The UI helper and the renderer's omission pass are two readers of
        # one rule; this parity pin fails if they ever drift.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = bundle.docs["providers"]["providers"]
        for resolver in (
            lambda name: None,
            lambda name: "dummy",
            lambda name: "dummy" if name == "QWEN_CLAUDE_API_KEY" else None,
        ):
            via_helper = render.unavailable_providers(
                providers, resolve_secret=resolver
            )
            via_render = _render(resolve_secret=resolver).unavailable
            self.assertEqual(via_helper, via_render)


class EmitterTests(unittest.TestCase):
    def test_special_characters_escaped(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.lines)
        models["kimi-k3"]["display"] = 'Quote "and" : colon # hash ünïcode'
        yaml = _render(models=models).yaml
        self.assertIn(
            'display-name: "Quote \\"and\\" : colon # hash ünïcode"', yaml
        )

    def test_document_root_must_be_mapping(self) -> None:
        with self.assertRaises(RenderError):
            render.emit_yaml([1, 2])

    def test_unknown_payload_contract_rejected(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["kimi"]["payload_contracts"] = ["nonexistent-contract"]
        with self.assertRaisesRegex(RenderError, "unknown payload contract"):
            _render(providers=providers)

    def test_unknown_adapter_has_no_contracts(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["kimi"]["adapter"] = "cliproxy-unknown-v9"
        with self.assertRaisesRegex(RenderError, "unknown payload contract"):
            _render(providers=providers)


class AliasOwnerTests(unittest.TestCase):
    """An aggregator's aliases name the owner of each model line, not
    "unknown"; other providers keep their own family."""

    def test_owned_by_follows_the_line_family_on_an_aggregator(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = bundle.docs["providers"]["providers"]
        document = render.build_config_document(
            bundle.docs["gateway"], providers, bundle.lines, home=Path("/home/test"),
            gateway_token="a" * 64, resolve_secret=lambda name: "dummy-key", continuity={},
        )[0]
        owners: dict[str, str] = {}
        for section in document["claude-api-key"]:
            for model in section["models"]:
                if "owned-by" in model:
                    owners[model["alias"]] = model["owned-by"]
        aggregated = 0
        for key, line in bundle.lines.items():
            provider = providers.get(line["provider"])
            if provider is None:
                continue
            for _level, selector, _contract in catalog.line_selectors(line):
                alias = selector.removesuffix("[1m]")
                if alias not in owners:
                    continue
                expected = (line["family"] if provider["independence_family"] == "unknown"
                            else provider["independence_family"])
                with self.subTest(alias=alias):
                    self.assertEqual(owners[alias], expected)
                aggregated += provider["independence_family"] == "unknown"
        self.assertGreater(aggregated, 0)

    @staticmethod
    def _owners(bundle, seeds, retired) -> dict[str, str]:
        document = render.build_config_document(
            bundle.docs["gateway"], bundle.docs["providers"]["providers"], bundle.lines, home=Path("/home/test"),
            gateway_token="a" * 64, resolve_secret=lambda name: "dummy-key", continuity=seeds, retired=retired,
        )[0]
        return {model["alias"]: model["owned-by"] for section in document["claude-api-key"]
                for model in section["models"] if "owned-by" in model}

    @uses_shipped_catalog
    def test_retained_aggregator_aliases_name_the_owner_their_retired_entry_declares(self) -> None:
        # The packaged catalog and its continuity seeds (invariants only).
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        providers = bundle.docs["providers"]["providers"]
        retired = bundle.docs["retired"]["retired"]
        seeds = continuity.seed_entries(bundle)
        owners = self._owners(bundle, seeds, retired)
        checked = 0
        for alias, entry in sorted(seeds.items()):
            provider = providers.get(entry["provider"])
            if provider is None or provider["independence_family"] != render.UNKNOWN_FAMILY:
                continue
            key = entry["source"].removeprefix("seed:")
            with self.subTest(alias=alias):
                self.assertIn(alias, owners)
                self.assertEqual(owners[alias], retired[key]["family"])
                self.assertNotEqual(owners[alias], render.UNKNOWN_FAMILY)
            checked += 1
        self.assertGreater(checked, 0, "the packaged catalog retains an aggregator alias")

    @uses_shipped_catalog
    def test_a_retained_alias_off_its_retired_route_stays_unknown(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        retired = bundle.docs["retired"]["retired"]
        seeds = continuity.seed_entries(bundle)
        providers = bundle.docs["providers"]["providers"]
        alias = next(alias for alias, entry in sorted(seeds.items())
                     if providers.get(entry["provider"], {}).get("independence_family") == render.UNKNOWN_FAMILY)
        moved = {alias: {**seeds[alias], "wire": "vendor/another-model"}}
        self.assertEqual(self._owners(bundle, moved, retired)[alias], render.UNKNOWN_FAMILY)
        self.assertEqual(self._owners(bundle, {alias: seeds[alias]}, None)[alias], render.UNKNOWN_FAMILY)

    def test_an_undeclared_family_stays_unknown(self) -> None:
        spec = render.AliasSpec("openrouter", "w", "a", None, "D", 1, "continuity")
        self.assertEqual(render.alias_owner({"independence_family": "unknown"}, spec), "unknown")
        self.assertEqual(render.alias_owner({"independence_family": "meta"},
                                            render.AliasSpec("meta", "w", "a", None, "D", 1, "catalog", family="x")),
                         "meta")


if __name__ == "__main__":
    unittest.main()


class BearerAuthAndReasoningContractTests(unittest.TestCase):
    def test_bearer_auth_omits_auth_header_field(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["qwen"] = {
            "display": "Qwen",
            "independence_family": "alibaba",
            "support": "locally-validated-experimental",
            "support_note": "test",
            "adapter": "cliproxy-claude-compatible-v1",
            "transport": {
                "kind": "direct",
                "base_url": "https://token-plan.example.com/apps/anthropic",
                "auth": {"kind": "bearer", "secret_ref": "env:QWEN_CLAUDE_API_KEY"},
            },
            "passthrough_routes": [],
            "payload_contracts": ["reasoning-effort-xhigh"],
        }
        yaml = _render(
            providers=providers,
            resolve_secret=lambda name: "dummy" if name == "QWEN_CLAUDE_API_KEY" else None,
        ).yaml
        self.assertIn("token-plan.example.com", yaml)
        self.assertNotIn("auth-header", yaml)

    def test_header_auth_still_emits_auth_header(self) -> None:
        self.assertIn('auth-header: "x-api-key"', _render().yaml)

    def test_reasoning_effort_contract_rendered_for_declaring_lanes(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.lines)
        models["kimi-k3"]["efforts"]["max"]["proxy_contract"] = "reasoning-effort-xhigh"
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["kimi"]["payload_contracts"] = ["reasoning-effort-xhigh"]
        yaml = _render(models=models, providers=providers).yaml
        self.assertIn("reasoning_effort", yaml)
        self.assertIn("xhigh", yaml)


class AdapterContractTests(unittest.TestCase):
    """Every declared payload contract is pinned by its adapter.

    The codex ``reasoning-effort-low/medium`` and claude-compatible
    ``output-config-low/medium`` contracts join the adapter table; an unused
    contract renders nothing, so declaring one moves no golden.
    """

    LEVEL_PREFIXES = ("reasoning-effort-", "output-config-")

    def test_every_declared_contract_exists_for_its_adapter(self) -> None:
        for label, root in (("fixture", FIXTURE_ROOT), ("shipped", SHIPPED_ROOT)):
            bundle = catalog.load_catalog(root)
            for provider_id, provider in sorted(bundle.providers.items()):
                table = render.ADAPTER_PAYLOAD_CONTRACTS.get(provider["adapter"], {})
                for contract_id in provider["payload_contracts"]:
                    with self.subTest(root=label, provider=provider_id, contract=contract_id):
                        self.assertIn(contract_id, table)

    def test_override_params_match_the_contract_level(self) -> None:
        checked = 0
        for adapter, table in sorted(render.ADAPTER_PAYLOAD_CONTRACTS.items()):
            for contract_id, contract in sorted(table.items()):
                prefix = next(
                    (p for p in self.LEVEL_PREFIXES if contract_id.startswith(p)), None
                )
                if prefix is None:
                    continue
                with self.subTest(adapter=adapter, contract=contract_id):
                    self.assertEqual(contract["kind"], "override")
                    level = contract_id[len(prefix):]
                    self.assertEqual(list(contract["params"].values()), [level])
                    checked += 1
        self.assertGreaterEqual(checked, 4)
        for adapter, levels in (
            ("cliproxy-oauth-codex-v1", ("low", "medium")),
            ("cliproxy-claude-compatible-v1", ("low", "medium")),
        ):
            prefix = "reasoning-effort-" if "codex" in adapter else "output-config-"
            for level in levels:
                with self.subTest(adapter=adapter, level=level):
                    self.assertIn(prefix + level, render.ADAPTER_PAYLOAD_CONTRACTS[adapter])

    def _document(self, models, providers):
        bundle = catalog.load_catalog(CATALOG_ROOT)
        document, _available, _unavailable, _info = render.build_config_document(
            bundle.docs["gateway"],
            providers,
            models,
            home=Path("/home/test"),
            gateway_token="a" * 64,
            resolve_secret=lambda name: "dummy-key",
            continuity={},
        )
        return document

    def test_low_and_medium_lanes_render_payload_overrides(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.lines)
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        # A test-local low lane on a codex-pool model and a medium lane on a
        # claude-compatible direct model, each declared by its provider.
        codex_id, codex = next(
            (model_id, model)
            for model_id, model in sorted(models.items())
            if providers[model["provider"]]["adapter"] == "cliproxy-oauth-codex-v1"
        )
        compat_id, compat = next(
            (model_id, model)
            for model_id, model in sorted(models.items())
            if providers[model["provider"]]["adapter"] == "cliproxy-claude-compatible-v1"
        )
        codex["efforts"]["low"] = {
            "selector": "gpt-multi-fixture-low",
            "proxy_contract": "reasoning-effort-low",
        }
        compat["efforts"]["medium"] = {
            "selector": "claude-multi-fixture-medium",
            "proxy_contract": "output-config-medium",
        }
        providers[codex["provider"]]["payload_contracts"].append("reasoning-effort-low")
        providers[compat["provider"]]["payload_contracts"].append("output-config-medium")
        overrides = self._document(models, providers)["payload"]["override"]
        self.assertIn(
            {
                "models": [{"name": "gpt-multi-fixture-low", "protocol": "codex"}],
                "params": {"reasoning.effort": "low"},
            },
            overrides,
            codex_id,
        )
        self.assertIn(
            {
                "models": [{"name": "claude-multi-fixture-medium", "protocol": "claude"}],
                "params": {"output_config.effort": "medium"},
            },
            overrides,
            compat_id,
        )

    def test_codex_max_lane_renders_the_max_override(self) -> None:
        # Catalog 34: the codex adapter pins reasoning-effort-max; a
        # test-local max lane on the fixture's codex-pool model renders its
        # payload override, like the other levels.
        self.assertEqual(
            render.ADAPTER_PAYLOAD_CONTRACTS["cliproxy-oauth-codex-v1"]["reasoning-effort-max"],
            {"kind": "override", "protocol": "codex", "params": {"reasoning.effort": "max"}},
        )
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.lines)
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        codex_id, codex = next(
            (model_id, model)
            for model_id, model in sorted(models.items())
            if providers[model["provider"]]["adapter"] == "cliproxy-oauth-codex-v1"
        )
        codex["efforts"]["max"] = {
            "selector": "gpt-multi-fixture-max",
            "proxy_contract": "reasoning-effort-max",
        }
        providers[codex["provider"]]["payload_contracts"].append("reasoning-effort-max")
        document = self._document(models, providers)
        self.assertIn(
            {
                "models": [{"name": "gpt-multi-fixture-max", "protocol": "codex"}],
                "params": {"reasoning.effort": "max"},
            },
            document["payload"]["override"],
            codex_id,
        )
        self.assertIn("gpt-multi-fixture-max", render.rendered_selectors(document))

    def test_declared_but_unused_contract_renders_nothing(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = bundle.lines
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        baseline = self._document(models, providers)["payload"]
        for provider in providers.values():
            if provider["adapter"] == "cliproxy-oauth-codex-v1":
                provider["payload_contracts"][:0] = [
                    "reasoning-effort-low", "reasoning-effort-medium"
                ]
            elif provider["adapter"] == "cliproxy-claude-compatible-v1":
                provider["payload_contracts"][:0] = [
                    contract
                    for contract in ("output-config-low", "output-config-medium")
                    if contract not in provider["payload_contracts"]
                ]
        self.assertEqual(self._document(models, providers)["payload"], baseline)


def _two_lane_direct_model(base: dict, provider: str, stem: str, wire: str) -> dict:
    """A test-local 1M gateway-effort (high/max) v2 line on a direct provider."""

    model = copy.deepcopy(base)
    model["provider"] = provider
    model["wire_model"] = wire
    model["display"] = f"Fixture {stem}"
    model["default_effort"] = "high"
    model["context"]["provider_tokens"] = 1000000
    model["context"]["scalar_tokens"] = None
    model["efforts"] = {
        effort: {
            "selector": f"claude-multi-{stem}-{effort}[1m]",
            "proxy_contract": f"output-config-{effort}",
        }
        for effort in ("high", "max")
    }
    model["status"] = "active"
    return model


class DeepSeekProRenderTests(unittest.TestCase):
    """A second model on one direct provider shares its effort contracts.

    The shipped DeepSeek Pro/Flash shape (stable wire alias, no dated
    wire) is catalog data pinned by test_catalog's shipped
    DeepSeekProductionModelsTests. Here the render behaviour it relied on is
    pinned on test-local models riding the fixture's (model-less) deepseek
    provider: both lanes of each model render under that provider's section
    with the wire name as ``name``, and each effort contract is ONE override
    entry listing every model's alias for that tier, sorted.
    """

    def test_pro_aliases_and_overrides_render(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        provider = bundle.providers["deepseek"]
        self.assertEqual(
            set(provider["payload_contracts"]),
            {"output-config-high", "output-config-max"},
        )
        lines = copy.deepcopy(bundle.lines)
        base = lines["kimi-k3"]
        lines["fixture-flash"] = _two_lane_direct_model(
            base, "deepseek", "fixture-flash", "fixture-flash-wire"
        )
        lines["fixture-pro"] = _two_lane_direct_model(
            base, "deepseek", "fixture-pro", "fixture-pro-wire"
        )
        raw = _raw_with_models(lines)
        self.assertEqual(catalog.validate_catalog(raw), [])
        yaml = _render(models=lines).yaml
        section = yaml.split(
            f'base-url: "{provider["transport"]["base_url"]}"', 1
        )[1].split("- api-key:", 1)[0]
        self.assertEqual(section.count('name: "fixture-pro-wire"'), 2)
        self.assertEqual(section.count('name: "fixture-flash-wire"'), 2)
        self.assertIn('alias: "claude-multi-fixture-pro-high"', section)
        self.assertIn('alias: "claude-multi-fixture-pro-max"', section)
        self.assertNotIn("[1m]", section)
        self.assertIn(f'owned-by: "{provider["independence_family"]}"', section)
        self.assertIn('context-length: 1000000', section)
        high = (
            '- models:\n'
            '        - name: "claude-multi-fixture-flash-high"\n'
            '          protocol: "claude"\n'
            '        - name: "claude-multi-fixture-pro-high"\n'
            '          protocol: "claude"\n'
            '      params:\n'
            '        "output_config.effort": "high"'
        )
        max_ = (
            '- models:\n'
            '        - name: "claude-multi-fixture-flash-max"\n'
            '          protocol: "claude"\n'
            '        - name: "claude-multi-fixture-pro-max"\n'
            '          protocol: "claude"\n'
            '      params:\n'
            '        "output_config.effort": "max"'
        )
        self.assertIn(high, yaml)
        self.assertIn(max_, yaml)


class Grok46RenderTests(unittest.TestCase):
    """Rendered active route is exact Grok 4.6 with xhigh override."""

    def test_exact_46_route_and_no_active_45(self) -> None:
        yaml=_render().yaml
        self.assertIn('name: "x-ai/grok-4.6"',yaml)
        self.assertIn('alias: "claude-multi-grok46-xhigh"',yaml)
        self.assertNotIn('x-ai/grok-4.5',yaml)
        self.assertNotIn('claude-multi-grok45',yaml)
        payload = yaml.split('payload:', 1)[1]
        for level in ("high", "xhigh"):
            # The alias sits in the override group of its own effort (other
            # lines of the same contract share the group).
            entry = f'        - name: "claude-multi-grok46-{level}"\n          protocol: "claude"\n'
            self.assertIn(entry, payload)
            group = payload.split(entry, 1)[1].split('      params:\n', 1)[1]
            self.assertTrue(group.startswith(f'        "output_config.effort": "{level}"'), group[:80])
        self.assertNotIn('reasoning_effort: "xhigh"', yaml.split('payload:',1)[1].split('claude-multi-qwen38-max',1)[0])


class ZeroModelSectionTests(unittest.TestCase):
    """A provider with zero models stays configured but renders
    no section - never ``models: []`` (the pinned gateway would serve its
    whole embedded Claude registry for an empty claude-api-key list)."""

    @staticmethod
    def _document(root=CATALOG_ROOT, models=None, providers=None, continuity_aliases=None):
        bundle = catalog.load_catalog(root)
        return render.build_config_document(
            bundle.docs["gateway"],
            providers or bundle.providers,
            bundle.lines if models is None else models,
            home=Path("/home/test"),
            gateway_token="a" * 64,
            resolve_secret=lambda _name: "dummy-secret",
            continuity={} if continuity_aliases is None else continuity_aliases,
        )

    def test_credentialed_zero_model_direct_provider_emits_no_item(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        document, available, _unavailable, _info = self._document()
        empty = [
            provider_id
            for provider_id, provider in bundle.providers.items()
            if provider["transport"]["kind"] == "direct"
            and not any(m["provider"] == provider_id for m in bundle.lines.values())
        ]
        self.assertTrue(empty)  # the fixture has model-less deepseek and meta
        for provider_id in empty:
            with self.subTest(provider=provider_id):
                self.assertIn(provider_id, available)  # credential status kept
                base_url = bundle.providers[provider_id]["transport"]["base_url"]
                self.assertFalse(
                    [item for item in document["claude-api-key"] if item["base-url"] == base_url]
                )
                self.assertEqual(
                    render.provider_selectors(
                        provider_id, bundle.providers[provider_id], bundle.lines, continuity={},
                    ),
                    frozenset(),
                )

    def test_no_item_ever_has_an_empty_models_list(self) -> None:
        for root in (FIXTURE_ROOT, SHIPPED_ROOT):
            for seeded in (False, True):
                with self.subTest(root=root.name, continuity=seeded):
                    document = self._document(
                        root, continuity_aliases=_seed(root) if seeded else {}
                    )[0]
                    for section in ("claude-api-key", "openai-compatibility"):
                        for item in document[section]:
                            self.assertTrue(item["models"], f"{section} item with models: []")

    def test_zero_model_provider_with_continuity_emits_an_item(self) -> None:
        # A model-less provider renders a section once it carries a
        # continuity alias (the fixture's retired muse-spark rides meta).
        bundle = catalog.load_catalog(CATALOG_ROOT)
        document = self._document(continuity_aliases=_seed())[0]
        base_url = bundle.providers["meta"]["transport"]["base_url"]
        items = [item for item in document["claude-api-key"] if item["base-url"] == base_url]
        self.assertEqual(len(items), 1)
        self.assertEqual(
            sorted(model["alias"] for model in items[0]["models"]),
            sorted(_seed()),
        )

    def test_zero_model_compat_provider_emits_only_the_sentinel(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = {
            key: model for key, model in bundle.lines.items()
            if bundle.providers[model["provider"]]["transport"]["kind"] != "direct-openai"
        }
        document = self._document(models=models)[0]
        self.assertEqual(
            [item["name"] for item in document["openai-compatibility"]],
            ["claude-multi-render"],
        )

    def test_custom_provider_without_models_emits_no_item(self) -> None:
        from claude_multi import custom

        bundle = catalog.load_catalog(CATALOG_ROOT)
        registry = {
            "version": 1,
            "providers": {
                "my-lab": {
                    "base_url": "https://lab.example.com/apps/anthropic",
                    "auth_kind": "bearer",
                    "secret_env": "MY_LAB_API_KEY",
                }
            },
            "models": {},
        }
        docs = custom.merge_docs(bundle.docs, registry)
        document, available, _unavailable, _info = self._document(
            models=docs["models-v2"]["models"], providers=docs["providers"]["providers"]
        )
        self.assertIn("my-lab", available)
        self.assertFalse(
            [item for item in document["claude-api-key"] if "lab.example.com" in item["base-url"]]
        )


def _yaml_key_paths(lines: list[str]) -> set[str]:
    """Key paths of a restricted-YAML block (``a.[].b`` for list items).

    Enough for the emitter's own shape: mappings, lists of mappings and
    scalars, two-space indentation, ``- `` item markers.
    """

    paths: set[str] = set()
    stack: list[tuple[int, str]] = []
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if content.startswith("- "):
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, "[]"))
            content = content[2:]
            indent += 2
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = content.split(":", 1)[0].strip('"')
        stack.append((indent, key))
        paths.add(".".join(segment for _indent, segment in stack))
    return paths


class KimiCompatGoGateSyncTests(unittest.TestCase):
    """The Go gate's embedded YAML tracks the renderer.

    ``internal/config/claude_compat_fields_test.go`` (added by the
    kimi-claude-compat patch) decodes a claude-api-key block shaped like
    this renderer's output and asserts the compat fields are populated — a
    silently ignored YAML key would hide a partial port. The Go tree cannot
    read this repository inside the Nix sandbox, so the YAML is a COPY of
    the render shape; this test keeps the copy honest: every claude-api-key
    key path the renderer emits must be decoded by the Go gate, and the Go
    YAML may carry only the documented extra patch field.
    """

    PATCH = PATCH_DIR / "cli-proxy-api-kimi-claude-compat.patch"
    # Patch fields the renderer does not emit (yet): decoded by the Go gate
    # so the field is proven wired, harmless while unrendered.
    GO_ONLY = {"claude-api-key.[].models.[].max-completion-tokens"}
    _OPEN = "const claudeCompatGoldenYAML = `"

    def _go_patch_body(self) -> str:
        self.assertTrue(self.PATCH.is_file(), f"{self.PATCH} missing next to the package")
        text = self.PATCH.read_text(encoding="utf-8")
        return "\n".join(line[1:] for line in text.splitlines() if line.startswith("+"))

    def _go_yaml_lines(self) -> list[str]:
        body = self._go_patch_body()
        start = body.index(self._OPEN) + len(self._OPEN)
        return body[start:body.index("`", start)].splitlines()

    def _rendered_lines(self) -> list[str]:
        lines = _render().yaml.splitlines()
        start = lines.index("claude-api-key:")
        block = [lines[start]]
        for line in lines[start + 1:]:
            if line and not line.startswith(" "):
                break
            block.append(line)
        return block

    def test_go_gate_yaml_decodes_every_rendered_claude_api_key_field(self) -> None:
        go_paths = _yaml_key_paths(self._go_yaml_lines())
        rendered = _yaml_key_paths(self._rendered_lines())
        # The fixture render exercises header auth (auth-header) and the
        # model metadata fields; guard that premise so the comparison
        # cannot pass vacuously.
        for field in ("auth-header", "models.[].owned-by", "models.[].context-length"):
            with self.subTest(rendered=field):
                self.assertIn(f"claude-api-key.[].{field}", rendered)
        self.assertEqual(
            sorted(rendered - go_paths), [], "renderer keys the Go gate never decodes"
        )
        self.assertEqual(
            sorted(go_paths - rendered - self.GO_ONLY),
            [],
            "Go gate keys the renderer never emits",
        )

    def test_go_gate_asserts_each_kimi_compat_field(self) -> None:
        body = self._go_patch_body()
        gate = body[body.index("func TestClaudeCompatFieldsDecodeFromRenderedGoldenShape"):]
        for accessor in ("AuthHeader", "GetOwnedBy()", "GetContextLength()", "ForceMapping"):
            with self.subTest(accessor=accessor):
                self.assertIn(accessor, gate)
