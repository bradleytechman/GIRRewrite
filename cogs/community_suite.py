"""Modern community management tools inspired by popular general-purpose bots.

The module is intentionally configured through GIR's private dashboard.  Safety
actions start disabled, and every automatic action can be reviewed in Discord.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils import cfg, logger
from community_rules import caps_percent, has_invite, recent_count


DATA_FILE = Path(os.environ.get(
    "GIR_COMMUNITY_FILE",
    str(Path.home() / "Library/Application Support/SowensServer/GIRRuntime/dashboard/data/community.json"),
))

DEFAULTS = {
    "automod": {
        "enabled": False, "reportChannelID": 0, "deleteMessage": True,
        "timeoutHours": 168, "imageSpam": True, "imageThreshold": 4,
        "imageWindowSeconds": 20, "fastSpam": True, "messageThreshold": 7,
        "messageWindowSeconds": 8, "capsSpam": True, "capsPercent": 80,
        "mentionSpam": True, "mentionThreshold": 6, "inviteLinks": False,
        "ignoredChannelIDs": [], "ignoredRoleIDs": [],
    },
    "welcome": {"enabled": False, "channelID": 0, "message": "Welcome {mention} to {server}!", "goodbyeEnabled": False, "goodbyeMessage": "{name} left the server."},
    "autorole": {"enabled": False, "roleIDs": []},
    "starboard": {"enabled": False, "channelID": 0, "threshold": 3, "emoji": "⭐"},
    "suggestions": {"enabled": False, "channelID": 0},
    "customCommands": [], "autoResponses": [], "reactionRoles": [],
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

    def settings(self):
        return load_settings()

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
        if not auto.get("enabled") or self._staff(message.author, auto) or message.channel.id in set(auto.get("ignoredChannelIDs", [])):
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


async def setup(bot):
    await bot.add_cog(CommunitySuite(bot))
