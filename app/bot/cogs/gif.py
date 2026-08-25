from __future__ import annotations

import io

import discord
import httpx
from discord import app_commands
from discord.ext import commands


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


def _error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return str(exc)


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
        name="gif", description="Generate a GIF clip from a film at a timecode."
    )
    @app_commands.describe(
        film="The film to search for", timecode="Timestamp, e.g. 1:23:45"
    )
    @app_commands.autocomplete(film=film_autocomplete)
    async def gif(
        self, interaction: discord.Interaction, film: str, timecode: str
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

        try:
            resolved = await self.bot.worker.resolve(rating_key)
        except httpx.HTTPStatusError as exc:
            await interaction.followup.send(
                f"Couldn't find that film: {_error_detail(exc)}", ephemeral=True
            )
            return

        embed = discord.Embed(title=resolved.title)
        if resolved.year:
            embed.add_field(name="Year", value=str(resolved.year))
        embed.add_field(name="Runtime", value=_format_duration(resolved.duration_ms))
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

        await interaction.edit_original_response(
            content="Generating…", embed=None, view=None
        )

        try:
            chunks = [
                chunk async for chunk in self.bot.worker.render(rating_key, timecode)
            ]
        except httpx.HTTPStatusError as exc:
            await interaction.edit_original_response(
                content=f"Couldn't generate the GIF: {_error_detail(exc)}"
            )
            return

        gif_bytes = b"".join(chunks)
        file = discord.File(io.BytesIO(gif_bytes), filename="clip.gif")
        post_view = PostToChannelView(gif_bytes)
        await interaction.edit_original_response(
            content=None, attachments=[file], view=post_view
        )
