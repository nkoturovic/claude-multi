# claude-multi lineup (lineup_generation 1)

Profile: direct · lead class large (1M) · providers: anthropic 0 (+lead)

## Lead
- lead: Opus 5.5 · 1M selector · ultracode · anthropic · `claude-multi-opus-5-5[1m]`
- /model offers only the lead set (4 entries); any other model needs a relaunch with another profile.
- /model's built-in Default row is `claude-opus-4-8[1m]`, outside this lead set; it is refused.

## Agents
- none: this session has no cm-* agents; do the work yourself or change the lineup with /cm.

## Review routing
| Author | Normal change | High-stakes change |
|---|---|---|
| lead (anthropic) | — (no reviewer bound) | — (no reviewer bound) |

Review rounds: at most 2 per change set; then decide yourself and report open disagreement to the operator.

## Rules
- Trust the lineup with the highest lineup_generation; this one is generation 1.
- After a lineup change, run `/reload-plugins` before spawning: until then new spawns keep the previous bindings. Running agents keep their model.
- Never SendMessage-continue an agent spawned under an earlier lineup_generation: spawn it fresh with a self-contained brief. A round-2 review after a lineup change is a fresh spawn of the same agent type, given the round-1 findings; first re-read the routing table of the current lineup_generation: if that author's cell is now same-family or names the other reviewer, follow the current table.
- `/model`: press `s` (this session only). Enter saves the model into the user's global settings, which plain `claude` then inherits.
