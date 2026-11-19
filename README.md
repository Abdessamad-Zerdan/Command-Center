# Daily Command Center

A self-hosted personal dashboard that pulls your Gmail, Calendar, and
Google Tasks into one triaged daily brief, then gives you a handful of
small tools around it — project tracking, a lightweight assistant you
can talk to, a monthly finances tracker, and a history/report view over
how your days actually went.

Single-tenant by design: one instance, one person, your own data, your
own API keys. No accounts, no shared backend, no analytics — but some of
your data *does* leave the machine when you use a hosted AI provider;
see [What leaves your machine](#what-leaves-your-machine).
FastAPI + SQLite + server-rendered Jinja templates, prebuilt Tailwind and vendored
Alpine — nothing loads from a CDN.

> **This repo is public to showcase the project — not for reuse.**
> All rights reserved; see [LICENSE](LICENSE). Cloning it for a look
> is fine, running your own copy isn't, without asking first — and
> the app itself won't start without a license key it doesn't ship
> with (see `license_gate.py`), so this isn't just a legal ask.

## About this public copy

This repository is a **showcase** of how the app is built — its
structure, UI, data pipeline, security hardening, tests and CI. It is
not a working copy of it: the **AI core is withheld**. In the files
below, each withheld function keeps its name, signature and docstring
so you can see where it fits, but its body raises a "withheld" error
(the same error the app treats as "AI provider unavailable", so every
page still loads and shows its honest degraded state), and the prompts
and tool schemas are blank:

- `triage.py` — the LLM triage of mail, calendar and tasks into lanes
  (the provider plumbing — Groq/Ollama/Anthropic fallback, rate-limit
  handling — is public)
- `prioritize.py` — the High/Medium/Low sort of the day's open work
- `reading.py` — the picker that keeps a few articles worth reading
- `history/reflection.py`, `finances/reflection.py` — the AI-written
  reflections
- `assistant/` — the chatbot's reasoning, tool use and retrieval (the
  tool-argument validation in `tools.validate_args` is public)
- the Map's hand-drawn connectors

Everything else is real and tested. The full version runs privately.

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
`profile_example.py` and edit it, copy `prompts.py` from
`prompts_example.py` and write your own triage/assistant instructions,
`make run`. Google (Gmail/Calendar/Tasks) is optional and separate —
see SETUP.md step 4 — the brief just shows sample data until you
connect it.

Changing templates? The CSS is prebuilt with Tailwind (no Node needed):
run `uv run make-css` after editing classes in a template and commit
`static/css/app.css`. CI fails if it's out of date, and the app warns at
startup when a template is newer than the CSS.

## What leaves your machine

Your data lives in `command_center.db` on your own computer. These are
the only things that leave it, and only when the feature is in use:

| Where it goes | What is sent | When |
|---|---|---|
| **Google** (Gmail, Calendar, Tasks APIs) | Read requests for your mail, events and tasks; tasks/events the assistant creates or changes | Every pull, and when you confirm an assistant action |
| **Groq** or **Anthropic** — whichever `TRIAGE_PROVIDER` selects | For every item that reaches the AI step: the email's subject, sender and cleaned body text, its Gmail labels and arrival time; a calendar event's title, start/end times, attendee addresses, links and the first 400 characters of its description; a Google Task's title, notes and due date | Every pull. Mail your triage rules hide (promotions, social, job alerts, surveys by default) is decided locally and **not** sent; mail they send to Reading is not triaged either, but see the Reading picker below |
| **Groq** (always, whatever the triage provider) | **Assistant:** your question and the conversation so far, today's brief items, your task titles, recent items, your projects/competitions/research/targets with their notes, and the best-matching passages of `data/vision.md`, your profile and any uploaded documents. **Reports:** for the History report, task-completion counts per lane plus the titles of tasks finished or left open in the period and Pomodoro focus time per task name (never notes, mail or attachments) and monthly spending totals per category (Finances). **Medium:** article titles and subtitles plus your interests. **Reading picker:** the subject and sender of each newsletter headed for Reading (never its body), plus your interests, so it can keep the few worth reading. **Priorities:** the title, section, due date/deadline and first 200 characters of the summary of each open task on today's brief, so it can sort them into High/Medium/Low | When you ask the assistant something, press a report's "Write my reflection" / Regenerate (opening the page sends nothing), the Medium source pulls, a pull has newsletters for Reading, or a pull changed your open tasks |
| **Ollama** (`TRIAGE_PROVIDER=ollama`, or `auto` while the local model is available) | Same as the Groq/Anthropic triage row — but to a model running **on your own machine**; nothing leaves it | Every pull |
| **RapidAPI / Medium** | Your Medium username | When the Medium source pulls (only if `MEDIUM_API_KEY` is set) |
| **ntfy** (`NTFY_SERVER`, default the public ntfy.sh) | Nudge texts, e.g. "3 urgent items untouched since yesterday" — readable by anyone who knows your topic name | Only if `NTFY_TOPIC` is set |
| **Hugging Face** | A one-time download of the local embedding model; your documents are embedded locally | First assistant use |

To keep triage fully local, use `TRIAGE_PROVIDER=ollama`. The assistant,
the report reflections, the Medium ranking, the Reading picker and the priority sort currently always use Groq,
so leave `GROQ_API_KEY` unset if nothing should go to a hosted model —
those features then simply won't work.

## Sharing it with someone else

This repo requires a license key to run at all (see License below) —
if you want to give a friend a real, working instance, that's what a
separate, unrestricted private copy is for: add them as a collaborator
there instead of pointing them at this repo. The in-app invite system
(**Settings → Invites**) still exists for bootstrapping a second
instance's setup wizard (profile + Groq key), but it doesn't bypass
the license key — an instance started from an invite link still won't
run without one.

## License

All rights reserved — see [LICENSE](LICENSE). This repo is public so
you can see how it's built, not to hand out a free copy of the app.
Want to use something from it (a pattern, a snippet, the whole thing)?
Ask first: zerdanabdessamad@gmail.com.

Enforced in code, not just on paper: the app requires a license key
(`license.key` at the repo root, or `COMMAND_CENTER_LICENSE_KEY`) to
actually start — see `license_gate.py`. If you've been given
permission to run an instance, you'll be given a key too.

## Notes

- `.env`, `secrets/`, `credentials.json`, `command_center.db`,
  `profile.py`, `prompts.py`, `license.key`, `CLAUDE.md`, and
  `prompt.md` are all gitignored — nothing personal or secret gets
  committed by default.
  `prompts.py` holds the actual tuned triage/assistant prompts;
  `prompts_example.py` (committed) is a bare-bones placeholder you're
  meant to replace. `CLAUDE.md`/
  `prompt.md` were the original author's working notes for
  pair-programming with an AI assistant, not used by the app itself —
  if you keep using an AI assistant on your own fork, you may want
  equivalent notes of your own; they just won't be shared here.
- No outbound email is ever sent by this app. Invite links are meant to
  be delivered manually, however you'd normally reach that person.
