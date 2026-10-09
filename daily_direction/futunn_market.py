"""Read-only Futunn snapshot validation; credentials stay outside the repository."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Iterable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def configured() -> bool:
    return bool(os.getenv("FUTUNN_APP_KEY") and os.getenv("FUTUNN_PRIVATE_KEY_FILE"))


def fetch_us_snapshots(tickers: Iterable[str]) -> dict[str, dict]:
    if not configured():
        return {}
    private_key = serialization.load_pem_private_key(Path(os.environ["FUTUNN_PRIVATE_KEY_FILE"]).read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RuntimeError("Futunn requires an Ed25519 key")
    symbols = list(dict.fromkeys(tickers))
    result = {}
    path = "/api/v1.0/quote/snapshot"
    for start in range(0, len(symbols), 20):
        body = json.dumps({"code_list": [f"US.{ticker}" for ticker in symbols[start:start + 20]]}, separators=(",", ":")).encode()
        timestamp = str(int(time.time() * 1000))
        signing_text = "\n".join([timestamp, "POST", path, "", hashlib.sha256(body).hexdigest()]).encode()
        request = urllib.request.Request(
            f"https://webapi.futunn.com{path}", data=body,
            headers={
                "Content-Type": "application/json", "X-Api-Key": os.environ["FUTUNN_APP_KEY"],
                "X-Timestamp": timestamp, "X-Nonce": secrets.token_urlsafe(24),
                "Authorization": base64.b64encode(private_key.sign(signing_text)).decode(),
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        if data.get("ret_code") != 0:
            raise RuntimeError(f"Futunn quote error {data.get('ret_code')}")
        for row in (data.get("data") or {}).get("snapshot_list", []):
            result[str(row.get("code", "")).removeprefix("US.")] = row
    return result
