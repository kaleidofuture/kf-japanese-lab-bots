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
  - Message Content Intent (privileged, enabled in portal 2026-06-18 — without it
    m.content returns "" so reactions/inspect-user excerpts and the content-based
    scam flags silently no-op)

The bot has zero write permissions — cannot post, kick, ban, or change roles.

Usage examples:
  python observe_lab.py snapshot
  python observe_lab.py members
  python observe_lab.py role-distribution
  python observe_lab.py recent-activity --hours 24
  python observe_lab.py reactions --channel daily-japanese --days 7
  python observe_lab.py messages --channel 日本語-only --hours 96
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
import re
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

# β期トリガー判定で「除外」する Marine 系 user_id（18_beta_trigger_definition.md と整合）
MARINE_USER_ID = 1498849053121843352
MARINE7310_USER_ID = 1190550699151478784
EXCLUDED_USER_IDS: set[int] = {MARINE_USER_ID, MARINE7310_USER_ID}

# scam 検知用パターン（project_jp_lab_discord_alpha.md / project_jp_lab_kpi_measurement.md と整合）
PROTECTED_ROLE_NAMES = {"Founder", "Admin", "Moderator", "Mod", "Owner", "Staff"}

DM_REDIRECTION_PATTERNS = [
    re.compile(r"\bch[.\W_]*ck[.\W_]+(?:your[.\W_]+)?(?:dm|pm|inbox|messages?)", re.IGNORECASE),
    re.compile(r"\bdm\s+me\b", re.IGNORECASE),
    re.compile(r"\bpm\s+you\b", re.IGNORECASE),
    re.compile(r"\bmessag(?:ed|ing)?\s+you\b", re.IGNORECASE),
    re.compile(r"\binbox\b.*\bme\b", re.IGNORECASE),
]

TITLE_IMPERSONATION_PATTERN = re.compile(
    r"\b(?:prof|professor|admin|administrator|mod|moderator|staff|owner|founder|sensei|teacher|tutor)[\W_]*",
    re.IGNORECASE,
)

FIRST_POST_FAST_THRESHOLD_MINUTES = 5
REACTION_ONLY_SCAN_HOURS = 24
REACTION_ONLY_MIN_COUNT = 3


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
    intents.message_content = True  # 2026-06-18: 本文取得（messages dump + reactions/inspect-user の excerpt + content系scam検知）
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
# messages — full-content dump for a single channel (requires Message Content Intent)
# ---------------------------------------------------------------------------

async def _action_messages(
    client: discord.Client, guild: discord.Guild, channel_name: str, hours: int, limit: int,
) -> dict[str, Any]:
    target = next((c for c in guild.text_channels if c.name == channel_name), None)
    if target is None:
        return {"error": f"channel not found: {channel_name}"}
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        msgs = [m async for m in target.history(limit=limit, after=since, oldest_first=True)]
    except discord.Forbidden:
        return {"error": f"forbidden: bot cannot read history of #{channel_name}"}
    rows: list[dict[str, Any]] = []
    for m in msgs:
        rows.append({
            "message_id": m.id,
            "created_at_utc": m.created_at.isoformat(),
            "created_at_jst": m.created_at.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
            "author": m.author.name,
            "author_display": m.author.display_name,
            "content": m.content or "",
            "attachments": [a.url for a in m.attachments],
            "reactions": {str(r.emoji): r.count for r in m.reactions},
        })
    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "channel": channel_name,
        "since_utc": since.isoformat(),
        "message_count": len(rows),
        "messages": rows,
    }


def _md_messages(payload: dict[str, Any]) -> str:
    if "error" in payload:
        return f"**error:** {payload['error']}"
    lines = [
        f"**#{payload['channel']}** — {payload['message_count']} messages "
        f"(since {payload['since_utc']})",
        "",
    ]
    for m in payload["messages"]:
        body = (m["content"] or "").replace("\n", "\n    ")  # 複数行はインデントで可読性維持
        if not body:
            body = "[no text]" if not m["attachments"] else ""
        att = f"  [+{len(m['attachments'])} attachment(s)]" if m["attachments"] else ""
        react = ""
        if m["reactions"]:
            react = "  " + " ".join(f"{k}×{v}" for k, v in m["reactions"].items())
        lines.append(
            f"- `{m['created_at_jst']} JST` **{m['author_display']}**: {body}{att}{react}"
        )
    return "\n".join(lines)


