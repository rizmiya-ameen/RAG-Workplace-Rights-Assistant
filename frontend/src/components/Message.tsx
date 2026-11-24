import { citationLabel, formatSeconds } from "../format";
import type { ChatResponse, Citation } from "../types";
import { RouteBadge } from "./RouteBadge";

export type Turn =
  | { kind: "user"; id: number; text: string; note?: string }
  | { kind: "answer"; id: number; response: ChatResponse }
  | { kind: "error"; id: number; text: string };

export function UserMessage({ text, note }: { text: string; note?: string }) {
  return (
    <div className="flex flex-col items-end">
      <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-accent px-4 py-2.5 whitespace-pre-wrap text-accent-ink">
        {text}
      </p>
      {note && <p className="mt-1 text-xs text-muted">{note}</p>}
    </div>
  );
}

export function AnswerMessage({
  response,
  onOpenCitation,
}: {
  response: ChatResponse;
  onOpenCitation: (c: Citation) => void;
}) {
  return (
    <article className="rounded-2xl rounded-bl-sm border border-line bg-surface px-4 py-3.5 shadow-sm">
      <RouteBadge route={response.route} />
      {/* Plain text, never HTML: the answer comes from a language model. */}
      <p className="mt-2.5 leading-relaxed whitespace-pre-wrap">{response.answer}</p>

      {response.citations.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-xs font-semibold tracking-wider text-muted uppercase">
            Sources
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {response.citations.map((c) => {
              const label = citationLabel(c.id);
              return (
                <li key={c.id}>
                  <button
                    onClick={() => onOpenCitation(c)}
                    title={c.heading || c.id}
                    className="inline-flex items-baseline gap-1.5 rounded-md border border-line bg-paper px-2 py-1 text-sm hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-accent"
                  >
                    <span className="font-semibold">{label.source}</span>
                    <span className="font-mono text-xs">{label.ref}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <p className="mt-3 border-t border-line pt-2.5 text-xs leading-relaxed text-muted">
        {response.disclaimer}
      </p>
      <p className="mt-1 font-mono text-[0.7rem] text-muted" title="Request id (for support)">
        {formatSeconds(response.latency_ms)} · ref {response.request_id.slice(0, 8)}
      </p>
    </article>
  );
}

export function ErrorMessage({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 rounded-xl bg-danger-soft px-4 py-3 text-sm text-danger"
    >
      <span className="flex-1">{text}</span>
      {onRetry && (
        <button onClick={onRetry} className="font-semibold underline underline-offset-2">
          Try again
        </button>
      )}
    </div>
  );
}
