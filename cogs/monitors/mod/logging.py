import aiohttp
import discord
from discord.ext import commands
from discord.utils import format_dt

from collections import OrderedDict
from datetime import datetime, timezone
from io import BytesIO
from typing import List, Optional, Union

import discord
from data.services.guild_service import guild_service
from data.services.user_service import user_service
from utils.config import cfg

# Discord's own cache only keeps the last 1000 messages, so deletes of older
# messages (and attachment-only messages) were dropped. Keep a larger snapshot.
MESSAGE_CACHE_LIMIT = 20000
MAX_LOGGED_ATTACHMENT_BYTES = 8 * 1024 * 1024


class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._message_cache = OrderedDict()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Log member join messages, send log to #server-logs
        Parameters
        ----------
        member : discord.Member
            The member that joined
        """
        if cfg.features.disable_member_join_logging:
            return

        if member.guild.id != cfg.guild_id:
            return

        db_user = user_service.get_user(member.id)
        channel = member.guild.get_channel(cfg.channels.private_logs)

        embed = discord.Embed(title="Member joined")
        embed.color = discord.Color.green()
        embed.set_thumbnail(url=member.display_avatar)
        embed.add_field(
            name="User", value=f'{member} ({member.mention})', inline=True)
        embed.add_field(name="Warnpoints",
                        value=db_user.warn_points, inline=True)
        embed.add_field(
            name="Join date", value=f"{format_dt(member.joined_at, style='F')} ({format_dt(member.joined_at, style='R')})", inline=True)
        embed.add_field(name="Created",
                        value=f"{format_dt(member.created_at, style='F')} ({format_dt(member.created_at, style='R')})", inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=member.id)

        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Log member leaves in #server-logs
        Parameters
        ----------
        member : discord.Member
            Member that left
        """

        if member.guild.id != cfg.guild_id:
            return

        channel = member.guild.get_channel(cfg.channels.private_logs)

        async for action in member.guild.audit_logs(limit=1, action=discord.AuditLogAction.kick):
            if action.target.id == member.id:
                await self.on_member_kick(action, channel)
                return

        embed = discord.Embed(title="Member Left")
        embed.color = discord.Color.purple()
        embed.set_thumbnail(url=member.display_avatar)
        embed.add_field(
            name="User", value=f'{member} ({member.mention})', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=member.id)
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, member: discord.User):
        if reaction.message.guild is None:
            return
        if reaction.message.guild.id != cfg.guild_id:
            return
        if member.bot:
            return
        if reaction.message.author.bot:
            return
        if reaction.message.channel.is_news():
            return


        db_guild = guild_service.get_guild()
        webhook = db_guild.emoji_logging_webhook
        if webhook is None:
            channel = member.guild.get_channel(cfg.channels.emoji_logs)
            if channel is None:
                return

            webhook = (await channel.create_webhook(name=f"Webhook {channel.name}")).url
            db_guild.emoji_logging_webhook = webhook
            db_guild.save()

        content = f"{reaction.emoji}\n\n{reaction.message.channel.mention} | [Link to message]({reaction.message.jump_url}) | **{member.id}**"
        body = {
            "username": str(member),
            "avatar_url": member.display_avatar,
            "content": content
        }

        async with aiohttp.ClientSession() as session:
            the_webhook: discord.Webhook = discord.Webhook.from_url(
                webhook, session=session)
            # send message to webhook
            await the_webhook.send(**body, allowed_mentions=discord.AllowedMentions(users=False, everyone=False, roles=False))

    def _remember_message(self, message: discord.Message) -> None:
        if message is None or message.guild is None or message.guild.id != cfg.guild_id:
            return
        if message.author is None or message.author.bot:
            return

        self._message_cache[message.id] = {
            "author_id": message.author.id,
            "author_name": str(message.author),
            "author_mention": message.author.mention,
            "author_avatar": str(message.author.display_avatar),
            "content": message.content or "",
            "channel_id": message.channel.id,
            "jump_url": message.jump_url,
            "created_at": message.created_at,
            "attachments": [
                {
                    "filename": attachment.filename,
                    "url": attachment.url,
                    "proxy_url": attachment.proxy_url,
                    "size": attachment.size,
                    "spoiler": attachment.is_spoiler(),
                }
                for attachment in message.attachments
            ],
            "stickers": [sticker.name for sticker in message.stickers],
        }
        self._message_cache.move_to_end(message.id)
        while len(self._message_cache) > MESSAGE_CACHE_LIMIT:
            self._message_cache.popitem(last=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        self._remember_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Log message edits with before and after content
        Parameters
        ----------
        before : discord.Message
            Before edit message data
        after : discord.Message
            Aftere edit message data
        """

        self._remember_message(after)

        if not before.guild:
            return
        if before.guild.id != cfg.guild_id:
            return
        if not before.content or not after.content or before.content == after.content:
            return

        db_guild = guild_service.get_guild()
        if before.channel.id in db_guild.logging_excluded_channels:
            return

        channel = before.guild.get_channel(cfg.channels.private_logs)

        embed = discord.Embed(title="Message Updated")
        embed.color = discord.Color.orange()
        embed.set_thumbnail(url=before.author.display_avatar)
        embed.add_field(
            name="User", value=f'{before.author} ({before.author.mention})', inline=False)

        before_content = before.content
        if len(before.content) > 400:
            before_content = before_content[0:400] + "..."

        after_content = after.content
        if len(after.content) > 400:
            after_content = after_content[0:400] + "..."

        embed.add_field(name="Old message", value=before_content, inline=False)
        embed.add_field(name="New message", value=after_content, inline=False)
        embed.add_field(
            name="Channel", value=before.channel.mention + f"\n\n[Link to message]({before.jump_url})", inline=False)
        embed.timestamp = datetime.now()
        embed.set_footer(text=before.author.id)
        await channel.send(embed=embed)

    def _deleted_message_text(self, content: str, attachments, stickers, jump_url: Optional[str]) -> str:
        parts = []
        text = (content or "").strip()
        if text:
            parts.append(text if len(text) <= 500 else text[:500] + "...")

        if attachments:
            lines = []
            for attachment in attachments:
                if isinstance(attachment, dict):
                    filename = attachment.get("filename") or "attachment"
                    url = attachment.get("url") or ""
                else:
                    filename = attachment.filename
                    url = attachment.url
                lines.append(f"[{filename}]({url})" if url else filename)
            parts.append("**Attachments**\n" + "\n".join(lines))

        if stickers:
            parts.append("**Stickers**\n" + ", ".join(stickers))

        body = "\n\n".join(parts) if parts else "*No text content*"
        link = f"\n\n[Link to message]({jump_url})" if jump_url else ""
        # Embed field values cannot be longer than 1024 characters.
        limit = 1024 - len(link)
        if len(body) > limit:
            body = body[:max(limit - 3, 0)] + "..."
        return body + link

    async def _files_from_message(self, message: discord.Message) -> List[discord.File]:
        files = []
        for attachment in message.attachments:
            if len(files) >= 10:
                break
            if attachment.size and attachment.size > MAX_LOGGED_ATTACHMENT_BYTES:
                continue
            saved = None
            for use_cached in (True, False):
                try:
                    saved = await attachment.to_file(use_cached=use_cached, spoiler=attachment.is_spoiler())
                    break
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    continue
            if saved is not None:
                files.append(saved)
        return files

    async def _files_from_snapshots(self, attachments) -> List[discord.File]:
        files = []
        if not attachments:
            return files

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attachment in attachments:
                if len(files) >= 10:
                    break
                size = attachment.get("size") or 0
                if size > MAX_LOGGED_ATTACHMENT_BYTES:
                    continue
                data = None
                for url in (attachment.get("proxy_url"), attachment.get("url")):
                    if not url:
                        continue
                    try:
                        async with session.get(url) as response:
                            if response.status != 200:
                                continue
                            data = await response.read()
                            break
                    except (aiohttp.ClientError, TimeoutError):
                        continue
                if not data or len(data) > MAX_LOGGED_ATTACHMENT_BYTES:
                    continue
                filename = attachment.get("filename") or "attachment"
                files.append(discord.File(
                    BytesIO(data),
                    filename=filename,
                    spoiler=bool(attachment.get("spoiler")),
                ))
        return files

    async def _delete_audit_entry(self, guild: discord.Guild, channel_id: int, author_id: Optional[int]):
        """Return the audit entry for a recent moderator delete in this channel, if any."""
        try:
            now = datetime.now(timezone.utc)
            async for action in guild.audit_logs(limit=6, action=discord.AuditLogAction.message_delete):
                created = action.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if (now - created).total_seconds() > 15:
                    return None
                extra_channel = getattr(action.extra, "channel", None)
                if extra_channel is not None and getattr(extra_channel, "id", None) != channel_id:
                    continue
                if author_id is not None and getattr(action.target, "id", None) != author_id:
                    continue
                return action
        except (discord.HTTPException, discord.Forbidden):
            return None
        return None

    async def _send_delete_embed(self, channel: discord.TextChannel, embed: discord.Embed, files: List[discord.File]) -> None:
        try:
            if files:
                await channel.send(embed=embed, files=files)
                return
        except discord.HTTPException:
            pass
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Log message deletes, including attachment-only and uncached messages.
        Parameters
        ----------
        payload : discord.RawMessageDeleteEvent
            Delete event. cached_message is only set when Discord.py still has it.
        """

        if payload.guild_id != cfg.guild_id:
            return

        message = payload.cached_message
        snapshot = self._message_cache.pop(payload.message_id, None)
        if message is not None and message.author.bot:
            return

        guild = message.guild if message is not None and message.guild is not None else self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        if message is not None:
            channel_id = message.channel.id
            author_id = message.author.id
            author_name = str(message.author)
            author_mention = message.author.mention
            author_avatar = str(message.author.display_avatar)
            content = message.content or ""
            attachments = message.attachments
            stickers = [sticker.name for sticker in message.stickers]
            jump_url = message.jump_url
            channel_value = message.channel.mention
        elif snapshot is not None:
            channel_id = snapshot["channel_id"]
            author_id = snapshot["author_id"]
            author_name = snapshot["author_name"]
            author_mention = snapshot["author_mention"]
            author_avatar = snapshot["author_avatar"]
            content = snapshot["content"]
            attachments = snapshot["attachments"]
            stickers = snapshot["stickers"]
            jump_url = snapshot["jump_url"]
            source = guild.get_channel(channel_id)
            channel_value = source.mention if source is not None else f"<#{channel_id}>"
        else:
            channel_id = payload.channel_id
            author_id = None
            author_name = None
            author_mention = None
            author_avatar = None
            content = ""
            attachments = []
            stickers = []
            jump_url = None
            source = guild.get_channel(channel_id)
            channel_value = source.mention if source is not None else f"<#{channel_id}>"

        db_guild = guild_service.get_guild()
        if channel_id in db_guild.logging_excluded_channels:
            return

        log_channel = guild.get_channel(cfg.channels.private_logs)
        if log_channel is None:
            return

        audit_entry = await self._delete_audit_entry(guild, channel_id, author_id)
        if author_id is None and audit_entry is not None and audit_entry.target is not None:
            author_id = audit_entry.target.id
            author_name = str(audit_entry.target)
            author_mention = audit_entry.target.mention

        embed = discord.Embed(title="Message Deleted")
        embed.color = discord.Color.red()
        if author_avatar:
            embed.set_thumbnail(url=author_avatar)
        if author_name is not None:
            embed.add_field(name="User", value=f"{author_name} ({author_mention})", inline=True)
        embed.add_field(name="Channel", value=channel_value, inline=True)
        if audit_entry is not None and audit_entry.user is not None:
            embed.add_field(
                name="Deleted by",
                value=f"{audit_entry.user} ({audit_entry.user.mention})",
                inline=True,
            )

        if message is None and snapshot is None:
            message_value = f"Content was not available (message ID `{payload.message_id}`)."
        else:
            message_value = self._deleted_message_text(content, attachments, stickers, jump_url)
        embed.add_field(name="Message", value=message_value, inline=False)
        embed.set_footer(text=str(author_id or payload.message_id))
        embed.timestamp = datetime.now()

        files = []
        try:
            if message is not None:
                files = await self._files_from_message(message)
            elif snapshot is not None:
                files = await self._files_from_snapshots(snapshot["attachments"])
        except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
            files = []

        await self._send_delete_embed(log_channel, embed, files)

    # @commands.Cog.listener()
    # async def on_command_error(self, ctx: GIRContext, error):
    #     if isinstance(error, commands.CommandNotFound):
    #         return

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Log bulk message deletes. Messages are outputted to file and sent to #server-logs.

        Uses the raw event so messages that have fallen out of Discord's cache are still included
        when this cog has seen them, including ones that were only attachments.
        """

        if payload.guild_id != cfg.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        db_guild = guild_service.get_guild()
        if payload.channel_id in db_guild.logging_excluded_channels:
            return

        channel = guild.get_channel(cfg.channels.private_logs)
        if channel is None:
            return

        source = guild.get_channel(payload.channel_id)
        channel_value = source.mention if source is not None else f"<#{payload.channel_id}>"
        cached_by_id = {message.id: message for message in payload.cached_messages}

        members = []
        seen_members = set()
        output = BytesIO()
        for message_id in sorted(payload.message_ids):
            message = cached_by_id.get(message_id)
            snapshot = self._message_cache.pop(message_id, None)

            if message is not None:
                author_name = str(message.author)
                author_id = message.author.id
                author_mention = message.author.mention
                created = message.created_at.strftime("%B %d, %Y, %I:%M %p")
                content = message.content or ""
                attachment_urls = [attachment.url for attachment in message.attachments]
                stickers = [sticker.name for sticker in message.stickers]
            elif snapshot is not None:
                author_name = snapshot["author_name"]
                author_id = snapshot["author_id"]
                author_mention = snapshot["author_mention"]
                created = snapshot["created_at"].strftime("%B %d, %Y, %I:%M %p")
                content = snapshot["content"] or ""
                attachment_urls = [attachment["url"] for attachment in snapshot["attachments"] if attachment.get("url")]
                stickers = snapshot["stickers"]
            else:
                author_name = "Unknown"
                author_id = message_id
                author_mention = None
                created = "unknown time"
                content = "(content was not cached)"
                attachment_urls = []
                stickers = []

            if author_mention is not None and author_id not in seen_members:
                seen_members.add(author_id)
                members.append(author_mention)

            string = f'{author_name} ({author_id}) [{created}] UTC\n'
            string += content
            for url in attachment_urls:
                string += f'\n{url}'
            if stickers:
                string += "\nStickers: " + ", ".join(stickers)
            string += "\n\n"
            output.write(string.encode('UTF-8'))
        output.seek(0)

        member_string = ""
        for i, member in enumerate(members):
            if i == len(members) - 1 and i == 0:
                member_string += f"{member}"
            elif i == len(members) - 1 and i != 0:
                member_string += f"and {member}"
            else:
                member_string += f"{member}, "

        embed = discord.Embed(title="Bulk Message Deleted")
        embed.color = discord.Color.red()
        users_value = f"This batch included {len(payload.message_ids)} messages"
        if member_string:
            users_value += f" from {member_string}"
        if len(users_value) > 1024:
            users_value = users_value[:1021] + "..."
        embed.add_field(name="Users", value=users_value, inline=True)
        embed.add_field(name="Channel", value=channel_value, inline=True)
        embed.timestamp = datetime.now()
        await channel.send(embed=embed)
        await channel.send(file=discord.File(output, 'message.txt'))

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user: Union[discord.User, discord.Member]):
        if not guild.id == cfg.guild_id:
            return

        channel = guild.get_channel(cfg.channels.private_logs)

        embed = discord.Embed(title="Member Banned")
        embed.color = discord.Color.red()
        embed.add_field(
            name="User", value=f'{user} ({user.mention})', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=user.id)

        async for action in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            if action.target.id == user.id:
                embed.title = "Member Left"
                embed.color = discord.Color.purple()
                embed.add_field(
                    name="Banned by", value=f'{action.user} ({action.user.mention})', inline=True)
                await channel.send(embed=embed)
                return

        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user: discord.User):
        if not guild.id == cfg.guild_id:
            return

        channel = guild.get_channel(cfg.channels.private_logs)

        embed = discord.Embed(title="User Unbanned")
        embed.color = discord.Color.yellow()
        embed.add_field(
            name="User", value=f'{user} ({user.mention})', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=user.id)

        async for action in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
            if action.target.id == user.id:
                embed.add_field(
                    name="Unbanned by", value=f'{action.user} ({action.user.mention})', inline=True)
                await channel.send(embed=embed)
                return

        await channel.send(embed=embed)

    async def on_member_kick(self, action: discord.AuditLogEntry, channel: discord.TextChannel):
        embed = discord.Embed(title="Member Left")
        embed.color = discord.Color.purple()
        embed.add_field(
            name="User", value=f'{action.target} ({action.target.mention})', inline=True)
        embed.add_field(
            name="Kicked by", value=f'{action.user} ({action.user.mention})', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=action.user.id)
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if before.name == after.name and before.discriminator == after.discriminator:
            return

        guild = self.bot.get_guild(cfg.guild_id)
        channel = guild.get_channel(cfg.channels.private_logs)

        embed = discord.Embed(title="Username Updated")
        embed.color = discord.Color.magenta()
        embed.add_field(
            name="Before", value=f'{before} ({before.mention})', inline=True)
        embed.add_field(
            name="After", value=f'{after} ({after.mention})', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=before.id)
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.guild.id != cfg.guild_id:
            return
        if not before or not after:
            return
        if before.display_name != after.display_name:
            await self.member_nick_update(before, after)
            return
        if before.timed_out_until != after.timed_out_until:
            await self.member_timeout_update(before, after)
            return

        new_roles = [role.mention
                     for role in after.roles if role not in before.roles]
        if new_roles:
            if cfg.features.disable_role_add_logging_for_recent_joiners:
                # if the user joined in the past 5 minutes, don't log
                # this is for high load situations
                if (datetime.now(timezone.utc) - after.joined_at).total_seconds() < 300:
                    return

            await self.member_roles_update(member=after, roles=new_roles, added=True)
            return

        removed_roles = [role.mention
                         for role in before.roles if role not in after.roles]
        if removed_roles:
            await self.member_roles_update(member=after, roles=removed_roles, added=False)
            return

    async def member_nick_update(self, before, after):
        embed = discord.Embed(title="Member Renamed")
        embed.color = discord.Color.orange()
        embed.set_thumbnail(url=after.display_avatar)
        embed.add_field(
            name="Member", value=f'{after} ({after.mention})', inline=False)
        embed.add_field(
            name="Old nickname", value=f'{before.display_name}', inline=True)
        embed.add_field(
            name="New nickname", value=f'{after.display_name}', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=after.id)

        private = after.guild.get_channel(cfg.channels.private_logs)
        if private:
            await private.send(embed=embed)

    async def member_roles_update(self, member, roles, added):
        embed = discord.Embed()
        if added:
            embed.title = "Member Role Added"
            embed.color = discord.Color.blue()
        else:
            embed.title = "Member Role Removed"
            embed.color = discord.Color.red()

        embed.set_thumbnail(url=member.display_avatar)
        embed.add_field(
            name="Member", value=f'{member} ({member.mention})', inline=True)
        embed.add_field(
            name="Role difference", value=', '.join(roles), inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=member.id)

        async for action in member.guild.audit_logs(limit=1, action=discord.AuditLogAction.member_role_update):
            if action.target.id == member.id:
                embed.add_field(
                    name="Updated by", value=f'{action.user} ({action.user.mention})', inline=False)

        private = member.guild.get_channel(cfg.channels.private_logs)
        if private:
            await private.send(embed=embed)

    async def member_timeout_update(self, before: discord.Member, after: discord.Member):
        embed = discord.Embed()
        if before.timed_out_until is not None and after.timed_out_until is None:
            embed.title = "Member Timeout Removed"
            embed.color = discord.Color.dark_blue()
        elif (before.timed_out_until is None and after.timed_out_until is not None) or (before.timed_out_until < after.timed_out_until):
            embed.title = "Member Timed Out"
            embed.color = discord.Color.greyple()
        else:
            embed.title = "Member Timeout Removed"
            embed.color = discord.Color.dark_blue()

        member = after
        embed.set_thumbnail(url=member.display_avatar)
        embed.add_field(
            name="Member", value=f'{member} ({member.mention})', inline=True)
        embed.timestamp = datetime.now()
        embed.set_footer(text=member.id)
        private = member.guild.get_channel(cfg.channels.private_logs)
        if private:
            await private.send(embed=embed)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.guild.id != cfg.guild_id:
            return
        if interaction.type != discord.InteractionType.application_command:
            return
        data = interaction.data
        if data.get("type") != 1:
            return

        options = interaction.data.get("options") or []

        message_content = ""
        for option in options:
            if option.get("type") == 1:
                message_content += f"{option.get('name')} "
                for sub_option in option.get("options"):
                    message_content += f"{sub_option.get('name')}: {sub_option.get('value')} "
            else:
                message_content += f"{option.get('name')}: {option.get('value')} "

        private = interaction.guild.get_channel(cfg.channels.private_logs)

        embed = discord.Embed(title="Member Used Command",
                              color=discord.Color.dark_teal())
        embed.set_thumbnail(url=interaction.user.display_avatar)
        embed.add_field(
            name="Member", value=f'{interaction.user} ({interaction.user.mention})', inline=True)
        if interaction.channel is not None:
            embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
        embed.add_field(
            name="Command", value=f'`/{data.get("name")}{" " + message_content.strip() if message_content else ""}`', inline=False)
        embed.timestamp = datetime.now()
        embed.set_footer(text=interaction.user.id)

        if private is not None:
            await private.send(embed=embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.guild.id != cfg.guild_id:
            return

        embed = discord.Embed()
        embed.color = discord.Color.dark_blue()
        embed.set_thumbnail(url=member.display_avatar)
        embed.add_field(
            name="Member", value=f'{member} ({member.mention})', inline=False)

        if before.channel is None and member in after.channel.members:
            embed.title = "Member VC Joined"
            embed.add_field(name="Joined", value=after.channel.mention, inline=True)
        elif before.channel is not None and after.channel is None:
            embed.title = "Member VC Left"
            embed.add_field(name="Left", value=before.channel.mention, inline=True)
        elif before.channel.id != after.channel.id:
            embed.title = "Member VC Moved"
            embed.add_field(name="Left", value=before.channel.mention, inline=True)
            embed.add_field(name="Joined", value=after.channel.mention, inline=True)
        else:
            return

        embed.timestamp = datetime.now()
        embed.set_footer(text=member.id)

        private = member.guild.get_channel(cfg.channels.private_logs)
        if private:
            await private.send(embed=embed)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return


async def setup(bot):
    await bot.add_cog(Logging(bot))
