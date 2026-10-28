"""Route-level tests: status codes, titles, nav state, filters, prev/next, hot reload."""

import json
import os
import re
from datetime import date, datetime, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from the_bot_radio.resources import RESOURCE_DIR
from the_bot_radio.web import main as web_main
from the_bot_radio.web.config import TZ

LATEST = "2026-10-26"
FIXTURE_DJ = {
    "2026-10-15": "resident-03",
    "2026-10-16": "resident-04",
    "2026-10-17": "resident-02",
    "2026-10-18": "resident-01",
    "2026-10-19": "resident-03",
    "2026-10-20": "resident-04",
    "2026-10-21": "resident-02",
    "2026-10-22": "resident-01",
    "2026-10-23": "resident-03",
    "2026-10-24": "resident-04",
    "2026-10-25": "resident-01",
    "2026-10-26": "resident-02",
}

HTML_PAGES = {
    # path: (expected <title>, active nav href)
    "/": ("The Bot Radio — Today", "/"),
    "/index": ("The Bot Radio — The Index", "/index"),
    f"/index/{LATEST}": (f"The Bot Radio — Overcast, G running local ({LATEST})", "/index"),
    "/inputs": ("The Bot Radio — Inputs", "/inputs"),
    "/bots": ("The Bot Radio — Bots", "/bots"),
    "/about": ("The Bot Radio — About", "/about"),
}

NOT_FOUND_PATHS = [
    "/nope",
    "/index/2099-01-01",
    "/index/not-a-date",
    "/index/2026-13-45",
    "/index?month=bad",
    "/data/personas.json",
    "/data/days/2026-10-26.json",
    "/data/runs/private.json",
    "/published/index.json",
    "/.env",
    "/static/../data/personas.json",
]

DAY_LINK_RE = re.compile(r'href="/index/(\d{4}-\d{2}-\d{2})"')
A_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
HREF_RE = re.compile(r'href="([^"]*)"')


# ---- helpers -----------------------------------------------------------------------


def title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert m, "page has no <title>"
    return m.group(1).strip()


def h1_count(html: str) -> int:
    return len(re.findall(r"<h1[\s>]", html, re.IGNORECASE))


def day_links(html: str) -> set[str]:
    return set(DAY_LINK_RE.findall(html))


def hrefs_of_links_with(html: str, attr: str) -> list[str]:
    """hrefs of every <a> tag carrying the given attribute string (e.g. aria-current="page")."""
    out = []
    for tag in A_TAG_RE.findall(html):
        if attr in tag:
            m = HREF_RE.search(tag)
            out.append(m.group(1) if m else "")
    return out


def day_json(data_dir: Path, d: str) -> dict:
    return json.loads((data_dir / "days" / f"{d}.json").read_text(encoding="utf-8"))


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")
    # Make sure the store sees a changed mtime even on coarse-grained filesystems.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


# ---- status codes ------------------------------------------------------------------


@pytest.mark.parametrize("path", list(HTML_PAGES))
def test_html_pages_return_200(client: TestClient, path: str):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_healthz(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "latest_day": LATEST}


