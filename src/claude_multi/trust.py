"""Release signature verification: OpenSSH signatures over Ed25519 (stdlib only).

claude-multi releases publish ``SHA256SUMS`` with a detached signature made by
``ssh-keygen -Y sign`` (the ``PROTOCOL.sshsig`` format) under the namespace
``claude-multi-release``. This module verifies such a signature the way
``ssh-keygen -Y verify`` does, against an ``allowed_signers`` document, so the
self-update path needs no OpenSSH installation:

- the armored blob is parsed strictly (magic, version 1, no trailing bytes,
  an empty reserved field, SHA-512 message prehash only);
- the namespace must equal the expected one, and an allowed signer line must
  name the principal, the exact public key, a matching ``namespaces=`` list
  (when present) and a validity window (``valid-after``/``valid-before``)
  containing the verification time — that is how a key rotation is stated;
- Ed25519 is verified per RFC 8032 with the strict rules: canonical point
  encodings for the key and R, ``S < L``, no small-order key or R.

Only Ed25519 signatures are accepted. Certificate-authority lines never match
a plain key. Every refusal raises :class:`TrustError` with a reason.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import ClaudeMultiError

RELEASE_NAMESPACE = "claude-multi-release"
RELEASE_PRINCIPAL = "release@claude-multi"
SUMS_NAME = "SHA256SUMS"
SIGNATURE_SUFFIX = ".sshsig"

_MAGIC = b"SSHSIG"
_SIG_VERSION = 1
_KEY_TYPE = "ssh-ed25519"
_HASH = "sha512"
_ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_ARMOR_END = "-----END SSH SIGNATURE-----"
_MAX_FIELD = 64 * 1024
_MAX_SIGNATURE_TEXT = 64 * 1024
_MAX_SUMS_TEXT = 1024 * 1024


class TrustError(ClaudeMultiError, ValueError):
    """A signature, signer list or checksum list failed verification."""


# --------------------------------------------------------------- Ed25519 core
# RFC 8032 section 5.1 over edwards25519, extended coordinates (X, Y, Z, T)
# with x = X/Z, y = Y/Z and x*y = T/Z.

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, P - 2, P)) % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)
_D2 = (2 * _D) % P

Point = tuple[int, int, int, int]
_IDENTITY: Point = (0, 1, 1, 0)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_BASE_Y = 4 * pow(5, P - 2, P) % P
_BASE_X = _recover_x(_BASE_Y, 0)
assert _BASE_X is not None
BASE: Point = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % P)


def _add(p: Point, q: Point) -> Point:
    """The complete twisted-Edwards addition (RFC 8032 5.1.4); doubles too."""

    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * _D2 * t2 % P
    d = z1 * 2 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(scalar: int, point: Point) -> Point:
    result = _IDENTITY
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _equal(p: Point, q: Point) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % P == 0 and (p[1] * q[2] - q[1] * p[2]) % P == 0


def _encode(point: Point) -> bytes:
    zinv = pow(point[2], P - 2, P)
    x = point[0] * zinv % P
    y = point[1] * zinv % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes) -> Point | None:
    """Strict decoding: 32 bytes, canonical ``y < p``, no ``-0``."""

    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _small_order(point: Point) -> bool:
    return _equal(_mul(8, point), _IDENTITY)


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 Ed25519 verification with the strict checks; never raises."""

    if len(public_key) != 32 or len(signature) != 64:
        return False
    key = _decode(public_key)
    if key is None or _small_order(key):
        return False
    r_bytes = signature[:32]
    r_point = _decode(r_bytes)
    if r_point is None or _small_order(r_point):
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    k = int.from_bytes(hashlib.sha512(r_bytes + public_key + message).digest(), "little") % L
    return _equal(_mul(s, BASE), _add(r_point, _mul(k, key)))


# ------------------------------------------------------------ SSH wire format


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def take(self, count: int) -> bytes:
        end = self._pos + count
        if count < 0 or end > len(self._data):
            raise TrustError("signature blob is truncated")
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def uint32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def string(self) -> bytes:
        length = self.uint32()
        if length > _MAX_FIELD:
            raise TrustError("signature blob field is too long")
        return self.take(length)

    def text(self) -> str:
        raw = self.string()
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise TrustError("signature blob text field is not ASCII") from exc

    def finish(self) -> None:
        if self._pos != len(self._data):
            raise TrustError("signature blob has trailing bytes")


