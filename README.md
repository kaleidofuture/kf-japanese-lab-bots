# KF Japanese Lab Bots

Self-hosted Discord bots powering the [KaleidoFuture Japanese Lab](https://discord.gg/gaujRWbmg) — a building-in-public community for Japanese learners worldwide.

KF Lab に所属するメンバー向けに自社運用している Discord bot 群。Building in Public 哲学のもとオープンソース公開。

## Bots

| Bot | Status | Role |
|---|---|---|
| **kf_tenshi** | ✅ live since 2026-05-01 | Watches Kotoba bot JLPT quiz results and grants matching `N* V` Verified roles to members who clear the pass threshold (default: 20 / 25 correct). |
| **kf_role_logger** | ✅ live since 2026-05-01 | Records every role transition to SQLite for Phase 4 motivation-evolution analytics; auto-promotes Newcomer → Member after `PROMOTION_GRACE_DAYS` (default 7). |

Each bot runs as an independent process / scheduled task with its own venv and Discord application token, so a push to one bot's folder does not restart the other. They share one repository and one auto-deploy pipeline.

## Stack

- Python 3.12+
- discord.py 2.x
- python-dotenv
- SQLite (kf_role_logger event store, ~5 KB/month at α-phase scale)

## Quick start (kf_tenshi)

```bash
cd kf_tenshi
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# edit .env with DISCORD_TOKEN and role IDs from your guild
.venv\Scripts\python main.py
```

For persistent operation on Windows, register `run.bat` with Task Scheduler (`schtasks /Create /TN KFTenshi /TR <path>\run.bat /SC ONLOGON /RL HIGHEST`).

## Quick start (kf_role_logger)

```bash
cd kf_role_logger
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# edit .env with the KF RoleLogger application token and the role IDs below
.venv\Scripts\python main.py
```

Required intents on the Discord application: **Server Members Intent** ✅, Message Content Intent ❌, Presence Intent ❌. The bot's role must be placed above `Newcomer` and `Member` in the guild role hierarchy.

For testing time-based promotion without waiting 7 days, override the grace period via shell env (the value in `.env` is left at the production default of `7`):

```powershell
$env:PROMOTION_GRACE_DAYS="0.001"; .venv\Scripts\python main.py
```

The SQLite event store is created automatically at `kf_role_logger/data/role_events.db`. Every role grant/revoke is recorded with a `source` tag (`backfill`, `on_member_update`, `on_member_remove`, `auto_promote`) so analytics can isolate the synthetic pre-bot region from real events.

For persistent operation: `schtasks /Create /TN KFRoleLogger /TR <path>\run.bat /SC ONLOGON /RL HIGHEST`.

## Auto-deploy (host machine, optional)

The `services/` folder contains a 1-minute polling auto-deploy:

- `services/run_hidden_pull.vbs` — wscript launcher (no console flicker)
- `services/auto_git_pull.ps1` — `git pull --ff-only`, restart bots only when their files actually changed, warn on `requirements.txt` / `services/*` (manual action)
- `services/install_auto_deploy.ps1` — registers the `KFLabBotsAutoPull` Task Scheduler task (run once per host)

Install:

```powershell
cd <repo>\services
powershell -ExecutionPolicy Bypass -File install_auto_deploy.ps1
```

After install, every `git push` to `origin/main` reaches the host within a minute. Logs go to `logs/auto_git_pull.log` (silent on no-op pulls).

## Architecture notes

- **Wrapper log vs app log are separated** — `run.bat` redirects stdout/stderr to `wrapper.log`; the app itself writes structured logs to `kf_tenshi.log` via Python `logging.FileHandler`. They cannot share the same file due to Windows file locking.
- **Bot role hierarchy matters** — the bot's role must be placed *above* the roles it grants in the Discord guild settings, otherwise role assignment fails with `Forbidden`.
- **Privileged Gateway Intents required** — `Server Members Intent` and `Message Content Intent` must be enabled in the Discord Developer Portal for each bot application.

## Why not [Tenshi-Bot OSS](https://github.com/Miraii133/Tenshi-Bot)?

The original Tenshi-Bot is a Replit-hosted personal project for the EJLX (日本語勉強部) server, with hard-coded server / channel / role IDs spread across multiple JS files and no setup documentation. Adapting it would have meant a deeper rewrite than implementing the same logic from scratch — and would not have shared a process with the upcoming KF RoleLogger. We re-implemented the quiz-pass detection in Python (~150 LOC) and kept compatibility with Kotoba bot's quiz-end embed format (`Multiple Deck Quiz Ended` / `JLPT N* * Quiz Ended`).

## License

MIT — see [LICENSE](LICENSE).

## Operated by

Marine（まりにぃ）— [KaleidoFuture Japanese Lab](https://kaleidofuture.com)
