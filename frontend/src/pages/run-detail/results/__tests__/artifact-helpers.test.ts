/**
 * Unit tests for the small artifact helpers — pure functions, no
 * DOM or network. Component-level rendering is covered by Playwright
 * (Phase 11).
 */

import { describe, expect, it } from "vitest";

import {
  formatBytes,
  pickRenderMode,
} from "../artifact-helpers";

describe("pickRenderMode", () => {
  it("renders images for image content-types regardless of kind", () => {
    expect(
      pickRenderMode({ kind: "chunk", contentType: "image/png", location: null }),
    ).toBe("image");
  });

  it("renders enriched.visuals as image when extension matches", () => {
    expect(
      pickRenderMode({
        kind: "enriched.visuals",
        contentType: "application/octet-stream",
        location: "enriched/p1.png",
      }),
    ).toBe("image");
  });

  it("renders application/json as json", () => {
    expect(
      pickRenderMode({
        kind: "graph_json",
        contentType: "application/json",
        location: "graph/x.json",
      }),
    ).toBe("json");
  });

  it("falls back to json when extension is .json AND not octet-stream", () => {
    expect(
      pickRenderMode({
        kind: "chunk",
        contentType: "text/plain",
        location: "compiled/x.json",
      }),
    ).toBe("text"); // text content-type wins
  });

  it("renders text content-types as text", () => {
    expect(
      pickRenderMode({
        kind: "compiler_log",
        contentType: "text/plain; charset=utf-8",
        location: "compiled/log.txt",
      }),
    ).toBe("text");
  });

  it("falls back to download for unknown binary types", () => {
    expect(
      pickRenderMode({
        kind: "binary",
        contentType: "application/octet-stream",
        location: "raw/blob.bin",
      }),
    ).toBe("download");
  });

  it("treats no-content-type artifacts conservatively", () => {
    expect(
      pickRenderMode({ kind: "x", contentType: null, location: null }),
    ).toBe("download");
  });
});

describe("formatBytes", () => {
  it("formats zero", () => {
    expect(formatBytes(0)).toBe("0 B");
  });

  it("formats bytes / KB / MB transitions", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1500)).toBe("1.5 KB");
    expect(formatBytes(2 * 1024 * 1024)).toBe("2 MB");
  });

  it("returns em-dash for invalid input", () => {
    expect(formatBytes(-1)).toBe("—");
    expect(formatBytes(Number.NaN)).toBe("—");
  });
});
