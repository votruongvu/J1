/**
 * Confirmation dialogs for the destructive document-lifecycle
 * actions (detach + remove).
 *
 * Copy comes verbatim from the spec's section 10 — the user
 * explicitly asked us not to call Remove a "soft delete" in the UI,
 * so the wording here is deliberate. Both dialogs render as a
 * shared lightweight modal; we avoid a heavyweight component
 * library since the rest of the FE is also vanilla.
 */

import type { DocumentListItem } from "@/types/documents";


interface DialogProps {
  document: DocumentListItem | null;
  onConfirm: () => void;
  onCancel: () => void;
}


export function ConfirmDetachDialog({
  document, onConfirm, onCancel,
}: DialogProps) {
  if (!document) return null;
  return (
    <ModalShell onClose={onCancel} testId="confirm-detach-dialog">
      <h3>Detach this document from Knowledge?</h3>
      <p>
        J1 will stop using <strong>{document.displayName}</strong> for
        search, answers, validation, and domain context.
      </p>
      <p>
        The document, run history, and generated artifacts will be
        kept so you can attach it again later.
      </p>
      <div className="modal__actions">
        <button
          type="button"
          className="btn btn--ghost"
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn--primary"
          onClick={onConfirm}
          data-testid="confirm-detach"
        >
          Detach from Knowledge
        </button>
      </div>
    </ModalShell>
  );
}


export function ConfirmRemoveDialog({
  document, onConfirm, onCancel,
}: DialogProps) {
  if (!document) return null;
  return (
    <ModalShell onClose={onCancel} testId="confirm-remove-dialog">
      <h3>Remove this document from Knowledge?</h3>
      <p>
        J1 will remove the generated knowledge for{" "}
        <strong>{document.displayName}</strong> from active indexes.
        It will no longer be searchable or usable in answers.
      </p>
      <p>
        Previous run history may be kept only as minimal internal
        audit/debug metadata. Re-attaching this document requires
        re-uploading it.
      </p>
      <div className="modal__actions">
        <button
          type="button"
          className="btn btn--ghost"
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn--danger"
          onClick={onConfirm}
          data-testid="confirm-remove"
        >
          Remove from Knowledge
        </button>
      </div>
    </ModalShell>
  );
}


// Tiny shared modal shell — backdrop + centered card. Closes on
// backdrop click (Esc handling would be nice but is out of scope
// for this phase; defer to a later UX pass).
function ModalShell({
  children, onClose, testId,
}: {
  children: React.ReactNode;
  onClose: () => void;
  testId?: string;
}) {
  return (
    <div
      className="modal-backdrop"
      onClick={onClose}
      data-testid={testId}
    >
      <div
        className="modal-card"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        {children}
      </div>
    </div>
  );
}
