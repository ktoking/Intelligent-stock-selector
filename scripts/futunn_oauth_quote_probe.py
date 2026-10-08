#!/usr/bin/env python3
"""One-shot, read-only Futunn OpenAPI OAuth PKCE connectivity probe.

The OAuth client, verifier, authorization code, and tokens live only in memory.
No OpenD or third-party Python package is needed.
"""

import base64
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


API = "https://webapi.futunn.com"
PORT = 60355
REDIRECT = f"http://localhost:{PORT}/callback"


def request_json(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def main():
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and not sys.argv[1].startswith("US.")):
        raise SystemExit("Usage: python3 scripts/futunn_oauth_quote_probe.py [US.AAPL]")
    symbol = sys.argv[1] if len(sys.argv) == 2 else "US.AAPL"

    client = request_json(
        f"{API}/oauth2/register",
        {
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "stock-agent read-only quote probe",
        },
    )
    client_id = client["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "state": state,
        }
    )
    auth_url = f"{API}/oauth2/authorize/confirm?{query}"

    class Callback(BaseHTTPRequestHandler):
        result = None

        def log_message(self, *_args):
            pass  # A request path contains the short-lived authorization code.

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback" or params.get("state") != [state]:
                self.send_error(400, "Invalid OAuth callback")
                return
            Callback.result = params
            message = "Authorization received. You can return to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode())

    server = HTTPServer(("127.0.0.1", PORT), Callback)
    server.timeout = 180
    print("OAuth client registration: OK")
    print("Opening Futunn authorization page. If it does not open, use this URL:")
    print(auth_url)
    webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()
    params = Callback.result
    if params is None:
        raise RuntimeError("No OAuth callback received within 180 seconds")
    if "error" in params:
        raise RuntimeError(f"OAuth authorization error: {params['error'][0]}")
    code = params.get("code", [None])[0]
    if not code:
        raise RuntimeError("OAuth callback did not contain an authorization code")

    token_body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }
    ).encode()
    token_request = urllib.request.Request(
        f"{API}/oauth2/token",
        data=token_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(token_request, timeout=20) as response:
        token = json.load(response)
    print(f"OAuth token: OK; scope={token.get('scope', '<unspecified>')}")

    quote = request_json(
        f"{API}/api/v1.0/quote/snapshot",
        {"code_list": [symbol]},
        {"Authorization": f"Bearer {token['access_token']}"},
    )
    print(f"Quote request: HTTP OK; symbol={symbol}")
    print(json.dumps(quote, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(f"Futunn HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from None
