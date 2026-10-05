"""Fixture helpers for .github/scripts/journey.sh (standard library only).

The journey runs this file with the installed bundle's own Python, so it
works on hosts without one. Nothing here contacts anything but loopback
listeners it starts itself, except ``client``, which only reads the
installed release's pin.

  serve --port-file F --log F
      a keyless OpenAI-compatible chat endpoint on 127.0.0.1 (an ephemeral
      port, written to F once listening). The Nth chat request is answered
      "fixture reply N"; each request is logged as one JSON line of
      metadata (method, path, model, the earlier fixture replies its
      conversation carried), never a message text.
  carried --log F --reply N --earlier M
      exit 0 when the request answered "fixture reply N" carried "fixture
      reply M" back (a resumed conversation), else 1.
  answer -- CMD [ARG...]
      run CMD on a pseudo-terminal, answer "y" to every "[y/N]" question,
      keep the credentials when an uninstall asks whether to delete them
      (Enter), and exit with its status (a journey has no human at the
      keyboard; it only ever confirms requests to these fixtures and never
      types the phrase that deletes credentials).
  listen --port N --log F [--host H] [--port-file F]
      a listener on H:N (default 127.0.0.1; port 0 picks one, written to
      the port file) that answers every request and logs whether it carried
      credentials, until it is killed.
  addresses
      the host's non-loopback IPv4 addresses, one per line.
  reach --host H --port N [--timeout S]
      exit 0 when a TCP connection to H:N opens, 1 when it does not.
  client --install DIR
      the installed release's pinned Claude Code for this host: its
      download URL, sha256 and size on one line.
  first-run --launcher CM --install DIR
      the first run's checks of a new installation (HOME's): CM doctor
      --first-run --json and CM doctor --first-run. Exit 0 (printing the
      result) only when the checks are ready, or not ready for nothing but
      the checks every fresh install fails before its setup
      (expected_unconfigured: claude, gateway, providers — nothing connected
      yet —, models and profile waiting, hooks; each by its state, its whole
      detail and its fix), with both runs agreeing; any other outcome, a
      nonzero exit for another reason included, exits 1 and says why.
  serve-files --dir D --port-file F [--cert C --key K]
      the regular files directly in D (no listing, no subdirectory) on
      127.0.0.1, an ephemeral port written to F once listening; over https
      with the certificate C and its key K when given (the Windows
      journey's loopback source of install.sh); until it is killed.
  private-dirs
      refuse any group/other-accessible mutable product directory in HOME;
      the signed release payload keeps its archive modes and is not state.
  ended --install DIR [--timeout S]
      wait (S seconds at most, default 60) until every managed session
      record of the state root (HOME's, or XDG_STATE_HOME's) has recorded
      its end, as uninstall requires before it removes anything without
      --force. Exit 0 printing how many there are; else 1, naming each
      session still open by the first 8 characters of its id and its last
      lifecycle event (metadata only, never a transcript).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "fixture-model-1"
REPLY = "fixture reply {n}"
_REPLY = re.compile(r"fixture reply ([0-9]+)")
_QUESTION = re.compile(rb"\[y/N\]\s*$")
_KEEP_CREDENTIALS = re.compile(rb"press Enter to keep them:\s*$")


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _log(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_port(path: Path, port: int) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(f"{port}\n")
    os.replace(temporary, path)


class _Chat(BaseHTTPRequestHandler):
    log_path: Path
    counter = [0]
    lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the handler protocol
        _log(self.log_path, {"method": "GET", "path": self.path})
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]}).encode())
        else:
            self._send(404, b'{"error": {"message": "not found"}}')

    def do_POST(self) -> None:  # noqa: N802 - the handler protocol
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            request = {}
        if not (self.path.rstrip("/").endswith("/chat/completions") and isinstance(request, dict)):
            _log(self.log_path, {"method": "POST", "path": self.path})
            self._send(404, b'{"error": {"message": "not found"}}')
            return
        messages = request.get("messages") if isinstance(request.get("messages"), list) else []
        earlier = sorted({int(n) for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
                          for n in _REPLY.findall(_text(m.get("content")))})
        with self.lock:
            self.counter[0] += 1
            number = self.counter[0]
        _log(self.log_path, {"method": "POST", "path": self.path, "model": request.get("model"),
                             "stream": bool(request.get("stream")), "reply": number, "carried": earlier})
        reply = REPLY.format(n=number)
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        model = request.get("model") or MODEL
        head = {"id": f"chatcmpl-fixture-{number}", "created": 0, "model": model}
        if not request.get("stream"):
            body = {**head, "object": "chat.completion", "usage": usage,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                 "finish_reason": "stop"}]}
            self._send(200, json.dumps(body).encode())
            return
        events = [{**head, "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                  for delta in ({"role": "assistant", "content": ""}, {"content": reply})]
        events.append({**head, "object": "chat.completion.chunk", "usage": usage,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
        self._send(200, body, "text/event-stream")


class _Files(BaseHTTPRequestHandler):
    directory: Path

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        pass

    def do_GET(self) -> None:  # noqa: N802 - the handler protocol
        name = self.path.split("?", 1)[0].lstrip("/")
        path = self.directory / name
        if "/" in name or not name or name.startswith(".") or path.is_symlink() or not path.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_files(directory: Path, port_file: Path, cert: Path | None, key: Path | None) -> int:
    import ssl

    handler = type("Files", (_Files,), {"directory": directory.resolve()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if cert is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    _write_port(port_file, server.server_address[1])
    server.serve_forever()
    return 0


def serve(port_file: Path, log_path: Path) -> int:
    handler = type("Chat", (_Chat,), {"log_path": log_path, "counter": [0]})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    _write_port(port_file, server.server_address[1])
    server.serve_forever()
    return 0


def carried(log_path: Path, reply: int, earlier: int) -> int:
    try:
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError) as exc:
        print(f"journey_fixture: {log_path} is unreadable ({exc})", file=sys.stderr)
        return 1
    for record in records:
        if record.get("reply") == reply:
            if earlier in record.get("carried", []):
                return 0
            print(f"journey_fixture: the request answered with reply {reply} did not carry reply {earlier}",
                  file=sys.stderr)
            return 1
    print(f"journey_fixture: no request was answered with reply {reply}", file=sys.stderr)
    return 1


def answer(argv: list[str]) -> int:
    """Run ``argv`` on a pseudo-terminal, answering every [y/N] with y."""

    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    tail = b""
    while True:
        try:
            ready, _, _ = select.select([master], [], [], 1.0)
        except InterruptedError:
            continue
        if not ready:
            continue
        try:
            data = os.read(master, 4096)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        tail = (tail + data)[-256:]
        if _QUESTION.search(tail):
            os.write(master, b"y\n")
            tail = b""
        elif _KEEP_CREDENTIALS.search(tail):
            os.write(master, b"\n")
            tail = b""
    os.close(master)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


class _Listener(BaseHTTPRequestHandler):
    log_path: Path

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        pass

    def _record(self) -> None:
        credentials = any(self.headers.get(name) is not None for name in ("Authorization", "X-Api-Key"))
        _log(self.log_path, {"method": self.command, "path": self.path, "credentials": credentials})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"ok")

    do_GET = do_POST = do_HEAD = _record  # noqa: N815 - the handler protocol


def listen(host: str, port: int, log_path: Path, port_file: Path | None) -> int:
    handler = type("Listener", (_Listener,), {"log_path": log_path})
    server = ThreadingHTTPServer((host, port), handler)
    if port_file is not None:
        _write_port(port_file, server.server_address[1])
    server.serve_forever()
    return 0


def addresses() -> int:
    found: set[str] = set()
    try:
        for _, _, _, _, address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(address[0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # sends nothing: it only selects the source address
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    for address in sorted(found):
        if not address.startswith("127.") and address != "0.0.0.0":
            print(address)
    return 0


def reach(host: str, port: int, timeout: float) -> int:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return 0
    except OSError:
        return 1


def _installed_package(install: Path) -> bool:
    """Put the installed release's package first on the import path."""

    sites = sorted(install.glob("lib/python3.*/site-packages"))
    if not sites:
        print(f"journey_fixture: {install} is not an installed release", file=sys.stderr)
        return False
    sys.path.insert(0, str(sites[0]))
    return True


