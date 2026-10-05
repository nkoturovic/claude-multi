"""Recipe for the frozen test asset root ``tests/fixtures/assets/``.

Usage (from the repository root)::

    python3 tests/fixtures/build_fixture_assets.py tests/fixtures/catalog32-models.json [SHIPPED_ROOT [OUT_DIR]]

``tests/fixtures/catalog32-models.json`` is the frozen input: a checked-in
catalog-32 models file, never derived from the shipped catalog. Defaults:
SHIPPED_ROOT is the packaged resources (src/claude_multi/data), OUT_DIR is
``tests/fixtures/assets``. The output is COMMITTED and FROZEN: tests never
run this script and never regenerate the fixture from the shipped catalog,
so adding/removing a shipped model changes no test outcome. Re-run it only
as a deliberate fixture change (then review the diff, re-bless the goldens,
and expect id/count assertions to move).

The fixture holds the seven model keys the default composition references
plus three line classes (direct scalar ``max`` = qwen38, 500K = grok46,
llm-local lead-only = qwen-flash-next), all eight providers, prose
neutralised so the fixture cannot be mistaken for shipped truth,
``gateway.json`` with ``patches: []`` (patch parity stays a shipped test)
and native-contract executable paths under ``/nonexistent`` so fixture
tests can never resolve the real pinned binary.

What this recipe (re)writes (catalog v2):

- ``catalog/models.json``: the same ten keys, taken from the frozen
  catalog-32 file (NOT the shipped catalog: catalog 33 renamed or removed
  most of them), prose neutralised, converted to models v2 with
  ``tests/fixtures/convert_models_v1_to_v2.py`` and the
  ``GENERATIONS`` table below; every line ``status: active``.
- ``catalog/models.json`` also carries the aggregator line class: the
  ``FAMILIES`` table gives ``grok46`` its declared family, and the
  ``AGGREGATOR_LINES`` literal adds five OpenRouter lines of declared
  families (anthropic, openai, google, deepseek and a 500K x-ai line), so
  the ``openrouter`` seed resolves on the fixture with the shipped seed's
  slot families (tests that turn ``grok46`` New never touch a seed).
- ``catalog/retired.json``: the ``FIXTURE_RETIRED`` literal (one retired
  key, ``muse-spark`` on ``meta``, successor null).
- byte copies from SHIPPED_ROOT: ``schemas/``, ``settings.json``,
  ``catalog/prompts/`` and ``catalog/roles.json`` (``test_fixture_assets``
  guards them byte-equal): roles v2 (ten role ids) and six prompts, one per
  function: ``cm-lead``, ``cm-explorer``, ``cm-analyst``,
  ``cm-implementer``, ``cm-reviewer``, ``cm-designer``.
- ``catalog/profiles/<name>.json``: the seed profiles from
  the ``FIXTURE_SEEDS`` literal below, NEVER copied from the shipped seeds
  (the fixture has no astra, luna or sonnet line and one effort per
  Anthropic line). They are frozen literals that mirror each shipped
  seed's slot -> family map, ``native_agents``, ``workflows``,
  ``lead_providers``, ``primary_provider`` and ``seed``; the guard is
  ``test_fixture_assets.test_fixture_seeds_mirror_shipped_structure``. The
  directory is removed and rewritten from the literal on every run.

Frozen, never rewritten here (edit deliberately, then re-bless):
``catalog/providers.json`` (catalog-32 providers, support notes
neutralised, llm-local base URL ``.invalid``; the aggregator ``openrouter``
has independence family ``unknown``, as shipped), ``catalog/gateway.json``,
``catalog/native-contract.json`` (mirrors the shipped contract except the
``claude`` block) and ``version.json`` (catalog 31).

The catalog-32 default composition is 2.x test input,
``tests/fixtures/v3/default-composition.json`` (sha256 pinned by
``test_fixture_assets``), read only by ``tests/_v3.py`` and never by
``load_catalog``. This recipe neither writes nor reads it.
"""

import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))  # tests/, for _layout

from _layout import FIXTURES_ROOT, RESOURCES_ROOT  # noqa: E402

from convert_models_v1_to_v2 import v1_to_v2_entry  # noqa: E402

if len(sys.argv) < 2:
    sys.exit(__doc__)
CATALOG32_MODELS = Path(sys.argv[1])
SHIPPED = Path(sys.argv[2]) if len(sys.argv) > 2 else RESOURCES_ROOT
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else FIXTURES_ROOT / "assets"