def cmd_messages(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        with_client(lambda c, g: _action_messages(c, g, args.channel, args.hours, args.limit))
    )
    _emit(payload, _md_messages, args)
    return 0


# ---------------------------------------------------------------------------
# inspect-user — screening for new joiners (account age, roles, activity)
# ---------------------------------------------------------------------------

async def _scan_user_messages(
    guild: discord.Guild, user_id: int, since: datetime, history_limit: int = 200,
) -> tuple[int, discord.Message | None, discord.Message | None]:
    """全 text channel をスキャンし、user_id の投稿数・最初の投稿・最新の投稿を返す。

    Returns:
        (msg_count, first_message, last_message)
    """
    msg_count = 0
    first_msg: discord.Message | None = None
    last_msg: discord.Message | None = None
    for ch in guild.text_channels:
        try:
            async for m in ch.history(limit=history_limit, after=since):
                if m.author.id == user_id:
                    msg_count += 1
                    if first_msg is None or m.created_at < first_msg.created_at:
                        first_msg = m
                    if last_msg is None or m.created_at > last_msg.created_at:
                        last_msg = m
        except discord.Forbidden:
            continue
    return msg_count, first_msg, last_msg


def _detect_dm_redirection(content: str) -> bool:
    if not content:
        return False
    for pat in DM_REDIRECTION_PATTERNS:
        if pat.search(content):
            return True
    return False


def _detect_title_impersonation(display_name: str) -> bool:
    if not display_name:
        return False
    return bool(TITLE_IMPERSONATION_PATTERN.search(display_name))


def _detect_protected_role_mention(message: discord.Message) -> bool:
    if not message:
        return False
    mentioned_role_names = {r.name for r in (message.role_mentions or [])}
    if mentioned_role_names & PROTECTED_ROLE_NAMES:
        return True
    # 表記が "@Founder" などをテキストで書いている場合（実 mention でなく文字列）も拾う
    content = message.content or ""
    for protected in PROTECTED_ROLE_NAMES:
        if re.search(rf"@\s*{re.escape(protected)}\b", content, re.IGNORECASE):
            return True
    return False


async def _count_reactions_given_by_user(
    guild: discord.Guild, user_id: int, since: datetime, history_limit: int = 100,
) -> int:
    """user_id がリアクションを付けた message 数を概算でカウント。

    Discord API レート制限を踏まえ、history_limit を絞り、リアクションが付いている message にのみ
    users() を呼ぶ。Newcomer 投稿0 + 参加24h以内のような限定的な scenario を想定。
    """
    count = 0
    for ch in guild.text_channels:
        try:
            async for m in ch.history(limit=history_limit, after=since):
                if not m.reactions:
                    continue
                for r in m.reactions:
                    try:
                        async for u in r.users():
                            if u.id == user_id:
                                count += 1
                                break
                    except discord.HTTPException:
                        continue
        except discord.Forbidden:
            continue
    return count


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
    msg_count, first_msg, last_msg = await _scan_user_messages(guild, user_id, cutoff)

    last_msg_at = last_msg.created_at if last_msg else None
    last_msg_channel = last_msg.channel.name if last_msg else None

    # First-post derived fields (2026-05-18 追加, Prof 事案ベース)
    first_post_at: datetime | None = first_msg.created_at if first_msg else None
    first_post_channel: str | None = first_msg.channel.name if first_msg else None
    first_post_excerpt: str | None = (first_msg.content or "")[:100] if first_msg else None
    first_post_lag_min: float | None = None
    if first_msg and joined_at:
        first_post_lag_min = (first_msg.created_at - joined_at).total_seconds() / 60.0

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

    # 2026-05-18 追加: aged-account × DM phishing 検知用フラグ (Prof_🧑‍🏫 事案ベース)
    if first_msg is not None:
        if _detect_protected_role_mention(first_msg):
            flags.append("founder_mention_within_first_post")
        if _detect_dm_redirection(first_msg.content or ""):
            flags.append("dm_redirection_phrase_detected")
    if _detect_title_impersonation(member.display_name):
        flags.append("title_impersonation_in_display_name")
    if (
        first_post_lag_min is not None
        and 0 <= first_post_lag_min < FIRST_POST_FAST_THRESHOLD_MINUTES
    ):
        flags.append(f"first_post_within_{FIRST_POST_FAST_THRESHOLD_MINUTES}min_of_join")

    # reaction_only_engagement: Newcomer × 投稿0 × 参加24h以内 でのみ実行（重い処理を限定）
    is_newcomer = any(r.name == "Newcomer" for r in member.roles)
    reaction_count_within_24h: int | None = None
    if (
        is_newcomer
        and msg_count == 0
        and joined_at is not None
        and (datetime.now(timezone.utc) - joined_at) <= timedelta(hours=REACTION_ONLY_SCAN_HOURS)
    ):
        reaction_count_within_24h = await _count_reactions_given_by_user(
            guild, user_id, joined_at,
        )
        if reaction_count_within_24h >= REACTION_ONLY_MIN_COUNT:
            flags.append(
                f"reaction_only_engagement_within_first_{REACTION_ONLY_SCAN_HOURS}h"
            )

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
        "first_post_at_utc": first_post_at.isoformat() if first_post_at else None,
        "first_post_channel": first_post_channel,
        "first_post_excerpt": first_post_excerpt,
        "first_post_lag_min": first_post_lag_min,
        "reaction_count_within_24h": reaction_count_within_24h,
        "flags": flags,
    }


