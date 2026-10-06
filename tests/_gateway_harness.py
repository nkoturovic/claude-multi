"""Offline gateway harness. No production state, ambient network or providers.

The ordinary unittest entry point finalizes each evidence module in its
tearDownModule; --run additionally checks the complete inventory and refuses
skips. atexit is ONLY a process/fixture cleanup backstop, never publication.
"""
from __future__ import annotations

import argparse
import atexit
import copy
import http.client
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_ROOT, logical_path
from _layout import SERVICE_SPEC

if __name__ == "__main__":
    sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "src")]

from claude_multi import catalog, probe, render, state
from _gateway import NIX_STORE, _gateway_binary, diagnostic_problem, sha256_file

REQUIRE_ENV = "CLAUDE_MULTI_TEST_REQUIRE_GATEWAY"
BINARY_ENV = "CLAUDE_MULTI_TEST_CLI_PROXY_API"
EVIDENCE_ENV = "CLAUDE_MULTI_TEST_GATEWAY_EVIDENCE"
GATEWAY_CHECK_MODULES = (
    "tests.test_gateway_harness", "tests.test_gateway_hot_reload",
    "tests.test_proxy", "tests.test_gateway_unit", "tests.test_gateway_unit_view",
    "tests.test_gateway_contracts", "tests.test_gateway_hints", "tests.test_gateway_clamp",
    "tests.test_gateway_patches",
)
GATEWAY_CHECK_EXCLUSIONS = {
    "test_gateway_seam", "test_gateway_auth_tools", "test_gateway_hint_client", "test_gateway_retry_after",
    "test_gateway_keyed_compat",  # the keyed core selected explicitly, full audit host-only
    "test_gateway_regressions",  # Observation-only, separately timed lane
    # The lifecycle, its doctor facts and the service hand-off run against fake
    # worlds and stub managers in the main suite; they need no gateway binary.
    "test_gateway_lifecycle", "test_gateway_doctor", "test_gateway_service",
}
# Later matrix legs extend this closed inventory together with their tests.
REQUIRED_EVIDENCE = {
    "tests.test_gateway_harness": {"smoke": {"readiness", "version", "claude", "codex", "compat", "listeners"}},
}
CODEX = "cliproxy-oauth-codex-v1"
CLAUDE = "cliproxy-claude-compatible-v1"
COMPAT = "cliproxy-openai-compat-v1"
# Single-omission proof, not a claim that patch hunks or effects are disjoint.
STARTUP_PROBE_ENV = "CLAUDE_MULTI_TEST_GATEWAY_STARTUP_PROBE"
PATCH_ROWS = {
    "cli-proxy-api-loopback-oauth.patch": (
        "tests.test_gateway_patches.LoopbackOAuthListenerTests.test_claude_login_callback_listens_on_loopback_only",
        "tests.test_gateway_patches.LoopbackOAuthListenerTests.test_codex_login_callback_listens_on_loopback_only",
    ),
    "cli-proxy-api-kimi-claude-compat.patch": (
        "tests.test_gateway_contracts.KimiCompatAuthTests.test_header_auth_sends_x_api_key_only",
        "tests.test_gateway_contracts.KimiCompatAuthTests.test_custom_authorization_header_never_displaces_x_api_key",
        "tests.test_gateway_contracts.KimiCompatAuthTests.test_models_listing_carries_owned_by_and_context_length",
    ),
    "cli-proxy-api-non-claude-cache-retention.patch": (
        "tests.test_gateway_contracts.RetentionBoundaryTests.test_top_level_retention_stripped",
        "tests.test_gateway_contracts.RetentionBoundaryTests.test_duplicate_top_level_retention_stripped",
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_compat_retention",
    ),
    "cli-proxy-api-management-readonly-allowlist.patch": (
        "tests.test_gateway_patches.ManagementAllowlistTests.test_refused_route_is_404_with_valid_key",
        "tests.test_gateway_patches.ManagementAllowlistTests.test_oauth_callback_is_404",
        "tests.test_gateway_patches.ManagementAllowlistTests.test_browser_origin_is_404_on_allowlisted_route",
    ),
    "cli-proxy-api-watcher-parentdir.patch": (
        "tests.test_gateway_hot_reload.GatewayHotReloadTests.test_repeated_atomic_replaces_with_old_inode_open",
    ),
    "cli-proxy-api-serve-after-initial-auth-load.patch": (
        "tests.test_gateway_patches.StartupFileAuthTests.test_first_model_route_waits_for_file_auth",
    ),
    "cli-proxy-api-management-env-only.patch": (
        "tests.test_gateway_patches.ManagementEnvOnlyTests.test_config_secret_cannot_enable_management",
    ),
    "cli-proxy-api-credential-save-report.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_credential_receipt",
    ),
    "cli-proxy-api-credentialed-redirects.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_credentialed_redirect",
    ),
    "cli-proxy-api-openai-compat-keyed-safety.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_keyed_safety",
    ),
    "cli-proxy-api-auth-snapshot-locking.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_auth_snapshot",
    ),
    "cli-proxy-api-server-config-snapshot.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_config_snapshot",
    ),
    "cli-proxy-api-plugin-host-locking.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_plugin_host_lock",
    ),
    "cli-proxy-api-claude-metadata-locking.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_metadata_lock",
    ),
    "cli-proxy-api-oauth-model-overlay.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_overlay",
    ),
    "cli-proxy-api-no-antigravity-egress.patch": (
        "tests.test_packaging.NoAntigravityEgressTests.test_local_model_never_starts_antigravity_updater",
    ),
    "cli-proxy-api-codex-client-identity.patch": (
        "tests.test_gateway_patches.CodexIdentityTests.test_codex_identity",
    ),
    "cli-proxy-api-antigravity-loopback-callback.patch": (
        "tests.test_gateway_patches.LoopbackOAuthListenerTests.test_antigravity_login_callback_listens_on_loopback_only",
    ),
    "cli-proxy-api-codex-api-key-safety.patch": (
        "tests.test_gateway_patches.CodexApiKeySafetyTests.test_key_route_failures_and_identity",
    ),
    "cli-proxy-api-refresh-shutdown-join.patch": (
        "tests.test_gateway_patches.PatchOmissionProbeTests.test_refresh_shutdown",
    ),
    "cli-proxy-api-openai-content-chunks.patch": (
        "tests.test_gateway_patches.MistralContentChunkTests.test_streaming_content_chunks",
        "tests.test_gateway_patches.MistralContentChunkTests.test_non_streaming_content_chunks",
    ),
}


# Regression controls travel with a patch's omission proof, but are not its
# discriminating rows: their bytes must stay green even when it is omitted.
PATCH_CONTROL_ROWS = {
    "cli-proxy-api-openai-content-chunks.patch": (
        "tests.test_gateway_patches.ContentChunkGoldenTests.test_legacy_chat_bytes",
        "tests.test_gateway_patches.ContentChunkGoldenTests.test_native_route_bytes",
    ),
}

class DiagnosticMissing(AssertionError):
    """No usable diagnostic probe is selected (unset, not an executable,
    neither a store output nor a content-bound build)."""


def selected_diagnostic(gateway, sibling=None):
    """The startup diagnostic STARTUP_PROBE_ENV selects, or the probe named
    ``sibling`` beside it (an omission probe of the same build), built
    against ``gateway``: a store output naming that gateway's output, or a
    content-bound build whose record names that gateway's sha256
    (``_gateway.diagnostic_problem``). Raises DiagnosticMissing when nothing
    usable is selected, AssertionError when it belongs to another gateway."""
    value = os.environ.get(STARTUP_PROBE_ENV, "")
    if not value:
        raise DiagnosticMissing("matching diagnostic missing: set " + STARTUP_PROBE_ENV)
    binary = Path(os.path.realpath(value))
    if sibling is not None:
        binary = binary.with_name(sibling)
    problem = diagnostic_problem(binary, gateway)
    if problem is None:
        return binary
    if "another gateway" in problem or "another patch series" in problem:
        raise AssertionError("diagnostic belongs to another gateway build: " + problem)
    raise DiagnosticMissing("matching diagnostic missing: " + problem)


def run_codex_diagnostic(name, *, package="service", fixture=None):
    """Explicit matching build, no installed-binary fallback.

    A missing/empty selection is a failure, including on an omission variant.
    Identity fixtures contain rendered dummy config only; no live auth/config reads.
    """
    if not os.environ.get(BINARY_ENV):
        boundary("BOUNDARY: Codex identity needs an explicitly selected manifest build")
    gateway, reason = find_gateway_binary()
    if gateway is None:
        boundary("BOUNDARY: " + reason)
    reason = probe.network_isolation_available()
    if reason:
        boundary("BOUNDARY: " + reason)
    binary = selected_diagnostic(gateway, None if package == "service" else "gwtest-executor-probe")
    with tempfile.TemporaryDirectory(prefix="probe-codex-") as directory:
        root = Path(directory).resolve()
        for filename, content in (fixture or {}).items():
            if filename not in ("config.yaml", "spec.json"):
                raise AssertionError("unexpected diagnostic fixture file")
            state.atomic_write(root / filename, content)
        with (root / "probe.log").open("wb") as log:
            process = probe.start_isolated_process(
                [str(binary), "-test.v", "-test.run=^" + name + "$", "-test.timeout=60s"],
                cwd=root, env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin",
                               "CODEX_PROBE_FIXTURE_DIR": str(root)}, stdout=log, stderr=subprocess.STDOUT)
            try:
                process.wait(timeout=65)
            finally:
                probe.stop_isolated_process(process)
        output = (root / "probe.log").read_text()
        if process.returncode or "--- PASS: " + name not in output or "--- SKIP:" in output:
            # Never echo a Go test's auth/config/log payloads.
            raise AssertionError("isolated Codex diagnostic failed or empty: " + name)
    return str(binary)


def gateway_check_selections():
    # A leased patch discriminator may live outside the ordinary modules.
    # Run those rows too, without importing all unrelated packaging checks.
    return (*GATEWAY_CHECK_MODULES, "tests.test_gateway_keyed_compat.KeyedGatewayCoreTests", *(row for rows in PATCH_ROWS.values() for row in rows
             if not any(row.startswith(module + ".") for module in GATEWAY_CHECK_MODULES)))


