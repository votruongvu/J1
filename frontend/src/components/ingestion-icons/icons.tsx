/**
 * Pixel Ingestion Icons — TypeScript port.
 *
 * Each icon is a 32×32 SVG of unit-rect "pixels" wrapped in an
 * `IconFrame` (a rounded retro-CRT screen). Animations are CSS-
 * keyframe driven (see `./icons.css`); this file only renders the
 * static markup + the per-state badge overlay.
 *
 * Source: `j1-step-animation-icon/project/icons.jsx` from the
 * Claude Design hand-off bundle. Markup is pixel-identical so the
 * animations land in the right cells; only the host wrapper +
 * TypeScript typing differs.
 */

import { type CSSProperties, type ReactNode } from "react";
import "./icons.css";

export type IconState =
  | "running"
  | "completed"
  | "pending"
  | "skipped"
  | "failed";

export type IconSize = "xs" | "sm" | "md" | "lg" | number;

export interface IngestionIconProps {
  state?: IconState;
  size?: IconSize;
  /** Render without the rounded "screen" frame — used inside chips. */
  flat?: boolean;
  /** Accessible label. Defaults to the icon's intent. */
  ariaLabel?: string;
  className?: string;
  style?: CSSProperties;
}

interface IconFrameProps extends IngestionIconProps {
  children: ReactNode;
  /** CSS class of the inner SVG, e.g. `pix-ic-parse`. */
  svgClass: string;
  /** Static decorative SVG content (the pixel-art rects). */
}

function IconFrame({
  state = "running",
  size = "md",
  flat = false,
  ariaLabel,
  className,
  style,
  children,
}: IconFrameProps) {
  const sizeClass =
    typeof size === "number" ? "" : `size-${size}`;
  const inlineStyle: CSSProperties =
    typeof size === "number"
      ? { ...style, ["--pix-size" as string]: `${size}px` }
      : style ?? {};
  const cls = [
    "pix-icon-frame",
    sizeClass,
    `state-${state}`,
    flat ? "flat" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div
      className={cls}
      style={inlineStyle}
      role="img"
      aria-label={ariaLabel ?? `Step state: ${state}`}
    >
      {children}
      {state === "completed" && <CheckBadge />}
      {state === "failed" && <WarnBadge />}
    </div>
  );
}

function CheckBadge() {
  return (
    <div className="pix-state-badge" aria-hidden="true">
      <svg viewBox="0 0 12 12">
        <rect x="2" y="6" width="1" height="1" fill="#fff" />
        <rect x="3" y="7" width="1" height="1" fill="#fff" />
        <rect x="4" y="8" width="1" height="1" fill="#fff" />
        <rect x="5" y="7" width="1" height="1" fill="#fff" />
        <rect x="6" y="6" width="1" height="1" fill="#fff" />
        <rect x="7" y="5" width="1" height="1" fill="#fff" />
        <rect x="8" y="4" width="1" height="1" fill="#fff" />
        <rect x="9" y="3" width="1" height="1" fill="#fff" />
      </svg>
    </div>
  );
}

function WarnBadge() {
  return (
    <div className="pix-state-badge warn" aria-hidden="true">
      <svg viewBox="0 0 12 12">
        <rect x="5" y="3" width="2" height="4" fill="#fff" />
        <rect x="5" y="8" width="2" height="2" fill="#fff" />
      </svg>
    </div>
  );
}

// shorthand colors used inside the icons (mirrors the design's `C`).
const C = {
  ink2: "#3a3f55",
  paper: "#fff8e8",
  papsh: "#d9c79b",
  muted: "#b6ad95",
  mint: "#9ee7c1",
  mintD: "#4fae84",
  sky: "#9cc8f5",
  skyD: "#4f86c2",
  lav: "#c5b6f4",
  lavD: "#7e6ed1",
  peach: "#ffc89e",
  peachD: "#d18a55",
  sun: "#ffe18a",
  sunD: "#c79a30",
  ok: "#2f7a4a",
};

