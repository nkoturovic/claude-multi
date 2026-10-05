# claude-multi lineup (lineup_generation 1)

Profile: balanced · lead class large (1M) · providers: openai 6 · anthropic 2 (+lead)

## Lead
- lead: Opus 5.5 · 1M selector · ultracode · anthropic · `claude-multi-opus-5-5[1m]`
- /model offers only the lead set (16 entries); any other model needs a relaunch with another profile.
- /model's built-in Default row is `claude-opus-4-8[1m]`, outside this lead set; it is refused.

## Agents
- `cm-explorer`: GPT-5.6 Sol · high · openai · `gpt-multi-sol-high[1m]`
- `cm-analyst`: GPT-5.6 Sol · high · openai · `gpt-multi-sol-high[1m]`
- `cm-analyst-strong`: Opus 5.5 · 1M selector · xhigh · anthropic · `claude-multi-opus-5-5[1m]` · not a step up from the lead
- `cm-implementer-light`: GPT-5.6 Sol · high · openai · `gpt-multi-sol-high[1m]`
- `cm-implementer`: GPT-5.6 Sol · high · openai · `gpt-multi-sol-high[1m]`
- `cm-implementer-strong`: GPT-5.6 Sol · xhigh · openai · `gpt-multi-sol-xhigh[1m]`
- `cm-reviewer`: GPT-5.6 Sol · xhigh · openai · `gpt-multi-sol-xhigh[1m]`
- `cm-reviewer-strong`: Opus 5.5 · 1M selector · xhigh · anthropic · `claude-multi-opus-5-5[1m]` · not a step up from the lead
- not bound: `cm-designer`

## Provider exhaustion
- explorer: openai: `cm-explorer` — no other-provider grade; ask the operator for `/cm profile claude` (or another profile that avoids that provider) or `/cm set <agent>=<model>:<effort>`, then `/reload-plugins`.
- analyst: openai: `cm-analyst`; anthropic: `cm-analyst-strong` — reroute to the grade on another provider.
- implementer: openai: `cm-implementer-light`, `cm-implementer`, `cm-implementer-strong` — no other-provider grade; ask the operator for `/cm profile claude` (or another profile that avoids that provider) or `/cm set <agent>=<model>:<effort>`, then `/reload-plugins`.
- reviewer: openai: `cm-reviewer`; anthropic: `cm-reviewer-strong` — reroute to the grade on another provider.

## Review routing
| Author | Normal change | High-stakes change |
|---|---|---|
| cm-implementer-light (openai) | — (its check) | cm-reviewer-strong |
| cm-implementer (openai) | cm-reviewer-strong (only other-family) | cm-reviewer-strong |
| cm-implementer-strong (openai) | cm-reviewer-strong | cm-reviewer-strong |
| lead (anthropic) | cm-reviewer (only other-family) | cm-reviewer (only other-family) |

Review rounds: at most 2 per change set; then decide yourself and report open disagreement to the operator.

## Rules
- Trust the lineup with the highest lineup_generation; this one is generation 1.
- After a lineup change, run `/reload-plugins` before spawning: until then new spawns keep the previous bindings. Running agents keep their model.
- Never SendMessage-continue an agent spawned under an earlier lineup_generation: spawn it fresh with a self-contained brief. A round-2 review after a lineup change is a fresh spawn of the same agent type, given the round-1 findings; first re-read the routing table of the current lineup_generation: if that author's cell is now same-family or names the other reviewer, follow the current table.
- `/model`: press `s` (this session only). Enter saves the model into the user's global settings, which plain `claude` then inherits.

## Checks
- ! explorer and every implementer are on openai (codex quota shared)
