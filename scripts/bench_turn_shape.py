"""Measure how many tool calls / LLM round-trips a turn actually costs.

Runs a fixed prompt set through the real AgentRuntime against the real LLM and
prints each turn's shape (iterations, tool calls, time-to-first-token, wall
time, tokens). Prompts are split into two groups that must move in opposite
directions when we tune tool-usage policy:

  knowledge — answerable from the model's own knowledge; tool calls here are
              pure latency and the number we want to drive toward zero.
  agentic   — genuinely needs tools; a drop here is a regression, not a win.

Storage is a throwaway SQLite file and a throwaway workspace, so a run never
touches the real database. Write a run to JSON with --out and compare before
and after a change:

    uv run python scripts/bench_turn_shape.py --out /tmp/before.json
    uv run python scripts/bench_turn_shape.py --out /tmp/after.json --compare /tmp/before.json
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from claw.config import Settings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime
from claw.db.engine import create_engine_and_factory, init_db
from claw.db.stores import (
    AuditStore,
    MemoryStore,
    MessageStore,
    SessionStore,
    UsageStore,
    UserStore,
)
from claw.providers.litellm_provider import LiteLLMProvider

PROMPTS: tuple[tuple[str, str], ...] = (
    ("knowledge", "ช่วยเขียน PRD สำหรับระบบจองห้องประชุมภายในองค์กร"),
    ("knowledge", "ช่วยเขียนแผนการตลาดสำหรับการเปิดตัวแอปสั่งอาหารใหม่"),
    ("knowledge", "อธิบายความแตกต่างระหว่าง OAuth 2.0 กับ OIDC"),
    ("knowledge", "ร่างอีเมลแจ้งลูกค้าเรื่องการปรับราคาแพ็กเกจขึ้น 10%"),
    ("agentic", "สร้างไฟล์ report.md ในเวิร์กสเปซ เขียนสรุปสั้นๆ ว่าไฟล์นี้ใช้ทดสอบอะไร"),
    ("agentic", "มีไฟล์อะไรอยู่ในเวิร์กสเปซของฉันบ้าง"),
)


async def _run(limit: int | None) -> list[dict]:
    settings = Settings()
    tmp = Path(tempfile.mkdtemp(prefix="claw-bench-"))
    settings.workspaces_root = tmp / "workspaces"
    settings.workspaces_root.mkdir(parents=True, exist_ok=True)

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp}/bench.db")
    await init_db(engine)
    provider = LiteLLMProvider(
        api_key=settings.llm.api_key,
        api_base=settings.llm.api_base,
        default_model=settings.llm.model,
    )
    users = UserStore(factory)
    sessions = SessionStore(factory, is_postgres=False)
    messages = MessageStore(factory, is_postgres=False)
    memories = MemoryStore(factory)
    audit = AuditStore(factory, is_postgres=False)
    usage = UsageStore(factory, is_postgres=False)
    runtime = AgentRuntime(
        settings=settings,
        provider=provider,
        bus=EventBus(),
        users=users,
        sessions=sessions,
        messages=messages,
        memory=MemoryService(
            memories, messages, sessions, provider, model=settings.llm.model, is_postgres=False
        ),
        audit=audit,
        usage=usage,
    )
    user = await users.get_or_create_by_email("bench@local", signup_method="dev_token")

    rows: list[dict] = []
    for group, prompt in PROMPTS[: limit or len(PROMPTS)]:
        # Fresh session per prompt: a shared one would let earlier turns' tool
        # results prime the model and understate the tool count we're measuring.
        session = await sessions.create(user.id, title=prompt[:40])
        outcome_holder: dict = {}
        original = runtime.usage.record

        async def _capture(*args, metrics=None, **kwargs):
            outcome_holder.update(metrics or {})
            return await original(*args, metrics=metrics, **kwargs)

        runtime.usage.record = _capture  # type: ignore[method-assign]
        try:
            await runtime.handle_message(user.id, session.id, prompt, locale="th")
            await runtime.drain()
        finally:
            runtime.usage.record = original  # type: ignore[method-assign]

        row = {"group": group, "prompt": prompt, **outcome_holder}
        rows.append(row)
        print(
            f"[{group:9}] iters={row.get('iterations', 0):2} "
            f"tools={row.get('tool_calls', 0):2} "
            f"ttft={row.get('ttft_ms', 0):6}ms "
            f"total={row.get('duration_ms', 0):6}ms  {prompt[:48]}"
        )

    await engine.dispose()
    return rows


def _summarize(rows: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for group in ("knowledge", "agentic"):
        group_rows = [r for r in rows if r["group"] == group]
        if not group_rows:
            continue
        n = len(group_rows)
        out[group] = {
            key: round(sum(r.get(key, 0) for r in group_rows) / n, 1)
            for key in ("iterations", "tool_calls", "ttft_ms", "duration_ms")
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write this run's rows to a JSON file")
    parser.add_argument("--compare", type=Path, help="a previous --out file to diff against")
    parser.add_argument("--limit", type=int, help="only run the first N prompts")
    args = parser.parse_args()

    rows = asyncio.run(_run(args.limit))
    summary = _summarize(rows)

    print("\naverages per group")
    for group, stats in summary.items():
        print(f"  {group:9} " + "  ".join(f"{k}={v}" for k, v in stats.items()))

    if args.compare and args.compare.exists():
        before = _summarize(json.loads(args.compare.read_text()))
        print(f"\ndelta vs {args.compare}")
        for group, stats in summary.items():
            prior = before.get(group)
            if not prior:
                continue
            diffs = "  ".join(f"{k}={v - prior.get(k, 0):+.1f}" for k, v in stats.items())
            print(f"  {group:9} {diffs}")

    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
