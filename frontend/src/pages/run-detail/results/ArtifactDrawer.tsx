/**
 * Artifact content viewer drawer (Raw Artifacts tab).
 *
 * Reuses the standard `.drawer` shell. Header shows artifact id +
 * kind; body delegates to `ArtifactPreview`, which decides inline
 * preview vs. download.
 */

import { Icon } from "@/components/icons";
import type { ReviewArtifactRecord } from "@/types/review";
import { ArtifactPreview } from "./ArtifactPreview";
import { formatBytes } from "./artifact-helpers";

interface ArtifactDrawerProps {
  runId: string;
  record: ReviewArtifactRecord | null;
  onClose: () => void;
}

export function ArtifactDrawer({
  runId,
  record,
  onClose,
}: ArtifactDrawerProps) {
  const open = record != null;
  return (
    <div
      className={`drawer ${open ? "is-open" : ""}`}
      role="dialog"
      aria-hidden={!open}
      aria-label={record ? `Artifact ${record.artifactId}` : "Artifact viewer"}
    >
      <div className="drawer__head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Icon.Code className="icon" />
          <strong>{record?.artifactId ?? "Artifact"}</strong>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onClose}
          aria-label="Close drawer"
        >
          <Icon.X className="icon-sm" />
        </button>
      </div>
      {record ? (
        <div className="drawer__byline">
          <span>{record.kind}</span>
          <span> · </span>
          <span>{formatBytes(record.byteSize)}</span>
          <span> · </span>
          <code>{record.location}</code>
        </div>
      ) : null}
      <div className="drawer__body">
        {record ? (
          <ArtifactPreview runId={runId} record={record} />
        ) : (
          <div className="results__empty">
            Click &quot;View&quot; on any artifact to inspect its content.
          </div>
        )}
      </div>
    </div>
  );
}
