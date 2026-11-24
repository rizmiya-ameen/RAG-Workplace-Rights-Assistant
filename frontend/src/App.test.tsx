import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { followUpQuestion } from "./App";
import type { ChatResponse } from "./types";

// Clause text is verbatim MA000009 clause 11.3 (parse_award --award MA000009 --show 11.3).
function response(over: Partial<ChatResponse> = {}): ChatResponse {
  return {
    answer: "A casual must be paid for at least 2 consecutive hours each time (MA000009:11.3).",
    citations: [
      {
        id: "MA000009:11.3",
        title: "Hospitality Industry (General) Award 2020",
        url: "https://awards.fairwork.gov.au/MA000009.html",
        clause_text:
          "11.3 A casual employee must be engaged and paid for at least 2 consecutive hours of work on each occasion they are required to attend work.",
        heading: "11. Casual employees",
      },
    ],
    route: "rag",
    guardrails: [],
    request_id: "5b2d4119196a427c9e4d994add50595b",
    disclaimer: "General information only, not legal advice.",
    latency_ms: 4210,
    ...over,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function ok(body: ChatResponse) {
  return new Response(JSON.stringify(body), { status: 200 });
}
function sentBodies() {
  return fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body));
}

describe("App", () => {
  it("shows the disclaimer banner and example questions before the first question", () => {
    render(<App />);
    expect(screen.getByText(/General information only, not legal advice/)).toBeInTheDocument();
    expect(screen.getByText(/13 13 94/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /\?$/ }).length).toBeGreaterThanOrEqual(4);
  });

  it("asks a question and shows the answer, route badge, citation chip and request ref", async () => {
    fetchMock.mockResolvedValue(ok(response()));
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText("Your question"), "What is the casual loading?{Enter}");

    expect(await screen.findByText(/2 consecutive hours each time/)).toBeInTheDocument();
    expect(screen.getByText("What is the casual loading?")).toBeInTheDocument();
    expect(screen.getByText("Answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Hospitality cl\. 11\.3/ })).toBeInTheDocument();
    expect(screen.getByText(/4\.2 s · ref 5b2d4119/)).toBeInTheDocument();
    expect(sentBodies()).toEqual([{ question: "What is the casual loading?", award: null }]);
  });

  it("sends the award filter", async () => {
    fetchMock.mockResolvedValue(ok(response()));
    const user = userEvent.setup();
    render(<App />);
    await user.selectOptions(screen.getByRole("combobox"), "MA000004");
    await user.type(screen.getByLabelText("Your question"), "Minimum shift?{Enter}");
    await screen.findByText(/2 consecutive hours each time/);
    expect(sentBodies()[0].award).toBe("MA000004");
  });

  it("opens the clause text and official link from a citation chip, Escape closes", async () => {
    fetchMock.mockResolvedValue(ok(response()));
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByText("What is the casual loading for hospitality workers?"));
    await user.click(await screen.findByRole("button", { name: /Hospitality cl\. 11\.3/ }));

    const dialog = screen.getByRole("dialog", { name: "11. Casual employees" });
    expect(within(dialog).getByText(/at least 2 consecutive hours of work/)).toBeInTheDocument();
    const link = within(dialog).getByRole("link", { name: /official source/ });
    expect(link).toHaveAttribute("href", "https://awards.fairwork.gov.au/MA000009.html");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("titles the panel with the last part of a heading path", async () => {
    const c = response().citations[0];
    const heading = "Schedule B—Summary of Hourly Rates of Pay > B.2 Adult employees > B.2.3 Adult casual employees—ordinary and penalty rates";
    fetchMock.mockResolvedValue(
      ok(
        response({
          citations: [
            {
              ...c,
              id: "MA000009:B.2.3",
              heading,
              clause_text: "B.2.3 Adult casual employees—ordinary and penalty rates", // verbatim first line
            },
          ],
        }),
      ),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText("Your question"), "q{Enter}");
    await user.click(await screen.findByRole("button", { name: /Hospitality cl\. B\.2\.3/ }));
    expect(
      screen.getByRole("dialog", { name: "B.2.3 Adult casual employees—ordinary and penalty rates" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Schedule B—Summary of Hourly Rates of Pay › B.2 Adult employees"),
    ).toBeInTheDocument();
  });

  it("renders the answer as text, never as HTML", async () => {
    fetchMock.mockResolvedValue(ok(response({ answer: "<img src=x onerror=alert(1)>" })));
    const user = userEvent.setup();
    const { container } = render(<App />);
    await user.type(screen.getByLabelText("Your question"), "q{Enter}");
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("sends a reply to a clarifying question together with the original question", async () => {
    fetchMock
      .mockResolvedValueOnce(
        ok(response({ route: "clarify", citations: [], answer: "Which classification level?" })),
      )
      .mockResolvedValueOnce(ok(response({ route: "pay_rate" })));
    const user = userEvent.setup();
    render(<App />);
    const box = screen.getByLabelText("Your question");
    await user.type(box, "How much does a casual get on Sunday in a hotel?{Enter}");
    expect(await screen.findByText("Needs a detail")).toBeInTheDocument();
    await user.type(box, "level 2{Enter}");
    expect(await screen.findByText("Pay rate")).toBeInTheDocument();

    expect(sentBodies()[1].question).toBe(
      "How much does a casual get on Sunday in a hotel? level 2",
    );
    expect(screen.getByText("level 2")).toBeInTheDocument();
    expect(screen.getByText("Sent with your earlier question")).toBeInTheDocument();
  });

  it("shows an API error with a retry that resends the same question", async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "The language model is busy right now." }), {
          status: 503,
        }),
      )
      .mockResolvedValueOnce(ok(response()));
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText("Your question"), "What is the casual loading?{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("busy right now");

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/2 consecutive hours each time/)).toBeInTheDocument();
    expect(sentBodies()[1].question).toBe("What is the casual loading?");
    expect(screen.getAllByText("What is the casual loading?")).toHaveLength(1);
  });
});

describe("followUpQuestion", () => {
  it("joins a reply to the pending question, or sends the reply alone", () => {
    expect(followUpQuestion(null, "level 2")).toBe("level 2");
    expect(followUpQuestion("Sunday rate?", "level 2")).toBe("Sunday rate? level 2");
    expect(followUpQuestion("x".repeat(995), "level 2")).toBe("level 2");
  });
});