def client(install: Path) -> int:
    """URL, sha256 and size of the pinned Claude Code the installation ships."""

    if not _installed_package(install):
        return 1
    from claude_multi import acquire, pin, resources_root, strict_json

    contract = strict_json.load(resources_root() / "catalog" / "native-contract.json")
    version, platform = pin.version(contract), pin.host_platform()
    record = pin.platform_record(contract, platform)
    if record is None:
        print(f"journey_fixture: the release pins no Claude Code for {platform}", file=sys.stderr)
        return 1
    print(acquire.download_url(version, platform), record["sha256"], record["size"])
    return 0


# The first-run checks, in their order (doctor --first-run's contract).
FIRST_RUN_CHECKS = ("computer", "files", "policy", "claude", "gateway", "providers", "models", "profile", "hooks")


def _shown(path: Path, home: str) -> str:
    """A pattern for ``path`` as claude-multi may print it: under HOME as
    ``~/...``, else whole (HOME as given or resolved)."""

    whole = list(dict.fromkeys((str(path), os.path.realpath(path))))
    found = list(whole)
    for item in whole:
        for base in dict.fromkeys((home, os.path.realpath(home))):
            if base and item.startswith(base.rstrip("/") + "/"):
                found.append("~/" + os.path.relpath(item, base))
    return "(?:" + "|".join(re.escape(item) for item in dict.fromkeys(found)) + ")"


