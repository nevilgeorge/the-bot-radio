import base64
import copy
import json
import os
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import anyio
import httpx
import pytest
import respx

from the_bot_radio.services import spotify_service
from the_bot_radio.services.inputs.base import make_http_client
from the_bot_radio.services.spotify_service import (
    API_BASE,
    TOKEN_URL,
    SpotifyCredentials,
    SpotifyError,
    SpotifyService,
    build_track_query,
)

from .conftest import FIXTURES, load_fixture

pytestmark = pytest.mark.anyio

CREDENTIALS = SpotifyCredentials("client-id", "client-secret", "refresh-token")
PLAYLIST_ID = "playlist-id"
COVER_JPEG = (FIXTURES / "spotify" / "cover.jpg").read_bytes()


def _token_response(**overrides: Any) -> httpx.Response:
    return httpx.Response(200, json={**load_fixture("spotify", "token.json"), **overrides})


@pytest.fixture
async def spotify():
    async with make_http_client() as http_client:
        yield SpotifyService(http_client, CREDENTIALS)


def _mock_token(**overrides: Any) -> respx.Route:
    return respx.post(TOKEN_URL).mock(return_value=_token_response(**overrides))


@respx.mock
async def test_refresh_sends_basic_auth_and_caches_token(spotify):
    token_route = _mock_token()
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("spotify", "search_tracks.json"))
    )
    await spotify.search_tracks("q")
    await spotify.search_tracks("q")

    assert token_route.call_count == 1
    token_request = token_route.calls.last.request
    expected_basic = base64.b64encode(b"client-id:client-secret").decode()
    assert token_request.headers["Authorization"] == f"Basic {expected_basic}"
    assert parse_qs(token_request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-token"],
    }
    access_token = load_fixture("spotify", "token.json")["access_token"]
    api_request = respx.calls.last.request
    assert api_request.headers["Authorization"] == f"Bearer {access_token}"


@respx.mock
async def test_rotated_refresh_token_is_used(spotify):
    _mock_token(refresh_token="rotated-token")
    await spotify.refresh_token()
    assert spotify.credentials.refresh_token == "rotated-token"


@respx.mock
async def test_token_error_raises(spotify):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Invalid refresh token"}
        )
    )
    with pytest.raises(SpotifyError, match="400: invalid_grant: Invalid refresh token"):
        await spotify.refresh_token()


@respx.mock
async def test_401_refreshes_once_then_retries(spotify):
    token_route = _mock_token()
    expired = httpx.Response(401, json={"error": {"status": 401, "message": "expired"}})
    search_route = respx.get(f"{API_BASE}/search").mock(
        side_effect=[
            expired,
            httpx.Response(200, json=load_fixture("spotify", "search_tracks.json")),
        ]
    )
    assert await spotify.search_tracks("q")
    assert token_route.call_count == 2 and search_route.call_count == 2


@respx.mock
async def test_second_401_raises(spotify):
    _mock_token()
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(401, json={"error": {"status": 401, "message": "nope"}})
    )
    with pytest.raises(SpotifyError, match="401: nope") as error_info:
        await spotify.search_tracks("q")
    assert error_info.value.status_code == 401


@respx.mock
async def test_429_waits_for_retry_after(spotify, monkeypatch):
    fake_sleep = AsyncMock()
    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    _mock_token()
    respx.get(f"{API_BASE}/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, json=load_fixture("spotify", "search_tracks.json")),
        ]
    )
    assert await spotify.search_tracks("q")
    fake_sleep.assert_awaited_once_with(spotify_service.RETRY_AFTER_MAX_SECONDS)


@respx.mock
async def test_search_clamps_limit_and_parses_tracks(spotify):
    _mock_token()
    search_payload = load_fixture("spotify", "search_tracks.json")
    search_route = respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json=search_payload)
    )
    tracks = await spotify.search_tracks('track:"x"', limit=50, offset=10)

    params = search_route.calls.last.request.url.params
    assert params["type"] == "track" and params["limit"] == "10" and params["offset"] == "10"
    assert params["q"] == 'track:"x"'
    first_payload = search_payload["tracks"]["items"][0]
    assert len(tracks) == len(search_payload["tracks"]["items"])
    assert tracks[0].id == first_payload["id"] and tracks[0].uri == first_payload["uri"]
    assert tracks[0].artists == [artist["name"] for artist in first_payload["artists"]]
    assert tracks[0].album == first_payload["album"]["name"]


