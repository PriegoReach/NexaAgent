import { useState } from "react";
import type { ConversationSummary } from "../api";

interface SidebarProps {
  conversations: ConversationSummary[];
  activeId: number | null;
  loading: boolean;
  error: string | null;
  open: boolean;
  googleState: "on" | "warn" | "off";
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (id: number) => void;
  onOpenDocuments: () => void;
  onOpenConnections: () => void;
  onSignOut: () => void;
}

// Tiempo relativo en español. created_at/last_message_at vienen con offset de
// zona (columna timestamptz), así que new Date() los interpreta bien.
function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.round((Date.now() - then) / 1000);
  if (s < 45) return "ahora";
  const m = Math.round(s / 60);
  if (m < 60) return `hace ${m} min`;
  const h = Math.round(m / 60);
  if (h < 24) return `hace ${h} h`;
  const d = Math.round(h / 24);
  if (d === 1) return "ayer";
  if (d < 7) return `hace ${d} d`;
  return new Date(iso).toLocaleDateString("es", { day: "numeric", month: "short" });
}

export function Sidebar({
  conversations,
  activeId,
  loading,
  error,
  open,
  googleState,
  onSelect,
  onNew,
  onDelete,
  onOpenDocuments,
  onOpenConnections,
  onSignOut,
}: SidebarProps) {
  return (
    <aside className={`sidebar${open ? " sidebar--open" : ""}`}>
      <div className="sidebar__head">
        <div className="brand">
          <span className="brand__mark">Nexa</span>
          <span className="status-dot" aria-hidden="true" />
          <span className="brand__status">en línea</span>
        </div>
      </div>

      <button className="btn btn--new" type="button" onClick={onNew}>
        <PlusIcon />
        Nueva conversación
      </button>

      <nav className="sidebar__list" aria-label="Conversaciones">
        {loading ? (
          <ListSkeleton />
        ) : error ? (
          <p className="sidebar__msg sidebar__msg--error">{error}</p>
        ) : conversations.length === 0 ? (
          <p className="sidebar__msg">Aún no hay conversaciones. Empieza una arriba.</p>
        ) : (
          conversations.map((c) => (
            <ConversationItem
              key={c.id}
              conv={c}
              active={c.id === activeId}
              onSelect={onSelect}
              onDelete={onDelete}
            />
          ))
        )}
      </nav>

      <div className="sidebar__foot">
        <button
          className="btn btn--ghost btn--block-left"
          type="button"
          onClick={onOpenDocuments}
        >
          <DocsIcon />
          Documentos
        </button>
        <button
          className="btn btn--ghost btn--block-left"
          type="button"
          onClick={onOpenConnections}
        >
          <LinkIcon />
          Conexiones
          <span
            className={`conn-dot${googleState === "off" ? "" : ` conn-dot--${googleState}`}`}
            aria-label={
              googleState === "on"
                ? "Google conectado"
                : googleState === "warn"
                  ? "Hay que reconectar Google"
                  : "Google sin conectar"
            }
          />
        </button>
        <button className="btn btn--ghost btn--block-left" type="button" onClick={onSignOut}>
          <DoorIcon />
          Salir
        </button>
      </div>
    </aside>
  );
}

function ConversationItem({
  conv,
  active,
  onSelect,
  onDelete,
}: {
  conv: ConversationSummary;
  active: boolean;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const title = conv.title?.trim() || conv.last_message?.trim() || "Conversación";
  const preview = conv.last_message?.trim() || "Sin mensajes todavía";

  return (
    <div
      className={`conv-item${active ? " conv-item--active" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(conv.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(conv.id);
        }
      }}
    >
      <div className="conv-item__main">
        <div className="conv-item__top">
          <span className="conv-item__title">{title}</span>
          <span className="conv-item__time">
            {relTime(conv.last_message_at ?? conv.created_at)}
          </span>
        </div>
        <span className="conv-item__preview">{preview}</span>
      </div>

      {confirming ? (
        <div className="conv-item__confirm" onClick={(e) => e.stopPropagation()}>
          <button
            className="conv-confirm conv-confirm--yes"
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onDelete(conv.id);
            }}
          >
            Borrar
          </button>
          <button
            className="conv-confirm"
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setConfirming(false);
            }}
          >
            No
          </button>
        </div>
      ) : (
        <button
          className="conv-item__del"
          type="button"
          aria-label="Borrar conversación"
          onClick={(e) => {
            e.stopPropagation();
            setConfirming(true);
          }}
        >
          <TrashIcon />
        </button>
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Cargando conversaciones">
      {[68, 52, 60, 44].map((w, i) => (
        <div className="conv-skel" key={i}>
          <div className="skeleton skeleton--line" style={{ width: `${w}%` }} />
          <div className="skeleton skeleton--line skeleton--faint" style={{ width: `${w + 18}%` }} />
        </div>
      ))}
    </div>
  );
}

function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
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

function DoorIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h8M14 12h7m0 0-3-3m3 3-3 3"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1m-1 8a5 5 0 0 1-7 0 5 5 0 0 1 0-7l2-2a5 5 0 0 1 7 0"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function DocsIcon() {
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
