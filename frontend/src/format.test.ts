import { describe, expect, it } from "vitest";
import { citationLabel, formatSeconds, ROUTE_INFO } from "./format";

describe("citationLabel", () => {
  it.each([
    ["MA000009:28.2", "Hospitality", "cl. 28.2"],
    ["MA000004:B.2.3", "General Retail", "cl. B.2.3"],
    ["MA000002:15A.1", "Clerks", "cl. 15A.1"],
    ["MA000009:28.2#1", "Hospitality", "cl. 28.2 (part 1)"],
    ["NES:s117", "NES", "s 117"],
    ["NES:s117#2", "NES", "s 117 (part 2)"],
    ["WSV:what-do-when-youve-been-injured-work:first-steps", "WorkSafe", "first steps"],
    ["WSV:bullying:report-it#1", "WorkSafe", "report it (part 1)"],
  ])("%s -> %s %s", (id, source, ref) => {
    expect(citationLabel(id)).toEqual({ source, ref });
  });

  it("keeps an unknown award code as is", () => {
    expect(citationLabel("MA000999:1.1")).toEqual({ source: "MA000999", ref: "cl. 1.1" });
  });
});

describe("ROUTE_INFO", () => {
  it("has a label for every backend route", () => {
    const routes = [
      "rag",
      "pay_rate",
      "clarify",
      "insufficient_context",
      "fallback",
      "blocked",
      "refer",
      "out_of_scope",
    ] as const;
    for (const r of routes) expect(ROUTE_INFO[r].label).toBeTruthy();
  });
});

it("formats latency", () => {
  expect(formatSeconds(35)).toBe("35 ms");
  expect(formatSeconds(4210)).toBe("4.2 s");
});
