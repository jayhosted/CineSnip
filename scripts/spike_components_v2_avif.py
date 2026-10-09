"""PROTOTYPE / THROWAWAY: lives only on the spike/components-v2-avif branch.

Question: can a CineSnip AVIF clip go inside a Components V2 message
(LayoutView + MediaGallery) and still behave the way decision #1 requires:
autoplay, loop, GIF-picker favoriting, and no media-proxy re-encode?

Posts to a test channel (REST only, no gateway, so it doesn't clash with
the running bot):
  A. Baseline: today's "Post to channel" shape (content + plain attachment).
  B. V2: Container(TextDisplay, MediaGallery(attachment), ActionRow(button)).
  C. Edits B after a pause to swap in a second clip (the style-change flow).
Then prints cdn vs media-proxy byte counts for every attachment.

VERDICT (2026-10-09): PASSES. Feasible.
  - Discord accepted the new-format message (flag 32768) and the clip swap on edit.
  - Proxy bytes for the gallery clip match today's attachment exactly: 100%
    untouched on the plain and resize-only links, so no re-encode.
  - Manually confirmed by Jay on Discord: autoplays + loops, looks identical
    to the baseline, can be added to GIF-picker favourites and plays from
    there, and the client loads the .avif (not a format=webp variant).
  Gotcha: in a V2 message the media lives under components[].items[].media,
  NOT message.attachments (which comes back empty).

Usage:
  .venv/bin/python scripts/spike_components_v2_avif.py CHANNEL_ID CLIP_A.avif CLIP_B.avif
"""

import asyncio
import os
import sys

import discord
import httpx
from dotenv import load_dotenv


def v2_view(filename: str, label: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(f"**Spike (Components V2)**: {label}"),
            discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{filename}")),
            discord.ui.ActionRow(
                discord.ui.Button(label="Post to channel", custom_id="spike-noop", disabled=True)
            ),
            accent_colour=0x5865F2,
        )
    )
    return view


def report(tag: str, message: discord.Message) -> None:
    for a in message.attachments:
        cdn = httpx.get(a.url, timeout=60).content
        proxy = httpx.get(a.proxy_url, timeout=60).content
        print(
            f"{tag}: {a.filename} uploaded={a.size} cdn={len(cdn)} "
            f"proxy={len(proxy)} ({len(proxy) / a.size:.0%}) type={a.content_type}"
        )
    print(f"{tag}: flags={message.flags.value} link={message.jump_url}")


async def main(channel_id: int, clip_a: str, clip_b: str) -> None:
    load_dotenv()
    client = discord.Client(intents=discord.Intents.none())
    await client.login(os.environ["DISCORD_TOKEN"])
    try:
        channel = await client.fetch_channel(channel_id)

        baseline = await channel.send(
            content="**Spike baseline**: today's Post to channel shape",
            file=discord.File(clip_a, filename="clip.avif"),
        )
        v2 = await channel.send(
            view=v2_view("clip.avif", "first clip"),
            file=discord.File(clip_a, filename="clip.avif"),
        )
        report("A baseline", baseline)
        report("B v2", v2)

        await asyncio.sleep(5)
        v2 = await v2.edit(
            view=v2_view("clip2.avif", "edited: swapped clip"),
            attachments=[discord.File(clip_b, filename="clip2.avif")],
        )
        report("C v2-edited", v2)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], sys.argv[3]))
