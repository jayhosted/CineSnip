from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Awaitable, Callable, Literal

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from app.bot.worker_client import (
    LibraryQuoteMatchResult,
    QuoteMatchResult,
    RandomQuoteResult,
    SubtitleEntryResult,
)

logger = logging.getLogger("cinesnip.bot.gif")

# Discord select menus cap at 25 options; the worker now fetches up to
# quote_match.fetch_limit candidates (default 50, app/settings.py) per
# search — QuoteMatchView and LibrarySearchView below hold that full batch
# and page through it in place _PAGE_SIZE at a time (CineSnip issue #7),
# rather than truncating to what one select/embed can show.
_PAGE_SIZE = 8


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


def _pagination_footer(page: int, total_pages: int, truncated: bool) -> str | None:
    """Shared footer text for QuoteMatchView and LibrarySearchView/
    _library_results_embed: "Page X of Y" once there's more than one page,
    plus a short note on the LAST page only when the worker's fetch hit
    quote_match.fetch_limit — so a user who paged all the way through
    knows the list they just finished browsing might not be everything,
    without repeating the note on every page (issue #7 follow-up)."""
    parts = []
    if total_pages > 1:
        parts.append(f"Page {page} of {total_pages}")
    if truncated and page == total_pages:
        parts.append("more results may exist — try a more specific quote")
    return " · ".join(parts) if parts else None


def _match_embed(
    title: str,
    match: QuoteMatchResult,
    min_score: float,
    confident_score: float,
    page: int,
    total_pages: int,
    truncated: bool,
) -> discord.Embed:
    lines = [f"> {line}" for line in match.context_before]
    lines.append(f"> **{match.text}**")
    lines.extend(f"> {line}" for line in match.context_after)

    embed = discord.Embed(title=title, description="\n".join(lines))
    label = _confidence_label(match.score, min_score, confident_score)
    embed.add_field(name="Timecode", value=match.timecode)
    embed.add_field(name="Confidence", value=f"{match.score:.0f}% ({label})")
    footer = _pagination_footer(page, total_pages, truncated)
    if footer:
        embed.set_footer(text=footer)
    return embed


class QuoteMatchView(discord.ui.View):
    """Confirm/cancel a quote match, with a select menu to browse
    alternatives. Per CLAUDE.md decision #4, browsing alternatives never
    stops the view or re-asks the film step — only Confirm/Cancel does.

    Holds the FULL fetched batch (up to quote_match.fetch_limit candidates,
    already ranked by the worker) and pages through it in place with
    Next/Previous buttons, _PAGE_SIZE at a time — Discord's own 25-option
    select cap means each page's select mirrors only that page's slice,
    never the whole batch (issue #7). The picker is shown immediately
    regardless of how confident the top match is: true pagination makes
    browsing cheap, so hiding it behind a "Show other matches" button (an
    earlier design, before this class could page) just adds a click.

    `title` is normally a fixed string — every candidate line comes from
    the same already-known film/episode (/snip movie, /snip tv with a
    specific episode). Pass `title=None` when candidates can come from
    DIFFERENT titles instead (/snip tv's whole-show search, whose matches
    are `LibraryQuoteMatchResult`s each carrying their own `.title`/
    `.rating_key`) — the embed then reads the title off whichever match
    is currently selected, and the caller reads `.selected.rating_key`
    after Confirm instead of already knowing it up front.
    """

    def __init__(
        self,
        title: str | None,
        matches: list[QuoteMatchResult] | list[LibraryQuoteMatchResult],
        min_score: float,
        confident_score: float,
        initial_index: int = 0,
        truncated: bool = False,
    ) -> None:
        # 120s was calibrated for scanning a fixed 8 results — with up to
        # quote_match.fetch_limit (default 50) now possible across several
        # pages, that timed out mid-browse (issue #7 follow-up). This is an
        # idle timeout, not a session cap — discord.py resets it on every
        # click (see _scheduled_task in its View), so 10 minutes only
        # matters if the user walks away entirely.
        super().__init__(timeout=600)
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
        # Land on the page containing the pre-selected index, so a
        # LibrarySearchView-driven re-search opens on the right page
        # instead of always page 0.
        self._page = initial_index // _PAGE_SIZE
        self._truncated = truncated

        self._select: discord.ui.Select | None = None
        self._prev_button: discord.ui.Button | None = None
        self._next_button: discord.ui.Button | None = None

        if len(matches) > 1:
            self._add_select()
            if len(matches) > _PAGE_SIZE:
                self._add_page_buttons()

    @property
    def selected(self) -> QuoteMatchResult | LibraryQuoteMatchResult:
        return self.matches[self.index]

    def _total_pages(self) -> int:
        return max(1, (len(self.matches) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def embed(self) -> discord.Embed:
        title = self._title if self._title is not None else self.selected.title
        return _match_embed(
            title,
            self.selected,
            self.min_score,
            self.confident_score,
            self._page + 1,
            self._total_pages(),
            self._truncated,
        )

    def _add_select(self) -> None:
        start = self._page * _PAGE_SIZE
        options = [
            discord.SelectOption(
                label=(m.text if len(m.text) <= 100 else m.text[:97] + "..."),
                description=f"{m.timecode} · {m.score:.0f}%",
                value=str(start + offset),
                default=(start + offset == self.index),
            )
            for offset, m in enumerate(self.matches[start : start + _PAGE_SIZE])
        ]
        select = discord.ui.Select(placeholder="Choose a match", options=options, row=1)
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    def _add_page_buttons(self) -> None:
        total_pages = self._total_pages()
        prev_button = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self._page == 0,
            row=2,
        )
        prev_button.callback = self._on_previous
        self._prev_button = prev_button
        self.add_item(prev_button)

        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self._page >= total_pages - 1,
            row=2,
        )
        next_button.callback = self._on_next
        self._next_button = next_button
        self.add_item(next_button)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        self._page = max(0, min(self._page + delta, self._total_pages() - 1))
        if self._select is not None:
            self.remove_item(self._select)
            self._select = None
        if self._prev_button is not None:
            self.remove_item(self._prev_button)
            self._prev_button = None
        if self._next_button is not None:
            self.remove_item(self._next_button)
            self._next_button = None
        self._add_select()
        self._add_page_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if self._select is None:
            return
        self.index = int(self._select.values[0])
        # SelectOption.default is baked in at construction time and never
        # updates on its own — leaving the old select attached would keep
        # showing the FIRST-ever-picked option as "selected" regardless of
        # self.index. Rebuild the select from scratch (same page) so its
        # default always matches reality.
        self.remove_item(self._select)
        self._select = None
        self._add_select()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=0)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=0)
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


