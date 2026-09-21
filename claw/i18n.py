"""User-facing message catalog. Core code never hardcodes UI language."""

from typing import Any


def locale_for_text(configured: str, text: str) -> str:
    """Prefer the language the user is writing for runtime-owned messages."""
    if any("\u0e00" <= char <= "\u0e7f" for char in (text or "")):
        return "th"
    return configured if configured in {"en", "th"} else "en"

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
    # Distinct from error.llm for the same reason as error.llm_no_tool_support:
    # retrying with an image attached to a text-only model fails identically
    # every time, so this tells the user to remove the attachment or switch
    # models instead of "please try again".
    "error.llm_no_vision_support": {
        "en": (
            "This model doesn't support image input, so it can't read the attached image. "
            "Please remove the attachment or pick a vision-capable model."
        ),
        "th": "โมเดลนี้ไม่รองรับการรับรูปภาพ จึงไม่สามารถอ่านรูปที่แนบมาได้ กรุณานำไฟล์แนบออก หรือเลือกโมเดลที่รองรับรูปภาพแทน",
    },
    "error.tool": {
        "en": "A tool failed while working on your request ({reason}).",
        "th": "เครื่องมือทำงานไม่สำเร็จระหว่างประมวลผลคำขอ ({reason})",
    },
    # Distinct from error.llm because the model is not at fault: the answer was
    # produced and then lost on the way to storage. Saying "could not reach the
    # model" here sends the user (and whoever reads the bug report) after the
    # provider instead of the database.
    "error.save": {
        "en": "The answer was generated but could not be saved (internal error). Please try again.",
        "th": "สร้างคำตอบสำเร็จแล้ว แต่บันทึกไม่สำเร็จ (ข้อผิดพลาดภายใน) กรุณาลองใหม่อีกครั้ง",
    },
    # Distinct from error.save: this is the user's own message failing to
    # persist *before* the model was ever called — no answer exists yet, so
    # error.save's "the answer was generated" would be false here.
    "error.save_request": {
        "en": "Your message could not be saved (internal error). Please try again.",
        "th": "บันทึกข้อความของคุณไม่สำเร็จ (ข้อผิดพลาดภายใน) กรุณาลองใหม่อีกครั้ง",
    },
    # The model hit its output cap before writing a visible answer — typical of
    # a reasoning model that spends the whole budget on hidden thinking. Saying
    # "could not reach the model" would be wrong: it answered, and the answer
    # was cut off. Retrying is worth it (the next attempt may think less).
    "error.truncated": {
        "en": (
            "The answer hit the response-length limit before it could be written out. "
            "Please try again, or ask for a shorter answer."
        ),
        "th": "คำตอบชนขีดจำกัดความยาวก่อนจะเขียนออกมาได้ กรุณาลองใหม่อีกครั้ง หรือขอคำตอบที่สั้นลง",
    },
    # Model finished normally but produced no text at all. Rare, and there is
    # nothing actionable to say beyond "try again" — but it must still be said,
    # because an empty turn otherwise looks like the app silently did nothing.
    "error.empty_response": {
        "en": "The model returned an empty response. Please try again.",
        "th": "โมเดลตอบกลับมาว่างเปล่า กรุณาลองใหม่อีกครั้ง",
    },
    # The turn ran past its wall-clock budget. Distinct from error.max_iterations:
    # the step count may be nowhere near its limit, so telling the user to use
    # fewer steps would be misleading — what ran out was time.
    "error.turn_timeout": {
        "en": "This is taking too long, so I stopped before finishing. Try a smaller task, or ask me to continue.",
        "th": "งานนี้ใช้เวลานานเกินกำหนด จึงหยุดก่อนทำเสร็จ ลองแบ่งงานให้เล็กลง หรือสั่งให้ทำต่อได้",
    },
    # The deadline cut the stream off while the model was mid-answer. Distinct
    # from error.turn_timeout: there IS an answer, it is just unfinished, so the
    # message has to mark where it stops rather than replace it.
    "error.turn_timeout_partial": {
        "en": "[This answer was cut off — the turn ran out of time. Ask me to continue.]",
        "th": "[คำตอบนี้ถูกตัดกลางคัน เพราะหมดเวลาที่กำหนดไว้ สั่งให้ทำต่อได้]",
    },
    "artifact.sandboxBlocked": {
        "en": "File creation is paused because the execution environment is unavailable. Progress is saved. An administrator must restore the sandbox before this job can continue.",
        "th": "พักงานสร้างไฟล์ไว้ เนื่องจากระบบรันคำสั่งไม่พร้อมใช้งาน บันทึกความคืบหน้าแล้ว ผู้ดูแลต้องกู้คืน sandbox ก่อนจึงจะทำงานต่อได้",
    },
    "artifact.started": {
        "en": "Preparing a resumable artifact job.",
        "th": "กำลังเตรียมงานสร้างไฟล์แบบทำต่ออัตโนมัติ",
    },
    "artifact.resuming": {
        "en": "Continuing automatically from checkpoint (segment {segment}).",
        "th": "กำลังทำต่ออัตโนมัติจากจุดบันทึก (ช่วงที่ {segment})",
    },
    "artifact.completed": {
        "en": "Artifact job completed.",
        "th": "งานสร้างไฟล์เสร็จแล้ว",
    },
    "artifact.cancelled": {
        "en": "Artifact job cancelled.",
        "th": "ยกเลิกงานสร้างไฟล์แล้ว",
    },
    "artifact.limitReached": {
        "en": "The artifact job stopped at its cumulative safety limit. Completed checkpoints were preserved.",
        "th": "งานสร้างไฟล์หยุดเมื่อถึงขีดจำกัดสะสม โดยเก็บขั้นตอนที่ทำเสร็จแล้วไว้ครบถ้วน",
    },
    "artifact.limitReachedPartial": {
        "en": "[The artifact job reached its cumulative safety limit; completed checkpoints were preserved.]",
        "th": "[งานสร้างไฟล์ถึงขีดจำกัดสะสมแล้ว โดยเก็บขั้นตอนที่ทำเสร็จไว้ครบถ้วน]",
    },
    "error.provider_stream_partial": {
        "en": "[This response was cut off because the upstream model provider became unavailable. Ask me to continue.]",
        "th": "[คำตอบนี้ถูกตัดกลางคัน เพราะผู้ให้บริการโมเดลต้นทางไม่พร้อมใช้งานชั่วคราว สั่งให้ทำต่อได้]",
    },
    "error.provider_unavailable": {
        "en": "The upstream model provider is temporarily unavailable.",
        "th": "ผู้ให้บริการโมเดลต้นทางไม่พร้อมใช้งานชั่วคราว",
    },
    "error.max_iterations": {
        "en": "I reached the step limit before finishing. Try splitting the task into smaller parts.",
        "th": "ถึงจำนวนขั้นตอนสูงสุดก่อนงานเสร็จ ลองแบ่งงานเป็นส่วนย่อยลง",
    },
    # Appended to any of the "no answer" messages above when the turn did in fact
    # produce files before it ran out of time/steps. Without this the user is told
    # the turn failed and never learns that the work is sitting in their workspace.
    "error.partial_artifacts": {
        "en": "Files created before I stopped: {files}",
        "th": "ไฟล์ที่สร้างไว้ก่อนหยุด: {files}",
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
    "reason.provider_unavailable": {
        "en": "upstream model provider temporarily unavailable",
        "th": "ผู้ให้บริการโมเดลต้นทางไม่พร้อมใช้งานชั่วคราว",
    },
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


def is_no_vision_support_error(detail: str) -> bool:
    """True when the provider rejected the request specifically because the
    selected model doesn't support image/vision input — retrying gets the
    identical rejection every time (the model can never read the image), so
    this needs its own message telling the user to drop the attachment or
    switch models, same as is_no_tool_support_error above. This is the
    reactive safety net for any text-only model the registry's proactive
    supports_vision() check doesn't yet know about."""
    lowered = detail.lower()
    return any(
        phrase in lowered
        for phrase in (
            "does not support image",
            "does not support vision",
            "no endpoints found that support image",
            "multimodal messages are not enabled",
            "image content is not supported",
            "unsupported image",
            "invalid content type: image",
            "model does not support the image_url",
            # Observed verbatim from an OpenAI-compatible gateway fronting a
            # text-only model: the server's message schema has no image_url
            # variant at all, so it fails at JSON deserialization rather than
            # with a capability message.
            "unknown variant `image_url`",
        )
    )


def classify_error_reason(detail: str) -> str:
    """Map a raw exception string to a translatable reason key."""
    lowered = detail.lower()
    if any(
        tok in lowered
        for tok in (
            "provider_unavailable",
            "upstream error from",
            "h2 protocol error",
            "error reading a body from connection",
        )
    ):
        return "reason.provider_unavailable"
    if any(tok in lowered for tok in ("timeout", "timed out", "deadline")):
        return "reason.timeout"
    if any(
        tok in lowered for tok in ("401", "403", "unauthorized", "forbidden", "api key", "authentication")
    ):
        return "reason.auth"
    if any(tok in lowered for tok in ("429", "rate limit", "quota", "too many requests")):
        return "reason.rate_limit"
    if any(tok in lowered for tok in ("connection", "network", "dns", "unreachable", "ssl")):
        return "reason.network"
    return "reason.internal"
