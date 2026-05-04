"""KF Observer — read-only Discord state inspector for KF Japanese Lab.

Unlike kf_tenshi / kf_role_logger which run as resident daemons, this is a
**short-lived CLI**: connect → query → output → disconnect. It carries the
Discord bot token in `.env`, and exposes subcommands so Claude (in dev sessions)
or weekly KPI scripts can pull authoritative server state without scraping
Discord UI.

Bot permissions (configured in Developer Portal + OAuth2 invite URL):
  - View Channels
  - Read Message History
  - Server Members Intent (privileged, enabled in portal)

The bot has zero write permissions — cannot post, kick, ban, or change roles.

Usage examples:
  python observe_lab.py snapshot
  python observe_lab.py members
  python observe_lab.py role-distribution
  python observe_lab.py recent-activity --hours 24
  python observe_lab.py reactions --channel daily-japanese --days 7
  python observe_lab.py pain-points

Output is JSON to stdout by default. Pass `--markdown` for human-readable
formatting where applicable. `snapshot` always also writes a timestamped file
to `kf_observer/data/snapshots/YYYY-MM-DD_HHMM.json` for KPI tally pickup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

import discord
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID_RAW = os.environ.get("GUILD_ID", "")

DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

JST = timezone(timedelta(hours=9))

DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01 UTC, used for snowflake → datetime decode


def snowflake_to_datetime(snowflake_id: int) -> datetime:
    """Decode a Discord snowflake ID into the UTC datetime it was minted."""
    timestamp_ms = (snowflake_id >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def _require_env() -> tuple[str, int]:
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN not set. Copy .env.example to .env and fill in the bot token."
        )
    if not GUILD_ID_RAW:
        raise SystemExit("GUILD_ID not set in .env.")
    try:
        guild_id = int(GUILD_ID_RAW)
    except ValueError as e:
        raise SystemExit(f"GUILD_ID must be an integer, got {GUILD_ID_RAW!r}") from e
    return DISCORD_TOKEN, guild_id


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True
    intents.guilds = True
    intents.reactions = True
    return intents


async def with_client(
    action: Callable[[discord.Client, discord.Guild], Coroutine[Any, Any, Any]],
) -> Any:
    """Connect → run action → disconnect. Returns the action's return value."""
    token, guild_id = _require_env()
    intents = _build_intents()
    client = discord.Client(intents=intents)
    result: dict[str, Any] = {"data": None, "error": None}
    done = asyncio.Event()

    @client.event
    async def on_ready() -> None:
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                # Not yet cached; fetch directly
                guild = await client.fetch_guild(guild_id)
            # Force member chunk so .members is populated when intent allows
            if guild is not None and not guild.chunked:
                try:
                    await guild.chunk(cache=True)
                except (discord.HTTPException, discord.ClientException):
                    pass
            result["data"] = await action(client, guild)
        except Exception as e:  # noqa: BLE001 — we propagate via result
            result["error"] = e
        finally:
            done.set()
            await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure as e:
        raise SystemExit(f"Discord login failed: {e}. Check DISCORD_TOKEN value.") from e

    await done.wait()
    if result["error"] is not None:
        raise result["error"]
    return result["data"]


def _emit(payload: Any, markdown_renderer: Callable[[Any], str] | None, args: argparse.Namespace) -> None:
    if getattr(args, "markdown", False) and markdown_renderer is not None:
        print(markdown_renderer(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

async def _action_snapshot(client: discord.Client, guild: discord.Guild) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    for role in guild.roles:
        if role.is_default():
            continue
        role_counts[role.name] = sum(1 for m in guild.members if role in m.roles)

    channels: list[dict[str, Any]] = []
    for ch in guild.channels:
        channels.append({
            "id": ch.id,
            "name": ch.name,
            "type": str(ch.type),
            "category": ch.category.name if ch.category else None,
            "position": ch.position,
        })

    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "guild_id": guild.id,
        "guild_name": guild.name,
        "member_count": guild.member_count,
        "members_chunked": guild.chunked,
        "role_distribution": role_counts,
        "channels": channels,
    }


def cmd_snapshot(args: argparse.Namespace) -> int:
    payload = asyncio.run(with_client(_action_snapshot))

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(JST).strftime("%Y-%m-%d_%H%M")
    snapshot_path = SNAPSHOT_DIR / f"{timestamp}.json"
    latest_path = SNAPSHOT_DIR / "latest.json"
    snapshot_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    latest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"# Snapshot saved: {snapshot_path}", file=sys.stderr)

    _emit(payload, None, args)
    return 0


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------

