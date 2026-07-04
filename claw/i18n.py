"""User-facing message catalog. Core code never hardcodes UI language."""

from typing import Any

_MESSAGES: dict[str, dict[str, str]] = {
    "error.llm": {
        "en": "The AI model could not be reached ({reason}). Please try again.",
        "th": "ไม่สามารถติดต่อโมเดล AI ได้ ({reason}) กรุณาลองใหม่อีกครั้ง",
    },
    "error.tool": {
        "en": "A tool failed while working on your request ({reason}).",
        "th": "เครื่องมือทำงานไม่สำเร็จระหว่างประมวลผลคำขอ ({reason})",
    },
    "error.max_iterations": {
        "en": "I reached the step limit before finishing. Try splitting the task into smaller parts.",
        "th": "ถึงจำนวนขั้นตอนสูงสุดก่อนงานเสร็จ ลองแบ่งงานเป็นส่วนย่อยลง",
    },
    "error.rate_limited": {
        "en": "You're sending messages too fast. Please wait a moment and try again.",
        "th": "คุณส่งข้อความถี่เกินไป กรุณารอสักครู่แล้วลองใหม่อีกครั้ง",
    },
    "reason.timeout": {"en": "connection timed out", "th": "การเชื่อมต่อหมดเวลา"},
    "reason.auth": {"en": "authentication failed", "th": "การยืนยันตัวตนล้มเหลว"},
    "reason.rate_limit": {"en": "rate limit exceeded", "th": "เกินขีดจำกัดการเรียกใช้งาน"},
    "reason.network": {"en": "network unreachable", "th": "เชื่อมต่อเครือข่ายไม่ได้"},
    "reason.internal": {"en": "internal error", "th": "ข้อผิดพลาดภายใน"},
}

DEFAULT_LOCALE = "en"


def t(key: str, locale: str | None = None, **params: Any) -> str:
    entry = _MESSAGES.get(key)
    if not entry:
        return key
    text = entry.get(locale or DEFAULT_LOCALE) or entry[DEFAULT_LOCALE]
    try:
        return text.format(**params)
    except (KeyError, IndexError):
        return text


def classify_error_reason(detail: str) -> str:
    """Map a raw exception string to a translatable reason key."""
    lowered = detail.lower()
    if any(tok in lowered for tok in ("timeout", "timed out", "deadline")):
        return "reason.timeout"
    if any(tok in lowered for tok in ("401", "403", "unauthorized", "forbidden", "api key", "authentication")):
        return "reason.auth"
    if any(tok in lowered for tok in ("429", "rate limit", "quota", "too many requests")):
        return "reason.rate_limit"
    if any(tok in lowered for tok in ("connection", "network", "dns", "unreachable", "ssl")):
        return "reason.network"
    return "reason.internal"
