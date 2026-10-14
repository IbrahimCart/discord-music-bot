"""Discord music bot: YouTube first, SoundCloud fallback, Spotify links via YouTube search.

Private-server use only. See README.md for setup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: makes slash commands appear instantly
COOKIES_FILE = os.getenv("COOKIES_FILE")  # optional: cookies.txt if YouTube asks to "confirm you're not a bot"
MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST", "100"))
IDLE_TIMEOUT = 300  # seconds of an empty queue before the bot leaves
SEARCH_RESULTS = 5  # how many YouTube results to try, in order, before falling back to SoundCloud

log = logging.getLogger("musicbot")

# discord.py needs the Opus library; on macOS it comes from `brew install opus`.
if not discord.opus.is_loaded():
    for _name in ("opus", "/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"):
        try:
            discord.opus.load_opus(_name)
            break
        except OSError:
            continue

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
}
if COOKIES_FILE:
    YDL_OPTS["cookiefile"] = COOKIES_FILE

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

URL_RE = re.compile(r"^https?://", re.I)
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)")


# --------------------------------------------------------------------------- #
# Finding audio (yt-dlp)
# --------------------------------------------------------------------------- #
def _extract(source: str) -> Optional[dict]:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(source, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else None
    return info


def _youtube_candidates(query: str) -> list[str]:
    """Return links for the top YouTube search results (a quick listing, nothing is resolved yet)."""
    opts = {**YDL_OPTS, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    urls = []
    for entry in (info or {}).get("entries") or []:
        url = entry and (entry.get("url") or entry.get("webpage_url"))
        if url:
            urls.append(url)
    return urls


async def resolve(query: str) -> Optional[dict]:
    """Return yt-dlp info (with a direct stream 'url') for a link or search text.

    Search text tries the top YouTube results in order (skipping any that are protected or
    unavailable), then falls back to SoundCloud. Stream URLs expire, so this is called right
    before a track plays rather than when it is queued.
    """
    loop = asyncio.get_running_loop()
    if URL_RE.match(query):
        sources = [query]
    else:
        try:
            sources = await loop.run_in_executor(None, _youtube_candidates, query)
        except Exception as exc:
            log.warning("YouTube search failed for %r: %s", query, str(exc)[:200])
            sources = []
        sources.append(f"scsearch1:{query}")

    for source in sources:
        try:
            info = await loop.run_in_executor(None, _extract, source)
        except Exception as exc:  # yt-dlp raises many kinds of errors (DRM, private, region-locked...)
            log.warning("Skipping %r: %s", source, str(exc)[:200])
            continue
        if info and info.get("url"):
            return info
    return None


# --------------------------------------------------------------------------- #
# Spotify (optional): turns links into "Artist - Title" search text
# --------------------------------------------------------------------------- #
def _spotify_client():
    if not (os.getenv("SPOTIPY_CLIENT_ID") and os.getenv("SPOTIPY_CLIENT_SECRET")):
        return None
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials

    return spotipy.Spotify(auth_manager=SpotifyClientCredentials())


def _track_query(track: dict) -> Optional[str]:
    if not track or not track.get("name"):
        return None
    artists = track.get("artists") or []
    artist = artists[0]["name"] if artists else ""
    return f"{artist} - {track['name']}".strip(" -")


def _spotify_queries(kind: str, sp_id: str) -> tuple[str, list[str]]:
    sp = _spotify_client()
    if sp is None:
        raise RuntimeError("Spotify isn't set up. Add SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET to .env.")
    if kind == "track":
        t = sp.track(sp_id)
        return t["name"], [q for q in [_track_query(t)] if q]

    if kind == "album":
        album = sp.album(sp_id)
        page, name = album["tracks"], album["name"]
        tracks = []
        while page:
            tracks += page["items"]
            page = sp.next(page) if page.get("next") and len(tracks) < MAX_PLAYLIST else None
    else:  # playlist
        name = sp.playlist(sp_id, fields="name")["name"]
        page, tracks = sp.playlist_items(sp_id, additional_types=("track",)), []
        while page:
            tracks += [i.get("track") for i in page["items"]]
            page = sp.next(page) if page.get("next") and len(tracks) < MAX_PLAYLIST else None

    queries = [q for q in (_track_query(t) for t in tracks[:MAX_PLAYLIST]) if q]
    return name, queries


# --------------------------------------------------------------------------- #
# Per-server player
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    query: str  # a link, or "Artist - Title" search text
    title: Optional[str] = None
    requester: str = ""

    @property
    def label(self) -> str:
        return self.title or self.query


players: dict[int, "GuildPlayer"] = {}


class GuildPlayer:
    def __init__(self, guild: discord.Guild, channel: discord.abc.Messageable):
        self.guild = guild
        self.channel = channel
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.wake = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self.wake.set()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self.wake.clear()
            if not self.queue:
                self.current = None
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self.channel.send("Queue empty, leaving the voice channel.")
                    await self.stop()
                    return
                continue

            track = self.queue.popleft()
            info = await resolve(track.query)
            vc = self.guild.voice_client
            if vc is None:
                return
            if info is None:
                await self.channel.send(f"Couldn't find anything playable for **{track.label}**, skipping.")
                continue

            track.title = info.get("title") or track.label
            self.current = track
            finished = asyncio.Event()
            source = discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTS)
            vc.play(source, after=lambda _err: loop.call_soon_threadsafe(finished.set))
            await self.channel.send(f"Now playing: **{track.title}**")
            await finished.wait()

    async def stop(self) -> None:
        self.queue.clear()
        if self.task is not asyncio.current_task():
            self.task.cancel()
        players.pop(self.guild.id, None)
        vc = self.guild.voice_client
        if vc:
            await vc.disconnect(force=True)


# --------------------------------------------------------------------------- #
# Bot + slash commands
# --------------------------------------------------------------------------- #
class MusicBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_voice_state_update(self, member, before, after):
        # If the bot is disconnected from voice (kicked, channel deleted), clean up.
        if member.id == self.user.id and before.channel and not after.channel:
            player = players.pop(member.guild.id, None)
            if player:
                player.queue.clear()
                player.task.cancel()


bot = MusicBot()


def _active_player(interaction: discord.Interaction) -> Optional[GuildPlayer]:
    return players.get(interaction.guild_id) if interaction.guild_id else None


@bot.tree.command(name="play", description="Play a song name, a YouTube/SoundCloud link, or a Spotify link")
@app_commands.describe(query="Song name or link")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
    await interaction.response.defer()

    # Build the list of tracks to queue.
    query = query.strip()
    who = interaction.user.display_name
    match = SPOTIFY_RE.search(query)
    if match:
        try:
            name, queries = await asyncio.get_running_loop().run_in_executor(
                None, _spotify_queries, match.group(1), match.group(2)
            )
        except Exception as exc:
            return await interaction.followup.send(f"Spotify error: {str(exc)[:300]}")
        if not queries:
            return await interaction.followup.send("That Spotify link had no playable tracks.")
        tracks = [Track(q, requester=who) for q in queries]
        summary = f"Queued **{len(tracks)}** track(s) from **{name}**."
    else:
        tracks = [Track(query, requester=who)]
        summary = f"Queued: **{query}**"

    # Connect (or move) to the user's voice channel, then hand the tracks to the player.
    vc = interaction.guild.voice_client
    try:
        if vc is None:
            await voice.channel.connect()
        elif vc.channel != voice.channel:
            await vc.move_to(voice.channel)
    except Exception as exc:
        return await interaction.followup.send(f"Couldn't join the voice channel: {str(exc)[:200]}")

    player = players.get(interaction.guild_id)
    if player is None:
        player = players[interaction.guild_id] = GuildPlayer(interaction.guild, interaction.channel)
    player.channel = interaction.channel
    player.add(tracks)
    await interaction.followup.send(summary)


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # the player loop moves on to the next track
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause playback")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="queue", description="Show what's playing and what's next")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    player = _active_player(interaction)
    if not player or (not player.current and not player.queue):
        return await interaction.response.send_message("The queue is empty.", ephemeral=True)
    lines = []
    if player.current:
        lines.append(f"**Now playing:** {player.current.label}")
    upcoming = list(player.queue)
    for i, t in enumerate(upcoming[:10], 1):
        lines.append(f"{i}. {t.label}")
    if len(upcoming) > 10:
        lines.append(f"...and {len(upcoming) - 10} more")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="stop", description="Stop playback, clear the queue and leave the voice channel")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    player = _active_player(interaction)
    if player:
        await player.stop()
        await interaction.response.send_message("Stopped and left the voice channel.")
    elif interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect(force=True)
        await interaction.response.send_message("Left the voice channel.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(TOKEN, log_handler=None)
