# Connect a server on your network

A model server on your own computer or local network (for example a local
model runner) can be added as a keyless provider when it speaks the
OpenAI chat protocol. Its valid models can lead a session or run `cm-*`
agents without admission, qualification or declared agent roles. Tool and
context compatibility are not guaranteed; missing or failed evidence warns.

## 1. What you need

The server's address, including its API path (for example
`http://llm.example.lan:8000/v1`), and the model ids it serves. The
address is `http` or `https`, and its host must be local: this computer
(`localhost` or a loopback address), a private IP address, a name without
a dot, or a name ending in `.lan`, `.local`, `.home.arpa` or `.internal`;
any other host is refused (“the keyless LAN kind needs a local-network
host”). No key is used: the server must accept requests without
authentication from your computer.

## Presets

Presets fill in a common server's default address and the address of its
model list, from its documentation; they ship no models. Each is
labelled `preset` (not tested by claude-multi) and is available in every
build. The generic one, `lan-openai-compatible`, is the template for any
other server; the launcher lists it first, as “Server on your network”:

| Preset | Server | Default address |
| --- | --- | --- |
| `ollama` | Ollama | `http://localhost:11434/v1` |
| `lm-studio` | LM Studio | `http://localhost:1234/v1` |
| `vllm` | vLLM | `http://localhost:8000/v1` |
| `llama-cpp` | llama.cpp (`llama-server`) | `http://localhost:8080/v1` |
| `lan-openai-compatible` | any other OpenAI-compatible server | `http://llm.lan:8000/v1`, used when you give no address |

The four named servers' default addresses are on this computer. For a server on another
computer, give its address with `--base-url` (for example
`http://llm.example.lan:11434/v1` for Ollama there), keeping the server's
port and `/v1` path. A server started with a key of its own (vLLM's
`--api-key`, for one) is not keyless: it cannot be added here. Each of
these servers lists its models, so `claude-multi discover <provider>` (one
request you agree to) shows what it serves; with Ollama, a model name
without a `:tag` means its latest tag.

On Windows with WSL2, a server running on Windows is not at `localhost`
from inside WSL unless WSL's mirrored networking mode is on; otherwise
use the Windows host's address
([Microsoft's WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking)).

## 2. What is supported

- A keyless OpenAI-compatible server (`openai-compatible-lan`), for leads
  and agents. An enabled provider and a valid routable model are required;
  admission is an optional local badge, qualification an optional diagnostic.
- There is no key and no credential-route approval step on this route.
  Local-host, endpoint and payload checks still apply.
- Servers that require a key are not supported on this keyless route. A
  keyed server needs an `https` address and must be added as an
  [Anthropic-compatible](anthropic-compatible.md) or keyed
  [OpenAI-compatible](openai-compatible.md) endpoint instead, with explicit
  credential-route approval. There is no automatic protocol fallback.

## 3. Add it

In the launcher: **G** Providers → **N** (or **W** Get started → **A**) →
a server preset or "Server on your network"; give its name (`lan` unless
you change it) and its address (Enter keeps the preset's). The flow then
moves on to adding models and offers optional admission; skipping it still
lets you select the line. Later, **A** on the provider adds more; **M** →
Enter changes the optional badge and **Q** offers diagnostics. From a terminal (the
provider takes the preset's name unless `--as` gives another):

```bash
claude-multi providers add --preset ollama                # Ollama on this computer
claude-multi discover ollama                               # one listing request, after you agree
claude-multi discover ollama --add <wire> --context <n> --as custom-local-model
claude-multi profile edit <name>         # bind custom-local-model as a lead or agent
```

Any other server, here one on another computer:

```bash
claude-multi providers add --preset lan-openai-compatible --as lanbox --base-url http://llm.example.lan:8000/v1
claude-multi models add lanbox local-model --context 32768 --source operator --as custom-local-model
claude-multi profile edit <name>         # bind custom-local-model as a lead or agent
```

The context size is yours to state: use what the server is configured to
serve (`--source operator`), since a local server's window depends on how
you started it.

You can also choose a direct lead with
`claude-multi direct --model custom-local-model`. Any nonempty, single-line
printable family label up to 64 characters is accepted (not controls or
secrets); an unrecognized label means **independence unknown** for reviews,
not an agent-use ban.

Optional `claude-multi models qualify custom-local-model --agents` checks
need a human's explicit consent to the request plan at a terminal outside
Claude Code, default **No**. They never run during selection or launch. A
small local model can overflow before the client's shared compaction trigger;
use the actual window and provider-bound warning, not an assumed per-agent
window ([models](../guides/models.md#context-windows)).

## 4. Your network, not the gateway

Two different things are local here:

- the **gateway** is claude-multi's own process; it listens on loopback
  only (127.0.0.1) and is never exposed to your network;
- the **LAN server** is your upstream: the gateway connects to it over
  HTTP (or HTTPS) on your network, carrying the session's prompts and file
  contents. Use it only on a network you trust.

## 5. Reachability is network-scoped

A server reachable at one place may be absent at another. An observed
unreachable server produces an Attention warning for a launch that uses it,
not a fast refusal; requests can still fail. Connect to the server's network
or choose another model. Unknown reachability stays unknown, and local
readiness is not upstream verification. claude-multi never substitutes
another model by itself and never probes in the background.

## 6. Costs

No provider bills you; your hardware and power do. Context size and speed
depend on your server.

## 7. Remove it

`claude-multi models rm <line>` removes a line and
`claude-multi providers rm <provider>` the server (refused while a line is
admitted, bound or used by a live session).