def expected_unconfigured(home: str, environ: dict[str, str] | None = None) -> dict[str, tuple[str, str, str | None]]:
    """The first-run checks a fresh installation does not pass before its
    setup (the journey installs with --no-setup), by check name: the state,
    the whole detail (a pattern) and the fix each one has. A first run that
    fails or waits on any other check, or on one of these another way,
    fails the journey."""

    env = os.environ if environ is None else environ
    state = Path(env.get("XDG_STATE_HOME") or Path(home) / ".local" / "state") / "claude-multi"
    return {
        # setup --step claude has not copied the pinned Claude Code yet.
        "claude": ("fail", re.escape("not set up for claude-multi"), "claude-multi setup --step claude"),
        # setup --step gateway has not recorded the gateway's port yet.
        "gateway": ("fail", re.escape("not set up yet"), "claude-multi setup --step gateway"),
        # Providers: nothing connected yet, so models and the profile wait.
        "providers": ("fail", re.escape("nothing connected yet"), "claude-multi setup --step providers"),
        "models": ("waiting", re.escape("waits for a provider"), None),
        "profile": ("waiting", re.escape("waits for a provider"), None),
        # The session helpers are written by the first command that changes state.
        "hooks": ("fail", _shown(state / "bin" / "claude-multi-hook", home) + re.escape(" is missing"),
                  "claude-multi doctor --repair-all"),
    }


