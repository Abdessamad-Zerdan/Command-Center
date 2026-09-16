# Daily Command Center

A self-hosted personal dashboard that pulls your Gmail, Calendar, and
Google Tasks into one triaged daily brief, then gives you a handful of
small tools around it — project tracking, a lightweight assistant you
can talk to, a monthly finances tracker, and a history/report view over
how your days actually went.

Single-tenant by design: one instance, one person, your own data, your
own API keys. No accounts, no shared backend, nothing phones home.
FastAPI + SQLite + server-rendered Jinja templates, Tailwind and Alpine
via CDN — no frontend build step.

> **This repo is public to showcase the project — not for reuse.**
> All rights reserved; see [LICENSE](LICENSE). Cloning it for a look
> is fine, running your own copy isn't, without asking first.

## What's in it

- **Brief** (`/brief`) — today's Gmail, Calendar, and Tasks items,
  triaged by an LLM into Urgent / Action items / Meeting prep / Tasks
  due / Reading lanes, plus a timeline strip of today's calendar. Add
  manual tasks, snooze or complete anything, drag items around.
- **Projects** (`/projects`) — register local codebases you're working
  on; each one gets a page showing its current sprint/goal/blockers, a
  "last worked on" signal pulled from git log or file mtimes, linked
  tasks from the brief, and a collapsed file-tree browser.
- **Schedule** (`/schedule`) — free-time finder over your recurring
  commitments (work hours, gym, standing syncs) plus today's calendar,
  with AI-suggested placements for tasks that need a slot.
- **History & Report** (`/history`, `/history/report`) — every past
  day's brief, plus completion-rate charts, streaks, and an AI
  reflection comparing this period to the last one.
- **Finances** (`/finances`, reachable via the More menu) — manual
  spend/income/savings entries, a category breakdown chart, a
  month-over-month trend line, and the same kind of AI reflection as
  the history report.
- **Assistant** — a floating chat widget on the brief that answers
  questions grounded in your own data and can create/update/complete
  Google Tasks on your behalf via tool-calling.
- **Settings** (`/settings`) — per-source pull intervals, recurring
  schedule commitments, and (if you're sharing this instance with
  someone) admin-generated setup invites.

## Setup

See **[SETUP.md](SETUP.md)** for the full install walkthrough — cloning,
API keys, Google OAuth, and running it for the first time.

Short version: `uv sync`, fill in `.env` from `.env.example` (Ollama
works out of the box with no key), copy `profile.py` from
`profile_example.py` and edit it, `make run`. Google (Gmail/Calendar/
Tasks) is optional and separate — see SETUP.md step 4 — the brief just
shows sample data until you connect it.

## Sharing it with someone else

This app has no login system — reachability is the only gate. If you
want to run a second, separate instance for a friend (their own data,
their own Google login, never touching yours), generate them a
one-time setup link from **Settings → Invites** on a fresh, empty
instance. See the "Invites" card in Settings for details; each invite
is single-use and expires on its own schedule.

## License

All rights reserved — see [LICENSE](LICENSE). This repo is public so
you can see how it's built, not to hand out a free copy of the app.
Want to use something from it (a pattern, a snippet, the whole thing)?
Ask first: zerdanabdessamad@gmail.com.

## Notes

- `.env`, `secrets/`, `credentials.json`, `command_center.db`,
  `profile.py`, `CLAUDE.md`, and `prompt.md` are all gitignored —
  nothing personal or secret gets committed by default. `CLAUDE.md`/
  `prompt.md` were the original author's working notes for
  pair-programming with an AI assistant, not used by the app itself —
  if you keep using an AI assistant on your own fork, you may want
  equivalent notes of your own; they just won't be shared here.
- No outbound email is ever sent by this app. Invite links are meant to
  be delivered manually, however you'd normally reach that person.
