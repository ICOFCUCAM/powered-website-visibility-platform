"use client";

/**
 * The AI Strategist.
 *
 * Opens with questions rather than a blank box (docs/07-ui.md §5): a chat
 * that asks a non-technical owner to think of a good question usually gets no
 * question at all.
 *
 * Tool calls are shown as they happen — "checking your search performance" —
 * for two reasons. It is the only honest thing to show during a pause of
 * several seconds, and it tells the customer the answer came from THEIR data
 * rather than from a model's general opinion about websites. The steps stay
 * visible under the answer afterwards, collapsed, because when somebody
 * disputes a figure the useful reply is which query ran over which window.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  askAssistant,
  type Assistant,
  type ConversationMessage,
  type ConversationStep,
  type Website,
} from "@/lib/api";
import { clearToken, readToken } from "@/lib/session";

interface Turn {
  role: "user" | "assistant";
  content: string;
  steps: ConversationStep[];
  pending?: boolean;
}

function Steps({ steps }: { steps: ConversationStep[] }) {
  if (steps.length === 0) return null;
  return (
    <details className="steps-log">
      <summary>
        {steps.length === 1 ? "1 check" : `${steps.length} checks`} against your
        data
      </summary>
      <ul>
        {steps.map((step, index) => (
          <li key={index}>
            <code>{step.tool}</code>
            {Object.keys(step.input ?? {}).length > 0 ? (
              <span className="meta"> {describe(step.input)}</span>
            ) : null}
            {step.error ? (
              <span className="steps-log__error"> — {step.error}</span>
            ) : typeof step.rows === "number" ? (
              // Only where a count means something. A summary returns one
              // answer, not rows, and "0 rows" beside it reads as "nothing
              // came back".
              <span className="meta">
                {" "}
                · {step.rows} {step.rows === 1 ? "row" : "rows"}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

/** `period=28d direction=down` — exact, and readable without a JSON parser. */
function describe(input: Record<string, unknown>): string {
  return Object.entries(input ?? {})
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(" ");
}