def test_build_track_query_strips_quotes():
    assert build_track_query('Say "Hi"', "Low") == 'track:"Say Hi" artist:"Low"'


@respx.mock
async def test_create_playlist_posts_to_me_playlists(spotify):
    _mock_token()
    create_payload = load_fixture("spotify", "create_playlist.json")
    create_route = respx.post(f"{API_BASE}/me/playlists").mock(
        return_value=httpx.Response(201, json=create_payload)
    )
    playlist = await spotify.create_playlist("Name", "Notes", public=False)

    assert json.loads(create_route.calls.last.request.content) == {
        "name": "Name",
        "description": "Notes",
        "public": False,
    }
    assert playlist.id == create_payload["id"] and playlist.tracks == []
    assert playlist.snapshot_id == create_payload["snapshot_id"]


@pytest.mark.parametrize("public", [True, False])
@respx.mock
async def test_set_playlist_visibility_sends_only_public(spotify, public: bool) -> None:
    _mock_token()
    visibility_route = respx.put(f"{API_BASE}/playlists/{PLAYLIST_ID}").mock(
        return_value=httpx.Response(200)
    )

    await spotify.set_playlist_visibility(PLAYLIST_ID, public=public)

    assert visibility_route.call_count == 1
    assert json.loads(visibility_route.calls.last.request.content) == {"public": public}


async def _perform_mutation(spotify: SpotifyService, mutation: str) -> None:
    """Exercise each publication mutation through its public service method."""
    if mutation == "create":
        await spotify.create_playlist("Morning Static", "Notes", public=False)
    elif mutation == "add":
        await spotify.add_items_to_playlist(PLAYLIST_ID, ["spotify:track:track-id"])
    else:
        await spotify.set_playlist_visibility(PLAYLIST_ID, public=True)


def _mutation_route(mutation: str) -> respx.Route:
    """Return the mocked route for a publication mutation."""
    if mutation == "create":
        return respx.post(f"{API_BASE}/me/playlists")
    if mutation == "add":
        return respx.post(f"{API_BASE}/playlists/{PLAYLIST_ID}/items")
    return respx.put(f"{API_BASE}/playlists/{PLAYLIST_ID}")


@pytest.mark.parametrize("mutation", ["create", "add", "visibility"])
@respx.mock
async def test_publication_mutations_do_not_retry_transport_errors(spotify, mutation: str) -> None:
    _mock_token()
    mutation_route = _mutation_route(mutation).mock(side_effect=httpx.ReadTimeout("lost response"))

    with pytest.raises(httpx.ReadTimeout, match="lost response"):
        await _perform_mutation(spotify, mutation)

    assert mutation_route.call_count == 1


@pytest.mark.parametrize("mutation", ["create", "add", "visibility"])
@respx.mock
async def test_publication_mutations_do_not_retry_cancellation(spotify, mutation: str) -> None:
    _mock_token()
    dispatched_requests: list[httpx.Request] = []
    with anyio.CancelScope() as cancellation_scope:

        async def cancel_request(request: httpx.Request) -> httpx.Response:
            """Cancel after dispatch, while the server outcome is unknown."""
            dispatched_requests.append(request)
            cancellation_scope.cancel()
            await anyio.sleep(0)
            raise AssertionError("cancelled request continued")

        _mutation_route(mutation).mock(side_effect=cancel_request)
        await _perform_mutation(spotify, mutation)

    assert cancellation_scope.cancel_called
    assert len(dispatched_requests) == 1


@pytest.mark.parametrize("payload", [{}, {"snapshot_id": None}, {"snapshot_id": ""}, []])
@respx.mock
async def test_add_items_rejects_malformed_acknowledgement_without_retry(spotify, payload) -> None:
    _mock_token()
    add_route = _mutation_route("add").mock(return_value=httpx.Response(201, json=payload))

    with pytest.raises(SpotifyError, match="malformed acknowledgement"):
        await _perform_mutation(spotify, "add")

    assert add_route.call_count == 1


@pytest.mark.parametrize("known_playlist_id", [None, PLAYLIST_ID])
@respx.mock
async def test_create_rejects_malformed_acknowledgement_without_retry(
    spotify, known_playlist_id: str | None
) -> None:
    _mock_token()
    payload = {} if known_playlist_id is None else {"id": known_playlist_id}
    create_route = _mutation_route("create").mock(return_value=httpx.Response(201, json=payload))

    with pytest.raises(SpotifyError, match="malformed acknowledgement") as error_info:
        await _perform_mutation(spotify, "create")

    assert create_route.call_count == 1
    assert error_info.value.spotify_playlist_id == known_playlist_id


