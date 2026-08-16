"""Fast, no-network check for whether the app is ready to leave /setup.
Deliberately structural (files/env present and non-placeholder) rather
than a live re-test on every request — the live tests only run once, in
the wizard's own step 10.

Google credentials are deliberately NOT required here, even though the
app is much more useful once connected. Two reasons: (1) the built
wizard steps (STEP_ORDER in state.py) don't collect Google credentials
at all yet — that's the still-unbuilt "Part 2" — so requiring it here
would make it impossible for the wizard to ever satisfy its own gate,
permanently bouncing a freshly-finished user back to /setup; combined
with invite-gating, that's a hard lockout, not just an inconvenience.
(2) an instance with no Google connection isn't broken — it degrades
to fixture data exactly the way a disabled Medium source degrades that
one lane (see pipeline.py, fixtures.py) — same "usable, just less
complete" posture, not a blocking precondition. Connect Google later
via Settings/SETUP.md whenever you're ready; this check doesn't gate
on it either way.
"""

from command_center import config, profile_example


def is_setup_complete() -> bool:
    # Ollama needs no key — TRIAGE_PROVIDER=="ollama" (the documented
    # default in .env.example) is itself the "chose this provider"
    # signal, same as a non-empty key is for Groq/Anthropic. Without this
    # branch, an instance following SETUP.md's own Ollama instructions
    # (the free/local option, listed there alongside Groq and Anthropic)
    # would never satisfy this check and get permanently redirected to
    # /setup, which then 403s it for lacking an invite — a real lockout,
    # not just Google's (see the docstring above).
    triage_ready = (
        bool(config.GROQ_API_KEY)
        or bool(config.ANTHROPIC_API_KEY)
        or config.TRIAGE_PROVIDER == "ollama"
    )

    # config.PROFILE falls back to profile_example.PROFILE when
    # profile.py doesn't exist (see config.py) — comparing against that
    # same placeholder catches both "no profile.py" and "profile.py
    # exists but was never actually edited."
    profile_filled = config.PROFILE["name"] != profile_example.PROFILE["name"]

    return triage_ready and profile_filled
