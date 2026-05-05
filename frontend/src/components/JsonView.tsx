/**
 * Read-only JSON viewer with cheap regex-based syntax highlighting.
 *
 * `dangerouslySetInnerHTML` is safe here because we escape the input
 * (`&`, `<`, `>`) BEFORE running the highlight regexes — the regexes
 * only insert known `<span>` wrappers around already-escaped content.
 */

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function jsonHighlight(value: unknown): string {
  const json = JSON.stringify(value, null, 2) ?? "";
  return escapeHtml(json)
    .replace(/("(?:\\.|[^"\\])*")(\s*:)/g, '<span class="k">$1</span>$2')
    .replace(/:\s*("(?:\\.|[^"\\])*")/g, ': <span class="s">$1</span>')
    .replace(/:\s*(true|false)/g, ': <span class="b">$1</span>')
    .replace(/:\s*(-?\d+(?:\.\d+)?)/g, ': <span class="n">$1</span>');
}

export function JsonView({ value }: { value: unknown }) {
  return (
    <pre className="json" dangerouslySetInnerHTML={{ __html: jsonHighlight(value ?? {}) }} />
  );
}
