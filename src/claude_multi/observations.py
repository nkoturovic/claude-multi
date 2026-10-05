"""Small, effect-free public report vocabulary.

Collectors, not renderers, own minimization. Values are scalars: no source
response, exception, credential object or raw log can be serialized accidentally.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import re

Scalar = str | int | float | bool | None


def instant(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def identifier(value: object) -> str | None:
    """A metadata identifier, never an email, path or arbitrary terminal text."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:#\[\]-]{0,255}", value):
        return value
    return None


@dataclass(frozen=True)
class Fact:
    code: str
    subject_kind: str
    subject_id: str | None
    status: str
    value: Scalar = None
    observed_at: datetime | None = None
    source: str = "local"
    freshness: str = "unknown"
    coverage: str = "partial"
    reason: str | None = None
    classification: str = "info"

    def __post_init__(self):
        if self.status not in {"known", "unknown", "unavailable", "invalid"}:
            raise ValueError("invalid fact status")
        if self.classification not in {"block", "attention", "info"}:
            raise ValueError("invalid fact classification")
        if self.subject_id is not None and identifier(self.subject_id) is None:
            raise ValueError("invalid fact subject")
        if self.value is not None and type(self.value) not in {str, int, float, bool}:
            raise ValueError("fact values must be minimized scalars")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("non-finite fact value")
        if self.status != "known" and self.value is not None:
            raise ValueError("unknown fact must have a null value")

    def document(self) -> dict:
        result = asdict(self)
        result["observed_at"] = instant(self.observed_at)
        return result


SEVERITIES = ("block", "attention", "info")
DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# A remedy is one command line or one short sentence, never a log or an
# exception body: printable, single line, bounded.
_REMEDY = re.compile(r"^[^\x00-\x1f\x7f]{1,240}$")


def status_of(levels) -> str:
    """The one health reduction every surface uses: any ``block`` is
    ``blocked``, else any ``attention`` is ``attention``, else ``ready``."""

    found = set(levels)
    return "blocked" if "block" in found else "attention" if "attention" in found else "ready"


@dataclass(frozen=True)
class Diagnostic:
    """One finding: a stable kebab-case ``code``, its ``severity``, the
    text, an optional safe ``subject_id`` (``identifier``) and an optional
    structured ``remedy`` (one command or sentence)."""

    severity: str
    code: str
    text: str
    subject_id: str | None = None
    remedy: str | None = None

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            raise ValueError("invalid diagnostic severity")
        if not isinstance(self.code, str) or not DIAGNOSTIC_CODE.fullmatch(self.code):
            raise ValueError("invalid diagnostic code")
        if self.subject_id is not None and identifier(self.subject_id) is None:
            raise ValueError("invalid diagnostic subject")
        if self.remedy is not None and (not isinstance(self.remedy, str) or not _REMEDY.fullmatch(self.remedy)):
            raise ValueError("invalid diagnostic remedy")


@dataclass(frozen=True)
class Report:
    report: str
    generated_at: datetime
    coverage: dict[str, str]
    facts: tuple[Fact, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def status(self) -> str:
        return status_of([f.classification for f in self.facts] + [d.severity for d in self.diagnostics])

    def document(self) -> dict:
        return {"schema_version": 1, "report": self.report,
                "generated_at": instant(self.generated_at), "status": self.status,
                "coverage": self.coverage, "facts": [f.document() for f in self.facts],
                "diagnostics": [asdict(d) for d in self.diagnostics]}

    def json(self) -> str:
        return json.dumps(self.document(), ensure_ascii=True, allow_nan=False, indent=2) + "\n"


def legacy_diagnostic(severity: str, text: str) -> Diagnostic:
    """Fail-closed bridge until a legacy producer has typed fields.

    Legacy lines may interpolate arbitrary exception bodies. A blacklist cannot
    sanitize those reliably. Keep a recognized static topic only; plain doctor
    retains the detailed, terminal-sanitized diagnostic and remedy.
    """
    topics = ("local gateway", "session record", "state marker", "scope", "quota",
              "gateway credential save", "Contract", "Shared daemon", "Sessions",
              "Evidence", "managed", "profile", "binding", "custom", "native contract")
    topic = next((t for t in topics if text.casefold().startswith(t.casefold())), "local check")
    return Diagnostic(severity, "legacy-line", f"{topic}: {severity}; details and remedy in plain claude-multi doctor")


def error_report(name: str) -> Report:
    """No exception body enters stdout, including failed report collection."""
    return Report(name, datetime.now(timezone.utc), {}, diagnostics=(
        Diagnostic("block", "report-unavailable", "Report could not be collected; run without --json for the diagnostic and remedy."),))
