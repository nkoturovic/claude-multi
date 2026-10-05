"""Tests for the durable per-session scope: compile, write, gate, goldens."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, compiler, layout, scope, state, strict_json
from claude_multi.scope import ScopeError
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _golden import assertGolden
from _layout import REPO_ROOT


CATALOG_ROOT = FIXTURE_ROOT
GOLDENS = GOLDENS_ROOT / "default"
FIXED_SESSION = "11111111-1111-4111-8111-111111111111"


def _bundle():
    return catalog.load_catalog(CATALOG_ROOT)


def _frontmatter(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8")
    lines = text.splitlines()
    assert lines[0] == "---"
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


class WriteScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-scope-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        # The 2.x compile path is gone; the effects are plan-agnostic.
        import bless

        self.plan = bless.v2_scope_plan("managed")

    def test_writes_tree_with_private_modes(self) -> None:
        live = scope.write_scope(self.root, FIXED_SESSION, self.plan)
        self.assertEqual(live, self.root / "scopes" / FIXED_SESSION)
        self.assertFalse((self.root / "scopes" / f".{FIXED_SESSION}.new").exists())
        for dirpath in (
            self.root / "scopes",
            live,
            live / ".claude",
            live / ".claude" / "agents",
        ):
            mode = stat.S_IMODE(os.lstat(dirpath).st_mode)
            self.assertEqual(mode, 0o700, f"{dirpath}: {oct(mode)}")
        for relpath, data in self.plan.agent_files.items():
            target = live.joinpath(*relpath.split("/"))
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o600)
        settings = live / "settings.json"
        self.assertEqual(
            settings.read_bytes(), strict_json.canonical_file_bytes(self.plan.settings)
        )
        self.assertEqual(stat.S_IMODE(os.lstat(settings).st_mode), 0o600)


    def test_stale_staging_dir_removed_first(self) -> None:
        staging = self.root / "scopes" / f".{FIXED_SESSION}.new"
        staging.mkdir(parents=True)
        os.chmod(self.root / "scopes", 0o700)
        os.chmod(staging, 0o700)
        (staging / "junk").write_bytes(b"junk")
        os.chmod(staging / "junk", 0o600)
        live = scope.write_scope(self.root, FIXED_SESSION, self.plan)
        self.assertFalse(staging.exists())
        self.assertFalse((live / "junk").exists())

    def test_symlinked_live_scope_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        scopes_root = self.root / "scopes"
        scopes_root.mkdir()
        os.chmod(scopes_root, 0o700)
        (scopes_root / FIXED_SESSION).symlink_to(outside)
        with self.assertRaises(state.StateError):
            scope.write_scope(self.root, FIXED_SESSION, self.plan)

    def test_traversal_relpath_refused(self) -> None:
        plan = scope.ScopePlan(
            agent_files={"../escape.md": b"x"}, settings=dict(self.plan.settings)
        )
        with self.assertRaises(ScopeError):
            scope.write_scope(self.root, FIXED_SESSION, plan)

    def test_scope_paths_require_uuid4(self) -> None:
        with self.assertRaisesRegex(ScopeError, "UUIDv4"):
            scope.scope_dir(self.root, "safe-but-not-a-uuid")
        with self.assertRaisesRegex(ScopeError, "UUIDv4"):
            scope.write_scope(self.root, "safe-but-not-a-uuid", self.plan)
        with self.assertRaisesRegex(ScopeError, "UUIDv4"):
            scope.remove_scope(self.root, "safe-but-not-a-uuid")

    def test_remove_scope(self) -> None:
        scope.write_scope(self.root, FIXED_SESSION, self.plan)
        self.assertTrue(scope.remove_scope(self.root, FIXED_SESSION))
        self.assertFalse((self.root / "scopes" / FIXED_SESSION).exists())
        self.assertFalse(scope.remove_scope(self.root, FIXED_SESSION))

    def test_remove_scope_clears_staging(self) -> None:
        staging = self.root / "scopes" / f".{FIXED_SESSION}.new"
        staging.mkdir(parents=True)
        os.chmod(self.root / "scopes", 0o700)
        self.assertTrue(scope.remove_scope(self.root, FIXED_SESSION))
        self.assertFalse(staging.exists())


class CollisionGateTests(unittest.TestCase):
    # The generated ids the 2.x default composition used to compile (that
    # path is gone); the gate itself is id-agnostic.
    NAMES = frozenset({
        "cm-analyst-kimi-k3-max", "cm-analyst-sol-high", "cm-implementer-kimi-k3-max",
        "cm-implementer-sol-high", "cm-reviewer-opus55-xhigh", "cm-reviewer-sol-xhigh",
    })

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-gate-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.names = self.NAMES

    def _agent_file(self, directory: Path, name: str, *, stem: str | None = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem or name}.md"
        path.write_text(f"---\nname: {name}\n---\n\nbody\n")
        return path

    def test_project_tree_collision_found(self) -> None:
        project = self.root / "repo"
        cwd = project / "sub" / "dir"
        cwd.mkdir(parents=True)
        (project / ".git").mkdir(parents=True)
        offender = self._agent_file(
            project / ".claude" / "agents", "cm-analyst-sol-high"
        )
        collisions = scope.find_cm_collisions(cwd, (), self.names)
        self.assertEqual(collisions, [(offender, "cm-analyst-sol-high")])

    def test_agent_directories_are_scanned_recursively(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        offender = self._agent_file(
            project / ".claude" / "agents" / "review" / "security",
            "cm-analyst-sol-high",
        )
        collisions = scope.find_cm_collisions(project, (), self.names)
        self.assertEqual(collisions, [(offender, "cm-analyst-sol-high")])

    def test_walk_stops_after_git_root(self) -> None:
        project = self.root / "repo"
        cwd = project / "sub"
        cwd.mkdir(parents=True)
        (project / ".git").mkdir(parents=True)
        above = self._agent_file(
            self.root / ".claude" / "agents", "cm-analyst-sol-high"
        )
        collisions = scope.find_cm_collisions(cwd, (), self.names)
        self.assertEqual(collisions, [])
        # Without the git marker the walk continues to the filesystem root.
        (project / ".git").rmdir()
        collisions = scope.find_cm_collisions(cwd, (), self.names)
        self.assertEqual(collisions, [(above, "cm-analyst-sol-high")])

    def test_exact_match_only(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        self._agent_file(project / ".claude" / "agents", "cm-unrelated")
        self._agent_file(project / ".claude" / "agents", "cm-analyst-sol-high-x")
        self._agent_file(project / ".claude" / "agents", "other-agent")
        collisions = scope.find_cm_collisions(project, (), self.names)
        self.assertEqual(collisions, [])

    def test_passthrough_add_dir_scanned(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        extra = self.root / "extra"
        offender = self._agent_file(
            extra / ".claude" / "agents", "cm-reviewer-sol-xhigh"
        )
        collisions = scope.find_cm_collisions(project, (extra,), self.names)
        self.assertEqual(collisions, [(offender, "cm-reviewer-sol-xhigh")])

    def test_managed_dir_scanned_when_present(self) -> None:
        managed = self.root / "managed"
        offender = self._agent_file(managed, "cm-implementer-sol-high")
        collisions = scope.find_cm_collisions(
            self.root, (), self.names, managed_agents_dir=managed
        )
        self.assertIn((offender, "cm-implementer-sol-high"), collisions)

    def test_managed_dir_absent_is_not_an_error(self) -> None:
        collisions = scope.find_cm_collisions(
            self.root,
            (),
            self.names,
            managed_agents_dir=self.root / "no-such-dir",
        )
        self.assertEqual(collisions, [])

    def test_quoted_name_and_missing_frontmatter(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        agents = project / ".claude" / "agents"
        offender = self._agent_file(agents, '"cm-analyst-sol-high"', stem="quoted")
        self._agent_file(agents, "no-frontmatter", stem="plain").write_text("no markers\n")
        collisions = scope.find_cm_collisions(project, (), self.names)
        self.assertEqual(collisions, [(offender, "cm-analyst-sol-high")])

    def test_inline_yaml_comments_do_not_hide_collisions(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        unquoted = agents / "unquoted.md"
        unquoted.write_text(
            "---\nname: cm-analyst-sol-high # project override\n---\n\nbody\n"
        )
        quoted = agents / "quoted-comment.md"
        quoted.write_text(
            '---\nname: "cm-reviewer-sol-xhigh" # project override\n---\n\nbody\n'
        )
        collisions = scope.find_cm_collisions(project, (), self.names)
        self.assertEqual(
            collisions,
            [
                (quoted, "cm-reviewer-sol-xhigh"),
                (unquoted, "cm-analyst-sol-high"),
            ],
        )

    def test_non_md_files_ignored(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "notes.txt").write_text("name: cm-analyst-sol-high\n")
        collisions = scope.find_cm_collisions(project, (), self.names)
        self.assertEqual(collisions, [])

    def test_result_sorted_deterministically(self) -> None:
        project = self.root / "repo"
        (project / ".git").mkdir(parents=True)
        agents = project / ".claude" / "agents"
        self._agent_file(agents, "cm-reviewer-opus55-xhigh")
        self._agent_file(agents, "cm-analyst-sol-high")
        collisions = scope.find_cm_collisions(project, (), self.names)
        self.assertEqual(
            [name for _, name in collisions],
            ["cm-analyst-sol-high", "cm-reviewer-opus55-xhigh"],
        )


if __name__ == "__main__":
    unittest.main()


class HookShimTests(unittest.TestCase):
    """Stable lifecycle-hook indirection (store-path volatility root fix)."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-shim-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_scope_bytes_stable_across_package_rebuilds(self) -> None:
        """The money test on the v2 path: a
        rebuilt package (new store path) must not change the compiled scope
        bytes for the same lineup — only the protocol-3 shim's target moves."""
        from claude_multi import profile, settings

        bundle = _bundle()
        lcat = profile.LineupCatalog.from_docs(bundle.docs)
        eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
        lineup = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff)

        def compile_with(environ):
            resolved_cmd = scope.resolve_hook_command(environ)
            shim = scope.ensure_hook_shim_v3(self.root, resolved_cmd)
            return scope.compile_lineup_scope(
                lineup,
                lcat,
                eff,
                bundle.prompt_bodies,
                scope.catalog_meta_v2(bundle.docs),
                lineup_generation=1,
                managed_id=FIXED_SESSION,
                hook_command=str(shim),
                launch_epoch=1,
                token_helper_command="/state/bin/claude-multi-gateway-token",
            )

        plan_a = compile_with({"CLAUDE_MULTI_HOOK_COMMAND": "/nix/store/gen95/bin/claude-multi"})
        plan_b = compile_with({"CLAUDE_MULTI_HOOK_COMMAND": "/nix/store/gen96/bin/claude-multi"})
        self.assertEqual(scope.plan_hash(plan_a), scope.plan_hash(plan_b))
        shim_body = scope.hook_shim_v3_path(self.root).read_text()
        self.assertIn("/nix/store/gen96/bin/claude-multi", shim_body)
        self.assertNotIn("/nix/store/gen95/bin/claude-multi", shim_body)

    def test_shim_written_executable_and_idempotent(self) -> None:
        path = scope.ensure_hook_shim(self.root, "/nix/store/a/bin/claude-multi")
        self.assertEqual(path, scope.hook_shim_path(self.root))
        info = os.lstat(path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode) & 0o111, 0o100)
        body = path.read_text()
        self.assertIn("exec /nix/store/a/bin/claude-multi", body)
        self.assertIn("command -v claude-multi", body)
        before = path.read_bytes()
        again = scope.ensure_hook_shim(self.root, "/nix/store/a/bin/claude-multi")
        self.assertEqual(again, path)
        self.assertEqual(path.read_bytes(), before)

    def test_shim_refreshes_when_resolved_command_changes(self) -> None:
        path = scope.ensure_hook_shim(self.root, "/nix/store/a/bin/claude-multi")
        scope.ensure_hook_shim(self.root, "/nix/store/b/bin/claude-multi")
        body = path.read_text()
        self.assertIn("/nix/store/b/bin/claude-multi", body)
        self.assertNotIn("/nix/store/a/bin/claude-multi", body)

    def test_shim_target_is_shell_quoted(self) -> None:
        path = scope.ensure_hook_shim(self.root, "/nix/store/a b/bin/claude-multi")
        body = path.read_text()
        self.assertIn("'/nix/store/a b/bin/claude-multi'", body)

    def test_shim_requires_a_resolved_command(self) -> None:
        with self.assertRaises(ScopeError):
            scope.ensure_hook_shim(self.root, "")

    def test_resolve_hook_command_precedence(self) -> None:
        env = {"CLAUDE_MULTI_HOOK_COMMAND": "/some/wrapper/../wrapper/claude-multi"}
        self.assertEqual(scope.resolve_hook_command(env), "/some/wrapper/claude-multi")
        # Without the wrapper's command: the running installation's own entry
        # point (here the source tree's bin/), never a resource directory.
        self.assertEqual(scope.resolve_hook_command({}), str((REPO_ROOT / "bin" / "claude-multi").resolve()))
        self.assertEqual(scope.resolve_hook_command({}, installation=Path("/install")),
                         "/install/bin/claude-multi")
        with mock.patch.object(layout, "installation", return_value=None), self.assertRaises(ScopeError):
            scope.resolve_hook_command({})

    def test_a_resource_override_never_moves_the_hook_command(self) -> None:
        for env in ({"CLAUDE_MULTI_ASSETS": "/assets"}, {"CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}):
            with self.subTest(env=env):
                self.assertEqual(scope.resolve_hook_command(env), scope.resolve_hook_command({}))
                self.assertNotIn(env["CLAUDE_MULTI_ASSETS"], scope.resolve_hook_command(env))


    def test_shim_bytes_are_the_pure_text(self) -> None:
        # The 2.x text is factored into scope.hook_shim_text with its
        # bytes unchanged (golden-pinned; the live 2.26 shims match it).
        command = "/nix/store/a/bin/claude-multi"
        path = scope.ensure_hook_shim(self.root, command)
        self.assertEqual(path.read_bytes(), scope.hook_shim_text(command))
        assertGolden(
            self,
            GOLDENS_ROOT / "shim" / "claude-multi-hook",
            scope.hook_shim_text("/nix/store/fixture/bin/claude-multi"),
        )
        with self.assertRaises(ScopeError):
            scope.hook_shim_text("")

    def test_shim_repairs_a_lost_exec_bit(self) -> None:
        # Crash-window: atomic_write leaves 0600 before chmod; the next
        # ensure must restore 0700 even when the content is unchanged.
        path = scope.ensure_hook_shim(self.root, "/nix/store/a/bin/claude-multi")
        os.chmod(path, 0o600)
        scope.ensure_hook_shim(self.root, "/nix/store/a/bin/claude-multi")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)


class GatewayTokenShimTests(unittest.TestCase):
    """apiKeyHelper indirection: gateway auth survives daemon env scrubbing."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-token-shim-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_shim_written_executable_and_idempotent(self) -> None:
        token_path = self.root / "cfg" / "api-key"
        path = scope.ensure_gateway_token_shim(self.root, token_path)
        self.assertEqual(path, scope.gateway_token_shim_path(self.root))
        info = os.lstat(path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        body = path.read_text()
        self.assertIn(f"exec cat {token_path}", body)
        before = path.read_bytes()
        scope.ensure_gateway_token_shim(self.root, token_path)
        self.assertEqual(path.read_bytes(), before)

    def test_shim_mode_repair_is_unconditional(self) -> None:
        path = scope.ensure_gateway_token_shim(self.root, self.root / "api-key")
        os.chmod(path, 0o600)
        scope.ensure_gateway_token_shim(self.root, self.root / "api-key")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)

    def test_shim_refreshes_when_token_path_changes(self) -> None:
        path = scope.ensure_gateway_token_shim(self.root, self.root / "a" / "api-key")
        scope.ensure_gateway_token_shim(self.root, self.root / "b" / "api-key")
        body = path.read_text()
        self.assertIn("/b/", body)
        self.assertNotIn("/a/", body)

    def test_shim_never_embeds_the_token_value(self) -> None:
        token_path = self.root / "api-key"
        token_path.write_text("sentinel-secret-value\n")
        os.chmod(token_path, 0o600)
        path = scope.ensure_gateway_token_shim(self.root, token_path)
        self.assertNotIn("sentinel-secret-value", path.read_text())

    def test_resolve_gateway_token_path(self) -> None:
        env = {"HOME": "/home/test"}
        self.assertEqual(
            scope.resolve_gateway_token_path(env),
            Path("/home/test/.config/claude-multi/api-key"),
        )
        # XDG_CONFIG_HOME is deliberately ignored: the token writer
        # (proxy.config_dir) and reader (launch via gateway.token_file) are
        # strictly HOME-relative — an XDG shim would point at an unpopulated
        # file.
        env = {"HOME": "/home/test", "XDG_CONFIG_HOME": "/xdg"}
        self.assertEqual(
            scope.resolve_gateway_token_path(env),
            Path("/home/test/.config/claude-multi/api-key"),
        )


class GatewayRoutingDurabilityTests(unittest.TestCase):
    """Compiled settings carry non-secret routing + helper, never the token."""


    def test_api_key_helper_is_inside_the_allowlist(self) -> None:
        self.assertIn("apiKeyHelper", scope.COMPILED_SETTINGS_KEYS)