export default function AssistantPage() {
  const router = useRouter();
  const [website, setWebsite] = useState<Website | null>(null);
  const [meta, setMeta] = useState<Assistant | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) return router.replace("/login");
    try {
      const websites = await api.listWebsites(token);
      if (websites.length === 0) return router.replace("/onboarding");
      setWebsite(websites[0]);
      setMeta(await api.conversations(token, websites[0].id));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        return router.replace("/login");
      }
      setError(
        err instanceof ApiError ? err.message : "We couldn't reach the API.",
      );
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, running]);

  async function open(id: string) {
    const token = readToken();
    if (!token || !website) return;
    const body = await api.conversation(token, website.id, id);
    setConversationId(id);
    setTurns(
      body.messages.map((message: ConversationMessage) => ({
        role: message.role,
        content: message.content,
        steps: message.steps ?? [],
      })),
    );
  }

  async function ask(question: string) {
    const token = readToken();
    if (!token || !website || busy) return;
    const asked = question.trim();
    if (!asked) return;

    setDraft("");
    setError(null);
    setBusy(true);
    // Set before the first frame arrives. The long pause in a conversation is
    // the model's first round, which streams no text at all because it goes
    // straight to tool calls — so a UI that only reacts to frames shows an
    // empty box for several seconds.
    setRunning("reading your question");
    setTurns((current) => [
      ...current,
      { role: "user", content: asked, steps: [] },
      { role: "assistant", content: "", steps: [], pending: true },
    ]);

    const steps: ConversationStep[] = [];
    let text = "";

    const update = () =>
      setTurns((current) => {
        const next = [...current];
        next[next.length - 1] = {
          role: "assistant",
          content: text,
          steps: [...steps],
          pending: true,
        };
        return next;
      });

    try {
      await askAssistant(
        token,
        website.id,
        asked,
        conversationId,
        (frame) => {
          if (frame.type === "conversation") {
            setConversationId(frame.id as string);
          } else if (frame.type === "delta") {
            text += frame.text as string;
            // Once words are arriving, the indicator has nothing left to say.
            setRunning(null);
            update();
          } else if (frame.type === "step") {
            setRunning(frame.label as string);
            steps.push({
              tool: frame.tool as string,
              input: (frame.input as Record<string, unknown>) ?? {},
              rows: 0,
              ms: 0,
              error: null,
            });
          } else if (frame.type === "done") {
            setRunning(null);
            const final = (frame.steps as ConversationStep[]) ?? steps;
            setTurns((current) => {
              const next = [...current];
              next[next.length - 1] = {
                role: "assistant",
                content: text.trim(),
                steps: final,
              };
              return next;
            });
          } else if (frame.type === "error") {
            setRunning(null);
            setError(frame.message as string);
            setTurns((current) => current.slice(0, -1));
          }
        },
      );
    } catch (err) {
      setTurns((current) => current.slice(0, -1));
      setError(
        err instanceof ApiError
          ? err.message
          : "The assistant stopped answering. Please try again.",
      );
    } finally {
      setRunning(null);
      setBusy(false);
      const token2 = readToken();
      if (token2 && website) setMeta(await api.conversations(token2, website.id));
    }
  }

  if (!website || !meta) {
    return (
      <main className="wrap">
        {error ? <p className="error">{error}</p> : <p className="meta">Loading…</p>}
      </main>
    );
  }

  if (!meta.available) {
    return (
      <main className="wrap">
        <p className="eyebrow">
          <Link href="/dashboard">← Dashboard</Link>
        </p>
        <h1 className="plan__title">Ask about your website</h1>
        <p className="empty">
          The assistant isn&apos;t switched on for this installation. Your
          dashboard, audit and weekly plan work without it — every figure in
          them comes from your own Google data and our scan.
        </p>
      </main>
    );
  }

  return (
    <main className="wrap">
      <p className="eyebrow">
        <Link href="/dashboard">← Dashboard</Link>
      </p>
      <h1 className="plan__title">Ask about {website.domain}</h1>
      <p className="plan__sub">
        Answers come from your own Search Console data and our scan of your
        site. Nothing else.
      </p>

      {turns.length === 0 ? (
        <div className="section">
          <p className="section__title">Try asking</p>
          <div className="filters">
            {meta.suggested_questions.map((question) => (
              <button
                className="chip"
                key={question}
                disabled={busy}
                onClick={() => ask(question)}
              >
                {question}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="chat">
        {turns.map((turn, index) => (
          <div className={`turn turn--${turn.role}`} key={index}>
            {turn.role === "assistant" ? (
              <>
                {turn.content ? (
                  <div className="turn__body">{turn.content}</div>
                ) : null}
                {!turn.pending ? <Steps steps={turn.steps} /> : null}
              </>
            ) : (
              <div className="turn__body">{turn.content}</div>
            )}
          </div>
        ))}
        {running ? <p className="running">{running}…</p> : null}
        <div ref={bottom} />
      </div>

      {error ? <p className="error">{error}</p> : null}

      <form
        className="ask"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(draft);
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Why did my traffic change?"
          disabled={busy}
          aria-label="Ask a question about your website"
        />
        <button type="submit" disabled={busy || draft.trim().length === 0}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>

      {meta.conversations.length > 0 ? (
        <div className="section">
          <p className="section__title">Earlier questions</p>
          <ul className="list">
            {meta.conversations.slice(0, 8).map((conversation) => (
              <li className="item" key={conversation.id}>
                <button
                  className="linkish"
                  onClick={() => open(conversation.id)}
                >
                  {conversation.title ?? "Untitled"}
                </button>
                <span className="meta">
                  {new Date(conversation.last_message_at).toLocaleDateString(
                    "en-GB",
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </main>
  );
}