async def _action_members(client: discord.Client, guild: discord.Guild) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in guild.members:
        if m.bot:
            continue
        rows.append({
            "user_id": m.id,
            "name": m.name,
            "display_name": m.display_name,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "roles": [r.name for r in m.roles if not r.is_default()],
        })
    rows.sort(key=lambda r: r["joined_at"] or "")
    return rows


def _md_members(rows: list[dict[str, Any]]) -> str:
    lines = ["| user_id | name | joined_at | roles |", "| --- | --- | --- | --- |"]
    for r in rows:
        roles = ", ".join(r["roles"]) or "—"
        lines.append(f"| `{r['user_id']}` | {r['display_name']} | {r['joined_at'] or '—'} | {roles} |")
    return "\n".join(lines)


def cmd_members(args: argparse.Namespace) -> int:
    rows = asyncio.run(with_client(_action_members))
    _emit(rows, _md_members, args)
    return 0


# ---------------------------------------------------------------------------
# role-distribution
# ---------------------------------------------------------------------------

async def _action_role_distribution(client: discord.Client, guild: discord.Guild) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for role in guild.roles:
        if role.is_default():
            continue
        counts[role.name] = sum(1 for m in guild.members if role in m.roles and not m.bot)
    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "total_human_members": sum(1 for m in guild.members if not m.bot),
        "role_counts": counts,
    }


def _md_role_distribution(payload: dict[str, Any]) -> str:
    lines = [f"**Total human members:** {payload['total_human_members']}", "", "| role | count |", "| --- | --- |"]
    for role, n in sorted(payload["role_counts"].items(), key=lambda x: -x[1]):
        lines.append(f"| {role} | {n} |")
    return "\n".join(lines)


def cmd_role_distribution(args: argparse.Namespace) -> int:
    payload = asyncio.run(with_client(_action_role_distribution))
    _emit(payload, _md_role_distribution, args)
    return 0


# ---------------------------------------------------------------------------
# recent-activity
# ---------------------------------------------------------------------------

async def _action_recent_activity(
    client: discord.Client, guild: discord.Guild, since_utc: datetime,
) -> dict[str, Any]:
    per_channel: list[dict[str, Any]] = []
    for ch in guild.text_channels:
        try:
            msgs = [m async for m in ch.history(limit=200, after=since_utc)]
        except discord.Forbidden:
            continue
        if not msgs:
            continue
        reaction_total = sum(sum(r.count for r in m.reactions) for m in msgs)
        per_channel.append({
            "channel": ch.name,
            "message_count": len(msgs),
            "reaction_total": reaction_total,
            "first_at_utc": msgs[-1].created_at.isoformat() if msgs else None,
            "last_at_utc": msgs[0].created_at.isoformat() if msgs else None,
        })
    per_channel.sort(key=lambda c: -c["message_count"])
    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "since_utc": since_utc.isoformat(),
        "channels": per_channel,
    }


def cmd_recent_activity(args: argparse.Namespace) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    payload = asyncio.run(with_client(lambda c, g: _action_recent_activity(c, g, since)))
    _emit(payload, None, args)
    return 0


# ---------------------------------------------------------------------------
# reactions (per channel)
# ---------------------------------------------------------------------------

