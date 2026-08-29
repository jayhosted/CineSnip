# CineSnip

Generate short clips from your own Plex library, straight into Discord.

`/snip` for films, `/snip-tv` for TV episodes: give it a title and either a
`quote` (fuzzy-matched against subtitles) or a `timecode` (e.g. `1:23:45`),
one Docker container. Searches across every Plex library you configure.
See [CLAUDE.md](CLAUDE.md) for the full project plan and later-stage
features.

## 1. Prerequisites

- Docker + Docker Compose
- A running Plex server, reachable from the machine running Docker
- The films you want to clip already in a Plex movie library

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

`libraries` lists every Plex library CineSnip should search, each with its
own `path_mappings` telling CineSnip how to translate the file path Plex
reports into the path the container actually sees (via the bind mounts in
`docker-compose.yml`). CineSnip searches across all configured libraries by
default — if the same title exists in more than one (e.g. a regular Movies
library and a separate 4K one), results are labeled with which library they
came from.

For each library you want CineSnip to search:

1. Add a `libraries` entry with `name` matching that library's title in
   Plex exactly (case-sensitive).
2. For each folder that library spans, in Plex open a title in it → **...**
   → **Get Info** → **View XML**, and note the `file="..."` path Plex
   reports for its media part.
3. Add a `path_mappings` entry under that library where `plex_prefix` is
   that path's folder prefix (exactly as Plex reports it, backslashes and
   all if Plex runs on Windows), and `container_path` matches the mount
   target for that same folder in `docker-compose.yml`.

If a library only lives in one folder, it only needs one `path_mappings`
entry. If it spans more folders than the examples, add more of both (and a
matching bind mount in `docker-compose.yml` for each new folder).

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

In your Discord server, either give a quote:

```
/snip film:<start typing a title> quote:here's johnny
```

or a direct timecode:

```
/snip film:<start typing a title> timecode:1:23:45
```

Timecodes accept `1:23:45`/`23:45`/a plain number of seconds, or
unit-suffixed forms like `1h23m45s` or `22min 12sec` — whichever's easiest
to type. For a direct timecode (not a quote), you can also give
`end_timecode` for a custom-length clip instead of the default ~4s — clips
must land within `render_defaults.min_duration_seconds`/`max_duration_seconds`
in `config.yaml` (1s–15s by default); asking for something outside that
range gives a clear error rather than a silently different-length clip.

Pick a film from the autocomplete suggestions. With a
quote, CineSnip fuzzy-matches it against the film's subtitles (sidecar
`.srt` or an embedded subtitle stream — see [CLAUDE.md](CLAUDE.md) Section 5
for what happens when neither exists), uses the matched line's own length
for the clip, and shows the best match with surrounding context and a
confidence score; if it's not confident, or you want a different line, use
"Show other matches" to pick from the next-best candidates.

Either way you'll get a clip back straight away, with subtitles already
burned in using a sensible default style — Classic after a quote match
(there's a known line to show), or no subtitles for a bare timecode (no
guarantee that title even has usable subtitles). A style dropdown sits
below the result if you want something else — Boxed, Cinematic, Meme,
Original, or No Subtitles — and picking one re-renders in place, no need to
restart the command. A "Post to channel" button shares whatever's currently
shown beyond just you. If you ask for a style on a title with no usable
subtitles, CineSnip still renders the clip, just without burned-in text, and
says so.

Add `format:gif`/`format:mp4`/`format:webm` to pick the output — gif is the
default (`render_defaults.format` in `config.yaml`) because it's the only
one of the three that actually autoplays/loops inline in Discord and can be
added to the GIF picker's favorites; mp4/webm are much smaller but show up
as a real video player (play button, volume slider) instead, so they're
worth picking only if you specifically want the smaller file and don't mind
clicking play.

### Searching without picking a film first

`/snip-search quote:<text>` searches for a line across every film CineSnip
has *already read subtitles for* — from any prior `/snip` or `/snip-search`
use — instead of requiring you to pick a film up front. It's fast (no
re-parsing, no Plex calls), but its scope is exactly what's been touched so
far: a title you've never generated a clip from won't show up yet. Pick a
result from the list and it funnels straight into the same quote-confirm
step as the normal flow. If nothing's indexed yet, run `/snip` on a specific
film first (or just start using the bot normally — the searchable set grows
automatically as you go).

### TV episodes

`/snip-tv` works the same way, one layer deeper: pick a `show` from
autocomplete, then either give `season`/`episode` together with a `quote` or
`timecode` for a specific episode (same rules as `/snip` from there — clip
length, styles, format all work identically), or give just a `quote` with no
`season`/`episode` to search across the *whole show* for that line. A
whole-show search checks every episode CineSnip has already read subtitles
for instantly, and extracts subtitles on the spot for any episode it
hasn't seen yet — which can take a little while the first time through a
show, faster on repeat searches. A bare `timecode` needs a specific episode
to seek within, so `season`/`episode` are required whenever you're not using
`quote`.

## 8. Library auto-sync (optional)

