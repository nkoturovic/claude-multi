"""Shared per-slot readiness, first-satisfiable seed, starter.

Fixture catalog and hermetic Runtimes only (``test_cli.CLITestCase``: a
fixture served set, a fixture token, no gateway, no provider). Pools and
lines are selected by shape (transport kind), never by shipped id. OAuth
credential records are fixture file names in the fixture HOME's auth dir.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from unittest import mock

import test_cli  # module import: no test classes re-exported
import test_discovery  # module import: no test classes re-exported
from claude_multi import catalog, cli, profile, readiness, sessions, state, strict_json
from claude_multi.cli import doctor as doctor_mod
from claude_multi.cli import gateway_facts, selection, session_facts
from claude_multi.cli import types as cli_types


class _Case(test_cli.CLITestCase):
    def pools(self) -> dict[str, str]:
        """provider id -> OAuth pool, from the fixture providers (by shape)."""

        providers = self.runtime.lineup_catalog().providers
        return {pid: p["transport"]["pool"] for pid, p in providers.items()
                if p["transport"]["kind"] == "oauth-pool"}

    def sign_in(self, *pools: str) -> None:
        """Fixture OAuth credential records (names only) for ``pools``."""

        auth = Path(self.runtime.environ["HOME"]) / self.runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        state.ensure_private_dir(auth)
        for pool in pools:
            state.atomic_write(auth / f"{pool}-fixture@example.invalid.json", b"{}")

    def seed_pools(self, name: str) -> set[str]:
        lcat = self.runtime.lineup_catalog()
        document = self.runtime.profiles.load(name)
        keys = [document["lead"]["model"], *(b["model"] for b in document["agents"].values())]
        pools = self.pools()
        return {pools[lcat.lines[key]["provider"]] for key in keys if lcat.lines[key]["provider"] in pools}

    def pools_only(self, name: str) -> bool:
        """Every binding of seed ``name`` is on an OAuth pool provider."""

        lcat = self.runtime.lineup_catalog()
        document = self.runtime.profiles.load(name)
        keys = [document["lead"]["model"], *(b["model"] for b in document["agents"].values())]
        return all(lcat.lines[key]["provider"] in self.pools() for key in keys)

    def prepare(self, name: str, **kwargs):
        document = self.runtime.profiles.load(name)
        target = cli_types.LaunchTarget("profile", document, name, True, f"Profile {name}")
        return self.runtime.prepare(target, action="fresh", passthrough=[], **kwargs)

    def doctor(self):
        return doctor_mod._collect_doctor_reports(self.runtime)


class ReadinessTests(_Case):
    def test_card_doctor_and_seed_verdicts_agree(self) -> None:
        """One computation: the card warning, the doctor Attention and the
        seed choice read the same per-slot verdict; readiness never blocks."""

        pools = sorted(set(self.pools().values()))
        self.sign_in(*pools)
        self.assertEqual(selection.default_profile(self.runtime), (readiness.SEED_ORDER[0], None))
        prepared = self.prepare(readiness.SEED_ORDER[0])
        self.assertFalse([n for n in prepared.notices if n.startswith("! ")])
        _problems, _info, attention = self.doctor()
        self.assertFalse([a for a in attention if "next launch" in a], attention)
        # Sign one pool out: every seed that binds it is unready; the first
        # seed that does not is chosen; the card and doctor name the slot.
        missing = next(pool for pool in pools if pool in self.seed_pools(readiness.SEED_ORDER[0]))
        auth = Path(self.runtime.environ["HOME"]) / self.runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        for path in auth.glob(f"{missing}-*.json"):
            path.unlink()
        # The next seed that binds only the signed-in pools (a seed on a keyed
        # provider needs its key, which this fixture HOME does not hold).
        expected = next(name for name in readiness.SEED_ORDER
                        if missing not in self.seed_pools(name) and self.pools_only(name))
        self.assertEqual(selection.default_profile(self.runtime), (expected, None))
        verdicts = self.runtime.seed_readiness()
        blocked = [row for row in verdicts[readiness.SEED_ORDER[0]] if row.state == readiness.BLOCKED]
        self.assertTrue(blocked)
        login = gateway_facts._OAUTH_LOGIN_COMMANDS[missing]
        text = f"{profile.label(blocked[0].slot)}: no {missing} OAuth credential record — {login}"
        self.assertEqual(blocked[0].text(), text)
        prepared = self.prepare(readiness.SEED_ORDER[0])  # never a block
        self.assertIn(f"! {text}", prepared.notices)
        self.assertEqual(prepared.secret_problems, ())
        from claude_multi import views

        card = views.card_model(target=prepared.target, lineup=prepared.lineup, notices=prepared.notices)
        self.assertIn(f"! {text}", [row.text for row in card.rows if row.kind == "readiness"])
        self.assertTrue(card.ready)
        problems, _info, attention = self.doctor()
        self.assertIn(f"profile {readiness.SEED_ORDER[0]}: next launch: {text}", " ".join(attention))
        self.assertFalse([p for p in problems if "OAuth credential record" in p])

    def test_unobserved_facts_are_valid_inputs_never_a_block(self) -> None:
        """Management disabled, no journal, no quota data, no auth
        directory and a down gateway are unknown observations: the launch
        prepares, doctor adds no readiness finding and the seed choice falls
        back to DEFAULT_SEED with the warning."""

        self.served = None  # gateway down
        observations = self.runtime.readiness_observations()
        self.assertIsNone(observations.served)
        self.assertIsNone(observations.oauth_records)  # no auth directory at all
        problems, info, attention = self.doctor()
        self.assertFalse([a for a in attention if "next launch" in a], attention)
        self.assertIn("Readiness: 0 of", " ".join(info))
        self.assertIn(readiness.LEGEND, " ".join(info))
        name, warning = selection.default_profile(self.runtime)
        self.assertEqual(name, catalog.DEFAULT_SEED)
        self.assertTrue(warning.startswith("no profile is connected here ("), warning)
        self.assertTrue(warning.endswith(f"); using {catalog.DEFAULT_SEED} — P picks another, or P → N builds "
                                         "one from your connected providers"), warning)
        prepared = self.prepare(catalog.DEFAULT_SEED)
        self.assertFalse([n for n in prepared.notices if n.startswith("! ")])
        rows = self.runtime.lineup_readiness(prepared.lineup)
        self.assertTrue(all(row.state in (readiness.UNKNOWN, readiness.UNBOUND) for row in rows))
        # The fallback warning reaches the next fresh prepare once.
        self.runtime.selection_notices.append(warning)
        prepared = self.prepare(catalog.DEFAULT_SEED)
        self.assertIn(f"! {warning}", prepared.notices)
        self.assertEqual(self.runtime.selection_notices, [])

    def test_an_unserved_agent_or_lead_warns_and_never_blocks(self) -> None:
        self.sign_in(*sorted(set(self.pools().values())))
        prepared = self.prepare(catalog.DEFAULT_SEED)
        lineup = prepared.lineup
        agent = next(iter(lineup.agents.values())).binding
        for binding in (agent, lineup.lead.binding):
            alias = binding.selector.removesuffix("[1m]")
            with self.subTest(slot=binding.slot):
                self.served = {s for s in self.served if s.removesuffix("[1m]") != alias}
                prepared = self.prepare(catalog.DEFAULT_SEED)
                self.assertEqual(prepared.secret_problems, ())
                self.assertIn(f"! {profile.label(binding.slot)}: {alias} is not served by the local gateway — "
                              f"{readiness.SERVED_REMEDY}", prepared.notices)
                self.served = set(test_cli.served_selectors(test_cli.CATALOG_ROOT))

    def test_the_most_recently_used_profile_is_never_replaced(self) -> None:
        self.sign_in(*sorted(set(self.pools().values())))
        record = {"version": sessions.RECORD_VERSION, "cwd": str(self.runtime.cwd), "profile": "max",
                  "last_seen_at": "2026-09-30T00:00:00Z"}
        with mock.patch.object(session_facts, "_session_records", return_value=[record]):
            self.assertEqual(selection.default_profile(self.runtime), ("max", None))
        self.served = None  # even when nothing is ready
        with mock.patch.object(session_facts, "_session_records", return_value=[record]):
            self.assertEqual(selection.default_profile(self.runtime), ("max", None))

    def test_the_slot_verdicts_are_structured(self) -> None:
        binding = mock.Mock(key="k", provider="p", selector="alias-a[1m]", slot="cm-reviewer")
        pool = {"p": {"transport": {"kind": "oauth-pool", "pool": "codex"}}}
        cases = (
            (readiness.Observations(served=frozenset({"alias-a"}), oauth_records={"codex": 1}), readiness.READY),
            (readiness.Observations(served=None, oauth_records={"codex": 1}), readiness.UNKNOWN),
            (readiness.Observations(served=frozenset({"alias-a"}), oauth_records=None), readiness.UNKNOWN),
            (readiness.Observations(served=frozenset({"alias-a"}), oauth_records={"codex": 0}), readiness.BLOCKED),
            (readiness.Observations(served=frozenset({"other"}), oauth_records={"codex": 1}), readiness.BLOCKED),
            (readiness.Observations(served=frozenset({"alias-a"}), oauth_records={"codex": 1},
                                    credential_problems={"p": "provider p (p): no key"}), readiness.BLOCKED),
        )
        for observed, state_ in cases:
            with self.subTest(observed=observed):
                row = readiness.slot_readiness("cm-reviewer", binding, pool, observed)
                self.assertEqual(row.state, state_)
        row = readiness.slot_readiness("cm-reviewer", binding, pool, cases[-1][0])
        self.assertTrue(row.first.authority)
        self.assertEqual(readiness.unready_texts((row,)), [])  # authority names itself elsewhere
        self.assertEqual(readiness.slot_readiness("cm-reviewer", None, pool, cases[0][0]).state, readiness.UNBOUND)
        self.assertFalse(readiness.satisfiable((readiness.SlotReadiness("cm-lead", readiness.UNBOUND),)))
        self.assertEqual(readiness.LEGEND, "Ready means locally configured and served; upstream authentication, "
                                           "quota, and reachability may still fail.")


class LanTests(_Case):
    """A slot bound to a known-unreachable LAN line refuses a launch
    fast with the network-scoped text; unknown or reachable launches."""

    def lan_line(self) -> str:
        lcat = self.runtime.lineup_catalog()
        return next(key for key, entry in lcat.lines.items()
                    if readiness.is_lan(lcat.providers[entry["provider"]]) and entry.get("lead") is not None)

    def test_a_known_unreachable_lan_slot_refuses_fast(self) -> None:
        key = self.lan_line()
        lcat = self.runtime.lineup_catalog()
        base = lcat.providers[lcat.lines[key]["provider"]]["transport"]["base_url"]
        host = gateway_facts.lan_host(base)
        probes: list[str] = []
        self.runtime.lan_probe = lambda url: probes.append(url) or gateway_facts.LanReachability(
            "unreachable", host, "nxdomain")
        target = cli_types.LaunchTarget("ad-hoc", profile.ad_hoc_direct(key), None, False, f"Direct {key}")
        with self.assertRaises(cli_types.LaunchPlanError) as caught:
            self.runtime.prepare(target, action="fresh", passthrough=[])
        self.assertEqual(caught.exception.problems, (
            f"lead: {key}: not reachable from this network: {host} does not resolve — connect to the host's "
            "network, or bind another model",))
        self.assertEqual(probes, [base])
        # --print-launch (read-only) reports instead of refusing.
        prepared = self.runtime.prepare(target, action="fresh", passthrough=[], read_only=True)
        self.assertIn(f"! lead: not reachable from this network: {host} does not resolve — "
                      "connect to the host's network, or bind another model", prepared.notices)
        for state_ in ("reachable", "unknown"):
            self.runtime.lan_probe = lambda url, s=state_: gateway_facts.LanReachability(s, host)
            prepared = self.runtime.prepare(target, action="fresh", passthrough=[])
            self.assertFalse([n for n in prepared.notices if "reachable from this network" in n])

    def test_a_later_resolved_address_that_connects_launches(self) -> None:
        """The real probe behind the launch seam; a refused
        IPv6 address before a working IPv4 one never refuses the launch."""

        import socket
        import time

        key = self.lan_line()
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::10", 8000, 0, 0))
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8000))

        def connect(entry, _seconds):
            if entry[0] == socket.AF_INET6:
                raise ConnectionRefusedError()
            return mock.Mock()

        self.runtime.lan_probe = lambda url: gateway_facts.probe_lan(
            url, timeout=0.2, resolve=lambda *_: [v6, v4], connect=connect)
        target = cli_types.LaunchTarget("ad-hoc", profile.ad_hoc_direct(key), None, False, f"Direct {key}")
        prepared = self.runtime.prepare(target, action="fresh", passthrough=[])
        self.assertFalse([n for n in prepared.notices if "reachable from this network" in n], prepared.notices)

        # A first address that times out (dropped packets)
        # uses only its share of the deadline; the launch still proceeds.
        def drop_v6(entry, seconds):
            if entry[0] == socket.AF_INET6:
                time.sleep(seconds)
                raise socket.timeout()
            return mock.Mock()

        self.runtime.lan_probe = lambda url: gateway_facts.probe_lan(
            url, timeout=0.2, resolve=lambda *_: [v6, v4], connect=drop_v6)
        prepared = self.runtime.prepare(target, action="fresh", passthrough=[])
        self.assertFalse([n for n in prepared.notices if "reachable from this network" in n], prepared.notices)
        # The control: the same list with only the refused address refuses fast.
        self.runtime.lan_probe = lambda url: gateway_facts.probe_lan(
            url, timeout=0.2, resolve=lambda *_: [v6], connect=connect)
        with self.assertRaises(cli_types.LaunchPlanError):
            self.runtime.prepare(target, action="fresh", passthrough=[])

    def test_a_reachable_lan_lead_is_ready_on_the_card_seed_and_doctor_alike(self) -> None:
        """One computation: doctor observes the LAN providers
        of the profiles it reports, so a reachable LAN lead is ready there
        as it is for the launch; one refresh probes each provider once."""

        key = self.lan_line()
        lcat = self.runtime.lineup_catalog()
        base = lcat.providers[lcat.lines[key]["provider"]]["transport"]["base_url"]
        host = gateway_facts.lan_host(base)
        probes: list[str] = []
        self.runtime.lan_probe = lambda url: probes.append(url) or gateway_facts.LanReachability("reachable", host)
        document = dict(profile.ad_hoc_direct(key), name="lanlead")
        evaluation = profile.evaluate(document, lcat, bindings={}, effective=self.runtime.current_effective(),
                                      ad_hoc=False)
        self.assertEqual(evaluation.errors, ())
        rows = self.runtime.lineup_readiness(evaluation.lineup)
        self.assertEqual([(row.slot, row.state) for row in rows if row.slot == "cm-lead"],
                         [("cm-lead", readiness.READY)])
        self.assertTrue(readiness.satisfiable(rows))
        # The profile store is built per read: stub the class for the report.
        with mock.patch.object(profile.ProfileStore, "names", return_value=["lanlead"]), \
                mock.patch.object(profile.ProfileStore, "load", return_value=document):
            probes.clear()
            _attention, info = doctor_mod._doctor_readiness_report(self.runtime, True)
            self.assertIn("Readiness: 1 of 1 profile(s) locally ready", " ".join(info))
            self.assertEqual(probes, [base])
            # The whole doctor refresh (its LAN report and readiness report
            # share the observation) probes the provider once.
            probes.clear()
            _problems, _info, attention = self.doctor()
            self.assertEqual(probes.count(base), 1, probes)
            self.assertFalse([a for a in attention if "next launch" in a], attention)


class LanDoctorAttentionTests(test_discovery.DiscoveryCLICase):
    def test_a_live_binding_on_an_unreachable_lan_line_is_attention_never_block(self) -> None:
        code, _out, err = self.op(test_discovery.LAN_ADD)
        self.assertEqual(code, 0, err)
        code, _out, err = self.op(["models", "add", "lanbox", "lan-one", "--as", "custom-lan-one", "--context",
                                   "32768", "--source", "operator"])
        self.assertEqual(code, 0, err)
        self.runtime.lan_probe = lambda url: gateway_facts.LanReachability("unreachable", "box.lan", "nxdomain")
        sessions_dir = self.runtime.session_store.root / "sessions"
        state.ensure_private_dir(sessions_dir)
        for mid, event in (("abcdef01-0000-4000-8000-000000000000", "start"),
                           ("abcdef02-0000-4000-8000-000000000000", "end")):
            record = {"version": 4, "last_event_source": event, "applied": {
                "lead": {"key": "custom-lan-one", "selector": "custom-lan-one"}, "agents": {}}}
            state.atomic_write(sessions_dir / f"{mid}.json", strict_json.canonical_file_bytes(record))
        attention: list[str] = []
        info = doctor_mod._doctor_lan_report(self.runtime, attention)
        self.assertTrue(info)
        self.assertEqual(attention, [
            "session abcdef01: bound to custom-lan-one on provider lanbox, not reachable from this network: "
            "box.lan does not resolve — connect to the host's network, or bind another model (network-scoped; "
            "the session keeps its binding)"])
        problems, _info, attention = doctor_mod._collect_doctor_reports(self.runtime)
        self.assertFalse([p for p in problems if "box.lan" in p])
        self.assertTrue([a for a in attention if a.startswith("session abcdef01: bound to custom-lan-one")])


class StarterTests(_Case):
    def test_missing_roles_remain_unbound_and_preview_is_read_only(self) -> None:
        # Only one line is ready: a lead-capable line whose roles admit a
        # strict subset of the agent slots (chosen by shape).
        lcat = self.runtime.lineup_catalog()
        key = next(k for k, e in lcat.lines.items()
                   if isinstance(e["roles"], list) and e["roles"] and e.get("lead") is not None)
        entry = lcat.lines[key]
        self.sign_in(self.pools()[entry["provider"]])
        self.served = {selector.removesuffix("[1m]") for _l, selector, _c in catalog.line_selectors(entry)}
        before = sorted(str(p) for p in Path(self.runtime.environ["XDG_CONFIG_HOME"]).rglob("*"))
        out = io.StringIO()
        code = cli.main(["profile", "starter"], runtime=self.runtime, output_stream=out, interactive=False)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        # The exact legend precedes the exact preview block.
        self.assertTrue(text.startswith(readiness.LEGEND + "\nstarter preview — no profile written\n"), text)
        self.assertIn("\nNo model was admitted or qualified.\n", text)
        self.assertIn("save it: claude-multi profile starter --name starter --apply\n", text)
        self.assertNotIn("Save as", text)
        # Preview writes nothing.
        self.assertEqual(sorted(str(p) for p in Path(self.runtime.environ["XDG_CONFIG_HOME"]).rglob("*")), before)
        self.assertFalse(self.runtime.profiles.contains("starter"))
        self.assertNotIn("starter", self.runtime.catalog.seed_profiles)
        plan, _warnings = self.runtime.starter_plan("starter")
        self.assertEqual(plan.document["lead"]["model"], key)
        # Starter never fabricates eligibility: only slots the line's roles
        # admit are bound; every other template slot stays unbound, named.
        self.assertEqual(set(plan.document["agents"]) - set(entry["roles"]), set())
        template = self.runtime.catalog.seed_profiles[readiness.STARTER_TEMPLATE]["agents"]
        self.assertEqual(set(plan.unresolved), set(template) - set(plan.document["agents"]))
        self.assertTrue(plan.unresolved)
        for slot in plan.unresolved:
            self.assertIn(f"{profile.label(slot)} unbound (no ready line admits it)", text)
            self.assertIn(f"{profile.label(slot)}: {template[slot]['model']} · {template[slot]['effort']} -> unbound\n",
                          text)

    def test_apply_confirms_saves_once_and_refuses_an_existing_name(self) -> None:
        self.sign_in(*sorted(set(self.pools().values())))
        code, out = self.run_cli(["profile", "starter", "--apply"], "n\n")
        self.assertEqual(code, 1)
        self.assertIn("Save as starter? [y/N] Nothing written.", out)
        self.assertFalse(self.runtime.profiles.contains("starter"))
        code, out = self.run_cli(["profile", "starter", "--apply"], "y\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Saved profile 'starter'", out)
        saved = self.runtime.profiles.load("starter")
        evaluation = profile.evaluate(saved, self.runtime.lineup_catalog(), bindings={},
                                      effective=self.runtime.current_effective())
        self.assertEqual(evaluation.errors, ())
        # With every pool ready the template's exact bindings are kept.
        template = self.runtime.catalog.seed_profiles[readiness.STARTER_TEMPLATE]
        self.assertEqual(saved["lead"], template["lead"])
        self.assertEqual(saved["agents"], template["agents"])
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code, out = self.run_cli(["profile", "starter", "--apply"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("profile starter: profile 'starter' already exists; choose another --name — nothing written",
                      err.getvalue())
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code, _out = self.run_cli(["profile", "starter", "--name", catalog.DEFAULT_SEED])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err.getvalue())

    def test_no_ready_lead_refuses(self) -> None:
        self.served = None
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code, out = self.run_cli(["profile", "starter"], interactive=False)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("starter: no ready lead line on this machine", err.getvalue())


if __name__ == "__main__":
    import unittest

    unittest.main()
