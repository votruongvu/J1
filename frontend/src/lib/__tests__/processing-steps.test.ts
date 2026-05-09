/**
 * Tests for the user-facing step-name mapping. The mapping is the
 * single source of truth that drives Timeline labels, the
 * PrimaryStatusPanel "Running …" copy, the ProcessingStepper, and
 * the Result tab grouping. Pinning each internal-name → user-facing
 * id keeps backend renames from silently changing UI copy.
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
    it("contains the seven user-facing steps in spec order", () => {
      expect(PROCESSING_STEPS.map((s) => s.id)).toEqual([
        "parse_source_content",
        "build_content_inventory",
        "create_execution_plan",
        "generate_knowledge_chunks",
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

    it("'Initial Plan' is NOT in the user-facing vocabulary", () => {
      const labels = PROCESSING_STEPS.map((s) => s.label).join(" | ");
      expect(labels).not.toMatch(/initial plan/i);
      expect(labels).not.toMatch(/raganything/i);
      expect(labels).not.toMatch(/mineru/i);
    });
  });

  describe("internalStepToUserFacing()", () => {
    it("maps compile / parse / parser to parse_source_content", () => {
      expect(internalStepToUserFacing("compile")).toBe("parse_source_content");
      expect(internalStepToUserFacing("parse")).toBe("parse_source_content");
      expect(internalStepToUserFacing("parser")).toBe("parse_source_content");
      expect(internalStepToUserFacing("raganything_compile")).toBe(
        "parse_source_content",
      );
    });

    it("maps parsed-content/content list to build_content_inventory", () => {
      expect(internalStepToUserFacing("parsed_content_manifest")).toBe(
        "build_content_inventory",
      );
      expect(internalStepToUserFacing("content_inventory")).toBe(
        "build_content_inventory",
      );
    });

    it("maps planning events to create_execution_plan", () => {
      expect(internalStepToUserFacing("plan")).toBe("create_execution_plan");
      expect(internalStepToUserFacing("planning")).toBe("create_execution_plan");
      expect(internalStepToUserFacing("plan.revised")).toBe(
        "create_execution_plan",
      );
      expect(internalStepToUserFacing("initial_plan")).toBe(
        "create_execution_plan",
      );
    });

    it("maps chunks to generate_knowledge_chunks", () => {
      expect(internalStepToUserFacing("chunk")).toBe(
        "generate_knowledge_chunks",
      );
      expect(internalStepToUserFacing("chunking")).toBe(
        "generate_knowledge_chunks",
      );
      expect(internalStepToUserFacing("chunk_task")).toBe(
        "generate_knowledge_chunks",
      );
    });

    it("maps enrich variants to enrich_extracted_content", () => {
      expect(internalStepToUserFacing("enrich")).toBe(
        "enrich_extracted_content",
      );
      expect(internalStepToUserFacing("enrichment")).toBe(
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
      expect(internalStepToUserFacing("COMPILE")).toBe("parse_source_content");
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
      expect(userFacingStepLabel("compile")).toBe("Parse Source Content");
      expect(userFacingStepLabel("plan.revised")).toBe("Create Execution Plan");
      expect(userFacingStepLabel("graph")).toBe("Build Knowledge Graph");
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
