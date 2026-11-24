// Mirrors backend/app/pipeline.py (ChatResponse, Citation, Route) and app/main.py (Award).

export type Route =
  | "rag"
  | "pay_rate"
  | "clarify"
  | "insufficient_context"
  | "fallback"
  | "blocked"
  | "refer"
  | "out_of_scope";

export interface Citation {
  id: string; // "MA000009:28.2", "MA000009:28.2#1", "NES:s117", "WSV:<page>:<section>"
  title: string;
  url: string;
  clause_text: string;
  heading: string;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  route: Route;
  guardrails: string[];
  request_id: string;
  disclaimer: string;
  latency_ms: number;
}

export const AWARDS = [
  { code: "MA000009", name: "Hospitality", full: "Hospitality Industry (General) Award 2020" },
  { code: "MA000004", name: "General Retail", full: "General Retail Industry Award 2020" },
  { code: "MA000003", name: "Fast Food", full: "Fast Food Industry Award 2020" },
  { code: "MA000002", name: "Clerks", full: "Clerks—Private Sector Award 2020" },
] as const;

export type AwardCode = (typeof AWARDS)[number]["code"];

export const MAX_QUESTION_CHARS = 1000; // backend/app/main.py MAX_QUESTION_CHARS
