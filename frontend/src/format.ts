import { AWARDS, type Route } from "./types";

export interface CitationLabel {
  source: string; // "Hospitality", "NES", "WorkSafe"
  ref: string; // "cl. 28.2", "s 117", "first steps"
}

const AWARD_NAMES: Record<string, string> = Object.fromEntries(
  AWARDS.map((a) => [a.code, a.name]),
);

/** Short chip label for a citation id (see CLAUDE.md "Citations"). "#n" marks a split part;
 * WorkSafe ids split at their last ":" (page slug, then section slug). */
export function citationLabel(id: string): CitationLabel {
  const [base, part] = id.split("#", 2);
  const suffix = part ? ` (part ${part})` : "";
  if (base.startsWith("WSV:")) {
    const rest = base.slice(4);
    const section = rest.slice(rest.lastIndexOf(":") + 1);
    return { source: "WorkSafe", ref: section.replace(/-/g, " ") + suffix };
  }
  if (base.startsWith("NES:")) {
    return { source: "NES", ref: base.slice(4).replace(/^s/, "s ") + suffix };
  }
  const i = base.indexOf(":");
  if (i < 0) return { source: base, ref: suffix.trim() };
  const code = base.slice(0, i);
  return { source: AWARD_NAMES[code] ?? code, ref: `cl. ${base.slice(i + 1)}${suffix}` };
}

export type Tone = "accent" | "warn" | "danger" | "muted";

export const ROUTE_INFO: Record<Route, { label: string; tone: Tone; hint: string }> = {
  rag: { label: "Answer", tone: "accent", hint: "Answered from the cited clauses" },
  pay_rate: {
    label: "Pay rate",
    tone: "accent",
    hint: "Amounts come from the award's pay-rate table, not from the language model",
  },
  clarify: { label: "Needs a detail", tone: "warn", hint: "Reply with the missing detail" },
  insufficient_context: {
    label: "Not in sources",
    tone: "muted",
    hint: "The awards, NES and WorkSafe pages don't cover this",
  },
  fallback: { label: "No reliable answer", tone: "muted", hint: "Try rephrasing the question" },
  refer: {
    label: "Referred",
    tone: "warn",
    hint: "Personal situations need advice from the Fair Work Ombudsman",
  },
  blocked: { label: "Not answered", tone: "danger", hint: "Stopped by a safety check" },
  out_of_scope: { label: "Out of scope", tone: "muted", hint: "Not a workplace-rights question" },
};

export function formatSeconds(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}
