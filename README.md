# KF Japanese Lab Bots

Self-hosted Discord bots powering the [KaleidoFuture Japanese Lab](https://discord.gg/gaujRWbmg) — a building-in-public community for Japanese learners worldwide.

KF Lab に所属するメンバー向けに自社運用している Discord bot 群。Building in Public 哲学のもとオープンソース公開。

## Bots

| Bot | Status | Role |
|---|---|---|
| **kf_tenshi** | ✅ live since 2026-05-01 | Watches Kotoba bot JLPT quiz results and grants matching `N* V` Verified roles to members who clear the pass threshold (default: 20 / 25 correct). |
| **kf_role_logger** | 🛠 planned | Records role transitions to SQLite for Phase 4 analytics; auto-promotes Newcomer → Member after 7 days. |

Both bots are designed to share a single process and venv on the host machine.

## Stack

- Python 3.12+
- discord.py 2.x
- python-dotenv
- SQLite (kf_role_logger only, planned)

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
