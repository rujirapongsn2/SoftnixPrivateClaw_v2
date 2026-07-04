import {
  ChatComposer,
  ChatLayout,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageList,
  ChatMessageMetadata,
  ChatToolCalls,
} from "@astryxdesign/core/Chat";
import { Markdown } from "@astryxdesign/core/Markdown";
import { Button } from "@astryxdesign/core/Button";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Icon } from "@astryxdesign/core/Icon";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { File as FileIcon, Image as ImageIcon, Paperclip, ThumbsDown, ThumbsUp } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorText } from "./ErrorText";
import { AgentEvent, AttachmentRef, api, openChatSocket } from "./api";
import { SoftnixLogo } from "./Logo";

interface ToolCallRow {
  name: string;
  status: "running" | "complete" | "error";
  argsPreview?: string;
  resultPreview?: string;
  startedAt: number;
  duration?: string;
}

type TranscriptItem =
  | { kind: "message"; role: "user" | "assistant"; content: string }
  | { kind: "tools"; calls: ToolCallRow[] };

interface ChatProps {
  sessionId: string;
  userName?: string;
  onFirstMessage?: (text: string) => void;
}

export function Chat({ sessionId, userName, onFirstMessage }: ChatProps) {
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const [feedback, setFeedback] = useState<Record<number, "up" | "down">>({});
  const socketRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const sentCountRef = useRef(0);

  useEffect(() => {
    setItems([]);
    setStreaming("");
    setError("");
    setBusy(false);
    setAttachments([]);
    setFeedback({});
    sentCountRef.current = 0;
    api
      .listMessages(sessionId)
      .then((msgs) => {
        sentCountRef.current = msgs.length;
        setItems(msgs.map((m) => ({ kind: "message", role: m.role, content: m.content })));
      })
      .catch((e) => setError(String(e)));

    const socket = openChatSocket(sessionId);
    socketRef.current = socket;
    socket.onmessage = (raw) => {
      const event: AgentEvent = JSON.parse(raw.data);
      switch (event.type) {
        case "turn_started":
          setBusy(true);
          setStreaming("");
          break;
        case "text_delta":
          setStreaming((prev) => prev + (event.text ?? ""));
          break;
        case "tool_started":
          setStreaming("");
          setItems((prev) => {
            const last = prev[prev.length - 1];
            const row: ToolCallRow = {
              name: event.tool ?? "tool",
              status: "running",
              argsPreview: event.args_preview,
              startedAt: Date.now(),
            };
            if (last?.kind === "tools") {
              return [...prev.slice(0, -1), { kind: "tools", calls: [...last.calls, row] }];
            }
            return [...prev, { kind: "tools", calls: [row] }];
          });
          break;
        case "tool_finished":
          setItems((prev) => {
            const last = prev[prev.length - 1];
            if (last?.kind !== "tools") return prev;
            const calls = last.calls.map((c, i) =>
              i === last.calls.length - 1
                ? {
                    ...c,
                    status: (event.is_error ? "error" : "complete") as ToolCallRow["status"],
                    resultPreview: event.result_preview,
                    duration: `${((Date.now() - c.startedAt) / 1000).toFixed(1)}s`,
                  }
                : c,
            );
            return [...prev.slice(0, -1), { kind: "tools", calls }];
          });
          break;
        case "turn_completed":
          setBusy(false);
          setStreaming("");
          if (event.content) {
            setItems((prev) => [
              ...prev,
              { kind: "message", role: "assistant", content: event.content! },
            ]);
          }
          break;
        case "turn_error":
          setBusy(false);
          setStreaming("");
          setError(event.message ?? "unknown error");
          break;
      }
    };
    socket.onclose = (ev) => {
      if (ev.code === 4401) setError("Authentication failed — check your token and email.");
    };
    return () => socket.close();
  }, [sessionId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [items, streaming]);

  const send = useCallback(
    (value: string) => {
      const content = value.trim();
      const atts = attachments;
      if ((!content && atts.length === 0) || socketRef.current?.readyState !== WebSocket.OPEN) return;
      if (sentCountRef.current === 0 && content) onFirstMessage?.(content);
      sentCountRef.current += 1;
      socketRef.current.send(JSON.stringify({ content, attachments: atts.map((a) => a.path) }));
      const shown = atts.length ? `${content}${content ? "\n" : ""}📎 ${atts.map((a) => a.name).join(", ")}` : content;
      setItems((prev) => [...prev, { kind: "message", role: "user", content: shown }]);
      setAttachments([]);
      setError("");
    },
    [onFirstMessage, attachments],
  );

  const onPickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      setUploading(true);
      setError("");
      try {
        const refs = await api.uploadAttachments(sessionId, Array.from(files));
        setAttachments((prev) => [...prev, ...refs]);
      } catch (e) {
        setError(`Upload failed: ${String(e)}`);
      } finally {
        setUploading(false);
        if (fileRef.current) fileRef.current.value = "";
      }
    },
    [sessionId],
  );

  const rate = useCallback(
    (index: number, content: string, signal: "up" | "down") => {
      setFeedback((prev) => ({ ...prev, [index]: signal }));
      void api
        .submitFeedback(signal, { session_id: sessionId, message_preview: content.slice(0, 400) })
        .catch(() => undefined);
    },
    [sessionId],
  );

  const greeting = (
    <div className="claw-greeting">
      <SoftnixLogo height={40} />
      <Text type="display-2">
        {new Date().getHours() < 12 ? "Good morning" : "Hello"}
        {userName ? `, ${userName}` : ""}
      </Text>
      <Text color="secondary">How can Claw help you today?</Text>
    </div>
  );

  return (
    <div className="claw-chat" ref={scrollRef}>
      <ChatLayout
        emptyState={items.length === 0 && !streaming && !busy ? greeting : undefined}
        composer={
          <div className="claw-composer">
            {error && <ErrorText>{error}</ErrorText>}
            {attachments.length > 0 && (
              <div className="claw-attach-row">
                {attachments.map((a, i) => (
                  <span key={a.path} className="claw-attach-chip">
                    <Icon icon={a.is_image ? ImageIcon : FileIcon} size="sm" color="secondary" />
                    {a.name}
                    <IconButton
                      label="Remove attachment"
                      icon={<Icon icon="close" size="xsm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                    />
                  </span>
                ))}
              </div>
            )}
            <input
              ref={fileRef}
              type="file"
              multiple
              style={{ display: "none" }}
              onChange={(e) => void onPickFiles(e.target.files)}
            />
            <ChatComposer
              onSubmit={send}
              placeholder="Message Claw…"
              isDisabled={false}
              footerActions={
                <Button
                  label={uploading ? "Uploading…" : "Attach"}
                  icon={<Icon icon={Paperclip} size="sm" />}
                  size="md"
                  variant="ghost"
                  isDisabled={uploading}
                  clickAction={() => fileRef.current?.click()}
                />
              }
            />
          </div>
        }
      >
        <div className="claw-column">
          <ChatMessageList density="spacious">
            {items.map((item, i) =>
              item.kind === "tools" ? (
                <ChatToolCalls
                  key={i}
                  calls={item.calls.map((c, j) => ({
                    key: `${i}-${j}`,
                    name: c.name,
                    status: c.status,
                    target: c.argsPreview,
                    duration: c.duration,
                    errorMessage: c.status === "error" ? c.resultPreview : undefined,
                    resultDetail: c.resultPreview ? <Text size="sm">{c.resultPreview}</Text> : undefined,
                  }))}
                />
              ) : (
                <ChatMessage key={i} sender={item.role === "user" ? "user" : "assistant"}>
                  {item.role === "assistant" ? (
                    <>
                      <ChatMessageBubble variant="ghost">
                        <Markdown>{item.content}</Markdown>
                      </ChatMessageBubble>
                      <ChatMessageMetadata
                        footer={
                          <div className="claw-feedback">
                            <IconButton
                              label="Good response"
                              icon={
                                <Icon icon={ThumbsUp} size="sm" color={feedback[i] === "up" ? "success" : "secondary"} />
                              }
                              variant="ghost"
                              size="sm"
                              clickAction={() => rate(i, item.content, "up")}
                            />
                            <IconButton
                              label="Bad response"
                              icon={
                                <Icon icon={ThumbsDown} size="sm" color={feedback[i] === "down" ? "error" : "secondary"} />
                              }
                              variant="ghost"
                              size="sm"
                              clickAction={() => rate(i, item.content, "down")}
                            />
                          </div>
                        }
                      />
                    </>
                  ) : (
                    <ChatMessageBubble>{item.content}</ChatMessageBubble>
                  )}
                </ChatMessage>
              ),
            )}
            {streaming && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost">
                  <Markdown>{streaming}</Markdown>
                  <span className="claw-cursor">▍</span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
            {busy && !streaming && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost">
                  <span className="claw-thinking">
                    <Spinner size="sm" shade="subtle" /> Thinking…
                  </span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
          </ChatMessageList>
        </div>
      </ChatLayout>
    </div>
  );
}
