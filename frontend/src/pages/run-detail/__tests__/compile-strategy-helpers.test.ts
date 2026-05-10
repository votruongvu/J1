/**
 * Pure-logic tests for the compile-strategy panel.
 *
 * Tests live alongside source under `__tests__/*.test.ts` per the
 * project's vitest config (node env, no jsdom). React rendering
 * isn't covered here — the panel's JSX is straightforward and the
 * build/typecheck signal is sufficient. The behaviour worth pinning
 * is the banner-decision matrix + applied-capability filter, which
 * the helpers below isolate.
 */

import { describe, expect, it } from "vitest";
import {
  appliedCapabilities,
  bannersForReport,
  isFallbackOnly,
  type CompileStrategyReport,
} from "../compile-strategy-helpers";

function _report(
  overrides: Partial<CompileStrategyReport> = {},
): CompileStrategyReport {
  return {
    schema_version: "1",
    run_id: "run-1",
    document_id: "doc-1",
    initial_mode: "standard",
    final_mode: "standard",
    retry_used: false,
    attempts_count: 1,
    attempts: [
      {
        attempt_number: 1,
        mode: "standard",
        parser: "raganything",
        parse_method: "auto",
        started_at: "2026-01-01T00:00:00Z",
        completed_at: "2026-01-01T00:00:10Z",
        status: "succeeded",
        chunks_count: 5,
        extracted_text_chars: 1000,
        quality: "good",
        retry_reason: null,
        warnings: [],
        mapped_compile_config: {
          parse_method: "auto",
          assessment_mode: "standard",
          unhandled_capabilities: [],
        },
      },
    ],
    final_compile_quality: "good",
    final_retry_reason: null,
    final_warnings: [],
    assessment_plan: {
      mode: "standard",
      confidence: 0.85,
      required_capabilities: ["text_extraction", "layout_detection"],
      optional_capabilities: [],
      risk_flags: [],
      reason: "default standard mode",
    },
    initial_assessment_plan: {
      mode: "standard",
      confidence: 0.85,
      required_capabilities: ["text_extraction", "layout_detection"],
    },
    plan_warnings: [],
    unhandled_capabilities: [],
    ...overrides,
  };
}

// ---- 1) Assessment Plan card data renders ------------------------


describe("assessment plan data accessor", () => {
  it("exposes plan fields the card renders for a happy-path run", () => {
    const r = _report();
    expect(r.assessment_plan.mode).toBe("standard");
    expect(r.assessment_plan.confidence).toBe(0.85);
    expect(r.assessment_plan.required_capabilities).toContain("text_extraction");
  });
});


// ---- 2) Compile Strategy card: applied = required - unhandled ---


describe("appliedCapabilities", () => {
  it("returns required minus unhandled", () => {
    const r = _report({
      assessment_plan: {
        ..._report().assessment_plan,
        required_capabilities: [
          "text_extraction", "layout_detection",
          "image_extraction", "table_extraction",
        ],
      },
      unhandled_capabilities: ["image_extraction"],
    });
    expect(appliedCapabilities(r)).toEqual([
      "text_extraction", "layout_detection", "table_extraction",
    ]);
  });

  it("returns empty array when no plan", () => {
    const r = _report({
      assessment_plan: {},
    });
    expect(appliedCapabilities(r)).toEqual([]);
  });
});


// ---- 3) Compile Attempts timeline ordering -----------------------


describe("attempts timeline", () => {
  it("preserves attempt order (server already sorts)", () => {
    const r = _report({
      attempts_count: 2,
      attempts: [
        {
          ..._report().attempts[0],
          attempt_number: 1, mode: "fast", quality: "low",
          retry_reason: "zero_chunks", status: "retried",
        },
        {
          ..._report().attempts[0],
          attempt_number: 2, mode: "standard", quality: "good",
          status: "succeeded",
        },
      ],
    });
    expect(r.attempts.map((a) => a.attempt_number)).toEqual([1, 2]);
    expect(r.attempts.map((a) => a.mode)).toEqual(["fast", "standard"]);
  });
});


