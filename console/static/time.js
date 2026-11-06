// Time formatting for every page of the JFrog OIDC Console (Serverless, Infrastructure):
// one copy, no side effects, so any page can import it. Times are stored in UTC and shown in the
// browser's own time zone.

const asDate = (iso) => { const d = new Date(iso || ""); return Number.isNaN(d.getTime()) ? null : d; };

// "just now", "4 min ago", "3 h ago", "2 d ago".
export function ago(iso) {
  const d = asDate(iso);
  if (!d) return "";
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// "Nov 1, 2026, 09:10:41 PDT"
export const stamp = (iso) => asDate(iso)?.toLocaleString([], { year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" }) || "";
// Log rows: "Nov 1, 2026, 09:10:41", 24-hour.
export const clock = (iso) => asDate(iso)?.toLocaleString([], { year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) || "";
// Short form for cards and lists: "Nov 1, 2026, 09:10".
export const when = (iso) => asDate(iso)?.toLocaleString([], { year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", hour12: false }) || "";

// Milliseconds as "<1 s", "42 s", "1 m 32 s", "2 h 5 m".
export function dur(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 1) return "<1 s";
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} m ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} m`;
}

// Between two timestamps (to: now when absent).
export const duration = (from, to) => (from ? dur((to ? Date.parse(to) : Date.now()) - Date.parse(from)) : "");