# key -> generation
GENERATIONS = {
    "fable": "5",
    "gpt55": "5.5",
    "grok46": "4.6",
    "kimi-k3": "3",
    "opus": "4.8",
    "opus5": "5",
    "opus55": "5.5",
    "qwen-flash-next": "3.8",
    "qwen38": "3.8",
    "sol": "5.6",
}
KEEP = [
    "fable", "gpt55", "kimi-k3", "opus", "opus5", "opus55", "sol",  # the 2.x default composition's availability (tests/fixtures/v3/)
    "qwen38",           # direct claude-compatible, scalar_tokens, max lane
    "grok46",           # 500K class (grok profile), two lanes
    "qwen-flash-next",  # llm-local, lead-only, flash431 profile
]
# Declared families of the aggregator lines (models v2 ``family``).
FAMILIES = {"grok46": "x-ai"}


def _aggregator_line(key, family, wire, display, generation, default_effort, tokens=1000000):
    """One fixture OpenRouter line (high and xhigh output contracts; the 1M
    class unless ``tokens`` is smaller)."""

    suffix = "[1m]" if tokens >= 1000000 else ""
    return {
        "capabilities": ["lead", "agents"],
        "context": {
            "client_tokens": tokens,
            "declared_tokens": tokens,
            "ordinary_profile": "large" if tokens >= 1000000 else "grok",
            "provider_tokens": tokens,
            "qualification": "Fixture context; not benchmark-verified.",
            "scalar_tokens": None,
            "validated_tokens": 200000,
        },
        "default_effort": default_effort,
        "display": display,
        "efforts": {
            level: {"proxy_contract": f"output-config-{level}", "selector": f"claude-multi-{key}-{level}{suffix}"}
            for level in ("high", "xhigh")
        },
        "family": family,
        "generation": generation,
        "lead": {"effort": "ultracode", "env": {}},
        "minimum_tested": {"claude_code": "2.1.216", "cliproxyapi": "7.2.80"},
        "provider": "openrouter",
        "registry_overlay": None,
        "roles": "all",
        "routing_note": f"Fixture line {key}: frozen test data, never re-synced from the shipped catalog.",
        "status": "active",
        "wire_model": wire,
    }


AGGREGATOR_LINES = {
    "or-opus": _aggregator_line("or-opus", "anthropic", "anthropic/claude-opus-5.5",
                                "Opus 5.5 · OpenRouter", "5.5", "xhigh"),
    "or-gpt": _aggregator_line("or-gpt", "openai", "openai/gpt-6-sol", "GPT-6 Sol · OpenRouter", "6", "high"),
    "or-gemini": _aggregator_line("or-gemini", "google", "google/gemini-3-flash",
                                  "Gemini 3 Flash · OpenRouter", "3", "high"),
    "or-deepseek": _aggregator_line("or-deepseek", "deepseek", "deepseek/deepseek-v4-flash",
                                    "DeepSeek V4 Flash · OpenRouter", "4", "high"),
    "or-grok": _aggregator_line("or-grok", "x-ai", "x-ai/grok-4.7", "Grok 4.7 · OpenRouter", "4.7", "high",
                                tokens=500000),
}
FIXTURE_RETIRED = {
    "retired": {
        "muse-spark": {
            "capabilities": ["lead", "agents"],
            "context_tokens": 1000000,
            "display": "Muse Spark 1.3",
            "last_wire": "muse-spark-1.3",
            "provider": "meta",
            "reason": "Fixture retired line: frozen test data.",
            "roles": "all",
            "selectors": {
                "claude-multi-muse-spark-high[1m]": "output-config-high",
                "claude-multi-muse-spark-xhigh[1m]": "output-config-xhigh",
            },
            "since_catalog": 31,
            "successor": None,
        }
    },
    "version": 1,
}


# The shipped seeds' ``seed.version`` markers, mirrored (FixtureSeedGuardTests):
# catalog 34 bumped the three seeds whose Luna slots moved.
SEED_VERSIONS = {"balanced": 3, "quality": 2, "max": 2, "economy": 3, "openai": 3, "claude": 2}


def _seed(name, lead, agents, **extra):
    """One fixture seed profile; ``agents`` maps id -> (model, effort)."""

    document = {
        "agents": {rid: {"effort": effort, "model": model} for rid, (model, effort) in agents.items()},
        "description": (
            f"Fixture seed {name}: frozen test data mirroring the shipped seed's slot families."
        ),
        "lead": {"effort": "ultracode", "model": lead},
        "name": name,
        "native_agents": {"explore": "replace", "general_purpose": "off", "plan": "native"},
        "seed": {"id": name, "version": SEED_VERSIONS.get(name, 1)},
        "version": 2,
        "workflows": "native",
    }
    document.update(extra)
    return document


