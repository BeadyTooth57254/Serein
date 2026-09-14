import "./awake-reading.css";
import { createPortal } from "react-dom";
import { X } from "@phosphor-icons/react";

// Shared paper, veil and entrance motion for Awake's reading surfaces.
export function ReadingLayer({ title, titleId, label, closeLabel, onClose, closeButtonRef, className = "", readerClassName = "", children }) {
  return createPortal(
    <div className={`window-shadow-layer ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <div className="window-shadow-layer__veil" aria-hidden="true" onClick={onClose} />
      <section className={`window-shadow-reader ${readerClassName}`}>
        <header className="window-shadow-reader__topbar">
          <div><span>{label}</span><h2 id={titleId}>{title}</h2></div>
          <button ref={closeButtonRef} type="button" aria-label={closeLabel} onClick={onClose}><X size={22} weight="light" aria-hidden="true" /></button>
        </header>
        {children}
      </section>
    </div>, document.body,
  );
}
