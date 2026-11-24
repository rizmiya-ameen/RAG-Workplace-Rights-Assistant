import { useEffect, useRef } from "react";
import { citationLabel } from "../format";
import type { Citation } from "../types";

/** The cited clause as the assistant saw it, with a link to the official page. Side sheet on
 * wide screens, bottom sheet on phones. Escape or the backdrop closes it. */
export function CitationPanel({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const label = citationLabel(citation.id);
  // Headings are paths ("Schedule B—… > B.2 Adult employees > B.2.3 …"): last part as title.
  const path = (citation.heading || citation.id).split(" > ");
  const title = path[path.length - 1];
  const parents = path.slice(0, -1).join(" › ");

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-30 flex items-end justify-end sm:items-stretch">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} aria-hidden="true" />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="citation-title"
        className="relative flex max-h-[85vh] w-full flex-col rounded-t-2xl bg-surface shadow-xl sm:max-h-none sm:w-[28rem] sm:rounded-none sm:border-l sm:border-line"
      >
        <header className="flex items-start gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold tracking-wider text-muted uppercase">
              {label.source} · {label.ref}
            </p>
            <h2 id="citation-title" className="mt-1 text-lg leading-snug font-semibold">
              {title}
            </h2>
            {citation.title && <p className="mt-1 text-sm text-muted">{citation.title}</p>}
            {parents && <p className="mt-0.5 text-xs text-muted">{parents}</p>}
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-muted hover:bg-paper hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            Close
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <p className="mb-2 font-mono text-xs text-muted">{citation.id}</p>
          <p className="text-[0.95rem] leading-relaxed whitespace-pre-wrap">{citation.clause_text}</p>
        </div>
        {citation.url && (
          <footer className="border-t border-line px-5 py-3">
            <a
              href={citation.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm font-semibold text-accent underline-offset-2 hover:underline"
            >
              Open the official source ↗
            </a>
          </footer>
        )}
      </aside>
    </div>
  );
}
