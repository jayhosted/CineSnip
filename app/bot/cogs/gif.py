from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Literal

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from app.bot.worker_client import LibraryQuoteMatchResult, QuoteMatchResult

logger = logging.getLogger("cinesnip.bot.gif")


def _confidence_label(score: float, min_score: float, confident_score: float) -> str:
    if score >= confident_score:
        return "high"
    # Bucket the range between the engine's real noise floor (min_score,
    # echoed from the worker's config — nothing below it is ever returned
    # as a match) and confident_score, both sent by the API so this never
    # drifts from the actual configured values.
    midpoint = (confident_score + min_score) / 2
    if score >= midpoint:
        return "medium"
    return "low"


def _match_embed(
    title: str,
    match: QuoteMatchResult,
    min_score: float,
    confident_score: float,
    position: int,
    total: int,
) -> discord.Embed:
    lines = [f"> {line}" for line in match.context_before]
    lines.append(f"> **{match.text}**")
    lines.extend(f"> {line}" for line in match.context_after)

    embed = discord.Embed(title=title, description="\n".join(lines))
    label = _confidence_label(match.score, min_score, confident_score)
    embed.add_field(name="Timecode", value=match.timecode)
    embed.add_field(name="Confidence", value=f"{match.score:.0f}% ({label})")
    embed.set_footer(text=f"Match {position} of {total}")
    return embed