@pytest.mark.parametrize("mutation", ["create", "add", "visibility"])
@pytest.mark.parametrize("status_code", [401, 429])
@respx.mock
async def test_publication_mutations_keep_explicit_rejection_retries(
    spotify, mutation: str, status_code: int, monkeypatch
) -> None:
    token_route = _mock_token()
    monkeypatch.setattr(anyio, "sleep", AsyncMock())
    response_payload = (
        load_fixture("spotify", "create_playlist.json")
        if mutation == "create"
        else {"snapshot_id": "verified-snapshot"}
    )
    mutation_route = _mutation_route(mutation).mock(
        side_effect=[
            httpx.Response(status_code, headers={"Retry-After": "1"}),
            httpx.Response(200, json=response_payload),
        ]
    )

    await _perform_mutation(spotify, mutation)

    assert mutation_route.call_count == 2
    assert token_route.call_count == (2 if status_code == 401 else 1)


@respx.mock
async def test_get_playlist_follows_pages_and_skips_null_items(spotify):
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    all_rows = playlist_payload["items"]["items"]
    assert len(all_rows) >= 2, "fixture needs at least two items to split into pages"
    next_url = f"{API_BASE}/playlists/{playlist_payload['id']}/items?offset=1&limit=1"
    first_page = copy.deepcopy(playlist_payload)
    first_page["items"]["items"] = all_rows[:1]
    first_page["items"]["next"] = next_url
    second_page = {"items": [{"item": None}, *all_rows[1:]], "next": None}

    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=first_page)
    )
    page_route = respx.get(next_url).mock(return_value=httpx.Response(200, json=second_page))
    playlist = await spotify.get_playlist(playlist_payload["id"])

    assert page_route.call_count == 1
    assert [track.id for track in playlist.tracks] == [row["item"]["id"] for row in all_rows]
    assert playlist.name == playlist_payload["name"]


@pytest.mark.parametrize("contents", [None, "missing"])
@respx.mock
async def test_get_playlist_strict_rejects_unavailable_contents(spotify, contents):
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    if contents == "missing":
        playlist_payload.pop("items")
    else:
        playlist_payload["items"] = contents
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )
    with pytest.raises(SpotifyError, match="own or collaborate"):
        await spotify.get_playlist(playlist_payload["id"], require_contents=True)
    assert (await spotify.get_playlist(playlist_payload["id"])).tracks == []


@respx.mock
async def test_get_playlist_strict_accepts_empty_and_legacy_contents(spotify):
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    playlist_payload.pop("items")
    playlist_payload["tracks"] = {"items": [], "next": None}
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )
    assert (await spotify.get_playlist(playlist_payload["id"], require_contents=True)).tracks == []


@pytest.mark.parametrize("contents", [{}, {"items": None}, {"items": [None]}, {"items": [{}]}])
@respx.mock
async def test_get_playlist_strict_rejects_malformed_contents(spotify, contents):
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    playlist_payload["items"] = contents
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )
    with pytest.raises(SpotifyError, match="malformed or incomplete"):
        await spotify.get_playlist(playlist_payload["id"], require_contents=True)


@respx.mock
async def test_get_playlist_strict_checks_later_pages(spotify):
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    next_url = f"{API_BASE}/playlists/{playlist_payload['id']}/items?offset=1"
    playlist_payload["items"]["next"] = next_url
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )
    respx.get(next_url).mock(return_value=httpx.Response(200, json={"next": None}))
    with pytest.raises(SpotifyError, match="malformed or incomplete"):
        await spotify.get_playlist(playlist_payload["id"], require_contents=True)


@pytest.mark.parametrize("field,value", [("next", 7), ("total", "3"), ("total", -1)])
@respx.mock
async def test_get_playlist_strict_rejects_invalid_pagination(spotify, field, value) -> None:
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    playlist_payload["items"][field] = value
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )

    with pytest.raises(SpotifyError, match="malformed or incomplete"):
        await spotify.get_playlist(playlist_payload["id"], require_contents=True)


@respx.mock
async def test_get_playlist_strict_rejects_truncated_contents(spotify) -> None:
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    playlist_payload["items"]["items"].pop()
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )

    with pytest.raises(SpotifyError, match="total mismatch"):
        await spotify.get_playlist(playlist_payload["id"], require_contents=True)


