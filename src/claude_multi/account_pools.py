"""Account pools: the account sign-ins this build offers, as data.

An account pool is one of the gateway's OAuth channels: the gateway program
signs a person's account in with one of its login commands, keeps the
sign-in records in its auth directory, and the pool's catalog provider
serves its models through them. The packaged ``account-pools.json`` (closed
schema ``schemas/account-pools.schema.json``) describes every pool; it is
part of the release and never comes from a resource override. A pool names:

- ``provider``: the catalog provider whose ``transport`` is this pool;
- ``display``: what a person sees (``Claude account``);
- ``client_account`` (optional, default false): the managed client signs
  in with this kind of account itself, so a sign-in here is said to be
  separate from that login;
- ``sign_in``: how the sign-in opens (``flow`` ``browser``: a page on this
  computer, or an address printed for another device; ``device``: an
  address and a one-time code), the account ``host``, the
  ``claude-multi-proxy`` login ``command`` that runs it and the
  ``signin_policy`` values (``version.json``) that offer it;
- ``acknowledgement``: the personal-use text shown before a sign-in and
  its versioned ``id`` (a changed text needs a fresh acknowledgement);
- ``record_prefix``: the file-name prefix of its sign-in records;
- ``registry``: the pinned registry ``section`` its catalog lines are
  checked against and every one of the ``sections`` its sign-ins serve
  from (the codex plan tiers, say).

A pool's name is its gateway channel: the ``oauth-model-alias`` and
``oauth-excluded-models`` key and the provider its records name. Two pools
may share a registry section (regional variants of one service), never a
provider, a login command or a record prefix.

Records are told apart by file name only; their contents (account tokens)
are never opened. ``record_prefixes`` lists every prefix the pinned gateway
names its records with, a pool's or not, and a record belongs to the
longest prefix it starts with (followed by ``-``): ``kimi-ai-1.json`` is a
``kimi-ai`` record, never a ``kimi`` one.

Adding a pool is an entry here plus its catalog provider and lines and the
gateway program's login command in ``claude-multi-proxy``; the sign-in,
acknowledgement, account listing, sign-out, registry evidence and the
render read it from here.
"""

from __future__ import annotations

import functools
import re
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import claude_multi
from claude_multi import errors, strict_json, validate

FILE = "account-pools.json"
SCHEMA = Path("schemas") / "account-pools.schema.json"
# The build's sign-in policies (``version.json`` ``signin_policy``); every
# pool is offered by the default one.
SIGNIN_POLICIES = ("public", "public-strict")
DEFAULT_POLICY = "public"
# The ways a sign-in opens, per flow: a browser flow opens a page here or
# prints the address for another device; a device flow prints an address
# and a one-time code.
FLOW_METHODS: Mapping[str, tuple[str, ...]] = {"browser": ("browser", "address"), "device": ("device",)}
METHODS = ("browser", "address", "device")
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RECORD_SUFFIX = ".json"
REMEDY = "reinstall claude-multi"


class AccountPoolsError(errors.ClaudeMultiError, ValueError):
    """The packaged account pools cannot be read or are invalid."""

    remedy = REMEDY


@dataclass(frozen=True)
class SignIn:
    flow: str
    host: str
    command: str
    policies: tuple[str, ...]

    @property
    def methods(self) -> tuple[str, ...]:
        """The methods this sign-in can open with (the setup layer picks the
        default by environment)."""

        return FLOW_METHODS[self.flow]


@dataclass(frozen=True)
class Acknowledgement:
    id: str
    text: str


@dataclass(frozen=True)
class Pool:
    name: str
    provider: str
    display: str
    sign_in: SignIn
    acknowledgement: Acknowledgement
    record_prefix: str
    registry_section: str
    registry_sections: tuple[str, ...]
    client_account: bool = False

    def offered(self, policy: str) -> bool:
        """Whether a build with sign-in ``policy`` offers this pool."""

        return policy in self.sign_in.policies

    def account(self, file_name: str) -> str:
        """The account part of one of this pool's record names."""

        return file_name[len(self.record_prefix) + 1: -len(RECORD_SUFFIX)]


