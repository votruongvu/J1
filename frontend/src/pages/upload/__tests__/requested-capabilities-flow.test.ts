/**
 * Contract — Knowledge Index checkbox state flows to upload payload.
 *
 * Pins the load-bearing seam between the FE picker and the
 * server: when the operator submits the Start Indexing dialog,
 * the three checkbox booleans MUST appear in the request payload
 * as ``requestedCapabilities`` (JSON string in the multipart
 * field). The backend re-parses them into the typed
 * ``RequestedCapabilities`` model and folds them onto the
 * AssessmentPlan's ``required_capabilities`` (BE contract tests
 * pin that in ``test_requested_capabilities_end_to_end.py``).
 *
 * This file exercises the FE side end-to-end at the seam:
 *   - ``ApiClient.upload`` accepts the new ``requestedCapabilities``
 *     argument.
 *   - When provided, it appends a JSON-encoded
 *     ``requestedCapabilities`` field to the multipart body.
 *   - When omitted, the field is absent (legacy fallback path).
 *   - The dialog's ``onConfirm`` callback receives the
 *     capabilities object alongside the selected profile.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";

import { ApiClient } from "@/lib/api/api-client";
import type { ProjectContext } from "@/types/ui";
import type { RequestedCapabilities } from "@/types/execution-profile";


// ---- ApiClient.upload sends the new field on the multipart -----


function _ctx(): ProjectContext {
  return { tenant: "acme", project: "alpha" };
}


function _client(): ApiClient {
  return new ApiClient({
    baseUrl: "http://api.test",
    getCtx: _ctx,
    getAuth: () => ({} as never),
  });
}


describe("ApiClient.upload — requestedCapabilities flow", () => {
  let fetchMock: Mock;

  beforeEach(() => {
    // Implementation rather than ``mockResolvedValue`` so each
    // call gets a fresh ``Response`` — bodies are single-use.
    fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(
        JSON.stringify({ data: { runId: "run-1" } }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    ));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("appends requestedCapabilities as a JSON string when provided", async () => {
    const client = _client();
    const file = new File(["hello"], "doc.txt", { type: "text/plain" });
    const requestedCapabilities: RequestedCapabilities = {
      imageProcessing: true,
      tableProcessing: false,
      equationProcessing: true,
    };
    await client.upload(
      file, _ctx(), "knowledge_index", null, requestedCapabilities,
    );
    const fd = fetchMock.mock.calls[0]![1]!.body as FormData;
    const raw = fd.get("requestedCapabilities");
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw as string);
    expect(parsed).toEqual({
      imageProcessing: true,
      tableProcessing: false,
      equationProcessing: true,
    });
  });

  it("omits requestedCapabilities when null (legacy callers)", async () => {
    const client = _client();
    const file = new File(["hello"], "doc.txt", { type: "text/plain" });
    await client.upload(
      file, _ctx(), "knowledge_index", null, null,
    );
    const fd = fetchMock.mock.calls[0]![1]!.body as FormData;
    expect(fd.has("requestedCapabilities")).toBe(false);
  });

  it("omits requestedCapabilities when argument absent", async () => {
    const client = _client();
    const file = new File(["hello"], "doc.txt", { type: "text/plain" });
    // Legacy 4-arg signature still works — no caller change required.
    await client.upload(file, _ctx(), "knowledge_index", null);
    const fd = fetchMock.mock.calls[0]![1]!.body as FormData;
    expect(fd.has("requestedCapabilities")).toBe(false);
  });

  it("each capability toggles independently in the payload", async () => {
    const client = _client();
    const file = new File(["hello"], "doc.txt", { type: "text/plain" });

    // All three OFF
    await client.upload(file, _ctx(), "knowledge_index", null, {
      imageProcessing: false,
      tableProcessing: false,
      equationProcessing: false,
    });
    let parsed = JSON.parse(
      (fetchMock.mock.calls[0]![1]!.body as FormData)
        .get("requestedCapabilities") as string,
    );
    expect(parsed.imageProcessing).toBe(false);
    expect(parsed.tableProcessing).toBe(false);
    expect(parsed.equationProcessing).toBe(false);

    // Image ON
    fetchMock.mockClear();
    await client.upload(file, _ctx(), "knowledge_index", null, {
      imageProcessing: true,
      tableProcessing: false,
      equationProcessing: false,
    });
    parsed = JSON.parse(
      (fetchMock.mock.calls[0]![1]!.body as FormData)
        .get("requestedCapabilities") as string,
    );
    expect(parsed.imageProcessing).toBe(true);
    expect(parsed.tableProcessing).toBe(false);
    expect(parsed.equationProcessing).toBe(false);

    // All three ON
    fetchMock.mockClear();
    await client.upload(file, _ctx(), "knowledge_index", null, {
      imageProcessing: true,
      tableProcessing: true,
      equationProcessing: true,
    });
    parsed = JSON.parse(
      (fetchMock.mock.calls[0]![1]!.body as FormData)
        .get("requestedCapabilities") as string,
    );
    expect(parsed).toEqual({
      imageProcessing: true,
      tableProcessing: true,
      equationProcessing: true,
    });
  });
});