# Declared application dependencies, companion metadata to PATCH_ROWS.
# The env-only config-load hunk is written against the watcher-guarded source.
# Omit the dependent as well, then attribute the watcher row by difference
# against the dependent-only omission. Never infer edges from build failures.
PATCH_DEPENDENCIES = {
    "cli-proxy-api-refresh-shutdown-join.patch": {
        "cli-proxy-api-credential-save-report.patch": {
            "file": "sdk/auth/refresh_shutdown_test.go", "hunk": 0,
            "reason": "durable-save tests use the reporting store and its private saveFileOps sync seam",
        },
        "cli-proxy-api-serve-after-initial-auth-load.patch": {
            "file": "sdk/cliproxy/service_shutdown_test.go", "hunk": 0,
            "reason": "the Run context regression uses the initial-auth barrier and authQueueDone consumer join",
        },
    },
    "cli-proxy-api-oauth-model-overlay.patch": {
        "cli-proxy-api-serve-after-initial-auth-load.patch": {
            "file": "sdk/cliproxy/oauth_extra_models_cm053_test.go", "hunk": 817,
            "reason": "overlay reload gate uses the initial-auth DispatchAuthBarrier seam",
        },
        "cli-proxy-api-watcher-parentdir.patch": {
            "file": "sdk/cliproxy/oauth_extra_models_cm053_test.go", "hunk": 65,
            "reason": "overlay registration gate uses LoadConfigBytes introduced by watcher-parentdir",
        },
    },
    "cli-proxy-api-openai-compat-keyed-safety.patch": {
        "cli-proxy-api-credentialed-redirects.patch": {
            "file": "internal/runtime/executor/openai_compat_executor.go", "hunk": 191,
            "reason": "keyed error handling replaces the redirect-guarded request paths (also hunk 432)",
        },
    },
    "cli-proxy-api-auth-snapshot-locking.patch": {
        "cli-proxy-api-credential-save-report.patch": {
            "file": "sdk/cliproxy/auth/auth_snapshot_cm053_test.go", "hunk": 120,
            "reason": "registration ownership gate reads RegistrationEpoch introduced by credential-save-report",
        },
    },
    "cli-proxy-api-management-env-only.patch": {
        "cli-proxy-api-watcher-parentdir.patch": {
            "file": "internal/config/config_load.go", "hunk": 122,
        },
    },
    "cli-proxy-api-codex-api-key-safety.patch": {
        "cli-proxy-api-credentialed-redirects.patch": {
            "file": "internal/runtime/executor/codex_executor_execute.go", "hunk": 107,
            "reason": "the transport-error hunks sit on the redirect-guarded request calls (also compact and stream)",
        },
        "cli-proxy-api-openai-compat-keyed-safety.patch": {
            "file": "internal/runtime/executor/codex_key_safety.go", "hunk": 0,
            "reason": "the fixed failure texts and the bounded error-body read come from internal/compatsafe",
        },
        "cli-proxy-api-codex-client-identity.patch": {
            "file": "internal/runtime/executor/codex_executor_request.go", "hunk": 341,
            "reason": "the plain-key Version default replaces the codex client Version default (also the websocket hunk)",
        },
    },
}


def contract_row(adapter, cid, stream, choice=None):
    parts = [adapter, cid] + ([choice] if choice else [])
    return ":".join(parts + ["stream" if stream else "json"])


def stream_rows(*rows):
    return {row + suffix for row in rows for suffix in ("-json", "-stream")}


# Required *cells*, not only table names. Missing variants cannot leave G4g/G8
# green. The override inventory and counts have one owner.
REQUIRED_EVIDENCE["tests.test_gateway_contracts"] = {
    "contracts": {contract_row(adapter, cid, stream)
                  for adapter, contracts in render.ADAPTER_PAYLOAD_CONTRACTS.items()
                  for cid, contract in contracts.items() if contract["kind"] == "override"
                  for stream in (False, True)},
    "contract_exceptions": {contract_row(CLAUDE, cid, stream, choice)
                            for cid, contract in render.ADAPTER_PAYLOAD_CONTRACTS[CLAUDE].items()
                            if contract["kind"] == "override"
                            for stream in (False, True) for choice in ("any", "tool")},
    "composition": {"A5", "C4", "C5", "C6-claude", "C6-codex", "C6-compat"} | stream_rows(
        "A8-gwtest-cc", "A8-gwtest-ccb", "C-claude", "C-codex", "C-compat"),
    "filter": stream_rows("gwtest-ccf-a", "gwtest-ccf-b", "gwtest-cont-ccf",
                          "gwtest-cc-think-a", "gwtest-cc-think-b"),
    "auth": {"K4"} | stream_rows("K1", "K2-False", "K2-True", "K3-catalog", "K3-continuity"),
    "passthrough": stream_rows("claude", "codex", "compat"),
    "fidelity": {"F1", "F2-any", "F2-tool", "F2-serial", "F3", "F4", "F5", "F5b", "F5c",
                 "F6-text", "F6-tool_calls", "F6-length", "F7", "F8"} | stream_rows("F9", "F10", "F11"),
    "reasoning_blocks": stream_rows("F9"),
    "retention": stream_rows("R1", "R2", "R3", "R4-codex", "R4-compat"),
    "observations": {"O2-B088"} | stream_rows("B081"),
}


# The axes are shared with the evidence inventory: every stream/form cell
# must execute, even when no particular outcome is pinned for that cell.
CLAMP_TARGETS = {
    "claude": {variant: "gwtest-o12-" + variant for variant in ("none", "declared", "superset")},
    "codex": {variant: "gwtest-codex-e-" + variant for variant in ("none", "declared", "superset")},
    "compat": {variant: "gwtest-compat-" + variant for variant in ("plain", "override", "declared", "superset")},
}
CLAMP_FORMS = ("absent", "adaptive", "adaptive-low", "adaptive-medium", "adaptive-high",
               "adaptive-xhigh", "adaptive-max", "enabled-1024", "enabled-32000", "disabled")
REQUIRED_EVIDENCE["tests.test_gateway_hints"] = {
    "H": stream_rows("H1", "H2", "H3", "H8") | {"H4", "H5", "H6", "HV1", "HV2"},
}
REQUIRED_EVIDENCE["tests.test_gateway_clamp"] = {
    "E": {f"{family}:{variant}:{form}:{stream}"
          for family, variants in CLAMP_TARGETS.items() for variant in variants
          for form in CLAMP_FORMS for stream in (False, True)},
    # Written only after all caller-supplied census lines have been classified.
    # The inventory is deliberately not loaded from the shipped catalog here.
    "census": {"coverage"},
}


REQUIRED_EVIDENCE["tests.test_gateway_patches"] = {
    "patches": {"LO1", "LO2", "LO1-A", "LO3", "LO4", "M0", "M1", "M2", "M3", "M4", "ME1", "S1", "P1", "CS1", "CR1", "KS1", "AS1", "SC1", "PH1", "ML1", "OM1", "CI1", "CI2", "CK1", "RS1", "MC1", "MC2", "MG1", "MG2"},
}


REQUIRED_EVIDENCE["tests.test_packaging"] = {"patches": {"AG1"}}


WATCHER_READY = "file watcher started for config and auth directory changes"
MARKER = "x-gwtest-upstream-marker"
NONCE = re.compile(r"gwtest-case-[0-9a-f]{12}")


def require_gateway() -> bool:
    value = os.environ.get(REQUIRE_ENV, "")
    if value in ("", "0"):
        return False
    if value == "1":
        return True
    raise RuntimeError(f"{REQUIRE_ENV}={value!r}: use 1 or leave it unset")


def boundary(reason: str) -> NoReturn:
    if require_gateway():
        raise AssertionError(f"{REQUIRE_ENV}=1: {reason}")
    raise unittest.SkipTest(reason)


@lru_cache(maxsize=1)
def find_gateway_binary() -> tuple[str | None, str]:
    # Import-only reuse; the resolver's isolated banner/--version process
    # is permitted once per harness runner. Also check the launched banner.
    return _gateway_binary()


@lru_cache(maxsize=4)
def binary_identity(path: str, version: str) -> dict:
    """The launched gateway's identity in the evidence: its realpath and
    version; a content-bound build outside the store adds its sha256."""
    identity = {"realpath": path, "version": version}
    if not path.startswith(NIX_STORE):
        identity["sha256"] = sha256_file(Path(path))
    return identity


def ports() -> tuple[int, int]:
    # Linux's default ephemeral source range ends at 60999. A readiness dial
    # before listen can otherwise self-connect and wedge a diagnostic startup.
    # Each gateway runs in its own network namespace.
    return tuple(random.SystemRandom().sample(range(61000, 65536), 2))


def sandbox_proof() -> dict:
    uid_map = [list(map(int, line.split())) for line in Path("/proc/self/uid_map").read_text().splitlines()]
    binary = Path(os.path.realpath(shutil.which("bwrap") or "/nonexistent/bwrap"))
    overflow = int(Path("/proc/sys/kernel/overflowuid").read_text())
    proven = (os.environ.get("NIX_BUILD_TOP") == "/build" and len(uid_map) == 1
              and uid_map[0][2] == 1 and binary.stat().st_uid == overflow)
    if not proven:
        raise AssertionError("single-uid Nix sandbox / overflow-owned bwrap proof failed")
    return {"nix_build_top": "/build", "in_nix_sandbox": True}


def sandbox_metadata() -> dict:
    # Pure evidence self-tests also run in the main check, without bwrap.
    # Only the separate gateway check asserts --sandbox-proof as a gate.
    if os.environ.get("NIX_BUILD_TOP"):
        try:
            return sandbox_proof()
        except (OSError, AssertionError):
            pass
    return {"nix_build_top": os.environ.get("NIX_BUILD_TOP"), "in_nix_sandbox": False}


