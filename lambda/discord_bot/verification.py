import base64
import binascii
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .responses import RequestError


def raw_body(event: dict) -> bytes:
    body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if not isinstance(body, str) or type(encoded) is not bool:
        raise RequestError(400, "invalid_body")
    try:
        return base64.b64decode(body, validate=True) if encoded else body.encode("utf-8")
    except (UnicodeError, ValueError, binascii.Error):
        raise RequestError(400, "invalid_body") from None


def verify_request(event: dict, body: bytes, key: Ed25519PublicKey, now: float | None = None) -> None:
    supplied = event.get("headers")
    if not isinstance(supplied, dict):
        raise RequestError(401, "invalid_signature")
    headers = {}
    for name, value in supplied.items():
        if not isinstance(name, str):
            raise RequestError(401, "invalid_signature")
        name = name.lower()
        if name in ("x-signature-ed25519", "x-signature-timestamp"):
            if name in headers or not isinstance(value, str):
                raise RequestError(401, "invalid_signature")
            headers[name] = value
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    if not re.fullmatch(r"[0-9a-fA-F]{128}", signature) or not re.fullmatch(r"[0-9]{1,12}", timestamp):
        raise RequestError(401, "invalid_signature")
    try:
        key.verify(bytes.fromhex(signature), timestamp.encode("ascii") + body)
    except (InvalidSignature, ValueError):
        raise RequestError(401, "invalid_signature") from None
    if abs(int(time.time() if now is None else now) - int(timestamp)) > 300:
        raise RequestError(401, "expired_signature")
