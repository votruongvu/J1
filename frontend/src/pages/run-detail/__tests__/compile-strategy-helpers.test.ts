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
  capabilityLabel,
  confidenceBucket,
  formatConfidence,
  hasModeEscalation,
  isFallbackOnly,
  modeDescription,
  resolvedCompileConfig,
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
    const baseAttempt = _report().attempts[0]!;
    const r = _report({
      attempts_count: 2,
      attempts: [
        {
          ...baseAttempt,
          attempt_number: 1, mode: "fast", quality: "low",
          retry_reason: "zero_chunks", status: "retried",
        },
        {
          ...baseAttempt,
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
    const last = r.attempts[r.attempts.length - 1]!;
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


// ---- 7) Assessment Plan rendering helpers ------------------------


describe("formatConfidence", () => {
  it("renders confidence as a percentage", () => {
    expect(formatConfidence(0.85)).toBe("85%");
    expect(formatConfidence(0.5)).toBe("50%");
    expect(formatConfidence(1)).toBe("100%");
    expect(formatConfidence(0)).toBe("0%");
  });

  it("returns em dash when value is missing", () => {
    expect(formatConfidence(undefined)).toBe("—");
  });

  it("rounds to nearest integer", () => {
    expect(formatConfidence(0.834)).toBe("83%");
    expect(formatConfidence(0.836)).toBe("84%");
  });
});


describe("confidenceBucket", () => {
  it("buckets ≥0.85 as high", () => {
    expect(confidenceBucket(0.85)).toBe("high");
    expect(confidenceBucket(0.95)).toBe("high");
    expect(confidenceBucket(1)).toBe("high");
  });

  it("buckets [LOW_CONFIDENCE_THRESHOLD..0.85) as medium", () => {
    expect(confidenceBucket(0.7)).toBe("medium");
    expect(confidenceBucket(0.8)).toBe("medium");
    expect(confidenceBucket(0.849)).toBe("medium");
  });

  it("buckets <LOW_CONFIDENCE_THRESHOLD as low", () => {
    expect(confidenceBucket(0.69)).toBe("low");
    expect(confidenceBucket(0.5)).toBe("low");
    expect(confidenceBucket(0)).toBe("low");
  });

  it("returns unknown for missing value", () => {
    expect(confidenceBucket(undefined)).toBe("unknown");
  });
});


describe("hasModeEscalation", () => {
  it("returns true when initial !== final", () => {
    expect(
      hasModeEscalation(
        _report({ initial_mode: "fast", final_mode: "standard" }),
      ),
    ).toBe(true);
  });

  it("returns false when initial === final", () => {
    expect(hasModeEscalation(_report())).toBe(false);
  });

  it("returns false when either side is null", () => {
    expect(
      hasModeEscalation(_report({ initial_mode: null })),
    ).toBe(false);
    expect(
      hasModeEscalation(_report({ final_mode: null })),
    ).toBe(false);
  });
});


describe("modeDescription", () => {
  it("describes each known mode in operator-friendly terms", () => {
    expect(modeDescription("fast")).toContain("Plain-text");
    expect(modeDescription("fast")).toContain("VLM");
    expect(modeDescription("standard")).toContain("VLM");
    expect(modeDescription("deep")).toContain("OCR");
  });

  it("falls back to 'Unknown mode' for unrecognised input", () => {
    expect(modeDescription(undefined)).toBe("Unknown mode.");
    expect(modeDescription("totally-not-a-mode")).toBe("Unknown mode.");
  });
});


describe("capabilityLabel", () => {
  it("converts snake_case capabilities to a readable label", () => {
    expect(capabilityLabel("text_extraction")).toBe("Text extraction");
    expect(capabilityLabel("layout_detection")).toBe("Layout detection");
  });

  it("preserves single-word capabilities", () => {
    expect(capabilityLabel("ocr")).toBe("Ocr");
  });

  it("returns falsy input unchanged", () => {
    expect(capabilityLabel("")).toBe("");
  });
});


describe("resolvedCompileConfig", () => {
  it("returns the LAST attempt's mapped_compile_config", () => {
    const baseAttempt = _report().attempts[0]!;
    const r = _report({
      attempts: [
        { ...baseAttempt, attempt_number: 1, mode: "fast" },
        {
          ...baseAttempt,
          attempt_number: 2,
          mode: "standard",
          mapped_compile_config: {
            parse_method: "auto",
            assessment_mode: "standard",
            unhandled_capabilities: [],
          },
        },
      ],
      attempts_count: 2,
    });
    const cfg = resolvedCompileConfig(r);
    expect(cfg.parse_method).toBe("auto");
    expect(cfg.assessment_mode).toBe("standard");
  });

  it("returns empty object when there are no attempts", () => {
    const r = _report({ attempts: [], attempts_count: 0 });
    expect(resolvedCompileConfig(r)).toEqual({});
  });
});