def first_run_problem(json_run: subprocess.CompletedProcess, text_run: subprocess.CompletedProcess,
                      expected: dict[str, tuple[str, str, str | None]]) -> tuple[str | None, str]:
    """``(problem, result)`` of a first run's two runs (doctor --first-run
    --json and doctor --first-run): ``problem`` is None when the run
    succeeded, and ``result`` then says how."""

    try:
        report = json.loads(json_run.stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict) or report.get("version") != 1 or not isinstance(report.get("ready"), bool) \
            or not isinstance(report.get("items"), list) or not isinstance(report.get("info"), list):
        return f"doctor --first-run --json printed no first-run report (exit {json_run.returncode})", ""
    items = report["items"]
    if not all(isinstance(item, dict) for item in items) or [item.get("id") for item in items] != list(
            FIRST_RUN_CHECKS):
        return "doctor --first-run --json does not list the nine checks in order", ""
    ready = report["ready"]
    wanted = 0 if ready else 1
    if json_run.returncode != wanted:
        return f"doctor --first-run --json exited {json_run.returncode} with ready {str(ready).lower()}", ""
    if text_run.returncode != json_run.returncode:
        return f"doctor --first-run exited {text_run.returncode}, doctor --first-run --json {json_run.returncode}", ""
    failing = [item for item in items if item.get("state") in ("fail", "waiting")]
    if ready != (not failing) or report.get("next") != (failing[0]["id"] if failing else None):
        return "doctor --first-run --json disagrees with itself (ready, next and the failing checks)", ""
    lines = text_run.stdout.splitlines()
    summary = "Ready." if ready else f"Not ready: {len(failing)} item(s) need a fix"
    if not lines or lines[0] != "claude-multi doctor --first-run" or not lines[-1].startswith(summary):
        return "doctor --first-run and doctor --first-run --json disagree", ""
    if ready:
        return None, "ready"
    matched: list[str] = []
    for item in failing:
        check, state, detail = item["id"], item.get("state"), item.get("detail")
        fix = item.get("fix")
        fix_text = fix.get("text") if isinstance(fix, dict) else None
        condition = expected.get(check)
        if condition is None or condition[0] != state or not isinstance(detail, str) \
                or not re.fullmatch(condition[1], detail) or fix_text != condition[2]:
            return f"the first run {state}s a check a fresh install passes: {check} ({detail}; fix: {fix_text})", ""
        if not any(detail in line for line in lines):
            return f"doctor --first-run does not show the {check} check", ""
        matched.append(check)
    return None, f"not ready only for what a fresh install has not set up yet ({', '.join(matched)})"


def first_run(launcher: str, install: Path) -> int:
    """doctor's first run on a new installation (see the module docstring)."""

    if not _installed_package(install):
        return 1
    expected = expected_unconfigured(os.environ.get("HOME", ""))
    runs = []
    for argv in ([launcher, "doctor", "--first-run", "--json"], [launcher, "doctor", "--first-run"]):
        try:
            runs.append(subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                                       check=False))
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"journey_fixture: {' '.join(argv[1:])} did not run: {exc}", file=sys.stderr)
            return 1
    problem, result = first_run_problem(runs[0], runs[1], expected)
    if problem is not None:
        print(f"journey_fixture: the first run failed: {problem}", file=sys.stderr)
        for run in runs:  # doctor's own report, for the log (it never prints a secret)
            sys.stderr.write("".join(f"  | {line}\n" for line in (run.stdout + run.stderr).splitlines()[-40:]))
        return 1
    print(result)
    return 0


def session_ends(root: Path) -> list[tuple[str, str | None]]:
    """The managed session records under the state root ``root``: ``(id
    prefix, last lifecycle event)`` each, the event "end" once the session
    recorded its end. A record that cannot be read has no event (uninstall
    counts it as possibly running too)."""

    from claude_multi import sessions, strict_json

    directory = root / "sessions"
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    found: list[tuple[str, str | None]] = []
    for name in names:
        stem = name.removesuffix(".json")
        if name == stem or not sessions.UUID4.fullmatch(stem):
            continue
        try:
            event = sessions._lifecycle_view(strict_json.load(directory / name)).get("last_event_source")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            event = None
        found.append((stem[:8], event if isinstance(event, str) else None))
    return found


def ended(install: Path, timeout: float) -> int:
    """Wait for every managed session to record its end (module docstring)."""

    if not _installed_package(install):
        return 1
    from claude_multi import paths

    root = paths.state_root()
    deadline = time.monotonic() + timeout
    while True:
        records = session_ends(root)
        still = [(prefix, event) for prefix, event in records if event != "end"]
        if not still or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if still:
        for prefix, event in still:
            print(f"journey_fixture: session {prefix} has not recorded its end after {timeout:g} s "
                  f"(last event: {event or 'unreadable record'})", file=sys.stderr)
        return 1
    print(f"{len(records)} managed session record(s), every one ended")
    return 0


