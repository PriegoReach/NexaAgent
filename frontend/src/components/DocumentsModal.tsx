import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, deleteDocument, listDocuments, uploadDocument } from "../api";
import type { DocumentItem } from "../api";

interface DocumentsModalProps {
  onClose: () => void;
  onSessionExpired: () => void;
}

// El parser del backend maneja bien PDF y texto plano; binarios como .docx no.
// Misma lista que SUPPORTED_SUFFIXES en el backend (app/rag/ingest.py).
const ACCEPT = ".pdf,.txt,.md,.markdown,.csv,.tsv,.json,.log,.yaml,.yml,.rst,.text";
const SUPPORTED = ACCEPT.split(",");

// `accept` solo filtra el diálogo de abrir archivo: lo que se arrastra llega tal cual.
function isSupported(file: File): boolean {
  const dot = file.name.lastIndexOf(".");
  return dot !== -1 && SUPPORTED.includes(file.name.slice(dot).toLowerCase());
}

export function DocumentsModal({ onClose, onSessionExpired }: DocumentsModalProps) {
  const [docs, setDocs] = useState<DocumentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setDocs(await listDocuments());
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setError(err instanceof Error ? err.message : "No se pudieron cargar los documentos.");
    }
  }, [onSessionExpired]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Cerrar con Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // La ingesta corre en el worker; mientras haya alguno "pending", sondeamos para
  // reflejar el paso a "ready"/"failed". Paramos cuando ninguno queda pendiente.
  const hasPending = !!docs?.some((d) => d.status === "pending");
  useEffect(() => {
    if (!hasPending) return;
    const t = setInterval(() => {
      void refresh();
    }, 2500);
    return () => clearInterval(t);
  }, [hasPending, refresh]);

  const handleFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      const all = Array.from(files);
      const accepted = all.filter(isSupported);
      const skipped = all.filter((f) => !isSupported(f));
      setError(
        skipped.length
          ? `No subí ${skipped.map((f) => f.name).join(", ")}: solo se pueden indexar PDF y archivos de texto (.txt, .md, .csv, .json…).`
          : null,
      );
      if (accepted.length === 0) {
        if (inputRef.current) inputRef.current.value = "";
        return;
      }
      setUploading(true);
      try {
        for (const file of accepted) {
          await uploadDocument(file);
        }
        await refresh();
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onSessionExpired();
          return;
        }
        setError(err instanceof Error ? err.message : "No se pudo subir el documento.");
      } finally {
        setUploading(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [refresh, onSessionExpired],
  );

  const handleDelete = useCallback(
    async (id: number) => {
      // Optimista: lo quitamos de la lista ya; si falla, recargamos.
      setDocs((prev) => (prev ? prev.filter((d) => d.id !== id) : prev));
      try {
        await deleteDocument(id);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onSessionExpired();
          return;
        }
        setError(err instanceof Error ? err.message : "No se pudo quitar el documento.");
        void refresh();
      }
    },
    [refresh, onSessionExpired],
  );

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Documentos"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal__head">
          <h2 className="modal__title">Documentos</h2>
          <button className="modal__close" type="button" onClick={onClose} aria-label="Cerrar">
            <CrossIcon />
          </button>
        </header>

        <div className="modal__body">
          <p className="conn__hint">
            Sube documentos para que Nexa los busque al responder. Funciona con PDF
            y texto (.txt, .md, .csv, .json). Los PDF escaneados, sin texto, todavía
            no se pueden leer.
          </p>

          <div
            className={`dropzone${dragOver ? " dropzone--over" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => inputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                inputRef.current?.click();
              }
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              void handleFiles(e.dataTransfer.files);
            }}
          >
            <UploadIcon />
            <span className="dropzone__text">
              {uploading ? "Subiendo…" : "Arrastra un archivo o haz clic para elegir"}
            </span>
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              multiple
              hidden
              onChange={(e) => void handleFiles(e.target.files)}
            />
          </div>

          {error && (
            <div className="banner banner--error" role="alert">
              {error}
            </div>
          )}

          <div className="doc-list">
            {docs === null ? (
              <ListSkeleton />
            ) : docs.length === 0 ? (
              <p className="conn__hint">Aún no hay documentos.</p>
            ) : (
              docs.map((d) => <DocRow key={d.id} doc={d} onDelete={handleDelete} />)
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function DocRow({
  doc,
  onDelete,
}: {
  doc: DocumentItem;
  onDelete: (id: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="doc-row">
      <span className="doc-row__icon" aria-hidden="true">
        <FileIcon />
      </span>
      <span className="doc-row__name" title={doc.filename}>
        {doc.filename}
      </span>
      <StatusBadge status={doc.status} />
      {confirming ? (
        <div className="doc-row__confirm">
          <button
            className="conv-confirm conv-confirm--yes"
            type="button"
            onClick={() => onDelete(doc.id)}
          >
            Quitar
          </button>
          <button className="conv-confirm" type="button" onClick={() => setConfirming(false)}>
            No
          </button>
        </div>
      ) : (
        <button
          className="doc-row__del"
          type="button"
          aria-label="Quitar documento"
          onClick={() => setConfirming(true)}
        >
          <TrashIcon />
        </button>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  if (status === "ready") {
    return (
      <span className="doc-badge doc-badge--ready">
        <CheckIcon /> Listo
      </span>
    );
  }
  if (status === "empty") {
    return (
      <span
        className="doc-badge doc-badge--empty"
        title="No encontré texto que indexar. Si es un PDF escaneado, todavía no se puede leer."
      >
        Sin texto
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="doc-badge doc-badge--failed">
        <CrossIcon /> Error
      </span>
    );
  }
  // pending (u otros): aún procesando.
  return (
    <span className="doc-badge doc-badge--pending">
      <span className="spinner" /> Procesando…
    </span>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Cargando documentos">
      {[70, 55, 62].map((w, i) => (
        <div className="doc-row" key={i}>
          <div className="skeleton skeleton--line" style={{ width: `${w}%` }} />
        </div>
      ))}
    </div>
  );
}

/* --- Iconos --- */

function CrossIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M18 6 6 18M6 6l12 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 16V4m0 0L7 9m5-5 5 5M5 17v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V8m-5-5 5 5m-5-5v5h5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m2 0v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
