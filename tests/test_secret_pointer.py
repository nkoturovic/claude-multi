"""The key file every reader shares: the environment override, then a
validated pointer, then the default; the gateway's start render and spawn
read the same file."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from claude_multi import gateway_lifecycle, paths, proxy, secret_store, state
from claude_multi.platform import systemd_unit

from _catalog import FIXTURE_ROOT


class PointerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-pointer-")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home)}

    def key_file(self, relative: str = ".config/secrets/claude.env",
                 data: bytes = b"KIMI_CLAUDE_API_KEY=pointed-dummy-value\n") -> Path:
        path = self.home / relative
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, data)
        return path

    def pointer(self, document: object) -> Path:
        target = paths.secret_pointer_path(self.env)
        state.ensure_private_dir(target.parent)
        state.atomic_write(target, json.dumps(document).encode())
        return target


class PrecedenceTests(PointerCase):
    def test_default_path_is_home_relative_under_the_product_folder(self) -> None:
        self.assertEqual(secret_store.secret_env_path(self.env),
                         self.home / ".config/claude-multi/secrets/provider-keys.env")
        self.assertEqual(paths.secret_pointer_path(self.env), self.home / ".config/claude-multi/secret-file.json")
        # XDG_CONFIG_HOME moves settings, never the key file (the gateway reads it too).
        moved = {**self.env, "XDG_CONFIG_HOME": str(self.root / "xdg")}
        self.assertEqual(secret_store.secret_env_path(moved), secret_store.secret_env_path(self.env))
        self.assertEqual(secret_store.key_file_location(self.env).source, "default")

    def test_environment_then_pointer_then_default(self) -> None:
        pointed = self.key_file()
        secret_store.write_pointer(self.env, pointed)
        self.assertEqual(json.loads(paths.secret_pointer_path(self.env).read_text()),
                         {"version": 1, "path": "~/.config/secrets/claude.env"})
        location = secret_store.key_file_location(self.env)
        self.assertEqual((location.path, location.source), (pointed, "pointer"))
        override = {**self.env, secret_store.SECRET_ENV_OVERRIDE: str(self.root / "other.env")}
        location = secret_store.key_file_location(override)
        self.assertEqual((location.path, location.source), (self.root / "other.env", "environment"))
        # The supervised service drops the override, so it follows the pointer.
        self.assertIn(secret_store.SECRET_ENV_OVERRIDE, systemd_unit.UNSET_ENVIRONMENT)
        self.assertEqual(secret_store.service_key_file(override).path, pointed)
        self.assertEqual(secret_store.default_store(self.env).get("KIMI_CLAUDE_API_KEY"), "pointed-dummy-value")

    def test_an_invalid_pointer_raises_and_never_falls_back(self) -> None:
        self.key_file(".config/claude-multi/secrets/provider-keys.env", b"KIMI_CLAUDE_API_KEY=default-dummy\n")
        cases = {
            "not JSON": b"{nope",
            "wrong version": json.dumps({"version": 2, "path": "~/.config/secrets/claude.env"}).encode(),
            "extra field": json.dumps({"version": 1, "path": "~/.config/secrets/x", "x": 1}).encode(),
            "relative": json.dumps({"version": 1, "path": "keys.env"}).encode(),
            "outside the protected folders": json.dumps({"version": 1, "path": "~/keys.env"}).encode(),
            "outside HOME": json.dumps({"version": 1, "path": "/etc/keys.env"}).encode(),
        }
        target = paths.secret_pointer_path(self.env)
        for label, raw in cases.items():
            with self.subTest(case=label):
                state.atomic_write(target, raw)
                with self.assertRaises(secret_store.SecretStoreError) as caught:
                    secret_store.secret_env_path(self.env)
                self.assertIn("key file pointer ~/.config/claude-multi/secret-file.json is unreadable", str(caught.exception))
                self.assertEqual(caught.exception.remedy, "fix or delete ~/.config/claude-multi/secret-file.json")
                # Readers fail closed too (never the default file).
                with self.assertRaises(proxy.ProxyError):
                    proxy.resolve_secret("KIMI_CLAUDE_API_KEY", environ=self.env)
        os.chmod(target, 0o644)
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.secret_env_path(self.env)

    def test_a_symlinked_folder_is_judged_where_it_leads(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)  # Exercise shared access even under umask 077.
        (self.home / ".config").mkdir(mode=0o700)
        (self.home / ".config" / "secrets").symlink_to(outside)
        self.assertIsNotNone(secret_store.location_problem("~/.config/secrets/claude.env", self.env))
        os.chmod(outside, 0o700)
        self.assertIsNone(secret_store.location_problem("~/.config/secrets/claude.env", self.env))
        self.assertIsNone(secret_store.location_problem("~/.config/claude-multi/secrets/k.env", self.env))

    def test_no_folder_of_one_person_ships_as_a_protected_location(self) -> None:
        from claude_multi import scope

        self.assertEqual(secret_store.PROTECTED_DIRS, (".config/claude-multi",))
        self.assertFalse([rule for rule in scope.SECRET_PATH_DENIES if ".config/secrets" in rule])

    def test_a_pointer_to_a_private_folder_of_your_choice_is_accepted(self) -> None:
        # The shape a person may already use: ~/.config/secrets/claude.env,
        # a private folder and file of their own.
        from claude_multi import scope

        pointed = self.key_file(".config/secrets/claude.env")
        self.assertEqual(stat.S_IMODE(pointed.parent.stat().st_mode), 0o700)
        secret_store.write_pointer(self.env, pointed)
        location = secret_store.key_file_location(self.env)
        self.assertEqual((location.path, location.source), (pointed, "pointer"))
        self.assertEqual(secret_store.default_store(self.env).get("KIMI_CLAUDE_API_KEY"), "pointed-dummy-value")
        self.assertEqual(secret_store.check_key_file(pointed, self.env), {"KIMI_CLAUDE_API_KEY": "pointed-dummy-value"})
        # Managed sessions are denied that file by name, with the pointer as the cause.
        causes = dict(scope.secret_deny_causes(self.env, wsl=False))
        for verb in ("Read", "Edit"):
            self.assertIn("pointer", causes[f"{verb}(~/.config/secrets/claude.env)"])
        self.assertIn(pointed, secret_store.credential_locations(self.env))

    def test_a_folder_or_file_others_can_reach_is_refused(self) -> None:
        shared = self.home / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        problem = secret_store.location_problem("~/shared/keys.env", self.env)
        self.assertIn("private folder", problem or "")
        with self.assertRaises(ValueError):
            secret_store.write_pointer(self.env, shared / "keys.env")
        loose = self.key_file(".config/keys/loose.env")
        os.chmod(loose, 0o644)
        self.assertIn("private regular file", secret_store.location_problem(loose, self.env) or "")
        # The home folder itself is no key folder, whatever its mode.
        self.assertIsNotNone(secret_store.location_problem("~/keys.env", self.env))
        self.assertFalse(paths.secret_pointer_path(self.env).exists())

    def test_check_key_file_validates_without_echoing_values(self) -> None:
        good = self.key_file(data=b"A_KEY=dummy-1\nB_KEY=dummy-2\n")
        self.assertEqual(sorted(secret_store.check_key_file(good, self.env)), ["A_KEY", "B_KEY"])
        bad = self.key_file(".config/secrets/bad.env", b"A_KEY=dummy-1\nnot an assignment\n")
        with self.assertRaises(secret_store.SecretStoreError) as caught:
            secret_store.check_key_file(bad, self.env)
        self.assertIn(":2: malformed assignment line", str(caught.exception))
        self.assertNotIn("dummy-1", str(caught.exception))
        shared = self.key_file(".config/secrets/shared.env", b"A_KEY=dummy-1\n")
        os.chmod(shared, 0o644)
        with self.assertRaises(secret_store.SecretStoreError):
            secret_store.check_key_file(shared, self.env)
        with self.assertRaises(secret_store.SecretStoreError) as caught:
            secret_store.check_key_file(self.home / "loose.env", self.env)
        self.assertIn("not your home folder itself", str(caught.exception))

    def test_every_selectable_location_is_denied_to_managed_sessions(self) -> None:
        from claude_multi import scope

        for relative in secret_store.PROTECTED_DIRS:
            self.assertIn(f"Read(~/{relative}/**)", scope.SECRET_PATH_DENIES)
        outside = self.root / "elsewhere" / "keys.env"
        outside.parent.mkdir(mode=0o700)
        outside.write_bytes(b"A_KEY=dummy-1\n")
        linked = self.home / "work" / "keys.env"
        linked.parent.mkdir(mode=0o700)
        linked.symlink_to(outside)
        cases = {
            "default": dict(self.env),
            "pointer": dict(self.env),
            "environment, outside HOME": {**self.env, secret_store.SECRET_ENV_OVERRIDE: str(outside)},
            "environment, inside HOME": {**self.env, secret_store.SECRET_ENV_OVERRIDE: str(self.home / "keys.env")},
            "environment, a Windows drive": {**self.env,
                                             secret_store.SECRET_ENV_OVERRIDE: "/mnt/c/Users/user/keys.env"},
            "environment, a link": {**self.env, secret_store.SECRET_ENV_OVERRIDE: str(linked)},
        }
        for label, env in cases.items():
            with self.subTest(case=label):
                if label == "pointer":
                    secret_store.write_pointer(env, self.key_file())
                location = secret_store.key_file_location(env)
                denies = scope.secret_path_denies(env)
                files = [location.path, Path(os.path.realpath(location.path))]
                for path in files:
                    self.assertTrue(denied(path, denies, self.home, "Read"), (label, path, denies))
                    if location.source == "environment":
                        self.assertTrue(denied(path, denies, self.home, "Edit"), (label, path, denies))
                if label == "default":
                    # The folder rule already covers it: nothing is added.
                    self.assertEqual(denies, scope.SECRET_PATH_DENIES)
                if label == "pointer":
                    # The pointed file outside claude-multi's folder is denied by name.
                    self.assertEqual(denies, (*scope.SECRET_PATH_DENIES, "Read(~/.config/secrets/claude.env)",
                                              "Edit(~/.config/secrets/claude.env)"))
                    paths.secret_pointer_path(env).unlink(missing_ok=True)
        self.assertIn("Read(//mnt/c/Users/user/keys.env)",
                      scope.secret_path_denies(cases["environment, a Windows drive"]))
        self.assertIn("Read(~/keys.env)", scope.secret_path_denies(cases["environment, inside HOME"]))

    def test_the_compiled_session_denies_the_selected_key_file(self) -> None:
        import test_compiler_v2

        outside = self.root / "elsewhere.env"
        _lineup, result = test_compiler_v2._seed_launch(
            launch_environ={**self.env, secret_store.SECRET_ENV_OVERRIDE: str(outside)})
        deny = result.scope_plan.settings["permissions"]["deny"]
        self.assertIn(f"Read(/{outside})", deny)
        self.assertIn(f"Edit(/{outside})", deny)
        _lineup, plain = test_compiler_v2._seed_launch(launch_environ=dict(self.env))
        self.assertNotIn(f"Read(/{outside})", plain.scope_plan.settings["permissions"]["deny"])


def denied(path: Path, rules: tuple[str, ...], home: Path, verb: str) -> bool:
    """Whether a rule of the client's path syntax covers ``path``: ``~/x``
    (HOME-relative) or ``//x`` (absolute), exact or ``/**`` below."""

    for rule in rules:
        if not rule.startswith(f"{verb}(") or not rule.endswith(")"):
            continue
        spec = rule[len(verb) + 1:-1]
        if spec.startswith("~/"):
            base = home / spec[2:]
        elif spec.startswith("//"):
            base = Path(spec[1:])
        else:
            continue
        if str(base).endswith("/**"):
            if Path(str(base)[:-3]) in path.parents:
                return True
        elif base == path:
            return True
    return False


