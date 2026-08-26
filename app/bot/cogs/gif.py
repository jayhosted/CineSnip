from __future__ import annotations

import io

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from app.bot.worker_client import QuoteMatchResult


class ConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=120)
        self.value: bool | None = None

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
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                self.index = int(item.values[0])
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


class PostToChannelView(discord.ui.View):
    def __init__(self, gif_bytes: bytes) -> None:
        super().__init__(timeout=300)
        self._gif_bytes = gif_bytes

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.primary)
    async def post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        file = discord.File(io.BytesIO(self._gif_bytes), filename="clip.gif")
        await interaction.channel.send(file=file)
        button.disabled = True
        await interaction.response.edit_message(view=self)


def _format_duration(duration_ms: int) -> str:
    total_seconds = duration_ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


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
            choices.append(
                app_commands.Choice(name=label[:100], value=str(movie.rating_key))
            )
        return choices

    @app_commands.command(
        name="cinesnip",
        description="Generate a GIF clip from a film at a quote or timecode.",
    )
    @app_commands.describe(
        film="The film to search for",
        quote="A line of dialogue to find (fuzzy — close is fine)",
        timecode="Timestamp, e.g. 1:23:45",
    )
    @app_commands.autocomplete(film=film_autocomplete)
    async def gif(
        self,
        interaction: discord.Interaction,
        film: str,
        quote: str | None = None,
        timecode: str | None = None,
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

        try:
            resolved = await self.bot.worker.resolve(rating_key)
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Couldn't find that film: {_error_detail(exc)}", ephemeral=True
            )
            return

        embed = discord.Embed(title=resolved.title)
        if resolved.year:
            embed.add_field(name="Year", value=str(resolved.year))
        embed.add_field(name="Runtime", value=_format_duration(resolved.duration_ms))
        if quote:
            embed.add_field(name="Quote", value=quote, inline=False)
            if timecode:
                embed.add_field(
                    name="Note",
                    value="Both `quote` and `timecode` were given — searching by quote.",
                    inline=False,
                )
        else:
            embed.add_field(name="Timecode", value=timecode, inline=False)
        if resolved.thumb_url:
            embed.set_thumbnail(url=resolved.thumb_url)

        view = ConfirmView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if view.value is None:
            await interaction.edit_original_response(
                content="Timed out.", embed=None, view=None
            )
            return
        if view.value is False:
            await interaction.edit_original_response(
                content="Cancelled.", embed=None, view=None
            )
            return

        if quote:
            await interaction.edit_original_response(
                content="Searching subtitles…", embed=None, view=None
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

            render_timecode = str(match_view.selected.start)
        else:
            render_timecode = timecode

        await interaction.edit_original_response(
            content="Generating…", embed=None, view=None
        )

        try:
            gif_bytes = await self.bot.worker.render(rating_key, render_timecode)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't generate the GIF: {_error_detail(exc)}"
            )
            return

        file = discord.File(io.BytesIO(gif_bytes), filename="clip.gif")
        post_view = PostToChannelView(gif_bytes)
        await interaction.edit_original_response(
            content=None, attachments=[file], view=post_view
        )
