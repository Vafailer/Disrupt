"""Cookie page and the files that make the cookie banner work."""

import re

PAGES = ["/", "/privacy", "/terms", "/data-deletion", "/cookies"]


def test_cookie_page_lists_real_cookies_and_storage(client):
    page = client.get("/cookies")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    for name in (
        "notes_session", "notes_tg_login", "notes_email_registration", "beresta_admin",
        "beresta.cookie-consent.v1", "beresta.onboarding.dismissed.v1", "beresta.reminderZone",
    ):
        assert name in page.text
    assert "Рекламные" in page.text and "support@berestaapp.ru" in page.text


def test_listed_cookie_names_match_the_code():
    from app import admin_auth, security
    from app.routes import auth_flows, email_auth

    assert {security.COOKIE_NAME, admin_auth.COOKIE_NAME, auth_flows.BINDING_COOKIE, email_auth.COOKIE} == {
        "notes_session", "beresta_admin", "notes_tg_login", "notes_email_registration",
    }


def test_every_public_page_loads_the_banner_without_inline_code(client):
    for path in PAGES:
        page = client.get(path)
        assert page.status_code == 200
        assert '/static/cookie-consent.js"' in page.text and '/static/cookie-consent.css"' in page.text
        assert "data-cookie-settings" in page.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", page.text) and " style=" not in page.text
    for path in ("/privacy", "/terms", "/data-deletion", "/"):
        assert 'href="/cookies"' in client.get(path).text
    assert client.get("/static/cookie-consent.js").status_code == 200
    assert client.get("/static/cookie-consent.css").status_code == 200


def test_policy_mentions_cookies_age_and_current_version(client):
    from app.policy import POLICY_VERSION

    page = client.get("/privacy").text
    assert POLICY_VERSION == "2026-10-12" and f"Версия {POLICY_VERSION}" in page
    assert 'href="/cookies"' in page and "старше 18 лет" in page


def test_landing_explains_the_ai_allowance_up_front(client):
    page = client.get("/").text
    assert 'id="landing-ai-allowance"' in page and "30 единиц" in page and "бесплатна" in page
    assert "Ваши записи видите только вы" not in page