class GatewayReadsThePointedFileTests(PointerCase):
    """The start render (what ``run --prepare-and-exec`` and the logins do
    before the gateway runs) resolves keys through the shared reader."""

    def setUp(self) -> None:
        super().setUp()
        self.env.update({"CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT),
                         "XDG_STATE_HOME": str(self.root / "state")})

    def render(self) -> str:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            target, _result, _report = proxy.render_runtime_config(
                self.home, environ=dict(self.env), state_root=self.root / "state" / "claude-multi", policy="start")
        return target.read_text()

    def test_start_render_uses_the_pointed_file(self) -> None:
        self.key_file(".config/claude-multi/secrets/provider-keys.env", b"KIMI_CLAUDE_API_KEY=default-file-key\n")
        self.assertIn("default-file-key", self.render())
        pointed = self.key_file(data=b"KIMI_CLAUDE_API_KEY=pointed-file-key\n")
        secret_store.write_pointer(self.env, pointed)
        config = self.render()
        self.assertIn("pointed-file-key", config)
        self.assertNotIn("default-file-key", config)

    def test_a_bad_pointer_refuses_the_render(self) -> None:
        self.pointer({"version": 1, "path": "~/nowhere.env"})
        with self.assertRaises(proxy.ProxyError) as caught:
            self.render()
        self.assertEqual(caught.exception.remedy, "fix or delete ~/.config/claude-multi/secret-file.json")

    def test_the_spawned_gateway_keeps_home_so_it_reads_the_same_pointer(self) -> None:
        pointed = self.key_file()
        secret_store.write_pointer(self.env, pointed)
        spawned = gateway_lifecycle.spawn_environment(self.env, self.home)
        self.assertEqual(spawned["HOME"], str(self.home))
        self.assertEqual(secret_store.secret_env_path(spawned), pointed)


if __name__ == "__main__":
    unittest.main()
