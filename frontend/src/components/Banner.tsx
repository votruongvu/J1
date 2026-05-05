/**
 * Inline banner used for one-line context messages — info / warn / err.
 * Renders an icon + title + optional body and an optional action node.
 */

import type { ReactNode } from "react";
import { Icon } from "./icons";

export type BannerKind = "info" | "warn" | "err";

interface BannerProps {
  kind?: BannerKind;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}

export function Banner({ kind = "info", title, children, action }: BannerProps) {
  const IconEl = kind === "err" ? Icon.XCircle : kind === "warn" ? Icon.Alert : Icon.Info;
  return (
    <div className={`banner banner--${kind}`}>
      <IconEl className="icon" />
      <div style={{ flex: 1 }}>
        <p className="banner__title">{title}</p>
        {children && <p className="banner__body">{children}</p>}
      </div>
      {action}
    </div>
  );
}