@respx.mock
async def test_get_playlist_strict_rejects_repeated_pages(spotify) -> None:
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    next_url = f"{API_BASE}/playlists/{playlist_payload['id']}/items?offset=3"
    playlist_payload["items"]["next"] = next_url
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )
    next_route = respx.get(next_url).mock(
        return_value=httpx.Response(200, json={"items": [], "next": next_url})
    )

    with pytest.raises(SpotifyError, match="repeated page"):
        await spotify.get_playlist(playlist_payload["id"], require_contents=True)

    assert next_route.call_count == 1


@respx.mock
async def test_add_items_batches_in_order_and_advances_position(spotify):
    _mock_token()
    add_payload = load_fixture("spotify", "add_items.json")
    add_route = respx.post(f"{API_BASE}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(201, json=add_payload)
    )
    track_uris = [f"spotify:track:{index}" for index in range(230)]
    snapshot_id = await spotify.add_items_to_playlist(PLAYLIST_ID, track_uris, position=5)

    bodies = [json.loads(call.request.content) for call in add_route.calls]
    assert [len(body["uris"]) for body in bodies] == [100, 100, 30]
    assert [uri for body in bodies for uri in body["uris"]] == track_uris
    assert [body["position"] for body in bodies] == [5, 105, 205]
    assert snapshot_id == add_payload["snapshot_id"]


async def test_add_items_rejects_empty(spotify):
    with pytest.raises(ValueError, match="no track URIs"):
        await spotify.add_items_to_playlist(PLAYLIST_ID, [])


@respx.mock
async def test_upload_cover_sends_base64_jpeg(spotify):
    _mock_token()
    cover_route = respx.put(f"{API_BASE}/playlists/{PLAYLIST_ID}/images").mock(
        return_value=httpx.Response(202)
    )
    await spotify.upload_playlist_cover(PLAYLIST_ID, COVER_JPEG)

    cover_request = cover_route.calls.last.request
    assert cover_request.headers["Content-Type"] == "image/jpeg"
    assert base64.b64decode(cover_request.content) == COVER_JPEG


async def test_upload_cover_rejects_non_jpeg_and_oversize(spotify):
    with pytest.raises(ValueError, match="must be a JPEG"):
        await spotify.upload_playlist_cover(PLAYLIST_ID, b"\x89PNG....")
    oversize_jpeg = spotify_service.JPEG_MAGIC + b"\0" * spotify_service.COVER_MAX_BYTES
    with pytest.raises(ValueError, match="max is"):
        await spotify.upload_playlist_cover(PLAYLIST_ID, oversize_jpeg)


def test_credentials_from_env_reports_missing(monkeypatch):
    for env_var_name in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"):
        monkeypatch.delenv(env_var_name, raising=False)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "set")
    with pytest.raises(RuntimeError, match="SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN"):
        SpotifyCredentials.from_env()


@respx.mock
async def test_current_user_playlists_follows_all_pages_and_keeps_metadata(spotify):
    _mock_token()
    first_playlist = load_fixture("spotify", "get_playlist.json")
    second_playlist = {**copy.deepcopy(first_playlist), "id": "another-playlist"}
    next_url = f"{API_BASE}/me/playlists?offset=50&limit=50"
    first_route = respx.get(f"{API_BASE}/me/playlists", params={"limit": 50, "offset": 0}).mock(
        return_value=httpx.Response(200, json={"items": [first_playlist], "next": next_url})
    )
    next_route = respx.get(next_url).mock(
        return_value=httpx.Response(200, json={"items": [None, second_playlist], "next": None})
    )

    playlists = await spotify.get_current_user_playlists()

    assert playlists == [first_playlist, second_playlist]
    assert first_route.call_count == next_route.call_count == 1


@respx.mock
async def test_current_user_playlists_empty_and_repeated_page(spotify):
    _mock_token()
    playlist_route = respx.get(f"{API_BASE}/me/playlists").mock(
        return_value=httpx.Response(200, json={"items": [], "next": None})
    )
    assert await spotify.get_current_user_playlists() == []
    playlist_route.mock(
        return_value=httpx.Response(200, json={"items": [], "next": "/me/playlists"})
    )
    with pytest.raises(SpotifyError, match="repeated page"):
        await spotify.get_current_user_playlists()


