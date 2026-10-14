"""Discord music bot: YouTube first, SoundCloud fallback, Spotify links via YouTube search.

Private-server use only. See README.md for setup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field  # "field" lets Track.pending default to None without sharing state
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
# How many YouTube results to list when the top result is a bad pick or fails to play
# (edit/remix/reupload, DRM-protected, private, region-locked...).
# Higher = more chances to find the right version, but a slower fallback.
SEARCH_RESULTS = 5

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


# Words that usually mean "not the original recording". A result is only penalised for a word the
# user did NOT type themselves, so searching "song name remix" still finds the remix.
EDIT_WORDS = (
    "remix", "mashup", "extended", "sped up", "speed up", "slowed", "nightcore", "reverb",
    "8d", "bass boosted", "cover", "live", "karaoke", "instrumental", "reupload", "loop",
)
EDIT_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in EDIT_WORDS) + r")\b", re.I)


def _edit_words(text: Optional[str]) -> set[str]:
    """The edit/remix-style words found in some text (lower-cased)."""
    return {m.lower() for m in EDIT_RE.findall(text or "")}


def _looks_like_edit(title: Optional[str], query: str) -> bool:
    """True if the title has an edit-style word that the search text didn't ask for."""
    return bool(_edit_words(title) - _edit_words(query))


# Titles containing these words are music videos (or other videos) and are never played.
VIDEO_RE = re.compile(r"\b(videos?|mv|m/v)\b", re.I)  # covers "Music Video", "Official Video", "[MV]"...


def _is_video(title: Optional[str], query: str) -> bool:
    """True if the title says it's a video. Not applied if the search text itself contains one of
    these words, so a song like "Video Games" or a search for "song music video" still works."""
    return bool(VIDEO_RE.search(title or "")) and not VIDEO_RE.search(query)


def _audio_query(query: str) -> str:
    """Add "audio" to the search text so YouTube ranks audio uploads (and auto-generated
    "- Topic" channels) above music videos. Skipped if the user already said what they want."""
    return query if re.search(r"\b(audio|video|lyrics?|mv)\b", query, re.I) else f"{query} audio"


def _is_clean_audio(info: dict, query: str) -> bool:
    """True if a result is clearly the plain audio: an auto-generated "- Topic" channel, or a
    title saying audio/lyrics, and no edit-style words we weren't asked for."""
    title = info.get("title") or ""
    channel = info.get("channel") or info.get("uploader") or ""
    if _looks_like_edit(title, query):
        return False
    return channel.endswith(" - Topic") or bool(re.search(r"\b(audio|lyrics?)\b", title, re.I))


def _score(entry: dict, query: str) -> int:
    """Rank a search result: higher = more likely to be the plain, original audio upload."""
    title = entry.get("title") or ""
    channel = entry.get("channel") or entry.get("uploader") or ""
    score = 0
    if channel.endswith(" - Topic"):  # YouTube's auto-generated official-audio channels
        score += 3
    if re.search(r"\b(audio|lyrics?)\b", title, re.I):  # "(Official Audio)", "(Lyrics)"...
        score += 2
    if "vevo" in channel.lower():  # VEVO channels mostly host the music videos, so rank them lower
        score -= 2
    # remix/mashup/etc. that weren't asked for (videos never get this far, see _is_video)
    score -= 3 * len(_edit_words(title) - _edit_words(query))
    return score