// ── 1. ParseIcon ────────────────────────────────
export function ParseIcon(props: IngestionIconProps) {
  return (
    <IconFrame {...props} svgClass="pix-ic-parse" ariaLabel={props.ariaLabel ?? "Compile"}>
      <svg viewBox="0 0 32 32" className="pix-ic-parse">
        <rect x="9" y="6" width="14" height="22" fill={C.papsh} />
        <rect x="8" y="5" width="14" height="22" fill={C.paper} />
        <rect x="8" y="4" width="14" height="1" fill={C.ink2} />
        <rect x="7" y="5" width="1" height="23" fill={C.ink2} />
        <rect x="22" y="5" width="1" height="23" fill={C.ink2} />
        <rect x="8" y="28" width="14" height="1" fill={C.ink2} />
        <rect x="20" y="5" width="2" height="1" fill={C.papsh} />
        <rect x="20" y="6" width="1" height="1" fill={C.papsh} />
        <rect x="20" y="4" width="1" height="1" fill={C.ink2} />
        <rect x="21" y="5" width="1" height="1" fill={C.ink2} />
        <rect className="ln-1" x="10" y="9" width="9" height="1" fill={C.lavD} />
        <rect className="ln-2" x="10" y="12" width="11" height="1" fill={C.lavD} />
        <rect className="ln-3" x="10" y="15" width="7" height="1" fill={C.lavD} />
        <rect className="ln-4" x="10" y="18" width="10" height="1" fill={C.lavD} />
        <rect className="ln-1" x="10" y="21" width="6" height="1" fill={C.lavD} />
        <g className="scan">
          <rect x="8" y="8" width="14" height="1" fill={C.sun} />
          <rect x="8" y="9" width="14" height="1" fill={C.sunD} opacity="0.45" />
        </g>
      </svg>
    </IconFrame>
  );
}

// ── 2. InventoryIcon ────────────────────────────
export function InventoryIcon(props: IngestionIconProps) {
  return (
    <IconFrame {...props} svgClass="pix-ic-inv" ariaLabel={props.ariaLabel ?? "Build Content Inventory"}>
      <svg viewBox="0 0 32 32" className="pix-ic-inv">
        <rect x="5" y="9" width="23" height="20" fill={C.papsh} />
        <rect x="5" y="8" width="22" height="20" fill={C.paper} />
        <rect x="4" y="7" width="24" height="1" fill={C.ink2} />
        <rect x="4" y="28" width="24" height="1" fill={C.ink2} />
        <rect x="4" y="8" width="1" height="20" fill={C.ink2} />
        <rect x="27" y="8" width="1" height="20" fill={C.ink2} />
        <g className="item-1">
          <rect x="7" y="11" width="18" height="4" fill={C.sky} />
          <rect x="7" y="14" width="18" height="1" fill={C.skyD} />
          <rect x="9" y="12" width="3" height="2" fill={C.paper} opacity="0.75" />
          <rect x="13" y="12" width="9" height="1" fill={C.paper} opacity="0.55" />
        </g>
        <g className="item-2">
          <rect x="7" y="17" width="18" height="4" fill={C.peach} />
          <rect x="7" y="20" width="18" height="1" fill={C.peachD} />
          <rect x="9" y="18" width="3" height="2" fill={C.paper} opacity="0.75" />
          <rect x="13" y="18" width="8" height="1" fill={C.paper} opacity="0.55" />
        </g>
        <g className="item-3">
          <rect x="7" y="23" width="18" height="4" fill={C.mint} />
          <rect x="7" y="26" width="18" height="1" fill={C.mintD} />
          <rect x="9" y="24" width="3" height="2" fill={C.paper} opacity="0.75" />
          <rect x="13" y="24" width="10" height="1" fill={C.paper} opacity="0.55" />
        </g>
      </svg>
    </IconFrame>
  );
}

