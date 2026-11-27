import { useState } from "react";
import { useT } from "./branding";

// Reasoning can run to tens of thousands of characters; only the tail is ever
// shown, so only the tail is kept.
const KEEP_CHARS = 4000;
const PEEK_CHARS = 140;
const SLOW_HINT_MS = 30_000;

type TraceState = {
  text: string;
  /** The server withholds reasoning while output guardrails are on and sends
   * empty deltas instead, so we know the model is thinking but not what. */
  hidden: boolean;
  /** Last time anything happened in the turn; the slow hint counts from here. */
  quietSince: number | null;
  /** Survives the trace unmounting while a tool runs. */
  open: boolean;
};

export type ReasoningTrace = {
  state: TraceState;
  reset(): void;
  onThinking(chunk: string): void;
};

function keepTail(text: string): string {
  if (text.length <= KEEP_CHARS) return text;
  const tail = text.slice(-KEEP_CHARS);
  const first = tail.charCodeAt(0);
  return first >= 0xdc00 && first <= 0xdfff ? tail.slice(1) : tail;
}

/** Mutable on purpose: reasoning streams per token, and the chat already
 * re-renders once a second while busy, which is when this is read. */
export function useReasoningTrace(): ReasoningTrace {
  const [trace] = useState<ReasoningTrace>(() => {
    const state: TraceState = { text: "", hidden: false, quietSince: null, open: false };
    return {
      state,
      reset() {
        state.text = "";
        state.hidden = false;
        state.quietSince = Date.now();
      },
      onThinking(chunk) {
        if (chunk) state.text = keepTail(state.text + chunk);
        else state.hidden = true;
        state.quietSince = Date.now();
      },
    };
  });
  return trace;
}

function lastLine(text: string): string {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const line = lines[lines.length - 1] ?? "";
  return line.length > PEEK_CHARS ? `…${line.slice(-PEEK_CHARS)}` : line;
}

/** Shown under the "Thinking… Ns" spinner while no tool is running: the
 * model's latest reasoning when it may be shown, otherwise a hint that a long
 * silence is expected so the turn doesn't read as stuck. */
export function ThinkingTrace({ trace, busySince }: { trace: ReasoningTrace; busySince: number | null }) {
  const t = useT();
  const { state } = trace;
  const peek = lastLine(state.text);
  if (peek) {
    return (
      <details
        className="claw-thinking-trace"
        open={state.open}
        onToggle={(e) => {
          state.open = e.currentTarget.open;
        }}
      >
        <summary>{peek}</summary>
        <div className="claw-thinking-trace-body">
          <div>{state.text}</div>
        </div>
      </details>
    );
  }
  if (state.hidden) return <div className="claw-thinking-hint">{t("chat.msg.thinkingHidden")}</div>;
  const since = state.quietSince ?? busySince;
  if (since != null && Date.now() - since >= SLOW_HINT_MS) {
    return <div className="claw-thinking-hint">{t("chat.msg.thinkingSlow")}</div>;
  }
  return null;
}
