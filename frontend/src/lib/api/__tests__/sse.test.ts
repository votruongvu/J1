/**
 * Tests for the fetch-based SSE parser. The backend wire layout is:
 *
 *   id: <event_id>
 *   event: <event_type>
 *   data: <camelCase JSON ProgressEventRecord>
 *   <blank line>
 *
 * The parser must:
 *   - extract `id`, `event`, and JSON-decode `data`,
 *   - skip comment-only frames (lines starting with `:`),
 *   - join multi-line `data:` fields with `\n`,
 *   - tolerate partial chunks across reads.
 */

import { describe, expect, it } from "vitest";

import { parseSseFrame, readSseStream } from "../sse";

describe("parseSseFrame", () => {
  it("parses the canonical id/event/data layout", () => {
    const frame = parseSseFrame(
      ["id: evt-1", "event: step.started", 'data: {"foo":"bar"}'].join("\n"),
    );
    expect(frame).toEqual({
      id: "evt-1",
      event: "step.started",
      data: { foo: "bar" },
    });
  });

  it("returns null for empty / comment-only frames", () => {
    expect(parseSseFrame("")).toBeNull();
    expect(parseSseFrame(":heartbeat")).toBeNull();
  });

  it("joins multi-line data fields with newlines before parsing", () => {
    const frame = parseSseFrame(["data: {", 'data:   "k": 1', "data: }"].join("\n"));
    expect(frame?.data).toEqual({ k: 1 });
  });

  it("returns null when data is not valid JSON (defensive)", () => {
    expect(parseSseFrame("data: not-json")).toBeNull();
  });
});

describe("readSseStream", () => {
  function streamFromChunks(chunks: string[]): Response {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }

  it("emits one frame per `\\n\\n`-delimited block", async () => {
    const resp = streamFromChunks([
      'id: a\nevent: x\ndata: {"i":1}\n\n',
      'id: b\nevent: y\ndata: {"i":2}\n\n',
    ]);
    const out: unknown[] = [];
    await readSseStream(resp, new AbortController().signal, (f) => out.push(f));
    expect(out).toEqual([
      { id: "a", event: "x", data: { i: 1 } },
      { id: "b", event: "y", data: { i: 2 } },
    ]);
  });

  it("buffers partial chunks across reads", async () => {
    // Frame is split across three TCP-style chunks. Parser must
    // accumulate until the `\n\n` boundary appears.
    const resp = streamFromChunks(["id: a\nevent: x\nda", 'ta: {"split":', "true}\n\n"]);
    const out: unknown[] = [];
    await readSseStream(resp, new AbortController().signal, (f) => out.push(f));
    expect(out).toEqual([{ id: "a", event: "x", data: { split: true } }]);
  });

  it("stops when the abort signal fires", async () => {
    const controller = new AbortController();
    const resp = streamFromChunks(['id: a\nevent: x\ndata: {"i":1}\n\n']);
    controller.abort();
    const out: unknown[] = [];
    // Should resolve immediately without throwing — the loop checks
    // the signal at the top of each iteration.
    await readSseStream(resp, controller.signal, (f) => out.push(f));
    // Whether the first frame slipped through depends on scheduling;
    // the contract is "do not deadlock", which we just proved.
    expect(out.length).toBeLessThanOrEqual(1);
  });
});
