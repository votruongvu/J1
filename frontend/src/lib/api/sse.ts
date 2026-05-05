/**
 * SSE frame parser used by the live ApiClient.
 *
 * The browser's native `EventSource` cannot send custom headers and
 * the J1 contract requires `X-Tenant-Id` / `X-Project-Id` on every
 * request, so we read the response body ourselves via `fetch` +
 * `ReadableStream` and parse SSE frames by hand.
 */

/** Result of parsing one SSE frame. `data` is the raw JSON payload. */
export interface ParsedSseFrame {
  id?: string;
  event?: string;
  data: unknown;
}

/**
 * Parse one SSE frame (a single event delimited by `\n\n`).
 *
 * Returns `null` for empty frames, comment-only frames, or invalid
 * JSON in `data:`. SSE allows multi-line `data:` (joined with `\n`)
 * and comment lines starting with `:`.
 */
export function parseSseFrame(frame: string): ParsedSseFrame | null {
  let id: string | undefined;
  let event: string | undefined;
  const dataLines: string[] = [];

  for (const raw of frame.split("\n")) {
    if (!raw || raw.startsWith(":")) continue;
    const colon = raw.indexOf(":");
    if (colon < 0) continue;
    const field = raw.slice(0, colon);
    let value = raw.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return null;
  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  return { id, event, data };
}

/**
 * Read an SSE stream from a `fetch` response body. Calls `onFrame`
 * for each complete frame. Resolves when the stream ends. Aborts
 * cleanly when the supplied `signal` fires.
 */
export async function readSseStream(
  response: Response,
  signal: AbortSignal,
  onFrame: (frame: ParsedSseFrame) => void,
): Promise<void> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (!signal.aborted) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const parsed = parseSseFrame(frame);
      if (parsed) onFrame(parsed);
    }
  }
}
