"""Tisztítás, PII-maszkolás, injekció-jelzés, webhook-hitelesítés.

Az injekció-detektor csak heurisztika, a lezárást a gates.py kapui védik.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import unicodedata

# Tisztítás
# Vezérlőkarakterek (a \n és \t kivételével), valamint láthatatlan és irányváltó Unicode-jelek,
# amelyekkel szöveget lehet elrejteni az emberi olvasó elől.
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤⁦-⁩﻿]"
)


def sanitize_text(text: str | None, max_chars: int) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _CONTROL_RE.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + " [...]"
    return t


# PII-maszkolás
# Minta-alapú: nevek nem szűrhetők vele (README, Korlátok). A sorrend számít.

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")),
    ("PHONE", re.compile(r"(?<![\w+])(?:\+36|06)[ -/]?\(?\d{1,2}\)?[ -/]?\d{3}[ -]?\d{3,4}\b")),
    ("CARD", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("TAJ", re.compile(r"\b\d{3}[ -]?\d{3}[ -]?\d{3}\b")),
    ("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
]


def mask_pii(text: str) -> str:
    for label, pattern in _PII_PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    return text


# Prompt-injection jelzés
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in [
        ("ignore_instructions",
         r"\b(?:ignore|disregard|forget)\b.{0,30}\b(?:instructions?|rules|prompts?|above)\b"),
        ("ignore_instructions_hu",
         r"(?:hagyd|hagyja|felejtsd|felejtse)\b.{0,40}(?:utasítás|instrukci|szabály)"),
        ("system_prompt", r"system\s*prompt|rendszer\s*prompt|developer\s+message"),
        ("role_override", r"\byou\s+are\s+now\b|\bact\s+as\b|mostantól\s+(?:te\s+)?(?:egy|vagy)\b"),
        ("fake_tags", r"</?\s*(?:system|assistant|user|incident|kb_article)\b[^>]*>"),
        ("close_request",
         r"\b(?:close|resolve|mark)\b.{0,25}\b(?:ticket|incident)\b.{0,15}\b(?:closed|resolved)?"
         r"|\b(?:mark|set)\b.{0,20}\b(?:as\s+)?(?:resolved|closed)\b"),
        ("close_request_hu",
         r"(?:zárd|zárja|zárjátok)\s+le|(?:állítsd|állítsa)\b.{0,25}(?:megoldott|lezárt)"),
        ("output_steering", r"\bcan_auto_resolve\b|\bconfidence\s*[:=]|\brecord_triage\b"),
    ]
]


def detect_injection(text: str) -> list[str]:
    """Visszaadja a talált gyanús minták nevét (üres lista = nincs jelzés)."""
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]


def neutralize_markup(text: str) -> str:
    """A < és > karaktereket ártalmatlan párjukra cseréli, hogy a szöveg ne zárhassa le
    a promptban használt <incident> / <kb_article> határoló címkéket."""
    return text.replace("<", "‹").replace(">", "›")


# Webhook-hitelesítés
def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_hmac_signature(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    max_skew_s: int,
    now: float | None = None,
) -> bool:
    """HMAC-SHA256(secret, "<timestamp>.<body>") ellenőrzése, időablakkal a visszajátszás ellen.

    Az időablakon belüli visszajátszást az idempotencia-kezelés szűri ki (automation.py).
    """
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_s:
        return False
    expected = compute_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, signature.strip().lower())


def verify_shared_secret(secret: str, provided: str | None) -> bool:
    if not secret or provided is None:
        return False
    return hmac.compare_digest(secret.encode(), provided.encode())
