import { Markdown } from "@astryxdesign/core/Markdown";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { ExternalLink, Link2Off } from "lucide-react";
import { useEffect, useState } from "react";
import { SoftnixLogo } from "./Logo";
import { SharedConversation, api, shareFileUrl } from "./api";

/**
 * Public, read-only view of a shared answer. Rendered (from main.tsx) for the
 * /s/<token> route WITHOUT any authentication — the capability token in the URL
 * is the only credential. No composer, sidebar, or tool internals: just the
 * question, the answer, and any files copied into the snapshot.
 */
export function SharedView({ token }: { token: string }) {
  const [data, setData] = useState<SharedConversation | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Keep shared pages out of search indexes even if the header is missed.
    const meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex, nofollow";
    document.head.appendChild(meta);
    return () => {
      document.head.removeChild(meta);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .getShare(token)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        document.title = d.title ? `${d.title} · Softnix PrivateClaw` : "Shared answer";
      })
      .catch(() => {
        if (!cancelled) setError("This link has expired or is no longer available.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className="claw-share-page">
      <header className="claw-share-header">
        <a className="claw-share-brand" href="/" title="Softnix PrivateClaw">
          <SoftnixLogo />
        </a>
      </header>

      <main className="claw-share-main">
        {loading ? (
          <div className="claw-share-status">
            <Spinner size="md" shade="subtle" />
          </div>
        ) : error ? (
          <div className="claw-share-status claw-share-empty">
            <Icon />
            <Text size="lg" weight="semibold">
              Link unavailable
            </Text>
            <Text size="sm" color="secondary">
              {error}
            </Text>
          </div>
        ) : data ? (
          <article className="claw-share-doc">
            {data.title && (
              <Text as="h1" size="xl" weight="bold" className="claw-share-title">
                {data.title}
              </Text>
            )}
            {data.messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="claw-share-question">
                  <Text size="sm" weight="semibold" color="secondary" className="claw-share-role">
                    Question
                  </Text>
                  <div className="claw-share-question-body">
                    <Text size="base">{m.content}</Text>
                  </div>
                </div>
              ) : (
                <div key={i} className="claw-share-answer">
                  <Markdown>{m.content}</Markdown>
                  {m.files.length > 0 && (
                    <div className="claw-artifacts">
                      {m.files.map((f) => {
                        const href = shareFileUrl(token, f.name);
                        return f.is_image ? (
                          <a
                            key={f.name}
                            className="claw-artifact-image"
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <img src={href} alt={f.name} loading="lazy" />
                          </a>
                        ) : (
                          <a
                            key={f.name}
                            className="claw-artifact-chip"
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <span className="claw-artifact-name">{f.name.replace(/^\d+-/, "")}</span>
                            <ExternalLink size={14} />
                          </a>
                        );
                      })}
                    </div>
                  )}
                </div>
              ),
            )}
          </article>
        ) : null}
      </main>

      <footer className="claw-share-footer">
        <Text size="xsm" color="secondary">
          Shared from{" "}
          <a href="/" className="claw-share-link">
            Softnix PrivateClaw
          </a>
          . This is a read-only snapshot.
        </Text>
      </footer>
    </div>
  );
}

// Small helper so the empty-state icon matches lucide sizing without extra deps.
function Icon() {
  return <Link2Off size={40} strokeWidth={1.5} className="claw-share-empty-icon" />;
}
