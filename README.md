# Discord Music Bot

A small Discord bot that plays music in a voice channel.

- Searches **YouTube** first and falls back to **SoundCloud**
- Accepts YouTube and SoundCloud links directly
- Accepts **Spotify** track, album and playlist links (it reads the song list from Spotify, then finds each song on YouTube or SoundCloud)
- Slash commands: `/play`, `/skip`, `/pause`, `/resume`, `/queue`, `/stop`
- Leaves the voice channel after 5 minutes with an empty queue

> **For private use in your own server.** Streaming from YouTube with yt-dlp goes against YouTube's terms of service, and Spotify does not allow its audio to be played by bots. Run it only in your own server and don't offer it as a public bot for others to add.

## Setup

Works on macOS and Windows. Steps that differ are marked.

### 1. Get the code

```bash
git clone https://github.com/IbrahimCart/discord-music-bot.git
cd discord-music-bot
```

### 2. Install the tools

You need Python 3.10 or newer and FFmpeg.

**macOS**

```bash
brew install python ffmpeg opus
```

**Windows** (PowerShell or Command Prompt)

```powershell
winget install Git.Git Python.Python.3.12 Gyan.FFmpeg
```

Close and reopen the terminal afterwards so `python`/`py` and `ffmpeg` are on your PATH. Check with `py --version` and `ffmpeg -version`. Opus is bundled with discord.py on Windows, so there's nothing extra to install.

### 3. Create the Discord bot

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open the **Bot** tab, click **Reset Token**, and copy the token. Keep it secret.
3. Open **OAuth2 → URL Generator**. Under scopes pick `bot` and `applications.commands`. Under bot permissions pick **View Channels, Send Messages, Connect, Speak**.
4. Open the generated URL and add the bot to your server.

No privileged intents are needed. If you already did this on another computer, reuse the same token — don't reset it, or the other copy of `.env` stops working.

### 4. (Optional) Spotify

Only needed for Spotify links.

1. Go to <https://developer.spotify.com/dashboard> and click **Create app**. For the redirect URI, enter `http://127.0.0.1:8888/callback` (it isn't used).
2. Copy the **Client ID** and **Client Secret**.

Only **public** playlists, albums and tracks work. For a private playlist, make it public temporarily or add the songs to a public one.

### 5. Configure

`.env` is not in the repo (on purpose), so create it on each computer:

```bash
cp .env.example .env        # macOS
copy .env.example .env      # Windows
```

Open `.env` (on Windows: `notepad .env`) and fill in `DISCORD_TOKEN`. Also set `GUILD_ID` (in Discord, turn on Developer Mode in Settings → Advanced, then right-click your server and choose **Copy Server ID**) so slash commands appear instantly. Add the two Spotify values if you set up Spotify.

### 6. Run

**macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

**Windows**

Double-click `run.bat`, or run `.\run.bat` in a terminal. The first run creates the `.venv` folder and installs everything; later runs just start the bot (and update yt-dlp).

To do it by hand instead:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```

Join a voice channel in Discord and type `/play never gonna give you up`.

**Run only one copy at a time.** Both computers use the same bot token, so if the bot is running on your Mac and your PC at once, both will answer commands and fight over the voice channel. Stop one (Ctrl+C) before starting the other.

### Getting updates on the other computer

After you change code and `git push` on one computer, run `git pull` on the other, then restart the bot.

## Commands

| Command | What it does |
|---|---|
| `/play <song or link>` | Plays a song name, YouTube/SoundCloud link or Spotify link. Adds to the queue if something is already playing. |
| `/skip` | Skips the current song |
| `/pause` / `/resume` | Pauses or resumes playback |
| `/queue` | Shows what's playing and the next 10 songs |
| `/stop` | Stops, clears the queue and leaves the voice channel |

## Troubleshooting

- **Nothing plays or errors appear:** YouTube changes often. Update the downloader: `pip install -U yt-dlp`, then restart the bot. This fixes most problems.
- **"Sign in to confirm you're not a bot":** export your browser cookies to a `cookies.txt` file (with a browser extension) and set `COOKIES_FILE=cookies.txt` in `.env`. Running from a home connection (not a cloud server) also helps.
- **`OpusNotLoaded` error:** on macOS run `brew install opus`. On Windows, reinstall discord.py: `.venv\Scripts\python -m pip install --force-reinstall "discord.py[voice]"`.
- **Slash commands don't show up:** set `GUILD_ID` in `.env`. Without it, global commands can take a while to appear.
- **`ffmpeg was not found` (Windows):** install it with `winget install Gyan.FFmpeg`, then open a new terminal.
- **Bot joins but no sound:** check that FFmpeg is installed (`ffmpeg -version`) and that the bot has the **Speak** permission in that channel.

## Security

Never commit `.env` or `cookies.txt` (both are in `.gitignore`). Before your first push, `git status` should not list `.env`; `git check-ignore -v .env` confirms it's ignored. If your bot token is ever leaked, click **Reset Token** in the Developer Portal right away.

## Running it 24/7

The bot is only online while this program is running. To keep it on all the time, run it on a Raspberry Pi or a small always-on computer, and update it with `git pull`. Home internet connections work better than cloud servers for YouTube.
