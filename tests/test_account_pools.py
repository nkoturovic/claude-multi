"""Account pools as data: the packaged pool table and its closed schema,
the rules the schema cannot state, the collision-free classification of
sign-in records by file name (``kimi`` vs ``kimi-ai``), the registry
section mapping (two pools may share a section, never a channel), the
catalog's check that every OAuth-pool provider names a pool that names it
back, and the sign-in layer reading every pool fact from the table.

The shipped pools are checked against what the sign-in did before the pools
became data (provider, account kind, host, login command, flow, offering
policies, acknowledgement ids and text digests, record prefix, registry
sections), so an acknowledgement given earlier stays current. Synthetic
pools stand in for a later subscription; no new pool is shipped. Temp
directories only; no gateway and no provider request."""

from __future__ import annotations

import ast
import copy
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from _catalog import FIXTURE_ROOT, SHIPPED_ROOT
from _layout import RESOURCES_ROOT
from claude_multi import account_pools, catalog, choices, discovery, operator as operator_mod, proxy, strict_json
from claude_multi.setup import model, signin, texts

SCHEMA = strict_json.load(RESOURCES_ROOT / account_pools.SCHEMA)
SHIPPED = strict_json.load(RESOURCES_ROOT / account_pools.FILE)

# What the sign-in did for the two pools before they became data.
BEFORE = {
    "claude": {
        "provider": "anthropic", "display": "Claude account", "flow": "browser", "host": "claude.ai",
        "command": "claude-login", "policies": ("public",), "methods": ("browser", "address"),
        "ack_id": "claude-account-personal-use/1",
        "ack_sha256": "35e1d3ab3c7ccc3fbcd822d8fbef83ab6bbe7fe8a8d727edb471371ad6180fef",
        "prefix": "claude", "section": "claude", "sections": ("claude",),
        # The account modal said the sign-in is separate from the client's
        # own login (a Claude account) for this pool only.
        "client_account": True,
    },
    "codex": {
        "provider": "openai", "display": "ChatGPT account", "flow": "device", "host": "auth.openai.com",
        "command": "codex-device-login", "policies": ("public", "public-strict"), "methods": ("device",),
        "ack_id": "chatgpt-account-personal-use/1",
        "ack_sha256": "ebae2232ecd1814407f16d9a2eaa129410f74a4b0fd6a83413eb694565a75ed6",
        "prefix": "codex", "section": "codex-pro",
        "sections": ("codex-free", "codex-team", "codex-plus", "codex-pro"),
        "client_account": False,
    },
}


def pool_doc(provider: str, *, flow: str = "device", prefix: str | None = None, section: str = "kimi",
             sections: tuple[str, ...] | None = None, command: str | None = None,
             policies: tuple[str, ...] = ("public", "public-strict")) -> dict[str, Any]:
    """One synthetic pool entry (a later subscription's shape)."""

    return {
        "provider": provider,
        "display": f"{provider} account",
        "sign_in": {"flow": flow, "host": f"auth.{provider}.example", "command": command or f"{provider}-login",
                    "policies": list(policies)},
        "acknowledgement": {"id": f"{provider}-account-personal-use/1",
                            "text": f"Sign in with your {provider} account for your own use.\n"
                                    "Type personal to confirm this is your own account, for your own use."},
        "record_prefix": prefix or provider,
        "registry": {"section": section, "sections": list(sections or (section,))},
    }


def table_doc(**pools: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "record_prefixes": list(SHIPPED["record_prefixes"]), "pools": pools}


def kimi_table() -> account_pools.PoolTable:
    """The two Kimi regions as pools: one registry section, two channels."""

    document = copy.deepcopy(SHIPPED)
    document["pools"]["kimi"] = pool_doc("kimi")
    document["pools"]["kimi-ai"] = pool_doc("kimi-ai")
    return account_pools.parse(document, SCHEMA)


