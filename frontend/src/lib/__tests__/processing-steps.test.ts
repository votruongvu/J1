/**
 * Tests for the user-facing step-name mapping. The mapping is the
 * single source of truth that drives Timeline labels, the
 * PrimaryStatusPanel "Running …" copy, and the Result tab
 * grouping. Pinning each internal-name → user-facing id keeps
 * backend renames from silently changing UI copy.
 */

import { describe, expect, it } from "vitest";
import {
  PROCESSING_STEPS,
  PROCESSING_STEP_IDS,
  internalStepToUserFacing,
  maxStatus,
  processingStepById,
  userFacingStepLabel,
} from "@/lib/processing-steps";

describe("processing-steps", () => {
  describe("PROCESSING_STEPS canonical list", () => {
    it("contains the six macro-phase steps in compile-first order", () => {
      // Post-split-mode pipeline: assess → compile (sealed) →
      // assess enrichment → enrich → graph → finalize. The earlier
      // split-mode-era IDs (parse_source_content,
      // build_content_inventory, generate_knowledge_chunks) all fold
      // onto the single "compile" step now.
      expect(PROCESSING_STEPS.map((s) => s.id)).toEqual([
        "assess_compile_strategy",
        "compile",
        "assess_enrichment",
        "enrich_extracted_content",
        "build_knowledge_graph",
        "finalize_ingestion",
      ]);
    });

    it("each step has a user-facing label and description", () => {
      for (const step of PROCESSING_STEPS) {
        expect(step.label).toMatch(/[A-Za-z]/);
        expect(step.description).toMatch(/[A-Za-z]/);
        expect(step.label).not.toMatch(/initial plan/i);
      }
    });

    it("vocabulary stays operator-friendly", () => {
      const labels = PROCESSING_STEPS.map((s) => s.label).join(" | ");
      expect(labels).not.toMatch(/initial plan/i);
      expect(labels).not.toMatch(/raganything/i);
      expect(labels).not.toMatch(/mineru/i);
    });

    it("no leftover split-mode step ids remain", () => {
      const ids = PROCESSING_STEP_IDS as readonly string[];
      expect(ids).not.toContain("parse_source_content");
      expect(ids).not.toContain("build_content_inventory");
      expect(ids).not.toContain("generate_knowledge_chunks");
    });
  });

  describe("internalStepToUserFacing()", () => {
    it("maps compile / parse / parser to the canonical compile id", () => {
      expect(internalStepToUserFacing("compile")).toBe("compile");
      expect(internalStepToUserFacing("parse")).toBe("compile");
      expect(internalStepToUserFacing("parser")).toBe("compile");
      expect(internalStepToUserFacing("raganything_compile")).toBe("compile");
    });

    it("folds legacy split-mode aliases onto compile", () => {
      // parse_source_content / build_content_inventory /
      // generate_knowledge_chunks were the user-facing ids in the
      // split-mode era. Historic runs may still carry them in the
      // audit log; they MUST project to the unified compile step
      // rather than fall through to the raw-capitalised fallback.
      expect(internalStepToUserFacing("parse_source_content")).toBe("compile");
      expect(internalStepToUserFacing("build_content_inventory")).toBe(
        "compile",
      );
      expect(internalStepToUserFacing("generate_knowledge_chunks")).toBe(
        "compile",
      );
      expect(internalStepToUserFacing("parsed_content_manifest")).toBe(
        "compile",
      );
      expect(internalStepToUserFacing("content_inventory")).toBe("compile");
      expect(internalStepToUserFacing("chunk")).toBe("compile");
      expect(internalStepToUserFacing("chunks")).toBe("compile");
      expect(internalStepToUserFacing("chunking")).toBe("compile");
    });

    it("maps profile / assessment events to assess_compile_strategy", () => {
      expect(internalStepToUserFacing("profile_document")).toBe(
        "assess_compile_strategy",
      );
      expect(internalStepToUserFacing("assessment")).toBe(
        "assess_compile_strategy",
      );
      expect(internalStepToUserFacing("ingestion.assessment.created")).toBe(
        "assess_compile_strategy",
      );
      expect(internalStepToUserFacing("assessment_plan")).toBe(
        "assess_compile_strategy",
      );
    });

    it("maps post-compile enrich-assessment events to assess_enrichment", () => {
      expect(internalStepToUserFacing("post_compile_assess")).toBe(
        "assess_enrichment",
      );
      expect(internalStepToUserFacing("enrich_assessment")).toBe(
        "assess_enrichment",
      );
      expect(
        internalStepToUserFacing("ingestion.post_compile.enrich_assessment"),
      ).toBe("assess_enrichment");
      expect(internalStepToUserFacing("post_compile_enrich_plan")).toBe(
        "assess_enrichment",
      );
    });

    it("legacy plan.* events no longer map (workflow does not emit them)", () => {
      expect(internalStepToUserFacing("plan")).toBeNull();
      expect(internalStepToUserFacing("planning")).toBeNull();
      expect(internalStepToUserFacing("plan.revised")).toBeNull();
      expect(internalStepToUserFacing("initial_plan")).toBeNull();
    });

    it("maps enrich variants to enrich_extracted_content", () => {
      expect(internalStepToUserFacing("enrich")).toBe(
        "enrich_extracted_content",
      );
      expect(internalStepToUserFacing("enrichment")).toBe(
        "enrich_extracted_content",
      );
      expect(internalStepToUserFacing("enrich_stage")).toBe(
        "enrich_extracted_content",
      );
      expect(internalStepToUserFacing("multimodal_enrich")).toBe(
        "enrich_extracted_content",
      );
    });

    it("maps graph variants to build_knowledge_graph", () => {
      expect(internalStepToUserFacing("graph")).toBe("build_knowledge_graph");
      expect(internalStepToUserFacing("graph_build")).toBe(
        "build_knowledge_graph",
      );
      expect(internalStepToUserFacing("knowledge_graph")).toBe(
        "build_knowledge_graph",
      );
    });

    it("maps finalize variants to finalize_ingestion", () => {
      expect(internalStepToUserFacing("finalize")).toBe("finalize_ingestion");
      expect(internalStepToUserFacing("complete")).toBe("finalize_ingestion");
      expect(internalStepToUserFacing("index")).toBe("finalize_ingestion");
      expect(internalStepToUserFacing("run.completed")).toBe(
        "finalize_ingestion",
      );
    });

    it("returns null for unrecognised strings (no silent mislabeling)", () => {
      expect(internalStepToUserFacing("totally_made_up")).toBeNull();
      expect(internalStepToUserFacing("")).toBeNull();
      expect(internalStepToUserFacing(null)).toBeNull();
      expect(internalStepToUserFacing(undefined)).toBeNull();
    });

    it("is case-insensitive", () => {
      expect(internalStepToUserFacing("COMPILE")).toBe("compile");
      expect(internalStepToUserFacing("Enrich")).toBe(
        "enrich_extracted_content",
      );
    });

    it("each canonical id passes through as itself", () => {
      for (const id of PROCESSING_STEP_IDS) {
        expect(internalStepToUserFacing(id)).toBe(id);
      }
    });
  });

  describe("userFacingStepLabel()", () => {
    it("returns the canonical label for known steps", () => {
      expect(userFacingStepLabel("compile")).toBe("Compile");
      expect(userFacingStepLabel("profile_document")).toBe(
        "Assess Compile Strategy",
      );
      expect(userFacingStepLabel("post_compile_assess")).toBe(
        "Assess Enrichment",
      );
      expect(userFacingStepLabel("graph")).toBe("Build Knowledge Graph");
      // Legacy split-mode aliases produce "Compile" too.
      expect(userFacingStepLabel("parse_source_content")).toBe("Compile");
      expect(userFacingStepLabel("generate_knowledge_chunks")).toBe("Compile");
    });

    it("preserves the raw string for unrecognised names (capitalised)", () => {
      expect(userFacingStepLabel("custom_step")).toBe("Custom_step");
    });

    it("returns em dash for empty/undefined", () => {
      expect(userFacingStepLabel(null)).toBe("—");
      expect(userFacingStepLabel("")).toBe("—");
    });
  });

  describe("processingStepById()", () => {
    it("returns the step definition for every canonical id", () => {
      for (const id of PROCESSING_STEP_IDS) {
        const step = processingStepById(id);
        expect(step.id).toBe(id);
        expect(step.label).toBeTruthy();
      }
    });
  });

  describe("maxStatus()", () => {
    it("failed wins over completed", () => {
      expect(maxStatus("completed", "failed")).toBe("failed");
      expect(maxStatus("failed", "completed")).toBe("failed");
    });

    it("completed wins over running/pending", () => {
      expect(maxStatus("running", "completed")).toBe("completed");
      expect(maxStatus("pending", "completed")).toBe("completed");
    });

    it("running wins over pending", () => {
      expect(maxStatus("pending", "running")).toBe("running");
    });
  });
});