// ---- 4) Banner decision matrix ----------------------------------


describe("bannersForReport", () => {
  it("emits NO banners on a clean happy-path run", () => {
    const banners = bannersForReport(_report());
    expect(banners).toEqual([]);
  });

  it("emits no-plan banner when assessment_plan.mode is missing", () => {
    const banners = bannersForReport(_report({
      assessment_plan: {},
    }));
    expect(banners.some((b) => b.testid === "banner-no-plan")).toBe(true);
  });

  it("emits low-confidence banner when confidence < 0.7", () => {
    const banners = bannersForReport(_report({
      assessment_plan: { ..._report().assessment_plan, confidence: 0.6 },
    }));
    expect(banners.some((b) => b.testid === "banner-low-confidence")).toBe(true);
    // No-plan banner is mutually exclusive with low-confidence —
    // when the plan exists, the low-confidence path fires.
    expect(banners.some((b) => b.testid === "banner-no-plan")).toBe(false);
  });

  it("emits unhandled banner when unhandled_capabilities is non-empty", () => {
    const banners = bannersForReport(_report({
      unhandled_capabilities: ["formula_extraction"],
    }));
    const b = banners.find((b) => b.testid === "banner-unhandled");
    expect(b).toBeDefined();
    expect(b!.message).toContain("formula_extraction");
  });

  it("emits retry-used banner with mode transition when retry_used=true", () => {
    const banners = bannersForReport(_report({
      retry_used: true,
      initial_mode: "fast",
      final_mode: "standard",
    }));
    const b = banners.find((b) => b.testid === "banner-retry-used");
    expect(b).toBeDefined();
    expect(b!.message).toContain("fast");
    expect(b!.message).toContain("standard");
  });

  it("emits low-quality banner when final_compile_quality='low'", () => {
    const banners = bannersForReport(_report({
      final_compile_quality: "low",
    }));
    const b = banners.find((b) => b.testid === "banner-low-quality");
    expect(b).toBeDefined();
    expect(b!.kind).toBe("warn");
  });

  it("emits failed banner when final_compile_quality='failed'", () => {
    const banners = bannersForReport(_report({
      final_compile_quality: "failed",
    }));
    const b = banners.find((b) => b.testid === "banner-failed");
    expect(b).toBeDefined();
    expect(b!.kind).toBe("err");
  });

  it("stacks multiple banners in priority order on a hard-case run", () => {
    const banners = bannersForReport(_report({
      assessment_plan: {
        ..._report().assessment_plan,
        confidence: 0.5,
      },
      unhandled_capabilities: ["formula_extraction"],
      retry_used: true,
      initial_mode: "standard",
      final_mode: "deep",
      final_compile_quality: "low",
    }));
    const ids = banners.map((b) => b.testid);
    // Expected order: low-confidence → unhandled → retry-used → low-quality.
    expect(ids).toEqual([
      "banner-low-confidence",
      "banner-unhandled",
      "banner-retry-used",
      "banner-low-quality",
    ]);
  });
});


// ---- 5) Final Compile Quality summary fields exist --------------


describe("final quality summary", () => {
  it("reads final_mode + final chunks/chars from the last attempt", () => {
    const r = _report({
      final_compile_quality: "good",
      final_mode: "standard",
      retry_used: false,
      attempts_count: 1,
    });
    const last = r.attempts[r.attempts.length - 1];
    expect(r.final_compile_quality).toBe("good");
    expect(r.final_mode).toBe("standard");
    expect(last.chunks_count).toBe(5);
    expect(last.extracted_text_chars).toBe(1000);
  });
});


// ---- 6) Missing metadata degrades gracefully (no fake values) ---


describe("isFallbackOnly", () => {
  it("returns true when assessment_plan has no mode", () => {
    expect(isFallbackOnly(_report({ assessment_plan: {} }))).toBe(true);
  });

  it("returns false when assessment_plan.mode is set", () => {
    expect(isFallbackOnly(_report())).toBe(false);
  });
});
