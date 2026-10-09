"""PROTOTYPE / THROWAWAY: spike/components-v2-avif branch only.

Question: can a deferred EPHEMERAL interaction response be edited into a
Components V2 message (both straight from the deferred state and after a
plain-text status edit), and can a component click on it keep swapping the
attachment?

Run with the cinesnip container STOPPED (one gateway session for the bot
token), then run /v2check in the DEV_GUILD_ID server. Ctrl-C when done and
start the container again: its startup tree.sync(guild=...) wipes this
temporary guild command.

VERDICT (2026-10-09): PASSES. Both a deferred ephemeral response and one
already edited to plain text can be edited into a V2 message; component
clicks keep swapping the attachment in place. Confirmed by console
(OK plain_first=False / True) and by Jay in Discord.

Usage:
  .venv/bin/python scripts/spike_v2_interaction.py CLIP_A.avif CLIP_B.avif
"""

import asyncio
import os
import sys

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
GUILD = discord.Object(int(os.environ["DEV_GUILD_ID"]))
CLIPS = {"a.avif": sys.argv[1], "b.avif": sys.argv[2]}


def card(filename: str, label: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=900)
    swap = discord.ui.Button(label="Swap clip (component edit)")

    async def on_swap(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        nxt = "b.avif" if filename == "a.avif" else "a.avif"
        await interaction.edit_original_response(
            view=card(nxt, f"swapped to {nxt}"),
            attachments=[discord.File(CLIPS[nxt], filename=nxt)],
        )

    swap.callback = on_swap
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(f"**V2 interaction spike**: {label}"),
            discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{filename}")),
            discord.ui.ActionRow(swap),
        )
    )
    return view


def status(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(text))
    return view


@app_commands.command(name="v2check", description="TEMP spike: deferred ephemeral -> V2 edit")
@app_commands.describe(plain_first="Edit to a plain-text status before the V2 card")
async def v2check(interaction: discord.Interaction, plain_first: bool = False) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        if plain_first:
            await interaction.edit_original_response(content="Plain status…")
        else:
            await interaction.edit_original_response(view=status("V2 status: Searching…"))
        await asyncio.sleep(2)
        await interaction.edit_original_response(
            content=None,
            view=card("a.avif", "first clip"),
            attachments=[discord.File(CLIPS["a.avif"], filename="a.avif")],
        )
        print(f"OK plain_first={plain_first}")
    except discord.HTTPException as exc:
        print(f"FAILED plain_first={plain_first}: {exc.status} {exc.text}")
        await interaction.followup.send(f"EDIT FAILED: {exc.status} {exc.text}", ephemeral=True)


class SpikeBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.tree.add_command(v2check, guild=GUILD)
        await self.tree.sync(guild=GUILD)


SpikeBot().run(os.environ["DISCORD_TOKEN"])
