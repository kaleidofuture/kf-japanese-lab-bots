"""KF Tenshi — Kotoba quiz pass detector & N* V role granter for KF Japanese Lab.

Listens for Kotoba bot quiz-end embeds, parses final scores, and grants the
matching `N* V` Verified role to members who clear the pass threshold
(default: 20 / 25 — see Tenshi-Bot's `jlptVariables.js`).

Designed to be extended later with KF RoleLogger features (role transition log,
time-based Newcomer→Member promotion) in the same process.
"""

import logging
import os
import re
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
KOTOBA_BOT_ID = int(os.environ.get("KOTOBA_BOT_ID", "251239170058616833"))

ROLE_MAP = {
    "N5": int(os.environ["ROLE_N5_V"]),
    "N4": int(os.environ["ROLE_N4_V"]),
    "N3": int(os.environ["ROLE_N3_V"]),
    "N2": int(os.environ["ROLE_N2_V"]),
    "N1": int(os.environ["ROLE_N1_V"]),
}

PASS_SCORE = int(os.environ.get("PASS_SCORE", "20"))

LOG_FILE = ROOT / "kf_tenshi.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("kf_tenshi")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!kft ", intents=intents)


DECK_PATTERN = re.compile(r"\bgN([1-5])\+N\1\b|\bN([1-5])\b")
SCORE_LINE_PATTERN = re.compile(
    r"<@!?(?P<uid>\d+)>\s+has\s+(?P<score>\d+)\s+points?"
    r"|"
    r"<@!?(?P<uid2>\d+)>\s*[—–\-]\s*(?P<score2>\d+)"
)


def detect_level(embed: discord.Embed) -> str | None:
    """Determine which JLPT level a quiz embed corresponds to.

    Looks at title, description, and footer for a deck token like `gN5+N5` or
    a bare `N5` mention. Returns the matched level token (`'N1'`..`'N5'`) or None.
    """
    blobs: list[str] = []
    if embed.title:
        blobs.append(embed.title)
    if embed.description:
        blobs.append(embed.description)
    if embed.footer and embed.footer.text:
        blobs.append(embed.footer.text)
    for f in embed.fields:
        if f.name:
            blobs.append(f.name)
        if f.value:
            blobs.append(f.value)

    for blob in blobs:
        m = DECK_PATTERN.search(blob)
        if m:
            level = m.group(1) or m.group(2)
            return f"N{level}"
    return None


def parse_final_scores(embed: discord.Embed) -> list[tuple[int, int]]:
    """Extract (user_id, score) pairs from the Final Scores field."""
    for f in embed.fields:
        if f.name and "Final Scores" in f.name:
            results: list[tuple[int, int]] = []
            for line in (f.value or "").splitlines():
                m = SCORE_LINE_PATTERN.search(line)
                if m:
                    uid = m.group("uid") or m.group("uid2")
                    score = m.group("score") or m.group("score2")
                    results.append((int(uid), int(score)))
            return results
    return []


@bot.event
async def on_ready() -> None:
    log.info("KF Tenshi online as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    log.info("Watching guild=%s, kotoba=%s, pass_score=%s", GUILD_ID, KOTOBA_BOT_ID, PASS_SCORE)
    log.info("Role map: %s", ROLE_MAP)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.id != KOTOBA_BOT_ID:
        return
    if message.guild is None or message.guild.id != GUILD_ID:
        return
    if not message.embeds:
        return

    for embed in message.embeds:
        if not embed.title or "Quiz Ended" not in embed.title:
            continue
        if embed.description and "asked me to stop the quiz" in embed.description:
            log.info("Quiz stopped by user — skipping role grant.")
            continue

        level = detect_level(embed)
        if level is None:
            log.warning("Quiz ended but level could not be detected from embed: title=%r", embed.title)
            continue

        role_id = ROLE_MAP.get(level)
        if role_id is None:
            log.warning("No role configured for detected level %s", level)
            continue

        scores = parse_final_scores(embed)
        if not scores:
            log.warning("No Final Scores parsed from embed (level=%s)", level)
            continue

        guild = message.guild
        role = guild.get_role(role_id)
        if role is None:
            log.error("Role id=%s for level %s not found in guild", role_id, level)
            continue

        for user_id, score in scores:
            if score < PASS_SCORE:
                log.info("User %s scored %d (<%d) on %s — no grant.", user_id, score, PASS_SCORE, level)
                continue
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    log.warning("Passing user %s not found in guild — skipping.", user_id)
                    continue
            if role in member.roles:
                log.info("User %s already has %s V — skipping.", member, level)
                continue
            try:
                await member.add_roles(role, reason=f"Kotoba quiz pass {level} ({score}/{PASS_SCORE})")
                log.info("Granted %s V to %s (score=%d).", level, member, score)
            except discord.Forbidden:
                log.error("Forbidden: cannot grant %s V to %s — check role hierarchy.", level, member)
            except discord.HTTPException as e:
                log.error("HTTPException granting %s V to %s: %s", level, member, e)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