def _format_clip_position(seconds: float) -> str:
    """Like _format_unit_timecode, but keeps one decimal place on the
    seconds component instead of rounding to a whole second — used only by
    _clip_span_line, where start/end/duration are shown together and must
    stay arithmetically consistent (start + duration == end exactly, not
    just after independent rounding). _format_unit_timecode's whole-second
    rounding is intentional and unchanged for its own callers (e.g. the
    posted-message metadata line), where sub-second precision isn't
    useful."""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    hours, minutes = int(hours), int(minutes)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:04.1f}s"
    if minutes:
        return f"{minutes}m{secs:04.1f}s"
    return f"{secs:.1f}s"


def _clip_span_line(start: float, duration: float) -> str:
    """One-line confirmation of the clip's current span within the source
    film — shown after every ClipEditView edit action (nudge/merge/custom/
    subtitle edit/style change) so a change registers even when the new
    frame looks visually similar to the old one (e.g. a <1s nudge, or a
    merge onto an adjacent line with a similar dark shot)."""
    end = start + duration
    return f"⏱ {_format_clip_position(start)} – {_format_clip_position(end)} ({duration:.1f}s)"


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


def _entries_in_window(
    entries: list[SubtitleEntryResult], clip_start: float, clip_end: float
) -> list[SubtitleEntryResult]:
    """Subtitle entries overlapping the clip's current [clip_start, clip_end)
    window, in time order — used to build the Edit Subs modal and to decide
    which entries are "in the clip" for override purposes. Unlike the
    worker's own entries_in_window(), this doesn't rebase/trim to
    clip-relative time: the bot only needs to know *which* entries are in
    play, not render them."""
    window = [e for e in entries if e.end > clip_start and e.start < clip_end]
    return sorted(window, key=lambda e: e.start)


_EDIT_BLOCK_SEPARATOR = "\n---\n"


def _format_edit_blocks(
    entries: list[SubtitleEntryResult], overrides: dict[int, str | None]
) -> str:
    """Build the Edit Subs modal's prefilled text: each in-window entry's
    *current* text (its override applied if one exists, blank if
    suppressed), joined by the same `---`-only-line separator
    `_parse_edit_blocks` splits on."""
    blocks = []
    for entry in entries:
        if entry.index in overrides:
            override = overrides[entry.index]
            blocks.append(override if override is not None else "")
        else:
            blocks.append(entry.text)
    return _EDIT_BLOCK_SEPARATOR.join(blocks)


def _parse_edit_blocks(
    raw_text: str,
    entries: list[SubtitleEntryResult],
    overrides: dict[int, str | None],
) -> dict[int, str | None]:
    """Parse a submitted Edit Subs modal back into an updated overrides dict.
    Block N (split on `_EDIT_BLOCK_SEPARATOR`) maps to entries[N] — a
    locked, position-based mapping; the modal never lets the user add,
    remove, or reorder blocks. Returns a new dict built from `overrides`: a
    blank block suppresses its entry, a block matching the entry's original
    text clears any existing override, and any other text sets/replaces the
    override."""
    blocks = raw_text.split(_EDIT_BLOCK_SEPARATOR)
    result = dict(overrides)
    for entry, block in zip(entries, blocks):
        text = block.strip()
        if not text:
            result[entry.index] = None
        elif text == entry.text:
            result.pop(entry.index, None)
        else:
            result[entry.index] = text
    return result


def _find_merge_previous(
    entries: list[SubtitleEntryResult], clip_start: float
) -> SubtitleEntryResult | None:
    """The entry immediately before the clip's current start, if any — its
    own *start* becomes the new clip start when "Merge Previous" is
    pressed (not its end: entries_in_window()'s strict overlap check would
    exclude an entry whose end lands exactly on the new clip_start, so the
    merged-in line's text would never actually render)."""
    candidates = [e for e in entries if e.end <= clip_start]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.end)


def _find_merge_next(
    entries: list[SubtitleEntryResult], clip_end: float
) -> SubtitleEntryResult | None:
    """The entry immediately after the clip's current end, if any — its own
    *end* becomes the new clip end when "Merge Next" is pressed (not its
    start, for the same exclusion reason as _find_merge_previous)."""
    candidates = [e for e in entries if e.start >= clip_end]
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.start)


def _find_unmerge_previous(
    entries: list[SubtitleEntryResult], clip_start: float, clip_end: float
) -> SubtitleEntryResult | None:
    """The trim counterpart to Merge Previous: the second-earliest entry
    currently in the clip's window, if there are at least two — its own
    start becomes the new clip start when "Unmerge Previous" is pressed,
    dropping whichever line is currently first (however it got there,
    whether from an earlier Merge Previous or the clip's own original
    span)."""
    window = _entries_in_window(entries, clip_start, clip_end)
    if len(window) < 2:
        return None
    return window[1]


def _find_unmerge_next(
    entries: list[SubtitleEntryResult], clip_start: float, clip_end: float
) -> SubtitleEntryResult | None:
    """The trim counterpart to Merge Next: the second-latest entry
    currently in the clip's window, if there are at least two — its own
    end becomes the new clip end when "Unmerge Next" is pressed, dropping
    whichever line is currently last."""
    window = _entries_in_window(entries, clip_start, clip_end)
    if len(window) < 2:
        return None
    return window[-2]


def _merge_previous_n(
    entries: list[SubtitleEntryResult], clip_start: float, count: int
) -> float | None:
    """Repeat _find_merge_previous's single-line jump `count` times,
    walking further back one line at a time (each step's new boundary
    becomes the next step's search point) — the underlying operation for
    a multi-line merge (_MergeCountModal). Stops early if fewer than
    `count` lines exist before clip_start; returns None if none exist at
    all (0 lines walked), otherwise the resulting new clip_start."""
    current = clip_start
    result = None
    for _ in range(count):
        entry = _find_merge_previous(entries, current)
        if entry is None:
            break
        current = entry.start
        result = entry.start
    return result


def _merge_next_n(
    entries: list[SubtitleEntryResult], clip_end: float, count: int
) -> float | None:
    """_merge_previous_n's mirror for the end boundary."""
    current = clip_end
    result = None
    for _ in range(count):
        entry = _find_merge_next(entries, current)
        if entry is None:
            break
        current = entry.end
        result = entry.end
    return result