class Evidence:
    """Metadata only; capture bodies and credentials must never enter this object."""

    def __init__(self):
        self.tables: dict[str, dict[str, list[dict]]] = {}
        self.binary = {"realpath": None, "version": None}

    @staticmethod
    def scalar(value):
        if isinstance(value, str):
            if any(secret in value for secret in (FIXTURE_GATEWAY_TOKEN, "dummy-gwtest", "gwtesthint-", "keyedtest-key-")):
                raise ValueError("credential or hint value refused in gateway evidence")
        elif value is not None and type(value) not in (int, float, bool):
            raise TypeError("gateway evidence accepts scalar metadata only")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite evidence number")

    def record(self, module: str, table: str, row: dict) -> None:
        for name in (module, table):
            self.scalar(name)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise ValueError("unsafe evidence name")
        if not isinstance(row, dict):
            raise TypeError("evidence row must be a dictionary")
        for key, value in row.items():
            if not isinstance(key, str):
                raise TypeError("evidence keys must be strings")
            self.scalar(key)
            for item in value if isinstance(value, list) else [value]:
                self.scalar(item)
        self.tables.setdefault(module, {}).setdefault(table, []).append(copy.deepcopy(row))

    def finalize_module(self, module: str, directory: Path | None = None) -> None:
        if directory is None:
            destination = os.environ.get(EVIDENCE_ENV)
            if not destination:
                return
            directory = Path(destination)
        state.ensure_private_dir(directory)  # creates 0700; rejects an unsafe existing directory
        document = {"schema": "gwtest-evidence-v1", "module": module,
                    "binary": self.binary, "sandbox": sandbox_metadata(),
                    "tables": self.tables.get(module, {})}
        validate_evidence_document(document, module)
        state.atomic_write(directory / (module + ".json"),
                           (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def validate_evidence_document(document: dict, module: str, *, store_only: bool = False) -> None:
    """``store_only``: the gateway must be a store output (published Nix
    evidence); otherwise a content-bound build outside the store also
    qualifies, identified by its realpath and sha256."""
    if document.get("schema") != "gwtest-evidence-v1" or document.get("module") != module:
        raise AssertionError("incorrect evidence identity")
    binary = document.get("binary", {})
    realpath = str(binary.get("realpath", ""))
    stored = realpath.startswith(NIX_STORE)
    bound = (not store_only and realpath.startswith("/") and not stored
             and re.fullmatch(r"[0-9a-f]{64}", str(binary.get("sha256", ""))) is not None)
    if not (stored or bound) or not re.fullmatch(r"\d+\.\d+\.\d+", str(binary.get("version", ""))):
        raise AssertionError("missing launched binary identity")
    for table, required in REQUIRED_EVIDENCE.get(module, {}).items():
        rows = document.get("tables", {}).get(table, [])
        present = {row.get("row") for row in rows}
        if not required <= present:
            raise AssertionError(f"missing required evidence rows in {module}/{table}: {sorted(required - present)}")
    # E7's required coverage row carries the dynamic input inventory. Check
    # that inventory against the per-wire rows without another shipped read.
    for coverage in document.get("tables", {}).get("census", []):
        if coverage.get("row") == "coverage":
            rows = [row for row in document["tables"]["census"] if row.get("row") != "coverage"]
            lines = coverage.get("lines")
            if (not isinstance(lines, list) or not lines or any(not isinstance(line, str) for line in lines)
                    or len(set(lines)) != len(lines)
                    or coverage.get("complete") is not True or coverage.get("count") != len(lines)
                    or len(rows) != len(lines) or {row.get("line") for row in rows} != set(lines)):
                raise AssertionError("missing required census line evidence")
    # Reapply the value allowlist to files too, not just in-memory writes.
    checker = Evidence()
    for table, rows in document["tables"].items():
        for row in rows:
            checker.record(module, table, row)


def validate_evidence(directory: Path, *, published: bool = False) -> None:
    """Private host/build evidence is 0700/0600. published=True is the separate
    content check for the gateway check's immutable Nix-store copy, which Nix
    normalizes to 0555/0444 public metadata; it never relaxes the private check."""
    if published:
        if not str(directory.resolve()).startswith("/nix/store/"):
            raise AssertionError("published evidence must be an immutable /nix/store output")
        modes = [directory.stat().st_mode] + [(directory / (m + ".json")).stat().st_mode for m in REQUIRED_EVIDENCE]
        if any(mode & 0o222 for mode in modes):
            raise AssertionError("published evidence must be read-only")
    elif directory.stat().st_mode & 0o077:
        raise AssertionError("evidence directory must be 0700")
    for module in REQUIRED_EVIDENCE:
        path = directory / (module + ".json")
        if not published and path.stat().st_mode & 0o077:
            raise AssertionError("evidence file must be private")
        validate_evidence_document(json.loads(path.read_text()), module, store_only=published)


# The keyed lanes extend the metadata evidence, with explicit selected inventories.
KEYED_LANE_ENV = "CLAUDE_MULTI_TEST_KEYED_LANE"
KEYED_EVIDENCE_ENV = "CLAUDE_MULTI_TEST_KEYED_EVIDENCE"
KEYED_INVENTORIES = {
    "core": {"K1-render", "K1-pairing", "K12", "S1", "A1-reload"},
    "audit": {"K1-omissions", "P1-CR1-KS1", "P1-controls", "CR1", "KS1", "M1", "F1",
              "F1-losses", "K1-headers", "A1-authority", "K12-qualification"},
    "client": {"C1-lead", "C1-replay", "C1-agent", "C1-workflow", "C1-fence-fallback", "C1-policy", "C1-permission",
               "C2-manual", "C2-auto"},
}
KEYED_RELEVANT_SOURCES = (
    "src/claude_multi/catalog.py", "src/claude_multi/operator.py", "src/claude_multi/render.py",
    "src/claude_multi/served_plan.py", "src/claude_multi/qualify.py", "src/claude_multi/discovery.py",
    "src/claude_multi/profile.py", "src/claude_multi/compiler.py", "src/claude_multi/scope.py",
    "src/claude_multi/cli/commands/models.py", "src/claude_multi/cli/commands/providers.py",
    "schemas/gateway.schema.json", "schemas/providers.schema.json", "schemas/operator-provider.schema.json",
    "schemas/operator-ledger.schema.json", "schemas/models.schema.json",
)


def keyed_lane():
    lane = os.environ.get(KEYED_LANE_ENV, "host")
    if lane not in ("host", "unit"):
        raise AssertionError("unknown keyed test lane")
    return lane


def keyed_identity():
    binary, reason = find_gateway_binary()
    if binary is None:
        raise AssertionError("required candidate unavailable: " + reason)
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return {"gateway": str(binary), "gateway_sha256": digest(binary),
            "sources": {name: digest(logical_path(name)) for name in KEYED_RELEVANT_SOURCES},
            "gateway_patch_names": json.loads((RESOURCES_ROOT / "catalog/gateway.json").read_text())["gateway"]["patches"],
            "client_contract_sha256": digest(RESOURCES_ROOT / "catalog/native-contract.json")}


def validate_keyed_document(document, selected, *, identity=None):
    module = document.get("module")
    validate_evidence_document(document, module)
    if document.get("keyed_identity") != (keyed_identity() if identity is None else identity):
        raise AssertionError("keyed source or binary identity mismatch")
    if set(document.get("selected", ())) != set(selected):
        raise AssertionError("selected keyed inventory mismatch")
    for table in selected:
        rows = document["tables"].get(table, [])
        expected = KEYED_INVENTORIES[table]
        if {row.get("row") for row in rows} != expected or len(rows) != len(expected):
            raise AssertionError("missing or duplicate required keyed rows: " + table)
        if any(row.get("status") != "passed" for row in rows):
            raise AssertionError("required keyed row failed or skipped")


def finalize_keyed(evidence, module, selected):
    # Validate even without an output directory. Missing proofs fail
    # the ordinary host invocation too; a unit selection promises no proof.
    if keyed_lane() == "unit":
        if selected:
            raise AssertionError("required proof selected in unit-only lane")
        return
    if not selected:
        raise AssertionError("required keyed proof inventory was not selected")
    document = {"schema": "gwtest-evidence-v1", "module": module,
                "binary": evidence.binary, "sandbox": sandbox_metadata(),
                "tables": evidence.tables.get(module, {}), "selected": sorted(selected),
                "keyed_identity": keyed_identity()}
    validate_keyed_document(document, selected)
    destination = os.environ.get(KEYED_EVIDENCE_ENV)
    if destination:
        directory = state.ensure_private_dir(Path(destination))
        state.atomic_write(directory / (module + ".json"),
                           (json.dumps(document, sort_keys=True, indent=2) + "\n").encode())


def validate_keyed_evidence(directory, *, core_only=False, gateway_only=False):
    """The private keyed proof: core, or the gateway side (core and audit,
    no client file), or the complete proof with the pinned client."""
    if core_only and gateway_only:
        raise AssertionError("choose one keyed evidence scope")
    if directory.stat().st_mode & 0o077:
        raise AssertionError("keyed evidence directory must be private")
    selected = {"tests.test_gateway_keyed_compat": {"core"} if core_only else {"core", "audit"}}
    if not core_only and not gateway_only:
        selected["tests.test_keyed_compat_client"] = {"client"}
    for module, inventory in selected.items():
        path = directory / (module + ".json")
        if path.stat().st_mode & 0o077:
            raise AssertionError("keyed evidence must be private")
        validate_keyed_document(json.loads(path.read_text()), inventory)


# The one pinned vendor (go-modules) output every diagnostic reuses.
PINNED_GO_MODULES = "/nix/store/xkljmgxi3cwh12myims7q3m9hjgmw0yf-cli-proxy-api-7.3.15-go-modules"


def consumed_vendor(installable, run=subprocess.run) -> dict:
    """The vendor inputs the build consumes, never a diagnostic attribute: its
    $goModules and the outputs of every *-go-modules input derivation.
    `installable` is a .drv or a built output (Nix resolves its deriver)."""
    shown = run(["nix", "derivation", "show", "--offline", str(installable)],
                capture_output=True, text=True, timeout=120)
    if shown.returncode:
        raise RuntimeError("nix derivation show failed for a diagnostic")
    document = json.loads(shown.stdout)
    derivations = document.get("derivations", document)
    if len(derivations) != 1:
        raise RuntimeError("expected exactly one diagnostic derivation")
    ((name, derivation),) = derivations.items()

    def store(path):
        return path if path.startswith("/nix/store/") else "/nix/store/" + path
    inputs = derivation.get("inputs", {}).get("drvs", derivation.get("inputDrvs", {}))
    vendor = sorted(store(path) for path in inputs if path.endswith("-go-modules.drv"))
    outputs = []
    if vendor:
        queried = run(["nix-store", "--query", "--outputs", *vendor], capture_output=True, text=True, timeout=120)
        if queried.returncode:
            raise RuntimeError("nix-store could not resolve a go-modules input")
        outputs = sorted(queried.stdout.split())
    return {"derivation": store(name), "env_go_modules": derivation.get("env", {}).get("goModules"),
            "go_modules_inputs": vendor, "go_modules_input_outputs": outputs}


def vendor_pinned(consumed: dict, pinned: str = PINNED_GO_MODULES) -> bool:
    return consumed.get("env_go_modules") == pinned and consumed.get("go_modules_input_outputs") == [pinned]


EVIDENCE = Evidence()


@dataclass(frozen=True)
class AliasInfo:
    route: str
    provider: str
    adapter: str
    family: str
    wire: str
    contract: str | None
    levels: tuple[str, ...] = ()
    origin: str = "catalog"


def templates(bundle) -> dict:
    def pick(adapter, auth=None):
        for name, provider in sorted(bundle.providers.items()):
            if provider["adapter"] == adapter and (
                auth is None or provider["transport"].get("auth", {}).get("kind") == auth
            ):
                line = next((line for _, line in sorted(bundle.lines.items()) if line["provider"] == name), None)
                if line is not None:
                    return provider, line
        raise AssertionError(f"fixture lacks template {adapter}/{auth}")
    return {"codex": pick(CODEX), "header": pick(CLAUDE, "header"),
            "bearer": pick(CLAUDE, "bearer"), "compat": pick(COMPAT)}


def build_document(bundle, egress: int, gateway: int, home: Path, variant="main"):
    """Real v2 render, then the six explicitly test-local surgeries.

    Returns (document, aliases, render_info). No fixture/shipped model key is
    pinned; only the codex registry wire is selected structurally from v2.
    """
    if variant not in ("main", "ua"):
        raise ValueError("unknown harness variant")
    source = templates(bundle)
    providers, lines, aliases, continuity = {}, {}, {}, {}
    all_contracts = render.ADAPTER_PAYLOAD_CONTRACTS
    levels = tuple(c.rsplit("-", 1)[1] for c in all_contracts[CODEX])

    def provider(route, template, contracts):
        p = copy.deepcopy(source[template][0])
        p.update(payload_contracts=list(contracts), passthrough_routes=[], independence_family="gwtest-fam-" + route)
        p["transport"]["base_url"] = f"http://127.0.0.1:{egress}/{route}"
        if p["transport"].get("auth", {}).get("secret_ref") is not None:
            p["transport"]["auth"]["secret_ref"] = "env:GWTEST_" + route.removeprefix("gwtest-").upper()
        providers[route] = p

    provider("gwtest-codex", "codex", all_contracts[CODEX])
    provider("gwtest-cc", "header", [k for k, v in all_contracts[CLAUDE].items() if v["kind"] == "override"])
    provider("gwtest-ccb", "bearer", ("output-config-medium", "output-config-low"))
    provider("gwtest-ccf", "header", ("output-config-high", "output-config-xhigh", "filter-thinking"))
    provider("gwtest-cch", "header", ("output-config-high",))
    provider("gwtest-o12", "header", ("output-config-low", "output-config-high"))
    provider("gwtest-compat", "compat", ())
    # B081: a DeepSeek-shaped nested base, still entirely fixture-local.
    provider("gwtest-nested", "header", ("output-config-high",))
    providers["gwtest-nested"]["transport"]["base_url"] += "/anthropic"

    def line(key, route, contracts=(), wire=None, declared=(), selectors=None):
        p = providers[route]
        template = "codex" if p["adapter"] == CODEX else "compat" if p["adapter"] == COMPAT else "header"
        entry = copy.deepcopy(source[template][1])
        wire = wire or "gwtest-wire-" + key.removeprefix("gwtest-")
        entry.update(provider=route, wire_model=wire, display="gwtest " + key)
        entry["context"]["provider_tokens"] = 100000 + 1000 * len(lines)
        if contracts:
            entry.pop("selector", None)
            entry["efforts"] = {}
            for contract in contracts:
                effort = contract.rsplit("-", 1)[1]
                alias = selectors[contract] if selectors else key + "-" + contract
                entry["efforts"][effort] = {"selector": alias, "proxy_contract": contract}
        else:
            entry["efforts"] = ["high"]
            entry["selector"] = key
        entry["default_effort"] = next(iter(entry["efforts"]))
        lines[key] = entry
        for _effort, alias, contract in catalog.line_selectors(entry):
            aliases[alias] = AliasInfo(route, route, p["adapter"],
                                      "codex" if p["adapter"] == CODEX else "compat" if p["adapter"] == COMPAT else "claude",
                                      wire, contract, tuple(declared))

    line("gwtest-nested", "gwtest-nested", ("output-config-high",),
         selectors={"output-config-high": "gwtest-nested-high"})
    codex_wire = source["codex"][1]["wire_model"]
    line("gwtest-codex-c", "gwtest-codex", all_contracts[CODEX], wire=codex_wire,
         selectors={c: "gwtest-codex-" + c for c in all_contracts[CODEX]})
    for name, contract, declared in (("none", "low", ()), ("declared", "high", ("high",)), ("superset", "low", levels)):
        key = "gwtest-codex-e-" + name
        line(key, "gwtest-codex", ("reasoning-effort-" + contract,),
             wire=codex_wire if name == "none" else None, declared=declared,
             selectors={"reasoning-effort-" + contract: key})
    for suffix, prefix in (("oc", "output-config-"), ("re", "reasoning-effort-")):
        contracts = [c for c in all_contracts[CLAUDE] if c.startswith(prefix)]
        line("gwtest-cc-" + suffix, "gwtest-cc", contracts,
             selectors={c: "gwtest-cc-" + c for c in contracts})
    for route, suffix, effort in (("gwtest-cc", "think-a", "high"), ("gwtest-cc", "think-b", "xhigh"),
                                  ("gwtest-ccf", "a", "high"), ("gwtest-ccf", "b", "xhigh"),
                                  ("gwtest-ccb", "x", "medium"), ("gwtest-cch", "x", "high")):
        key, contract = route + "-" + suffix, "output-config-" + effort
        line(key, route, (contract,), declared=(effort,) if route in ("gwtest-cc", "gwtest-ccf") else (),
             wire="gwtest-wire-" + route.removeprefix("gwtest-") if route in ("gwtest-ccb", "gwtest-cch") else None,
             selectors={contract: key})
    for name, effort, declared in (("none", "high", ()), ("declared", "high", ("high",)), ("superset", "low", levels)):
        key, contract = "gwtest-o12-" + name, "output-config-" + effort
        line(key, "gwtest-o12", (contract,), declared=declared, selectors={contract: key})
    for name in ("plain", "override", "declared", "superset", "iscompat"):
        line("gwtest-compat-" + name, "gwtest-compat", declared=("high",) if name == "declared" else levels if name == "superset" else ())
    for route, contract, declared in (("cc", "reasoning-effort-max", ()), ("ccb", "output-config-low", ()), ("ccf", "output-config-high", ("high",))):
        alias, provider_id = "gwtest-cont-" + route, "gwtest-" + route
        wire = "gwtest-wire-cont-" + route
        continuity[alias] = {"provider": provider_id, "wire": wire, "display": alias,
                             "context_tokens": 180000 + len(continuity) * 1000, "proxy_contract": contract}
        aliases[alias] = AliasInfo(provider_id, provider_id, CLAUDE, "claude", wire, contract, declared, "continuity")

    gateway_doc = copy.deepcopy(bundle.docs["gateway"])
    gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{gateway}"
    document, available, unavailable, info = render.build_config_document(
        gateway_doc, providers, lines, home=home, gateway_token=FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda name: "dummy-gwtest-" + name.removeprefix("GWTEST_").lower(), continuity=continuity,
    )
    if unavailable or set(available) != set(providers):
        raise AssertionError("synthetic provider omitted by real render")
    # S1: keep the OAuth table, but use a direct dummy codex credential offline.
    document["codex-api-key"] = [{"api-key": "dummy-gwtest-codex",
                                  "base-url": f"http://127.0.0.1:{egress}/gwtest-codex",
                                  "models": [{k: item[k] for k in ("name", "alias", "force-mapping")}
                                             for item in document["oauth-model-alias"]["codex"]]}]
    for section in model_sections(document):
        for model in section["models"]:
            alias = model["alias"]
            if alias in aliases and aliases[alias].levels:
                model["thinking"] = {"levels": list(aliases[alias].levels)}  # S2
            if alias == "gwtest-compat-iscompat":
                model["is-compat"] = True  # S4
        if urlsplit(section["base-url"]).path == "/gwtest-cch":
            section["headers"] = {"Authorization": "Bearer dummy-gwtest-override"}  # S5
    document["payload"]["override"].append({"models": [{"name": "gwtest-compat-override", "protocol": "openai"}],
                                            "params": {"reasoning_effort": "low"}})  # S3
    if variant == "ua":
        major, minor, _ = map(int, bundle.docs["native-contract"]["verified"][0]["version"].split("."))
        document["claude-header-defaults"] = {"user-agent": f"claude-cli/{major}.{minor + 1}.0 (external, cli)"}  # S6
    return document, aliases, info


def build_census_document(bundle, egress: int, gateway: int, home: Path, lines: dict):
    """E7: caller-selected lines, fixture provider, real render, no S2 levels.

    Only the marked census test reads shipped data. Provider settings, URLs
    and credentials always come from the fixture and synthetic local values.
    """
    template_provider, template_line = templates(bundle)["header"]
    route = "gwtest-census"
    provider = copy.deepcopy(template_provider)
    provider.update(payload_contracts=["output-config-high"], passthrough_routes=[],
                    independence_family="gwtest-fam-census")
    provider["transport"]["base_url"] = f"http://127.0.0.1:{egress}/{route}"
    provider["transport"]["auth"]["secret_ref"] = "env:GWTEST_CENSUS"
    synthetic, aliases, keys = {}, {}, {}
    for index, (key, source) in enumerate(sorted(lines.items())):
        alias = f"gwtest-census-{index}"
        line = copy.deepcopy(template_line)
        line.pop("selector", None)
        line.update(provider=route, wire_model=source["wire_model"], display=alias,
                    efforts={"high": {"selector": alias, "proxy_contract": "output-config-high"}},
                    default_effort="high")
        synthetic[alias] = line
        aliases[alias] = AliasInfo(route, route, CLAUDE, "claude", source["wire_model"], "output-config-high")
        keys[alias] = key
    if not synthetic:
        raise AssertionError("census requires at least one selected line")
    gateway_doc = copy.deepcopy(bundle.docs["gateway"])
    gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{gateway}"
    document, available, unavailable, info = render.build_config_document(
        gateway_doc, {route: provider}, synthetic, home=home, gateway_token=FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda _name: "dummy-gwtest-census", continuity={},
    )
    if unavailable or set(available) != {route}:
        raise AssertionError("census provider omitted by real render")
    info["census_lines"] = keys
    return document, aliases, info


def model_sections(document):
    return [section for key in ("claude-api-key", "codex-api-key", "openai-compatibility")
            for section in document.get(key, [])]


@dataclass(repr=False)
class Hit:
    route: str
    path: str
    query: str
    method: str
    header_names: list[str]
    header_values: dict[str, str]
    raw: bytes
    body: dict
    top_level_keys: list[str]
    nonce: str | None
    hint_markers: frozenset[str] = frozenset()


def hint_markers(header_values, raw):
    """Retain only synthetic hint/UUID markers from ALL header values + body.

    H1/H4/H5 can detect a hint moved into an otherwise uncaptured header,
    without expanding the raw header-value allowlist or persisting captures.
    """
    material = "\n".join(header_values) + "\n" + raw.decode(errors="replace")
    return frozenset(re.findall(r"gwtesthint-[a-z0-9-]+|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", material))


def sse_frame(kind: str, body: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(body)}\n\n".encode()


# The named shapes are protocol fixtures, not live-provider samples. The shipped
# Codex route uses Responses; the catalog's OpenRouter/DeepSeek/Kimi/Qwen/Meta
# routes and the Z.ai preset use Messages. Their native-route controls are below
# the chat goldens in the test, rather than mislabelled as chat integrations.
CONTENT_CHUNK_SHAPES = ("openai-codex", "openrouter", "deepseek", "kimi", "qwen", "meta", "local-llm", "keyed")
CONTENT_CHUNK_LEGACY_MESSAGES = {
    "string": {"content": "answer"},
    "null": {"content": None},
    "absent": {},
    "reasoning_content": {"reasoning_content": "thought", "content": "answer"},
    "reasoning": {"reasoning": "thought", "content": "answer"},
    "reasoning_details": {"reasoning_details": [{"type": "reasoning.text", "text": "thought"}], "content": "answer"},
    "precedence": {"reasoning_content": "thought", "reasoning": "ignored", "reasoning_details": [{"text": "ignored"}], "content": "answer"},
    "unknown": {"content": [{"type": "unknown", "text": "do not reinterpret"}]},
    "unknown_mixed": {"content": [{"type": "text", "text": "answer"}, {"type": "unknown", "text": "do not reinterpret"}]},
    "malformed": {"content": [None, {"type": "text", "text": 7}]},
    "malformed_thinking": {"content": [{"type": "thinking", "thinking": None}]},
}


def content_chunk_request(gateway, alias, *, stream=False, mode="text"):
    # The gateway estimates message_start input tokens from the request. A random
    # nonce would change those bytes even without a patch, so these sequential
    # golden fixtures use one fixed request and reset their transient captures.
    gateway.upstream.hits.clear()
    payload = {"model": alias, "max_tokens": 64, "stream": stream, "messages": [
        {"role": "user", "content": "gwtest-case-000000000000 gwtest-mode=" + mode}]}
    return gateway.request(alias, raw=json.dumps(payload).encode())


def content_chunk_reply(path: str, body: dict, raw: bytes, _headers=None) -> tuple[int, str, bytes]:
    """Canned Mistral chunks and legacy chat goldens; no clock or random output."""
    if not path.endswith("/chat/completions"):
        return fake_reply(path, body, raw)
    cases = re.findall(rb"gwtest-mode=([a-z0-9_]+)", raw)
    case = cases[-1].decode() if cases else "mistral"
    base = {"id": "chatcmpl_fixture", "model": "gwtest-fixture-model", "created": 1}
    usage = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    thinking = lambda text: {"type": "thinking", "thinking": [{"type": "text", "text": text}], "closed": True}
    if case in CONTENT_CHUNK_LEGACY_MESSAGES:
        message = copy.deepcopy(CONTENT_CHUNK_LEGACY_MESSAGES[case])
        deltas = [message]
    else:
        message = {"content": [thinking("first second"), {"type": "text", "text": "answer"}]}
        deltas = [{"content": [thinking("first ")]},
                  {"content": [thinking("second"), {"type": "text", "text": "answer"}]}]
    if not body.get("stream"):
        return 200, "application/json", json.dumps({**base, "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": "stop"}],
            "usage": usage}).encode()
    chunks = [{**base, "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
              for delta in [{"role": "assistant"}, *deltas]]
    chunks.extend([{**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                   {**base, "choices": [], "usage": usage}])
    return 200, "text/event-stream", b"".join(("data: " + json.dumps(chunk) + "\n\n").encode()
                                             for chunk in chunks) + b"data: [DONE]\n\n"


# Byte goldens captured from the admitted series before content-chunk decoding.
# Both the full series and the single-omission build must match these bytes.
CONTENT_CHUNK_HTTP_GOLDENS = {'keyed:absent:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:absent:True': 'event: message_start\n'
                      'data: '
                      '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                      '\n'
                      'event: message_delta\n'
                      'data: '
                      '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                      '\n'
                      'event: message_stop\n'
                      'data: {"type":"message_stop"}\n'
                      '\n',
 'keyed:malformed:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"7"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:malformed:True': 'event: message_start\n'
                         'data: '
                         '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                         '\n'
                         'event: content_block_start\n'
                         'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                         '\n'
                         'event: content_block_delta\n'
                         'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[null, '
                         '{\\"type\\": \\"text\\", \\"text\\": 7}]"}}\n'
                         '\n'
                         'event: content_block_stop\n'
                         'data: {"type":"content_block_stop","index":0}\n'
                         '\n'
                         'event: message_delta\n'
                         'data: '
                         '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                         '\n'
                         'event: message_stop\n'
                         'data: {"type":"message_stop"}\n'
                         '\n',
 'keyed:malformed_thinking:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:malformed_thinking:True': 'event: message_start\n'
                                  'data: '
                                  '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":18,"output_tokens":0}}}\n'
                                  '\n'
                                  'event: content_block_start\n'
                                  'data: '
                                  '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                                  '\n'
                                  'event: content_block_delta\n'
                                  'data: '
                                  '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[{\\"type\\": '
                                  '\\"thinking\\", \\"thinking\\": null}]"}}\n'
                                  '\n'
                                  'event: content_block_stop\n'
                                  'data: {"type":"content_block_stop","index":0}\n'
                                  '\n'
                                  'event: message_delta\n'
                                  'data: '
                                  '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                                  '\n'
                                  'event: message_stop\n'
                                  'data: {"type":"message_stop"}\n'
                                  '\n',
 'keyed:null:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:null:True': 'event: message_start\n'
                    'data: '
                    '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":14,"output_tokens":0}}}\n'
                    '\n'
                    'event: message_delta\n'
                    'data: '
                    '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                    '\n'
                    'event: message_stop\n'
                    'data: {"type":"message_stop"}\n'
                    '\n',
 'keyed:precedence:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:precedence:True': 'event: message_start\n'
                          'data: '
                          '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                          '\n'
                          'event: content_block_start\n'
                          'data: '
                          '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                          '\n'
                          'event: content_block_delta\n'
                          'data: '
                          '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                          '\n'
                          'event: content_block_stop\n'
                          'data: {"type":"content_block_stop","index":0}\n'
                          '\n'
                          'event: content_block_start\n'
                          'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                          '\n'
                          'event: content_block_delta\n'
                          'data: '
                          '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                          '\n'
                          'event: content_block_stop\n'
                          'data: {"type":"content_block_stop","index":1}\n'
                          '\n'
                          'event: message_delta\n'
                          'data: '
                          '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                          '\n'
                          'event: message_stop\n'
                          'data: {"type":"message_stop"}\n'
                          '\n',
 'keyed:reasoning:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:reasoning:True': 'event: message_start\n'
                         'data: '
                         '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                         '\n'
                         'event: content_block_start\n'
                         'data: '
                         '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                         '\n'
                         'event: content_block_delta\n'
                         'data: '
                         '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                         '\n'
                         'event: content_block_stop\n'
                         'data: {"type":"content_block_stop","index":0}\n'
                         '\n'
                         'event: content_block_start\n'
                         'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                         '\n'
                         'event: content_block_delta\n'
                         'data: '
                         '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                         '\n'
                         'event: content_block_stop\n'
                         'data: {"type":"content_block_stop","index":1}\n'
                         '\n'
                         'event: message_delta\n'
                         'data: '
                         '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                         '\n'
                         'event: message_stop\n'
                         'data: {"type":"message_stop"}\n'
                         '\n',
 'keyed:reasoning_content:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:reasoning_content:True': 'event: message_start\n'
                                 'data: '
                                 '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":0}}}\n'
                                 '\n'
                                 'event: content_block_start\n'
                                 'data: '
                                 '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                                 '\n'
                                 'event: content_block_delta\n'
                                 'data: '
                                 '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                                 '\n'
                                 'event: content_block_stop\n'
                                 'data: {"type":"content_block_stop","index":0}\n'
                                 '\n'
                                 'event: content_block_start\n'
                                 'data: '
                                 '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                                 '\n'
                                 'event: content_block_delta\n'
                                 'data: '
                                 '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                                 '\n'
                                 'event: content_block_stop\n'
                                 'data: {"type":"content_block_stop","index":1}\n'
                                 '\n'
                                 'event: message_delta\n'
                                 'data: '
                                 '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                                 '\n'
                                 'event: message_stop\n'
                                 'data: {"type":"message_stop"}\n'
                                 '\n',
 'keyed:reasoning_details:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:reasoning_details:True': 'event: message_start\n'
                                 'data: '
                                 '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":0}}}\n'
                                 '\n'
                                 'event: content_block_start\n'
                                 'data: '
                                 '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                                 '\n'
                                 'event: content_block_delta\n'
                                 'data: '
                                 '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                                 '\n'
                                 'event: content_block_stop\n'
                                 'data: {"type":"content_block_stop","index":0}\n'
                                 '\n'
                                 'event: content_block_start\n'
                                 'data: '
                                 '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                                 '\n'
                                 'event: content_block_delta\n'
                                 'data: '
                                 '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                                 '\n'
                                 'event: content_block_stop\n'
                                 'data: {"type":"content_block_stop","index":1}\n'
                                 '\n'
                                 'event: message_delta\n'
                                 'data: '
                                 '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                                 '\n'
                                 'event: message_stop\n'
                                 'data: {"type":"message_stop"}\n'
                                 '\n',
 'keyed:string:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"answer"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:string:True': 'event: message_start\n'
                      'data: '
                      '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":15,"output_tokens":0}}}\n'
                      '\n'
                      'event: content_block_start\n'
                      'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                      '\n'
                      'event: content_block_delta\n'
                      'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"answer"}}\n'
                      '\n'
                      'event: content_block_stop\n'
                      'data: {"type":"content_block_stop","index":0}\n'
                      '\n'
                      'event: message_delta\n'
                      'data: '
                      '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                      '\n'
                      'event: message_stop\n'
                      'data: {"type":"message_stop"}\n'
                      '\n',
 'keyed:unknown:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:unknown:True': 'event: message_start\n'
                       'data: '
                       '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":15,"output_tokens":0}}}\n'
                       '\n'
                       'event: content_block_start\n'
                       'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                       '\n'
                       'event: content_block_delta\n'
                       'data: '
                       '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[{\\"type\\": '
                       '\\"unknown\\", \\"text\\": \\"do not reinterpret\\"}]"}}\n'
                       '\n'
                       'event: content_block_stop\n'
                       'data: {"type":"content_block_stop","index":0}\n'
                       '\n'
                       'event: message_delta\n'
                       'data: '
                       '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                       '\n'
                       'event: message_stop\n'
                       'data: {"type":"message_stop"}\n'
                       '\n',
 'keyed:unknown_mixed:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[{"type":"text","text":"answer"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyed:unknown_mixed:True': 'event: message_start\n'
                             'data: '
                             '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"custom-chatco-chat","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":0}}}\n'
                             '\n'
                             'event: content_block_start\n'
                             'data: '
                             '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                             '\n'
                             'event: content_block_delta\n'
                             'data: '
                             '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[{\\"type\\": '
                             '\\"text\\", \\"text\\": \\"answer\\"}, {\\"type\\": \\"unknown\\", \\"text\\": \\"do not '
                             'reinterpret\\"}]"}}\n'
                             '\n'
                             'event: content_block_stop\n'
                             'data: {"type":"content_block_stop","index":0}\n'
                             '\n'
                             'event: message_delta\n'
                             'data: '
                             '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                             '\n'
                             'event: message_stop\n'
                             'data: {"type":"message_stop"}\n'
                             '\n',
 'keyless:absent:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:absent:True': 'event: message_start\n'
                        'data: '
                        '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                        '\n'
                        'event: message_delta\n'
                        'data: '
                        '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                        '\n'
                        'event: message_stop\n'
                        'data: {"type":"message_stop"}\n'
                        '\n',
 'keyless:malformed:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"7"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:malformed:True': 'event: message_start\n'
                           'data: '
                           '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                           '\n'
                           'event: content_block_start\n'
                           'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                           '\n'
                           'event: content_block_delta\n'
                           'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[null, '
                           '{\\"type\\": \\"text\\", \\"text\\": 7}]"}}\n'
                           '\n'
                           'event: content_block_stop\n'
                           'data: {"type":"content_block_stop","index":0}\n'
                           '\n'
                           'event: message_delta\n'
                           'data: '
                           '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                           '\n'
                           'event: message_stop\n'
                           'data: {"type":"message_stop"}\n'
                           '\n',
 'keyless:malformed_thinking:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:malformed_thinking:True': 'event: message_start\n'
                                    'data: '
                                    '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":18,"output_tokens":0}}}\n'
                                    '\n'
                                    'event: content_block_start\n'
                                    'data: '
                                    '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                                    '\n'
                                    'event: content_block_delta\n'
                                    'data: '
                                    '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[{\\"type\\": '
                                    '\\"thinking\\", \\"thinking\\": null}]"}}\n'
                                    '\n'
                                    'event: content_block_stop\n'
                                    'data: {"type":"content_block_stop","index":0}\n'
                                    '\n'
                                    'event: message_delta\n'
                                    'data: '
                                    '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                                    '\n'
                                    'event: message_stop\n'
                                    'data: {"type":"message_stop"}\n'
                                    '\n',
 'keyless:null:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:null:True': 'event: message_start\n'
                      'data: '
                      '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":14,"output_tokens":0}}}\n'
                      '\n'
                      'event: message_delta\n'
                      'data: '
                      '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                      '\n'
                      'event: message_stop\n'
                      'data: {"type":"message_stop"}\n'
                      '\n',
 'keyless:precedence:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:precedence:True': 'event: message_start\n'
                            'data: '
                            '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                            '\n'
                            'event: content_block_start\n'
                            'data: '
                            '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                            '\n'
                            'event: content_block_delta\n'
                            'data: '
                            '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                            '\n'
                            'event: content_block_stop\n'
                            'data: {"type":"content_block_stop","index":0}\n'
                            '\n'
                            'event: content_block_start\n'
                            'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                            '\n'
                            'event: content_block_delta\n'
                            'data: '
                            '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                            '\n'
                            'event: content_block_stop\n'
                            'data: {"type":"content_block_stop","index":1}\n'
                            '\n'
                            'event: message_delta\n'
                            'data: '
                            '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                            '\n'
                            'event: message_stop\n'
                            'data: {"type":"message_stop"}\n'
                            '\n',
 'keyless:reasoning:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:reasoning:True': 'event: message_start\n'
                           'data: '
                           '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":16,"output_tokens":0}}}\n'
                           '\n'
                           'event: content_block_start\n'
                           'data: '
                           '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                           '\n'
                           'event: content_block_delta\n'
                           'data: '
                           '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                           '\n'
                           'event: content_block_stop\n'
                           'data: {"type":"content_block_stop","index":0}\n'
                           '\n'
                           'event: content_block_start\n'
                           'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                           '\n'
                           'event: content_block_delta\n'
                           'data: '
                           '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                           '\n'
                           'event: content_block_stop\n'
                           'data: {"type":"content_block_stop","index":1}\n'
                           '\n'
                           'event: message_delta\n'
                           'data: '
                           '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                           '\n'
                           'event: message_stop\n'
                           'data: {"type":"message_stop"}\n'
                           '\n',
 'keyless:reasoning_content:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:reasoning_content:True': 'event: message_start\n'
                                   'data: '
                                   '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":0}}}\n'
                                   '\n'
                                   'event: content_block_start\n'
                                   'data: '
                                   '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                                   '\n'
                                   'event: content_block_delta\n'
                                   'data: '
                                   '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                                   '\n'
                                   'event: content_block_stop\n'
                                   'data: {"type":"content_block_stop","index":0}\n'
                                   '\n'
                                   'event: content_block_start\n'
                                   'data: '
                                   '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                                   '\n'
                                   'event: content_block_delta\n'
                                   'data: '
                                   '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                                   '\n'
                                   'event: content_block_stop\n'
                                   'data: {"type":"content_block_stop","index":1}\n'
                                   '\n'
                                   'event: message_delta\n'
                                   'data: '
                                   '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                                   '\n'
                                   'event: message_stop\n'
                                   'data: {"type":"message_stop"}\n'
                                   '\n',
 'keyless:reasoning_details:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"answer"},{"type":"thinking","thinking":"thought"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:reasoning_details:True': 'event: message_start\n'
                                   'data: '
                                   '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":0}}}\n'
                                   '\n'
                                   'event: content_block_start\n'
                                   'data: '
                                   '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n'
                                   '\n'
                                   'event: content_block_delta\n'
                                   'data: '
                                   '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"thought"}}\n'
                                   '\n'
                                   'event: content_block_stop\n'
                                   'data: {"type":"content_block_stop","index":0}\n'
                                   '\n'
                                   'event: content_block_start\n'
                                   'data: '
                                   '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n'
                                   '\n'
                                   'event: content_block_delta\n'
                                   'data: '
                                   '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n'
                                   '\n'
                                   'event: content_block_stop\n'
                                   'data: {"type":"content_block_stop","index":1}\n'
                                   '\n'
                                   'event: message_delta\n'
                                   'data: '
                                   '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                                   '\n'
                                   'event: message_stop\n'
                                   'data: {"type":"message_stop"}\n'
                                   '\n',
 'keyless:string:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"answer"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:string:True': 'event: message_start\n'
                        'data: '
                        '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":15,"output_tokens":0}}}\n'
                        '\n'
                        'event: content_block_start\n'
                        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                        '\n'
                        'event: content_block_delta\n'
                        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"answer"}}\n'
                        '\n'
                        'event: content_block_stop\n'
                        'data: {"type":"content_block_stop","index":0}\n'
                        '\n'
                        'event: message_delta\n'
                        'data: '
                        '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                        '\n'
                        'event: message_stop\n'
                        'data: {"type":"message_stop"}\n'
                        '\n',
 'keyless:unknown:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:unknown:True': 'event: message_start\n'
                         'data: '
                         '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":15,"output_tokens":0}}}\n'
                         '\n'
                         'event: content_block_start\n'
                         'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                         '\n'
                         'event: content_block_delta\n'
                         'data: '
                         '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[{\\"type\\": '
                         '\\"unknown\\", \\"text\\": \\"do not reinterpret\\"}]"}}\n'
                         '\n'
                         'event: content_block_stop\n'
                         'data: {"type":"content_block_stop","index":0}\n'
                         '\n'
                         'event: message_delta\n'
                         'data: '
                         '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                         '\n'
                         'event: message_stop\n'
                         'data: {"type":"message_stop"}\n'
                         '\n',
 'keyless:unknown_mixed:False': '{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[{"type":"text","text":"answer"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":4}}',
 'keyless:unknown_mixed:True': 'event: message_start\n'
                               'data: '
                               '{"type":"message_start","message":{"id":"chatcmpl_fixture","type":"message","role":"assistant","model":"gwtest-compat-plain","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":0}}}\n'
                               '\n'
                               'event: content_block_start\n'
                               'data: '
                               '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                               '\n'
                               'event: content_block_delta\n'
                               'data: '
                               '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[{\\"type\\": '
                               '\\"text\\", \\"text\\": \\"answer\\"}, {\\"type\\": \\"unknown\\", \\"text\\": \\"do '
                               'not reinterpret\\"}]"}}\n'
                               '\n'
                               'event: content_block_stop\n'
                               'data: {"type":"content_block_stop","index":0}\n'
                               '\n'
                               'event: message_delta\n'
                               'data: '
                               '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":11,"output_tokens":4}}\n'
                               '\n'
                               'event: message_stop\n'
                               'data: {"type":"message_stop"}\n'
                               '\n',
 'native:gwtest-cc-think-a:False': '{"id": "msg_gwtest", "type": "message", "role": "assistant", "model": '
                                   '"gwtest-cc-think-a", "content": [{"type": "text", "text": "FAKE_OK"}], '
                                   '"stop_reason": "end_turn", "stop_sequence": null, "usage": {"input_tokens": 7, '
                                   '"output_tokens": 3}}',
 'native:gwtest-cc-think-a:True': 'event: message_start\n'
                                  'data: {"type": "message_start", "message": {"id": "msg_gwtest", "type": "message", '
                                  '"role": "assistant", "model": "gwtest-cc-think-a", "content": [], "stop_reason": '
                                  'null, "stop_sequence": null, "usage": {"input_tokens": 7, "output_tokens": 3}}}\n'
                                  '\n'
                                  'event: content_block_start\n'
                                  'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", '
                                  '"text": ""}}\n'
                                  '\n'
                                  'event: content_block_delta\n'
                                  'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", '
                                  '"text": "FAKE_OK"}}\n'
                                  '\n'
                                  'event: content_block_stop\n'
                                  'data: {"type": "content_block_stop", "index": 0}\n'
                                  '\n'
                                  'event: message_delta\n'
                                  'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn", '
                                  '"stop_sequence": null}, "usage": {"output_tokens": 3}}\n'
                                  '\n'
                                  'event: message_stop\n'
                                  'data: {"type": "message_stop"}\n'
                                  '\n',
 'native:gwtest-ccb-x:False': '{"id": "msg_gwtest", "type": "message", "role": "assistant", "model": "gwtest-ccb-x", '
                              '"content": [{"type": "text", "text": "FAKE_OK"}], "stop_reason": "end_turn", '
                              '"stop_sequence": null, "usage": {"input_tokens": 7, "output_tokens": 3}}',
 'native:gwtest-ccb-x:True': 'event: message_start\n'
                             'data: {"type": "message_start", "message": {"id": "msg_gwtest", "type": "message", '
                             '"role": "assistant", "model": "gwtest-ccb-x", "content": [], "stop_reason": null, '
                             '"stop_sequence": null, "usage": {"input_tokens": 7, "output_tokens": 3}}}\n'
                             '\n'
                             'event: content_block_start\n'
                             'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", '
                             '"text": ""}}\n'
                             '\n'
                             'event: content_block_delta\n'
                             'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", '
                             '"text": "FAKE_OK"}}\n'
                             '\n'
                             'event: content_block_stop\n'
                             'data: {"type": "content_block_stop", "index": 0}\n'
                             '\n'
                             'event: message_delta\n'
                             'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": '
                             'null}, "usage": {"output_tokens": 3}}\n'
                             '\n'
                             'event: message_stop\n'
                             'data: {"type": "message_stop"}\n'
                             '\n',
 'native:gwtest-codex-e-none:False': '{"id":"resp_gwtest","type":"message","role":"assistant","model":"gwtest-codex-e-none","content":[{"type":"text","text":"FAKE_OK"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":7,"output_tokens":3}}',
 'native:gwtest-codex-e-none:True': 'event: message_start\n'
                                    'data: '
                                    '{"type":"message_start","message":{"id":"resp_gwtest","type":"message","role":"assistant","model":"gwtest-codex-e-none","stop_sequence":null,"usage":{"input_tokens":14,"output_tokens":0},"content":[],"stop_reason":null}}\n'
                                    '\n'
                                    'event: content_block_start\n'
                                    'data: '
                                    '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                                    '\n'
                                    'event: content_block_delta\n'
                                    'data: '
                                    '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"FAKE_OK"}}\n'
                                    '\n'
                                    'event: content_block_stop\n'
                                    'data: {"type":"content_block_stop","index":0}\n'
                                    '\n'
                                    'event: message_delta\n'
                                    'data: '
                                    '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":7,"output_tokens":3}}\n'
                                    '\n'
                                    'event: message_stop\n'
                                    'data: {"type":"message_stop"}\n'
                                    '\n'}