def _wire_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _ed25519_key_from_blob(blob: bytes) -> bytes:
    reader = _Reader(blob)
    key_type = reader.text()
    if key_type != _KEY_TYPE:
        raise TrustError(f"unsupported key type {key_type!r} (only {_KEY_TYPE})")
    key = reader.string()
    reader.finish()
    if len(key) != 32:
        raise TrustError("malformed Ed25519 public key")
    return key


def fingerprint(key_blob: bytes) -> str:
    """The ``SHA256:…`` fingerprint ``ssh-keygen -l`` prints for a key blob."""

    digest = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")


@dataclass(frozen=True)
class Signature:
    """A parsed ``PROTOCOL.sshsig`` blob."""

    key_blob: bytes
    namespace: str
    hash_algorithm: str
    signature: bytes  # the raw 64-byte Ed25519 signature

    @property
    def public_key(self) -> bytes:
        return _ed25519_key_from_blob(self.key_blob)


def dearmor(text: str | bytes) -> bytes:
    """The blob inside ``-----BEGIN SSH SIGNATURE-----`` armor."""

    if isinstance(text, bytes):
        if len(text) > _MAX_SIGNATURE_TEXT:
            raise TrustError("signature file is too large")
        try:
            text = text.decode("ascii")
        except UnicodeDecodeError as exc:
            raise TrustError("signature file is not ASCII") from exc
    if len(text) > _MAX_SIGNATURE_TEXT:
        raise TrustError("signature file is too large")
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) < 3 or lines[0] != _ARMOR_BEGIN or lines[-1] != _ARMOR_END:
        raise TrustError("not an SSH signature (armor markers missing)")
    body = "".join(lines[1:-1])
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", body):
        raise TrustError("SSH signature armor is not base64")
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustError("SSH signature armor is not base64") from exc


def parse_signature(text: str | bytes) -> Signature:
    """Parse an armored ``ssh-keygen -Y sign`` signature (strict)."""

    reader = _Reader(dearmor(text))
    if reader.take(6) != _MAGIC:
        raise TrustError("not an SSH signature (bad magic)")
    version = reader.uint32()
    if version != _SIG_VERSION:
        raise TrustError(f"unsupported SSH signature version {version}")
    key_blob = reader.string()
    namespace = reader.text()
    reserved = reader.string()
    hash_algorithm = reader.text()
    signature_blob = reader.string()
    reader.finish()
    if reserved:
        raise TrustError("SSH signature reserved field is not empty")
    _ed25519_key_from_blob(key_blob)
    inner = _Reader(signature_blob)
    sig_type = inner.text()
    if sig_type != _KEY_TYPE:
        raise TrustError(f"unsupported signature type {sig_type!r} (only {_KEY_TYPE})")
    raw = inner.string()
    inner.finish()
    if len(raw) != 64:
        raise TrustError("malformed Ed25519 signature")
    return Signature(key_blob=key_blob, namespace=namespace, hash_algorithm=hash_algorithm, signature=raw)


def signed_data(namespace: str, message: bytes, hash_algorithm: str = _HASH) -> bytes:
    """The bytes the signer's key signs (``PROTOCOL.sshsig`` "signed data")."""

    if hash_algorithm != _HASH:
        raise TrustError(f"unsupported signature hash {hash_algorithm!r} (only {_HASH})")
    digest = hashlib.sha512(message).digest()
    return (
        _MAGIC
        + _wire_string(namespace.encode("ascii"))
        + _wire_string(b"")
        + _wire_string(hash_algorithm.encode("ascii"))
        + _wire_string(digest)
    )


# ------------------------------------------------------------ allowed signers

_OPTION_NAMES = ("cert-authority", "namespaces", "valid-after", "valid-before")
_TIME_FORMATS = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}


@dataclass(frozen=True)
class AllowedSigner:
    """One ``allowed_signers`` line (``ssh-keygen(1)`` ALLOWED SIGNERS)."""

    principals: str
    key_type: str
    key_blob: bytes
    namespaces: str | None = None
    valid_after: int | None = None
    valid_before: int | None = None
    cert_authority: bool = False
    line: int = 0

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key_blob)