class QuoteMatchView(discord.ui.View):
    """Confirm/cancel a quote match, with an optional select menu to browse
    alternatives. Per CLAUDE.md decision #4, browsing alternatives never
    stops the view or re-asks the film step — only Confirm/Cancel does.
    """

    def __init__(
        self,
        title: str,
        matches: list[QuoteMatchResult],
        min_score: float,
        confident_score: float,
        initial_index: int = 0,
    ) -> None:
        super().__init__(timeout=120)
        self._title = title
        self.matches = matches
        self.min_score = min_score
        self.confident_score = confident_score
        self.value: bool | None = None
        # Defaults to the top-scored candidate (0), but a caller that
        # already knows which specific line the user wants (e.g.
        # LibrarySearchView, re-running this search for a line already
        # picked by its exact text) can pre-select it instead.
        self.index = initial_index

        self._show_others_button: discord.ui.Button | None = None

        if len(matches) > 1:
            if matches[self.index].score >= confident_score:
                self._add_show_others_button()
            else:
                # Borderline top match: open straight to the alternatives
                # menu rather than hiding it behind a button.
                self._add_select()

    @property
    def selected(self) -> QuoteMatchResult:
        return self.matches[self.index]

    def embed(self) -> discord.Embed:
        return _match_embed(
            self._title,
            self.selected,
            self.min_score,
            self.confident_score,
            self.index + 1,
            len(self.matches),
        )

    def _add_show_others_button(self) -> None:
        button = discord.ui.Button(
            label="Show other matches", style=discord.ButtonStyle.secondary
        )
        button.callback = self._on_show_others
        self._show_others_button = button
        self.add_item(button)

    def _add_select(self) -> None:
        options = [
            discord.SelectOption(
                label=(m.text if len(m.text) <= 100 else m.text[:97] + "..."),
                description=f"{m.timecode} · {m.score:.0f}%",
                value=str(i),
                default=(i == self.index),
            )
            for i, m in enumerate(self.matches)
        ]
        select = discord.ui.Select(placeholder="Choose a match", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_show_others(self, interaction: discord.Interaction) -> None:
        if self._show_others_button is None:
            # A select is already showing — e.g. a fast double-click sent
            # two interactions for this button before the first one's
            # edit_message() landed. Ignore the second one rather than
            # adding a duplicate discord.ui.Select.
            await interaction.response.defer()
            return
        self.remove_item(self._show_others_button)
        self._show_others_button = None
        self._add_select()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        # The Select item we just added is the only select on the view
        # besides the Confirm/Cancel buttons, so find it by type rather
        # than keeping a separate reference.
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                self.index = int(item.values[0])
                # SelectOption.default is baked in at construction time and
                # never updates on its own — leaving the old select attached
                # would keep showing the FIRST-ever-picked option as
                # "selected" regardless of self.index, and re-picking an
                # option Discord still thinks is already the selected one
                # doesn't reliably register as a new choice. Rebuild the
                # select from scratch so its default always matches reality.
                self.remove_item(item)
                self._add_select()
                break
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


# (value, label) — value is what the worker's /render `style` field
# expects; "none" is a real, explicit choice ("No Subtitles"), not the
# absence of one. Order matches CLAUDE.md Section 2's listed preset order.
_STYLE_OPTIONS: list[tuple[str, str]] = [
    ("classic", "Classic (white, black outline)"),
    ("boxed", "Boxed (white on black box)"),
    ("cinematic", "Cinematic (yellow)"),
    ("meme", "Meme (bold caps)"),
    ("original", "Original (mirrors source style)"),
    ("none", "No Subtitles"),
]


def _no_subtitles_note(requested_style: str, resolved_style: str) -> str:
    if requested_style != "none" and resolved_style == "none":
        return "No subtitles available for this title — generated without burn-in."
    return ""


# Matches PlexClient._to_result()'s episode title format ("Show — S02E01 —
# Episode Title", app/worker/plex_client.py) so the compact posted-message
# slug can pull the show name and S##E## back out without a new API field.
_TV_TITLE_PATTERN = re.compile(r"^(?P<show>.+) — S(?P<season>\d{2})E(?P<episode>\d{2}) — .+$")


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _format_unit_timecode(seconds: float) -> str:
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _post_metadata_line(title: str, start: float, end: float, posted_by: str) -> str:
    """Metadata line appended to a "Post to channel" message's content
    (CLAUDE.md issue #9 design): `<slug>[-S##E##]@<start>-<end> posted by
    <display name>`. The leading `-# ` is Discord's subtext markdown — kept
    as plain message content (not an embed, per the design), just
    de-emphasized so it doesn't compete with the clip itself."""
    match = _TV_TITLE_PATTERN.match(title)
    if match:
        slug = _slugify(match.group("show"))
        suffix = f"-s{match.group('season')}e{match.group('episode')}"
    else:
        slug = _slugify(title)
        suffix = ""
    span = f"{_format_unit_timecode(start)}-{_format_unit_timecode(end)}"
    return f"-# {slug}{suffix}@{span} posted by {posted_by}"


class ClipResultView(discord.ui.View):
    """Shown once a clip has rendered: Post to channel, plus a style
    dropdown that re-renders in place if you want a different look —
    generation itself no longer waits on a style choice up front (CLAUDE.md
    Section 2's options step, applied after the fact instead of gating
    generation), so accepting the default needs no interaction at all."""

    def __init__(
        self,
        worker,
        rating_key: int,
        title: str,
        timecode: str,
        duration: float | None,
        end_timecode: str | None,
        format: str | None,
        style: str,
        content: bytes,
        filename: str,
        clip_start: float,
        clip_duration: float,
    ) -> None:
        super().__init__(timeout=300)
        self._worker = worker
        self._rating_key = rating_key
        self._title = title
        self._timecode = timecode
        self._duration = duration
        self._end_timecode = end_timecode
        self._format = format
        self.style = style
        self._content = content
        self._filename = filename
        # Actual start/duration used (worker-echoed via X-Clip-Start/
        # X-Clip-Duration, since a bare timecode's true duration depends on
        # render_defaults, config the bot can't see) — needed for the
        # "posted by" metadata line's exact span.
        self._clip_start = clip_start
        self._clip_duration = clip_duration
        self._add_select()

    def _add_select(self) -> None:
        options = [
            discord.SelectOption(label=label, value=value, default=(value == self.style))
            for value, label in _STYLE_OPTIONS
        ]
        # Explicit row: item order alone puts this below the decorated
        # "Post to channel" button (added first, during super().__init__())
        # — pinning rows is what actually controls layout.
        select = discord.ui.Select(
            placeholder="Change subtitle style", options=options, row=0
        )
        select.callback = self._on_style_change
        self.add_item(select)

    async def _on_style_change(self, interaction: discord.Interaction) -> None:
        new_style = None
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                new_style = item.values[0]
                self.remove_item(item)
                break
        if new_style is None or new_style == self.style:
            self._add_select()
            await interaction.response.edit_message(view=self)
            return

        await interaction.response.defer()
        # A style change on a title that was rendered without one (e.g. a
        # bare-timecode clip) can be the first request that actually needs
        # this title's subtitles — same cold-extraction risk as a fresh
        # quote search, just reached via a different route.
        slow_warning = await _slow_subtitle_warning(self._worker, self._rating_key)
        if slow_warning:
            await interaction.edit_original_response(
                content=f"Regenerating with a new style…{slow_warning}"
            )
        try:
            render_result = await self._worker.render(
                self._rating_key,
                self._timecode,
                duration=self._duration,
                end_timecode=self._end_timecode,
                format=self._format,
                style=new_style,
            )
        except httpx.HTTPError as exc:
            self._add_select()
            await interaction.edit_original_response(
                content=f"Couldn't regenerate with that style: {_error_detail(exc)}",
                view=self,
            )
            return

        self.style = render_result.style
        self._content = render_result.content
        self._filename = f"clip.{render_result.format}"
        self._clip_start = render_result.start
        self._clip_duration = render_result.duration
        self._add_select()

        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        await interaction.edit_original_response(
            content=_no_subtitles_note(new_style, render_result.style) or None,
            attachments=[file],
            view=self,
        )

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.primary, row=1)
    async def post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        content = _post_metadata_line(
            self._title,
            self._clip_start,
            self._clip_start + self._clip_duration,
            interaction.user.display_name,
        )
        await interaction.channel.send(content=content, file=file)
        button.disabled = True
        await interaction.response.edit_message(view=self)