def _youtube_candidates(query: str) -> list[dict]:
    """List the top YouTube search results (id, url, title, channel), in YouTube's order.

    Only used as a fallback when the very first result isn't clearly plain audio or can't be played;
    see resolve(). Pass it the same audio-biased search text that resolve() used.
    """
    # extract_flat = just list the results, don't fetch each video's stream info. That keeps this
    # to one cheap request; we only do the expensive lookup for results we actually try.
    opts = {**YDL_OPTS, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    results = []
    for entry in (info or {}).get("entries") or []:  # entries can be None for unavailable results
        if entry and (entry.get("url") or entry.get("webpage_url")):
            entry.setdefault("url", entry.get("webpage_url"))
            results.append(entry)
    return results


async def _try(source: str) -> Optional[dict]:
    """Resolve one source; returns None (and logs) if it is protected, private, missing, etc."""
    loop = asyncio.get_running_loop()
    try:
        # yt-dlp is blocking, so run it in a thread to keep the bot responsive to other commands.
        info = await loop.run_in_executor(None, _extract, source)
    except Exception as exc:  # yt-dlp raises many kinds of errors (DRM, private, region-locked...)
        # Not fatal: log it and let resolve() move on to the next option.
        log.warning("Skipping %r: %s", source, str(exc)[:200])
        return None
    return info if info and info.get("url") else None


async def resolve(query: str) -> Optional[dict]:
    """Return yt-dlp info (with a direct stream 'url') for a link or search text.

    Search text gets "audio" added, and the top YouTube result is used straight away (one lookup)
    if it is clearly plain audio: a "- Topic" channel or an audio/lyrics title, with no edit or
    remix words. Results whose title says "video" (music video, official video, MV...) are never
    played. Otherwise (or if the top result fails to play) the next few results are listed, videos
    removed, ranked (auto-generated "- Topic" and "audio" titles first, edits and VEVO last) and
    tried in order, then SoundCloud. If every result is a video, nothing is found rather than
    playing a video. Stream URLs expire, so this runs shortly before a track plays.
    """
    # A pasted link: just resolve that one link, no searching or ranking.
    if URL_RE.match(query):
        return await _try(query)

    # Nudge YouTube toward audio uploads instead of music videos (see _audio_query).
    search = _audio_query(query)

    # Fast path: one lookup. Keep the top result if it is clearly plain audio.
    info = await _try(f"ytsearch1:{search}")
    top_failed = info is None  # couldn't be played (DRM, private...): it must not be retried below
    if info and _is_video(info.get("title"), query):
        log.info("Top result %r is a video; skipping it", info.get("title"))
        info = None  # never play it; the listing below filters it out too
    elif info and _is_clean_audio(info, query):
        return info
    elif info:
        log.info("Top result %r isn't clearly plain audio; checking other results", info.get("title"))

    # Slow path: the top result failed to play, or it might be a music video or edit. List the
    # next few results so we can choose a better one.
    try:
        candidates = await asyncio.get_running_loop().run_in_executor(None, _youtube_candidates, search)
    except Exception as exc:
        log.warning("YouTube search failed for %r: %s", query, str(exc)[:200])
        candidates = []
    if top_failed and candidates:
        candidates = candidates[1:]  # the first result is the one that just failed to play
    # Drop every video from the listing so none of them can be picked.
    candidates = [e for e in candidates if not _is_video(e.get("title"), query)]

    # Best-looking first. sorted() is stable, so ties keep YouTube's own order.
    for entry in sorted(candidates, key=lambda e: -_score(e, query)):
        if info and entry.get("id") == info.get("id"):
            chosen = info  # already looked up above, no need to ask YouTube again
        else:
            chosen = await _try(entry["url"])
        if chosen:
            if chosen is not info and info:
                log.info("Using %r instead of %r", chosen.get("title"), info.get("title"))
            return chosen

    # The top result was playable (and not a video) but the listing gave us nothing better, or the
    # listing failed: play it rather than fail. (If it had failed to play, info is None and we go on to SoundCloud.)
    if info:
        return info
    # Last resort: SoundCloud (not covered by the YouTube results above).
    return await _try(f"scsearch1:{query}")


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
    # Background lookup of this track's stream, started while the previous song is still playing
    # (see GuildPlayer._prefetch_next). None until/unless a prefetch was started.
    pending: Optional[asyncio.Task] = field(default=None, repr=False)

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
        self._prefetch_next()  # if a song is already playing, start looking up the new "next" track
        self.wake.set()

    def _prefetch_next(self) -> None:
        """While a song plays, look up the next one in the background so it starts instantly.

        Only runs while something is playing (self.current is set); when the player is idle it
        pops the track and resolves it right away anyway. Does nothing if the next track already
        has a lookup in flight, so it is safe to call repeatedly.
        """
        if self.current and self.queue and self.queue[0].pending is None:
            self.queue[0].pending = asyncio.create_task(resolve(self.queue[0].query))

    def clear_queue(self) -> None:
        """Empty the queue and cancel any background lookups so they don't keep running."""
        for t in self.queue:
            if t.pending:
                t.pending.cancel()
        self.queue.clear()

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
            # Use the lookup already started in the background if there is one (instant, or nearly);
            # otherwise resolve now. "or" only calls resolve() when there is no pending lookup.
            info = await (track.pending or resolve(track.query))
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
            self._prefetch_next()  # playback has started: get the following song ready in the background
            await finished.wait()

    async def stop(self) -> None:
        self.clear_queue()  # also cancels any prefetch lookups
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
                player.clear_queue()
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
