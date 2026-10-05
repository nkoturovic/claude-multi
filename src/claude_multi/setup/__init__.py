"""The setup layer: prompt-free plans and applies behind every setup surface.

Each operation is a pair: ``plan_*`` reads the current state and returns a
plan (the exact lines a person is shown, the consent it needs and a digest of
every fact it relied on); ``apply_*`` takes the plan and the person's
confirmation, re-checks those facts under the owning locks and writes. The
command line, the answers file and the full-screen views all go through the
same pairs. Nothing in this package prints, prompts or draws: callers pass
confirmations and secret values in and show the returned lines.

The package exports nothing itself; import the module that owns a name.
"""