// ── 3. PlanIcon ─────────────────────────────────
export function PlanIcon(props: IngestionIconProps) {
  const Row = ({
    y,
    lineW,
    descW,
    rowCls,
    checkCls,
  }: {
    y: number;
    lineW: number;
    descW: number;
    rowCls: string;
    checkCls: string;
  }) => (
    <g transform={`translate(0,${y})`}>
      <g className={rowCls}>
        <rect x="8" y="10" width="4" height="4" fill={C.ink2} />
        <rect x="9" y="11" width="2" height="2" fill={C.paper} />
        <rect x="14" y="10" width={lineW} height="1" fill={C.ink2} />
        <rect x="14" y="12" width={descW} height="1" fill={C.muted} />
      </g>
      <g className={checkCls}>
        <rect x="9" y="12" width="1" height="1" fill={C.ok} />
        <rect x="10" y="11" width="1" height="1" fill={C.ok} />
      </g>
    </g>
  );
  return (
    <IconFrame {...props} svgClass="pix-ic-plan" ariaLabel={props.ariaLabel ?? "Create Execution Plan"}>
      <svg viewBox="0 0 32 32" className="pix-ic-plan">
        <rect x="5" y="5" width="22" height="22" fill={C.paper} />
        <rect x="4" y="4" width="24" height="1" fill={C.ink2} />
        <rect x="4" y="27" width="24" height="1" fill={C.ink2} />
        <rect x="4" y="5" width="1" height="22" fill={C.ink2} />
        <rect x="27" y="5" width="1" height="22" fill={C.ink2} />
        <rect x="5" y="5" width="22" height="2" fill={C.lav} />
        <rect x="5" y="7" width="22" height="1" fill={C.lavD} />
        <rect x="7" y="6" width="6" height="1" fill={C.paper} opacity="0.6" />
        <Row y={-2} lineW={11} descW={8} rowCls="row-1" checkCls="check-1" />
        <Row y={4} lineW={9} descW={6} rowCls="row-2" checkCls="check-2" />
        <Row y={10} lineW={10} descW={7} rowCls="row-3" checkCls="check-3" />
      </svg>
    </IconFrame>
  );
}

// ── 4. ChunkIcon ────────────────────────────────
export function ChunkIcon(props: IngestionIconProps) {
  return (
    <IconFrame {...props} svgClass="pix-ic-chunk" ariaLabel={props.ariaLabel ?? "Generate Knowledge Chunks"}>
      <svg viewBox="0 0 32 32" className="pix-ic-chunk">
        <g className="src">
          <rect x="13" y="3" width="6" height="9" fill={C.paper} />
          <rect x="12" y="3" width="8" height="1" fill={C.ink2} />
          <rect x="12" y="12" width="8" height="1" fill={C.ink2} />
          <rect x="12" y="3" width="1" height="10" fill={C.ink2} />
          <rect x="19" y="3" width="1" height="10" fill={C.ink2} />
          <rect x="14" y="6" width="4" height="1" fill={C.lavD} />
          <rect x="14" y="8" width="4" height="1" fill={C.lavD} />
          <rect x="14" y="10" width="3" height="1" fill={C.lavD} />
        </g>
        <rect x="9" y="14" width="14" height="1" fill={C.muted} />
        <rect x="10" y="15" width="1" height="2" fill={C.muted} />
        <rect x="16" y="15" width="1" height="2" fill={C.muted} />
        <rect x="22" y="15" width="1" height="2" fill={C.muted} />
        <g className="blk blk-1">
          <rect x="6" y="18" width="6" height="5" fill={C.mint} />
          <rect x="6" y="22" width="6" height="1" fill={C.mintD} />
          <rect x="8" y="20" width="3" height="1" fill={C.paper} opacity="0.7" />
        </g>
        <g className="blk blk-2">
          <rect x="13" y="18" width="6" height="5" fill={C.sky} />
          <rect x="13" y="22" width="6" height="1" fill={C.skyD} />
          <rect x="15" y="20" width="3" height="1" fill={C.paper} opacity="0.7" />
        </g>
        <g className="blk blk-3">
          <rect x="20" y="18" width="6" height="5" fill={C.peach} />
          <rect x="20" y="22" width="6" height="1" fill={C.peachD} />
          <rect x="22" y="20" width="3" height="1" fill={C.paper} opacity="0.7" />
        </g>
        <g className="blk blk-4">
          <rect x="13" y="25" width="6" height="4" fill={C.lav} />
          <rect x="13" y="28" width="6" height="1" fill={C.lavD} />
          <rect x="15" y="26" width="3" height="1" fill={C.paper} opacity="0.7" />
        </g>
      </svg>
    </IconFrame>
  );
}

