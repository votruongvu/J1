/**
 * Backdrop-dismissable modal. Click outside to close; click inside is
 * stopped from propagating so it doesn't close.
 */

import type { ReactNode } from "react";
import { Icon } from "./icons";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}

export function Modal({ open, onClose, title, children, footer }: ModalProps) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h3 className="modal__title">{title}</h3>
          <button className="btn btn--ghost btn--sm" onClick={onClose} aria-label="Close">
            <Icon.X className="icon-sm" />
          </button>
        </div>
        <div className="modal__body">{children}</div>
        {footer && <div className="modal__foot">{footer}</div>}
      </div>
    </div>
  );
}
