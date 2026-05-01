"""KF RoleLogger — role transition logger & Newcomer→Member auto-promoter.

Records every role change in the KF Japanese Lab guild to a local SQLite DB
(Phase 4 motivation-evolution analysis foundation), and auto-promotes members
who have held the Newcomer role for `PROMOTION_GRACE_DAYS` days (default 7).

Runs as an independent process from KF Tenshi — same monorepo, separate venv,
separate Discord bot application, separate Windows scheduled task. This keeps
auto-deploy blast radius local: KF Tenshi pushes don't restart RoleLogger.
"""

import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ROLE_NEWCOMER = int(os.environ["ROLE_NEWCOMER"])
ROLE_MEMBER = int(os.environ["ROLE_MEMBER"])
PROMOTION_GRACE_DAYS = float(os.environ.get("PROMOTION_GRACE_DAYS", "7"))

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "role_events.db"

LOG_FILE = ROOT / "kf_role_logger.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("kf_role_logger")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!kfrl ", intents=intents)
conn = db.connect(DB_PATH)
db.init_schema(conn)


def _is_target_guild(guild: discord.Guild | None) -> bool:
    """True only for the configured KF Lab guild — guards against stray invites."""
    return guild is not None and guild.id == GUILD_ID


def _record_role_diff(
    user_id: int,
    before_roles: set[discord.Role],
    after_roles: set[discord.Role],
    source: str,
) -> tuple[int, int]:
    """Diff two role sets and persist `added` / `removed` events. Returns (n_added, n_removed)."""
    added = after_roles - before_roles
    removed = before_roles - after_roles
    ts = db.now_utc()
    n_a = n_r = 0
    for r in added:
        if r.is_default():
            continue
        db.insert_role_event(conn, user_id, "added", r.id, r.name, timestamp=ts, source=source)
        n_a += 1
    for r in removed:
        if r.is_default():
            continue
        db.insert_role_event(conn, user_id, "removed", r.id, r.name, timestamp=ts, source=source)
        n_r += 1
    return n_a, n_r


def _backfill_existing_members(guild: discord.Guild) -> None:
    """Seed members/sessions/role_events from the live guild snapshot.

    Runs at most once per DB lifetime (gated by `bot_state.backfill_done_at`).
    Each existing member's `joined_at` is replayed as a synthetic timestamp
    and tagged `source='backfill'` so Phase 4 analysis can isolate the
    pre-bot region from real events.
    """
    if db.backfill_already_done(conn):
        log.info("Backfill: skipped (already done at %s)", db.get_state(conn, "backfill_done_at"))
        return

    n_members = n_sessions = n_events = 0
    now_iso = db.now_utc()
    for member in guild.members:
        if member.bot:
            continue
        joined_iso = db.to_iso(member.joined_at) or now_iso

        if not db.member_exists(conn, member.id):
            db.insert_member(conn, member.id, first_seen_at=joined_iso, last_seen_at=now_iso)
            n_members += 1

        if db.get_active_session_id(conn, member.id) is None:
            db.insert_session(conn, member.id, joined_at=joined_iso)
            n_sessions += 1

        for role in member.roles:
            if role.is_default():
                continue
            db.insert_role_event(
                conn, member.id, "added", role.id, role.name,
                timestamp=joined_iso, source="backfill",
            )
            n_events += 1

    db.mark_backfill_done(conn)
    log.info(
        "Backfill: %d new members, %d new sessions, %d role events (source='backfill')",
        n_members, n_sessions, n_events,
    )


