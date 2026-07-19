"""User-facing message catalog. Core code never hardcodes UI language."""

from typing import Any

_MESSAGES: dict[str, dict[str, str]] = {
    "error.llm": {
        "en": "The AI model could not be reached ({reason}). Please try again.",
        "th": "ไม่สามารถติดต่อโมเดล AI ได้ ({reason}) กรุณาลองใหม่อีกครั้ง",
    },
    # Distinct from error.llm: retrying is futile here (the model itself
    # can never handle a tool-calling request), so the message tells the
    # user to switch models instead of "please try again".
    "error.llm_no_tool_support": {
        "en": (
            "This model doesn't support tool/function calling, so it can't be used as a "
            "chat model here. Please pick a different model."
        ),
        "th": "โมเดลนี้ไม่รองรับการเรียกใช้เครื่องมือ (tool calling) จึงใช้เป็นโมเดลแชทในระบบนี้ไม่ได้ กรุณาเลือกโมเดลอื่น",
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
    "error.daily_limit": {
        "en": "You've reached your plan's daily message limit. It resets tomorrow.",
        "th": "คุณใช้ข้อความครบตามโควตารายวันของแพ็กเกจแล้ว ระบบจะรีเซ็ตในวันพรุ่งนี้",
    },
    "error.no_model_for_plan": {
        "en": "Your plan doesn't allow any of the currently available chat models. Ask an admin to adjust your plan or the model lineup.",
        "th": "แพ็กเกจของคุณไม่อนุญาตให้ใช้โมเดลแชทที่มีอยู่ในระบบตอนนี้เลย กรุณาติดต่อผู้ดูแลระบบเพื่อปรับแพ็กเกจหรือรายการโมเดล",
    },
    # No Control Plane provider is configured AND the operator's env fallback
    # (CLAW_LLM__API_KEY / CLAW_LLM__API_BASE) is empty, so there is no usable
    # model at all — an admin-facing setup message, distinct from a raw provider
    # auth error, so the operator knows exactly what to configure.
    "error.no_model_configured": {
        "en": "No chat model is configured yet. An administrator needs to set CLAW_LLM__API_KEY or CLAW_LLM__API_BASE in .env, or add an LLM provider in the Control Plane.",
        "th": "ยังไม่ได้ตั้งค่าโมเดลแชท ผู้ดูแลระบบต้องตั้งค่า CLAW_LLM__API_KEY หรือ CLAW_LLM__API_BASE ใน .env หรือเพิ่ม LLM provider ใน Control Plane ก่อน",
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


def is_no_tool_support_error(detail: str) -> bool:
    """True when the provider rejected the request specifically because the
    selected model doesn't support tool/function calling at all (e.g. a
    pure image-generation model picked as the chat model) — retrying gets
    the identical error every time, so this needs its own message telling
    the user to switch models rather than error.llm's generic "try again"."""
    lowered = detail.lower()
    return any(
        phrase in lowered
        for phrase in (
            "no endpoints found that support tool use",
            "does not support tool",
            "does not support function calling",
        )
    )


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
