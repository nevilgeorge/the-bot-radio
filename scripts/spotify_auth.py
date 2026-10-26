"""One-time OAuth for the bot's Spotify account: prints a refresh token for `.env`.

Usage (log in to Spotify in your browser as the bot account first):

    uv run python scripts/spotify_auth.py

Reads SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET from `.env`. Register `REDIRECT_URI` exactly in
the Spotify app dashboard (Spotify rejects `localhost`; it must be the loopback IP).
"""

import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from the_bot_radio.services.spotify_service import SCOPES, TOKEN_URL
from the_bot_radio.web import config  # noqa: F401  (loads .env)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8888
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"


def wait_for_callback(expected_state: str) -> str:
    """Serve one request on the redirect URI and return its `code` (after checking `state`)."""
    callback_params: dict[str, list[str]] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        """Captures the query string of the OAuth redirect."""

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            callback_params.update(parse_qs(urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and return to the terminal.")

        def log_message(self, *_args: object) -> None:
            """Keep the terminal quiet."""

    with HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler) as callback_server:
        callback_server.handle_request()

    if "error" in callback_params:
        sys.exit(f"Authorization failed: {callback_params['error'][0]}")
    if callback_params.get("state", [""])[0] != expected_state:
        sys.exit("State mismatch in the callback; aborting.")
    return callback_params["code"][0]


def main() -> None:
    """Run the Authorization Code flow and print the refresh token."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.")

    oauth_state = secrets.token_urlsafe(16)
    authorize_query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": SCOPES,
            "redirect_uri": REDIRECT_URI,
            "state": oauth_state,
        }
    )
    authorize_link = f"{AUTHORIZE_URL}?{authorize_query}"
    print(f"Opening Spotify to authorize (as the bot account):\n  {authorize_link}\n")
    webbrowser.open(authorize_link)

    authorization_code = wait_for_callback(oauth_state)
    token_response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": REDIRECT_URI,
        },
        auth=(client_id, client_secret),
    )
    if token_response.is_error:
        sys.exit(f"Token exchange failed ({token_response.status_code}): {token_response.text}")
    token_payload = token_response.json()
    print(f"Granted scopes: {token_payload.get('scope')}")
    print(f"\nAdd this to .env:\n\nSPOTIFY_REFRESH_TOKEN={token_payload['refresh_token']}")


if __name__ == "__main__":
    main()
