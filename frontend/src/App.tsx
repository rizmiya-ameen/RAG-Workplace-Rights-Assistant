import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, askQuestion } from "./api";
import { CitationPanel } from "./components/CitationPanel";
import { AnswerMessage, ErrorMessage, UserMessage, type Turn } from "./components/Message";
import { AWARDS, MAX_QUESTION_CHARS, type AwardCode, type Citation } from "./types";

// Real questions from the eval sets (eval/golden_set.jsonl, eval/routes.jsonl).
const EXAMPLES = [
  { tag: "Hospitality", q: "What is the casual loading for hospitality workers?" },
  { tag: "Pay rate", q: "How much does a level 2 casual get in a hotel on a Sunday?" },
  { tag: "Retail", q: "What is the minimum shift length for a casual retail worker?" },
  { tag: "NES", q: "How much paid annual leave do full-time employees get each year?" },
  { tag: "Clerks", q: "Can an office worker ask to work from home?" },
  { tag: "WorkSafe", q: "What should a worker do if they are sexually harassed at work?" },
];

export const SLOW_AFTER_MS = 10_000; // longer than a normal answer (p95 ~10 s)

/** The API is stateless. When it asked for a missing detail (route "clarify"), the reply is
 * sent together with the question it belongs to, so "level 2" alone still makes sense. */
export function followUpQuestion(pending: string | null, reply: string): string {
  if (!pending) return reply;
  const joined = `${pending} ${reply}`;
  return joined.length <= MAX_QUESTION_CHARS ? joined : reply;
}

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [award, setAward] = useState<AwardCode | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<string | null>(null); // question awaiting a detail
  const [lastSent, setLastSent] = useState<string | null>(null);
  const [open, setOpen] = useState<Citation | null>(null);
  const nextId = useRef(0);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [turns, loading]);

  // The deployed API scales to zero when idle; the first answer after a quiet spell waits
  // for the container to start and load its models (up to about a minute).
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!loading) return setSlow(false);
    const timer = setTimeout(() => setSlow(true), SLOW_AFTER_MS);
    return () => clearTimeout(timer);
  }, [loading]);

  const send = useCallback(
    async (typed: string, opts: { retry?: boolean } = {}) => {
      const text = typed.trim();
      if (!text || loading) return;
      const question = opts.retry ? text : followUpQuestion(pending, text);
      const note = !opts.retry && question !== text ? "Sent with your earlier question" : undefined;
      if (!opts.retry) {
        setTurns((t) => [...t, { kind: "user", id: nextId.current++, text, note }]);
      }
      setDraft("");
      setLoading(true);
      setLastSent(question);
      try {
        const response = await askQuestion(question, award);
        setPending(response.route === "clarify" ? question : null);
        setTurns((t) => [...t, { kind: "answer", id: nextId.current++, response }]);
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : "Something went wrong.";
        setTurns((t) => [...t, { kind: "error", id: nextId.current++, text: msg }]);
      } finally {
        setLoading(false);
      }
    },
    [award, loading, pending],
  );

  const closePanel = useCallback(() => setOpen(null), []);
  const last = turns[turns.length - 1];

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-paper/95 backdrop-blur">
        <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <div className="min-w-[15rem] flex-1">
            <h1 className="text-lg font-bold tracking-tight">Workplace Rights Assistant</h1>
            <p className="hidden text-sm text-muted sm:block">
              Modern Awards, National Employment Standards, WorkSafe Victoria
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Award</span>
            <select
              value={award ?? ""}
              onChange={(e) => setAward((e.target.value || null) as AwardCode | null)}
              className="rounded-md border border-line bg-surface px-2 py-1.5 text-ink focus-visible:outline-2 focus-visible:outline-accent"
            >
              <option value="">Any award</option>
              {AWARDS.map((a) => (
                <option key={a.code} value={a.code}>
                  {a.name} ({a.code})
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="border-t border-line bg-warn-soft">
          <p className="mx-auto max-w-3xl px-4 py-2 text-sm text-warn">
            <strong>General information only, not legal advice.</strong> For your own situation,
            contact the Fair Work Ombudsman on <span className="whitespace-nowrap">13 13 94</span>.
          </p>
        </div>
      </header>

      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6">
        {turns.length === 0 ? (
          <section aria-labelledby="intro" className="pt-4">
            <h2 id="intro" className="text-2xl font-semibold tracking-tight">
              Ask about pay, hours, leave or safety at work
            </h2>
            <p className="mt-2 max-w-prose text-muted">
              Answers come from four Modern Awards (Hospitality, General Retail, Fast Food,
              Clerks), the National Employment Standards and WorkSafe Victoria guidance, and cite
              the clause they rely on. Exact pay rates come from the award's pay tables.
            </p>
            <ul className="mt-6 grid gap-2 sm:grid-cols-2">
              {EXAMPLES.map((ex) => (
                <li key={ex.q}>
                  <button
                    onClick={() => send(ex.q)}
                    className="h-full w-full rounded-xl border border-line bg-surface px-4 py-3 text-left hover:border-accent focus-visible:outline-2 focus-visible:outline-accent"
                  >
                    <span className="text-xs font-semibold tracking-wider text-accent uppercase">
                      {ex.tag}
                    </span>
                    <span className="mt-1 block">{ex.q}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : (
          <ol className="flex flex-col gap-4" aria-live="polite">
            {turns.map((t) => (
              <li key={t.id}>
                {t.kind === "user" && <UserMessage text={t.text} note={t.note} />}
                {t.kind === "answer" && (
                  <AnswerMessage response={t.response} onOpenCitation={setOpen} />
                )}
                {t.kind === "error" && (
                  <ErrorMessage
                    text={t.text}
                    onRetry={
                      t === last && lastSent && !loading
                        ? () => send(lastSent, { retry: true })
                        : undefined
                    }
                  />
                )}
              </li>
            ))}
            {loading && (
              <li className="flex items-center gap-2 text-sm text-muted" role="status">
                <span className="size-2 shrink-0 animate-pulse rounded-full bg-accent" />
                {slow
                  ? "Waking the assistant up. It sleeps when nobody is using it, so the first answer can take up to a minute…"
                  : "Checking the awards and guidance…"}
              </li>
            )}
          </ol>
        )}
        <div ref={endRef} />
      </main>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
        className="sticky bottom-0 border-t border-line bg-paper/95 backdrop-blur"
      >
        <div className="mx-auto flex max-w-3xl items-end gap-2 px-4 py-3">
          <div className="min-w-0 flex-1">
            <label htmlFor="question" className="sr-only">
              Your question
            </label>
            <textarea
              id="question"
              rows={2}
              value={draft}
              maxLength={MAX_QUESTION_CHARS}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(draft);
                }
              }}
              placeholder={
                pending ? "Reply with the missing detail…" : "e.g. Do casuals get more on Sundays?"
              }
              className="block w-full resize-none rounded-xl border border-line bg-surface px-3.5 py-2.5 focus-visible:outline-2 focus-visible:outline-accent"
            />
            <p className="mt-1 text-right text-xs text-muted">
              {draft.length}/{MAX_QUESTION_CHARS} · don't include names or contact details
            </p>
          </div>
          <button
            type="submit"
            disabled={loading || !draft.trim()}
            className="mb-5 rounded-xl bg-accent px-4 py-2.5 font-semibold text-accent-ink disabled:opacity-40"
          >
            Ask
          </button>
        </div>
      </form>

      {open && <CitationPanel citation={open} onClose={closePanel} />}
    </div>
  );
}
