"""Fixture isolation: a shipped-catalog model addition moves no test or golden.

Usage (from the repository root; NOT auto-discovered by unittest)::

    python3 tests/check_fixture_isolation.py [--keep] [--python-guard-only] [--retired-probe] [--operator-probe]

Three probes, each in its own disposable copy, run in order by default:

- **probe 1**: a fake model line in the shipped ``models.json``;
- **probe 2**: a fake retired key in the shipped ``retired.json``
  (``--retired-probe`` runs only this one) — retiring a line must move no
  test and no golden either;
- **probe 3**: a valid operator declaration (``providers.d``) and a
  valid operator ledger (an admission of its line) in the **probe HOME**
  the suite runs under (``--operator-probe`` runs only this one) — operator
  state on the machine must move no test and no golden, and no test may
  write it. The OVERALL line names all three (the receipt inventory).

What probe 1 does, all inside a disposable copy (probe 2 differs only in
step 2, see ``_add_fake_retired``):

1. Copies the repository tree to a temp dir, minus VCS metadata,
   ``.claude/``, build links and bytecode. The real checkout is only read.
2. Adds one well-formed fake model line to the COPY's shipped
   ``catalog/models.json`` (models v2): a clone of an existing lead-capable
   direct gateway-effort line (same provider, so no provider entry is
   needed) with a unique id and wire, one effort whose selector follows the
   shipped shape ``claude-multi-<key>-<effort>[1m]``, ``status: active``,
   ``minimum_tested.claude_code`` at the 2.1.216 baseline (<= the pinned
   client) and in no composition. It asserts ``validate_catalog == []`` and
   the shipped selector-shape rules on the copy before going on.
3. Regenerates the copy's documentation with ``tools/docs_gen.py``. Only
   the catalog-derived ``model-lines``, ``shipped-profiles``,
   ``openrouter-lines`` and ``provider-index`` region bodies may change;
   all other documentation bytes and the file set must stay identical.
   The operator-state probe allows no documentation changes.
4. Runs the full offline suite in the copy with the live gateway blocked:
   by default inside a private user+network namespace whose only interface is
   a fresh loopback (127.0.0.1:8317 simply does not exist there, for every
   process, including PTY children and real-binary probes), with all
   capabilities dropped before the suite starts. A Python connect tripwire
   (sitecustomize on PYTHONPATH) additionally logs any attempt to reach
   127.0.0.1:8317/8316. ``--python-guard-only`` skips the namespace (for hosts
   without unprivileged user namespaces; PTY children are then unguarded).
   If the ONLY failures are real-binary probe classes (``test_scope_probe``,
   ``test_client_*`` real-client modules — they run the pinned Claude binary and are
   environment-sensitive, AGENTS.md §4), those classes are re-run once
   outside the namespace with the tripwire still armed.
5. Runs ``tests/bless.py --check`` in the copy and diffs the whole ``tests/goldens``
   tree (file set and bytes) against the real checkout.

Exit 0 only if every probe run has zero suite failures/errors, no golden
changed, and no process attempted to reach the live gateway. Exit 1
otherwise; exit 2 when an experiment could not be set up.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Run as a script, the tests directory is sys.path[0]; imported (test_harness),
# it is on PYTHONPATH. _layout is the one root source.
from _layout import REPO_ROOT, RESOURCES_RELATIVE  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))

from claude_multi import catalog, continuity, operator, state, strict_json  # noqa: E402

FAKE_MODEL_ID = "isolation-probe"
FAKE_SELECTOR_BASE = "claude-multi-isolation-probe"
FAKE_WIRE = "isolation-probe-1"
FAKE_RETIRED_ID = "isolation-retired"
FAKE_RETIRED_SELECTOR_BASE = "claude-multi-isolation-retired"
FAKE_RETIRED_WIRE = "isolation-retired-1"
FAKE_OPERATOR_PROVIDER = "isolation-operator"
FAKE_OPERATOR_KEY = "custom-isolation-operator"
FAKE_OPERATOR_WIRE = "isolation-operator-1"
BASELINE_FLOOR = "2.1.216"
# Modules whose tests execute the real pinned Claude binary (shipped contract).
REAL_BINARY_MODULES = ("test_scope_probe", "test_client_agents", "test_client_gateway_auth", "test_client_hooks",
                       "test_client_models", "test_client_skill_workflow", "test_gateway_hint_client",
                       "test_gateway_retry_after")
# Never copied — VCS metadata, worktrees/session files, build links.
_SKIP_TOP = (".git", ".claude", "result")

GUARD_SOURCE = '''"""check_fixture_isolation tripwire: refuse and log live-gateway connects."""
import os
import socket

_LOG = os.environ["CM_ISOLATION_GUARD_LOG"]
_PORTS = {8317, 8316}
_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create = socket.create_connection


def _hit(address):
    try:
        return address[1] in _PORTS and address[0] in _HOSTS
    except Exception:
        return False


def _record(address):
    import traceback
    with open(_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"BLOCKED {address!r} pid={os.getpid()}\\n")
        handle.write("".join(traceback.format_stack(limit=12)) + "----\\n")


def connect(self, address):
    if _hit(address):
        _record(address)
        raise ConnectionRefusedError(111, "isolation guard: live gateway blocked")
    return _connect(self, address)


def connect_ex(self, address):
    if _hit(address):
        _record(address)
        return 111
    return _connect_ex(self, address)


def create_connection(address, *args, **kwargs):
    if _hit(address):
        _record(address)
        raise ConnectionRefusedError(111, "isolation guard: live gateway blocked")
    return _create(address, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.create_connection = create_connection

# No claude_multi import here: the guard also covers scope-only child paths
# without loading product modules before their own import restrictions apply.
import sys


def _listener_observation(event, args):
    if not os.environ.get("CM_ISOLATION_GUARD_LOG") or getattr(sys, "_cm_listener_tripwire", False):
        return  # the always-on in-process guard records and raises instead
    target = None
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.path.normpath(os.fsdecode(args[0]))
        if path in ("/proc/net/tcp", "/proc/net/tcp6"):
            target = path
    elif event == "subprocess.Popen":
        executable, argv = args[:2]
        if (os.path.basename(os.fsdecode(executable)) == "systemctl"
                and not isinstance(argv, (str, bytes))
                and any("MainPID" in os.fsdecode(arg) for arg in argv)):
            target = "systemctl MainPID"
    if target is not None:
        _record(("listener-owner", target))
        raise AssertionError("isolation guard: live listener observation")


sys.addaudithook(_listener_observation)
'''

PREFLIGHT = r'''
import json, os, socket
caps = {}
with open("/proc/self/status", encoding="utf-8") as handle:
    for line in handle:
        if line.startswith(("CapEff", "CapAmb", "CapPrm")):
            key, value = line.split(":", 1)
            caps[key] = int(value.strip(), 16)
probe = socket.socket()
probe.settimeout(2)
try:
    probe.connect(("127.0.0.1", 8317))
    gateway = "REACHABLE"
except OSError as exc:
    gateway = f"unreachable ({type(exc).__name__})"
finally:
    probe.close()
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
client = socket.create_connection(server.getsockname(), timeout=2)
client.close()
server.close()
print(json.dumps({"uid": os.getuid(), "caps": caps, "gateway": gateway, "loopback": "ok"}))
'''


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _copy_tree(workdir: Path) -> Path:
    """The repository tree (no VCS metadata, worktrees, build links or bytecode)."""

    bytecode = shutil.ignore_patterns("__pycache__", "*.pyc")

    def ignore(directory: str, names: list[str]) -> set[str]:
        here = Path(directory)
        skipped = set(bytecode(directory, names))
        if here == REPO_ROOT:
            skipped |= {name for name in names if name in _SKIP_TOP or name.startswith("result-")}
        return skipped

    component = workdir / "repo"
    shutil.copytree(REPO_ROOT, component, ignore=ignore)
    return component


def _resources(component: Path) -> Path:
    """The packaged resources inside the copied repository tree."""

    return component / RESOURCES_RELATIVE


def _template_line(component: Path, document: dict) -> str:
    """The first direct, lead-capable, >=1M gateway-effort shipped line."""

    raw = catalog.load_raw(_resources(component))
    providers = raw["docs"]["providers"]["providers"]
    return next(
        model_id
        for model_id, model in sorted(document["models"].items())
        if providers[model["provider"]]["transport"]["kind"] == "direct"
        and catalog.effort_mode(providers[model["provider"]]) == "gateway"
        and "lead" in model["capabilities"]
        and model["context"]["client_tokens"] >= 1_000_000
    )


def _refuse_named(component: Path, name: str) -> None:
    for path in [*_resources(component).glob("catalog/profiles/*.json"),
                 *component.glob("tests/fixtures/**/*.json")]:
        if name in path.read_text(encoding="utf-8"):
            raise SystemExit(f"setup: {path} already names {name}")


def _add_fake_model(component: Path) -> dict:
    """Clone a lead-capable direct gateway-effort line into a unique entry.

    Models v2: the template is the first direct, lead-capable, >=1M
    gateway-effort line; the fake keeps only its default effort, with the
    shipped selector shape ``claude-multi-<key>-<effort>[1m]`` and the
    template's contract for that effort (the shipped-shape test in
    test_catalog runs inside the copy, so the fake must pass it).
    """

    models_path = _resources(component) / "catalog" / "models.json"
    document = strict_json.load(models_path)
    template_id = _template_line(component, document)
    fake = copy.deepcopy(document["models"][template_id])
    default = fake["default_effort"]
    contract = fake["efforts"][default]["proxy_contract"]
    fake["efforts"] = {
        default: {
            "selector": f"{FAKE_SELECTOR_BASE}-{default}[1m]",
            "proxy_contract": contract,
        }
    }
    fake["wire_model"] = FAKE_WIRE
    fake["display"] = "Isolation Probe"
    fake["generation"] = "1"
    fake["status"] = "active"
    fake["registry_overlay"] = None
    fake["routing_note"] = "isolation probe model; exists only in a disposable copy."
    fake["minimum_tested"] = dict(fake["minimum_tested"], claude_code=BASELINE_FLOOR)
    assert FAKE_MODEL_ID not in document["models"]
    document["models"][FAKE_MODEL_ID] = fake
    models_path.write_bytes(strict_json.pretty_file_bytes(document))

    errors = catalog.validate_catalog(catalog.load_raw(_resources(component)))
    if errors:
        raise SystemExit(f"setup: fake model is not well-formed: {errors}")
    # The shipped-shape rules (test_catalog.SeedLoadTests): key + effort
    # selector, [1m] iff 1M, contract level == effort.
    for level, selector, level_contract in catalog.line_selectors(fake):
        if selector != f"claude-multi-{FAKE_MODEL_ID}-{level}[1m]":
            raise SystemExit(f"setup: fake selector {selector!r} breaks the shipped shape")
        if not level_contract.endswith("-" + level):
            raise SystemExit(f"setup: fake contract {level_contract!r} does not match {level!r}")
    bundle = catalog.load_catalog(_resources(component))
    pinned = bundle.docs["native-contract"]["verified"][0]["version"]
    if _version_key(BASELINE_FLOOR) > _version_key(pinned):
        raise SystemExit(f"setup: floor {BASELINE_FLOOR} exceeds pinned client {pinned}")
    _refuse_named(component, FAKE_MODEL_ID)
    return {
        "id": FAKE_MODEL_ID,
        "cloned_from": template_id,
        "provider": fake["provider"],
        "selector": fake["efforts"][default]["selector"],
        "minimum_tested": fake["minimum_tested"]["claude_code"],
        "pinned_client": pinned,
    }


def _add_fake_retired(component: Path) -> dict:
    """Probe 2: retire a fake key in the COPY's shipped ``retired.json``.

    The entry names the provider of probe 1's template line (so no provider
    entry is needed), one 2.x-shaped selector
    ``claude-multi-isolation-retired-<effort>[1m]`` carrying that line's
    default-effort contract, a unique wire, ``successor: null`` and
    ``since_catalog`` = the current catalog version. It asserts
    ``validate_catalog == []``, that ``resolve_key`` reports the "needs a
    model choice" notice, and that the continuity seed serves the selector
    on its recorded wire and contract — the SPEC 3.3 guarantee a retirement
    relies on. Nothing else in the copy changes.
    """

    models = strict_json.load(_resources(component) / "catalog" / "models.json")
    template_id = _template_line(component, models)
    template = models["models"][template_id]
    default = template["default_effort"]
    contract = template["efforts"][default]["proxy_contract"]
    selector = f"{FAKE_RETIRED_SELECTOR_BASE}-{default}[1m]"
    retired_path = _resources(component) / "catalog" / "retired.json"
    document = strict_json.load(retired_path)
    version = strict_json.load(_resources(component) / "version.json")["catalog_version"]
    assert FAKE_RETIRED_ID not in document["retired"]
    document["retired"][FAKE_RETIRED_ID] = {
        "successor": None,
        "reason": "isolation probe retired key; exists only in a disposable copy.",
        "since_catalog": version,
        "provider": template["provider"],
        "last_wire": FAKE_RETIRED_WIRE,
        "display": "Isolation Retired",
        "context_tokens": template["context"]["client_tokens"],
        "capabilities": ["lead", "agents"],
        "roles": "all",
        "selectors": {selector: contract},
    }
    retired_path.write_bytes(strict_json.pretty_file_bytes(document))

    errors = catalog.validate_catalog(catalog.load_raw(_resources(component)))
    if errors:
        raise SystemExit(f"setup: fake retired entry is not well-formed: {errors}")
    bundle = catalog.load_catalog(_resources(component))
    resolution = bundle.resolve_key(FAKE_RETIRED_ID)
    if resolution.key is not None or "needs a model choice" not in (resolution.notice or ""):
        raise SystemExit(f"setup: unexpected resolve_key result {resolution!r}")
    alias = continuity.seed_only(bundle)["aliases"].get(selector.removesuffix("[1m]"))
    if (
        alias is None
        or alias.get("provider") != template["provider"]
        or alias.get("wire") != FAKE_RETIRED_WIRE
        or alias.get("proxy_contract") != contract
    ):
        raise SystemExit(f"setup: continuity seed does not serve {selector}: {alias!r}")
    _refuse_named(component, FAKE_RETIRED_ID)
    return {
        "id": FAKE_RETIRED_ID,
        "provider": template["provider"],
        "selector": selector,
        "contract": contract,
        "since_catalog": version,
    }


def _add_fake_operator(component: Path, home: Path) -> dict:
    """Probe 3: operator state in the probe HOME, nothing in the copy.

    A keyless LAN provider declaration with one line in
    ``<home>/.config/claude-multi/providers.d/`` and a ledger that admits
    that line (its real definition digest). It asserts the loader resolves
    both cleanly against the COPY's shipped catalog, so a test that read
    the HOME's operator state would actually see an operator provider.
    """

    environ = {"HOME": str(home)}
    document = {
        "version": 1,
        "notes": "isolation probe 3; exists only in a disposable probe HOME.",
        "provider": {
            "display": "Isolation Operator",
            "kind": "openai-compatible-lan",
            "base_url": "http://isolation-operator.lan:8000/v1",
            "auth": {"kind": "none"},
            "independence_family": "isolation",
        },
        "lines": {
            FAKE_OPERATOR_KEY: {
                "wire_model": FAKE_OPERATOR_WIRE,
                "display": "Isolation Operator",
                "efforts": ["high"],
                "default_effort": "high",
                "context": {"declared_tokens": 65536, "source": "operator"},
            },
        },
    }
    raw = strict_json.pretty_file_bytes(document)
    directory = state.ensure_private_dir(operator.providers_dir(environ))
    state.atomic_write(directory / f"{FAKE_OPERATOR_PROVIDER}.json", raw)
    docs = catalog.load_catalog(_resources(component)).docs
    schemas = operator.load_schemas(_resources(component))
    layer = operator.validate_layer(docs, {FAKE_OPERATOR_PROVIDER: raw}, schemas=schemas)
    if layer.problems or FAKE_OPERATOR_KEY not in layer.lines:
        raise SystemExit(f"setup: fake operator declaration is not well-formed: "
                         f"{[problem.text() for problem in layer.problems]}")
    ledger = operator.ledger_document(None)
    ledger["admissions"][FAKE_OPERATOR_KEY] = operator.admission_record(
        layer, FAKE_OPERATOR_KEY, at="2026-09-30T00:00:00Z", via="admit")
    state.atomic_write(operator.ledger_path(environ), strict_json.canonical_file_bytes(ledger))
    snapshot = operator.load_snapshot(environ, docs, asset_root=_resources(component))
    if (snapshot.ledger_error is not None or snapshot.ledger is None
            or FAKE_OPERATOR_KEY not in snapshot.ledger.admissions
            or FAKE_OPERATOR_PROVIDER not in snapshot.layer.providers):
        raise SystemExit("setup: the probe HOME's operator state does not load")
    return {
        "provider": FAKE_OPERATOR_PROVIDER,
        "line": FAKE_OPERATOR_KEY,
        "home": str(home),
        "files": _operator_files(home),
    }


def _operator_files(home: Path) -> dict[str, bytes]:
    """Every file under the probe HOME's claude-multi config directory."""

    root = operator.ledger_path({"HOME": str(home)}).parent
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _netns_prefix() -> list[str] | None:
    if not all(shutil.which(tool) for tool in ("unshare", "ip", "setpriv")):
        return None
    return [
        "unshare", "--user", "--map-current-user", "--net", "--keep-caps", "--",
        "sh", "-c",
        'ip link set lo up && exec setpriv --inh-caps=-all --ambient-caps=-all -- "$@"',
        "isolation",
    ]


