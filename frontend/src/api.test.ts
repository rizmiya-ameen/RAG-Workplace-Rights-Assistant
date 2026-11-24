import { describe, expect, it, vi } from "vitest";
import { ApiError, askQuestion } from "./api";

function reply(status: number, body: unknown) {
  return vi.fn().mockResolvedValue(
    new Response(typeof body === "string" ? body : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("askQuestion", () => {
  it("posts the question and award to /api/chat", async () => {
    const fetchFn = reply(200, { answer: "ok", route: "rag" });
    const r = await askQuestion("What is the casual loading?", "MA000009", fetchFn);
    expect(r.answer).toBe("ok");
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe("/api/chat");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      question: "What is the casual loading?",
      award: "MA000009",
    });
  });

  it("shows the API's 503 message (LLM busy)", async () => {
    const fetchFn = reply(503, { detail: "The language model is busy right now." });
    await expect(askQuestion("q", null, fetchFn)).rejects.toEqual(
      new ApiError("The language model is busy right now.", 503),
    );
  });

  it("does not show FastAPI validation details", async () => {
    const fetchFn = reply(422, { detail: [{ msg: "String should have at most 1000 characters" }] });
    await expect(askQuestion("q", null, fetchFn)).rejects.toMatchObject({
      status: 422,
      message: "That question couldn't be sent. Please rephrase it.",
    });
  });

  it("explains a proxy error when the API is down", async () => {
    const fetchFn = reply(502, "<html>Bad gateway</html>");
    await expect(askQuestion("q", null, fetchFn)).rejects.toMatchObject({ status: 502 });
  });

  it("turns a network failure into a friendly error", async () => {
    const fetchFn = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(askQuestion("q", null, fetchFn)).rejects.toMatchObject({
      status: null,
      message: expect.stringContaining("Can't reach the assistant"),
    });
  });
});
