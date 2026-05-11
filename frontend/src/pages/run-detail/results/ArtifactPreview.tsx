/**
 * Single component that fetches an artifact's content and renders it
 * inline when safe (image / JSON / text). Falls back to a download
 * button for everything else.
 *
 * Owns the object-URL lifetime — any URL it creates for image blobs
 * is revoked on unmount or on artifactId change.
 */

import { useEffect, useState } from "react";
import { JsonView } from "@/components/JsonView";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ReviewArtifactContent,
  ReviewArtifactRecord,
} from "@/types/review";
import {
  type ArtifactRenderMode,
  downloadBlob,
  pickRenderMode,
} from "./artifact-helpers";

interface ArtifactPreviewProps {
  runId: string;
  record: ReviewArtifactRecord;
  /** When true, suppresses the surrounding chrome (used by AssetsTab
 * cards). The default layout is taller and includes a download
 * button — appropriate for the Raw Artifacts viewer. */
  compact?: boolean;
}

interface PreviewState {
  content: ReviewArtifactContent;
  mode: ArtifactRenderMode;
  /** Object URL for image previews — revoked on cleanup. */
  imageUrl: string | null;
  /** Decoded text body for JSON / text modes. */
  text: string | null;
  /** Parsed JSON (when mode=json AND parse succeeded). */
  parsedJson: unknown | undefined;
}

export function ArtifactPreview({
  runId,
  record,
  compact = false,
}: ArtifactPreviewProps) {
  const client = useClient();
  const [state, setState] = useState<PreviewState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let createdImageUrl: string | null = null;
    setLoading(true);
    setError(null);
    setState(null);

    void (async () => {
      try {
        const content = await client.getRunArtifactContent(
          runId, record.artifactId,
        );
        if (cancelled) return;
        const mode = pickRenderMode({
          kind: record.kind,
          contentType: content.contentType,
          location: record.location,
        });
        let imageUrl: string | null = null;
        let text: string | null = null;
        let parsedJson: unknown | undefined;
        if (mode === "image") {
          imageUrl = URL.createObjectURL(content.blob);
          createdImageUrl = imageUrl;
        } else if (mode === "json") {
          text = await content.blob.text();
          try {
            parsedJson = JSON.parse(text);
          } catch {
            // Producer claimed JSON but content didn't parse — fall
            // back to text view rather than crash.
            parsedJson = undefined;
          }
        } else if (mode === "text") {
          text = await content.blob.text();
        }
        if (cancelled) {
          if (imageUrl) URL.revokeObjectURL(imageUrl);
          return;
        }
        setState({ content, mode, imageUrl, text, parsedJson });
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      // The state's imageUrl is the same as `createdImageUrl`; revoke
      // when the effect tears down.
      if (createdImageUrl) URL.revokeObjectURL(createdImageUrl);
    };
  }, [client, runId, record.artifactId, record.kind, record.location]);

  if (error) {
    return (
      <div className="artifact-preview__error" role="alert">
        Couldn&apos;t load artifact: {error}
      </div>
    );
  }
  if (loading || !state) {
    return (
      <div className="artifact-preview__loading" aria-busy="true">
        Loading…
      </div>
    );
  }

  const { content, mode, imageUrl, text, parsedJson } = state;
  const downloadName =
    content.filename ??
    record.location.split("/").pop() ??
    record.artifactId;

  return (
    <div
      className={`artifact-preview ${compact ? "artifact-preview--compact" : ""}`}
    >
      {mode === "image" && imageUrl ? (
        <img
          src={imageUrl}
          alt={record.artifactId}
          className="artifact-preview__image"
        />
      ) : mode === "json" && parsedJson !== undefined ? (
        <div className="artifact-preview__json">
          <JsonView value={parsedJson} />
        </div>
      ) : mode === "text" || (mode === "json" && text != null) ? (
        <pre className="artifact-preview__text">{text ?? ""}</pre>
      ) : (
        <div className="artifact-preview__binary">
          <div className="artifact-preview__binary-label">
            Binary content — preview not available.
          </div>
        </div>
      )}

      {!compact ? (
        <div className="artifact-preview__actions">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => downloadBlob(content.blob, downloadName)}
          >
            Download
          </button>
          <span className="artifact-preview__contenttype">
            {content.contentType}
          </span>
        </div>
      ) : null}
    </div>
  );
}