def parse_time(value: str) -> int:
    """``YYYYMMDD[HHMM[SS]][Z]``: UTC with ``Z``, local time without (as OpenSSH)."""

    utc = value.endswith("Z") or value.endswith("z")
    digits = value[:-1] if utc else value
    pattern = _TIME_FORMATS.get(len(digits))
    if pattern is None or not digits.isdigit():
        raise TrustError(f"invalid signer time {value!r}")
    try:
        parsed = time.strptime(digits, pattern)
    except ValueError as exc:
        raise TrustError(f"invalid signer time {value!r}") from exc
    if utc:
        return calendar.timegm(parsed)
    return int(time.mktime(parsed))


def _split_field(text: str) -> tuple[str, str]:
    """The next whitespace-delimited field; double quotes group, never nest."""

    text = text.lstrip()
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            return text[:index], text[index:].lstrip()
    if quoted:
        raise TrustError("unterminated quote")
    return text, ""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _split_options(text: str) -> list[str]:
    parts: list[str] = []
    current = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if quoted:
        raise TrustError("unterminated quote in signer options")
    parts.append("".join(current))
    return parts


def _looks_like_options(token: str) -> bool:
    return "=" in token or "," in token or token.lower() == "cert-authority"


def parse_allowed_signers(text: str) -> tuple[AllowedSigner, ...]:
    """Parse an allowed_signers document; any malformed line refuses all.

    Lines with other key types parse but never match (only Ed25519 is
    verified). Unknown options refuse: a signer policy this verifier cannot
    honour must not silently widen.
    """

    signers: list[AllowedSigner] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            principals, rest = _split_field(line)
            token, rest = _split_field(rest)
            options: str | None = None
            if _looks_like_options(token):
                options = token
                token, rest = _split_field(rest)
            key_type = token
            encoded, _comment = _split_field(rest)
            principals = _unquote(principals)
            if not principals or not key_type or not encoded:
                raise TrustError("expected principals, key type and key")
            try:
                blob = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise TrustError("key is not base64") from exc
            embedded = _Reader(blob).text()
            if embedded != key_type:
                raise TrustError(f"key type {key_type!r} does not match the key ({embedded!r})")
            namespaces: str | None = None
            valid_after: int | None = None
            valid_before: int | None = None
            cert_authority = False
            for option in _split_options(options) if options is not None else []:
                name, sep, value = option.partition("=")
                name = name.strip().lower()
                if name == "cert-authority" and not sep:
                    cert_authority = True
                    continue
                if name not in _OPTION_NAMES or not sep:
                    raise TrustError(f"unsupported signer option {option!r}")
                if not (len(value) >= 2 and value[0] == value[-1] == '"'):
                    raise TrustError(f"signer option {name} needs a quoted value")
                value = value[1:-1]
                if name == "namespaces":
                    namespaces = value
                elif name == "valid-after":
                    valid_after = parse_time(value)
                else:
                    valid_before = parse_time(value)
            signers.append(AllowedSigner(
                principals=principals, key_type=key_type, key_blob=blob, line=number,
                namespaces=namespaces, valid_after=valid_after, valid_before=valid_before,
                cert_authority=cert_authority,
            ))
        except TrustError as exc:
            raise TrustError(f"allowed signers line {number}: {exc}") from exc
    return tuple(signers)


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    for char in pattern:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.DOTALL)


def match_pattern_list(value: str, patterns: str) -> bool:
    """OpenSSH pattern-list semantics: ``*``/``?`` globs, ``!`` negates and wins."""

    matched = False
    for pattern in patterns.split(","):
        negated = pattern.startswith("!")
        body = pattern[1:] if negated else pattern
        if body and _pattern_regex(body).fullmatch(value):
            if negated:
                return False
            matched = True
    return matched


# ------------------------------------------------------------------- verify


@dataclass(frozen=True)
class Verified:
    principal: str
    namespace: str
    fingerprint: str
    line: int


def _signers(allowed: str | Sequence[AllowedSigner]) -> Sequence[AllowedSigner]:
    if isinstance(allowed, str):
        return parse_allowed_signers(allowed)
    return allowed