def _merge_context_block(
    entries: list[SubtitleEntryResult],
    clip_start: float,
    clip_end: float,
    context: int = 2,
) -> str:
    """A short readout of the lines just outside (and inside) the clip's
    current window, each with its own duration, so picking Merge Previous/
    Next or a specific merge count isn't a guess about what's actually
    there. Rendered into the result embed's footer (see ClipEditView's
    _build_merge_embed), not the message's plain content — Discord never
    markdown-parses embed footer text, so this doesn't need a code fence
    to protect against subtitle text's own dashes/punctuation being
    reinterpreted as blockquotes/bullet lists the way plain content would.
    Returns "" if there's nothing to show (no entries at all)."""
    before = sorted((e for e in entries if e.end <= clip_start), key=lambda e: e.start)
    after = sorted((e for e in entries if e.start >= clip_end), key=lambda e: e.start)
    window = _entries_in_window(entries, clip_start, clip_end)

    def _line(e: SubtitleEntryResult, marker: str) -> str:
        text = e.text.replace("\n", " ")
        return f"{marker} {text} ({e.end - e.start:.1f}s)"

    lines = [_line(e, ">") for e in before[-context:]]
    lines += [_line(e, "»") for e in window]
    lines += [_line(e, ">") for e in after[:context]]
    return "\n".join(lines)


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
        # Deferred before the send: interaction.channel.send() can fail
        # (Discord's own upload size cap, a channel permission change,
        # etc.) — with no prior response, that exception would leave the
        # interaction never acknowledged at all, surfacing to the user as
        # Discord's generic "didn't respond in time" with no explanation
        # and the Post button still (wrongly) enabled for a retry that
        # would just fail the same way.
        await interaction.response.defer()
        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        content = _post_metadata_line(
            self._title,
            self._clip_start,
            self._clip_start + self._clip_duration,
            interaction.user.display_name,
        )
        try:
            await interaction.channel.send(content=content, file=file)
        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                content=f"Couldn't post that clip: {_error_detail_from_discord(exc)}"
            )
            return
        button.disabled = True
        await interaction.edit_original_response(view=self)


class _CustomDurationModal(discord.ui.Modal, title="Custom duration"):
    start_input = discord.ui.TextInput(label="Start (e.g. 1:23:45 or 1h23m45s)", required=True)
    end_input = discord.ui.TextInput(label="End (same format)", required=True)

    def __init__(self, view: "ClipEditView") -> None:
        super().__init__()
        self._view = view
        self.start_input.default = str(round(view._clip_start, 2))
        self.end_input.default = str(round(view._clip_end, 2))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Send the raw timecode strings straight to /render's existing
        # timecode/end_timecode fields rather than parsing them here —
        # timecode format support lives server-side only (RenderRequest's
        # field comments in app/worker/api.py), so the bot never imports
        # app.worker.ffmpeg.parse_timecode. A malformed value comes back as
        # a 422, surfaced the same way any other render failure is.
        await interaction.response.defer()
        await self._view._re_render_timecode(
            interaction, str(self.start_input.value), str(self.end_input.value)
        )


class _MergeCountModal(discord.ui.Modal, title="Merge N lines"):
    # "Previous lines" / "Next lines" are totals from the clip's original
    # (pre-edit) span, not "add N more from here" — prefilled with
    # whatever was last submitted so lowering the number (e.g. 2 -> 0)
    # actually shrinks the clip back down, undoing earlier merges from
    # this same modal, rather than only ever being able to add more.
    previous_input = discord.ui.TextInput(label="Previous lines", default="0", required=True, max_length=3)
    next_input = discord.ui.TextInput(label="Next lines", default="0", required=True, max_length=3)

    def __init__(self, view: "ClipEditView") -> None:
        super().__init__()
        self._view = view
        self.previous_input.default = str(view._last_merge_previous_count)
        self.next_input.default = str(view._last_merge_next_count)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            previous_count = int(str(self.previous_input.value))
            next_count = int(str(self.next_input.value))
        except ValueError:
            await interaction.response.send_message(
                "Previous/Next lines must be whole numbers.", ephemeral=True
            )
            return
        if previous_count < 0 or next_count < 0:
            await interaction.response.send_message(
                "Previous/Next lines can't be negative.", ephemeral=True
            )
            return

        await interaction.response.defer()
        entries = await self._view._ensure_entries()
        new_start = self._view._merge_origin_start
        if previous_count:
            result = _merge_previous_n(entries, self._view._merge_origin_start, previous_count)
            if result is not None:
                new_start = result
        new_end = self._view._merge_origin_end
        if next_count:
            result = _merge_next_n(entries, self._view._merge_origin_end, next_count)
            if result is not None:
                new_end = result
        self._view._last_merge_previous_count = previous_count
        self._view._last_merge_next_count = next_count
        await self._view._re_render(interaction, new_start, new_end)


class _EditSubsModal(discord.ui.Modal, title="Edit subtitles"):
    text_input = discord.ui.TextInput(
        label="Subtitle lines",
        placeholder="Separate entries with a line of just ---; blank a block to hide that line",
        style=discord.TextStyle.paragraph,
        required=False,
    )

    def __init__(self, view: "ClipEditView", window: list[SubtitleEntryResult]) -> None:
        super().__init__()
        self._view = view
        self._window = window
        self.text_input.default = _format_edit_blocks(window, view.overrides)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._view.overrides = _parse_edit_blocks(
            str(self.text_input.value), self._window, self._view.overrides
        )
        await interaction.response.defer()
        await self._view._re_render(interaction, self._view._clip_start, self._view._clip_end)


