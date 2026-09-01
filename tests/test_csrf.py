from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.runtime import SettingsHolder
from app.settings import LibraryConfig, Settings
from app.web.app import create_web_app
from app.web.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME


async def _noop() -> None:
    return None


def _build_app(settings: Settings | None = None):
    return create_web_app(SettingsHolder(settings=settings), _noop)


def _configured_settings(**overrides) -> Settings:
    defaults = dict(
        discord_token="",
        plex_url="http://plex.example",
        plex_token="real-secret-plex-token",
        libraries=[LibraryConfig(name="Movies")],
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---- The CRITICAL finding: the wizard's Plex-connect step must never send
# the live Plex token to a URL the caller supplies without proof the
# request actually came from the same browser that loaded the wizard page. ----


def test_plex_connect_without_csrf_token_is_rejected_before_any_plex_call(monkeypatch):
    called = {"count": 0}

    def fake_connect(plex_url, account_token):
        # If this ever runs for a request that skipped CSRF validation, the
        # real (seeded) Plex token would have just been sent to plex_url.
        called["count"] += 1
        return "attacker-server", []

    monkeypatch.setattr("app.web.app._connect_and_discover_sync", fake_connect)

    app = _build_app(_configured_settings())
    victim = TestClient(app)

    # Seed state.plex_account_token from the live settings, exactly like the
    # real attack path: a bare GET into the wizard's connect step. (This
    # legitimately calls fake_connect itself via auto-discovery, so the
    # baseline count below is taken *after* this, not assumed to be zero.)
    victim.get("/wizard/connect")
    assert victim.cookies.get(CSRF_COOKIE_NAME) is not None
    baseline = called["count"]

    # Forge the follow-up POST the way a third-party page would: a second
    # client with an empty cookie jar hitting the SAME running app (a
    # cross-origin page can't read the victim's cookie or know the token to
    # put one in), pointing at an attacker-controlled server.
    forged = TestClient(app)
    response = forged.post("/wizard/plex/connect", data={"plex_url": "http://attacker.example"})

    assert response.status_code == 403
    assert called["count"] == baseline, (
        "the real Plex token must never reach _connect_and_discover_sync for a forged request"
    )


def test_plex_connect_with_stolen_cookie_but_no_token_header_is_rejected(monkeypatch):
    # Slightly stronger attacker: somehow learns the cookie value (e.g. it
    # leaked some other way) but still can't produce the matching
    # header/form field an actual page render would have embedded.
    called = {"count": 0}

    def fake_connect(plex_url, account_token):
        called["count"] += 1
        return "x", []

    monkeypatch.setattr("app.web.app._connect_and_discover_sync", fake_connect)

    client = TestClient(_build_app(_configured_settings()))
    client.get("/wizard/connect")
    assert client.cookies.get(CSRF_COOKIE_NAME)
    baseline = called["count"]

    response = client.post(
        "/wizard/plex/connect",
        data={"plex_url": "http://attacker.example"},
        # deliberately no X-CSRF-Token header and no csrf_token form field
    )
    assert response.status_code == 403
    assert called["count"] == baseline


def test_plex_connect_with_matching_csrf_token_succeeds(monkeypatch):
    # The legitimate flow must keep working end to end: real browser loads
    # a wizard page (gets cookie + sees the token embedded), then submits
    # the form, echoing the token back — exactly what htmx's hx-headers
    # (set from request.state.csrf_token in the base templates) does.
    monkeypatch.setattr(
        "app.web.app._connect_and_discover_sync",
        lambda plex_url, account_token: ("My Plex Server", []),
    )

    client = TestClient(_build_app(_configured_settings()))
    page = client.get("/wizard/connect")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    # The rendered page must actually carry the same token somewhere for
    # htmx to pick up (via hx-headers on the shell).
    assert token in page.text

    response = client.post(
        "/wizard/plex/connect",
        data={"plex_url": "http://real-plex.local:32400"},
        headers={CSRF_HEADER_NAME: token},
    )
    assert response.status_code == 200
    assert "error-banner" not in response.text
    # The connect step actually ran (not short-circuited by CSRF) and
    # advanced the wizard to the libraries step.
    assert "Which libraries" in response.text or "library" in response.text.lower()


def test_plex_connect_with_csrf_token_as_form_field_also_succeeds():
    # Non-JS clients (or the one native <form> in Settings that isn't
    # htmx-driven) must be able to submit the token as a plain form field
    # instead of a header.
    client = TestClient(_build_app(_configured_settings()))
    client.get("/wizard/connect")
    token = client.cookies.get(CSRF_COOKIE_NAME)

    response = client.post("/wizard/connect/reset", data={"csrf_token": token})
    assert response.status_code == 200
    assert "Which media server do you use?" in response.text


# ---- CSRF must cover the whole wizard/dashboard surface, not just Plex. ----


@pytest.mark.parametrize(
    "path",
    [
        "/wizard/discord",
        "/wizard/connect",
        "/wizard/plex/connect",
        "/wizard/jellyfin/connect",
        "/wizard/libraries",
        "/wizard/sync",
        "/wizard/finish",
        "/wizard/connect/reset",
        "/wizard/plex/reauth",
        "/sync/run",
    ],
)
def test_state_changing_routes_reject_requests_with_no_csrf_evidence_at_all(path):
    # No prior GET at all — no cookie exists, so there is nothing a forged
    # cross-origin request could possibly have echoed back correctly.
    forged = TestClient(_build_app(_configured_settings()))
    response = forged.post(path, data={})
    assert response.status_code == 403


# ---- The two side-effecting GETs must no longer be GET-reachable. ----


def test_connect_reset_no_longer_a_bare_get():
    client = TestClient(_build_app(_configured_settings()))
    response = client.get("/wizard/connect/reset")
    assert response.status_code == 405


def test_plex_reauth_no_longer_a_bare_get():
    client = TestClient(_build_app(_configured_settings()))
    response = client.get("/wizard/plex/reauth")
    assert response.status_code == 405


# ---- Cross-origin Origin header is rejected even with a matching cookie
# (defense in depth, not a substitute for the token check). ----


def test_mismatched_origin_header_is_rejected_even_with_valid_token():
    client = TestClient(_build_app(_configured_settings()))
    client.get("/wizard/connect")
    token = client.cookies.get(CSRF_COOKIE_NAME)

    response = client.post(
        "/wizard/connect/reset",
        data={"csrf_token": token},
        headers={"origin": "http://attacker.example"},
    )
    assert response.status_code == 403


# ---- Legitimate reconfiguration must still work end to end after the fix. ----


def test_settings_plex_switch_link_carries_a_working_csrf_token():
    # The Settings page's "Switch to Plex/Jellyfin instead?" control is a
    # native (non-htmx) <form method="post">, so it can't rely on htmx's
    # hx-headers — it must embed a real, working token as a hidden field.
    client = TestClient(_build_app(_configured_settings()))
    page = client.get("/settings/plex")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    assert f'value="{token}"' in page.text

    response = client.post("/wizard/connect/reset?return_to=plex", data={"csrf_token": token})
    assert response.status_code == 200