def private_dirs(home: Path) -> int:
    """Check mutable product directories, not the immutable release payload."""

    def xdg(name: str, fallback: Path) -> Path:
        value = os.environ.get(name, "")
        return Path(value) if os.path.isabs(value) else fallback

    data = home / ".local/share/claude-multi"
    roots = (data, xdg("XDG_CONFIG_HOME", home / ".config") / "claude-multi",
             xdg("XDG_STATE_HOME", home / ".local/state") / "claude-multi")
    count = 0
    for root in roots:
        if not root.exists():
            continue
        for parent, children, _files in os.walk(root):
            path = Path(parent)
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                print(f"state directory {path} has group/other access ({mode:04o})", file=sys.stderr)
                return 1
            count += 1
            if path == data / "install/versions":
                # Signed, read-only bundle contents deliberately carry 0755
                # directory modes. Their enclosing mutable directories do not.
                children.clear()
    print(f"private state directories: ok ({count} checked)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="journey_fixture.py")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve")
    serve_cmd.add_argument("--port-file", type=Path, required=True)
    serve_cmd.add_argument("--log", type=Path, required=True)
    carried_cmd = commands.add_parser("carried")
    carried_cmd.add_argument("--log", type=Path, required=True)
    carried_cmd.add_argument("--reply", type=int, required=True)
    carried_cmd.add_argument("--earlier", type=int, required=True)
    answer_cmd = commands.add_parser("answer")
    answer_cmd.add_argument("argv", nargs=argparse.REMAINDER)
    listen_cmd = commands.add_parser("listen")
    listen_cmd.add_argument("--host", default="127.0.0.1")
    listen_cmd.add_argument("--port", type=int, required=True)
    listen_cmd.add_argument("--log", type=Path, required=True)
    listen_cmd.add_argument("--port-file", type=Path)
    commands.add_parser("addresses")
    commands.add_parser("private-dirs")
    reach_cmd = commands.add_parser("reach")
    reach_cmd.add_argument("--host", required=True)
    reach_cmd.add_argument("--port", type=int, required=True)
    reach_cmd.add_argument("--timeout", type=float, default=3.0)
    client_cmd = commands.add_parser("client")
    client_cmd.add_argument("--install", type=Path, required=True)
    first_run_cmd = commands.add_parser("first-run")
    first_run_cmd.add_argument("--launcher", required=True)
    first_run_cmd.add_argument("--install", type=Path, required=True)
    files_cmd = commands.add_parser("serve-files")
    files_cmd.add_argument("--dir", type=Path, required=True)
    files_cmd.add_argument("--port-file", type=Path, required=True)
    files_cmd.add_argument("--cert", type=Path)
    files_cmd.add_argument("--key", type=Path)
    ended_cmd = commands.add_parser("ended")
    ended_cmd.add_argument("--install", type=Path, required=True)
    ended_cmd.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.port_file, args.log)
    if args.command == "carried":
        return carried(args.log, args.reply, args.earlier)
    if args.command == "answer":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not command:
            parser.error("answer needs a command after --")
        return answer(command)
    if args.command == "listen":
        return listen(args.host, args.port, args.log, args.port_file)
    if args.command == "addresses":
        return addresses()
    if args.command == "private-dirs":
        return private_dirs(Path(os.environ["HOME"]))
    if args.command == "client":
        return client(args.install)
    if args.command == "first-run":
        return first_run(args.launcher, args.install)
    if args.command == "serve-files":
        if (args.cert is None) != (args.key is None):
            parser.error("serve-files: --cert and --key go together")
        return serve_files(args.dir, args.port_file, args.cert, args.key)
    if args.command == "ended":
        return ended(args.install, args.timeout)
    return reach(args.host, args.port, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