// ── 5. EnrichIcon ───────────────────────────────
export function EnrichIcon(props: IngestionIconProps) {
  return (
    <IconFrame {...props} svgClass="pix-ic-enrich" ariaLabel={props.ariaLabel ?? "Enrich Extracted Content"}>
      <svg viewBox="0 0 32 32" className="pix-ic-enrich">
        <g className="core">
          <rect x="9" y="11" width="14" height="12" fill={C.paper} />
          <rect x="8" y="10" width="16" height="1" fill={C.ink2} />
          <rect x="8" y="23" width="16" height="1" fill={C.ink2} />
          <rect x="8" y="11" width="1" height="12" fill={C.ink2} />
          <rect x="23" y="11" width="1" height="12" fill={C.ink2} />
          <rect x="11" y="13" width="10" height="1" fill={C.lav} />
          <rect x="11" y="16" width="7" height="1" fill={C.lav} />
          <rect x="11" y="19" width="9" height="1" fill={C.lav} />
        </g>
        <g className="spk-1">
          <rect x="5" y="6" width="1" height="1" fill={C.sun} />
          <rect x="4" y="7" width="3" height="1" fill={C.sun} />
          <rect x="5" y="8" width="1" height="1" fill={C.sun} />
        </g>
        <g className="spk-2">
          <rect x="26" y="6" width="1" height="1" fill={C.peach} />
          <rect x="25" y="7" width="3" height="1" fill={C.peach} />
          <rect x="26" y="8" width="1" height="1" fill={C.peach} />
        </g>
        <g className="spk-3">
          <rect x="5" y="25" width="1" height="1" fill={C.mint} />
          <rect x="4" y="26" width="3" height="1" fill={C.mint} />
          <rect x="5" y="27" width="1" height="1" fill={C.mint} />
        </g>
        <g className="tag">
          <rect x="22" y="25" width="6" height="3" fill={C.sky} />
          <rect x="21" y="26" width="1" height="1" fill={C.skyD} />
          <rect x="22" y="25" width="6" height="1" fill={C.skyD} />
          <rect x="22" y="28" width="6" height="1" fill={C.skyD} />
          <rect x="24" y="26" width="3" height="1" fill={C.paper} opacity="0.7" />
        </g>
      </svg>
    </IconFrame>
  );
}

// ── 6. GraphIcon ────────────────────────────────
export function GraphIcon(props: IngestionIconProps) {
  const Node = ({
    x,
    y,
    fill,
    fillD,
    cls,
  }: {
    x: number;
    y: number;
    fill: string;
    fillD: string;
    cls: string;
  }) => (
    <g className={`node ${cls}`}>
      <rect x={x} y={y} width="5" height="5" fill={fill} />
      <rect x={x} y={y} width="5" height="1" fill={fillD} />
      <rect x={x + 1} y={y + 1} width="3" height="3" fill={C.paper} />
      <rect x={x + 2} y={y + 2} width="1" height="1" fill={fillD} />
    </g>
  );
  return (
    <IconFrame {...props} svgClass="pix-ic-graph" ariaLabel={props.ariaLabel ?? "Build Knowledge Graph"}>
      <svg viewBox="0 0 32 32" className="pix-ic-graph">
        <line className="edge e-1" x1="8.5" y1="8.5" x2="24.5" y2="8.5" stroke={C.lavD} strokeWidth="1" />
        <line className="edge e-2" x1="8.5" y1="8.5" x2="15.5" y2="18.5" stroke={C.mintD} strokeWidth="1" />
        <line className="edge e-3" x1="24.5" y1="8.5" x2="23.5" y2="23.5" stroke={C.skyD} strokeWidth="1" />
        <line className="edge e-4" x1="15.5" y1="18.5" x2="23.5" y2="23.5" stroke={C.peachD} strokeWidth="1" />
        <Node x={6} y={6} fill={C.lav} fillD={C.lavD} cls="n-1" />
        <Node x={22} y={6} fill={C.sky} fillD={C.skyD} cls="n-2" />
        <Node x={13} y={16} fill={C.mint} fillD={C.mintD} cls="n-3" />
        <Node x={21} y={21} fill={C.peach} fillD={C.peachD} cls="n-4" />
      </svg>
    </IconFrame>
  );
}