def cmd_inspect_user(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        with_client(lambda c, g: _action_inspect_user(c, g, args.user_id))
    )
    _emit(payload, None, args)
    return 0


# ---------------------------------------------------------------------------
# assess-effective-members — β期トリガー判定用、有効 Member 概念で全 Member を集計
# (2026-05-18 追加, 18_beta_trigger_definition.md 改訂版と整合)
# ---------------------------------------------------------------------------

EFFECTIVE_MEMBERS_DIR = DATA_DIR / "effective_members"
NEWCOMER_LURKER_DAYS = 14  # Newcomer のまま 14日経過は母数除外
INACTIVE_DAYS_THRESHOLD = 90  # 90日連続無活動は母数除外


async def _evaluate_member_effectiveness(
    guild: discord.Guild, member: discord.Member, since: datetime,
) -> dict[str, Any]:
    """1人のメンバーの「有効 Member 条件」を評価して構造化 dict を返す。

    有効 Member = Member ロール保持 AND
      (ロール自己選択 ≥1個 OR パブリック投稿 ≥1件 OR リアクション付与 ≥1個)
      AND scam フラグなし
    """
    selectable_roles = [
        r.name for r in member.roles
        if not r.is_default() and r.name not in ("Newcomer", "Member")
    ]
    has_role = len(selectable_roles) >= 1

    msg_count, first_msg, _last_msg = await _scan_user_messages(
        guild, member.id, since, history_limit=500,
    )
    has_post = msg_count >= 1

    # リアクション付与カウントは、ロール選択も投稿も無い場合のみ実行（API 節約）
    reaction_count: int | None = None
    has_reaction = False
    if not (has_role or has_post):
        reaction_count = await _count_reactions_given_by_user(
            guild, member.id, since, history_limit=100,
        )
        has_reaction = reaction_count >= 1

    # scam フラグ（軽量判定）
    scam_flags: list[str] = []
    if _detect_title_impersonation(member.display_name):
        scam_flags.append("title_impersonation_in_display_name")
    if first_msg is not None:
        if _detect_protected_role_mention(first_msg):
            scam_flags.append("founder_mention_within_first_post")
        if _detect_dm_redirection(first_msg.content or ""):
            scam_flags.append("dm_redirection_phrase_detected")
        if member.joined_at is not None:
            lag_min = (first_msg.created_at - member.joined_at).total_seconds() / 60.0
            if 0 <= lag_min < FIRST_POST_FAST_THRESHOLD_MINUTES:
                scam_flags.append(
                    f"first_post_within_{FIRST_POST_FAST_THRESHOLD_MINUTES}min_of_join"
                )

    is_effective = (has_role or has_post or has_reaction) and not scam_flags

    return {
        "user_id": member.id,
        "name": member.name,
        "display_name": member.display_name,
        "joined_at_utc": member.joined_at.isoformat() if member.joined_at else None,
        "self_selected_role_count": len(selectable_roles),
        "self_selected_roles": selectable_roles,
        "public_post_count_30d": msg_count,
        "reaction_count_30d": reaction_count,
        "scam_flags": scam_flags,
        "is_effective_member": is_effective,
    }