async def _action_reactions(
    client: discord.Client, guild: discord.Guild, channel_name: str, days: int,
) -> dict[str, Any]:
    target = next((c for c in guild.text_channels if c.name == channel_name), None)
    if target is None:
        return {"error": f"channel not found: {channel_name}"}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    msgs = [m async for m in target.history(limit=500, after=since)]
    reaction_buckets: dict[str, int] = {}
    per_message: list[dict[str, Any]] = []
    for m in msgs:
        msg_reactions = {}
        for r in m.reactions:
            key = str(r.emoji)
            reaction_buckets[key] = reaction_buckets.get(key, 0) + r.count
            msg_reactions[key] = r.count
        per_message.append({
            "message_id": m.id,
            "created_at_utc": m.created_at.isoformat(),
            "author": m.author.name,
            "first_50_chars": (m.content or "")[:50],
            "reactions": msg_reactions,
        })
    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "channel": channel_name,
        "since_utc": since.isoformat(),
        "message_count": len(msgs),
        "reaction_totals": reaction_buckets,
        "per_message": per_message,
    }


def cmd_reactions(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        with_client(lambda c, g: _action_reactions(c, g, args.channel, args.days))
    )
    _emit(payload, None, args)
    return 0


# ---------------------------------------------------------------------------
# inspect-user — screening for new joiners (account age, roles, activity)
# ---------------------------------------------------------------------------

async def _action_inspect_user(
    client: discord.Client, guild: discord.Guild, user_id: int,
) -> dict[str, Any]:
    member: discord.Member | None = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            account_created = snowflake_to_datetime(user_id)
            return {
                "user_id": user_id,
                "in_guild": False,
                "account_created_utc": account_created.isoformat(),
                "note": "user not currently a member of this guild (kicked / left / never joined)",
            }
        except discord.HTTPException as e:
            return {"user_id": user_id, "error": f"fetch_member failed: {e}"}

    account_created = snowflake_to_datetime(user_id)
    joined_at = member.joined_at
    age_at_join = (joined_at - account_created) if joined_at else None

    # Activity scan: count messages from this user in last 7 days, server-wide
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    msg_count = 0
    last_msg_at: datetime | None = None
    last_msg_channel: str | None = None
    for ch in guild.text_channels:
        try:
            async for m in ch.history(limit=200, after=cutoff):
                if m.author.id == user_id:
                    msg_count += 1
                    if last_msg_at is None or m.created_at > last_msg_at:
                        last_msg_at = m.created_at
                        last_msg_channel = ch.name
        except discord.Forbidden:
            continue

    # Heuristic flags — explicit, not opinions
    flags: list[str] = []
    if age_at_join is not None and age_at_join < timedelta(days=1):
        flags.append("account_age_at_join_lt_24h")
    if age_at_join is not None and age_at_join < timedelta(hours=6):
        flags.append("account_age_at_join_lt_6h")
    selectable_roles = [r.name for r in member.roles if not r.is_default() and r.name != "Newcomer"]
    if not selectable_roles:
        flags.append("no_self_selected_roles")
    if msg_count == 0 and joined_at and (datetime.now(timezone.utc) - joined_at) > timedelta(hours=48):
        flags.append("no_messages_after_48h")

    return {
        "user_id": user_id,
        "in_guild": True,
        "name": member.name,
        "display_name": member.display_name,
        "is_bot": member.bot,
        "account_created_utc": account_created.isoformat(),
        "joined_kf_lab_at_utc": joined_at.isoformat() if joined_at else None,
        "account_age_at_join": str(age_at_join) if age_at_join else None,
        "roles": [r.name for r in member.roles if not r.is_default()],
        "avatar_url": str(member.display_avatar.url) if member.display_avatar else None,
        "messages_last_7d": msg_count,
        "last_message_at_utc": last_msg_at.isoformat() if last_msg_at else None,
        "last_message_channel": last_msg_channel,
        "flags": flags,
    }


