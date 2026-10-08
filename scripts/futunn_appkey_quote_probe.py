#!/usr/bin/env python3
"""Read-only Futunn OpenAPI quote probe using an Ed25519 AppKey.

Requires the matching public key to be registered in the Futunn dashboard.
The private key is loaded from a local file and never printed or persisted here.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and not re.fullmatch(r"[A-Z]{2,3}\.[A-Za-z0-9]+", sys.argv[1])):
        raise SystemExit("Usage: python3 scripts/futunn_appkey_quote_probe.py [US.AAPL|HK.00700|...]")
    symbol = sys.argv[1] if len(sys.argv) == 2 else "US.AAPL"
    app_key = os.environ.get("FUTUNN_APP_KEY")
    key_file = os.environ.get("FUTUNN_PRIVATE_KEY_FILE")
    if not app_key or not key_file:
        raise SystemExit("Set FUTUNN_APP_KEY and FUTUNN_PRIVATE_KEY_FILE first")

    private_key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SystemExit("The private key must be Ed25519")

    path = "/api/v1.0/quote/snapshot"
    body = json.dumps({"code_list": [symbol]}, separators=(",", ":")).encode()
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join([timestamp, "POST", path, "", body_hash]).encode()
    signature = base64.b64encode(private_key.sign(signing_text)).decode()
    req = urllib.request.Request(
        f"https://webapi.futunn.com{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": app_key,
            "X-Timestamp": timestamp,
            "X-Nonce": secrets.token_urlsafe(24),
            "Authorization": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read(2000).decode('utf-8', errors='replace')}", file=sys.stderr)
        raise SystemExit(1) from None
    if result.get("ret_code") != 0:
        print(f"Futunn API error: {result.get('ret_code')} {result.get('ret_msg')}", file=sys.stderr)
        raise SystemExit(1)
    print(f"HTTP request succeeded for {symbol}")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    main()