@tasks.loop(hours=1)
async def auto_promote_loop() -> None:
    """Promote Newcomers whose grant is older than PROMOTION_GRACE_DAYS."""
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        log.warning("auto_promote_loop: guild=%s not available, skipping tick.", GUILD_ID)
        return

    newcomer_role = guild.get_role(ROLE_NEWCOMER)
    member_role = guild.get_role(ROLE_MEMBER)
    if newcomer_role is None or member_role is None:
        log.error(
            "auto_promote_loop: role missing (newcomer=%s, member=%s) — check IDs.",
            newcomer_role, member_role,
        )
        return

    candidates = db.find_promotion_candidates(conn, ROLE_NEWCOMER, PROMOTION_GRACE_DAYS)
    if not candidates:
        return
    log.info("auto_promote_loop: %d candidate(s)", len(candidates))

    for user_id, session_id, granted_at in candidates:
        member = guild.get_member(user_id)
        if member is None:
            log.info("Promotion skip: user_id=%s left the guild.", user_id)
            continue
        if newcomer_role not in member.roles:
            log.info("Promotion skip: %s no longer has Newcomer (manual removal).", member)
            continue
        if member_role in member.roles:
            # Already a Member (manually granted) — record promotion to keep idempotency,
            # but do not perform Discord-side mutation.
            db.record_promotion(conn, user_id, session_id, granted_at, db.now_utc())
            log.info("Promotion noted (already Member): %s session=%s", member, session_id)
            continue

        try:
            await member.add_roles(member_role, reason="Auto-promote: 7 days since Newcomer")
            await member.remove_roles(newcomer_role, reason="Auto-promote: 7 days since Newcomer")
        except discord.Forbidden:
            log.error("Forbidden: cannot promote %s — check role hierarchy.", member)
            continue
        except discord.HTTPException as e:
            log.error("HTTPException promoting %s: %s", member, e)
            continue

        promoted_at = db.now_utc()
        db.record_promotion(conn, user_id, session_id, granted_at, promoted_at)
        db.insert_role_event(
            conn, user_id, "added", member_role.id, member_role.name,
            timestamp=promoted_at, source="auto_promote",
        )
        db.insert_role_event(
            conn, user_id, "removed", newcomer_role.id, newcomer_role.name,
            timestamp=promoted_at, source="auto_promote",
        )
        log.info(
            "Auto-promoted %s (session=%s, Newcomer granted %s)",
            member, session_id, granted_at,
        )


@bot.event
async def on_ready() -> None:
    log.info(
        "KF RoleLogger online as %s (id=%s)",
        bot.user,
        bot.user.id if bot.user else "?",
    )
    log.info(
        "Watching guild=%s, newcomer=%s, member=%s, grace_days=%s, db=%s",
        GUILD_ID,
        ROLE_NEWCOMER,
        ROLE_MEMBER,
        PROMOTION_GRACE_DAYS,
        DB_PATH,
    )

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        log.error("Configured guild_id=%s not found among bot.guilds — skipping backfill.", GUILD_ID)
        return
    _backfill_existing_members(guild)

    if not auto_promote_loop.is_running():
        auto_promote_loop.start()
        log.info("auto_promote_loop started (interval=1h, grace_days=%s)", PROMOTION_GRACE_DAYS)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if not _is_target_guild(member.guild):
        return
    if member.bot:
        return

    joined_iso = db.to_iso(member.joined_at) or db.now_utc()
    now_iso = db.now_utc()

    if db.member_exists(conn, member.id):
        db.increment_session_count(conn, member.id)
        log.info("Re-join: %s (id=%s) — incremented total_sessions", member, member.id)
    else:
        db.insert_member(conn, member.id, first_seen_at=joined_iso, last_seen_at=now_iso)
        log.info("New member: %s (id=%s) — first_seen_at=%s", member, member.id, joined_iso)

    sid = db.insert_session(conn, member.id, joined_at=joined_iso)
    db.update_last_seen(conn, member.id, now_iso)
    log.info("Opened session_id=%s for %s", sid, member)
    # Role grants (e.g. Carl-bot Newcomer auto-assign) follow within seconds via on_member_update.


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if not _is_target_guild(after.guild):
        return
    if after.bot:
        return

    before_set = set(before.roles)
    after_set = set(after.roles)
    if before_set == after_set:
        return  # non-role update (nick, avatar, timeout, etc.)

    n_a, n_r = _record_role_diff(after.id, before_set, after_set, source="on_member_update")
    if n_a or n_r:
        db.update_last_seen(conn, after.id, db.now_utc())
        log.info("Role diff for %s: +%d / -%d", after, n_a, n_r)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    if not _is_target_guild(member.guild):
        return
    if member.bot:
        return

    now_iso = db.now_utc()
    db.close_active_session(conn, member.id, left_at=now_iso)
    # Discord may not fire on_member_update for the role wipe that accompanies a leave/kick,
    # so explicitly record `removed` events for every role the member was holding at exit.
    n = 0
    for r in member.roles:
        if r.is_default():
            continue
        db.insert_role_event(
            conn, member.id, "removed", r.id, r.name,
            timestamp=now_iso, source="on_member_remove",
        )
        n += 1
    db.update_last_seen(conn, member.id, now_iso)
    log.info("Member left: %s (id=%s) — closed session, recorded %d removed events", member, member.id, n)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
