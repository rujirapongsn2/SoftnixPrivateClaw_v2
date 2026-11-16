/**
 * Renders one agent-authored A2UI (v0.9) surface inside the chat, using the
 * official @a2ui/react renderer with the Softnix catalog.
 *
 * Failure is always contained: a payload that fails our structural gate, the
 * renderer's schema validation, reports an error on the surface, or throws
 * while rendering falls back to the raw text shown as code (never a blank
 * bubble, never a crashed transcript). Since @a2ui/react 0.15 an unresolved or
 * cyclic component renders as nothing and is reported only through the
 * surface's onError, so that event must reach the fallback too.
 */
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Text } from "@astryxdesign/core/Text";
import { A2uiSurface } from "@a2ui/react/v0_9";
import { MessageProcessor, type ActionPayload } from "@a2ui/web_core/v0_9";
import { Component, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { A2uiInline, A2uiInteractive, A2uiLang, softnixCatalog } from "./catalog";
import { CARD_UI, actionToMessage, cardLang, parseA2ui } from "./protocol";

type Props = {
  /** Raw JSON text (fenced block / artifact file) or an already-parsed value. */
  source: string;
  /** Only the latest assistant message's card accepts input. */
  interactive: boolean;
  /** Sends an ordinary chat message on the user's behalf. */
  onAction?: (text: string) => void;
  /** Shown as the code block language on fallback. */
  language?: string;
};

class Boundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(err: unknown) {
    console.warn("[a2ui] render failed", err);
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

let surfaceSeq = 0;

/** Surface errors after which the renderer draws nothing for part of the card,
 * so the card would be partly or wholly blank. Other surface errors (a binding
 * that fails to resolve) leave a working card usable and are only logged. A
 * child that never arrived is not reported at all (the renderer treats it as
 * still streaming); parseA2ui's gate rejects that case before rendering. */
const STRUCTURAL_ERRORS = new Set(["CYCLIC_REFERENCE", "UNKNOWN_COMPONENT_TYPE", "UNRESOLVED_CHILD_REFERENCE"]);

export function A2uiCard({ source, interactive, onAction, language = "a2ui" }: Props) {
  const [sent, setSent] = useState(false);
  // Keyed by the source it came from, so a new source never shows the old error.
  const [renderError, setRenderError] = useState<{ source: string; msg: string } | null>(null);
  const disposing = useRef<{ processor: MessageProcessor; timer: ReturnType<typeof setTimeout> } | null>(null);
  const onActionRef = useRef(onAction);
  onActionRef.current = onAction;
  const live = interactive && !sent && !!onAction;
  const liveRef = useRef(live);
  liveRef.current = live;

  const built = useMemo(() => {
    const parsed = parseA2ui(source, `s${++surfaceSeq}`);
    if (!parsed.ok) return { error: parsed.error } as const;
    const lang = cardLang(parsed.messages);
    const handler = (a: ActionPayload) => {
      if (!liveRef.current) return;
      setSent(true);
      onActionRef.current?.(actionToMessage(a, lang));
    };
    try {
      const processor = new MessageProcessor([softnixCatalog], handler);
      processor.processMessages(parsed.messages as never);
      const surface = processor.model.surfacesMap.get(parsed.surfaceId);
      if (!surface) return { error: "surface was not created" } as const;
      // Subscribe now, before the first render: the renderer dispatches the error from
      // a child's effect, which runs before this component's effects, and only once.
      surface.onError.subscribe((e) => {
        console.warn("[a2ui] surface error", e);
        if (STRUCTURAL_ERRORS.has(e.code)) setRenderError({ source, msg: `${e.code}: ${e.message}` });
      });
      return { processor, surface, lang } as const;
    } catch (e) {
      return { error: `A2UI validation failed: ${(e as Error).message}` } as const;
    }
  }, [source]);

  // Dispose on the next tick, not in the cleanup itself: StrictMode runs cleanup and
  // then the effect again for the same memoized processor, which must stay alive.
  useEffect(() => {
    if (!("processor" in built) || !built.processor) return;
    const processor = built.processor;
    if (disposing.current?.processor === processor) {
      clearTimeout(disposing.current.timer);
      disposing.current = null;
    }
    return () => {
      disposing.current = { processor, timer: setTimeout(() => processor.dispose(), 0) };
    };
  }, [built]);

  const structuralError = renderError?.source === source ? renderError.msg : "";

  const fallback = (
    <div className="a2ui-fallback">
      <CodeBlock code={source} language={language} isCollapsible />
    </div>
  );
  if (!("surface" in built) || !built.surface || structuralError) {
    const why = structuralError || ("error" in built ? String(built.error ?? "") : "");
    return (
      <>
        {fallback}
        <Text size="sm" color="secondary">แสดงการ์ดไม่ได้ ({why.slice(0, 160)})</Text>
      </>
    );
  }
  return (
    <Boundary fallback={fallback}>
      <A2uiInteractive.Provider value={live}>
        <A2uiLang.Provider value={built.lang}>
          <A2uiInline.Provider value={language !== "json"}>
          <div className="a2ui-surface" data-sent={sent || undefined} lang={built.lang}>
            <A2uiSurface surface={built.surface as never} />
            <div className="a2ui-badge-ai" aria-hidden="true">{CARD_UI[built.lang].badge}</div>
            {sent ? <Text size="sm" color="secondary">{CARD_UI[built.lang].sent}</Text> : null}
          </div>
          </A2uiInline.Provider>
        </A2uiLang.Provider>
      </A2uiInteractive.Provider>
    </Boundary>
  );
}

/** Markdown `components.code` override: ```a2ui blocks become cards. */
export function makeA2uiCodeRenderer(opts: { interactive: boolean; streaming?: boolean; onAction?: (t: string) => void }) {
  return function A2uiAwareCode({ code, language }: { code: string; language?: string }) {
    const lang = (language ?? "").trim().toLowerCase();
    if (lang === "a2ui" || lang === "a2ui-json") {
      if (opts.streaming) return <Text size="sm" color="secondary">กำลังสร้างการ์ด…</Text>;
      return <A2uiCard source={code} interactive={opts.interactive} onAction={opts.onAction} language={lang} />;
    }
    return (
      <div className="a2ui-codeblock-default">
        <CodeBlock code={code} language={language} isCollapsible />
      </div>
    );
  };
}