def verify(
    message: bytes,
    signature: str | bytes,
    allowed_signers: str | Sequence[AllowedSigner],
    *,
    namespace: str = RELEASE_NAMESPACE,
    principal: str = RELEASE_PRINCIPAL,
    now: float | None = None,
) -> Verified:
    """Verify ``signature`` over ``message`` like ``ssh-keygen -Y verify``.

    ``now`` is the verification time (default: the current time) the signer
    validity window is checked against.
    """

    parsed = parse_signature(signature)
    if parsed.namespace != namespace:
        raise TrustError(
            f"signature namespace {parsed.namespace!r} is not {namespace!r}"
        )
    data = signed_data(parsed.namespace, message, parsed.hash_algorithm)
    if not ed25519_verify(parsed.public_key, data, parsed.signature):
        raise TrustError("signature does not verify (the file or the signature was altered)")
    moment = time.time() if now is None else now
    key_fp = fingerprint(parsed.key_blob)
    reasons: list[str] = []
    for signer in _signers(allowed_signers):
        if signer.cert_authority or signer.key_blob != parsed.key_blob:
            continue
        if not match_pattern_list(principal, signer.principals):
            continue
        if signer.namespaces is not None and not match_pattern_list(parsed.namespace, signer.namespaces):
            reasons.append(f"line {signer.line}: namespace not allowed for this key")
            continue
        if signer.valid_after is not None and moment < signer.valid_after:
            reasons.append(f"line {signer.line}: key not yet valid")
            continue
        if signer.valid_before is not None and moment > signer.valid_before:
            reasons.append(f"line {signer.line}: key expired")
            continue
        return Verified(principal=principal, namespace=parsed.namespace, fingerprint=key_fp, line=signer.line)
    detail = "; ".join(reasons) if reasons else "no allowed signer line names this key for the principal"
    raise TrustError(f"signing key {key_fp} is not trusted for {principal!r} ({detail})")


# ------------------------------------------------------------ SHA256SUMS

_SUMS_LINE = re.compile(r"([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9._+-]*)")


def parse_sums(text: str | bytes) -> dict[str, str]:
    """``sha256sum`` output: one ``<hex>  <name>`` per line, names unique and flat."""

    if isinstance(text, bytes):
        if len(text) > _MAX_SUMS_TEXT:
            raise TrustError("checksum list is too large")
        try:
            text = text.decode("ascii")
        except UnicodeDecodeError as exc:
            raise TrustError("checksum list is not ASCII") from exc
    sums: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = _SUMS_LINE.fullmatch(line)
        if match is None:
            raise TrustError(f"checksum list line {number} is malformed")
        digest, name = match.groups()
        if name in sums:
            raise TrustError(f"checksum list names {name} twice")
        sums[name] = digest
    if not sums:
        raise TrustError("checksum list is empty")
    return sums


def verify_sums(
    sums_bytes: bytes,
    signature: str | bytes,
    allowed_signers: str | Sequence[AllowedSigner],
    *,
    now: float | None = None,
) -> dict[str, str]:
    """Verify a signed ``SHA256SUMS`` and return its entries."""

    verify(sums_bytes, signature, allowed_signers, now=now)
    return parse_sums(sums_bytes)


# ------------------------------------------------------------ installed trust

RELEASE_TRUST = ("release-trust", "allowed_signers")


def release_trust_path() -> Path:
    """The packaged release trust: ``data/release-trust/allowed_signers``
    inside the running release (never a selected resource override)."""

    import claude_multi

    return claude_multi.resources_root().joinpath(*RELEASE_TRUST)


def release_signers(path: Path | str | None = None) -> tuple[AllowedSigner, ...]:
    """The signers an installed release trusts: the packaged
    ``allowed_signers``, parsed strictly, with at least one Ed25519 key for
    :data:`RELEASE_PRINCIPAL`.

    This is the only trust an installation verifies updates with. A signer
    named for one command (a test or bootstrap key on a command line) may
    authenticate that one bootstrap; it never replaces or extends this file.
    A build whose file names no key refuses here, so nothing is ever
    accepted without a signature check.
    """

    target = Path(path) if path is not None else release_trust_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrustError(f"the release trust {target} cannot be read ({exc})") from exc
    signers = parse_allowed_signers(text)
    usable = [signer for signer in signers
              if not signer.cert_authority and signer.key_type == _KEY_TYPE
              and match_pattern_list(RELEASE_PRINCIPAL, signer.principals)]
    if not usable:
        raise TrustError("this build carries no release signing key (a development build); "
                         "it cannot verify a release with its own trust")
    return signers
