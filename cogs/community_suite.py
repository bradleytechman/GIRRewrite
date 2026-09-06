"""Modern community management tools inspired by popular general-purpose bots.

The module is intentionally configured through GIR's private dashboard.  Safety
actions start disabled, and every automatic action can be reviewed in Discord.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import zipfile
from io import BytesIO
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks

from utils import cfg, logger
from community_rules import caps_percent, has_invite, recent_count


DATA_FILE = Path(os.environ.get(
    "GIR_COMMUNITY_FILE",
    str(Path.home() / "Library/Application Support/SowensServer/GIRRuntime/dashboard/data/community.json"),
))
GAME_STATE_FILE = DATA_FILE.with_name("free-games-state.json")
GAME_API = "https://www.gamerpower.com/api/giveaways"

DEFAULTS = {
    "automod": {
        "enabled": False, "reportChannelID": 0, "deleteMessage": True,
        "timeoutHours": 168, "imageSpam": True, "imageThreshold": 4,
        "imageWindowSeconds": 20, "fastSpam": True, "messageThreshold": 7,
        "messageWindowSeconds": 8, "capsSpam": True, "capsPercent": 80,
        "mentionSpam": True, "mentionThreshold": 6, "inviteLinks": False,
        "monitorAllChannels": True, "monitoredChannelIDs": [], "ignoredChannelIDs": [], "ignoredRoleIDs": [],
    },
    "welcome": {"enabled": False, "channelID": 0, "message": "Welcome {mention} to {server}!", "goodbyeEnabled": False, "goodbyeMessage": "{name} left the server."},
    "autorole": {"enabled": False, "roleIDs": []},
    "starboard": {"enabled": False, "channelID": 0, "threshold": 3, "emoji": "⭐"},
    "suggestions": {"enabled": False, "channelID": 0},
    "freeGames": {"enabled": False, "channelID": 0, "platforms": ["pc", "steam", "epic-games-store", "ps4", "ps5", "xbox-one", "xbox-series-xs"], "types": ["game"], "pingRoleID": 0},
    "movieNight": {"enabled": False, "channelID": 0, "pingRoleID": 0},
    "customCommands": [], "autoResponses": [], "reactionRoles": [], "disabledCommands": [],
}


def _merge(base, incoming):
    result = dict(base)
    for key, value in incoming.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def load_settings():
    try:
        return _merge(DEFAULTS, json.loads(DATA_FILE.read_text()))
    except (OSError, ValueError, TypeError):
        return _merge(DEFAULTS, {})


def render(template: str, member: discord.Member) -> str:
    return template.replace("{mention}", member.mention).replace("{name}", member.display_name).replace("{server}", member.guild.name)


class CommunitySuite(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.activity = defaultdict(lambda: deque(maxlen=30))
        self.images = defaultdict(lambda: deque(maxlen=30))
        self.star_posts = {}
        self.afk = {}
        self.free_game_check.start()

    def cog_unload(self):
        self.free_game_check.cancel()

    def settings(self):
        return load_settings()

    async def fetch_free_games(self):
        async with aiohttp.ClientSession(headers={"User-Agent": "GIR Discord Bot"}) as session:
            async with session.get(GAME_API, timeout=20) as response:
                if response.status == 201:
                    return []
                response.raise_for_status()
                return await response.json()

    def filtered_games(self, games, settings):
        platforms = {str(value).lower() for value in settings.get("platforms", [])}
        types = {str(value).lower() for value in settings.get("types", [])}
        return [game for game in games if (not platforms or any(platform in str(game.get("platforms", "")).lower() for platform in platforms))
                and (not types or str(game.get("type", "")).lower() in types)]

    @tasks.loop(minutes=15)
    async def free_game_check(self):
        settings = self.settings()["freeGames"]
        if not settings.get("enabled"):
            return
        channel = self.bot.get_channel(int(settings.get("channelID") or 0))
        if not channel:
            return
        try:
            games = self.filtered_games(await self.fetch_free_games(), settings)
            try:
                seen = set(json.loads(GAME_STATE_FILE.read_text()).get("seen", []))
            except (OSError, ValueError, TypeError):
                seen = {str(game.get("id")) for game in games}
            for game in reversed([item for item in games if str(item.get("id")) not in seen]):
                await self.post_game(channel, game, settings)
            seen.update(str(game.get("id")) for game in games)
            GAME_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = GAME_STATE_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps({"seen": sorted(seen)[-2000:]}) + "\n"); temporary.replace(GAME_STATE_FILE)
        except Exception:
            logger.exception("Free game check failed")

    @free_game_check.before_loop
    async def before_free_game_check(self):
        await self.bot.wait_until_ready()

    async def post_game(self, channel, game, settings):
        title = str(game.get("title", "Free game"))[:256]
        description = str(game.get("description", ""))[:2800]
        url = game.get("open_giveaway_url") or game.get("gamerpower_url") or "https://www.gamerpower.com/"
        embed = discord.Embed(title=title, url=url, description=description, color=discord.Color.green())
        embed.add_field(name="Platforms", value=str(game.get("platforms", "Unknown"))[:1024])
        embed.add_field(name="Normal price", value=str(game.get("worth") or "Unknown"))
        embed.add_field(name="Ends", value=str(game.get("end_date") or "While supplies last"))
        if game.get("image") or game.get("thumbnail"):
            embed.set_image(url=game.get("image") or game.get("thumbnail"))
        embed.add_field(name="Source", value="[Giveaway data from GamerPower](https://www.gamerpower.com/)", inline=False)
        role_id = int(settings.get("pingRoleID") or 0); role = channel.guild.get_role(role_id)
        await channel.send(content=role.mention if role else None, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))

    @staticmethod
    def _staff(member: discord.Member, settings: dict) -> bool:
        ignored = set(settings.get("ignoredRoleIDs", []))
        return member.guild_permissions.manage_messages or any(role.id in ignored for role in member.roles)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = self.settings()
        auto = settings["autorole"]
        if auto.get("enabled"):
            roles = [member.guild.get_role(int(role_id)) for role_id in auto.get("roleIDs", [])]
            roles = [role for role in roles if role and role < member.guild.me.top_role]
            if roles:
                try:
                    await member.add_roles(*roles, reason="GIR automatic member role")
                except discord.HTTPException:
                    logger.exception("Could not assign automatic roles")
        welcome = settings["welcome"]
        if welcome.get("enabled"):
            channel = member.guild.get_channel(int(welcome.get("channelID") or 0))
            if channel:
                await channel.send(render(str(welcome.get("message", "")), member), allowed_mentions=discord.AllowedMentions(users=True))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        welcome = self.settings()["welcome"]
        if welcome.get("goodbyeEnabled"):
            channel = member.guild.get_channel(int(welcome.get("channelID") or 0))
            if channel:
                await channel.send(render(str(welcome.get("goodbyeMessage", "")), member))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or message.guild.id != cfg.guild_id:
            return
        settings = self.settings()
        lowered = message.content.lower().strip()

        if message.author.id in self.afk:
            self.afk.pop(message.author.id, None)
            try:
                await message.channel.send(f"Welcome back, {message.author.mention}. I cleared your AFK status.", delete_after=7)
            except discord.HTTPException:
                pass
        for member in message.mentions:
            if member.id in self.afk:
                await message.channel.send(f"{member.display_name} is AFK: {self.afk[member.id]}", reference=message, mention_author=False)

        for item in settings.get("customCommands", []):
            if item.get("enabled", True) and lowered == str(item.get("trigger", "")).lower().strip():
                await message.channel.send(str(item.get("response", ""))[:2000], reference=message, mention_author=False)
                break
        for item in settings.get("autoResponses", []):
            trigger = str(item.get("trigger", "")).lower().strip()
            if item.get("enabled", True) and trigger and trigger in lowered:
                await message.channel.send(str(item.get("response", ""))[:2000], reference=message, mention_author=False)
                break

        auto = settings["automod"]
        monitored = set(auto.get("monitoredChannelIDs", []))
        channel_ids = {message.channel.id, getattr(message.channel, "parent_id", 0)}
        if (not auto.get("enabled") or self._staff(message.author, auto)
                or channel_ids & set(auto.get("ignoredChannelIDs", []))
                or (not auto.get("monitorAllChannels", True) and not channel_ids & monitored)):
            return
        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        self.activity[key].append(now)
        if message.attachments and any(a.content_type and a.content_type.startswith("image/") for a in message.attachments):
            self.images[key].extend([now] * len(message.attachments))
        findings = []
        window = int(auto.get("imageWindowSeconds", 20))
        image_count = recent_count(self.images[key], now, window)
        if auto.get("imageSpam") and image_count >= int(auto.get("imageThreshold", 4)):
            findings.append(f"{image_count} image attachments in {window} seconds")
        msg_window = int(auto.get("messageWindowSeconds", 8))
        message_count = recent_count(self.activity[key], now, msg_window)
        if auto.get("fastSpam") and message_count >= int(auto.get("messageThreshold", 7)):
            findings.append(f"{message_count} messages in {msg_window} seconds")
        if auto.get("capsSpam") and len(message.content) >= 20 and caps_percent(message.content) >= int(auto.get("capsPercent", 80)):
            findings.append("mostly capital letters")
        if auto.get("mentionSpam") and len(message.mentions) + len(message.role_mentions) >= int(auto.get("mentionThreshold", 6)):
            findings.append("too many mentions")
        if auto.get("inviteLinks") and has_invite(message.content):
            findings.append("Discord invite link")
        if findings:
            await self._moderate(message, auto, findings[0])
            self.activity[key].clear(); self.images[key].clear()

    async def _moderate(self, message: discord.Message, settings: dict, trigger: str):
        if settings.get("deleteMessage"):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        hours = max(0, min(int(settings.get("timeoutHours", 0)), 336))
        action = "Message removed"
        if hours and isinstance(message.author, discord.Member) and message.author < message.guild.me.top_role:
            try:
                await message.author.timeout(datetime.now(timezone.utc) + timedelta(hours=hours), reason=f"GIR AutoMod: {trigger}")
                action = f"Timed out for {hours} hours"
            except discord.HTTPException:
                action = "Message removed; timeout could not be applied"
        report_id = int(settings.get("reportChannelID") or getattr(cfg.channels, "reports", 0))
        channel = message.guild.get_channel(report_id)
        if not channel:
            return
        embed = discord.Embed(title="🚨 Automatic moderation alert", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Member", value=f"{message.author.mention}\n`{message.author.id}`", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Trigger", value=trigger, inline=True)
        embed.add_field(name="Action", value=action, inline=False)
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Ban", style=discord.ButtonStyle.danger, custom_id=f"gir:report:ban:{message.author.id}"))
        view.add_item(discord.ui.Button(label="Dismiss", style=discord.ButtonStyle.secondary, custom_id=f"gir:report:dismiss:{message.author.id}"))
        await channel.send(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        custom_id = (interaction.data or {}).get("custom_id", "") if interaction.type == discord.InteractionType.component else ""
        if not custom_id.startswith("gir:report:"):
            return
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("You need permission to ban members to use this.", ephemeral=True); return
        _, _, action, user_id = custom_id.split(":", 3)
        if action == "ban":
            await interaction.guild.ban(discord.Object(id=int(user_id)), reason=f"Reviewed AutoMod report by {interaction.user}")
            await interaction.response.edit_message(content=f"Banned by {interaction.user.mention}", view=None)
        else:
            await interaction.response.edit_message(content=f"Dismissed by {interaction.user.mention}", view=None)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id != cfg.guild_id or payload.member is None or payload.member.bot:
            return
        settings = self.settings()
        for item in settings.get("reactionRoles", []):
            if item.get("enabled", True) and int(item.get("messageID", 0)) == payload.message_id and str(item.get("emoji")) == str(payload.emoji):
                role = payload.member.guild.get_role(int(item.get("roleID", 0)))
                if role:
                    await payload.member.add_roles(role, reason="GIR reaction role")
        star = settings["starboard"]
        if not star.get("enabled") or str(payload.emoji) != str(star.get("emoji", "⭐")):
            return
        channel = self.bot.get_channel(payload.channel_id)
        destination = self.bot.get_channel(int(star.get("channelID") or 0))
        if not channel or not destination:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        reaction = next((r for r in message.reactions if str(r.emoji) == str(payload.emoji)), None)
        if not reaction or reaction.count < int(star.get("threshold", 3)):
            return
        embed = discord.Embed(description=message.content or "Shared attachment", color=discord.Color.gold(), timestamp=message.created_at)
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Original", value=f"[Open message]({message.jump_url})")
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)
        existing = self.star_posts.get(message.id)
        if existing:
            try:
                post = await destination.fetch_message(existing); await post.edit(content=f"⭐ **{reaction.count}**", embed=embed); return
            except discord.HTTPException:
                pass
        post = await destination.send(content=f"⭐ **{reaction.count}**", embed=embed)
        self.star_posts[message.id] = post.id

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id != cfg.guild_id:
            return
        for item in self.settings().get("reactionRoles", []):
            if item.get("enabled", True) and int(item.get("messageID", 0)) == payload.message_id and str(item.get("emoji")) == str(payload.emoji):
                guild = self.bot.get_guild(payload.guild_id); member = guild.get_member(payload.user_id) if guild else None
                role = guild.get_role(int(item.get("roleID", 0))) if guild else None
                if member and role:
                    await member.remove_roles(role, reason="GIR reaction role removed")

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="suggest", description="Send an idea to the server suggestion board")
    async def suggest(self, interaction: discord.Interaction, idea: str):
        settings = self.settings()["suggestions"]
        channel = interaction.guild.get_channel(int(settings.get("channelID") or 0)) if settings.get("enabled") else None
        if not channel:
            await interaction.response.send_message("Suggestions are not set up yet.", ephemeral=True); return
        embed = discord.Embed(title="New suggestion", description=idea[:4000], color=discord.Color.teal())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        post = await channel.send(embed=embed); await post.add_reaction("👍"); await post.add_reaction("👎")
        await interaction.response.send_message("Your suggestion was posted.", ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="announce", description="Post a clear announcement in a channel")
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str):
        embed = discord.Embed(title=title[:256], description=message[:4000], color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"Posted by {interaction.user.display_name}")
        await channel.send(embed=embed); await interaction.response.send_message("Announcement posted.", ephemeral=True)

    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="slowmode", description="Change how often members can send messages")
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"Changed by {interaction.user}")
        await interaction.response.send_message("Slow mode turned off." if seconds == 0 else f"Members can now send one message every {seconds} seconds.", ephemeral=True)

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="report", description="Privately report a member to the moderation team")
    async def report(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        channel = interaction.guild.get_channel(int(getattr(cfg.channels, "reports", 0)))
        if not channel:
            await interaction.response.send_message("The reports channel is not set up yet.", ephemeral=True); return
        embed = discord.Embed(title="Member report", description=reason[:4000], color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reported member", value=f"{member.mention}\n`{member.id}`")
        embed.add_field(name="Reported by", value=f"{interaction.user.mention}\n`{interaction.user.id}`")
        await channel.send(embed=embed); await interaction.response.send_message("Your report was sent privately to the moderators.", ephemeral=True)

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="afk", description="Let people know you are away")
    async def afk_command(self, interaction: discord.Interaction, reason: str = "Away for a while"):
        self.afk[interaction.user.id] = reason[:200]
        await interaction.response.send_message("Your AFK status is set. It will clear when you send a message.", ephemeral=True)

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction):
        await interaction.response.send_message(random.choice(("Heads 🪙", "Tails 🪙")))

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="roll", description="Roll dice")
    async def roll(self, interaction: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6):
        await interaction.response.send_message(f"🎲 You rolled **{random.randint(1, sides)}** out of {sides}.")

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="choose", description="Let GIR choose from a comma-separated list")
    async def choose(self, interaction: discord.Interaction, choices: str):
        items = [item.strip() for item in choices.split(",") if item.strip()]
        if len(items) < 2:
            await interaction.response.send_message("Give me at least two choices separated by commas.", ephemeral=True); return
        await interaction.response.send_message(f"I choose **{random.choice(items)}**.")

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="membercount", description="Show how many people are in this server")
    async def membercount(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"**{interaction.guild.member_count:,}** members are in {interaction.guild.name}.")

    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="nickname", description="Change or clear a member's nickname")
    async def nickname(self, interaction: discord.Interaction, member: discord.Member, nickname: str | None = None):
        await member.edit(nick=nickname, reason=f"Changed by {interaction.user}")
        await interaction.response.send_message("Nickname updated.", ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="embed", description="Post a simple formatted message")
    async def embed_message(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, color: str = "5865F2"):
        try:
            embed_color = discord.Color(int(color.lstrip("#"), 16))
        except ValueError:
            await interaction.response.send_message("Use a six-character color such as 5865F2.", ephemeral=True); return
        await channel.send(embed=discord.Embed(title=title[:256], description=message[:4000], color=embed_color))
        await interaction.response.send_message("Formatted message posted.", ephemeral=True)

    games = app_commands.Group(name="games", description="Free game alerts and current giveaways", guild_ids=[cfg.guild_id])

    @games.command(name="free", description="List free games and giveaways available now")
    async def games_free(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        settings = self.settings()["freeGames"]
        try:
            games = self.filtered_games(await self.fetch_free_games(), settings)[:10]
        except Exception:
            await interaction.followup.send("I could not reach the free-game list right now.", ephemeral=True); return
        if not games:
            await interaction.followup.send("No matching giveaways are listed right now.", ephemeral=True); return
        lines = [f"• [{game.get('title', 'Free game')}]({game.get('open_giveaway_url') or game.get('gamerpower_url')}) — {game.get('platforms', 'Unknown platform')}" for game in games]
        embed = discord.Embed(title="Free games available now", description="\n".join(lines)[:4000], color=discord.Color.green())
        embed.add_field(name="Source", value="[Giveaway data from GamerPower](https://www.gamerpower.com/)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    movie = app_commands.Group(name="movie", description="Plan a server movie night", guild_ids=[cfg.guild_id])

    @movie.command(name="suggest", description="Suggest a movie and let members vote")
    async def movie_suggest(self, interaction: discord.Interaction, title: str, notes: str = ""):
        settings = self.settings()["movieNight"]
        channel = interaction.guild.get_channel(int(settings.get("channelID") or 0)) if settings.get("enabled") else None
        if not channel:
            await interaction.response.send_message("Movie night is not set up yet.", ephemeral=True); return
        embed = discord.Embed(title=f"🎬 {title[:240]}", description=notes[:3500] or "Would you watch this?", color=discord.Color.purple())
        embed.set_footer(text=f"Suggested by {interaction.user.display_name}")
        post = await channel.send(embed=embed); await post.add_reaction("👍"); await post.add_reaction("👎")
        await interaction.response.send_message("Your movie was added for voting.", ephemeral=True)

    @movie.command(name="poll", description="Create a movie-night vote from comma-separated titles")
    @app_commands.default_permissions(manage_events=True)
    async def movie_poll(self, interaction: discord.Interaction, titles: str, when: str = "Date to be decided"):
        options = [value.strip() for value in titles.split(",") if value.strip()][:10]
        if len(options) < 2:
            await interaction.response.send_message("Add at least two movie titles separated by commas.", ephemeral=True); return
        settings = self.settings()["movieNight"]; channel = interaction.guild.get_channel(int(settings.get("channelID") or 0)) or interaction.channel
        numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        embed = discord.Embed(title="🎬 Movie night vote", description="\n".join(f"{numbers[i]} {title}" for i, title in enumerate(options)), color=discord.Color.purple())
        embed.add_field(name="When", value=when[:1024]); role = interaction.guild.get_role(int(settings.get("pingRoleID") or 0))
        await interaction.response.defer(ephemeral=True); post = await channel.send(content=role.mention if role else None, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
        for emoji in numbers[:len(options)]: await post.add_reaction(emoji)
        await interaction.followup.send("Movie-night vote posted.", ephemeral=True)

    emoji = app_commands.Group(name="emoji", description="Manage server emojis", guild_ids=[cfg.guild_id], default_permissions=discord.Permissions(manage_emojis=True))

    async def image_from_url(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise ValueError("Use an HTTPS image URL.")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as response:
                response.raise_for_status(); data = await response.read()
        if len(data) > 256 * 1024: raise ValueError("Discord emoji images must be smaller than 256 KB.")
        return data

    @emoji.command(name="add", description="Add an emoji from an image URL")
    async def emoji_add(self, interaction: discord.Interaction, name: str, image_url: str):
        await interaction.response.defer(ephemeral=True)
        try: emoji = await interaction.guild.create_custom_emoji(name=name[:32], image=await self.image_from_url(image_url), reason=f"Added by {interaction.user}")
        except Exception as error: await interaction.followup.send(f"I could not add that emoji: {error}", ephemeral=True); return
        await interaction.followup.send(f"Added {emoji}.", ephemeral=True)

    @emoji.command(name="copy", description="Copy a custom emoji into this server")
    async def emoji_copy(self, interaction: discord.Interaction, emoji: str, new_name: str = ""):
        match = re.fullmatch(r"<a?:([A-Za-z0-9_]+):(\d+)>", emoji)
        if not match: await interaction.response.send_message("Paste a custom Discord emoji, such as <:name:123>.", ephemeral=True); return
        extension = "gif" if emoji.startswith("<a:") else "png"; url = f"https://cdn.discordapp.com/emojis/{match.group(2)}.{extension}"
        await interaction.response.defer(ephemeral=True)
        try: created = await interaction.guild.create_custom_emoji(name=(new_name or match.group(1))[:32], image=await self.image_from_url(url), reason=f"Copied by {interaction.user}")
        except Exception as error: await interaction.followup.send(f"I could not copy that emoji: {error}", ephemeral=True); return
        await interaction.followup.send(f"Added {created}.", ephemeral=True)

    @emoji.command(name="delete", description="Delete a server emoji by name")
    async def emoji_delete(self, interaction: discord.Interaction, name: str):
        emoji = discord.utils.get(interaction.guild.emojis, name=name)
        if not emoji: await interaction.response.send_message("I could not find that emoji.", ephemeral=True); return
        await emoji.delete(reason=f"Deleted by {interaction.user}"); await interaction.response.send_message(f"Deleted `{name}`.", ephemeral=True)

    @emoji.command(name="list", description="List this server's custom emojis")
    async def emoji_list(self, interaction: discord.Interaction):
        text = " ".join(str(emoji) for emoji in interaction.guild.emojis) or "This server has no custom emojis."
        await interaction.response.send_message(text[:2000], ephemeral=True)

    @emoji.command(name="stats", description="Show emoji capacity and usage")
    async def emoji_stats(self, interaction: discord.Interaction):
        static = sum(not item.animated for item in interaction.guild.emojis); animated = sum(item.animated for item in interaction.guild.emojis)
        await interaction.response.send_message(f"Static emojis: **{static}**\nAnimated emojis: **{animated}**\nServer emoji limit: **{interaction.guild.emoji_limit}** per type", ephemeral=True)

    @emoji.command(name="export", description="Export server emojis as a ZIP file")
    async def emoji_export(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True); archive = BytesIO()
        async with aiohttp.ClientSession() as session:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for item in interaction.guild.emojis:
                    try:
                        async with session.get(item.url, timeout=15) as response: data = await response.read()
                        bundle.writestr(f"{item.name}.{('gif' if item.animated else 'png')}", data)
                    except Exception: pass
        archive.seek(0); await interaction.followup.send(file=discord.File(archive, filename=f"{interaction.guild.name}-emojis.zip"), ephemeral=True)

    @emoji.command(name="import", description="Import PNG, JPEG, GIF, or WebP images from a ZIP")
    async def emoji_import(self, interaction: discord.Interaction, file: discord.Attachment):
        if file.size > 8 * 1024 * 1024: await interaction.response.send_message("Choose a ZIP smaller than 8 MB.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); added = 0
        try:
            with zipfile.ZipFile(BytesIO(await file.read())) as bundle:
                safe = [info for info in bundle.infolist() if not info.is_dir() and info.file_size <= 256*1024 and info.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))][:50]
                for info in safe:
                    name = re.sub(r"[^A-Za-z0-9_]", "_", Path(info.filename).stem)[:32]
                    if len(name) < 2: continue
                    try: await interaction.guild.create_custom_emoji(name=name, image=bundle.read(info), reason=f"Imported by {interaction.user}"); added += 1
                    except discord.HTTPException: pass
        except (zipfile.BadZipFile, OSError): await interaction.followup.send("That file is not a readable ZIP.", ephemeral=True); return
        await interaction.followup.send(f"Imported **{added}** emojis.", ephemeral=True)

    @emoji.command(name="role-icon", description="Set a role icon from an image URL")
    async def emoji_role_icon(self, interaction: discord.Interaction, role: discord.Role, image_url: str):
        await interaction.response.defer(ephemeral=True)
        try: await role.edit(display_icon=await self.image_from_url(image_url), reason=f"Changed by {interaction.user}")
        except Exception as error: await interaction.followup.send(f"I could not set that role icon: {error}", ephemeral=True); return
        await interaction.followup.send("Role icon updated.", ephemeral=True)

    @emoji.command(name="pfp", description="Make an emoji from a Discord user's profile picture")
    async def emoji_pfp(self, interaction: discord.Interaction, name: str, discord_id: str):
        if not discord_id.isdigit(): await interaction.response.send_message("Enter a numeric Discord user ID.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        try:
            user = await self.bot.fetch_user(int(discord_id)); data = await user.display_avatar.with_size(128).read()
            emoji = await interaction.guild.create_custom_emoji(name=name[:32], image=data, reason=f"Added by {interaction.user}")
        except Exception as error: await interaction.followup.send(f"I could not create that emoji: {error}", ephemeral=True); return
        await interaction.followup.send(f"Added {emoji} from {user}.", ephemeral=True)

    @app_commands.guilds(cfg.guild_id)
    @app_commands.command(name="pfp", description="Get a Discord user's profile picture from their ID")
    async def pfp(self, interaction: discord.Interaction, discord_id: str):
        if not discord_id.isdigit(): await interaction.response.send_message("Enter a numeric Discord user ID.", ephemeral=True); return
        try: user = await self.bot.fetch_user(int(discord_id))
        except discord.NotFound: await interaction.response.send_message("I could not find that Discord user.", ephemeral=True); return
        embed = discord.Embed(title=f"{user}'s profile picture", color=discord.Color.blurple()); embed.set_image(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    sticker = app_commands.Group(name="sticker", description="Manage server stickers", guild_ids=[cfg.guild_id], default_permissions=discord.Permissions(manage_emojis=True))

    @sticker.command(name="list", description="List this server's stickers")
    async def sticker_list(self, interaction: discord.Interaction):
        stickers = await interaction.guild.fetch_stickers(); text = "\n".join(f"• {item.name} — {item.url}" for item in stickers) or "This server has no stickers."
        await interaction.response.send_message(text[:2000], ephemeral=True)

    @sticker.command(name="add", description="Add a sticker from an image URL")
    async def sticker_add(self, interaction: discord.Interaction, name: str, image_url: str, emoji: str, description: str = "Server sticker"):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await self.image_from_url(image_url); extension = Path(image_url.split("?", 1)[0]).suffix.lower() or ".png"
            sticker = await interaction.guild.create_sticker(name=name[:30], description=description[:100], emoji=emoji, file=discord.File(BytesIO(data), filename="sticker" + extension), reason=f"Added by {interaction.user}")
        except Exception as error: await interaction.followup.send(f"I could not add that sticker: {error}", ephemeral=True); return
        await interaction.followup.send(f"Added sticker `{sticker.name}`.", ephemeral=True)

    @sticker.command(name="delete", description="Delete a server sticker by name")
    async def sticker_delete(self, interaction: discord.Interaction, name: str):
        sticker = discord.utils.get(await interaction.guild.fetch_stickers(), name=name)
        if not sticker: await interaction.response.send_message("I could not find that sticker.", ephemeral=True); return
        await sticker.delete(reason=f"Deleted by {interaction.user}"); await interaction.response.send_message(f"Deleted `{name}`.", ephemeral=True)

    @sticker.command(name="export", description="Export server stickers as a ZIP file")
    async def sticker_export(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True); archive = BytesIO(); stickers = await interaction.guild.fetch_stickers()
        async with aiohttp.ClientSession() as session:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for item in stickers:
                    try:
                        async with session.get(item.url, timeout=15) as response: data = await response.read()
                        extension = ".png" if item.format in {discord.StickerFormatType.png, discord.StickerFormatType.apng} else ".json"
                        bundle.writestr(item.name + extension, data)
                    except Exception: pass
        archive.seek(0); await interaction.followup.send(file=discord.File(archive, filename=f"{interaction.guild.name}-stickers.zip"), ephemeral=True)

    @sticker.command(name="import", description="Import PNG or APNG stickers from a ZIP")
    async def sticker_import(self, interaction: discord.Interaction, file: discord.Attachment):
        if file.size > 8 * 1024 * 1024: await interaction.response.send_message("Choose a ZIP smaller than 8 MB.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); added = 0
        try:
            with zipfile.ZipFile(BytesIO(await file.read())) as bundle:
                safe = [info for info in bundle.infolist() if not info.is_dir() and info.file_size <= 512*1024 and info.filename.lower().endswith((".png", ".apng"))][:15]
                for info in safe:
                    name = re.sub(r"[^A-Za-z0-9_ ]", "", Path(info.filename).stem)[:30]
                    if len(name) < 2: continue
                    try:
                        upload = discord.File(BytesIO(bundle.read(info)), filename=Path(info.filename).name)
                        await interaction.guild.create_sticker(name=name, description="Imported by GIR", emoji="⭐", file=upload, reason=f"Imported by {interaction.user}"); added += 1
                    except discord.HTTPException: pass
        except (zipfile.BadZipFile, OSError): await interaction.followup.send("That file is not a readable ZIP.", ephemeral=True); return
        await interaction.followup.send(f"Imported **{added}** stickers.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CommunitySuite(bot))