def _lineup(explorer, analyst, analyst_strong, light, implementer, strong, reviewer, reviewer_strong):
    return {
        "cm-explorer": explorer,
        "cm-analyst": analyst,
        "cm-analyst-strong": analyst_strong,
        "cm-implementer-light": light,
        "cm-implementer": implementer,
        "cm-implementer-strong": strong,
        "cm-reviewer": reviewer,
        "cm-reviewer-strong": reviewer_strong,
    }


SOL_H, SOL_X = ("sol", "high"), ("sol", "xhigh")
OR_OPUS_X, OR_GPT_H, OR_GPT_X = ("or-opus", "xhigh"), ("or-gpt", "high"), ("or-gpt", "xhigh")
OR_DS_H, OR_GEMINI_H, OR_GROK_X = ("or-deepseek", "high"), ("or-gemini", "high"), ("or-grok", "xhigh")
OPUS55_X, OPUS5_X, FABLE_M = ("opus55", "xhigh"), ("opus5", "xhigh"), ("fable", "max")
# Slot families equal the shipped seed's (anthropic <-> anthropic,
# openai <-> openai), so routing and warning codes match the shipped seeds.
FIXTURE_SEEDS = {
    "balanced": _seed("balanced", "opus55", _lineup(
        SOL_H, SOL_H, OPUS55_X, SOL_H, SOL_H, SOL_X, SOL_X, OPUS55_X)),
    "quality": _seed("quality", "opus55", _lineup(
        SOL_H, SOL_H, SOL_X, SOL_H, SOL_H, OPUS5_X, SOL_X, OPUS55_X)),
    "max": _seed("max", "fable", _lineup(
        SOL_H, SOL_H, FABLE_M, SOL_H, SOL_H, SOL_X, SOL_X, OPUS55_X)),
    "economy": _seed("economy", "opus55", _lineup(
        SOL_H, SOL_H, SOL_X, SOL_H, SOL_H, SOL_X, SOL_H, OPUS55_X)),
    "claude": _seed("claude", "opus55", _lineup(
        OPUS5_X, OPUS5_X, OPUS55_X, OPUS5_X, OPUS5_X, OPUS55_X, OPUS5_X, FABLE_M),
        primary_provider="anthropic"),
    "openai": _seed("openai", "sol", _lineup(
        SOL_H, SOL_H, SOL_X, SOL_H, SOL_H, SOL_X, SOL_H, SOL_X),
        primary_provider="openai"),
    "openrouter": _seed("openrouter", "or-opus", _lineup(
        OR_DS_H, OR_GPT_H, OR_GPT_X, OR_DS_H, OR_GPT_H, OR_OPUS_X, OR_GEMINI_H, OR_GROK_X),
        primary_provider="openrouter"),
    "direct": _seed("direct", "opus55", {},
        lead_providers=["anthropic"],
        native_agents={"explore": "native", "general_purpose": "on", "plan": "native"}),
}


def dump(rel, doc):
    (OUT / rel).write_text(
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


for rel in ("schemas", "catalog/prompts"):
    if (OUT / rel).exists():
        shutil.rmtree(OUT / rel)
    shutil.copytree(SHIPPED / rel, OUT / rel)
for rel in ("settings.json", "catalog/roles.json"):
    shutil.copy(SHIPPED / rel, OUT / rel)

providers = json.loads((OUT / "catalog" / "providers.json").read_text(encoding="utf-8"))["providers"]
models = json.loads(CATALOG32_MODELS.read_text(encoding="utf-8"))["models"]
fixture_models = {}
for key in KEEP:
    entry = copy.deepcopy(models[key])
    entry["routing_note"] = f"Fixture line {key}: frozen test data, never re-synced from the shipped catalog."
    entry["context"]["qualification"] = "Fixture context; not benchmark-verified."
    fixture_models[key] = v1_to_v2_entry(key, entry, providers[entry["provider"]], GENERATIONS[key])
    if key in FAMILIES:
        fixture_models[key]["family"] = FAMILIES[key]
fixture_models.update(copy.deepcopy(AGGREGATOR_LINES))
dump("catalog/models.json", {"models": fixture_models, "version": 2})
dump("catalog/retired.json", FIXTURE_RETIRED)
if (OUT / "catalog" / "profiles").exists():
    shutil.rmtree(OUT / "catalog" / "profiles")
(OUT / "catalog" / "profiles").mkdir()
for name, seed in FIXTURE_SEEDS.items():
    dump(f"catalog/profiles/{name}.json", seed)
print(f"wrote {OUT}")