def fake_reply(path: str, body: dict, raw: bytes) -> tuple[int, str, bytes]:
    """Pure protocol responses. Modes live in request text, never server state."""
    if not path.endswith(("/v1/messages", "/responses", "/chat/completions")):
        return 404, "application/json", b'{"error":"unexpected fake upstream path"}'
    modes = re.findall(rb"gwtest-mode=([a-z0-9_]+)", raw)
    mode = modes[-1].decode() if modes else "text"
    model = "gwtest-served-other" if mode == "substitute" else body.get("model", "gwtest-wire")
    stream = body.get("stream", False)
    if mode == "error400":
        return 400, "application/json", json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "message": "fake upstream rejection"}}).encode()
    if path.endswith("/v1/messages"):
        message = {"id": "msg_gwtest", "type": "message", "role": "assistant", "model": model,
                   "content": [{"type": "text", "text": "FAKE_OK"}], "stop_reason": "end_turn",
                   "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3}}
        if not stream:
            return 200, "application/json", json.dumps(message).encode()
        frames = [
            ("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "FAKE_OK"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
            ("message_stop", {}),
        ]
        return 200, "text/event-stream", b"".join(sse_frame(kind, {"type": kind, **data}) for kind, data in frames)
    if path.endswith("/responses"):
        item = {"id": "msg_gwtest", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "FAKE_OK", "annotations": []}]}
        response = {"id": "resp_gwtest", "object": "response", "model": model, "status": "completed",
                    "output": [item], "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}
        frames = [("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
                  ("response.output_item.done", {"output_index": 0, "item": item}),
                  ("response.completed", {"response": response})]
        return 200, "text/event-stream", b"".join(sse_frame(kind, {"type": kind, **data}) for kind, data in frames)
    if path.endswith("/chat/completions"):
        finish = "length" if mode == "length" else "tool_calls" if mode.startswith("tool_calls") else "stop"
        nonces = NONCE.findall(raw.decode(errors="replace"))
        reasoning = "gwtest-reasoning-" + (nonces[-1] if nonces else "fixture")
        base = {"id": "chatcmpl_gwtest", "model": model, "created": 1}
        if not stream:
            message = {"role": "assistant", "content": "FAKE_OK"}
            if mode == "tool_calls":
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_gwtest_a", "type": "function",
                    "function": {"name": "gwtest_tool", "arguments": '{"k":"v"}'}}]}
            if mode == "reasoning":
                message["reasoning_content"] = reasoning
            return 200, "application/json", json.dumps({**base, "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}).encode()
        deltas = [{"role": "assistant"}]
        if mode == "tool_calls_split":
            for index, suffix in enumerate(("a", "b")):
                deltas.append({"tool_calls": [{"index": index, "id": "call_gwtest_" + suffix, "type": "function",
                    "function": {"name": "gwtest_tool_" + suffix, "arguments": '{"k":'}}]})
            for index in (0, 1):
                deltas.append({"tool_calls": [{"index": index, "function": {"arguments": '"v"}'}}]})
        else:
            if mode == "reasoning":
                deltas.append({"reasoning_content": reasoning})
            deltas.append({"content": "FAKE_OK"})
        chunks = [{**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
        chunks.append({**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        return 200, "text/event-stream", b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks) + b"data: [DONE]\n\n"
    return 404, "application/json", b'{"error":"unexpected fake upstream path"}'


@dataclass(frozen=True)
class ChunkedBody:
    """Fixture HTTP body with explicit writes and a possible truncated length.

    Delays separate usage from finish; missing_bytes produces a transport EOF,
    unlike an otherwise clean SSE response that merely omits [DONE].
    """
    chunks: tuple[tuple[float, bytes], ...]
    missing_bytes: int = 0

    def __len__(self):
        return sum(len(data) for _, data in self.chunks) + self.missing_bytes

    def write(self, output):
        for delay, data in self.chunks:
            if delay:
                time.sleep(delay)
            output.write(data)
            output.flush()


class FakeUpstream:
    def __init__(self, path: Path, respond=None):
        self.hits: list[Hit] = []
        self.unexpected = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def __getattr__(self, name):
                # BaseHTTPRequestHandler dispatches any valid HTTP method via
                # do_<method>; unsupported methods must not bypass accounting.
                if name.startswith("do_"):
                    return self.dispatch
                raise AttributeError(name)

            def dispatch(self):
                raw, body, pairs = b"", {}, []
                valid = False
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length < 0:
                        raise ValueError("negative body length")
                    raw = self.rfile.read(length)
                    body = json.loads(raw)
                    valid = isinstance(body, dict)
                    if valid:
                        pairs = json.loads(raw, object_pairs_hook=lambda entries: entries)
                    else:
                        body = {}
                except (ValueError, UnicodeError):
                    pass
                parts = urlsplit(self.path)
                nonces = NONCE.findall(raw.decode(errors="replace"))
                header_lines = list(self.headers.items())
                headers = {key.lower(): value for key, value in header_lines}
                owner.hits.append(Hit(parts.path.lstrip("/").split("/")[0], parts.path, parts.query, self.command,
                    sorted(headers), {k: v for k, v in headers.items() if k.startswith("x-claude-code-") or k in (
                        "user-agent", "anthropic-dangerous-direct-browser-access", "authorization", "x-api-key")},
                    raw, body, [key for key, _ in pairs], nonces[-1] if nonces else None,
                    hint_markers((value for _, value in header_lines), raw)))
                result = ((respond(parts.path, body, raw, headers) if respond else fake_reply(parts.path, body, raw))
                          if valid and self.command == "POST" else
                          (404, "application/json", b'{"error":"unexpected fake upstream request"}'))
                status, content_type, data = result[:3]
                extra_headers = result[3] if len(result) == 4 else {}
                if status == 404:
                    owner.unexpected += 1
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                for name, value in extra_headers.items():
                    self.send_header(name, value)
                self.send_header(MARKER, "1")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    if isinstance(data, ChunkedBody):
                        data.write(self.wfile)
                    else:
                        self.wfile.write(data)

        self.server = probe.UnixHTTPServer(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass(repr=False)
class Reply:
    status: int
    headers: dict[str, str]
    raw: bytes
    hits: list[Hit] = field(default_factory=list)
    nonce: str | None = None

    def json(self):
        return json.loads(self.raw)

    def events(self):
        return [json.loads(line[6:]) for line in self.raw.decode().splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"]


def unix_request(path, method, target, *, headers=None, body=None, timeout=2) -> Reply:
    connection = probe.UnixHTTPConnection(path, timeout=timeout)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    finally:
        connection.close()


def descendant_gateway(parent_pid: int, binary: str) -> int:
    pending, seen = [parent_pid], set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            if Path(f"/proc/{pid}/exe").resolve(strict=True) == Path(binary):
                return pid
            for task in Path(f"/proc/{pid}/task").iterdir():
                pending.extend(map(int, (task / "children").read_text().split()))
        except (OSError, ValueError):
            continue
    raise AssertionError("isolated gateway descendant not found")


def socket_rows(text: str, *, listening=True) -> list[tuple[str, int]]:
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if listening and fields[3] != "0A":
            continue
        address, port = fields[1].split(":")
        raw = bytes.fromhex(address)
        if sys.byteorder == "little":
            raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
        rows.append((str(ipaddress.ip_address(raw)), int(port, 16)))
    return sorted(rows)


def loopback_listener(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip == ipaddress.ip_address("127.0.0.1") or ip == ipaddress.ip_address("::1") or (
        isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped == ipaddress.ip_address("127.0.0.1"))


def listener_rows(parent_pid: int, binary: str, *, udp=False) -> list[tuple[str, int]]:
    """Method B. Never read a socket table until child netns is verified."""
    pid = descendant_gateway(parent_pid, binary)
    if os.readlink(f"/proc/{pid}/ns/net") == os.readlink("/proc/self/ns/net"):
        raise AssertionError("refusing caller namespace listener inspection")
    return sorted(row for name in (("udp", "udp6") if udp else ("tcp", "tcp6"))
                  for row in socket_rows(Path(f"/proc/{pid}/net/{name}").read_text(), listening=not udp))


# Method A runs INSIDE the isolated namespace. Share the exact parser with B;
# -I -S prevents an ambient sitecustomize or import path from entering the child.
def listener_probe_source():
    import inspect
    return ("import ipaddress, json, subprocess, sys, time\nfrom pathlib import Path\n"
            + inspect.getsource(socket_rows) + LISTENER_PROBE)


LISTENER_PROBE = r"""
port = int(sys.argv[1])
child = subprocess.Popen(sys.argv[2:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = sorted(row for name in ("tcp", "tcp6")
                      for row in socket_rows(Path("/proc/self/net/" + name).read_text()))
        if any(p == port for _, p in rows):
            print("LISTEN=" + json.dumps(rows), flush=True)
            break
        if child.poll() is not None:
            raise RuntimeError("OAuth child exited before callback listener")
        time.sleep(0.02)
    else:
        raise RuntimeError("OAuth callback listener deadline exceeded")
finally:
    child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
"""


class GatewayHarness:
    def __init__(self, variant="main", *, census_lines=None, management=False, config_secret=False,
                 document_factory=None, upstream_factory=FakeUpstream):
        if threading.current_thread() is not threading.main_thread():
            raise AssertionError("isolated gateway must start on the main thread")
        self.binary, reason = find_gateway_binary()
        if self.binary is None:
            boundary("BOUNDARY: " + reason)
        reason = probe.network_isolation_available()
        if reason:
            boundary("BOUNDARY: " + reason)
        self.root = Path(tempfile.mkdtemp(prefix="gwtest-")).resolve()
        self.process = self.upstream = None
        self.egress, self.port = ports()
        try:
            state.ensure_private_dir(self.root)
            self.home, self.run = self.root / "home", self.root / "run"
            spec = json.loads((SERVICE_SPEC).read_text())
            for directory in (self.home, self.run, *(self.home / p for p in spec["private_dirs"]), self.home / ".local/share/claude-multi-release"):
                state.ensure_private_dir(directory)
            bundle = catalog.load_catalog(FIXTURE_ROOT)
            if document_factory is not None:
                # The hint client's factory supplies native-selector routing without mutating captured requests.
                self.document, self.aliases, self.info = document_factory(bundle, self.egress, self.port, self.home)
            elif census_lines is None:
                self.document, self.aliases, self.info = build_document(bundle, self.egress, self.port, self.home, variant)
            else:
                self.document, self.aliases, self.info = build_census_document(bundle, self.egress, self.port, self.home, census_lines)
            if config_secret:
                self.document["remote-management"]["secret-key"] = "dummy-gwtest-config-secret"
            self.config = self.home / ".config/claude-multi/config.yaml"
            state.atomic_write(self.config, render.emit_yaml(self.document).encode())
            self.socket = self.run / "gateway.sock"
            self.log_path = self.root / "gateway.log"
            self.upstream = upstream_factory(self.run / "upstream.sock")
            view = probe.HomeView(self.home, tuple(self.home / p for p in spec["bind_rw"]),
                                  tuple(self.home / p for p in spec["bind_ro"]))
            with self.log_path.open("wb") as log:
                self.process = probe.start_isolated_process(
                    [self.binary, "--config", str(self.config), "--local-model"],
                    env={"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                         **({"MANAGEMENT_PASSWORD": "dummy-gwtest-mgmt"} if management else {})},
                    cwd=self.home / spec["working_directory"], home_view=view,
                    bridges=[probe.PortBridge(self.egress, self.run / "upstream.sock", "egress"),
                             probe.PortBridge(self.port, self.socket, "ingress")], stdout=log, stderr=subprocess.STDOUT,
                )
            self.await_ready()
            match = re.search(r"CLIProxyAPI Version:\s*v?(\d+)\.(\d+)\.(\d+)", self.log_path.read_text(errors="replace"))
            if not match or tuple(map(int, match.groups())) < (7, 3, 15):
                boundary("BOUNDARY: launched gateway version unknown or predates 7.3.15")
            self.version = ".".join(match.groups())
            EVIDENCE.binary = dict(binary_identity(self.binary, self.version))
        except BaseException:
            self.close()
            raise

    def diagnostic(self, message):
        tail = self.log_path.read_text(errors="replace")[-2048:]
        tail = tail.replace(FIXTURE_GATEWAY_TOKEN, "[fixture-token]")
        tail = re.sub(r"(?:dummy-gwtest|gwtesthint-|keyedtest-key-)[^\s\"']*", "[fixture-value]", tail)
        return f"{message}; ports egress={self.egress} gateway={self.port}; log tail: {tail}"

    def headers(self):
        return {"Authorization": "Bearer " + FIXTURE_GATEWAY_TOKEN,
                "anthropic-version": "2023-06-01", "Content-Type": "application/json",
                "Host": f"127.0.0.1:{self.port}", "User-Agent": "gwtest-harness/1"}

    def get(self, target):
        return unix_request(self.socket, "GET", target, headers=self.headers())

    def await_ready(self):
        deadline = time.monotonic() + (40 if os.environ.get("NIX_BUILD_TOP") else 20)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(self.diagnostic(f"gateway exited rc={self.process.returncode}"))
            try:
                if self.get("/healthz").status == 200 and WATCHER_READY in self.log_path.read_text():
                    reply = self.get("/v1/models")
                    if reply.status == 200 and render.document_sentinel(self.document) in {row["id"] for row in reply.json()["data"]}:
                        return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        raise AssertionError(self.diagnostic("gateway readiness deadline exceeded"))

    def request(self, alias, *, stream=False, mode=None, body=None, headers=None, raw=None, path="/v1/messages"):
        nonce = "gwtest-case-" + uuid.uuid4().hex[:12]
        tag = nonce + (" gwtest-mode=" + mode if mode else "")
        payload = {"model": alias, "max_tokens": 64, "stream": stream,
                   "messages": [{"role": "user", "content": tag}]}
        if body:
            payload.update(copy.deepcopy(body))
            # Preserve multi-turn inputs, appending the current case and mode
            # after any earlier tags retained in replayed reasoning.
            if "messages" in body:
                payload["messages"].append({"role": "user", "content": tag})
        wire = raw if raw is not None else json.dumps(payload).encode()
        if raw is not None:
            found = NONCE.findall(raw.decode(errors="replace"))
            if not found:
                raise ValueError("raw request needs a gwtest case nonce")
            nonce = found[-1]
        before = self.upstream.unexpected
        reply = unix_request(self.socket, "POST", path, headers={**self.headers(), **(headers or {})}, body=wire, timeout=45)
        reply.nonce = nonce
        # A replay can retain a previous case tag inside assistant reasoning.
        # Match this request's unique tag, not the first tag in the body.
        reply.hits = [hit for hit in self.upstream.hits if hit.nonce == nonce]
        if MARKER in reply.headers or self.upstream.unexpected != before or self.process.poll() is not None:
            raise AssertionError(self.diagnostic("gateway request invariant failed"))
        return reply

    def close(self):
        if self.process is not None:
            probe.stop_isolated_process(self.process)
            self.process = None
        if self.upstream is not None:
            self.upstream.close()
            self.upstream = None
        shutil.rmtree(self.root)


_MAIN = None


def main_harness() -> GatewayHarness:
    global _MAIN
    if _MAIN is None:
        _MAIN = GatewayHarness()
        atexit.register(close_main_harness)
    return _MAIN


def close_main_harness():
    global _MAIN
    if _MAIN is not None:
        _MAIN.close()
        _MAIN = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--sandbox-proof", action="store_true")
    action.add_argument("--check-modules", action="store_true")
    action.add_argument("--validate-evidence", type=Path)
    action.add_argument("--validate-keyed-evidence", type=Path)
    parser.add_argument("--keyed-core-only", action="store_true")
    parser.add_argument("--keyed-gateway-only", action="store_true",
                        help="validate the gateway side (core and audit tables) without the client proof")
    action.add_argument("--validate-published-evidence", type=Path,
                        help="content check of the gateway check's read-only Nix-store evidence copy")
    action.add_argument("--run", action="store_true")
    action.add_argument("--run-reproducers", action="store_true")
    args = parser.parse_args(argv)
    if args.sandbox_proof:
        print(json.dumps(sandbox_proof(), sort_keys=True))
    elif args.check_modules:
        print(" ".join(gateway_check_selections()))
    elif args.validate_keyed_evidence:
        validate_keyed_evidence(args.validate_keyed_evidence, core_only=args.keyed_core_only,
                                gateway_only=args.keyed_gateway_only)
    elif args.validate_evidence:
        validate_evidence(args.validate_evidence)
    elif args.validate_published_evidence:
        validate_evidence(args.validate_published_evidence, published=True)
    elif args.run_reproducers:
        # A green execution gate here means complete observations, never that
        # the defects are fixed. Keep its wall time outside ordinary G7/G8.
        from tests import test_gateway_regressions as regressions
        destination = os.environ.get(EVIDENCE_ENV)
        if not destination:
            raise AssertionError("reproducer runner requires an evidence directory")
        directory = state.ensure_private_dir(Path(destination))
        report = directory / "reproducers.json"
        report.unlink(missing_ok=True)  # never leave a previous attempt looking current
        started = time.monotonic()
        suite = unittest.defaultTestLoader.loadTestsFromModule(regressions)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful() or result.skipped:
            return 1
        regressions.validate_report(json.loads(report.read_text()))
        print(f"reproducer execution wall time: {time.monotonic() - started:.3f}s (observation only)")
        return 0 if result.wasSuccessful() and not result.skipped else 1
    else:
        try:
            suite = unittest.defaultTestLoader.loadTestsFromNames(gateway_check_selections())
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            destination = os.environ.get(EVIDENCE_ENV)
            if not destination:
                raise AssertionError("runner requires an evidence directory")
            validate_evidence(Path(destination))
            return 0 if result.wasSuccessful() and not result.skipped else 1
        finally:
            close_main_harness()
    return 0


if __name__ == "__main__":
    # Test modules must share this runner's singleton and evidence instance.
    sys.modules["_gateway_harness"] = sys.modules[__name__]
    raise SystemExit(main())
