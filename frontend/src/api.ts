import type { AwardCode, ChatResponse } from "./types";

// Dev: "/api" is proxied to the local FastAPI app (vite.config.ts). Production: the API URL.
const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** POST /chat. Throws ApiError with a message that can be shown to the user as is. */
export async function askQuestion(
  question: string,
  award: AwardCode | null,
  fetchFn: typeof fetch = fetch,
): Promise<ChatResponse> {
  let res: Response;
  try {
    res = await fetchFn(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, award }),
    });
  } catch {
    throw new ApiError("Can't reach the assistant. Check your connection and try again.", null);
  }
  if (res.ok) return (await res.json()) as ChatResponse;

  // FastAPI errors: {"detail": "..."} (503, our 422) or {"detail": [{msg, ...}]} (validation).
  let detail = "";
  try {
    const body = await res.json();
    detail = typeof body?.detail === "string" ? body.detail : "";
  } catch {
    /* not JSON: a proxy error page */
  }
  if (res.status === 503) {
    throw new ApiError(detail || "The assistant is busy. Please try again in a minute.", 503);
  }
  if (res.status === 422) {
    throw new ApiError(detail || "That question couldn't be sent. Please rephrase it.", 422);
  }
  if (res.status >= 500 && !detail) {
    throw new ApiError("The assistant isn't available right now. Please try again soon.", res.status);
  }
  throw new ApiError(detail || `Something went wrong (HTTP ${res.status}).`, res.status);
}
