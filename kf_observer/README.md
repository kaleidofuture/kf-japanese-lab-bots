# KF Observer

Read-only Discord state inspector for the KF Japanese Lab guild.

Unlike the resident bots in this repo (`kf_tenshi`, `kf_role_logger`), this is a
**short-lived CLI**: connect → query → output → disconnect. It's designed to be
called on demand from SSH sessions or weekly KPI scripts, never registered as a
Windows scheduled task.

## Bot identity

- App name: `KF Observer`
- Discord application (separate from KF Tenshi / KF RoleLogger)
- Privileged Intents: **Server Members Intent ON**, others OFF
- OAuth2 invite scopes: `bot` only
- OAuth2 invite permissions: `View Channels` + `Read Message History`
- Zero write permissions (cannot post / kick / ban / change roles)

## Setup (per machine)

```bash
cd kf_observer
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
cp .env.example .env
# edit .env: set DISCORD_TOKEN to the bot token from Developer Portal
```

## Usage

```bash
# Full state snapshot → stdout + data/snapshots/<timestamp>.json
python observe_lab.py snapshot

# Human-only members with their current roles
python observe_lab.py members
python observe_lab.py members --markdown

# Member count per role (sorted desc)
python observe_lab.py role-distribution
python observe_lab.py role-distribution --markdown

# Per-channel activity in the last N hours (default 24)
python observe_lab.py recent-activity --hours 48

# Reaction tallies on every message in a channel over the last N days
python observe_lab.py reactions --channel daily-japanese --days 7

# Threads in #pain-points-board with 🙋 counts
python observe_lab.py pain-points
```

Default output is JSON (machine-readable, KPI-pipeline-friendly). Pass
`--markdown` for human-readable table output where supported.

## Snapshot files

`snapshot` always writes the result to two locations under `data/snapshots/`:
- `YYYY-MM-DD_HHMM.json` — timestamped, append-only history
- `latest.json` — overwritten each run, easy pickup target for KPI scripts

Both are git-ignored (`kf_observer/data/`).

## Why a separate bot, not just KF RoleLogger extension?

Single-responsibility. RoleLogger has write permissions (Manage Roles for
auto-promotion). Observer is **read-only by application identity**, so even a
token leak cannot result in unintended posts or role changes. The two bot
applications are separated at the Discord level so their permission scopes
cannot drift together.
