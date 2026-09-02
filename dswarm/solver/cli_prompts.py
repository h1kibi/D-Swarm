"""Prompt templates for CliSolver response modes.

This module contains prompt constants for CliSolver's response and
exploration conclusion modes. These are the simplest, standalone prompts
with no formatting dependencies.

Gradually migrating from cli_solver.py to improve maintainability.
Phase 1: Response prompts (2026-09-02).
"""

RESPOND_ASK_PROMPT = (
    "The operator has a follow-up about the challenge you just worked. Answer it "
    "directly and concretely, drawing on what you already confirmed this session. "
    "If answering needs a quick check, you may run a command — but do not start a "
    "long new investigation; this is a conversation, not a fresh solve.\n\n"
    "Operator: {text}"
)

RESPOND_MARK_FALSE_PROMPT = (
    "IMPORTANT: the flag you reported — {flag} — is a FALSE POSITIVE (the operator "
    "verified it does not work). Treat it as a dead-end: do NOT report it again. "
    "Resume solving from the facts you already confirmed and find the REAL flag.\n"
    "{note}\n"
    "Actually RUN commands against the real target/files. When you recover the TRUE "
    "flag from REAL output, print it on its own line exactly as:\n  FOUND_FLAG=<flag>\n"
    "It must appear verbatim in your shell output. Also print VERIFIED_FACT=<...> / "
    "DEADEND=<...> lines as you go so the team's board stays current."
)

RESPOND_WRITEUP_PROMPT = (
    "Write a concise CTF WRITEUP for the challenge you just solved, in Chinese. "
    "Base it ONLY on what you actually confirmed this session — do not invent steps. "
    "Structure it as:\n"
    "  ## 漏洞点  (the root cause / vulnerability)\n"
    "  ## 利用步骤  (numbered, reproducible — the real commands/requests you used)\n"
    "  ## Flag  (the flag and where it came from)\n"
    "Keep it tight and technical. Output ONLY the markdown writeup, nothing else."
)

EXPLORE_CONCLUDE_PROMPT = (
    "CONCLUDE: stop exploring NOW. Do not run any more commands.\n"
    "Summarize ONLY what you have already confirmed in REAL output, using these "
    "markers on their own lines:\n"
    "  VERIFIED_FACT=<a confirmed finding from real output>\n"
    "  DEADEND=<why this direction failed>\n"
    "  POC_SAVE=<path>|<entry_command>|<status>|<note>\n"
    "  CLEANUP=<remove_artifact|stop_listener|close_session|revoke_credential>:<target>\n"
    "  FOUND_FLAG=<the flag>  (only if seen in real output this session)\n"
    "If you found nothing, output DEADEND=<reason>. Do not guess."
)
