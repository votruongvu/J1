/**
 * Shared helpers for the Assets + Raw Artifacts tabs.
 *
 * Lives in a module separate from the components so the FE-tooling
 * react-refresh rule doesn't flag mixed component / non-component
 * exports.
 */

const IMAGE_KINDS = new Set([
  "enriched.visuals",
]);

const IMAGE_EXTENSIONS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
]);

const JSON_EXTENSIONS = new Set([".json"]);
const TEXT_EXTENSIONS = new Set([".txt", ".md", ".markdown", ".csv"]);

export type ArtifactRenderMode =
  | "image"
  | "json"
  | "text"
  | "download";

/**
 * Decide how to render an artifact based on its `kind` + filename
 * extension. Conservative: anything we can't display safely inline
 * defaults to `download`.
 */
export function pickRenderMode(args: {
  kind: string;
  contentType?: string | null;
  location?: string | null;
}): ArtifactRenderMode {
  const ct = (args.contentType ?? "").toLowerCase();
  // Content-type takes priority — the server's media-type decision
  // is more reliable than guessing from the filename, and producers
  // sometimes emit JSON-extension files with `text/plain` content
  // type when the JSON itself is malformed.
  if (ct.startsWith("image/")) return "image";
  if (ct.startsWith("application/json")) return "json";
  if (ct.startsWith("text/")) return "text";

  // Fall back to filename hints when the server didn't pin a
  // recognised content-type. Octet-stream and the empty content-type
  // both flow through the extension branches here.
  if (IMAGE_KINDS.has(args.kind)) {
    const ext = extOf(args.location);
    if (IMAGE_EXTENSIONS.has(ext)) return "image";
  }
  const ext = extOf(args.location);
  if (JSON_EXTENSIONS.has(ext) && !ct.startsWith("application/octet-stream")) {
    return "json";
  }
  if (TEXT_EXTENSIONS.has(ext)) return "text";
  return "download";
}

function extOf(location: string | null | undefined): string {
  if (!location) return "";
  const dot = location.lastIndexOf(".");
  if (dot < 0) return "";
  const slash = location.lastIndexOf("/");
  if (dot < slash) return "";
  return location.slice(dot).toLowerCase();
}

/**
 * Format a byte count using power-of-two suffixes (`1.4 KB`, `2.1 MB`).
 * Negative or NaN → `"—"`.
 */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  // Show one decimal for values < 10 in the larger units (1.5 KB),
  // none otherwise (12 KB). Drop a trailing ".0" so an exact integer
  // like 2 MB doesn't render as "2.0 MB".
  const formatted = v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1);
  const trimmed = formatted.endsWith(".0")
    ? formatted.slice(0, -2)
    : formatted;
  return `${trimmed} ${units[i]}`;
}

/**
 * Force a browser download for a Blob.
 *
 * Creates an object URL, anchors a hidden `<a>`, clicks it, and
 * revokes the URL afterwards. Works in jsdom-less unit tests too —
 * the click is a no-op without DOM.
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } finally {
    // Defer revoke so the browser has time to start the download —
    // some Chromiums will cancel an in-flight transfer if the URL
    // is revoked synchronously.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
