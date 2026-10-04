"""Measure how well the guardrail model's purpose score separates real messages.

Calls the configured Jev and Laya providers with the same question Auto uses, over a
labelled set, and prints accuracy, a confusion count and latency. Needs the provider keys
in the environment (CLAW_SEMANTIC_GUARDRAILS__*). Sends only the sample messages below.

    uv run python scripts/eval_auto_routing.py [--json out.json]
"""

import json
import statistics
import sys
import time

import httpx

from claw.config import load_settings
from claw.core.model_router import PURPOSE_CRITERIA, PURPOSES

SAMPLES = {
    "general": [
        "สวัสดีครับ วันนี้อากาศดีไหม", "แปลประโยคนี้เป็นภาษาอังกฤษ: ขอบคุณสำหรับความช่วยเหลือ",
        "ช่วยเขียนคำอวยพรวันเกิดให้เพื่อนหน่อย", "What is the capital of Australia?",
        "ช่วยแนะนำหนังสือน่าอ่านสักเล่ม", "Rewrite this sentence to sound more polite: give me the report now",
        "ทำไมท้องฟ้าถึงเป็นสีฟ้า", "Can you suggest a name for my new cat?",
    ],
    "fast": [
        "สรุปอีเมลนี้ให้สั้นๆ 3 บรรทัด", "จัดหมวดรีวิวนี้เป็นบวก กลาง หรือลบ: อาหารอร่อยแต่บริการช้า",
        "ดึงชื่อและเบอร์โทรจากข้อความนี้ออกมาเป็นรายการ", "Summarize this paragraph in one sentence.",
        "Extract all dates from the following text.", "จัดหมวดตั๋วนี้ว่าเป็นเรื่องบิล เทคนิค หรือทั่วไป",
        "Classify this message as spam or not spam.", "ย่อประชุมวันนี้เป็นสามข้อ",
    ],
    "reasoning": [
        "วิเคราะห์ข้อดีข้อเสียของการย้ายระบบทั้งหมดไปคลาวด์ แล้ววางแผนการย้าย 3 ปี",
        "Compare three strategies for entering the Thai market and recommend one with a risk analysis.",
        "พิสูจน์ว่าผลรวมของเลขคี่ n ตัวแรกเท่ากับ n กำลังสอง",
        "Plan a multi-step migration from a monolith to microservices, including rollback criteria.",
        "ช่วยคิดเชิงกลยุทธ์ว่าเราควรขึ้นราคาสินค้าหรือไม่ โดยพิจารณาคู่แข่ง ต้นทุน และลูกค้า",
        "If a train leaves at 3pm at 80 km/h and another at 4pm at 100 km/h, when does the second catch up? Show the reasoning.",
        "ประเมินความเสี่ยงของโครงการนี้และจัดลำดับความสำคัญของการแก้ไข",
        "Design an experiment to test whether the new onboarding flow improves retention.",
    ],
    "coding": [
        "ช่วยเขียนฟังก์ชัน Python เรียงลำดับรายการ", "Fix this bug: TypeError: 'NoneType' object is not subscriptable in my Flask handler",
        "เขียน SQL ดึงลูกค้าที่ซื้อมากกว่า 3 ครั้งในเดือนนี้", "Write a bash script that renames all .txt files to .md",
        "ช่วยรีแฟกเตอร์โค้ด React นี้ให้ใช้ hooks", "Run the tests and tell me which ones fail",
        "เขียน regex จับเบอร์โทรไทย", "Implement a debounce function in TypeScript.",
    ],
    "long_context": [
        "จากสัญญาฉบับเต็ม 80 หน้านี้ ข้อไหนพูดถึงการยกเลิกสัญญา", "Based on the attached 200-page annual report, what were the main risks listed?",
        "ค้นในฐานความรู้ของบริษัทว่านโยบายลาพักร้อนเป็นอย่างไร", "Search our knowledge base for the refund policy and quote the relevant section.",
        "อ่านรายงานการประชุมทั้งปีแล้วสรุปประเด็นที่ซ้ำกัน", "Across these 40 support transcripts, which complaint appears most often?",
        "เทียบเนื้อหาในเอกสารสามฉบับนี้ว่าขัดแย้งกันตรงไหน", "In the whole codebase documentation, where is rate limiting described?",
    ],
    "multimodal": [
        "ช่วยอ่านข้อความในรูปนี้ให้หน่อย", "What does this screenshot show?", "รูปใบเสร็จนี้ยอดรวมเท่าไหร่",
        "Describe the chart in the attached image.", "ดูภาพหน้าจอนี้แล้วบอกว่าปุ่มไหนผิดปกติ",
        "Extract the table from this scanned page.", "ในภาพถ่ายนี้มีอะไรบ้าง", "Is the person in this photo wearing a helmet?",
    ],
}


def ask(conn, text):
    body = {
        "model": conn.model,
        "state": {"scope": "input", "text": text},
        "questions": {"purpose": {"type": "score", "instructions": "Which description best fits the task that `text` asks for?", "criteria": list(PURPOSE_CRITERIA)}},
    }
    started = time.monotonic()
    response = httpx.post(conn.endpoint, json=body, headers={"Authorization": "Bearer " + conn.api_key.get_secret_value()}, timeout=30)
    elapsed = (time.monotonic() - started) * 1000
    response.raise_for_status()
    probabilities = response.json()["answers"]["purpose"]["probabilities"]
    values = [probabilities[str(i)] for i in range(len(PURPOSES))]
    return max(range(len(values)), key=values.__getitem__), max(values), elapsed


def main():
    settings = load_settings().semantic_guardrails
    report = {}
    for name in ("jev", "laya"):
        conn = getattr(settings, name)
        if not conn.api_key.get_secret_value().strip():
            print(f"{name}: no key, skipped")
            continue
        rows, errors = [], 0
        for label, texts in SAMPLES.items():
            for text in texts:
                try:
                    guess, confidence, ms = ask(conn, text)
                except Exception as exc:  # report and keep going; one failure should not hide the rest
                    errors += 1
                    print(f"  error {type(exc).__name__}")
                    continue
                rows.append({"label": label, "guess": PURPOSES[guess], "confidence": confidence, "ms": ms, "text": text})
        ok = sum(r["label"] == r["guess"] for r in rows)
        confident = [r for r in rows if r["confidence"] >= 0.5]
        times = sorted(r["ms"] for r in rows)
        print(f"\n{name} ({conn.model}): {ok}/{len(rows)} correct, errors={errors}")
        print(f"  confident (>=0.5): {len(confident)}/{len(rows)}, correct among them: {sum(r['label'] == r['guess'] for r in confident)}")
        if times:
            print(f"  latency ms p50={statistics.median(times):.0f} p95={times[int(len(times) * 0.95) - 1]:.0f} max={times[-1]:.0f}")
        for label in SAMPLES:
            part = [r for r in rows if r["label"] == label]
            print(f"  {label:13s} {sum(r['guess'] == label for r in part)}/{len(part)}")
        for r in rows:
            if r["label"] != r["guess"]:
                print(f"    miss {r['label']}->{r['guess']} ({r['confidence']:.2f}) {r['text'][:60]}")
        report[name] = rows
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
