from __future__ import annotations

from dataclasses import dataclass

from app.worker.quotes import strip_markup
from app.worker.subtitles import SubtitleEntry


@dataclass(frozen=True)
class StylePreset:
    """One entry from STYLE_PRESETS below. ASS colour fields use libass's
    &HAABBGGRR hex format (alpha-blue-green-red, not the usual RGB order)."""

    name: str
    font: str
    font_size: int
    primary_color: str
    outline_color: str
    back_color: str
    border_style: int  # 1 = outline + shadow, 3 = opaque box
    outline: float
    shadow: float
    bold: bool
    uppercase: bool
    margin_v: int
    alignment: int = 2  # bottom-center


# Font sizes/margins are calibrated against the default render_defaults.width
# (480px) — CLAUDE.md Section 7. A much wider/narrower configured width will
# look proportionally off; not auto-scaled in V2 (documented limitation, not
# a bug). Kept as Python constants rather than config.yaml, matching the
# precedent set by ffmpeg.py's _VIDEO_CODEC_ARGS for similar multi-field
# per-format presets.
STYLE_PRESETS: dict[str, StylePreset] = {
    "classic": StylePreset(
        name="classic",
        font="Liberation Sans",
        font_size=26,
        primary_color="&H00FFFFFF",
        outline_color="&H00000000",
        back_color="&H00000000",
        border_style=1,
        outline=2.0,
        shadow=0.0,
        bold=False,
        uppercase=False,
        margin_v=24,
    ),
    "boxed": StylePreset(
        name="boxed",
        font="Liberation Sans",
        font_size=26,
        primary_color="&H00FFFFFF",
        outline_color="&H00000000",
        # ASS alpha is inverted from the usual convention (00 = fully
        # opaque, FF = fully transparent) — &H00000000 here is a solid
        # opaque black box, matching what "Boxed (white on black box)"
        # actually promises. A half-transparent box tested nearly
        # invisible against a light background in real footage.
        back_color="&H00000000",
        border_style=3,
        outline=4.0,
        shadow=0.0,
        bold=False,
        uppercase=False,
        margin_v=24,
    ),
    "cinematic": StylePreset(
        name="cinematic",
        font="Liberation Sans",
        font_size=26,
        primary_color="&H0000FFFF",
        outline_color="&H00000000",
        back_color="&H00000000",
        border_style=1,
        outline=2.0,
        shadow=0.0,
        bold=False,
        uppercase=False,
        margin_v=24,
    ),
    "meme": StylePreset(
        name="meme",
        # True Impact isn't installed (proprietary, not in Debian's repos)
        # and libass's fallback for an unresolvable family name isn't
        # necessarily even a sans-serif face (confirmed: "Impact" fell back
        # to a monospace font). Liberation Sans (fonts-liberation, see
        # Dockerfile) + bold + all-caps gets a reasonable meme look instead.
        font="Liberation Sans",
        font_size=32,
        primary_color="&H00FFFFFF",
        outline_color="&H00000000",
        back_color="&H00000000",
        border_style=1,
        outline=3.0,
        shadow=0.0,
        bold=True,
        uppercase=True,
        margin_v=16,
    ),
    # "Original" is meant to mirror the source subtitle's own styling
    # (CLAUDE.md Section 7) — but a plain sidecar/embedded SRT carries no
    # style data to mirror, and extracting real style info from an
    # embedded ASS/SSA track isn't built (V2 gap, same class of limitation
    # as the rest of Section 5's subtitle-source handling). Falls back to
    # the same neutral look as Classic until that exists.
    "original": StylePreset(
        name="original",
        font="Liberation Sans",
        font_size=26,
        primary_color="&H00FFFFFF",
        outline_color="&H00000000",
        back_color="&H00000000",
        border_style=1,
        outline=2.0,
        shadow=0.0,
        bold=False,
        uppercase=False,
        margin_v=24,
    ),
}


def entries_in_window(
    entries: list[SubtitleEntry], clip_start: float, clip_end: float
) -> list[SubtitleEntry]:
    """Subtitle entries overlapping [clip_start, clip_end), trimmed to that
    span and rebased to clip-relative time (0 == clip_start) — the ASS
    document burned into a clip must be timed against the clip's own
    timeline, not the source film's."""
    window: list[SubtitleEntry] = []
    for e in entries:
        if e.end <= clip_start or e.start >= clip_end:
            continue
        rel_start = max(0.0, e.start - clip_start)
        rel_end = min(clip_end - clip_start, e.end - clip_start)
        if rel_end <= rel_start:
            continue
        window.append(SubtitleEntry(index=e.index, start=rel_start, end=rel_end, text=e.text))
    return window


def apply_overrides(
    entries: list[SubtitleEntry], overrides: dict[int, str | None]
) -> list[SubtitleEntry]:
    """Apply per-entry text overrides/suppressions (keyed by SubtitleEntry.index,
    from a Discord clip-edit session) to an already-windowed entry list, just
    before it's burned into an ASS document. An index with no key in
    `overrides` passes its entry through unchanged; a value of None
    suppresses the line entirely; any other string replaces its text."""
    result: list[SubtitleEntry] = []
    for entry in entries:
        if entry.index not in overrides:
            result.append(entry)
            continue
        override = overrides[entry.index]
        if override is None:
            continue
        result.append(SubtitleEntry(index=entry.index, start=entry.start, end=entry.end, text=override))
    return result


def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    centis = round((secs - int(secs)) * 100)
    whole_secs = int(secs)
    if centis == 100:
        centis = 0
        whole_secs += 1
    return f"{hours}:{minutes:02d}:{whole_secs:02d}.{centis:02d}"


def _escape_ass_text(text: str) -> str:
    # ASS override blocks are delimited by { }, and \ starts an escape/tag
    # sequence — an unescaped literal brace or backslash in dialogue text
    # would be silently parsed as a (probably broken) override instead of
    # displayed.
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def build_ass_document(
    entries: list[SubtitleEntry],
    style: StylePreset,
    play_res_x: int,
    play_res_y: int,
) -> str:
    """Render clip-relative subtitle entries into a standalone .ass document
    for ffmpeg's `subtitles` filter (libass) to burn in. play_res_x/y must
    match the clip's actual output frame size — libass scales the style's
    font size/margins against these, so a mismatch makes the text the wrong
    size relative to the frame rather than just misplaced."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        (
            f"Style: Default,{style.font},{style.font_size},{style.primary_color},"
            f"&H000000FF,{style.outline_color},{style.back_color},"
            f"{-1 if style.bold else 0},0,0,0,100,100,0,0,{style.border_style},"
            f"{style.outline},{style.shadow},{style.alignment},20,20,{style.margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for e in entries:
        text = strip_markup(e.text)
        if not text:
            continue
        if style.uppercase:
            text = text.upper()
        text = _escape_ass_text(text).replace("\n", "\\N")
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(e.start)},{_ass_timestamp(e.end)},"
            f"Default,,0,0,0,,{text}"
        )
    return "\n".join(lines) + "\n"
