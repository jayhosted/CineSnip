import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.runtime import SettingsHolder
from app.settings import Settings
from app.web.styles import register_styles_routes

_TEMPLATES_DIR = "app/web/templates"


@pytest.fixture
def settings_holder(tmp_path):
    settings = Settings(
        discord_token="x", plex_url="http://x", plex_token="x",
        cache_dir=tmp_path / "cache",
    )
    return SettingsHolder(settings=settings)


@pytest.fixture
def client(settings_holder, monkeypatch):
    app = FastAPI()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    def fake_write_config_yaml(new_settings, config_path=None):
        settings_holder.settings = new_settings

    monkeypatch.setattr("app.web.styles.write_config_yaml", fake_write_config_yaml)

    async def on_setup_complete():
        return None

    register_styles_routes(app, templates, settings_holder, on_setup_complete)
    return TestClient(app)


def test_styles_list_shows_all_builtins(client):
    response = client.get("/styles")
    assert response.status_code == 200
    for name in ("classic", "boxed", "cinematic", "meme"):
        assert name in response.text.lower()


def test_styles_save_creates_a_new_custom_preset(client, settings_holder):
    response = client.post(
        "/styles/save",
        data={
            "original_name": "", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "simpsons" in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_save_edits_a_builtin_field_without_renaming(client, settings_holder):
    response = client.post(
        "/styles/save",
        data={
            "original_name": "classic", "name": "classic", "font": "Liberation Sans",
            "font_size": "32", "primary_color": "&H00FFFFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert settings_holder.settings.style_presets()["classic"].font_size == 32


def test_styles_save_rejects_renaming_a_builtin(client, settings_holder):
    response = client.post(
        "/styles/save",
        data={
            "original_name": "classic", "name": "classic2", "font": "Liberation Sans",
            "font_size": "26", "primary_color": "&H00FFFFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "error" in response.text.lower() or "can't be renamed" in response.text.lower()
    assert "classic" in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_delete_rejects_a_builtin(client, settings_holder):
    response = client.post("/styles/classic/delete")
    assert response.status_code == 200
    assert "classic" in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_delete_removes_a_custom_preset(client, settings_holder):
    client.post(
        "/styles/save",
        data={
            "original_name": "", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    response = client.post("/styles/simpsons/delete")
    assert response.status_code == 200
    assert "simpsons" not in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def _valid_font_bytes() -> bytes:
    # Minimal-enough TrueType magic prefix for the upload route's own magic-
    # byte check; fc-scan is the real gate and is exercised via monkeypatch
    # below rather than shipping a real font fixture.
    return b"\x00\x01\x00\x00" + b"\x00" * 64


def test_styles_upload_font_then_save_does_not_wipe_the_preset(client, settings_holder, monkeypatch):
    # Regression for the final-review Critical finding: upload used to
    # re-render the form with preset=None, so a follow-up Save renamed the
    # real preset to "" and reset every other field to template defaults.
    from app.worker.font_upload import FontUploadResult

    async def fake_process_font_upload(data, filename, preset_name, fonts_dir):
        path = fonts_dir / f"{preset_name}.ttf"
        fonts_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return FontUploadResult(font_path=str(path), family="Simpsonfont", missing_punctuation="")

    monkeypatch.setattr("app.web.styles.process_font_upload", fake_process_font_upload)

    create_response = client.post(
        "/styles/save",
        data={
            "original_name": "", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert create_response.status_code == 200
    after_create = next(c for c in settings_holder.settings.subtitle_styles if c.name == "simpsons")
    assert after_create.font_path is None

    upload_response = client.post(
        "/styles/simpsons/font",
        files={"font_file": ("simpsons.ttf", _valid_font_bytes(), "font/ttf")},
    )
    assert upload_response.status_code == 200
    # font_path was previously a dead write-only field — confirm the upload
    # actually persisted it onto the real preset.
    after_upload = next(c for c in settings_holder.settings.subtitle_styles if c.name == "simpsons")
    assert after_upload.font_path is not None
    assert after_upload.font == "Simpsonfont"
    # The re-rendered form must show the real, current preset — not blanks.
    assert 'value="simpsons"' in upload_response.text

    save_response = client.post(
        "/styles/save",
        data={
            "original_name": "simpsons", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert save_response.status_code == 200
    names = {cfg.name for cfg in settings_holder.settings.subtitle_styles}
    assert "simpsons" in names
    assert "" not in names
    after_save = next(c for c in settings_holder.settings.subtitle_styles if c.name == "simpsons")
    assert after_save.font_path == after_upload.font_path
    assert after_save.font_size == 28


def test_styles_delete_removes_the_uploaded_font_file(client, settings_holder, monkeypatch, tmp_path):
    from app.worker.font_upload import FontUploadResult

    font_file = tmp_path / "cache" / "fonts" / "simpsons.ttf"

    async def fake_process_font_upload(data, filename, preset_name, fonts_dir):
        font_file.parent.mkdir(parents=True, exist_ok=True)
        font_file.write_bytes(data)
        return FontUploadResult(font_path=str(font_file), family="Simpsonfont", missing_punctuation="")

    monkeypatch.setattr("app.web.styles.process_font_upload", fake_process_font_upload)

    client.post(
        "/styles/save",
        data={
            "original_name": "", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    client.post(
        "/styles/simpsons/font",
        files={"font_file": ("simpsons.ttf", _valid_font_bytes(), "font/ttf")},
    )
    assert font_file.exists()

    client.post("/styles/simpsons/delete")
    assert not font_file.exists()


def test_styles_upload_font_rejects_when_preset_not_yet_saved(client):
    response = client.post(
        "/styles/not-yet-saved/font",
        files={"font_file": ("simpsons.ttf", _valid_font_bytes(), "font/ttf")},
    )
    assert response.status_code == 200
    assert "save the preset" in response.text.lower()


def test_new_style_form_hides_font_upload_control(client):
    response = client.get("/styles/new")
    assert response.status_code == 200
    assert "Upload font" not in response.text
    assert "Save the preset first" in response.text


def test_edit_style_form_shows_font_upload_control(client):
    response = client.get("/styles/classic/edit")
    assert response.status_code == 200
    assert "Upload font" in response.text


def test_styles_save_rejects_a_blank_name(client, settings_holder):
    response = client.post(
        "/styles/save",
        data={
            "original_name": "", "name": "", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "couldn" in response.text.lower() and "save" in response.text.lower()
    assert "" not in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_save_rejects_reserved_name_none(client, settings_holder):
    response = client.post(
        "/styles/save",
        data={
            "original_name": "", "name": "none", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "couldn" in response.text.lower() and "save" in response.text.lower()
    assert "none" not in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_worker_client_cache_returns_same_instance_across_preview_calls(client, monkeypatch):
    seen_clients = []
    original_get = None

    from app.web.generate import _WorkerClientCache

    original_get = _WorkerClientCache.get

    def spying_get(self, settings_holder):
        worker = original_get(self, settings_holder)
        seen_clients.append(worker)
        return worker

    monkeypatch.setattr(_WorkerClientCache, "get", spying_get)

    async def fake_preview_style(self, style):
        return b"\x89PNG\r\n\x1a\nfakepngbytes"

    monkeypatch.setattr("app.bot.worker_client.WorkerClient.preview_style", fake_preview_style)

    data = {
        "font": "Liberation Sans", "font_size": "26",
        "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
        "back_color": "&H00000000", "border_style": "1",
        "outline": "2.0", "shadow": "0.0", "margin_v": "24", "alignment": "2",
    }
    client.post("/styles/preview", data=data)
    client.post("/styles/preview", data=data)

    assert len(seen_clients) == 2
    assert seen_clients[0] is seen_clients[1]


def test_styles_font_upload_rejects_a_non_font_file(client):
    response = client.post(
        "/styles/classic/font",
        files={"font_file": ("not-a-font.ttf", b"definitely not a font", "font/ttf")},
    )
    assert response.status_code == 200
    assert "error" in response.text.lower() or "couldn't" in response.text.lower()


def test_styles_preview_proxies_the_worker_and_embeds_base64(client, monkeypatch):
    async def fake_preview_style(self, style):
        return b"\x89PNG\r\n\x1a\nfakepngbytes"

    monkeypatch.setattr(
        "app.bot.worker_client.WorkerClient.preview_style", fake_preview_style
    )
    response = client.post(
        "/styles/preview",
        data={
            "font": "Liberation Sans", "font_size": "26",
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
            "back_color": "&H00000000", "border_style": "1",
            "outline": "2.0", "shadow": "0.0", "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "data:image/png;base64," in response.text


def test_styles_edit_form_does_not_race_save_and_preview_on_one_element(client):
    # Regression: hx-post and hx-get used to both live on the <form>, so
    # clicking Save fired a POST and a GET concurrently, racing to swap
    # the content div. The form must carry only its save POST; the live
    # preview trigger belongs on #style-preview instead.
    response = client.get("/styles/classic/edit")
    assert response.status_code == 200
    import re

    form_match = re.search(r"<form\b[^>]*>", response.text)
    assert form_match, "expected a <form> element in the style edit template"
    form_tag = form_match.group(0)
    assert "hx-post" in form_tag
    assert "hx-get" not in form_tag
    assert "hx-target-error" not in response.text

    preview_match = re.search(r'<div id="style-preview"[^>]*>', response.text)
    assert preview_match, "expected #style-preview element"
    assert "delay:500ms" in preview_match.group(0)


def test_style_preview_fires_on_load_not_only_on_field_change(client):
    # Regression: the preview div only triggered on change/input, so opening
    # Edit (or the outerHTML swap right after a font upload) showed a blank
    # preview until the user happened to touch another field afterward —
    # reported as "upload doesn't preview it, even after saving and editing".
    response = client.get("/styles/classic/edit")
    assert response.status_code == 200
    import re

    preview_match = re.search(r'<div id="style-preview"[^>]*>', response.text)
    assert preview_match, "expected #style-preview element"
    assert "load" in preview_match.group(0).split('hx-trigger="')[1].split('"')[0]