async def _action_assess_effective_members(
    client: discord.Client, guild: discord.Guild, window_days: int,
) -> dict[str, Any]:
    member_role = discord.utils.get(guild.roles, name="Member")
    if member_role is None:
        return {"error": "Member role not found in guild"}

    holders = [
        m for m in guild.members
        if member_role in m.roles
        and not m.bot
        and m.id not in EXCLUDED_USER_IDS
    ]

    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    results = []
    for member in holders:
        evaluation = await _evaluate_member_effectiveness(guild, member, since)
        results.append(evaluation)

    # Newcomer のまま long-stay している外部メンバーも集計（lurker 除外条件の判定材料）
    newcomer_role = discord.utils.get(guild.roles, name="Newcomer")
    newcomer_only = []
    if newcomer_role is not None:
        for m in guild.members:
            if (
                newcomer_role in m.roles
                and member_role not in m.roles
                and not m.bot
                and m.id not in EXCLUDED_USER_IDS
            ):
                joined = m.joined_at
                days_in_lab = None
                if joined is not None:
                    days_in_lab = (datetime.now(timezone.utc) - joined).days
                lurker_excluded = (
                    days_in_lab is not None and days_in_lab >= NEWCOMER_LURKER_DAYS
                )
                newcomer_only.append({
                    "user_id": m.id,
                    "name": m.name,
                    "display_name": m.display_name,
                    "joined_at_utc": joined.isoformat() if joined else None,
                    "days_in_lab": days_in_lab,
                    "newcomer_lurker_excluded": lurker_excluded,
                })

    effective_count = sum(1 for r in results if r["is_effective_member"])
    scam_flagged_count = sum(1 for r in results if r["scam_flags"])
    beta_member_threshold = 10
    beta_member_pass = effective_count >= beta_member_threshold

    return {
        "captured_at_jst": datetime.now(JST).isoformat(),
        "guild_id": guild.id,
        "window_days": window_days,
        "excluded_user_ids": list(EXCLUDED_USER_IDS),
        "member_role_holders_count": len(holders),
        "effective_member_external_count": effective_count,
        "scam_flagged_external_count": scam_flagged_count,
        "beta_member_threshold": beta_member_threshold,
        "beta_member_pass": beta_member_pass,
        "member_evaluations": results,
        "newcomer_only_external": newcomer_only,
    }


def cmd_assess_effective_members(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        with_client(
            lambda c, g: _action_assess_effective_members(c, g, args.window_days)
        )
    )

    EFFECTIVE_MEMBERS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(JST).strftime("%Y-%m-%d_%H%M")
    snapshot_path = EFFECTIVE_MEMBERS_DIR / f"{timestamp}.json"
    latest_path = EFFECTIVE_MEMBERS_DIR / "latest.json"
    snapshot_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    latest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"# Effective-members assessment saved: {snapshot_path}", file=sys.stderr)

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

    sp = sub.add_parser(
        "messages",
        parents=[common],
        help="Full message content dump for a channel over the last N hours (needs Message Content Intent)",
    )
    sp.add_argument("--channel", required=True)
    sp.add_argument("--hours", type=int, default=24)
    sp.add_argument("--limit", type=int, default=200)

    sub.add_parser(
        "pain-points", parents=[common], help="Threads in #pain-points-board with 🙋 counts"
    )

    sp = sub.add_parser(
        "inspect-user",
        parents=[common],
        help="Screen a single user (snowflake age, roles, recent activity, heuristic flags)",
    )
    sp.add_argument("--user-id", type=int, required=True)

    sp = sub.add_parser(
        "assess-effective-members",
        parents=[common],
        help=(
            "Evaluate '有効 Member' for all Member-role holders (excludes Marine system "
            "accounts). Writes data/effective_members/latest.json for kfjl_ingestor to consume."
        ),
    )
    sp.add_argument(
        "--window-days", type=int, default=30,
        help="Activity scan window in days (default: 30, per 18_beta_trigger_definition.md)",
    )

    return parser


COMMANDS = {
    "snapshot": cmd_snapshot,
    "members": cmd_members,
    "role-distribution": cmd_role_distribution,
    "recent-activity": cmd_recent_activity,
    "reactions": cmd_reactions,
    "messages": cmd_messages,
    "pain-points": cmd_pain_points,
    "inspect-user": cmd_inspect_user,
    "assess-effective-members": cmd_assess_effective_members,
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
