"""Versioned prompts.

Every prompt is a module with a VERSION, and the version is written to
`llm_calls.prompt_version` and `plans.prompt_version`. Changing a prompt
without bumping its version makes past output unexplainable: the record would
name a prompt that no longer exists.

Each module also owns its TEMPLATE renderer. That is deliberate — the fallback
has to produce the same shape as the generation, and keeping the two in one
file is the only arrangement where a change to one is obviously a change to
the other.
"""

from __future__ import annotations