// ── 7. FinalizeIcon ─────────────────────────────
export function FinalizeIcon(props: IngestionIconProps) {
  return (
    <IconFrame {...props} svgClass="pix-ic-final" ariaLabel={props.ariaLabel ?? "Finalize Ingestion"}>
      <svg viewBox="0 0 32 32" className="pix-ic-final">
        <g className="capsule">
          <rect x="13" y="10" width="6" height="1" fill={C.peachD} />
          <rect x="12" y="11" width="8" height="1" fill={C.peachD} />
          <rect x="11" y="12" width="10" height="8" fill={C.peach} />
          <rect x="11" y="12" width="10" height="1" fill={C.peachD} />
          <rect x="12" y="20" width="8" height="1" fill={C.peachD} />
          <rect x="13" y="21" width="6" height="1" fill={C.peachD} />
          <rect x="13" y="9" width="6" height="1" fill={C.ink2} />
          <rect x="11" y="11" width="2" height="1" fill={C.ink2} />
          <rect x="19" y="11" width="2" height="1" fill={C.ink2} />
          <rect x="10" y="12" width="1" height="8" fill={C.ink2} />
          <rect x="21" y="12" width="1" height="8" fill={C.ink2} />
          <rect x="11" y="20" width="2" height="1" fill={C.ink2} />
          <rect x="19" y="20" width="2" height="1" fill={C.ink2} />
          <rect x="13" y="22" width="6" height="1" fill={C.ink2} />
          <rect x="12" y="13" width="6" height="1" fill={C.sun} />
          <rect x="11" y="13" width="1" height="3" fill={C.sun} opacity="0.55" />
          <rect x="13" y="16" width="1" height="1" fill={C.ok} />
          <rect x="14" y="17" width="1" height="1" fill={C.ok} />
          <rect x="15" y="18" width="1" height="1" fill={C.ok} />
          <rect x="16" y="17" width="1" height="1" fill={C.ok} />
          <rect x="17" y="16" width="1" height="1" fill={C.ok} />
          <rect x="18" y="15" width="1" height="1" fill={C.ok} />
        </g>
        <g className="glint">
          <rect x="16" y="13" width="1" height="1" fill="#ffffff" />
          <rect x="17" y="14" width="1" height="1" fill="#ffffff" opacity="0.6" />
        </g>
        <g className="spark sp-1">
          <rect x="6" y="6" width="1" height="1" fill={C.sun} />
          <rect x="5" y="7" width="3" height="1" fill={C.sun} />
          <rect x="6" y="8" width="1" height="1" fill={C.sun} />
        </g>
        <g className="spark sp-2">
          <rect x="25" y="22" width="1" height="1" fill={C.mint} />
          <rect x="24" y="23" width="3" height="1" fill={C.mint} />
          <rect x="25" y="24" width="1" height="1" fill={C.mint} />
        </g>
        <g className="spark sp-3">
          <rect x="25" y="6" width="1" height="1" fill={C.lav} />
          <rect x="24" y="7" width="3" height="1" fill={C.lav} />
          <rect x="25" y="8" width="1" height="1" fill={C.lav} />
        </g>
      </svg>
    </IconFrame>
  );
}
