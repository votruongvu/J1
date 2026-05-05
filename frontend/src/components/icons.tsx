/**
 * SVG icon set — minimal stroke icons used throughout the console.
 *
 * All icons accept a `className` prop (defaulting to `"icon"`) so they
 * can be sized via CSS utility classes (`icon-sm`, `icon` etc).
 */

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

const baseProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const cls = (extra: string | undefined, fallback = "icon"): string =>
  extra && extra.length > 0 ? extra : fallback;

export const Icon = {
  Upload: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  ),
  File: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  ),
  Check: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="2.25" {...baseProps} {...rest}>
      <polyline points="20 6 9 17 4 12" />
    </svg>
  ),
  CheckCircle: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <polyline points="22 4 12 14.01 9 11.01" />
    </svg>
  ),
  Alert: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  X: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="2" {...baseProps} {...rest}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  ),
  XCircle: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <circle cx="12" cy="12" r="10" />
      <line x1="15" y1="9" x2="9" y2="15" />
      <line x1="9" y1="9" x2="15" y2="15" />
    </svg>
  ),
  Info: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  ),
  Sun: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  ),
  Moon: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  ),
  Lock: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <rect x="3" y="11" width="18" height="11" rx="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  ),
  Unlock: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <rect x="3" y="11" width="18" height="11" rx="2" />
      <path d="M7 11V7a5 5 0 0 1 9.9-1" />
    </svg>
  ),
  Spark: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M12 2v6M12 16v6M4.93 4.93l4.24 4.24M14.83 14.83l4.24 4.24M2 12h6M16 12h6M4.93 19.07l4.24-4.24M14.83 9.17l4.24-4.24" />
    </svg>
  ),
  Loader: ({ className, ...rest }: IconProps) => (
    <svg className={`${cls(className)} spin`} strokeWidth="2" {...baseProps} {...rest}>
      <line x1="12" y1="2" x2="12" y2="6" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="4.93" y1="4.93" x2="7.76" y2="7.76" />
      <line x1="16.24" y1="16.24" x2="19.07" y2="19.07" />
      <line x1="2" y1="12" x2="6" y2="12" />
      <line x1="18" y1="12" x2="22" y2="12" />
      <line x1="4.93" y1="19.07" x2="7.76" y2="16.24" />
      <line x1="16.24" y1="7.76" x2="19.07" y2="4.93" />
    </svg>
  ),
  ChevronLeft: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <polyline points="15 18 9 12 15 6" />
    </svg>
  ),
  ChevronRight: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <polyline points="9 18 15 12 9 6" />
    </svg>
  ),
  Code: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <polyline points="16 18 22 12 16 6" />
      <polyline points="8 6 2 12 8 18" />
    </svg>
  ),
  Play: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <polygon points="5 3 19 12 5 21 5 3" />
    </svg>
  ),
  Pause: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <rect x="6" y="4" width="4" height="16" />
      <rect x="14" y="4" width="4" height="16" />
    </svg>
  ),
  RefreshCw: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  ),
  Cpu: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <rect x="9" y="9" width="6" height="6" />
      <line x1="9" y1="2" x2="9" y2="4" />
      <line x1="15" y1="2" x2="15" y2="4" />
      <line x1="9" y1="20" x2="9" y2="22" />
      <line x1="15" y1="20" x2="15" y2="22" />
      <line x1="20" y1="9" x2="22" y2="9" />
      <line x1="20" y1="15" x2="22" y2="15" />
      <line x1="2" y1="9" x2="4" y2="9" />
      <line x1="2" y1="15" x2="4" y2="15" />
    </svg>
  ),
  Eye: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
  EyeOff: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  ),
  UserCheck: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="8.5" cy="7" r="4" />
      <polyline points="17 11 19 13 23 9" />
    </svg>
  ),
  Layers: ({ className, ...rest }: IconProps) => (
    <svg className={cls(className)} strokeWidth="1.75" {...baseProps} {...rest}>
      <polygon points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 17 12 22 22 17" />
      <polyline points="2 12 12 17 22 12" />
    </svg>
  ),
} as const;
