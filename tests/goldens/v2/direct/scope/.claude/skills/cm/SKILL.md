---
name: cm
description: Show or change this claude-multi session's agent lineup (/cm, /cm profiles, /cm profile NAME, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, /cm direct [MODEL[:EFFORT]], /cm pin, /cm follow, /cm fallback PROVIDER [--preview], /cm review [high-stakes] [RANGE], /cm quota)
argument-hint: "profiles · profile NAME · set AGENT=MODEL[:EFFORT] · unset AGENT · direct [MODEL[:EFFORT]] · pin · follow · fallback PROVIDER [--preview] · review [high-stakes] [RANGE] · quota"
disable-model-invocation: true
allowed-tools: Bash(claude-multi lineup:*)
---
!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`