def _preflight(prefix: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [*prefix, sys.executable, "-c", PREFLIGHT],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"namespace launch failed: {exc}"
    if result.returncode != 0:
        return False, f"namespace preflight exit {result.returncode}: {result.stderr.strip()}"
    facts = json.loads(result.stdout.strip().splitlines()[-1])
    problems = []
    if facts["uid"] != os.getuid():
        problems.append(f"uid {facts['uid']} != {os.getuid()}")
    if any(facts["caps"].get(key) for key in ("CapEff", "CapAmb", "CapPrm")):
        problems.append(f"capabilities not dropped: {facts['caps']}")
    if facts["gateway"] == "REACHABLE":
        problems.append("127.0.0.1:8317 reachable inside the namespace")
    summary = (
        f"private net namespace (uid {facts['uid']}, caps dropped, "
        f"127.0.0.1:8317 {facts['gateway']}, loopback {facts['loopback']})"
    )
    return (not problems), (summary if not problems else "; ".join(problems))


def _goldens_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# Only these page/region pairs describe the shipped catalog. Keep the
# allowlist independent of the generator so an unrelated output cannot opt in.
CATALOG_DOC_REGIONS = (
    ("docs/guides/models.md", "model-lines"),
    ("docs/guides/profiles.md", "shipped-profiles"),
    ("docs/providers/openrouter.md", "openrouter-lines"),
    ("docs/providers/api-keys.md", "provider-index"),
)


def _docs_snapshot(component: Path) -> dict[str, bytes]:
    paths = [*(component / "docs").rglob("*"), *component.glob("*.md")]
    return {path.relative_to(component).as_posix(): path.read_bytes()
            for path in sorted(paths) if path.is_file()}


def _doc_changes(before: dict[str, bytes], after: dict[str, bytes], *, kind: str) -> tuple[list[str], list[str]]:
    """Name allowed body changes and reject every other changed documentation byte."""

    changed, problems = [], []
    allowed = () if kind == "operator" else CATALOG_DOC_REGIONS
    for path in sorted(before.keys() | after.keys()):
        if path not in before or path not in after:
            problems.append(f"{path}: document {'added' if path not in before else 'deleted'}")
            continue
        old, new = before[path], after[path]
        if old == new:
            continue
        for page, name in allowed:
            if page != path:
                continue
            begin = f"<!-- generated: {name} (tools/docs_gen.py) -->".encode()
            end = f"<!-- end of generated: {name} -->".encode()
            bodies, skeletons = [], []
            for data in (old, new):
                if data.count(begin) != 1 or data.count(end) != 1 or data.index(begin) > data.index(end):
                    problems.append(f"{path}: invalid {name} markers")
                    break
                head, rest = data.split(begin, 1)
                body, tail = rest.split(end, 1)
                if b"<!-- generated:" in body or b"<!-- end of generated:" in body:
                    problems.append(f"{path}: nested markers in {name}")
                    break
                bodies.append(body)
                skeletons.append(head + begin + end + tail)
            else:
                if bodies[0] != bodies[1]:
                    changed.append(f"{path}: {name}")
                old, new = skeletons
        if old != new:
            problems.append(f"{path}: changed outside allowed catalog region bodies")
    return changed, problems


def _refresh_catalog_docs(component: Path, env: dict[str, str], prefix: list[str], *, kind: str) -> bool:
    before = _docs_snapshot(component)
    result = subprocess.run(
        [*prefix, sys.executable, "tools/docs_gen.py"], cwd=component, env=env,
        capture_output=True, text=True, timeout=120,
    )
    changed, problems = _doc_changes(before, _docs_snapshot(component), kind=kind)
    print(f"docs: generator exit {result.returncode}; {len(changed)} catalog region(s) changed")
    for name in changed:
        print(f"  changed: {name}")
    for problem in problems:
        print(f"  refused: {problem}")
    if result.returncode != 0:
        print(result.stdout.strip(), file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
    return result.returncode == 0 and not problems


def _run_probe(args: argparse.Namespace, kind: str) -> int:
    """One probe (``model``, ``retired`` or ``operator``) in its own disposable copy."""

    workdir = Path(tempfile.mkdtemp(prefix=f"claude-multi-isolation-{kind}-"))
    ok = False
    try:
        component = _copy_tree(workdir)
        print(f"copy: {component}")
        probe_home: Path | None = None
        if kind == "operator":
            probe_home = workdir / "probe-home"
            probe_home.mkdir(mode=0o700)
            fake = _add_fake_operator(component, probe_home)
            print(
                "fake operator state in the probe HOME: provider {provider} (providers.d) and an "
                "admitted line {line} (operator-ledger.json); both load cleanly".format(**fake)
            )
        elif kind == "model":
            fake = _add_fake_model(component)
            print(
                "fake model: {id} (clone of {cloned_from}, provider {provider}, "
                "selector {selector}, minimum_tested {minimum_tested} <= pinned "
                "{pinned_client}); validate_catalog == []".format(**fake)
            )
        else:
            fake = _add_fake_retired(component)
            print(
                "fake retired key: {id} (provider {provider}, selector {selector} -> "
                "{contract}, successor null, since_catalog {since_catalog}); "
                "validate_catalog == [], resolve_key needs a choice, continuity "
                "seed serves it".format(**fake)
            )

        guard_dir = workdir / "guard"
        guard_dir.mkdir()
        (guard_dir / "sitecustomize.py").write_text(GUARD_SOURCE, encoding="utf-8")
        guard_log = workdir / "guard.log"
        guard_log.touch()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(guard_dir), str(component / "src"), str(component / "tests")]
        )
        env["CM_ISOLATION_GUARD_LOG"] = str(guard_log)
        env.pop("CLAUDE_MULTI_ASSETS", None)
        env.pop("CM_TEST_TIER", None)  # acceptance runs the full tier, never fast
        # The namespace cannot run the real gateway (bwrap is untrusted there):
        # gateway and keyed proofs belong to the host suite of the same gate,
        # so the probes use the explicit unit lane and write no gate evidence.
        for name in ("CLAUDE_MULTI_TEST_REQUIRE_GATEWAY", "CLAUDE_MULTI_TEST_KEYED_EVIDENCE",
                     "CLAUDE_MULTI_TEST_GATEWAY_LIFECYCLE_PROBES"):
            env.pop(name, None)
        env["CLAUDE_MULTI_TEST_KEYED_LANE"] = "unit"
        if probe_home is not None:
            env["HOME"] = str(probe_home)

        prefix: list[str] = []
        if args.python_guard_only:
            print("isolation: Python connect tripwire only (--python-guard-only)")
        else:
            candidate = _netns_prefix()
            if candidate is None:
                print("setup: unshare/ip/setpriv unavailable; use --python-guard-only",
                      file=sys.stderr)
                return 2
            passed, summary = _preflight(candidate)
            if not passed:
                print(f"setup: {summary}; use --python-guard-only", file=sys.stderr)
                return 2
            prefix = candidate
            print(f"isolation: {summary} + Python connect tripwire")

        if not _refresh_catalog_docs(component, env, prefix, kind=kind):
            print("RESULT: FAIL (documentation changed outside the catalog boundary)")
            return 1

        suite_log = workdir / "suite.log"
        if args.only:
            targets = list(args.only)
            print(f"suite: running only {' '.join(targets)} in the copy…", flush=True)
        else:
            targets = ["discover", "-s", "tests", "-t", "."]
            print("suite: running the full offline suite in the copy (several minutes)…",
                  flush=True)
        with suite_log.open("w", encoding="utf-8") as handle:
            suite = subprocess.run(
                [*prefix, sys.executable, "-m", "unittest", *targets],
                cwd=component, env=env, stdout=handle, stderr=subprocess.STDOUT,
                timeout=3600,
            )
        output = suite_log.read_text(encoding="utf-8", errors="replace")
        ran = re.findall(r"(?m)^Ran (\d+) tests? in", output)
        verdict = re.findall(r"(?m)^(OK|FAILED)(?: \((.*)\))?\s*$", output)
        # "FAIL: test_x (tests.mod.Class.test_x)" on 3.11+; older runtimes
        # print "(tests.mod.Class)" — normalise both to the dotted test id.
        failing = sorted(
            {
                where if where.endswith("." + name) else f"{where}.{name}"
                for name, where in re.findall(
                    r"(?m)^(?:FAIL|ERROR): (\S+) \(([^)\s]+)\)", output
                )
            }
        )
        print(
            f"suite: exit {suite.returncode}; ran {ran[-1] if ran else '?'}; "
            f"{' '.join(filter(None, verdict[-1])) if verdict else 'no verdict line'}"
        )
        for test_id in failing:
            print(f"  failing: {test_id}")
        suite_ok = suite.returncode == 0 and bool(verdict) and verdict[-1][0] == "OK"

        # Real-binary probe classes drive the pinned Claude binary against a
        # loopback fake provider; they are timing-/environment-sensitive
        # (AGENTS.md §4). Observed 2026-09-25: spike S2's --resume relaunch
        # misbehaves inside a private network namespace yet passes outside
        # it on the same copy. Such failures (and only those) are re-run ONCE
        # per class outside the namespace, with the Python tripwire still
        # armed; anything else failing is never retried.
        probe_failures = [t for t in failing if t.split(".")[1].startswith(REAL_BINARY_MODULES)]
        if prefix and failing and len(probe_failures) == len(failing):
            classes = sorted({t.rsplit(".", 1)[0] for t in probe_failures})
            print(f"suite: re-running {len(classes)} real-binary probe class(es) once "
                  "outside the namespace (tripwire armed): " + " ".join(classes))
            rerun = subprocess.run(
                [sys.executable, "-m", "unittest", *classes],
                cwd=component, env=env, capture_output=True, text=True, timeout=1800,
            )
            rerun_verdict = re.findall(r"(?m)^(OK|FAILED)(?: \((.*)\))?\s*$",
                                       rerun.stdout + rerun.stderr)
            rerun_ok = rerun.returncode == 0 and bool(rerun_verdict) and rerun_verdict[-1][0] == "OK"
            if rerun_ok:
                print("suite: probe re-run OK (namespace-sensitive real-binary "
                      "probe; not a catalog effect)")
            else:
                print("suite: probe re-run FAILED")
            suite_ok = rerun_ok
            failing = [] if rerun_ok else failing
        attempts = guard_log.read_text(encoding="utf-8").count("BLOCKED ")
        print(f"gateway: {attempts} attempted connect(s) to 127.0.0.1:8317/8316")

        before = _goldens_snapshot(REPO_ROOT / "tests" / "goldens")
        bless = subprocess.run(
            [*prefix, sys.executable, "tests/bless.py", "--check"],
            cwd=component, env=env, capture_output=True, text=True, timeout=600,
        )
        pruned = [line for line in bless.stdout.splitlines() if line.startswith("pruned ")]
        after = _goldens_snapshot(component / "tests" / "goldens")
        changed = sorted(
            name for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        )
        print(
            f"goldens: bless exit {bless.returncode}, "
            f"{len(after)} files, {len(pruned)} pruned, {len(changed)} changed"
        )
        for name in changed:
            print(f"  changed: tests/goldens/{name}")
        if bless.returncode != 0:
            print(bless.stdout.strip(), file=sys.stderr)
            print(bless.stderr.strip(), file=sys.stderr)

        operator_ok = True
        if probe_home is not None:
            operator_ok = _operator_files(probe_home) == fake["files"]
            print(f"operator: probe HOME operator state {'unchanged' if operator_ok else 'CHANGED by a test'}")
        ok = (
            suite_ok
            and not failing
            and attempts == 0
            and bless.returncode == 0
            and not changed
            and operator_ok
        )
        print(f"RESULT: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print(f"logs kept: {suite_log} {guard_log}")
        return 0 if ok else 1
    finally:
        if args.keep or not ok:
            print(f"temp copy kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="keep the temp copy")
    parser.add_argument(
        "--python-guard-only", action="store_true",
        help="no network namespace; rely on the Python tripwire only",
    )
    parser.add_argument(
        "--retired-probe", action="store_true",
        help="run only probe 2 (a fake retired key in the shipped retired.json); "
        "by default probe 1 (fake model) runs first, then probe 2",
    )
    parser.add_argument(
        "--operator-probe", action="store_true",
        help="run only probe 3 (operator declaration + ledger in the probe HOME)",
    )
    parser.add_argument(
        "--only", metavar="TEST", action="append", default=[],
        help=(
            "run only these unittest targets instead of full discovery (e.g. "
            "to re-run a timing-sensitive real-binary probe class in isolation, "
            "as AGENTS.md §4 prescribes); goldens are still diffed"
        ),
    )
    args = parser.parse_args(argv)

    if args.retired_probe and args.operator_probe:
        parser.error("--retired-probe and --operator-probe are exclusive")
    kinds = (("retired",) if args.retired_probe else ("operator",) if args.operator_probe
             else ("model", "retired", "operator"))
    labels = {"model": "probe 1 (model line)", "retired": "probe 2 (retired key)",
              "operator": "probe 3 (operator state in the probe HOME)"}
    results: dict[str, int] = {}
    for kind in kinds:
        label = labels[kind]
        print(f"=== {label}", flush=True)
        results[kind] = _run_probe(args, kind)
        if results[kind] == 2:
            return 2
    ok = all(code == 0 for code in results.values())
    if len(results) > 1:
        summary = ", ".join(
            f"{kind} {'PASS' if code == 0 else 'FAIL'}" for kind, code in results.items()
        )
        print(f"OVERALL: {'PASS' if ok else 'FAIL'} ({summary})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