@pytest.mark.parametrize("payload", [{}, {"items": [7]}, {"items": [], "next": 7}])
@respx.mock
async def test_current_user_playlists_rejects_incomplete_listing(spotify, payload):
    _mock_token()
    respx.get(f"{API_BASE}/me/playlists").mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(SpotifyError):
        await spotify.get_current_user_playlists()


@respx.mock
async def test_raw_playlist_details_preserves_omitted_contents_and_metadata(spotify):
    _mock_token()
    playlist_payload = load_fixture("spotify", "get_playlist.json")
    playlist_payload.pop("items", None)
    respx.get(f"{API_BASE}/playlists/{playlist_payload['id']}").mock(
        return_value=httpx.Response(200, json=playlist_payload)
    )

    assert await spotify.get_playlist_details(playlist_payload["id"]) == playlist_payload


# --- live -------------------------------------------------------------------------------------

LIVE_QUERIES = [
    ("Teardrop", "Massive Attack"),
    ("Pink + White", "Frank Ocean"),
    ("Re: Stacks", "Bon Iver"),
]


def _trim_for_fixture(fixture_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Drop secrets and bulky fields from a live response before saving it as a fixture."""
    if fixture_name == "token.json":
        payload = {**payload, "access_token": "test-access-token"}
        payload.pop("refresh_token", None)
    if fixture_name == "search_tracks.json":
        payload["tracks"]["items"] = payload["tracks"]["items"][:2]
    for track_holder in _iter_track_payloads(payload):
        album = track_holder.get("album") or {}
        album.pop("images", None)
        album.pop("artists", None)
    payload.pop("images", None)
    return payload


def _iter_track_payloads(payload: dict[str, Any]):
    """Every track object inside a search or playlist payload."""
    yield from payload.get("tracks", {}).get("items") or []
    for row in (payload.get("items") or {}).get("items") or []:
        if row.get("item"):
            yield row["item"]


@pytest.mark.live
async def test_live_publish_round_trip():
    """Refresh → search → create (private) → add 3 → cover → read back.

    With SPOTIFY_CAPTURE_FIXTURES=1, trimmed responses are written to fixtures/spotify/.
    """
    capture_fixtures = os.environ.get("SPOTIFY_CAPTURE_FIXTURES") == "1"
    captured_payloads: dict[str, dict[str, Any]] = {}

    async def capture_response(response: httpx.Response) -> None:
        await response.aread()
        if not capture_fixtures or response.is_error or not response.content:
            return
        request = response.request
        path = request.url.path
        if str(request.url).startswith(TOKEN_URL):
            fixture_name = "token.json"
        elif path.endswith("/search") and "search_tracks.json" not in captured_payloads:
            fixture_name = "search_tracks.json"
        elif path.endswith("/me/playlists"):
            fixture_name = "create_playlist.json"
        elif path.endswith("/items") and request.method == "POST":
            fixture_name = "add_items.json"
        elif request.method == "GET" and "/playlists/" in path:
            fixture_name = "get_playlist.json"
        else:
            return
        captured_payloads[fixture_name] = response.json()

    async with make_http_client(event_hooks={"response": [capture_response]}) as http_client:
        spotify = SpotifyService(http_client)
        found_tracks = []
        for title, artist in LIVE_QUERIES:
            matches = await spotify.search_tracks(build_track_query(title, artist), limit=3)
            assert matches, f"no match for {title} / {artist}"
            found_tracks.append(matches[0])
            print(f"search {title!r}: {matches[0].name} — {', '.join(matches[0].artists)}")

        playlist = await spotify.create_playlist(
            "The Bot Radio — test", "Live test playlist; safe to delete.", public=False
        )
        print(f"created {playlist.id} {playlist.external_url}")
        await spotify.add_items_to_playlist(playlist.id, [track.uri for track in found_tracks])
        await spotify.upload_playlist_cover(playlist.id, COVER_JPEG)
        fetched_playlist = await spotify.get_playlist(playlist.id)

    assert [track.id for track in fetched_playlist.tracks] == [track.id for track in found_tracks]
    print(f"embed: https://open.spotify.com/embed/playlist/{playlist.id}")

    for fixture_name, payload in captured_payloads.items():
        fixture_path = FIXTURES / "spotify" / fixture_name
        trimmed_payload = _trim_for_fixture(fixture_name, payload)
        fixture_path.write_text(json.dumps(trimmed_payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {fixture_path}")
