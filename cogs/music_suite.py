"""Music discovery, universal links, and Last.fm activity commands."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils import cfg, logger


SONG_LINK_API = "https://api.song.link/v1-alpha.1/links"
ITUNES_SEARCH_API = "https://itunes.apple.com/search"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
COMMUNITY_FILE = Path(os.environ.get(
    "GIR_COMMUNITY_FILE",
    str(Path.home() / "Library/Application Support/SowensServer/GIRRuntime/dashboard/data/community.json"),
))
MUSIC_USERS_FILE = COMMUNITY_FILE.with_name("music-users.json")
SUPPORTED_LINK = re.compile(
    r"https?://(?:open\.spotify\.com|spotify\.link|music\.apple\.com|youtu\.be|(?:www\.)?youtube\.com|tidal\.com|deezer\.com|soundcloud\.com)/\S+",
    re.IGNORECASE,
)
PLATFORMS = (
    ("spotify", "Spotify"), ("appleMusic", "Apple Music"), ("youtube", "YouTube"),
    ("youtubeMusic", "YouTube Music"), ("tidal", "TIDAL"), ("deezer", "Deezer"),
    ("amazonMusic", "Amazon Music"), ("soundcloud", "SoundCloud"),
)
PERIODS = {
    "week": "7day", "month": "1month", "three-months": "3month",
    "six-months": "6month", "year": "12month", "all-time": "overall",
}


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return fallback


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _music_settings() -> dict:
    defaults = {
        "enabled": True, "autoConvertLinks": True, "allowDotFm": True,
        "monitoredChannelIDs": [], "lastfmEnabled": True,
    }
    current = _read_json(COMMUNITY_FILE, {}).get("music", {})
    defaults.update(current if isinstance(current, dict) else {})
    return defaults


def _clean(value: object, limit: int = 250) -> str:
    return discord.utils.escape_markdown(str(value or "Unknown")[:limit])


def _number(value: object) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


class MusicSuite(commands.Cog):
    music = app_commands.Group(
        name="music", description="Find songs and open them in your preferred music service",
        guild_ids=[cfg.guild_id],
    )
    fm = app_commands.Group(
        name="fm", description="View Last.fm listening activity and profiles",
        guild_ids=[cfg.guild_id],
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._cooldown = commands.CooldownMapping.from_cooldown(4, 30.0, commands.BucketType.user)

    async def cog_load(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "GIR-Music/1.0"})

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    async def _json(self, url: str, *, params: dict | None = None) -> dict:
        assert self._session is not None
        async with self._session.get(url, params=params) as response:
            if response.status == 429:
                raise RuntimeError("The music service is busy. Try again in a moment.")
            response.raise_for_status()
            return await response.json(content_type=None)

    async def _song_links(self, url: str) -> dict:
        if not _music_settings().get("enabled", True):
            raise RuntimeError("Music tools are turned off for this server.")
        return await self._json(SONG_LINK_API, params={"url": url})

    async def _search_song(self, query: str) -> str | None:
        data = await self._json(ITUNES_SEARCH_API, params={"term": query, "entity": "song", "limit": "1"})
        rows = data.get("results") or []
        return rows[0].get("trackViewUrl") if rows else None

    @staticmethod
    def _song_embed(data: dict) -> tuple[discord.Embed, discord.ui.View]:
        entities = data.get("entitiesByUniqueId") or {}
        entity = entities.get(data.get("entityUniqueId")) or next(iter(entities.values()), {})
        title = _clean(entity.get("title"), 180)
        artist = _clean(entity.get("artistName"), 180)
        embed = discord.Embed(title=title, description=f"by **{artist}**", color=0x79D956)
        thumbnail = entity.get("thumbnailUrl")
        if thumbnail and str(thumbnail).startswith("https://"):
            embed.set_thumbnail(url=thumbnail)
        embed.set_footer(text="Choose a service to listen")
        view = discord.ui.View(timeout=180)
        links = data.get("linksByPlatform") or {}
        for index, (key, label) in enumerate(PLATFORMS):
            url = (links.get(key) or {}).get("url")
            if url:
                view.add_item(discord.ui.Button(
                    label=label, url=url, style=discord.ButtonStyle.link, row=index // 5))
        return embed, view

    async def _send_song(self, destination, url: str, *, ephemeral: bool = False):
        try:
            data = await self._song_links(url)
            embed, view = self._song_embed(data)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as error:
            logger.warning("Music link lookup failed: %s", error)
            if isinstance(destination, discord.Interaction):
                await destination.followup.send("I could not match that song across services right now.", ephemeral=True)
            return
        if isinstance(destination, discord.Interaction):
            await destination.followup.send(embed=embed, view=view, ephemeral=ephemeral)
        else:
            await destination.reply(embed=embed, view=view, mention_author=False)

    @music.command(name="link", description="Turn a song link into buttons for other music services")
    async def music_link(self, interaction: discord.Interaction, url: str):
        if not SUPPORTED_LINK.search(url):
            await interaction.response.send_message("Paste a Spotify, Apple Music, YouTube, TIDAL, Deezer, or SoundCloud song link.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._send_song(interaction, url)

    @music.command(name="search", description="Find a song and get links for popular music services")
    async def music_search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        if not _music_settings().get("enabled", True):
            await interaction.followup.send("Music tools are turned off for this server.", ephemeral=True)
            return
        try:
            url = await self._search_song(query[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            url = None
        if not url:
            await interaction.followup.send("I could not find a song for that search.", ephemeral=True)
            return
        await self._send_song(interaction, url)

    def _lastfm_username(self, discord_id: int, supplied: str | None = None) -> str | None:
        if supplied:
            return supplied.strip()[:64]
        return _read_json(MUSIC_USERS_FILE, {}).get(str(discord_id))

    async def _lastfm(self, method: str, **params) -> dict:
        settings = _music_settings()
        if not settings.get("enabled", True) or not settings.get("lastfmEnabled", True):
            raise RuntimeError("Last.fm commands are turned off for this server.")
        api_key = getattr(cfg, "lastfm_api_key", None)
        if not api_key:
            raise RuntimeError("Last.fm needs its free API key added by the bot owner.")
        payload = {"method": method, "api_key": api_key, "format": "json", **params}
        data = await self._json(LASTFM_API, params=payload)
        if data.get("error"):
            raise RuntimeError(str(data.get("message") or "Last.fm rejected that request."))
        return data

    async def _fm_error(self, interaction: discord.Interaction, error: Exception):
        message = str(error) if isinstance(error, RuntimeError) else "Last.fm did not answer in time."
        await interaction.followup.send(message[:500], ephemeral=True)

    @fm.command(name="set", description="Connect your Discord profile to a Last.fm username")
    async def fm_set(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        username = username.strip()[:64]
        try:
            data = await self._lastfm("user.getInfo", user=username)
        except Exception as error:
            await self._fm_error(interaction, error); return
        users = _read_json(MUSIC_USERS_FILE, {})
        users[str(interaction.user.id)] = str(data["user"]["name"])
        _write_json(MUSIC_USERS_FILE, users)
        await interaction.followup.send(f"Connected your profile to **{_clean(data['user']['name'])}**.", ephemeral=True)

    @fm.command(name="remove", description="Remove your saved Last.fm username")
    async def fm_remove(self, interaction: discord.Interaction):
        users = _read_json(MUSIC_USERS_FILE, {})
        users.pop(str(interaction.user.id), None)
        _write_json(MUSIC_USERS_FILE, users)
        await interaction.response.send_message("Removed your saved Last.fm username.", ephemeral=True)

    @fm.command(name="now", description="Show your latest or currently playing Last.fm track")
    async def fm_now(self, interaction: discord.Interaction, username: str | None = None):
        await interaction.response.defer()
        user = self._lastfm_username(interaction.user.id, username)
        if not user:
            await interaction.followup.send("Use `/fm set` first, or provide a Last.fm username.", ephemeral=True); return
        try:
            data = await self._lastfm("user.getRecentTracks", user=user, limit=1, extended=1)
            tracks = (data.get("recenttracks") or {}).get("track") or []
            if not tracks:
                raise RuntimeError("That profile has no recent tracks.")
            track = tracks[0]
        except Exception as error:
            await self._fm_error(interaction, error); return
        artist = track.get("artist") or {}
        artist_name = artist.get("name") if isinstance(artist, dict) else artist
        now = (track.get("@attr") or {}).get("nowplaying") == "true"
        embed = discord.Embed(title=_clean(track.get("name"), 180), description=f"by **{_clean(artist_name)}**", color=0xD92323)
        embed.set_author(name=f"{user} · {'Now playing' if now else 'Last played'}", url=f"https://www.last.fm/user/{quote(user)}")
        album = track.get("album") or {}
        if isinstance(album, dict) and album.get("#text"):
            embed.add_field(name="Album", value=_clean(album["#text"]), inline=True)
        images = track.get("image") or []
        image = next((item.get("#text") for item in reversed(images) if item.get("#text")), None)
        if image:
            embed.set_thumbnail(url=image)
        view = discord.ui.View(timeout=180)
        if track.get("url"):
            view.add_item(discord.ui.Button(label="Last.fm", url=track["url"], style=discord.ButtonStyle.link))
        await interaction.followup.send(embed=embed, view=view)

    @fm.command(name="recent", description="Show recent tracks from a Last.fm profile")
    async def fm_recent(self, interaction: discord.Interaction, username: str | None = None):
        await interaction.response.defer()
        user = self._lastfm_username(interaction.user.id, username)
        if not user:
            await interaction.followup.send("Use `/fm set` first, or provide a Last.fm username.", ephemeral=True); return
        try:
            data = await self._lastfm("user.getRecentTracks", user=user, limit=8)
            tracks = (data.get("recenttracks") or {}).get("track") or []
        except Exception as error:
            await self._fm_error(interaction, error); return
        lines = []
        for track in tracks[:8]:
            artist = track.get("artist") or {}
            artist_name = artist.get("#text") if isinstance(artist, dict) else artist
            lines.append(f"**{_clean(track.get('name'), 100)}** — {_clean(artist_name, 100)}")
        embed = discord.Embed(title=f"{_clean(user)} · recent tracks", description="\n".join(lines) or "No recent tracks.", color=0xD92323)
        await interaction.followup.send(embed=embed)

    @fm.command(name="top", description="Show top Last.fm artists for a time period")
    @app_commands.choices(period=[app_commands.Choice(name=name.replace("-", " ").title(), value=value) for name, value in PERIODS.items()])
    async def fm_top(self, interaction: discord.Interaction, period: app_commands.Choice[str], username: str | None = None):
        await interaction.response.defer()
        user = self._lastfm_username(interaction.user.id, username)
        if not user:
            await interaction.followup.send("Use `/fm set` first, or provide a Last.fm username.", ephemeral=True); return
        try:
            data = await self._lastfm("user.getTopArtists", user=user, period=period.value, limit=10)
            artists = (data.get("topartists") or {}).get("artist") or []
        except Exception as error:
            await self._fm_error(interaction, error); return
        lines = [f"`{index:>2}` **{_clean(item.get('name'), 100)}** · {_number(item.get('playcount'))} plays" for index, item in enumerate(artists[:10], 1)]
        embed = discord.Embed(title=f"{_clean(user)} · top artists", description="\n".join(lines) or "No listening history for that period.", color=0xD92323)
        embed.set_footer(text=period.name)
        await interaction.followup.send(embed=embed)

    @fm.command(name="profile", description="Show a Last.fm profile summary")
    async def fm_profile(self, interaction: discord.Interaction, username: str | None = None):
        await interaction.response.defer()
        user = self._lastfm_username(interaction.user.id, username)
        if not user:
            await interaction.followup.send("Use `/fm set` first, or provide a Last.fm username.", ephemeral=True); return
        try:
            profile = (await self._lastfm("user.getInfo", user=user))["user"]
        except Exception as error:
            await self._fm_error(interaction, error); return
        embed = discord.Embed(title=_clean(profile.get("realname") or profile.get("name")), url=profile.get("url"), color=0xD92323)
        embed.add_field(name="Scrobbles", value=_number(profile.get("playcount")))
        embed.add_field(name="Artists", value=_number(profile.get("artist_count")))
        embed.add_field(name="Tracks", value=_number(profile.get("track_count")))
        await interaction.followup.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        settings = _music_settings()
        if not settings.get("enabled", True) or not message.guild or message.author.bot or message.guild.id != cfg.guild_id:
            return
        channels = {int(item) for item in settings.get("monitoredChannelIDs", []) if str(item).isdigit()}
        if channels and message.channel.id not in channels:
            return
        bucket = self._cooldown.get_bucket(message)
        if bucket.update_rate_limit(message.created_at.timestamp()):
            return
        content = message.content.strip()
        if settings.get("allowDotFm", True) and re.fullmatch(r"\.fm(?:\s+([A-Za-z0-9_-]{1,64}))?", content, re.IGNORECASE):
            username = (re.fullmatch(r"\.fm(?:\s+([A-Za-z0-9_-]{1,64}))?", content, re.IGNORECASE).group(1)
                        or self._lastfm_username(message.author.id))
            if not username or not getattr(cfg, "lastfm_api_key", None):
                await message.reply("Use `/fm set` first. The server owner must also add a free Last.fm API key.", mention_author=False)
                return
            try:
                data = await self._lastfm("user.getRecentTracks", user=username, limit=1)
                track = ((data.get("recenttracks") or {}).get("track") or [])[0]
                artist = track.get("artist") or {}
                artist_name = artist.get("#text") if isinstance(artist, dict) else artist
                track_url = track.get("url")
                suffix = f"\n<{track_url}>" if track_url else ""
                await message.reply(
                    f"🎵 **{_clean(track.get('name'))}** — {_clean(artist_name)}{suffix}",
                    mention_author=False,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, IndexError, ValueError):
                await message.reply("I could not load that Last.fm activity right now.", mention_author=False)
            return
        match = SUPPORTED_LINK.search(content)
        if match and settings.get("autoConvertLinks", True):
            await self._send_song(message, match.group(0))


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicSuite(bot))