def cmd_inspect_user(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        with_client(lambda c, g: _action_inspect_user(c, g, args.user_id))
    )
    _emit(payload, None, args)
    return 0


# ---------------------------------------------------------------------------
# pain-points (threads in #pain-points-board with 🙋 counts)
# ---------------------------------------------------------------------------

async def _action_pain_points(client: discord.Client, guild: discord.Guild) -> dict[str, Any]:
    target = next(
        (c for c in guild.channels if c.name == "pain-points-board"),
        None,
    )
    if target is None:
        return {"error": "channel #pain-points-board not found"}

    threads: list[dict[str, Any]] = []
    if isinstance(target, discord.ForumChannel):
        for t in target.threads:
            row = await _summarize_thread(t)
            threads.append(row)
        async for t in target.archived_threads(limit=50):
            row = await _summarize_thread(t)
            row["archived"] = True
            threads.append(row)
    elif isinstance(target, discord.TextChannel):
        for t in target.threads:
            row = await _summarize_thread(t)
            threads.append(row)
        async for t in target.archived_threads(limit=50):
            row = await _summarize_thread(t)
            row["archived"] = True
            threads.append(row)
    else:
        return {"error": f"channel #pain-points-board has unexpected type: {type(target).__name__}"}

    threads.sort(key=lambda t: -t.get("hand_raise_count", 0))
    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "channel_type": type(target).__name__,
        "thread_count": len(threads),
        "threads": threads,
    }


async def _summarize_thread(thread: discord.Thread) -> dict[str, Any]:
    hand_raise = 0
    try:
        starter = thread.starter_message or await thread.fetch_message(thread.id)
        if starter is not None:
            for r in starter.reactions:
                if str(r.emoji) == "🙋":
                    hand_raise = r.count
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    return {
        "thread_id": thread.id,
        "name": thread.name,
        "created_at_utc": thread.created_at.isoformat() if thread.created_at else None,
        "message_count": thread.message_count,
        "member_count": thread.member_count,
        "hand_raise_count": hand_raise,
        "archived": False,
    }


def cmd_pain_points(args: argparse.Namespace) -> int:
    payload = asyncio.run(with_client(_action_pain_points))
    _emit(payload, None, args)
    return 0


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KF Observer — read-only Discord state inspector for KF Japanese Lab",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--markdown",
        action="store_true",
        help="Render output as markdown where supported (default: JSON)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "snapshot",
        parents=[common],
        help="Full state snapshot → stdout + data/snapshots/<ts>.json",
    )
    sub.add_parser("members", parents=[common], help="Human members with their roles")
    sub.add_parser("role-distribution", parents=[common], help="Member count per role")

    sp = sub.add_parser(
        "recent-activity", parents=[common], help="Per-channel activity in the last N hours"
    )
    sp.add_argument("--hours", type=int, default=24)

    sp = sub.add_parser(
        "reactions", parents=[common], help="Reaction tallies in a channel over the last N days"
    )
    sp.add_argument("--channel", required=True)
    sp.add_argument("--days", type=int, default=7)

    sub.add_parser(
        "pain-points", parents=[common], help="Threads in #pain-points-board with 🙋 counts"
    )

    sp = sub.add_parser(
        "inspect-user",
        parents=[common],
        help="Screen a single user (snowflake age, roles, recent activity, heuristic flags)",
    )
    sp.add_argument("--user-id", type=int, required=True)

    return parser


COMMANDS = {
    "snapshot": cmd_snapshot,
    "members": cmd_members,
    "role-distribution": cmd_role_distribution,
    "recent-activity": cmd_recent_activity,
    "reactions": cmd_reactions,
    "pain-points": cmd_pain_points,
    "inspect-user": cmd_inspect_user,
}


def main() -> int:
    args = build_parser().parse_args()
    handler = COMMANDS.get(args.cmd)
    if handler is None:
        print(f"unknown subcommand: {args.cmd}", file=sys.stderr)
        return 2
    return handler(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