class RandomResultView(discord.ui.View):
    """Shown by /snip random: just Shuffle (re-roll + re-render in place,
    same message-edit pattern as ClipResultView's style swap) and Post to
    channel — no confirm-the-timestamp step (doesn't apply to a random
    pick) and no style select (CLAUDE.md's random-command design keeps this
    minimal)."""

    def __init__(
        self,
        worker,
        quote: str | None,
        media: str,
        rating_key: int,
        timecode: str,
        duration: float,
        content: bytes,
        filename: str,
    ) -> None:
        super().__init__(timeout=300)
        self._worker = worker
        self._quote = quote
        self._media = media
        self._rating_key = rating_key
        self._timecode = timecode
        self._duration = duration
        self._content = content
        self._filename = filename

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        try:
            picked = await self._worker.random_quote(self._quote, self._media)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't find another match: {_error_detail(exc)}"
            )
            return

        self._rating_key = picked.rating_key
        self._timecode = str(picked.start)
        self._duration = picked.end - picked.start

        try:
            render_result = await self._worker.render(
                self._rating_key,
                self._timecode,
                duration=self._duration,
                style="classic",
            )
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't render that match: {_error_detail(exc)}"
            )
            return

        self._content = render_result.content
        self._filename = f"clip.{render_result.format}"
        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        await interaction.edit_original_response(
            content=f"**{picked.title}** — {picked.timecode}", attachments=[file], view=self
        )

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.primary, row=0)
    async def post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        await interaction.channel.send(file=file)
        button.disabled = True
        await interaction.response.edit_message(view=self)


def _validate_quote_or_timecode(
    quote: str | None, timecode: str | None, end_timecode: str | None
) -> str | None:
    """Shared by /snip movie and /snip tv so these checks can't drift between the
    two commands — the exact class of bug CLAUDE.md's library-search
    preferred_start fix (Section 2) already hit once from duplicated logic.
    Returns an error message, or None if valid.
    """
    if not quote and not timecode:
        return "Give either a `quote:` or a `timecode:`."
    if end_timecode and not timecode:
        return "`end_timecode` needs a `timecode` to start from."
    return None


async def _slow_subtitle_warning(worker, rating_key: int) -> str:
    """Best-effort hint (cheap: no ffmpeg involved on the worker side) that a
    quote search or styled render is about to fall through to a cold
    embedded-subtitle extraction, which has no fast seek and can take
    several minutes on a large file (CLAUDE.md's extraction_timeout_seconds
    build notes). A failure here should never block the real search/render
    — worst case, no warning gets shown. Shared by GifCog._generate's quote
    branch and ClipResultView's style-change handler, the two places that
    can trigger this cost.
    """
    try:
        status = await worker.subtitle_status(rating_key)
    except httpx.HTTPError:
        return ""
    if not status.likely_slow:
        return ""
    return (
        " — first time reading this title's subtitles from the video file "
        "itself, can take a few minutes for a large file (future searches "
        "will be instant)"
    )


