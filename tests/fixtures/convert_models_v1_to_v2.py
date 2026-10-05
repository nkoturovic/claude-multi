"""Catalog v1 -> v2 model-entry converter.

Test helper, not product code: the fixture builder uses it to convert the
frozen catalog-32 lines, and the ``claude-multi-dev drafts migrate``
classification mirrors it. A lane's id
must equal its agent effort; a client-effort (Anthropic OAuth pool or
OpenAI-compatible) entry must have exactly one lane whose selector is the
entry's ``client_selector``.
"""

from __future__ import annotations

import copy
from typing import Any

CLIENT = frozenset({"cliproxy-oauth-claude-v1", "cliproxy-openai-compat-v1"})
NON_LEAD_ROLES = ("cm-analyst", "cm-reviewer", "cm-implementer")  # roles.json order


def v1_to_v2_entry(
    key: str, e: dict[str, Any], provider: dict[str, Any], generation: str
) -> dict[str, Any]:
    lanes = e["lanes"]
    assert all(lane["agent_effort"] == lane_id for lane_id, lane in lanes.items()), key
    if provider["adapter"] in CLIENT:
        assert len(lanes) == 1 and e["client_selector"] == lanes[e["default_lane"]]["client_selector"], key
        shape: dict[str, Any] = {"selector": e["client_selector"], "efforts": [e["default_lane"]]}
    else:
        shape = {
            "efforts": {
                level: {"selector": lane["client_selector"], "proxy_contract": lane["proxy_effort_contract"]}
                for level, lane in lanes.items()
            }
        }
    roles = [role for role in e["compatible_roles"] if role != "cm-lead"]
    out = {
        field: copy.deepcopy(e[field])
        for field in (
            "provider",
            "display",
            "wire_model",
            "capabilities",
            "context",
            "lead",
            "routing_note",
            "minimum_tested",
        )
    }
    out.update(
        shape,
        generation=generation,
        default_effort=e["default_lane"],
        roles="all" if set(roles) == set(NON_LEAD_ROLES) else [r for r in NON_LEAD_ROLES if r in roles],
        status="active",
        registry_overlay=None,
    )
    return out