class ShippedPoolsTests(unittest.TestCase):
    def test_the_packaged_table_reads_and_is_the_default(self) -> None:
        table = account_pools.load()
        self.assertIs(table, account_pools.load(RESOURCES_ROOT))
        self.assertEqual(account_pools.names(), tuple(sorted(SHIPPED["pools"])))
        self.assertEqual(tuple(table.pools), account_pools.names())

    def test_the_two_shipped_pools_keep_what_the_sign_in_did_before(self) -> None:
        for name, before in BEFORE.items():
            pool = account_pools.pool(name)
            with self.subTest(pool=name):
                self.assertIsNotNone(pool)
                assert pool is not None
                self.assertEqual(
                    (pool.provider, pool.display, pool.sign_in.flow, pool.sign_in.host, pool.sign_in.command,
                     pool.sign_in.policies, pool.sign_in.methods, pool.acknowledgement.id,
                     signin.text_sha256(pool.acknowledgement.text), pool.record_prefix, pool.registry_section,
                     pool.registry_sections),
                    (before["provider"], before["display"], before["flow"], before["host"], before["command"],
                     before["policies"], before["methods"], before["ack_id"], before["ack_sha256"],
                     before["prefix"], before["section"], before["sections"]))
                self.assertEqual(signin.ack_text(name), (before["ack_id"], pool.acknowledgement.text))
                self.assertEqual(signin.methods(name), before["methods"])
                self.assertEqual((pool.client_account, signin.client_account(name)), (before["client_account"],) * 2)
                self.assertEqual(texts.ACCOUNT_KINDS[name], before["display"])
                self.assertEqual(texts.SIGNIN_HOSTS[name], before["host"])
                self.assertEqual(signin.LOGIN_COMMANDS[name], before["command"])
                self.assertEqual(signin.ACCOUNT_POOLS[before["provider"]], name)
        self.assertEqual(choices.ACK_POOLS, account_pools.names())

    def test_every_catalog_oauth_pool_provider_is_a_pool_and_back(self) -> None:
        for root in (SHIPPED_ROOT, FIXTURE_ROOT):
            providers = catalog.load_raw(root)["docs"]["providers"]["providers"]
            pooled = {provider["transport"]["pool"]: pid for pid, provider in providers.items()
                      if provider["transport"]["kind"] == "oauth-pool"}
            with self.subTest(root=root.name):
                self.assertEqual(pooled, {name: pool.provider for name, pool in account_pools.pools().items()})

    def test_each_pool_login_command_is_a_gateway_program_command(self) -> None:
        # The gateway program's login commands stay in claude-multi-proxy's
        # command table; the pools name them, one each.
        self.assertEqual(set(proxy.LOGIN_FLAGS),
                         {pool.sign_in.command for pool in account_pools.pools().values()})

    def test_the_acknowledgements_end_with_the_word_to_type(self) -> None:
        for name, pool in account_pools.pools().items():
            with self.subTest(pool=name):
                self.assertIn(f"Type {texts.ACK_WORD} to confirm", pool.acknowledgement.text.rsplit("\n", 1)[-1])
                self.assertLessEqual(len(pool.acknowledgement.id), choices.ACK_TEXT_ID_MAX)

    def test_the_sign_in_layer_names_no_pool(self) -> None:
        source = inspect.getsource(signin)
        literals = {node.value for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for name, pool in account_pools.pools().items():
            with self.subTest(pool=name):
                self.assertFalse({name, pool.provider, pool.sign_in.command} & literals)

    def test_a_broken_table_refuses_with_a_remedy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schemas").mkdir()
            (root / account_pools.SCHEMA).write_bytes((RESOURCES_ROOT / account_pools.SCHEMA).read_bytes())
            with self.assertRaises(account_pools.AccountPoolsError) as caught:
                account_pools.load(root)
            self.assertEqual(caught.exception.remedy, account_pools.REMEDY)
            (root / account_pools.FILE).write_text('{"version": 1, "pools": {}}')
            with self.assertRaisesRegex(account_pools.AccountPoolsError, "record_prefixes"):
                account_pools.load(root)


class TableRulesTests(unittest.TestCase):
    def refused(self, document: dict[str, Any], pattern: str) -> None:
        with self.assertRaisesRegex(account_pools.AccountPoolsError, pattern):
            account_pools.parse(document, SCHEMA)

    def test_a_later_pool_is_one_entry(self) -> None:
        table = kimi_table()
        self.assertEqual(table.names(), ("claude", "codex", "kimi", "kimi-ai"))
        self.assertEqual(table.by_provider("kimi-ai").name, "kimi-ai")
        self.assertEqual(table.pools["kimi"].sign_in.methods, ("device",))
        self.assertFalse(table.pools["kimi"].client_account, "absent means the client has no such login")

    def test_the_schema_is_closed(self) -> None:
        document = table_doc(kimi=dict(pool_doc("kimi"), endpoint="https://api.kimi.example"))
        self.refused(document, "endpoint")
        document = table_doc(kimi=pool_doc("kimi", flow="callback"))
        self.refused(document, "flow")
        document = table_doc(kimi=pool_doc("kimi", policies=("public", "everyone")))
        self.refused(document, "policies")
        document = table_doc(kimi=dict(pool_doc("kimi"), client_account="yes"))
        self.refused(document, "client_account")

    def test_the_rules_the_schema_cannot_state(self) -> None:
        self.refused(table_doc(Kimi=pool_doc("kimi")), "pool name 'Kimi'")
        self.refused(table_doc(kimi=pool_doc("kimi", prefix="moonshot")), "not one of record_prefixes")
        self.refused(table_doc(kimi=pool_doc("kimi", section="kimi", sections=("kimi-pro",))),
                     "not one of its sections")
        self.refused(table_doc(kimi=pool_doc("kimi", policies=("public-strict",))), "'public' sign-in policy")
        for field, second in (("provider", dict(pool_doc("kimi-ai"), provider="kimi")),
                              ("login command", pool_doc("kimi-ai", command="kimi-login")),
                              ("record prefix", pool_doc("kimi-ai", prefix="kimi"))):
            with self.subTest(field=field):
                self.refused(table_doc(kimi=pool_doc("kimi"), **{"kimi-ai": second}), f"share the {field}")
        second = pool_doc("kimi-ai")
        second["acknowledgement"]["id"] = "kimi-account-personal-use/1"
        self.refused(table_doc(kimi=pool_doc("kimi"), **{"kimi-ai": second}), "share the acknowledgement id")


class RecordClassificationTests(unittest.TestCase):
    def test_the_longest_prefix_wins(self) -> None:
        table = kimi_table()
        for name, pool in (("kimi-1767225600000.json", "kimi"), ("kimi-ai-1767225600000.json", "kimi-ai"),
                           ("claude-a@example.com.json", "claude"), ("codex-b@example.com-pro.json", "codex"),
                           ("xai-c@example.com.json", None), ("claude-notes.txt", None),
                           (".claude-a.json.cm-save-0123456789abcdef", None), ("claude.json", None)):
            with self.subTest(name=name):
                self.assertEqual(table.classify(name), pool)
        self.assertEqual(table.pools["kimi-ai"].account("kimi-ai-1767225600000.json"), "1767225600000")
        self.assertEqual(table.pools["kimi"].account("kimi-1767225600000.json"), "1767225600000")

    def test_a_prefix_no_pool_uses_still_keeps_its_records_apart(self) -> None:
        # Only the .com region is a pool: the gateway's .ai records stay out.
        document = copy.deepcopy(SHIPPED)
        document["pools"]["kimi"] = pool_doc("kimi")
        table = account_pools.parse(document, SCHEMA)
        self.assertEqual(table.classify("kimi-1.json"), "kimi")
        self.assertEqual(table.record_prefix("kimi-ai-2.json"), "kimi-ai")
        self.assertIsNone(table.classify("kimi-ai-2.json"))

    def test_the_sign_in_layer_lists_only_its_own_records(self) -> None:
        table = kimi_table()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(account_pools, "load", lambda root=None: table):
            directory = Path(tmp)
            for name in ("kimi-1.json", "kimi-ai-2.json", "kimi-ai-3.json", "claude-a@example.com.json"):
                (directory / name).write_bytes(b"{}")
            self.assertEqual(sorted(signin._record_files(directory, "kimi")), ["kimi-1.json"])
            self.assertEqual(sorted(signin._record_files(directory, "kimi-ai")), ["kimi-ai-2.json", "kimi-ai-3.json"])
            self.assertEqual(signin.account_name("kimi-ai", "kimi-ai-2.json"), "2")
            self.assertEqual(signin.method_default("kimi", {"DISPLAY": ":0"}), "device")
            self.assertEqual(signin.methods("kimi-ai"), ("device",))
            with mock.patch.object(signin, "signin_policy", return_value="public-strict"):
                self.assertTrue(signin.pool_offered("kimi"))
                self.assertFalse(signin.pool_offered("claude"))
            self.assertFalse(signin.pool_offered("unknown"))
            with self.assertRaisesRegex(model.Refused, "unknown account pool"):
                signin.methods("unknown")


class RegistryMappingTests(unittest.TestCase):
    def test_regional_pools_share_a_section_never_a_channel(self) -> None:
        table = kimi_table()
        with mock.patch.object(account_pools, "load", lambda root=None: table):
            self.assertEqual(account_pools.section_pools("kimi"), ("kimi", "kimi-ai"))
            for pool in ("kimi", "kimi-ai"):
                provider = {"transport": {"kind": "oauth-pool", "pool": pool}}
                with self.subTest(pool=pool):
                    self.assertEqual(catalog.registry_section(provider), "kimi")
            # A section two providers' pools share names neither of them.
            self.assertIsNone(discovery.section_provider("kimi"))
            self.assertEqual(discovery.section_provider("codex-plus"), "openai")

    def test_the_shipped_mapping(self) -> None:
        for name, before in BEFORE.items():
            provider = {"transport": {"kind": "oauth-pool", "pool": name}}
            with self.subTest(pool=name):
                self.assertEqual(catalog.registry_section(provider), before["section"])
                for section in before["sections"]:
                    self.assertEqual(discovery.section_provider(section), before["provider"])
        self.assertIsNone(catalog.registry_section({"transport": {"kind": "oauth-pool", "pool": "unknown"}}))
        self.assertIsNone(discovery.section_provider("gemini"))
        self.assertEqual(discovery.DEFAULT_SECTIONS, tuple(BEFORE[name]["section"] for name in sorted(BEFORE)))

    def test_static_overlay_wires_follow_each_channel_sections(self) -> None:
        registry = catalog.PinnedRegistry(root=Path("."), sections={
            "claude": frozenset({"Claude-A"}), "codex-free": frozenset({"gpt-free"}),
            "codex-pro": frozenset({"gpt-pro"}), "kimi": frozenset({"kimi-k"})}, codex={})
        self.assertEqual(operator_mod.static_overlay_wires(registry),
                         {"claude": frozenset({"claude-a"}), "codex": frozenset({"gpt-free", "gpt-pro"})})
        self.assertIsNone(operator_mod.static_overlay_wires(None))


class CatalogCheckTests(unittest.TestCase):
    def raw(self) -> dict[str, Any]:
        return copy.deepcopy(catalog.load_raw(FIXTURE_ROOT))

    def test_the_fixture_catalog_names_the_pools(self) -> None:
        self.assertEqual([error for error in catalog.validate_catalog(self.raw()) if "account pool" in error], [])

    def test_an_unknown_or_foreign_pool_is_refused(self) -> None:
        raw = self.raw()
        providers = raw["docs"]["providers"]["providers"]
        pooled = sorted(pid for pid, provider in providers.items() if provider["transport"]["kind"] == "oauth-pool")
        first, second = pooled[0], pooled[1]
        providers[first]["transport"]["pool"] = "nope"
        errors = catalog.validate_catalog(raw)
        self.assertIn(f"providers.{first}: unknown account pool 'nope'", errors)
        raw = self.raw()
        providers = raw["docs"]["providers"]["providers"]
        theirs = providers[second]["transport"]["pool"]
        providers[first]["transport"]["pool"] = theirs
        self.assertIn(f"providers.{first}: account pool {theirs!r} belongs to provider {second!r}",
                      catalog.validate_catalog(raw))


class AcknowledgementStoreTests(unittest.TestCase):
    ACK = {"text_id": "kimi-account-personal-use/1", "text_sha256": "0" * 64,
           "acknowledged_at": "2026-10-04T00:00:00Z", "typed": "personal"}

    def test_a_later_release_pool_is_kept_and_never_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "XDG_CONFIG_HOME": str(Path(tmp) / "config")}
            path = choices.path(env)
            path.parent.mkdir(parents=True, mode=0o700)
            path.write_text(strict_json.pretty_file_bytes(
                {"version": 1, "acknowledgements": {"kimi": self.ACK}}).decode())
            path.chmod(0o600)
            self.assertEqual(dict(choices.read(env).get("acknowledgements")), {"kimi": self.ACK})
            ours = dict(self.ACK, text_id="claude-account-personal-use/1")
            choices.set_acknowledgement(env, "claude", ours)
            self.assertEqual(dict(choices.read(env).get("acknowledgements")), {"claude": ours, "kimi": self.ACK})
            self.assertIsNone(signin.current_ack(env, "claude"))  # not today's text digest
            with self.assertRaisesRegex(choices.ChoicesError, "unknown account pool 'kimi'"):
                choices.set_acknowledgement(env, "kimi", self.ACK)
            path.write_text(strict_json.pretty_file_bytes(
                {"version": 1, "acknowledgements": {"Kimi Account": self.ACK}}).decode())
            with self.assertRaisesRegex(choices.ChoicesError, "names no account pool"):
                choices.read(env)


if __name__ == "__main__":
    unittest.main()