class ClipEditView(ClipResultView):
    """ClipResultView plus in-place duration/merge/subtitle editing controls
    (issue #5): three collapsible category toggles (⏱ Duration, 💬
    Subtitles, 🔀 Merge Subs) between the existing Style select and Post button.
    Merge is its own top-level toggle rather than nested under Duration —
    moved there after real-world testing showed Merge Previous/Next were
    easy to miss when buried in a second category. Edit state (current
    span, per-line text overrides) lives only on this view instance, the
    same approach QuoteMatchView already uses. Row layout is fixed
    regardless of which category (if any) is open, so Post never moves:
    row 0 Style select, row 1 category toggles, row 2 the open category's
    controls, row 4 Post — always last.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # ClipResultView's 300s (5 min) timeout fits its own one-shot
        # style-pick-then-post flow, but an editing session genuinely
        # takes longer — several nudge/merge/subtitle-edit round trips,
        # each with real thinking time in between. Past the timeout,
        # discord.py silently stops routing clicks on this message to any
        # callback, so the next click just fails with Discord's own
        # generic "didn't respond in time" — extending it here is the fix
        # for that, not a render-performance issue.
        self.timeout = 1800
        self.overrides: dict[int, str | None] = {}
        self._all_entries: list[SubtitleEntryResult] | None = None
        self._open_category: str | None = None
        self._category_buttons: list[discord.ui.Button] = []
        # Surrounding-lines readout for the Merge Subs screen (issue #5
        # follow-up) — recomputed by _open_merge() each time that category
        # opens or a render happens while it's open, so Merge/Unmerge/
        # Merge Count aren't a guess about what's actually adjacent.
        self._merge_context_text: str = ""
        # Fixed anchor for _MergeCountModal: the clip's span as first
        # rendered, before any edit. _MergeCountModal's counts are always
        # "N lines from here", not "N more from wherever the clip currently
        # is" — that's what lets typing a smaller number than last time
        # actually shrink the clip back down (e.g. 2 -> 0 undoes both),
        # rather than only ever being able to add more.
        self._merge_origin_start: float = self._clip_start
        self._merge_origin_end: float = self._clip_end
        self._last_merge_previous_count: int = 0
        self._last_merge_next_count: int = 0
        self._add_category_toggles()

    def _add_category_toggles(self) -> None:
        duration_button = discord.ui.Button(
            label="⏱ Duration", style=discord.ButtonStyle.secondary, row=1
        )
        duration_button.callback = self._on_toggle_duration
        subtitles_button = discord.ui.Button(
            label="💬 Subtitles", style=discord.ButtonStyle.secondary, row=1
        )
        subtitles_button.callback = self._on_toggle_subtitles
        # Its own always-visible toggle (not nested under Duration) per
        # real-world testing feedback: Merge Previous/Next were easy to
        # miss buried inside the Duration category, and users expect a
        # snap-to-adjacent-line action to be as prominent as the category
        # toggles themselves.
        merge_button = discord.ui.Button(
            label="🔀 Merge Subs", style=discord.ButtonStyle.secondary, row=1
        )
        merge_button.callback = self._on_toggle_merge
        self.add_item(duration_button)
        self.add_item(subtitles_button)
        self.add_item(merge_button)

    async def _on_style_change(self, interaction: discord.Interaction) -> None:
        # Overrides ClipResultView._on_style_change: the parent re-renders
        # against the *original* construction-time timecode/duration/
        # end_timecode and never passes subtitle_overrides, which would
        # silently revert any nudge/merge/custom span and drop any Edit
        # Subs text overrides made since the clip was first generated.
        # Every edit action on this view goes through the shared
        # _re_render(interaction, start, end) coroutine instead — this
        # override only extracts the newly-selected style from the Select
        # widget (UI bookkeeping _re_render doesn't do) and then delegates
        # to it with the *current* span, so the render call itself is
        # never duplicated.
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
        # quote search, just reached via a different route (CLAUDE.md
        # Section 2's upfront slow-extraction warning). Folded into
        # _re_render's status_message rather than a separate edit here, so
        # there's still exactly one place showing pre-render status text.
        slow_warning = await _slow_subtitle_warning(self._worker, self._rating_key)
        status_message = (
            f"Regenerating with a new style…{slow_warning}" if slow_warning else "Generating…"
        )
        self.style = new_style
        self._add_select()
        await self._re_render(
            interaction, self._clip_start, self._clip_end, status_message=status_message
        )

    async def _ensure_entries(self) -> list[SubtitleEntryResult]:
        if self._all_entries is None:
            try:
                entries = await self._worker.subtitles(self._rating_key)
            except httpx.HTTPError:
                # Don't cache the failure as a successful empty result — a
                # transient error (e.g. a slow cold extraction that timed
                # out) would otherwise permanently disable Merge
                # Previous/Next and Edit Subs for the rest of this edit
                # session. Leave self._all_entries as None so the next
                # click retries.
                return []
            self._all_entries = entries
        return self._all_entries

    def _clear_category_rows(self) -> None:
        for button in self._category_buttons:
            self.remove_item(button)
        self._category_buttons = []

    async def _open_duration(self) -> None:
        self._clear_category_rows()
        self._open_category = "duration"

        # Full two-sided control (spec requirement): each boundary (start,
        # end) must be nudgeable in BOTH directions, not just one — the
        # tvgif-style one-directional-extend limitation this feature is
        # explicitly meant to fix. That's 2 boundaries x 2 directions = 4
        # nudge buttons per magnitude. The spec lists three magnitudes
        # (0.5s/1s/5s), but a Discord view row caps out at 5 buttons; with
        # Merge Previous/Next now living in their own category (moved out
        # per real-world testing feedback) this row only needs to fit the
        # 4 nudges + Custom, so a single magnitude (1s, the spec's middle
        # value) with full symmetry is what fits — dropping 0.5s and 5s
        # entirely rather than compromising symmetry, per the earlier
        # review ruling that both-directions-per-boundary outranks
        # magnitude count. Custom (free-text modal) remains available for
        # any other span.
        # Arrows show which way the boundary moves along the timeline
        # (← earlier, → later), not the sign of the delta applied to its
        # timestamp — "Start +1s" reads as "extend the clip" but actually
        # moves the start point later (shortening the clip), which testing
        # showed was genuinely confusing. The arrow direction matches the
        # visible effect directly: ← always makes more of the source appear,
        # → always trims it, regardless of which boundary it's attached to.
        nudges = [
            ("Start ← 1s", -1.0, "start"),
            ("Start → 1s", 1.0, "start"),
            ("End ← 1s", -1.0, "end"),
            ("End → 1s", 1.0, "end"),
        ]
        for label, delta, side in nudges:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=2)
            button.callback = self._make_nudge_callback(delta, side)
            self._category_buttons.append(button)
            self.add_item(button)

        custom_button = discord.ui.Button(label="Custom", style=discord.ButtonStyle.secondary, row=2)
        custom_button.callback = self._on_custom_duration
        self._category_buttons.append(custom_button)
        self.add_item(custom_button)

    def _build_merge_embed(self, filename: str, description: str | None) -> discord.Embed:
        """Discord's message layout is fixed — content, then embeds, then
        the attached file — so plain content can never appear below the
        attached gif. Used only while Merge Subs is open: the embed's
        image slot shows the gif, and its footer (always rendered below
        the image, right above the button rows) carries the
        surrounding-lines readout — moved here after real-world feedback
        that it made more sense next to the merge buttons than above the
        gif with the rest of the info text."""
        embed = discord.Embed(description=description) if description else discord.Embed()
        embed.set_image(url=f"attachment://{filename}")
        if self._merge_context_text:
            embed.set_footer(text=self._merge_context_text[:2048])
        return embed

    async def _open_merge(self) -> None:
        self._clear_category_rows()
        self._open_category = "merge"
        entries = await self._ensure_entries()
        self._merge_context_text = _merge_context_block(entries, self._clip_start, self._clip_end)

        prev_entry = _find_merge_previous(entries, self._clip_start)
        prev_button = discord.ui.Button(
            label="Merge Previous", style=discord.ButtonStyle.secondary, row=2,
            disabled=prev_entry is None,
        )
        prev_button.callback = self._on_merge_previous
        self._category_buttons.append(prev_button)
        self.add_item(prev_button)

        next_entry = _find_merge_next(entries, self._clip_end)
        next_button = discord.ui.Button(
            label="Merge Next", style=discord.ButtonStyle.secondary, row=2,
            disabled=next_entry is None,
        )
        next_button.callback = self._on_merge_next
        self._category_buttons.append(next_button)
        self.add_item(next_button)

        unmerge_prev_entry = _find_unmerge_previous(entries, self._clip_start, self._clip_end)
        unmerge_prev_button = discord.ui.Button(
            label="Unmerge Previous", style=discord.ButtonStyle.secondary, row=2,
            disabled=unmerge_prev_entry is None,
        )
        unmerge_prev_button.callback = self._on_unmerge_previous
        self._category_buttons.append(unmerge_prev_button)
        self.add_item(unmerge_prev_button)

        unmerge_next_entry = _find_unmerge_next(entries, self._clip_start, self._clip_end)
        unmerge_next_button = discord.ui.Button(
            label="Unmerge Next", style=discord.ButtonStyle.secondary, row=2,
            disabled=unmerge_next_entry is None,
        )
        unmerge_next_button.callback = self._on_unmerge_next
        self._category_buttons.append(unmerge_next_button)
        self.add_item(unmerge_next_button)

        count_button = discord.ui.Button(
            label="Merge Count…", style=discord.ButtonStyle.secondary, row=2,
            disabled=prev_entry is None and next_entry is None,
        )
        count_button.callback = self._on_merge_count
        self._category_buttons.append(count_button)
        self.add_item(count_button)

    async def _open_subtitles(self) -> None:
        self._clear_category_rows()
        self._open_category = "subtitles"
        entries = await self._ensure_entries()
        window = _entries_in_window(entries, self._clip_start, self._clip_end)
        edit_button = discord.ui.Button(
            label="Edit Subs", style=discord.ButtonStyle.secondary, row=2,
            disabled=not window,
        )
        edit_button.callback = self._on_edit_subs
        self._category_buttons.append(edit_button)
        self.add_item(edit_button)

    async def _on_toggle_duration(self, interaction: discord.Interaction) -> None:
        # Collapsing needs no worker call, so it can go straight through —
        # but opening can hit _ensure_entries()'s cold-extraction path
        # (CLAUDE.md Section 2's upfront slow-extraction warning), which can
        # take tens of seconds to minutes and would blow Discord's 3-second
        # ack window if awaited before response.defer()/edit_message().
        await interaction.response.defer()
        if self._open_category == "duration":
            self._clear_category_rows()
            self._open_category = None
            await interaction.edit_original_response(view=self)
            return

        slow_warning = await _slow_subtitle_warning(self._worker, self._rating_key)
        if slow_warning:
            await interaction.edit_original_response(
                content=f"Loading duration controls…{slow_warning}", embed=None
            )
        await self._open_duration()
        # embed=None clears a Merge Subs embed left over from switching
        # straight from that category into this one.
        await interaction.edit_original_response(content=None, embed=None, view=self)

    async def _on_toggle_subtitles(self, interaction: discord.Interaction) -> None:
        # See _on_toggle_duration — same defer-before-possibly-slow-fetch
        # shape, mirroring _on_style_change's existing warning pattern.
        await interaction.response.defer()
        if self._open_category == "subtitles":
            self._clear_category_rows()
            self._open_category = None
            await interaction.edit_original_response(view=self)
            return

        slow_warning = await _slow_subtitle_warning(self._worker, self._rating_key)
        if slow_warning:
            await interaction.edit_original_response(
                content=f"Loading subtitle controls…{slow_warning}", embed=None
            )
        await self._open_subtitles()
        # embed=None clears a Merge Subs embed left over from switching
        # straight from that category into this one.
        await interaction.edit_original_response(content=None, embed=None, view=self)

    async def _on_toggle_merge(self, interaction: discord.Interaction) -> None:
        # See _on_toggle_duration — same defer-before-possibly-slow-fetch
        # shape, mirroring _on_style_change's existing warning pattern.
        await interaction.response.defer()
        if self._open_category == "merge":
            self._clear_category_rows()
            self._open_category = None
            # Collapsing Merge Subs means going back to the plain
            # content-only layout — clear the embed that carried the gif
            # and readout while it was open.
            await interaction.edit_original_response(content=None, embed=None, view=self)
            return

        slow_warning = await _slow_subtitle_warning(self._worker, self._rating_key)
        if slow_warning:
            await interaction.edit_original_response(
                content=f"Loading merge options…{slow_warning}", embed=None
            )
        await self._open_merge()
        embed = self._build_merge_embed(self._filename, description=None)
        await interaction.edit_original_response(content=None, embed=embed, view=self)

    def _make_nudge_callback(self, delta: float, side: str):
        async def _callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            new_start = self._clip_start + delta if side == "start" else self._clip_start
            new_end = self._clip_end + delta if side == "end" else self._clip_end
            await self._re_render(interaction, new_start, new_end)
        return _callback

    async def _on_merge_previous(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        entries = await self._ensure_entries()
        entry = _find_merge_previous(entries, self._clip_start)
        if entry is None:
            return
        await self._re_render(interaction, entry.start, self._clip_end)

    async def _on_merge_next(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        entries = await self._ensure_entries()
        entry = _find_merge_next(entries, self._clip_end)
        if entry is None:
            return
        await self._re_render(interaction, self._clip_start, entry.end)

    async def _on_unmerge_previous(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        entries = await self._ensure_entries()
        entry = _find_unmerge_previous(entries, self._clip_start, self._clip_end)
        if entry is None:
            return
        await self._re_render(interaction, entry.start, self._clip_end)

    async def _on_unmerge_next(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        entries = await self._ensure_entries()
        entry = _find_unmerge_next(entries, self._clip_start, self._clip_end)
        if entry is None:
            return
        await self._re_render(interaction, self._clip_start, entry.end)

    async def _on_merge_count(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_MergeCountModal(self))

    async def _on_custom_duration(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_CustomDurationModal(self))

    async def _on_edit_subs(self, interaction: discord.Interaction) -> None:
        entries = await self._ensure_entries()
        window = _entries_in_window(entries, self._clip_start, self._clip_end)
        await interaction.response.send_modal(_EditSubsModal(self, window))

    @property
    def _clip_end(self) -> float:
        return self._clip_start + self._clip_duration

    async def _re_render(
        self,
        interaction: discord.Interaction,
        start: float,
        end: float,
        status_message: str = "Generating…",
    ) -> None:
        # Shown immediately (the interaction is already deferred by every
        # caller before this runs) rather than leaving the message
        # unchanged until the worker responds — a nudge/merge/style-change
        # can take a few real seconds (re-encode, sometimes a subtitle
        # extraction), which read as an unresponsive click with no
        # feedback in between.
        await interaction.edit_original_response(content=status_message, view=self)
        requested_style = self.style
        try:
            render_result = await self._worker.render(
                self._rating_key,
                start=start,
                end=end,
                format=self._format,
                style=requested_style,
                subtitle_overrides=self.overrides or None,
            )
        except httpx.HTTPError as exc:
            await self._handle_render_error(interaction, exc)
            return
        await self._apply_render_result(interaction, requested_style, render_result)

    async def _re_render_timecode(
        self,
        interaction: discord.Interaction,
        timecode: str,
        end_timecode: str,
        status_message: str = "Generating…",
    ) -> None:
        # Same as _re_render, but for _CustomDurationModal's raw timecode
        # strings — sent via /render's timecode/end_timecode fields (parsed
        # server-side) instead of numeric start/end, per the layering
        # invariant in WorkerClient's docstring and RenderRequest's field
        # comments (app/worker/api.py). Shares all post-render handling
        # with _re_render via _handle_render_error/_apply_render_result so
        # there's exactly one place doing that work.
        await interaction.edit_original_response(content=status_message, view=self)
        requested_style = self.style
        try:
            render_result = await self._worker.render(
                self._rating_key,
                timecode=timecode,
                end_timecode=end_timecode,
                format=self._format,
                style=requested_style,
                subtitle_overrides=self.overrides or None,
            )
        except httpx.HTTPError as exc:
            await self._handle_render_error(interaction, exc)
            return
        await self._apply_render_result(interaction, requested_style, render_result)

    async def _handle_render_error(
        self, interaction: discord.Interaction, exc: httpx.HTTPError
    ) -> None:
        await interaction.edit_original_response(
            content=f"Couldn't update the clip: {_error_detail(exc)}", embed=None, view=self
        )

    async def _apply_render_result(
        self, interaction: discord.Interaction, requested_style: str, render_result
    ) -> None:
        self._content = render_result.content
        self._filename = f"clip.{render_result.format}"
        self.style = render_result.style
        self._clip_start = render_result.start
        self._clip_duration = render_result.duration
        await self._refresh_open_category()

        span_line = f"✏️ Edited — {_clip_span_line(render_result.start, render_result.duration)}"
        note = _no_subtitles_note(requested_style, render_result.style)
        description = f"{span_line}\n{note}" if note else span_line

        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        # _refresh_open_category() above already recomputed
        # _merge_context_text against the just-updated span if Merge Subs
        # is the open category — the embed path keeps it visible in the
        # footer through edits, not just on first opening, so Merge/
        # Unmerge/Merge Count stay informed.
        if self._open_category == "merge":
            embed = self._build_merge_embed(self._filename, description=description)
            await interaction.edit_original_response(
                content=None, embed=embed, attachments=[file], view=self
            )
        else:
            await interaction.edit_original_response(
                content=description, embed=None, attachments=[file], view=self
            )

    async def _refresh_open_category(self) -> None:
        # Merge-button enabled state and the Edit Subs window depend on the
        # just-updated clip span — rebuild whichever category is currently
        # open using the already-cached entry list (no new worker fetch).
        if self._open_category == "duration":
            await self._open_duration()
        elif self._open_category == "subtitles":
            await self._open_subtitles()
        elif self._open_category == "merge":
            await self._open_merge()

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.primary, row=4)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # See ClipResultView.post — same defer-before-send fix, same
        # reason (an unhandled send failure here previously left the
        # interaction unacknowledged, surfacing as Discord's generic
        # "didn't respond in time").
        await interaction.response.defer()
        file = discord.File(io.BytesIO(self._content), filename=self._filename)
        content = _post_metadata_line(
            self._title, self._clip_start, self._clip_end, interaction.user.display_name,
        )
        try:
            await interaction.channel.send(content=content, file=file)
        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                content=f"Couldn't post that clip: {_error_detail_from_discord(exc)}"
            )
            return
        button.disabled = True
        await interaction.edit_original_response(view=self)


# Signature shared by every random-pick source /snip random, /snip movie,
# and /snip tv can fetch from (library-wide, single-title, or whole-show
# scope) — RandomResultView only ever talks to its caller through this,
# never to a specific worker endpoint, so the Shuffle/Previous history
# logic below is written once and reused by all three.
RandomFetch = Callable[[frozenset[int], "int | None"], Awaitable[RandomQuoteResult]]


class _RandomHistoryEntry:
    __slots__ = ("pick", "content", "filename")

    def __init__(self, pick: RandomQuoteResult, content: bytes, filename: str) -> None:
        self.pick = pick
        self.content = content
        self.filename = filename


class RandomResultView(discord.ui.View):
    """Shown by /snip random and by /snip movie's/tv's random-pick path (no
    quote/timecode given): Shuffle (re-roll + re-render in place, same
    message-edit pattern as ClipResultView's style swap), Previous (steps
    back through this journey's history once it has more than one entry —
    instant, no re-render, since each history entry keeps its own rendered
    bytes), and Post to channel. No confirm-the-timestamp step (doesn't
    apply to a random pick) and no style select (CLAUDE.md's random-command
    design keeps this minimal).

    Shuffle tracks every entry_id shown this journey and excludes them from
    the next pick, so a small pool (e.g. a narrow quote match with only a
    couple of hits) can't silently repeat the same line — CLAUDE.md's
    "Celina" fix. Once every candidate has been shown, the worker resets
    the pool (reported via RandomQuoteResult.exhausted) rather than
    returning nothing, and this view's own tracking resets to match so the
    next few shuffles don't immediately loop back to the same handful of
    picks. Shuffle is disabled outright when pool_size <= 1 — nothing to
    shuffle to — rather than left as a button that looks broken.
    """

    def __init__(
        self,
        worker,
        fetch: RandomFetch,
        initial_pick: RandomQuoteResult,
        content: bytes,
        filename: str,
    ) -> None:
        super().__init__(timeout=300)
        self._worker = worker
        self._fetch = fetch
        self._history: list[_RandomHistoryEntry] = [
            _RandomHistoryEntry(initial_pick, content, filename)
        ]
        self._pointer = 0
        self._seen_entry_ids: set[int] = {initial_pick.entry_id}

        self._previous_button = discord.ui.Button(
            label="◀ Previous", style=discord.ButtonStyle.secondary, row=0
        )
        self._previous_button.callback = self._on_previous

        self.shuffle.disabled = initial_pick.pool_size <= 1

    @property
    def _current(self) -> _RandomHistoryEntry:
        return self._history[self._pointer]

    def _render_content(self, pick: RandomQuoteResult, exhausted: bool = False) -> str:
        note = " *(seen every match — starting over)*" if exhausted else ""
        return f"**{pick.title}** — {pick.timecode}{note}"

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        most_recent = self._current.pick.entry_id
        try:
            picked = await self._fetch(frozenset(self._seen_entry_ids), most_recent)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't find another match: {_error_detail(exc)}"
            )
            return

        try:
            render_result = await self._worker.render(
                picked.rating_key,
                str(picked.start),
                duration=picked.end - picked.start,
                style="classic",
            )
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't render that match: {_error_detail(exc)}"
            )
            return

        content = render_result.content
        filename = f"clip.{render_result.format}"

        self._seen_entry_ids = (
            {picked.entry_id} if picked.exhausted else self._seen_entry_ids | {picked.entry_id}
        )
        # Shuffling from a stepped-back point (via Previous) branches a new
        # path rather than resurrecting whatever used to come next.
        self._history = self._history[: self._pointer + 1]
        self._history.append(_RandomHistoryEntry(picked, content, filename))
        self._pointer += 1

        if self._previous_button not in self.children:
            self.add_item(self._previous_button)
        self._previous_button.disabled = self._pointer == 0
        self.shuffle.disabled = picked.pool_size <= 1

        file = discord.File(io.BytesIO(content), filename=filename)
        await interaction.edit_original_response(
            content=self._render_content(picked, exhausted=picked.exhausted),
            attachments=[file],
            view=self,
        )

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._pointer -= 1
        entry = self._current
        self._previous_button.disabled = self._pointer == 0
        self.shuffle.disabled = entry.pick.pool_size <= 1

        file = discord.File(io.BytesIO(entry.content), filename=entry.filename)
        await interaction.edit_original_response(
            content=self._render_content(entry.pick), attachments=[file], view=self
        )

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.primary, row=0)
    async def post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # See ClipResultView.post — same defer-before-send fix.
        await interaction.response.defer()
        entry = self._current
        file = discord.File(io.BytesIO(entry.content), filename=entry.filename)
        content = _post_metadata_line(
            entry.pick.title, entry.pick.start, entry.pick.end, interaction.user.display_name,
        )
        try:
            await interaction.channel.send(content=content, file=file)
        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                content=f"Couldn't post that clip: {_error_detail_from_discord(exc)}"
            )
            return
        button.disabled = True
        await interaction.edit_original_response(view=self)


def _validate_quote_or_timecode(
    quote: str | None, timecode: str | None, end_timecode: str | None
) -> str | None:
    """Shared by /snip movie and /snip tv so these checks can't drift between the
    two commands — the exact class of bug CLAUDE.md's library-search
    preferred_start fix (Section 2) already hit once from duplicated logic.
    Returns an error message, or None if valid.

    Giving neither quote nor timecode is valid — it means "pick a random
    line from this title" (see _run_random_result), not an error.
    """
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


def _error_detail_from_discord(exc: discord.HTTPException) -> str:
    # Error 40005 (HTTP 413) is by far the realistic failure mode for
    # interaction.channel.send() here — the worker's own
    # max_file_size_bytes downscale retry already tries to keep renders
    # under a safe default, but a server with a lower effective upload
    # cap (no boost) or an unusually long/high-motion clip can still slip
    # past it. Give a clear, actionable message for that specific case
    # rather than Discord's raw "Request entity too large".
    if exc.code == 40005:
        return "the clip file is too large for this server's upload limit — try a shorter span"
    return exc.text or str(exc)


def _library_results_embed(
    quote: str,
    matches: list[LibraryQuoteMatchResult],
    description: str = "Pick a film below to generate a clip from that line.",
    start_index: int = 1,
    page: int | None = None,
    total_pages: int | None = None,
    truncated: bool = False,
) -> discord.Embed:
    """`matches` is the slice to render (one page's worth once a
    LibrarySearchView exists — see its own embed() method), not the full
    fetched batch; `start_index` is what the first rendered item is
    numbered as, so a later page's items keep counting up from where the
    previous page left off rather than restarting at 1. Footer text is
    shared with QuoteMatchView via _pagination_footer()."""
    embed = discord.Embed(title=f'Results for "{quote}"', description=description)
    for offset, m in enumerate(matches):
        i = start_index + offset
        snippet = m.text if len(m.text) <= 200 else m.text[:197] + "..."
        embed.add_field(
            name=f"{i}. {m.title} — {m.library_name}",
            value=f"> {snippet}\n{m.timecode} · {m.score:.0f}%",
            inline=False,
        )
    footer = _pagination_footer(page or 1, total_pages or 1, truncated)
    if footer:
        embed.set_footer(text=footer)
    return embed


class LibrarySearchView(discord.ui.View):
    """Shown by /snip search (films) and /snip tv's show-wide search
    (episodes): pick a result from cross-title matches, then funnel into the
    normal quote-confirm -> render pipeline (CLAUDE.md Section 2: "just a
    different on-ramp into the same two steps, not a separate confirmation
    UI") via GifCog._generate. Reused as-is for episodes — an episode's
    MovieResult.title already reads as "Show — S02E01 — Title", so no
    TV-specific formatting is needed here.

    Holds the FULL fetched batch (up to quote_match.fetch_limit matches,
    already diversity-ranked by the worker) and pages through it in place
    with Next/Previous buttons, _PAGE_SIZE at a time — Discord's own
    25-option select cap means each page's select mirrors only that page's
    slice, never the whole batch (issue #7).
    """

    def __init__(
        self,
        cog: "GifCog",
        quote: str,
        matches: list[LibraryQuoteMatchResult],
        remaining_uncached: int | None = None,
        description: str = "Pick a film below to generate a clip from that line.",
        truncated: bool = False,
    ) -> None:
        # See QuoteMatchView's __init__ for why this isn't 120s anymore.
        super().__init__(timeout=600)
        self._cog = cog
        self._quote = quote
        self._matches = matches
        self._description = description
        self._truncated = truncated
        self._page = 0
        self._select: discord.ui.Select | None = None
        self._prev_button: discord.ui.Button | None = None
        self._next_button: discord.ui.Button | None = None

        self._add_select()
        if len(matches) > _PAGE_SIZE:
            self._add_page_buttons()

        # remaining_uncached is only ever a real int when Tier 2 (extend)
        # actually ran and hit its cap — None (extend didn't run, e.g.
        # library_sync disabled) or 0 (fully covered) both mean no button.
        if remaining_uncached:
            more_button = discord.ui.Button(
                label=f"🔍 Search {remaining_uncached} more",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            more_button.callback = self._on_search_more
            self.add_item(more_button)

    def _total_pages(self) -> int:
        return max(1, (len(self._matches) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _page_slice(self) -> list[LibraryQuoteMatchResult]:
        start = self._page * _PAGE_SIZE
        return self._matches[start : start + _PAGE_SIZE]

    def embed(self) -> discord.Embed:
        return _library_results_embed(
            self._quote,
            self._page_slice(),
            description=self._description,
            start_index=self._page * _PAGE_SIZE + 1,
            page=self._page + 1,
            total_pages=self._total_pages(),
            truncated=self._truncated,
        )

    def _add_select(self) -> None:
        start = self._page * _PAGE_SIZE
        options = []
        for offset, m in enumerate(self._page_slice()):
            i = start + offset
            label = m.text if len(m.text) <= 100 else m.text[:97] + "..."
            description = f"{m.title} — {m.library_name} · {m.timecode} · {m.score:.0f}%"
            if len(description) > 100:
                description = description[:97] + "..."
            options.append(
                discord.SelectOption(label=label, description=description, value=str(i))
            )
        select = discord.ui.Select(placeholder="Choose a quote", options=options, row=0)
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    def _add_page_buttons(self) -> None:
        total_pages = self._total_pages()
        prev_button = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self._page == 0,
            row=1,
        )
        prev_button.callback = self._on_previous
        self._prev_button = prev_button
        self.add_item(prev_button)

        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self._page >= total_pages - 1,
            row=1,
        )
        next_button.callback = self._on_next
        self._next_button = next_button
        self.add_item(next_button)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        self._page = max(0, min(self._page + delta, self._total_pages() - 1))
        if self._select is not None:
            self.remove_item(self._select)
            self._select = None
        if self._prev_button is not None:
            self.remove_item(self._prev_button)
            self._prev_button = None
        if self._next_button is not None:
            self.remove_item(self._next_button)
            self._next_button = None
        self._add_select()
        self._add_page_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)

    async def _on_search_more(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        await self._cog._run_library_search(interaction, self._quote)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if self._select is None:
            return
        match = self._matches[int(self._select.values[0])]

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
        if not interaction.response.is_done():
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
                truncated=resolved_quote.truncated,
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
        elif timecode:
            render_timecode = timecode
            render_duration = None
            # A bare timecode has no known subtitle availability — default
            # to off rather than guessing at burn-in the user didn't ask for.
            default_style = "none"
            await interaction.edit_original_response(
                content=f"Generating a clip from {resolved.title}{library_note}…"
            )
        else:
            # Neither quote nor timecode given: pick a random line from this
            # title (CLAUDE.md's per-title random-pick design) — its own
            # result view (Shuffle/Previous/Post), not the render pipeline
            # below, since there's no confirm-the-timestamp step to make and
            # no style select for a random pick (matches /snip random).
            await interaction.edit_original_response(
                content=f"Picking a random line from {resolved.title}{library_note}…"
            )

            async def fetch(exclude: frozenset[int], most_recent: int | None) -> RandomQuoteResult:
                return await self.bot.worker.random_line(
                    rating_key, exclude_entry_ids=exclude, most_recent_entry_id=most_recent
                )

            await self._run_random_result(interaction, fetch)
            return

        render_end_timecode = end_timecode if not quote else None
        await self._render_and_respond(
            interaction,
            rating_key,
            resolved.title,
            render_timecode,
            render_duration,
            render_end_timecode,
            format,
            default_style,
        )

    async def _render_and_respond(
        self,
        interaction: discord.Interaction,
        rating_key: int,
        title: str,
        render_timecode: str,
        render_duration: float | None,
        render_end_timecode: str | None,
        format: str | None,
        default_style: str,
    ) -> None:
        # Shared by _generate (movie / a single known episode) and
        # snip_tv's whole-show search (each candidate can resolve to a
        # DIFFERENT episode's rating_key, only known once QuoteMatchView's
        # Confirm step picks one) — everything past "what to render and
        # what to call it" is identical either way.
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
        result_view = ClipEditView(
            self.bot.worker,
            rating_key,
            title,
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
        quote="A line of dialogue to find (fuzzy — close is fine); omit both quote and "
        "timecode for a random line",
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
        cached_truncated = False
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
                    cached_truncated = event.truncated or False
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
                                quote, cached_matches[:_PAGE_SIZE],
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
        final_truncated = (final_event.truncated if final_event else cached_truncated) or False
        view = LibrarySearchView(
            self,
            quote,
            final_matches,
            remaining_uncached=remaining_uncached,
            truncated=final_truncated,
        )
        await _safe_edit(
            content=None,
            embed=view.embed(),
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

    async def _run_random_result(
        self, interaction: discord.Interaction, fetch: RandomFetch
    ) -> None:
        # Shared by /snip random and by /snip movie's/tv's random-pick path
        # (no quote/timecode given) — everything past "how to get the first
        # pick" is identical, so RandomResultView (Shuffle/Previous/Post)
        # only ever talks to `fetch`, never to a specific worker endpoint.
        try:
            picked = await fetch(frozenset(), None)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't find a match: {_error_detail(exc)}"
            )
            return

        try:
            render_result = await self.bot.worker.render(
                picked.rating_key,
                str(picked.start),
                duration=picked.end - picked.start,
                style="classic",
            )
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't generate the clip: {_error_detail(exc)}"
            )
            return

        filename = f"clip.{render_result.format}"
        file = discord.File(io.BytesIO(render_result.content), filename=filename)
        view = RandomResultView(self.bot.worker, fetch, picked, render_result.content, filename)
        # CLAUDE.md's "Celina" fix: a single-match pool must say so up
        # front, not leave Shuffle looking broken when clicked.
        note = " *(only match — nothing else to shuffle to)*" if picked.pool_size <= 1 else ""
        await interaction.edit_original_response(
            content=f"**{picked.title}** — {picked.timecode}{note}",
            attachments=[file],
            view=view,
        )

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

        async def fetch(exclude: frozenset[int], most_recent: int | None) -> RandomQuoteResult:
            return await self.bot.worker.random_quote(
                quote, effective_media, exclude_entry_ids=exclude, most_recent_entry_id=most_recent
            )

        await self._run_random_result(interaction, fetch)

    @snip_group.command(
        name="tv",
        description="Generate a clip from a TV episode at a quote or timecode.",
    )
    @app_commands.describe(
        show="The show to search for",
        season="Season number (requires episode)",
        episode="Episode number within the season (requires season)",
        quote="A line of dialogue to find (fuzzy — close is fine); omit season/episode to "
        "search the whole show; omit quote and timecode entirely for a random line",
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

        # No episode given, and the "timecode needs an episode" check above
        # already rejected a bare timecode here — so quote is either given
        # (search the whole show) or genuinely absent (random line from any
        # episode, CLAUDE.md's per-title/per-show random-pick design).
        if quote is None:
            await interaction.edit_original_response(
                content="Picking a random line from the show…"
            )

            async def fetch(exclude: frozenset[int], most_recent: int | None) -> RandomQuoteResult:
                return await self.bot.worker.random_line_show(
                    show_rating_key, exclude_entry_ids=exclude, most_recent_entry_id=most_recent
                )

            await self._run_random_result(interaction, fetch)
            return

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

        # Unlike /snip search (genuinely cross-title, kept as a scannable
        # list via LibrarySearchView), a whole-show search's candidates are
        # all episodes of the SAME show a user already picked — matching
        # /snip movie's UX (drop straight into the confirm screen, with its
        # picker/paging) reads better than an extra "pick an episode" list
        # step first. title=None: LibraryQuoteMatchResult carries its own
        # per-episode title, unlike a single already-known film.
        match_view = QuoteMatchView(
            None,
            result.matches,
            result.min_score,
            result.confident_score,
            truncated=result.truncated,
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
        await interaction.edit_original_response(content="Generating…", embed=None, view=None)
        await self._render_and_respond(
            interaction,
            selected.rating_key,
            selected.title,
            str(selected.start),
            selected.end - selected.start,
            None,
            format,
            # A quote match came from real subtitle text, so burn-in has
            # something to show by default (CLAUDE.md Section 7: "subtitles
            # on when triggered by a quote search") — same default _generate
            # uses for its own quote-match path.
            "classic",
        )
