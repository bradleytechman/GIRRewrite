"""Server-aware engagement, moderation, Apple event, and firmware tools."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.community_suite import DATA_FILE, load_settings
from utils import cfg, logger


IPSW_API = "https://api.ipsw.me/v4"
APPLE_EVENTS = "https://www.apple.com/apple-events/"
APPLE_RSS = "https://www.apple.com/newsroom/rss-feed.rss"
TSSCHECKER = os.environ.get("TSSCHECKER_PATH", str(Path.home() / "Library/Application Support/SowensServer/bin/tsschecker"))
TSS_WORK_ROOT = Path(os.environ.get("GIR_TSS_WORK_ROOT", "/Volumes/4TB/Services/TSSChecker/runtime"))
TSS_CONCURRENCY = asyncio.Semaphore(2)
APPLE_STATE = Path(os.environ.get("GIR_COMMUNITY_FILE", "community.json")).with_name("apple-events-state.json")

QUESTIONS = (
    "What small thing made your day better?", "What game deserves a remake?", "What is your perfect weekend?",
    "Which skill would you learn instantly?", "What song never gets old?", "What is your favorite comfort food?",
)
WOULD_YOU_RATHER = (
    "Would you rather explore space or the deepest ocean?", "Would you rather have unlimited travel or unlimited food?",
    "Would you rather always be ten minutes early or never wait in line?", "Would you rather relive one day or skip one bad day?",
)
COMPLIMENTS = ("brings great energy", "makes this server more fun", "has excellent taste", "is someone people can count on")


class ServerSuite(commands.Cog):
    engage = app_commands.Group(name="engage", description="Games and friendly community activities", guild_ids=[cfg.guild_id])
    modtools = app_commands.Group(name="modtools", description="Extra tools for moderators", guild_ids=[cfg.guild_id],
                                  default_permissions=discord.Permissions(manage_messages=True))
    apple = app_commands.Group(name="apple", description="Apple events and firmware signing", guild_ids=[cfg.guild_id])
    tss = app_commands.Group(name="tss", description="Check Apple firmware signing and device support", guild_ids=[cfg.guild_id])

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._devices = []
        self._device_cache_time = 0.0
        self.apple_event_check.start()

    def cog_unload(self):
        self.apple_event_check.cancel()

    @engage.command(name="eightball", description="Ask GIR a yes-or-no question")
    async def eightball(self, interaction: discord.Interaction, question: str):
        answer = random.choice(("Yes.", "Probably.", "Signs point to yes.", "Ask again later.", "Probably not.", "No."))
        await interaction.response.send_message(f"🎱 **{question[:300]}**\n{answer}")

    @engage.command(name="question", description="Post a conversation starter")
    async def question(self, interaction: discord.Interaction):
        await interaction.response.send_message("💬 " + random.choice(QUESTIONS))

    @engage.command(name="would-you-rather", description="Post a would-you-rather question")
    async def would_you_rather(self, interaction: discord.Interaction):
        await interaction.response.send_message("🤔 " + random.choice(WOULD_YOU_RATHER))

    @engage.command(name="compliment", description="Send a friendly compliment")
    async def compliment(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.send_message(f"✨ {member.mention} {random.choice(COMPLIMENTS)}.")

    @engage.command(name="highfive", description="Give someone a high five")
    async def highfive(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.send_message(f"🙌 {interaction.user.mention} high-fived {member.mention}!")

    @engage.command(name="hug", description="Send someone a friendly hug")
    async def hug(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.send_message(f"🫂 {interaction.user.mention} sent {member.mention} a hug.")

    @engage.command(name="match", description="Calculate a repeatable friendship score")
    async def match(self, interaction: discord.Interaction, first: discord.Member, second: discord.Member):
        pair = ":".join(map(str, sorted((first.id, second.id)))).encode()
        score = int(hashlib.sha256(pair).hexdigest()[:8], 16) % 101
        await interaction.response.send_message(f"💚 {first.mention} + {second.mention}: **{score}%** match")

    @engage.command(name="decide", description="Choose fairly from a list separated by commas")
    async def decide(self, interaction: discord.Interaction, choices: str):
        items = [item.strip() for item in choices.split(",") if item.strip()]
        if len(items) < 2:
            await interaction.response.send_message("Give me at least two choices separated by commas.", ephemeral=True); return
        await interaction.response.send_message(f"🎯 GIR chooses **{random.choice(items)}**")

    @engage.command(name="random-member", description="Choose a random non-bot member")
    async def random_member(self, interaction: discord.Interaction):
        choices = [member for member in interaction.guild.members if not member.bot]
        if not choices:
            await interaction.response.send_message("I could not find an eligible member.", ephemeral=True); return
        await interaction.response.send_message(f"🎉 {random.choice(choices).mention} was chosen!")

    @modtools.command(name="role-add", description="Give a manageable role to a member")
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if role >= interaction.guild.me.top_role or role.is_default() or role.managed:
            await interaction.response.send_message("GIR cannot manage that role. Move GIR above it or choose another role.", ephemeral=True); return
        await member.add_roles(role, reason=f"Added by {interaction.user}")
        await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", ephemeral=True)

    @modtools.command(name="role-remove", description="Remove a manageable role from a member")
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if role >= interaction.guild.me.top_role or role.is_default() or role.managed:
            await interaction.response.send_message("GIR cannot manage that role. Move GIR above it or choose another role.", ephemeral=True); return
        await member.remove_roles(role, reason=f"Removed by {interaction.user}")
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", ephemeral=True)

    @modtools.command(name="permissions", description="Explain what GIR can do to a member")
    async def permissions(self, interaction: discord.Interaction, member: discord.Member):
        manageable = member != interaction.guild.owner and member.top_role < interaction.guild.me.top_role
        await interaction.response.send_message(
            f"**{member.display_name}**\nTop role: {member.top_role.mention}\n"
            f"GIR can moderate this member: **{'Yes' if manageable else 'No'}**\n"
            f"Administrator: **{'Yes' if member.guild_permissions.administrator else 'No'}**", ephemeral=True)

    @modtools.command(name="clear-reactions", description="Remove every reaction from a message")
    async def clear_reactions(self, interaction: discord.Interaction, message_id: str):
        if not message_id.isdigit():
            await interaction.response.send_message("Enter a numeric message ID.", ephemeral=True); return
        try:
            message = await interaction.channel.fetch_message(int(message_id)); await message.clear_reactions()
        except discord.HTTPException:
            await interaction.response.send_message("I could not find that message or clear its reactions.", ephemeral=True); return
        await interaction.response.send_message("Reactions cleared.", ephemeral=True)

    @modtools.command(name="role-members", description="List members who have a role")
    async def role_members(self, interaction: discord.Interaction, role: discord.Role):
        names = [member.mention for member in role.members]
        text = " ".join(names[:80]) or "No cached members have this role."
        if len(names) > 80:
            text += f"\n…and {len(names) - 80} more."
        await interaction.response.send_message(f"**{role.name} · {len(names)} members**\n{text}"[:2000], ephemeral=True)

    @modtools.command(name="channel-info", description="Show a channel's IDs, topic, and rate limit")
    async def channel_info(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.send_message(
            f"**#{channel.name}**\nID: `{channel.id}`\nCategory: {channel.category.name if channel.category else 'None'}\n"
            f"Slow mode: {channel.slowmode_delay} seconds\nTopic: {channel.topic or 'None'}", ephemeral=True)

    @modtools.command(name="bots", description="List bot accounts currently in the server cache")
    async def bots(self, interaction: discord.Interaction):
        bots = [member for member in interaction.guild.members if member.bot]
        text = "\n".join(f"• {member} · `{member.id}`" for member in bots[:50]) or "No bots found."
        await interaction.response.send_message(f"**Bots · {len(bots)}**\n{text}"[:2000], ephemeral=True)

    async def _get_json(self, url: str):
        async with aiohttp.ClientSession(headers={"User-Agent": "GIR/1.0"}) as session:
            async with session.get(url, timeout=25) as response:
                response.raise_for_status(); return await response.json()

    async def _find_device(self, device_text: str):
        devices = await self._get_json(IPSW_API + "/devices")
        names = {str(item["name"]).lower(): item for item in devices}
        identifiers = {str(item["identifier"]).lower(): item for item in devices}
        needle = device_text.strip().lower()
        device = names.get(needle) or identifiers.get(needle)
        if not device:
            match = difflib.get_close_matches(needle, list(names) + list(identifiers), n=1, cutoff=.58)
            device = (names.get(match[0]) or identifiers.get(match[0])) if match else None
        if not device:
            raise ValueError(f"I could not match “{device_text}” to an Apple device.")
        return device

    async def _resolve_signing(self, question: str):
        version_match = re.search(r"(?:ios\s*)?(\d+(?:\.\d+){1,2})", question, re.I)
        device_text = re.split(r"\bfor\b", question, flags=re.I)[-1].strip(" ?.!")
        device_text = re.sub(r"\b(is|signed|signing|ios|ipados)\b|\d+(?:\.\d+){1,2}", " ", device_text, flags=re.I).strip()
        if not version_match or not device_text:
            raise ValueError("Ask like: Is iOS 17.1 signed for iPhone 16?")
        device = await self._find_device(device_text)
        version = version_match.group(1)
        data = await self._get_json(f"{IPSW_API}/device/{device['identifier']}?type=ipsw")
        firmware = next((item for item in data.get("firmwares", []) if item.get("version") == version), None)
        if not firmware:
            return device, version, "incompatible", "That iOS version was not released for this device."
        result = None
        if Path(TSSCHECKER).is_file():
            work_dir = None
            try:
                TSS_WORK_ROOT.mkdir(parents=True, exist_ok=True)
                work_dir = Path(tempfile.mkdtemp(prefix="request-", dir=TSS_WORK_ROOT))
                environment = os.environ.copy()
                environment.update({"TMPDIR": str(work_dir), "HOME": str(work_dir), "XDG_CACHE_HOME": str(work_dir / "cache")})
                async with TSS_CONCURRENCY:
                    process = await asyncio.create_subprocess_exec(
                        TSSCHECKER, "-d", device["identifier"], "-i", version, "-b",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        cwd=work_dir, env=environment,
                    )
                    try:
                        output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.communicate()
                        raise
                text = output.decode(errors="replace")
                if "IS being signed" in text: result = True
                elif "IS NOT being signed" in text: result = False
            except (OSError, asyncio.TimeoutError):
                logger.exception("TSSChecker signing request failed")
            finally:
                if work_dir:
                    shutil.rmtree(work_dir, ignore_errors=True)
        catalog_signed = bool(firmware.get("signed"))
        if result is not None and result != catalog_signed:
            return device, version, "uncertain", "TSSChecker and the firmware catalog disagree. Try again later before restoring."
        signed = catalog_signed if result is None else result
        return device, version, "signed" if signed else "unsigned", "Checked with TSSChecker and Apple's signing data."

    async def _current_signing_summary(self):
        """Return a useful live result when /tss check has no question."""
        devices = await self._get_json(IPSW_API + "/devices")
        preferred_names = ("iPhone 16", "iPhone 16 Pro", "iPhone 15", "iPad Pro 11-inch (M4)")
        by_name = {str(item.get("name", "")).lower(): item for item in devices}
        selected = [by_name[name.lower()] for name in preferred_names if name.lower() in by_name]
        if not selected:
            selected = [item for item in devices if str(item.get("name", "")).startswith("iPhone")][-4:]

        async def signed_versions(device):
            data = await self._get_json(f"{IPSW_API}/device/{device['identifier']}?type=ipsw")
            versions = list(dict.fromkeys(
                item.get("version") for item in data.get("firmwares", [])
                if item.get("signed") and item.get("version")
            ))
            return device, versions

        results = await asyncio.gather(*(signed_versions(device) for device in selected))
        lines = [
            f"**{device['name']}** (`{device['identifier']}`): " + (", ".join(versions[:8]) or "None reported")
            for device, versions in results
        ]
        return discord.Embed(
            title="Currently signed Apple firmware",
            description="\n".join(lines),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        ).set_footer(text="Live result from Apple's firmware signing catalog via IPSW.me")

    @tss.command(name="check", description="Check signing now, or ask about a version and device")
    @app_commands.describe(question="Example: Is iOS 17.1 signed for iPhone 16?")
    async def tss_check(self, interaction: discord.Interaction, question: str = ""):
        await interaction.response.defer()
        if not question.strip():
            try:
                await interaction.followup.send(embed=await self._current_signing_summary())
            except aiohttp.ClientError:
                await interaction.followup.send("I could not reach Apple's firmware signing catalog right now.", ephemeral=True)
            return
        try:
            device, version, status_code, note = await self._resolve_signing(question)
        except (ValueError, aiohttp.ClientError) as error:
            await interaction.followup.send(str(error), ephemeral=True); return
        status, color = {
            "signed": ("Signed", discord.Color.green()), "unsigned": ("Not signed", discord.Color.red()),
            "incompatible": ("Not compatible", discord.Color.orange()),
            "uncertain": ("Needs another check", discord.Color.orange()),
        }[status_code]
        embed = discord.Embed(title=f"{device['name']} · iOS {version}", description=f"**{status}**\n{note}", color=color,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Device identifier", value=device["identifier"])
        await interaction.followup.send(embed=embed)

    @tss.command(name="device", description="Find the identifier GIR uses for an Apple device")
    async def tss_device(self, interaction: discord.Interaction, device: str):
        await interaction.response.defer(ephemeral=True)
        try:
            match = await self._find_device(device)
            await interaction.followup.send(f"**{match['name']}** uses identifier `{match['identifier']}`.", ephemeral=True)
        except (ValueError, aiohttp.ClientError) as error:
            await interaction.followup.send(str(error), ephemeral=True)

    @tss.command(name="versions", description="List currently signed iOS versions for a device")
    async def tss_versions(self, interaction: discord.Interaction, device: str):
        await interaction.response.defer()
        try:
            match = await self._find_device(device)
            data = await self._get_json(f"{IPSW_API}/device/{match['identifier']}?type=ipsw")
            signed = [item.get("version") for item in data.get("firmwares", []) if item.get("signed")]
            versions = list(dict.fromkeys(value for value in signed if value))
            text = ", ".join(versions[:20]) or "No signed iOS versions were reported."
            await interaction.followup.send(f"**TSS signing for {match['name']}**\n{text}")
        except (ValueError, aiohttp.ClientError) as error:
            await interaction.followup.send(str(error), ephemeral=True)

    @tss.command(name="status", description="Check whether GIR's signing checker is ready")
    async def tss_status(self, interaction: discord.Interaction):
        installed = Path(TSSCHECKER).is_file()
        await interaction.response.send_message(
            f"**TSS Checker:** {'Ready' if installed else 'Catalog fallback only'}\n"
            "Checks use temporary external-drive storage and clean it after every request. No IPSW is downloaded.",
            ephemeral=True,
        )

    @tss.command(name="help", description="Show examples of every TSS command")
    async def tss_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**TSS commands**\n"
            "`/tss check question:Is iOS 17.1 signed for iPhone 16?`\n"
            "`/tss device device:iPhone 16`\n"
            "`/tss versions device:iPhone 16`\n"
            "`/tss status`",
            ephemeral=True,
        )

    @apple.command(name="events", description="Open Apple's official event page")
    async def apple_events(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🍎 Apple event schedule and replays: {APPLE_EVENTS}")

    @apple.command(name="latest", description="Show the latest item in Apple's official Newsroom feed")
    async def apple_latest(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "GIR/1.0"}) as session:
                async with session.get(APPLE_RSS, timeout=25) as response:
                    response.raise_for_status(); root = ET.fromstring(await response.text())
            item = root.find("./channel/item")
            await interaction.followup.send(f"🍎 **{item.findtext('title')}**\n{item.findtext('link')}")
        except Exception:
            await interaction.followup.send("I could not reach Apple Newsroom right now.", ephemeral=True)

    @apple.command(name="developer", description="Open Apple's developer event schedule")
    async def apple_developer(self, interaction: discord.Interaction):
        await interaction.response.send_message("Apple developer sessions, labs, and events: https://developer.apple.com/events/")

    def _save_apple_settings(self, enabled: bool, channel_id: int = 0, role_id: int = 0):
        try: current = json.loads(DATA_FILE.read_text())
        except (OSError, ValueError): current = {}
        current["appleEvents"] = {"enabled": enabled, "channelID": channel_id, "roleID": role_id}
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = DATA_FILE.with_suffix(".tmp"); temporary.write_text(json.dumps(current, indent=2) + "\n"); temporary.replace(DATA_FILE)

    @app_commands.default_permissions(manage_guild=True)
    @apple.command(name="subscribe", description="Send Apple event alerts to a channel")
    async def apple_subscribe(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
        self._save_apple_settings(True, channel.id, role.id if role else 0)
        await interaction.response.send_message(f"Apple event alerts will be posted in {channel.mention}.", ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @apple.command(name="unsubscribe", description="Turn off Apple event alerts")
    async def apple_unsubscribe(self, interaction: discord.Interaction):
        self._save_apple_settings(False)
        await interaction.response.send_message("Apple event alerts are off.", ephemeral=True)

    @apple.command(name="status", description="Show Apple event alert settings")
    async def apple_status(self, interaction: discord.Interaction):
        settings = load_settings().get("appleEvents", {})
        channel = interaction.guild.get_channel(int(settings.get("channelID", 0)))
        await interaction.response.send_message(
            f"Apple event alerts are **{'on' if settings.get('enabled') else 'off'}**"
            + (f" in {channel.mention}." if channel else "."), ephemeral=True)

    @tasks.loop(minutes=30)
    async def apple_event_check(self):
        settings = load_settings().get("appleEvents", {})
        if not settings.get("enabled"):
            return
        channel = self.bot.get_channel(int(settings.get("channelID", 0)))
        if not channel:
            return
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "GIR/1.0"}) as session:
                async with session.get(APPLE_RSS, timeout=25) as response:
                    response.raise_for_status(); body = await response.text()
            root = ET.fromstring(body)
            event_items = []
            for item in root.findall("./channel/item"):
                title, link = item.findtext("title", ""), item.findtext("link", "")
                searchable = title + " " + item.findtext("description", "")
                if re.search(r"\b(apple event|wwdc|keynote|worldwide developers conference)\b", searchable, re.I):
                    event_items.append({"title": title, "link": link})
            try: seen = set(json.loads(APPLE_STATE.read_text()).get("seen", []))
            except (OSError, ValueError): seen = set()
            current = {item["link"] for item in event_items if item["link"]}
            APPLE_STATE.parent.mkdir(parents=True, exist_ok=True)
            APPLE_STATE.write_text(json.dumps({"seen": sorted(current), "checkedAt": datetime.now(timezone.utc).isoformat()}))
            new_items = [item for item in event_items if seen and item["link"] not in seen]
            for item in reversed(new_items[:3]):
                role = channel.guild.get_role(int(settings.get("roleID", 0)))
                await channel.send(content=role.mention if role else None,
                    embed=discord.Embed(title=item["title"][:256], description=f"[Read the official Apple announcement]({item['link']})\n\n[Apple Events]({APPLE_EVENTS})", color=discord.Color.red()),
                    allowed_mentions=discord.AllowedMentions(roles=True))
        except Exception:
            logger.exception("Apple event check failed")

    @apple_event_check.before_loop
    async def before_apple_event_check(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ServerSuite(bot))
