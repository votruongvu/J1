/**
 * Stack of dismissable toasts pinned to the bottom-right. The host is
 * a controlled component — `App.tsx` owns the queue and auto-expires
 * each toast after a short delay.
 */

import type { Toast } from "@/types/ui";

interface ToastHostProps {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}

export function ToastHost({ toasts, onDismiss }: ToastHostProps) {
  return (
    <div className="toast-host">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast toast--${t.kind ?? ""}`}
          onClick={() => onDismiss(t.id)}
        >
          <strong style={{ display: "block", marginBottom: 2 }}>{t.title}</strong>
          {t.body && <span style={{ color: "var(--text-muted)" }}>{t.body}</span>}
        </div>
      ))}
    </div>
  );
}