def _error_detail(exc: httpx.HTTPError) -> str:
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return str(exc) or "the worker didn't respond in time"


def _library_results_embed(
    quote: str,
    matches: list[LibraryQuoteMatchResult],
    description: str = "Pick a film below to generate a clip from that line.",
) -> discord.Embed:
    embed = discord.Embed(title=f'Results for "{quote}"', description=description)
    for i, m in enumerate(matches, start=1):
        snippet = m.text if len(m.text) <= 200 else m.text[:197] + "..."
        embed.add_field(
            name=f"{i}. {m.title} — {m.library_name}",
            value=f"> {snippet}\n{m.timecode} · {m.score:.0f}%",
            inline=False,
        )
    return embed


class LibrarySearchView(discord.ui.View):
    """Shown by /snip search (films) and /snip tv's show-wide search
    (episodes): pick a result from cross-title matches, then funnel into the
    normal quote-confirm -> render pipeline (CLAUDE.md Section 2: "just a
    different on-ramp into the same two steps, not a separate confirmation
    UI") via GifCog._generate. Reused as-is for episodes — an episode's
    MovieResult.title already reads as "Show — S02E01 — Title", so no
    TV-specific formatting is needed here.
    """

    def __init__(
        self,
        cog: "GifCog",
        quote: str,
        matches: list[LibraryQuoteMatchResult],
        remaining_uncached: int | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self._cog = cog
        self._quote = quote
        self._matches = matches

        options = []
        for i, m in enumerate(matches):
            label = m.text if len(m.text) <= 100 else m.text[:97] + "..."
            description = f"{m.title} — {m.library_name} · {m.timecode} · {m.score:.0f}%"
            if len(description) > 100:
                description = description[:97] + "..."
            options.append(
                discord.SelectOption(label=label, description=description, value=str(i))
            )
        select = discord.ui.Select(placeholder="Choose a quote", options=options)
        select.callback = self._on_select
        self.add_item(select)

        # remaining_uncached is only ever a real int when Tier 2 (extend)
        # actually ran and hit its cap — None (extend didn't run, e.g.
        # library_sync disabled) or 0 (fully covered) both mean no button.
        if remaining_uncached:
            more_button = discord.ui.Button(
                label=f"🔍 Search {remaining_uncached} more",
                style=discord.ButtonStyle.secondary,
            )
            more_button.callback = self._on_search_more
            self.add_item(more_button)

    async def _on_search_more(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        await self._cog._run_library_search(interaction, self._quote)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                match = self._matches[int(item.values[0])]
                break
        else:
            return

        self.stop()
        await self._cog._generate(
            interaction,
            str(match.rating_key),
            self._quote,
            None,
            None,
            None,
            preferred_start=match.start,
        )


class GifCog(commands.Cog):
    snip_group = app_commands.Group(
        name="snip", description="Generate a clip from your Plex library."
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def film_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        try:
            results = await self.bot.worker.search(current)
        except httpx.HTTPError:
            return []
        choices = []
        for movie in results[:25]:
            label = f"{movie.title} ({movie.year})" if movie.year else movie.title
            label = f"{label} — {movie.library_name}"
            choices.append(
                app_commands.Choice(name=label[:100], value=str(movie.rating_key))
            )
        return choices

    async def show_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        try:
            results = await self.bot.worker.search_shows(current)
        except httpx.HTTPError:
            return []
        choices = []
        for show in results[:25]:
            label = f"{show.title} ({show.year})" if show.year else show.title
            label = f"{label} — {show.library_name}"
            choices.append(
                app_commands.Choice(name=label[:100], value=str(show.rating_key))
            )
        return choices

    async def _generate(
        self,
        interaction: discord.Interaction,
        film: str,
        quote: str | None,
        timecode: str | None,
        end_timecode: str | None,
        format: str | None,
        preferred_start: float | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            rating_key = int(film)
        except ValueError:
            await interaction.edit_original_response(
                content="Please select a result from the autocomplete suggestions."
            )
            return

        validation_error = _validate_quote_or_timecode(quote, timecode, end_timecode)
        if validation_error:
            await interaction.edit_original_response(content=validation_error)
            return

        try:
            resolved = await self.bot.worker.resolve(rating_key)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't find that film: {_error_detail(exc)}"
            )
            return

        # No separate "confirm the film" step: autocomplete already pins an
        # exact rating_key (title + year + library shown right there in the
        # picker). Per CLAUDE.md decision #4, a same-titled result from a
        # different library is surfaced as a lightweight note folded into
        # this status message, not a new click-gated confirmation step.
        library_note = f" ({resolved.library_name})"
        if quote:
            note = (
                " (both `quote` and `timecode` were given — searching by quote)"
                if timecode
                else ""
            )
            slow_warning = await _slow_subtitle_warning(self.bot.worker, rating_key)
            await interaction.edit_original_response(
                content=f"Searching {resolved.title}'s subtitles{library_note}…{note}{slow_warning}"
            )
            try:
                resolved_quote = await self.bot.worker.resolve_quote(rating_key, quote)
            except httpx.HTTPError as exc:
                await interaction.edit_original_response(
                    content=f"Couldn't search subtitles: {_error_detail(exc)}"
                )
                return

            initial_index = 0
            if preferred_start is not None and resolved_quote.matches:
                # LibrarySearchView already told us exactly which line the
                # user picked (by its start time) — find that same line in
                # this fresh per-film search instead of silently defaulting
                # to whatever this search ranks first, which need not be
                # the same line (see CLAUDE.md's library-search build notes).
                initial_index = min(
                    range(len(resolved_quote.matches)),
                    key=lambda i: abs(resolved_quote.matches[i].start - preferred_start),
                )

            match_view = QuoteMatchView(
                resolved.title,
                resolved_quote.matches,
                resolved_quote.min_score,
                resolved_quote.confident_score,
                initial_index=initial_index,
            )
            await interaction.edit_original_response(
                content=None, embed=match_view.embed(), view=match_view
            )
            await match_view.wait()

            if match_view.value is None:
                await interaction.edit_original_response(
                    content="Timed out.", embed=None, view=None
                )
                return
            if match_view.value is False:
                await interaction.edit_original_response(
                    content="Cancelled.", embed=None, view=None
                )
                return

            selected = match_view.selected
            render_timecode = str(selected.start)
            render_duration = selected.end - selected.start
            # A quote match came from real subtitle text, so burn-in has
            # something to show by default (CLAUDE.md Section 7: "subtitles
            # on when triggered by a quote search").
            default_style = "classic"
            await interaction.edit_original_response(
                content="Generating…", embed=None, view=None
            )
        else:
            render_timecode = timecode
            render_duration = None
            # A bare timecode has no known subtitle availability — default
            # to off rather than guessing at burn-in the user didn't ask for.
            default_style = "none"
            await interaction.edit_original_response(
                content=f"Generating a clip from {resolved.title}{library_note}…"
            )

        render_end_timecode = end_timecode if not quote else None

        try:
            render_result = await self.bot.worker.render(
                rating_key,
                render_timecode,
                duration=render_duration,
                end_timecode=render_end_timecode,
                format=format,
                style=default_style,
            )
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't generate the clip: {_error_detail(exc)}"
            )
            return

        filename = f"clip.{render_result.format}"
        file = discord.File(io.BytesIO(render_result.content), filename=filename)
        result_view = ClipResultView(
            self.bot.worker,
            rating_key,
            resolved.title,
            render_timecode,
            render_duration,
            render_end_timecode,
            format,
            render_result.style,
            render_result.content,
            filename,
            render_result.start,
            render_result.duration,
        )
        await interaction.edit_original_response(
            content=_no_subtitles_note(default_style, render_result.style) or None,
            attachments=[file],
            view=result_view,
        )

    @snip_group.command(
        name="movie",
        description="Generate a clip from a film at a quote or timecode.",
    )
    @app_commands.describe(
        film="The film to search for",
        quote="A line of dialogue to find (fuzzy — close is fine)",
        timecode="Timestamp, e.g. 1:23:45 or 1h23m45s",
        end_timecode="Custom clip end (timecode only, not quote) — same formats as timecode",
        format="Output format (default: gif — mp4/webm are smaller but do not autoplay in Discord)",
    )
    @app_commands.autocomplete(film=film_autocomplete)
    async def snip_movie(
        self,
        interaction: discord.Interaction,
        film: str,
        quote: str | None = None,
        timecode: str | None = None,
        end_timecode: str | None = None,
        format: Literal["gif", "mp4", "webm"] | None = None,
    ) -> None:
        await self._generate(interaction, film, quote, timecode, end_timecode, format)

    async def _run_library_search(self, interaction: discord.Interaction, quote: str) -> None:
        # Shared by /snip search itself and LibrarySearchView's "Search N
        # more" button — both stream the same worker endpoint into the same
        # message, just from different entry points (slash command vs.
        # component interaction), both already deferred by their caller.
        last_progress_edit = 0.0
        cached_matches: list[LibraryQuoteMatchResult] = []
        final_event = None
        interaction_dead = False

        async def _safe_edit(**kwargs) -> bool:
            # A capped batch of extend work can run well past Discord's
            # ~15-minute interaction-token lifetime — once
            # edit_original_response() starts raising discord.HTTPException
            # (token expired), every further edit will too, so log once and
            # signal the caller to stop trying rather than let it crash the
            # coroutine. The worker-side extraction already persists to the
            # cache regardless of whether the bot is still listening, so
            # abandoning the loop here is correct, not lossy.
            nonlocal interaction_dead
            try:
                await interaction.edit_original_response(**kwargs)
                return True
            except discord.HTTPException as exc:
                logger.warning(
                    "search-quote-extend: interaction token died mid-stream, "
                    "abandoning further updates: %s", exc,
                )
                interaction_dead = True
                return False

        try:
            async for event in self.bot.worker.search_quote_extend(quote):
                if event.type == "cached":
                    cached_matches = event.matches or []
                    if cached_matches:
                        # The "still searching" status lives in `content`
                        # (above the embed), not buried in the embed's own
                        # description below its title — a fast extend (e.g.
                        # a sidecar-subtitle title needing no ffmpeg) can
                        # replace this within a second or two, and content
                        # is what a user's eye actually lands on first.
                        ok = await _safe_edit(
                            content="🔍 **Still searching the rest of the library...** 🔍 Results below will update.",
                            embed=_library_results_embed(
                                quote, cached_matches,
                                description="Results so far — the picker below appears once the search finishes.",
                            ),
                            view=None,
                        )
                    else:
                        ok = await _safe_edit(
                            content="🔍 **Searching the rest of the library...** 🔍 No cached matches yet.",
                            embed=None,
                            view=None,
                        )
                    if not ok:
                        break
                elif event.type == "scanning":
                    # Enumerating the whole movie library against live Plex
                    # is the one real gap in this stream with no other
                    # signal — on a library of a thousand-plus titles it can
                    # take several seconds on its own, before any title is
                    # even looked at yet. Only touches `content` (same as
                    # "progress"), leaving whatever embed "cached" set alone.
                    ok = await _safe_edit(
                        content="🔍 **Checking the library for new titles...** 🔍"
                    )
                    if not ok:
                        break
                elif event.type == "progress":
                    now = asyncio.get_event_loop().time()
                    if now - last_progress_edit >= 2.0:
                        last_progress_edit = now
                        ok = await _safe_edit(
                            content=f"🔍 **Searching the rest of the library...** 🔍 ({event.index}/{event.total}) — {event.title}"
                        )
                        if not ok:
                            break
                elif event.type == "final":
                    final_event = event
        except httpx.HTTPError as exc:
            await _safe_edit(
                content=f"Couldn't search the library: {_error_detail(exc)}", embed=None, view=None
            )
            return

        if interaction_dead:
            return

        final_matches = final_event.matches if final_event else cached_matches
        if not final_matches:
            await _safe_edit(
                content="No matches in what CineSnip has indexed so far. Try `/snip movie` on a "
                "specific film first to add it, or rephrase your quote.",
                embed=None,
                view=None,
            )
            return

        remaining_uncached = final_event.remaining_uncached if final_event else None
        view = LibrarySearchView(self, quote, final_matches, remaining_uncached=remaining_uncached)
        await _safe_edit(
            content=None,
            embed=_library_results_embed(quote, final_matches),
            view=view,
        )

    @snip_group.command(
        name="search",
        description="Search your whole library for a quote, no film needed.",
    )
    @app_commands.describe(
        quote="A line of dialogue to find — searches cached films first, then the rest of "
        "the library automatically if library sync is enabled"
    )
    async def snip_search(self, interaction: discord.Interaction, quote: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._run_library_search(interaction, quote)

    @snip_group.command(
        name="random",
        description="Pick a random line from your library, optionally matching a word/phrase.",
    )
    @app_commands.describe(
        quote="Restrict to lines containing this as a whole word/phrase (omit for a fully random line)",
        media="Which libraries to search (default: all)",
    )
    async def snip_random(
        self,
        interaction: discord.Interaction,
        quote: str | None = None,
        media: Literal["movie", "tv", "all"] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        effective_media = media or "all"

        try:
            picked = await self.bot.worker.random_quote(quote, effective_media)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't find a match: {_error_detail(exc)}"
            )
            return

        render_timecode = str(picked.start)
        render_duration = picked.end - picked.start

        try:
            render_result = await self.bot.worker.render(
                picked.rating_key,
                render_timecode,
                duration=render_duration,
                style="classic",
            )
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't generate the clip: {_error_detail(exc)}"
            )
            return

        filename = f"clip.{render_result.format}"
        file = discord.File(io.BytesIO(render_result.content), filename=filename)
        view = RandomResultView(
            self.bot.worker,
            quote,
            effective_media,
            picked.rating_key,
            render_timecode,
            render_duration,
            render_result.content,
            filename,
        )
        await interaction.edit_original_response(
            content=f"**{picked.title}** — {picked.timecode}",
            attachments=[file],
            view=view,
        )

    @snip_group.command(
        name="tv",
        description="Generate a clip from a TV episode at a quote or timecode.",
    )
    @app_commands.describe(
        show="The show to search for",
        season="Season number (requires episode)",
        episode="Episode number within the season (requires season)",
        quote="A line of dialogue to find (fuzzy — close is fine); omit season/episode to "
        "search the whole show",
        timecode="Timestamp, e.g. 1:23:45 or 1h23m45s — requires season/episode",
        end_timecode="Custom clip end (timecode only, not quote) — same formats as timecode",
        format="Output format (default: gif — mp4/webm are smaller but do not autoplay in Discord)",
    )
    @app_commands.autocomplete(show=show_autocomplete)
    async def snip_tv(
        self,
        interaction: discord.Interaction,
        show: str,
        season: int | None = None,
        episode: int | None = None,
        quote: str | None = None,
        timecode: str | None = None,
        end_timecode: str | None = None,
        format: Literal["gif", "mp4", "webm"] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            show_rating_key = int(show)
        except ValueError:
            await interaction.edit_original_response(
                content="Please select a show from the autocomplete suggestions."
            )
            return

        if (season is None) != (episode is None):
            await interaction.edit_original_response(
                content="Give both `season` and `episode`, or neither (to search the "
                "whole show)."
            )
            return

        validation_error = _validate_quote_or_timecode(quote, timecode, end_timecode)
        if validation_error:
            await interaction.edit_original_response(content=validation_error)
            return

        if timecode and season is None:
            # There's no "the show's timeline" to seek within — a timecode
            # only makes sense once a specific episode is pinned.
            await interaction.edit_original_response(
                content="A `timecode` needs a specific episode — give `season` and "
                "`episode`, or use `quote` to search the whole show."
            )
            return

        if season is not None:
            try:
                resolved = await self.bot.worker.resolve_episode(show_rating_key, season, episode)
            except httpx.HTTPError as exc:
                await interaction.edit_original_response(
                    content=f"Couldn't find that episode: {_error_detail(exc)}"
                )
                return
            await self._generate(
                interaction, str(resolved.rating_key), quote, timecode, end_timecode, format
            )
            return

        # No episode given — quote is guaranteed at this point (a bare
        # timecode with no episode was already rejected above, and quote
        # XOR timecode was already validated).
        await interaction.edit_original_response(
            content=f'Searching every episode for "{quote}" — this can take a moment for '
            "episodes not seen before…"
        )
        try:
            result = await self.bot.worker.search_episodes_quote(show_rating_key, quote)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't search that show: {_error_detail(exc)}"
            )
            return

        if not result.matches:
            await interaction.edit_original_response(
                content="No matching line found in that show's episodes."
            )
            return

        view = LibrarySearchView(self, quote, result.matches)
        await interaction.edit_original_response(
            embed=_library_results_embed(
                quote,
                result.matches,
                description="Pick an episode below to generate a clip from that line.",
            ),
            view=view,
        )
