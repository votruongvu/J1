/**
 * Pixel Ingestion Icons — public surface.
 *
 * Most callers should reach for `<IngestionStepIcon />` — it
 * handles the step → icon mapping and the status → frame-state
 * mapping in one prop pair. The individual icon components are
 * exported for cases where the step is fixed at compile time
 * (e.g. a "Parsing..." loading panel).
 */

export {
  IngestionStepIcon,
  INGESTION_STEP_ICONS,
  type StepStatus,
} from "./IngestionStepIcon";
export {
  ChunkIcon,
  EnrichIcon,
  FinalizeIcon,
  GraphIcon,
  InventoryIcon,
  ParseIcon,
  PlanIcon,
  type IconState,
  type IconSize,
  type IngestionIconProps,
} from "./icons";
