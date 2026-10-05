"""Synthetic secrets as a gateway would log them, one per shape the log
redactor knows. Every value is assembled here at run time from dummy
pieces, so the tree never carries a credential-shaped literal."""

from __future__ import annotations

_HEX = "0123456789abcdef"


def secrets_and_lines() -> list[tuple[str, str]]:
    """``(secret, log line)`` pairs: the secret must never survive."""

    gateway_key = _HEX * 4
    management_key = _HEX[::-1] * 4
    anthropic = "sk-ant-api03-" + "dummy" * 6
    groq = "gsk_" + "Dummy0Key1" * 4
    google = "AIza" + "DummyKey0" * 4
    mixed = "AbCd1234" * 4
    jwt = "eyJ" + "dummyheader" * 2 + ".eyJ" + "dummypayload" * 2 + ".dummysig"
    email = "fixture.person@example.com"
    cookie = "session=dummy-cookie-value; other=dummy-two"
    bearer = "dummy-bearer-token-value"
    stamp = "[2026-10-02 12:00:00] [a1b2c3d4] [info ] [gin_logger.go:92]"
    return [
        (gateway_key, f"{stamp} 401 | 1ms | 127.0.0.1 | POST \"/v1/messages\" key {gateway_key} rejected"),
        (management_key, f"{stamp} management request X-Management-Key: {management_key}"),
        (bearer, f"{stamp} request headers Authorization: Bearer {bearer}"),
        (bearer, f'{stamp} headers map[Authorization:["Bearer {bearer}"] Content-Type:[application/json]]'),
        (anthropic, f"{stamp} upstream said: invalid x-api-key: {anthropic}"),
        (anthropic, f'{stamp} upstream error {{"error": "Incorrect API key provided: {anthropic}"}}'),
        (groq, f"{stamp} provider=groq key {groq} refused"),
        (google, f"{stamp} GET \"/v1beta/models?key={google}&alt=sse\""),
        (mixed, f"{stamp} provider=custom-vendor api_key={mixed} status=401"),
        (jwt, f"{stamp} oauth refresh returned access_token={jwt}"),
        (email, f"{stamp} model=claude-multi-opus provider=claude auth=claude-{email}.json"),
        (email, f"{stamp} account {email} quota exhausted"),
        ("dummy-cookie-value", f"{stamp} Cookie: {cookie}"),
        ("dummy-password", f"{stamp} proxy http://user:dummy-password@proxy.invalid:3128 refused"),
    ]


STAMP = "[2026-10-02 12:00:00] [a1b2c3d4] [info ] [gin_logger.go:92]"


def structured_cases() -> list[tuple[tuple[str, ...], str]]:
    """``(secrets, log line)``: whole structured values — a quoted string
    with spaces (escaped inside a JSON string too), arrays of keys (nested
    as well), JSON account fields and a whole sensitive header value with a
    quoted cookie. No piece of a secret may survive."""

    phrase = "dummy pass phrase"
    return [
        (("dummy cookie value", "cookie value", "dummy-csrf-value"),
         f'{STAMP} Cookie: session="dummy cookie value"; csrf=dummy-csrf-value'),
        (("dummy-key-one", "dummy-key-two"), f'{STAMP} config {{"api_keys": ["dummy-key-one","dummy-key-two"]}}'),
        (("dummy-key-one", "dummy-key-two"),
         f'{STAMP} config {{"api_keys": [["dummy-key-one"], ["dummy-key-two"]], "port": 1}}'),
        (("dummy-account-7",), f'{STAMP} auth file {{"account_id": "dummy-account-7", "type": "claude"}}'),
        (("dummy-user-name",), f"{STAMP} auth file {{'login': 'dummy-user-name'}}"),
        ((phrase, "pass phrase"), f'{STAMP} upstream proxy password="{phrase}" refused'),
        ((phrase, "pass phrase"), f'{STAMP} body {{"msg":"{{\\"password\\":\\"{phrase}\\"}}"}}'),
        (("dummy header value", "header value"), f"{STAMP} headers {{'x-api-key': 'dummy header value'}}"),
    ]


def escape_cases() -> list[tuple[tuple[str, ...], str]]:
    """``(secrets, log line)``: terminal escape sequences and control
    characters around a header name or between the chunks of a key."""

    return [
        ((_HEX,), f"{STAMP} 401 key {_HEX}\x1b[1m{_HEX}\x1b[0m{_HEX}\x1b[2m{_HEX} rejected"),
        ((_HEX,), f"{STAMP} 401 key {_HEX}\x00{_HEX}\u200b{_HEX}\x7f{_HEX} rejected"),
        (("dummy-basic-credential",),
         f"{STAMP} request headers Authorization\x1b[0m: Basic dummy-basic-credential"),
        (("dummy-basic-credential",),
         f"{STAMP} request headers \x1b[1mAuthorization\x1b[22m:\x1b[33m Basic dummy-basic-credential\x1b[0m"),
        (("dummy-osc-password",),
         f"{STAMP} link \x1b]8;;https://user:dummy-osc-password@proxy.invalid\x1b\\proxy\x1b]8;;\x1b\\ refused"),
    ]


