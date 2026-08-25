# CineSnip

Generate GIF clips from your own Plex library, straight into Discord.

This is the **MVP**: one `/cinesnip` command, films only, timecode input only
(e.g. "generate a GIF starting at 1:23:45"), one Plex library, one Docker
container. See [CLAUDE.md](CLAUDE.md) for the full project plan and
later-stage features.

## 1. Prerequisites

- Docker + Docker Compose
- A running Plex server, reachable from the machine running Docker
- The films you want to clip already in your Plex "Movies" library

## 2. Create a Discord bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Open the **Bot** tab → **Reset Token** → copy the token (you'll paste this into `.env` in step 4). Keep it secret — treat it like a password.
3. **On the same Bot tab, under Authorization Flow, turn OFF "Public Bot."** This is important: CineSnip is tied to *your* Plex library, and a public bot can be added to any server by anyone via the "Add to Server" button on its profile — which would hand them browse/generate access to your media. With Public Bot off, only you can generate a working invite URL; you can still invite it to as many of your own servers as you like, it just can't be self-service-invited by someone else.
4. No privileged intents are needed for the MVP (it's slash-command-only).
5. Open **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Attach Files`, `Use Slash Commands`
6. Open the generated URL in your browser and invite the bot to your test Discord server.

## 3. Get a Plex token

Follow Plex's official guide to find your `X-Plex-Token`:
https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

You'll paste this into `.env` in the next step.

## 4. Configure `.env`

```bash
cp .env.example .env
```

Fill in:

- `DISCORD_TOKEN` — from step 2
- `PLEX_URL` — where the container can reach Plex:
  - **Plex running natively on the same machine as Docker** (Docker Desktop, Windows/Mac): `http://host.docker.internal:32400`
  - **Plex running in its own Docker container**: use Plex's container name/IP on a shared Docker network, e.g. `http://plex:32400`
- `PLEX_TOKEN` — from step 3

## 5. Configure `config.yaml`

```bash
cp config.yaml.example config.yaml
```

`path_mappings` tells CineSnip how to translate the file path Plex reports
into the path the container actually sees (via the bind mounts in
`docker-compose.yml`). For each folder your Movies library spans:

1. In Plex, open a movie → **...** → **Get Info** → **View XML**, and note
   the `file="..."` path Plex reports for its media part.
2. Add (or edit) a `path_mappings` entry where `plex_prefix` is that path's
   folder prefix (exactly as Plex reports it, backslashes and all if Plex
   runs on Windows), and `container_path` matches the mount target for that
   same folder in `docker-compose.yml`.

If your Movies library only lives in one folder, delete the unused entry
(and its matching volume line in `docker-compose.yml`). If it spans more
folders than the two examples, add more of both.

## 6. Run it

Create the scratch render directory and the subtitle cache directory
yourself first — the container runs as a non-root user, and if Docker
creates these directories for you on first launch (because they don't
exist yet), they'll be owned by `root` and the container won't be able to
write to them. Unlike `scratch/` (cleared on every startup), `cache/` is
persistent — it holds parsed subtitles keyed by Plex media GUID so a
title's subtitles are only ever extracted once:

```bash
mkdir -p scratch cache
docker compose up --build
```

Watch the logs for two ready signals: uvicorn ("Uvicorn running on
http://127.0.0.1:8000") and discord.py logging in successfully. No ports
need to be published — the worker API is loopback-only inside the
container, and the bot only makes outbound connections to Discord.

## 7. Try it

In your Discord server:

```
/cinesnip film:<start typing a title> timecode:1:23:45
```

Pick a film from the autocomplete suggestions, confirm the embed, and
you'll get a GIF back — with a "Post to channel" button to share it beyond
just you.

## Troubleshooting

- **Bot fails to log in (401)** — `DISCORD_TOKEN` in `.env` is wrong or was reset since you copied it.
- **"No path mapping configured for ..." / "File not found on disk"** — your `config.yaml` `path_mappings` don't match what Plex reports or what's actually bind-mounted. Re-check step 5, and confirm the corresponding volume in `docker-compose.yml` points at the right host folder.
- **ffmpeg errors** — check the container logs for the actual ffmpeg stderr output; this usually means the source file is a format ffmpeg can't read directly, or the mapped path is wrong.
- **"Couldn't generate the GIF: ... timed out"** — the source file is unusually slow for ffmpeg to seek/decode near that timestamp (raise `render_defaults.timeout_seconds` in `config.yaml` if this happens on files that should be fine), or something is stuck — check `docker compose logs`.
- **Permission denied writing to `/app/scratch` or `/app/cache`** — the host `scratch/`/`cache/` directory got created by Docker (as `root`) instead of by you before first run. Stop the container, `rm -rf scratch cache && mkdir scratch cache`, then start it again. (Unlike `scratch/`, it's safe to leave `cache/` in place across restarts — only delete it if you actually want to force re-extraction of all subtitles.)
- **Command doesn't show up in Discord** — slash command sync can take a minute to propagate; try restarting Discord or waiting briefly.