@dataclass(frozen=True)
class PoolTable:
    """Every pool by name (sorted) and every record prefix of the pinned gateway."""

    pools: Mapping[str, Pool]
    record_prefixes: tuple[str, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(self.pools)

    def by_provider(self, provider_id: str) -> Pool | None:
        return next((pool for pool in self.pools.values() if pool.provider == provider_id), None)

    def record_prefix(self, file_name: str) -> str | None:
        """The longest record prefix ``file_name`` starts with (then ``-``),
        for a ``.json`` name; None for any other name."""

        if not file_name.endswith(RECORD_SUFFIX):
            return None
        best: str | None = None
        for prefix in self.record_prefixes:
            if file_name.startswith(f"{prefix}-") and (best is None or len(prefix) > len(best)):
                best = prefix
        return best

    def classify(self, file_name: str) -> str | None:
        """The pool whose record ``file_name`` is, or None (a record of a
        prefix no pool uses, or no record at all). Names only."""

        prefix = self.record_prefix(file_name)
        if prefix is None:
            return None
        return next((pool.name for pool in self.pools.values() if pool.record_prefix == prefix), None)

    def section_pools(self, section: str) -> tuple[str, ...]:
        """The pools whose sign-ins serve from registry ``section``."""

        return tuple(pool.name for pool in self.pools.values() if section in pool.registry_sections)


def _invalid(detail: str) -> AccountPoolsError:
    return AccountPoolsError(f"the packaged account pools are invalid: {detail}")


def parse(document: Any, schema: Mapping[str, Any]) -> PoolTable:
    """The pool table of ``document`` checked against ``schema`` and the
    rules the schema cannot state; raises :class:`AccountPoolsError`."""

    problems = validate.validate(document, dict(schema), "$")
    if problems:
        raise _invalid("; ".join(problems[:4]))
    prefixes = tuple(document["record_prefixes"])
    pools: dict[str, Pool] = {}
    owners: dict[tuple[str, str], str] = {}
    for name in sorted(document["pools"]):
        if not NAME.fullmatch(name):
            raise _invalid(f"pool name {name!r} must match {NAME.pattern}")
        raw = document["pools"][name]
        sign_in, registry = raw["sign_in"], raw["registry"]
        if raw["record_prefix"] not in prefixes:
            raise _invalid(f"{name}: record prefix {raw['record_prefix']!r} is not one of record_prefixes")
        if registry["section"] not in registry["sections"]:
            raise _invalid(f"{name}: registry section {registry['section']!r} is not one of its sections")
        if DEFAULT_POLICY not in sign_in["policies"]:
            raise _invalid(f"{name}: the {DEFAULT_POLICY!r} sign-in policy must offer it")
        for field, value in (("provider", raw["provider"]), ("display", raw["display"]),
                             ("login command", sign_in["command"]),
                             ("acknowledgement id", raw["acknowledgement"]["id"]),
                             ("record prefix", raw["record_prefix"])):
            other = owners.setdefault((field, value), name)
            if other != name:
                raise _invalid(f"{name} and {other} share the {field} {value!r}")
        pools[name] = Pool(
            name=name, provider=raw["provider"], display=raw["display"],
            sign_in=SignIn(sign_in["flow"], sign_in["host"], sign_in["command"], tuple(sign_in["policies"])),
            acknowledgement=Acknowledgement(raw["acknowledgement"]["id"], raw["acknowledgement"]["text"]),
            record_prefix=raw["record_prefix"], registry_section=registry["section"],
            registry_sections=tuple(registry["sections"]),
            client_account=bool(raw.get("client_account", False)),
        )
    return PoolTable(types.MappingProxyType(pools), prefixes)


def load(root: Path | str | None = None) -> PoolTable:
    """The pool table of the packaged resources (or of ``root``); read once."""

    return _load(str(Path(root) if root is not None else claude_multi.resources_root()))


@functools.lru_cache(maxsize=8)
def _load(base: str) -> PoolTable:
    path = Path(base) / FILE
    try:
        document = strict_json.load(path)
        schema = strict_json.load(Path(base) / SCHEMA)
        validate.check_schema(schema)
    except (OSError, ValueError, RecursionError) as exc:
        raise AccountPoolsError(f"the packaged account pools cannot be read ({path})") from exc
    return parse(document, schema)


# ------------------------------------------------------------------ the packaged table


def pools() -> Mapping[str, Pool]:
    """Every pool of this build, by name (sorted)."""

    return load().pools


def names() -> tuple[str, ...]:
    return load().names()


def pool(name: str) -> Pool | None:
    return load().pools.get(name)


def by_provider(provider_id: str) -> Pool | None:
    """The pool of catalog provider ``provider_id``, or None."""

    return load().by_provider(provider_id)


def classify(file_name: str) -> str | None:
    """The pool whose sign-in record ``file_name`` is (names only)."""

    return load().classify(file_name)


def section_pools(section: str) -> tuple[str, ...]:
    """The pools whose sign-ins serve from registry ``section``."""

    return load().section_pools(section)