def line_break_cases() -> list[tuple[tuple[str, ...], str]]:
    """``(secrets, journal record)``: a Unicode line separator, a next-line
    character, a vertical tab or a carriage return inside one record, between
    a header's name and its colon or between the colon and the value. Only a
    line feed ends a record, so none of them may split the header."""

    credential = "dummy-basic-credential"
    cases = []
    for control in (" ", "\u0085", "\x0b", "\r"):
        cases.append(((credential,), f"{STAMP} request headers Authorization{control}: Basic {credential}"))
        cases.append(((credential,), f"{STAMP} request headers X-Api-Key:{control}{credential}"))
    return cases


_BEGIN, _END = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"


def private_key_lines() -> tuple[tuple[str, ...], list[str]]:
    """``(body lines, log lines)``: a PEM private key logged across lines;
    no body line may survive, whichever line a tail starts at."""

    body = tuple(chunk * 8 for chunk in ("Ab1+cd2/", "Ef3+gh4/", "Ij5+kl6/", "Mn7+op8="))
    return body, [f"{STAMP} loading the signing key:", _BEGIN, *body, _END, *ORDINARY[:2]]


def short_key_body(count: int = 3) -> tuple[str, ...]:
    """Private-key body lines under 40 characters with no run of 24 letters
    and digits: only the key's context shows they are key material."""

    return tuple(f"Qx{index:02d}+Zk2/Wm4+Pv6/Ty8+Rn1/Hb3=" for index in range(count))


# What no shown line may hold once a key is redacted from its BEGIN marker
# through its END marker: a piece every body line carries, and the markers.
SHORT_KEY_PIECES = ("+Zk2/Wm4+", "PRIVATE " + "KEY")


def field_key_cases() -> list[list[str]]:
    """Log excerpts whose private key follows a secret field's name: across
    lines (bare, with an equals sign and a key type, quoted) and within one
    line (its line breaks escaped)."""

    body = short_key_body()
    rsa_begin, rsa_end = (marker.replace("PRIVATE", "RSA PRIVATE") for marker in (_BEGIN, _END))
    return [
        [f"{STAMP} loading private_key: {_BEGIN}", *body, _END, *ORDINARY[:2]],
        [f"{STAMP} loading private_key={rsa_begin}", *body, rsa_end, *ORDINARY[:2]],
        [f'{STAMP} auth file {{"private_key": "{_BEGIN}', *body, _END + '\\n", "type": "service_account"}',
         *ORDINARY[:2]],
        [f"{STAMP} loading private_key: {_BEGIN}\\n" + "\\n".join(body) + f"\\n{_END}", *ORDINARY[:2]],
    ]


def unfinished_key_lines(count: int = 3, *, field: bool = False) -> list[str]:
    """A log that ends inside a private key: its BEGIN line (after a field
    name when ``field``) and ``count`` short body lines, no END yet."""

    first = f"{STAMP} loading private_key: {_BEGIN}" if field else _BEGIN
    return [f"{STAMP} loading the signing key:", first, *short_key_body(count)]


# Lines that carry no secret: the redactor leaves them as they are.
ORDINARY = (
    "[2026-10-02 12:00:00] [a1b2c3d4] [info ] [gin_logger.go:92] 200 | 1.2s | 127.0.0.1 | POST \"/v1/messages?beta=true\"",
    "[2026-10-02 12:00:00] [a1b2c3d4] [debug] [selector.go:40] model=claude-multi-qwen38-max-xhigh provider=qwen",
    "credential_save_v1 operation=refresh result=persisted provider=claude auth_index=0123456789abcdef "
    "credentials_changed=true stage=none errno=0 category=none bytes=10 size=10 generation=1 epoch=1",
    'claude executor: upstream served model "claude-opus-5-5" for requested model "claude-opus-5-5" '
    "(auth_index=0123456789abcdef)",
    "claude-multi-proxy: gateway instance 0123456789abcdef starting",
    "API server started on 127.0.0.1:18329 (claude-multi-render-0123abcd)",
    "429 Too Many Requests: status code: 429, retry after 30s; input tokens: 1234",
)