**Off by default.** CineSnip's subtitle cache normally only grows through
normal use — a title only gets its subtitles extracted the first time
someone actually searches or generates a clip from it (see `scripts/build_full_cache.py`
for a manual one-off way to build the whole cache instead). Library
auto-sync is an opt-in alternative for keeping that cache current
automatically as your library changes, without needing to remember to run
anything manually.

Enable it in `config.yaml`:

```yaml
library_sync:
  enabled: true
  interval_hours: 24
```

What it does, each time it runs: it asks Plex for each library's own
`updatedAt` timestamp — a cheap check, confirmed not to fire on routine
per-title actions like Refresh Metadata or Analyze, only on genuine
scanner-detected adds/removals — and does nothing further unless something
actually changed. When a library *has* changed, it extracts and caches
subtitles for any new titles, and **removes cache entries for titles no
longer in Plex**.

**That last part is worth reading carefully before enabling this**: it can
delete cache files. Two independent safety checks have to both pass before
any deletion happens, specifically so a Plex outage or a dead/disconnected
drive can never be mistaken for genuine content removal:
1. A **mount check** — every path this library depends on must actually be
   reachable and non-empty on disk right now.
2. A **spot check** — a sample of *other* titles Plex still says are
   present must also actually exist on disk.

If either check fails, cleanup is skipped for that whole library and
retried on the next cycle — new titles still get added normally either way,
only the cache-removal step is held back. Nothing is ever silently lost:
the worst case of a false trigger is a delayed cleanup, not a wrongful one.

`interval_hours` controls how often it wakes up to check — the check itself
is nearly free when nothing's changed (a handful of lightweight Plex calls,
not a full library scan), so a short interval costs very little even on a
quiet day.

## Upgrading an existing install

If you already had CineSnip running before its subtitle search moved to
SQLite+FTS5, your existing per-title JSON cache isn't automatically
migrated into the new index — after upgrading, run the one-off migration
script once:

```
docker compose exec cinesnip .venv/bin/python scripts/migrate_to_fts5.py
```

(or the equivalent outside Docker if you run CineSnip natively). It's a
pure local read of your already-cached subtitles into the new index —
no Plex or ffmpeg calls — and it's safe to re-run; a title already migrated
is skipped unless you pass `--force`. Until you run it (or enable library
auto-sync above, which backfills titles gradually as they're touched),
`/snip-search` will silently return no results, since the new index starts
empty on upgrade.

## Troubleshooting

- **Bot fails to log in (401)** — `DISCORD_TOKEN` in `.env` is wrong or was reset since you copied it.
- **"No path mapping configured for ..." / "File not found on disk"** — that library's `path_mappings` in `config.yaml` don't match what Plex reports or what's actually bind-mounted. Re-check step 5, and confirm the corresponding volume in `docker-compose.yml` points at the right host folder.
- **"'X' is not a configured library"** — a title resolved to a Plex library that isn't listed under `libraries` in `config.yaml`. Add an entry for it (step 5).
- **ffmpeg errors** — check the container logs for the actual ffmpeg stderr output; this usually means the source file is a format ffmpeg can't read directly, or the mapped path is wrong.
- **"Couldn't generate the GIF: ... timed out"** — the source file is unusually slow for ffmpeg to seek/decode near that timestamp (raise `render_defaults.timeout_seconds` in `config.yaml` if this happens on files that should be fine), or something is stuck — check `docker compose logs`.
- **Permission denied writing to `/app/scratch` or `/app/cache`** — the host `scratch/`/`cache/` directory got created by Docker (as `root`) instead of by you before first run. Stop the container, `rm -rf scratch cache && mkdir scratch cache`, then start it again. (Unlike `scratch/`, it's safe to leave `cache/` in place across restarts — only delete it if you actually want to force re-extraction of all subtitles.)
- **Command doesn't show up (or a renamed command still shows its old name) in Discord** — without `DEV_GUILD_ID` set, commands sync globally on every startup, and Discord can take up to an **hour** to propagate a global slash command change to clients, not just a minute. To skip the wait while developing, set `DEV_GUILD_ID` in `.env` to your test server's ID — the bot then syncs only to that one server instead, which applies near-instantly (deliberately *not* global sync too, since both together left two copies of the same command visible side by side once the global one also propagated). **`DEV_GUILD_ID` is a local-dev-only setting — leave it unset once you're done iterating.** Left populated, it silently blocks commands from ever syncing to *any* other server the bot is invited to, including production servers; the bot logs a warning on startup whenever it's set as a reminder.
- **"No usable subtitles for ..."** — the film has neither a sidecar `.srt` next to the video file nor a text-based embedded subtitle stream (bitmap formats like PGS aren't extractable). Use `timecode:` for this title instead — there's no transcription fallback for this by design (see CLAUDE.md decision #7).
- **"No subtitle line ... resembled that quote"** — nothing scored well enough against the film's subtitles. Try a shorter, more distinctive phrase, or double-check the line isn't paraphrased/from a different cut.
- **"That's a Ns clip; clips must be between Xs and Ys"** — your `timecode`/`end_timecode` span is outside `render_defaults.min_duration_seconds`/`max_duration_seconds` in `config.yaml` (1s–15s by default). Pick a closer `end_timecode`, or raise the limit in `config.yaml` if you actually want longer clips.
