import { ROUTE_INFO, type Tone } from "../format";
import type { Route } from "../types";

const TONES: Record<Tone, string> = {
  accent: "bg-accent-soft text-accent",
  warn: "bg-warn-soft text-warn",
  danger: "bg-danger-soft text-danger",
  muted: "bg-paper text-muted ring-1 ring-line",
};

export function RouteBadge({ route }: { route: Route }) {
  const info = ROUTE_INFO[route];
  return (
    <span
      title={info.hint}
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold tracking-wide uppercase ${TONES[info.tone]}`}
    >
      {info.label}
    </span>
  );
}
