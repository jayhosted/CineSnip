import pytest

from app.worker.style_preview import render_style_preview
from app.worker.subtitle_render import STYLE_PRESETS


@pytest.mark.anyio
async def test_render_style_preview_produces_a_png(tmp_path):
    png_bytes = await render_style_preview(
        STYLE_PRESETS["classic"],
        fonts_dir=tmp_path / "fonts",
        scratch_dir=tmp_path / "scratch",
    )
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic number