def test_static_css(client: TestClient):
    r = client.get("/static/css/site.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


@pytest.mark.parametrize("path", ["/static/js/share.js", "/static/img/favicon.svg"])
def test_shared_assets(client: TestClient, path: str):
    """The application serves the assets required by its rendered pages."""
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", NOT_FOUND_PATHS)
def test_not_found_pages(client: TestClient, path: str):
    r = client.get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert title_of(r.text) == "The Bot Radio — Dead air"
    assert "Dead air" in r.text
    assert h1_count(r.text) == 1
    assert 'href="/"' in r.text  # link back to Today


# ---- titles, headings, nav ---------------------------------------------------------


@pytest.mark.parametrize("path", list(HTML_PAGES))
def test_title_and_single_h1(client: TestClient, path: str):
    html = client.get(path).text
    assert title_of(html) == HTML_PAGES[path][0]
    assert h1_count(html) == 1


@pytest.mark.parametrize("path", list(HTML_PAGES))
def test_active_nav(client: TestClient, path: str):
    html = client.get(path).text
    current = hrefs_of_links_with(html, 'aria-current="page"')
    assert current, "no nav link marked aria-current=page"
    assert set(current) == {HTML_PAGES[path][1]}


def test_day_page_highlights_index_nav(client: TestClient):
    html = client.get("/index/2026-10-19").text
    assert set(hrefs_of_links_with(html, 'aria-current="page"')) == {"/index"}


def test_not_found_has_no_active_nav(client: TestClient):
    html = client.get("/nope").text
    assert hrefs_of_links_with(html, 'aria-current="page"') == []


# ---- The Index ---------------------------------------------------------------------


def test_index_lists_all_days(client: TestClient):
    html = client.get("/index").text
    assert day_links(html) == set(FIXTURE_DJ)


def test_index_dj_filter(client: TestClient):
    html = client.get("/index?dj=resident-02").text
    expected = {d for d, dj in FIXTURE_DJ.items() if dj == "resident-02"}
    assert expected == {"2026-10-17", "2026-10-21", "2026-10-26"}
    assert day_links(html) == expected

    selected = hrefs_of_links_with(html, 'aria-current="true"')
    assert len(selected) == 1
    assert "dj=resident-02" in selected[0]


def test_index_all_chip_selected_without_filter(client: TestClient):
    html = client.get("/index").text
    selected = hrefs_of_links_with(html, 'aria-current="true"')
    assert len(selected) == 1
    assert "dj=" not in selected[0]


def test_index_unknown_dj_is_empty_not_error(client: TestClient):
    r = client.get("/index?dj=nobody")
    assert r.status_code == 200
    assert day_links(r.text) == set()


def test_index_month_filter(client: TestClient):
    r = client.get("/index?month=2026-10")
    assert r.status_code == 200
    assert day_links(r.text) == set(FIXTURE_DJ)
    r = client.get("/index?month=2026-09")
    assert r.status_code == 200
    assert day_links(r.text) == set()


# ---- day page ----------------------------------------------------------------------


def test_day_page_prev_next_at_latest(client: TestClient):
    html = client.get(f"/index/{LATEST}").text
    # Only neighbour is the older day; no link to a newer day exists.
    assert day_links(html) - {LATEST} == {"2026-10-25"}


def test_day_page_prev_next_at_first(client: TestClient):
    html = client.get("/index/2026-10-15").text
    assert day_links(html) - {"2026-10-15"} == {"2026-10-16"}


def test_day_page_prev_next_in_middle(client: TestClient):
    html = client.get("/index/2026-10-20").text
    assert {"2026-10-19", "2026-10-21"} <= day_links(html)


def test_day_page_weekday_labels(client: TestClient):
    html = client.get(f"/index/{LATEST}").text
    assert "MON 10.26" in html
    # Neighbour label derived from 2026-10-25 (a Sunday), in whatever case the template uses.
    assert re.search(r"sun 10\.25", html, re.IGNORECASE)


def test_day_page_placeholder_embed_has_no_iframe(client: TestClient):
    html = client.get(f"/index/{LATEST}").text
    assert "<iframe" not in html
    assert "open.spotify.com/embed/playlist/PLACEHOLDER" not in html


def test_day_page_real_playlist_renders_iframe(client: TestClient, site_data_dir: Path):
    day = day_json(site_data_dir, LATEST)
    day["spotify_playlist_id"] = "37i9dQZF1DXcBWIGoYBM5M"
    write_json(site_data_dir / "days" / f"{LATEST}.json", day)

    html = client.get(f"/index/{LATEST}").text
    assert "<iframe" in html
    assert "open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M" in html

    today = client.get("/").text
    assert "open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M" in today


def test_day_page_prompt_collapsed_by_default(client: TestClient):
    html = client.get(f"/index/{LATEST}").text
    assert '<details class="day-prompt__more">' in html
    assert '<details class="day-prompt__more" open' not in html
    assert '<h3 class="day-prompt__label">Prompt</h3>' in html


def test_day_page_prompt_splits_archived_json(client: TestClient, site_data_dir: Path):
    day = day_json(site_data_dir, LATEST)
    day["prompt"] = json.dumps(
        {"system": "Be a DJ.\nPick songs.", "messages": [{"role": "user", "content": "Go"}]}
    )
    write_json(site_data_dir / "days" / f"{LATEST}.json", day)

    html = client.get(f"/index/{LATEST}").text
    assert '<h3 class="day-prompt__label">System</h3>' in html
    assert '<pre class="day-prompt__text">Be a DJ.\nPick songs.</pre>' in html
    assert '<h3 class="day-prompt__label">User</h3>' in html
    assert "&#34;messages&#34;" not in html and "&quot;messages&quot;" not in html


# ---- Today -------------------------------------------------------------------------


def test_today_shows_latest_and_four_recent_days(client: TestClient):
    html = client.get("/").text
    assert "Overcast, G running local" in html
    recent = day_links(html) - {LATEST}
    assert recent == {"2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"}


# ---- hot reload & resilience -------------------------------------------------------


def test_hot_reload_new_day(client: TestClient, site_data_dir: Path):
    assert client.get("/healthz").json()["latest_day"] == LATEST
    assert "Brand new morning" not in client.get("/").text

    day = day_json(site_data_dir, LATEST)
    day["date"] = "2026-10-27"
    day["title"] = "Brand new morning"
    day["generated_at"] = "2026-10-27T06:00:01-04:00"
    write_json(site_data_dir / "days" / "2026-10-27.json", day)

    assert client.get("/healthz").json() == {"ok": True, "latest_day": "2026-10-27"}
    today = client.get("/").text
    assert "Brand new morning" in today
    assert "2026-10-26" in day_links(today)  # yesterday drops into the recent cards
    r = client.get("/index/2026-10-27")
    assert r.status_code == 200
    assert title_of(r.text) == "The Bot Radio — Brand new morning (2026-10-27)"
    # The old latest day now has a newer neighbour.
    assert "2026-10-27" in day_links(client.get(f"/index/{LATEST}").text)


@pytest.mark.parametrize("path", [*HTML_PAGES, "/healthz"])
def test_invalid_day_file_is_skipped(client: TestClient, site_data_dir: Path, path: str):
    (site_data_dir / "days" / "2026-10-27.json").write_text("{not json", encoding="utf-8")
    (site_data_dir / "days" / "2026-10-28.json").write_text('{"date": "2026-10-28"}', "utf-8")
    r = client.get(path)
    assert r.status_code == 200
    if path == "/healthz":
        assert r.json()["latest_day"] == LATEST


def test_empty_data_dir_still_renders(client: TestClient, site_data_dir: Path):
    for p in (site_data_dir / "days").glob("*.json"):
        p.unlink()
    assert client.get("/healthz").json() == {"ok": True, "latest_day": None}
    for path in ["/", "/index", "/inputs", "/bots", "/about"]:
        assert client.get(path).status_code == 200, path
    assert client.get(f"/index/{LATEST}").status_code == 404


# ---- resident status -----------------------------------------------------------------


@pytest.fixture
def freeze_today(monkeypatch: pytest.MonkeyPatch):
    """Pin the pages' notion of "now" to 08:00 ET on the given date."""

    def freeze(day: date) -> None:
        frozen_now = datetime.combine(day, time(8), tzinfo=TZ)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001, ANN206
                return frozen_now.astimezone(tz) if tz else frozen_now.replace(tzinfo=None)

        monkeypatch.setattr(web_main, "datetime", FrozenDatetime)

    return freeze


def resident_cards(html: str) -> dict[str, str]:
    """Resident card HTML keyed by persona id (from the card's /index?dj= link)."""
    cards = re.findall(r'<a class="resident-card" href="/index\?dj=([^"]+)">(.*?)</a>', html, re.S)
    return dict(cards)


def test_bots_resident_on_air_today(client: TestClient, freeze_today):
    freeze_today(date(2026, 10, 26))
    html = client.get("/bots").text
    cards = resident_cards(html)
    assert "On air today" in cards["resident-02"]
    assert '<span class="led" aria-hidden="true">' in cards["resident-02"]
    assert "resident-card__status--on" in cards["resident-02"]
    assert "Last aired on SUN 10.25" in cards["resident-01"]
    assert 'class="led off"' in cards["resident-01"]
    assert "On air today · rolled at" in html


def test_bots_before_todays_record_nobody_on_air(client: TestClient, freeze_today):
    freeze_today(date(2026, 10, 27))
    html = client.get("/bots").text
    cards = resident_cards(html)
    assert not any("On air today" in card for card in cards.values())
    assert "Last aired on MON 10.26" in cards["resident-02"]
    assert "Last on air · MON 10.26" in html
    assert "On air today" not in html


def test_bots_resident_without_days(client: TestClient, freeze_today):
    freeze_today(date(2026, 10, 26))
    cards = resident_cards(client.get("/bots").text)
    assert "Not on air yet" in cards["resident-05"]
    assert 'class="led off"' in cards["resident-05"]


def test_bots_cards_show_bio_without_label(client: TestClient):
    cards = resident_cards(client.get("/bots").text)
    assert cards
    assert not any("Persona:" in card for card in cards.values())


def test_bots_has_no_last_played(client: TestClient):
    assert "Last played" not in client.get("/bots").text


def test_today_residents_before_todays_record(client: TestClient, freeze_today):
    freeze_today(date(2026, 10, 27))
    assert "On air today" not in client.get("/").text


# ---- about ----------------------------------------------------------------------------


def test_about_station_log_comes_from_store(client: TestClient):
    html = client.get("/about").text
    log = dict(re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html))
    personas = json.loads((RESOURCE_DIR / "personas.json").read_text())
    assert log["Residents"] == f"{len(personas)} bots"
    assert log["On air since"] == "Oct 15, 2026"  # earliest fixture day
    assert log["Drops at"] == "06:00 ET"


def test_about_sections(client: TestClient):
    html = client.get("/about").text
    assert h1_count(html) == 1
    rows = re.findall(r'<th scope="row">(.*?)</th>', html)
    assert len(rows) == 7
    assert "NOAA Tides &amp; Currents" in rows
    for href in ["/bots", "/inputs", "/"]:
        assert f'href="{href}"' in html
    assert 'href="https://www.thelotradio.com/" rel="noopener"' in html
    assert 'href="https://shop.thelotradio.com/" rel="noopener"' in html
    assert "Say hi" not in html


def test_today_has_no_newsletter_footer(client: TestClient):
    html = client.get("/").text
    assert "Get the drop in your inbox" not in html
    assert 'action="/subscribe"' not in html
    assert client.post("/subscribe", data={"email": "dj@example.com"}).status_code in (404, 405)
