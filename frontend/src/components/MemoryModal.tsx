import { useCallback, useEffect, useState } from "react";
import { ApiError, deleteAllMemories, deleteMemory, listMemories } from "../api";
import type { MemoryItem } from "../api";

interface MemoryModalProps {
  onClose: () => void;
  onSessionExpired: () => void;
}

// Lo que Nexa recuerda de ti: los datos que extrae de las conversaciones cada
// pocos mensajes y usa al responder. Se pueden olvidar uno a uno o todos; las
// conversaciones no se tocan.
export function MemoryModal({ onClose, onSessionExpired }: MemoryModalProps) {
  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);

  const handleError = useCallback(
    (err: unknown, fallback: string) => {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setError(err instanceof Error ? err.message : fallback);
    },
    [onSessionExpired],
  );

  const refresh = useCallback(async () => {
    try {
      setMemories((await listMemories()).items);
    } catch (err) {
      handleError(err, "No se pudo cargar la memoria.");
    }
  }, [handleError]);

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

  const forget = useCallback(
    async (id: number) => {
      // Optimista: lo quitamos de la lista ya; si falla, recargamos.
      setMemories((prev) => (prev ? prev.filter((m) => m.id !== id) : prev));
      try {
        await deleteMemory(id);
      } catch (err) {
        handleError(err, "No se pudo olvidar ese dato.");
        void refresh();
      }
    },
    [handleError, refresh],
  );

  const forgetAll = useCallback(async () => {
    setConfirmAll(false);
    try {
      await deleteAllMemories();
      setMemories([]);
    } catch (err) {
      handleError(err, "No se pudo borrar la memoria.");
      void refresh();
    }
  }, [handleError, refresh]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Memoria"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal__head">
          <h2 className="modal__title">Memoria</h2>
          <button className="modal__close" type="button" onClick={onClose} aria-label="Cerrar">
            <CrossIcon />
          </button>
        </header>

        <div className="modal__body">
          <p className="conn__hint">
            Lo que Nexa ha aprendido de tus conversaciones y usa para responderte. Si
            borras una conversación, también olvida lo que aprendió en ella.
          </p>

          {error && (
            <div className="banner banner--error" role="alert">
              {error}
            </div>
          )}

          <div className="doc-list">
            {memories === null ? (
              <ListSkeleton />
            ) : memories.length === 0 ? (
              <p className="conn__hint">Nexa todavía no recuerda nada de ti.</p>
            ) : (
              memories.map((m) => <MemoryRow key={m.id} memory={m} onForget={forget} />)
            )}
          </div>

          {memories !== null && memories.length > 0 && (
            <div className="memory-actions">
              {confirmAll ? (
                <>
                  <span className="memory-actions__ask">
                    ¿Olvidar {memories.length === 1 ? "este dato" : `los ${memories.length} datos`}?
                  </span>
                  <button
                    className="conv-confirm conv-confirm--yes"
                    type="button"
                    onClick={() => void forgetAll()}
                  >
                    Olvidar todo
                  </button>
                  <button className="conv-confirm" type="button" onClick={() => setConfirmAll(false)}>
                    No
                  </button>
                </>
              ) : (
                <button
                  className="btn btn--sm btn--ghost btn--bordered"
                  type="button"
                  onClick={() => setConfirmAll(true)}
                >
                  Olvidar todo
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function MemoryRow({
  memory,
  onForget,
}: {
  memory: MemoryItem;
  onForget: (id: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const learned = new Date(memory.created_at).toLocaleDateString("es", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  return (
    <div className="memory-row">
      <div className="memory-row__main">
        <p className="memory-row__text">{memory.content}</p>
        <span className="memory-row__meta">
          {memory.conversation_title?.trim() || "Conversación"} · {learned}
        </span>
      </div>
      {confirming ? (
        <div className="doc-row__confirm">
          <button
            className="conv-confirm conv-confirm--yes"
            type="button"
            onClick={() => onForget(memory.id)}
          >
            Olvidar
          </button>
          <button className="conv-confirm" type="button" onClick={() => setConfirming(false)}>
            No
          </button>
        </div>
      ) : (
        <button
          className="doc-row__del"
          type="button"
          aria-label="Olvidar este dato"
          onClick={() => setConfirming(true)}
        >
          <TrashIcon />
        </button>
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Cargando la memoria">
      {[80, 62, 70].map((w, i) => (
        <div className="memory-row" key={i}>
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
