# Setup

Daily Command Center is self-hosted and single-tenant — one instance per
person, running on your own machine (or a small server), reading from
your own Google account and API keys. No shared backend, no accounts,
no login. This guide gets a fresh clone running from nothing.

There are two ways through this, and you only need one:

- **Manual** (this guide) — edit `.env` and `profile.py` by hand, run
  `make auth`, done. Full control, works offline once configured.
- **Setup wizard** (`/setup`) — a short in-browser flow that collects
  your profile and a Groq key and writes `.env`/`profile.py` for you.
  It's gated behind a one-time invite link, so it's really meant for
  handing a *second* instance to someone else (see "Sharing it with
  someone else" below) rather than your own first run — on your own
  instance there's no invite to give yourself, so this guide's manual
  steps are the actual path in. The wizard also doesn't collect Google
  credentials yet (step 4 below is still manual either way).

## 1. Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- Optional: [Ollama](https://ollama.com) if you'd rather run triage
  locally than pay for an API (see step 3)

## 2. Clone and install

```bash
git clone <your-fork-url>
cd daily-command-center
uv sync
```

## 3. Create your `.env`

```bash
cp .env.example .env
```

Then fill in:

- `APP_TIMEZONE` — an IANA timezone name (e.g. `Europe/Istanbul`,
  `America/New_York`). Defaults to `UTC` if you skip it.
- `TRIAGE_PROVIDER` — pick one:
  - `auto` — **the default.** Checks whether `llama3.2:3b` is pulled
    in a local Ollama install and uses it if so (free, private, no
    Groq tokens spent); automatically falls back to Groq the instant
    it isn't pulled, Ollama isn't running, or a request to it fails —
    no restart needed. Set `GROQ_API_KEY` below so the fallback
    actually has somewhere to go once it's needed.
    `ollama pull llama3.2:3b` to make the local leg usable, or set
    `OLLAMA_MODEL` in `.env` to check for a different tag you've
    already pulled instead.
  - `ollama` — same as auto's local leg, but with no Groq fallback at
    all. Install Ollama, `ollama pull llama3.2:3b` (or whatever you set
    `TRIAGE_MODEL` to for this mode specifically). No key needed.
  - `groq` — Groq only, no local fallback. Free tier, fast, hosted. Get
    a key at [console.groq.com](https://console.groq.com), set
    `GROQ_API_KEY`.
  - `anthropic` — set `ANTHROPIC_API_KEY` from
    [console.anthropic.com](https://console.anthropic.com).
- `MEDIUM_API_KEY` + `MEDIUM_USERNAME` — optional. Powers the "Reading"
  lane. Get a RapidAPI key for the
  [Medium2 API](https://rapidapi.com/nishujain199719-vgIfuFHZxVz/api/medium2)
  and use your own Medium username. Leave blank to skip — the Reading
  lane just stays empty (logged, not fatal).

## 4. Create a Google Cloud OAuth app

This is what lets the app read your Gmail, Calendar, and Tasks.

1. Go to [console.cloud.google.com](https://console.cloud.google.com),
   create a new project.
2. Under **APIs & Services → Library**, enable:
   - Gmail API
   - Google Calendar API
   - Google Tasks API
3. Under **APIs & Services → OAuth consent screen**, configure it (you
   as the test user is fine). **Important**: if you leave the app in
   **Testing** status, Google expires your refresh token every 7 days
   and you'll have to redo step 6 constantly — switch it to
   **Production** to avoid that (it doesn't need Google's review process
   for personal/internal use with a small user list).
4. Under **APIs & Services → Credentials**, create an **OAuth client
   ID**, application type **Desktop app**.
5. Download the resulting JSON and save it as `credentials.json` in the
   repo root (already gitignored — never commit this file).

Skipping this step doesn't block the rest of setup — the brief just
shows sample data (clearly labeled) until you come back and connect it.

## 5. Set up your profile

```bash
cp src/command_center/profile_example.py src/command_center/profile.py
```

Edit `src/command_center/profile.py` with your own name, bio, project
list, and (optionally) the brief lane labels and Medium ranking context.
This file is gitignored — it's your data, not the repo's. If you skip
this step the app still runs, just with placeholder content, and the
app will keep redirecting you to `/setup` until it's filled in (see
"Why does it keep sending me to /setup?" below).

## 6. Authenticate with Google

```bash
make auth
```

Opens a browser for the Google consent flow, then writes
`secrets/google_token.json` (also gitignored). Re-run this any time you
change `GOOGLE_SCOPES` in `config.py` or the token stops refreshing.
Skip this if you skipped step 4 — the app runs fine without it.

## 7. Run it

```bash
make run
```

Visit `http://localhost:8000` — the portfolio/landing page. Visit
`/brief` for the daily dashboard, `/settings` to configure per-source
pull intervals. Without a connected Google account, the brief shows
clearly-labeled sample data; once authenticated, it pulls your real
Gmail/Calendar/Tasks.

## Why does it keep sending me to `/setup`?

The app won't serve any page except `/setup` until it considers itself
minimally configured: a profile that's been edited away from the
placeholder, **and** a working triage provider (an Ollama provider
selection, or a real Groq/Anthropic key — Google is *not* required for
this check, since the brief degrades to sample data without it). If
you're stuck on `/setup`, it means steps 3 and 5 above aren't both
done yet. This check never touches the wizard or invites — it's purely
"is `.env`/`profile.py` filled in," so the manual path through this
guide satisfies it on its own.

## What you need to create, at a glance

| What | Where | Required? |
|---|---|---|
| Groq **or** Anthropic **or** Ollama | [console.groq.com](https://console.groq.com) / [console.anthropic.com](https://console.anthropic.com) / local install | Yes (pick one) |
| A profile (`profile.py`) | copied and edited from `profile_example.py` | Yes |
| Google Cloud project + OAuth client | [console.cloud.google.com](https://console.cloud.google.com) | No — brief shows sample data without it |
| `credentials.json` | downloaded from the OAuth client above | No — same as above |
| RapidAPI key for Medium2 + your Medium username | [rapidapi.com](https://rapidapi.com) | No — Reading lane only |

## Sharing it with someone else

This app has no login system — reachability is the only gate, and
each instance is meant for one person. To give a friend their own
instance (their own data, their own Google login, never touching
yours):

1. On **your own already-running instance**, go to **Settings →
   Invites** and generate a one-time link (it expires on its own
   schedule and can only be used once).
2. Send them that link plus the repo URL, however you'd normally reach
   them — there's no built-in email delivery.
3. They clone the repo fresh, run `uv sync`, and open your invite
   link at their own instance's `/setup` — that walks them through
   profile + Groq key without needing this whole guide.
4. They still do steps 4 and 6 above (their own Google Cloud OAuth
   app, their own `make auth`) independently, whenever they're ready —
   the wizard doesn't collect Google credentials yet.

## Notes

- `.env`, `secrets/`, `credentials.json`, `command_center.db`, and
  `profile.py` are all gitignored — nothing personal or secret gets
  committed by default.
- This repo also ships `CLAUDE.md` and `prompt.md` at the root, which
  contain the original author's personal notes/context. They're not used
  by the app itself — worth a look before you publish your own fork
  publicly.
