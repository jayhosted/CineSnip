from __future__ import annotations

import io
from typing import Literal

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from app.bot.worker_client import QuoteMatchResult


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
    ) -> None:
        super().__init__(timeout=120)
        self._title = title
        self.matches = matches
        self.min_score = min_score
        self.confident_score = confident_score
        self.value: bool | None = None
        self.index = 0

        self._show_others_button: discord.ui.Button | None = None

        if len(matches) > 1:
            if matches[0].score >= confident_score:
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
        timecode: str,
        duration: float | None,
        end_timecode: str | None,
        format: str | None,
        style: str,
        content: bytes,
        filename: str,
    ) -> None:
        super().__init__(timeout=300)
        self._worker = worker
        self._rating_key = rating_key
        self._timecode = timecode
        self._duration = duration
        self._end_timecode = end_timecode
        self._format = format
        self.style = style
        self._content = content
        self._filename = filename
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
        await interaction.channel.send(file=file)
        button.disabled = True
        await interaction.response.edit_message(view=self)


def _error_detail(exc: httpx.HTTPError) -> str:
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return str(exc) or "the worker didn't respond in time"


class GifCog(commands.Cog):
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

    async def _generate(
        self,
        interaction: discord.Interaction,
        film: str,
        quote: str | None,
        timecode: str | None,
        end_timecode: str | None,
        format: str | None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            rating_key = int(film)
        except ValueError:
            await interaction.followup.send(
                "Please select a film from the autocomplete suggestions.",
                ephemeral=True,
            )
            return

        if not quote and not timecode:
            await interaction.followup.send(
                "Give either a `quote:` or a `timecode:`.", ephemeral=True
            )
            return

        if end_timecode and not timecode:
            await interaction.followup.send(
                "`end_timecode` needs a `timecode` to start from.", ephemeral=True
            )
            return

        try:
            resolved = await self.bot.worker.resolve(rating_key)
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Couldn't find that film: {_error_detail(exc)}", ephemeral=True
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
            await interaction.followup.send(
                content=f"Searching {resolved.title}'s subtitles{library_note}…{note}",
                ephemeral=True,
            )
            try:
                resolved_quote = await self.bot.worker.resolve_quote(rating_key, quote)
            except httpx.HTTPError as exc:
                await interaction.edit_original_response(
                    content=f"Couldn't search subtitles: {_error_detail(exc)}"
                )
                return

            match_view = QuoteMatchView(
                resolved.title,
                resolved_quote.matches,
                resolved_quote.min_score,
                resolved_quote.confident_score,
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
            await interaction.followup.send(
                content=f"Generating a clip from {resolved.title}{library_note}…",
                ephemeral=True,
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
            render_timecode,
            render_duration,
            render_end_timecode,
            format,
            render_result.style,
            render_result.content,
            filename,
        )
        await interaction.edit_original_response(
            content=_no_subtitles_note(default_style, render_result.style) or None,
            attachments=[file],
            view=result_view,
        )

    @app_commands.command(
        name="cinesnip",
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
    async def cinesnip(
        self,
        interaction: discord.Interaction,
        film: str,
        quote: str | None = None,
        timecode: str | None = None,
        end_timecode: str | None = None,
        format: Literal["gif", "mp4", "webm"] | None = None,
    ) -> None:
        await self._generate(interaction, film, quote, timecode, end_timecode, format)

    @app_commands.command(
        name="snip",
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
    async def snip(
        self,
        interaction: discord.Interaction,
        film: str,
        quote: str | None = None,
        timecode: str | None = None,
        end_timecode: str | None = None,
        format: Literal["gif", "mp4", "webm"] | None = None,
    ) -> None:
        await self._generate(interaction, film, quote, timecode, end_timecode, format)
