import hashlib
import hmac

SIGNATURE_HEADER = "X-KB-Signature"
SIGNATURE_PREFIX = "sha256="


def sign_payload(secret: str, payload: bytes) -> str:
    """HMAC-SHA256 of the payload, prefixed with `sha256=`.

 Empty `secret` returns an empty string — callers should treat that as
 "unsigned" and decide whether to send the header at all.
 """
    if not secret:
        return ""
    digest = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, payload: bytes, signature: str) -> bool:
    """Constant-time signature verification — for use by webhook receivers."""
    expected = sign_payload(secret, payload)
    if not expected or not signature:
        return False
    return hmac.compare_digest(expected, signature)
