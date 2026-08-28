# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Self-hosted Plex owners who run CineSnip's Docker container on their own machine, weighted equally between the developer's own daily use and other self-hosters installing it fresh from GitHub. Two distinct jobs share the same app:
- **First-run/reconfiguration**: someone (potentially non-technical) setting up Discord auth, Plex auth, library path-mappings, and validation via the `/setup` wizard, with no terminal skill beyond `docker compose up`.
- **Ongoing use**: the app owner browsing their own Plex library and generating a GIF/MP4/WebM clip from a quote or timestamp via the `/generate` route, outside Discord.

## Product Purpose

CineSnip is a self-hosted Discord bot + local web app that turns a spoken movie/TV quote or timestamp into a short GIF/clip pulled from the owner's own Plex library, posted to Discord. The local web app (this surface) is the friendlier alternative to hand-editing `.env`/`config.yaml` for setup, and a non-Discord way to generate clips. Success: a non-technical installer can get from "cloned the repo" to "bot working in my server" without touching YAML, and the app owner can generate a styled clip in a few clicks.

## Positioning

Every installation is fully independent — no central service, no shared infrastructure, no data passing through anyone but the installer's own Plex/Discord. Extracts clips live/on-demand from the user's own library rather than pre-converting a whole library ahead of time (unlike the tvgif project that inspired it).

## Operating Context

- Runs as one Docker container, single Python process (FastAPI + htmx server-rendered templates, no SPA framework).
- The wizard binds to localhost only by default (LAN exposure is an explicit opt-in) since it handles raw secrets (Discord token, Plex token) during setup.
- Used on both desktop (setup, dev) and mobile (recent commits made the shell responsive: fluid cards, stacking grid, hamburger drawer, iOS Safari input-zoom fix) — real day-to-day usage spans both.
- Two supported Plex-hosting patterns (native-Windows-host and Dockerized-Plex) and per-library path mappings are configured through this UI.
- `/generate` is a live client of the same internal worker API the Discord bot uses (`/search`, `/resolve-quote`, `/render`, style presets, format options) — so its UI surfaces the same feature set: film/TV search, quote or timecode input, style presets (Classic/Boxed/Cinematic/Meme/Original/No Subtitles), format choice (GIF default, MP4/WebM opt-in).

## Capabilities and Constraints

- Current routes: `/setup` (wizard: Discord token entry + live validation, Plex PIN auth flow, library/path-mapping auto-suggest, final validation step) and `/generate` (film/TV search, quote/timecode entry, result preview, style/format switch that re-renders in place, "Post to channel" hookup back to Discord — per CLAUDE.md's decision #6).
- No telemetry, no external calls beyond the user's own Plex/Discord — nothing about usage should be designed as if it phones home.
- Token fields must never be logged or echoed back anywhere outside the config file they're written to (hard security constraint, not a style choice).
- Server-rendered via htmx (`app/web/templates/*.html`, `app/web/static/style.css`) — no client-side JS framework; interactions (style-preset swap, wizard steps) are htmx partial swaps (`panel_*.html`), not SPA routing.

## Brand Commitments

- Product name: CineSnip.
- Existing accent palette (mint/teal accent on dark backgrounds) is confirmed and binding — keep it recognizable through any redesign work. Everything else visual (typography, layout, component shapes, motion) is open to change.

## Evidence on Hand

- Existing implementation is real, running code (`app/web/`), not a mockup — treat `style.css` and the current templates as the incumbent visual system to extract the palette from, not placeholder scaffolding.
- No user research, testimonials, or usage metrics exist or should be fabricated — this is a single-owner/small-self-hoster-community tool with no analytics.

## Product Principles

- Self-hosted-first: never design as though a central service, account system, or cloud dependency exists.
- Serve two very different moments in one shell: a cautious, secret-handling first-run wizard, and a fast, repeat-use daily tool — both need to feel like the same product without forcing one's pacing onto the other.
- Non-technical installers are a real, equally-weighted audience, not an edge case — wizard clarity matters as much as the generate flow's efficiency.
- Keep the mint/teal-on-dark identity through any visual changes; everything else is negotiable.

## Accessibility & Inclusion

No product-specific accessibility requirement has been established yet; treat standard web accessibility practice (keyboard nav, contrast, focus states) as the baseline given the wizard handles password-masked secret fields and the audience includes non-technical users.
