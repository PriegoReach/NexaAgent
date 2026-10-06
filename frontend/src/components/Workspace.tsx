import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  deleteConversation,
  getGoogleStatus,
  listConversations,
  needsGoogleReconnect,
  type ConversationSummary,
  type GoogleStatus,
  type Voice,
} from "../api";
import { Sidebar } from "./Sidebar";
import { Chat } from "./Chat";
import { ConnectionsModal } from "./ConnectionsModal";
import { DocumentsModal } from "./DocumentsModal";

interface WorkspaceProps {
  token: string;
  onSignOut: () => void;
  onSessionExpired: () => void;
}

// Compone la barra lateral (historial) y el panel de chat.
//
// Dos ideas de estado:
//  - `selectedConvId` + `remount` forman la KEY del panel: cambian SOLO cuando el
//    usuario elige una conversación o pulsa "nueva" -> el panel se remonta y carga
//    de cero. Una conversación creada al vuelo NO toca estos, así que el chat en
//    curso no se borra.
//  - `activeId` es solo para resaltar en la lista; se actualiza también cuando un
//    turno crea/usa una conversación.
export function Workspace({ token, onSignOut, onSessionExpired }: WorkspaceProps) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedConvId, setSelectedConvId] = useState<number | null>(null);
  const [remount, setRemount] = useState(0);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const [googleStatus, setGoogleStatus] = useState<GoogleStatus | null>(null);
  const [connectionsOpen, setConnectionsOpen] = useState(false);
  const [documentsOpen, setDocumentsOpen] = useState(false);

  // Voz de lectura (TTS). Vive aquí, no en Chat, para que la elección persista
  // cuando el panel de chat se remonta al cambiar de conversación.
  const [voice, setVoice] = useState<Voice>("ana");

  const loadGoogleStatus = useCallback(async () => {
    try {
      setGoogleStatus(await getGoogleStatus(token));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onSessionExpired();
      // Otros errores: dejamos el indicador como esté (no es crítico).
    }
  }, [token, onSessionExpired]);

  const loadList = useCallback(async () => {
    setListError(null);
    try {
      const res = await listConversations(token);
      setConversations(res.items);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setListError(
        err instanceof Error ? err.message : "No se pudieron cargar las conversaciones.",
      );
    } finally {
      setListLoading(false);
    }
  }, [token, onSessionExpired]);

  useEffect(() => {
    void loadList();
    void loadGoogleStatus();
  }, [loadList, loadGoogleStatus]);

  const selectConversation = useCallback(
    (id: number) => {
      setSidebarOpen(false);
      if (id === activeId) return; // ya está abierta/activa: no recargar
      setActiveId(id);
      setSelectedConvId(id);
      setRemount((n) => n + 1);
    },
    [activeId],
  );

  const newConversation = useCallback(() => {
    setSidebarOpen(false);
    setActiveId(null);
    setSelectedConvId(null);
    setRemount((n) => n + 1);
  }, []);

  // Tras cada turno: resalta la conversación vigente y refresca la lista (orden y
  // preview cambian). No remonta el panel. También refresca el estado de Google: una
  // tool puede haber descubierto en este turno que el acceso ya no se renueva.
  const onConversationActivity = useCallback(
    (id: number) => {
      setActiveId(id);
      void loadList();
      void loadGoogleStatus();
    },
    [loadList, loadGoogleStatus],
  );

  // El panel muestra el estado al día, no el de cuando se cargó la página.
  const openConnections = useCallback(() => {
    setConnectionsOpen(true);
    void loadGoogleStatus();
  }, [loadGoogleStatus]);

  const handleDelete = useCallback(
    async (id: number) => {
      try {
        await deleteConversation(token, id);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onSessionExpired();
          return;
        }
        // Si falla, recargamos para reflejar el estado real del servidor.
        void loadList();
        return;
      }
      setConversations((prev) => prev.filter((c) => c.id !== id));
      // Si borramos la que estaba abierta, volvemos a una conversación nueva.
      if (id === activeId || id === selectedConvId) {
        newConversation();
      }
    },
    [token, onSessionExpired, loadList, activeId, selectedConvId, newConversation],
  );

  const paneKey = `${selectedConvId ?? "new"}-${remount}`;

  return (
    <div className="workspace">
      {sidebarOpen && (
        <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} aria-hidden="true" />
      )}
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        loading={listLoading}
        error={listError}
        open={sidebarOpen}
        googleState={
          needsGoogleReconnect(googleStatus) ? "warn" : googleStatus?.connected ? "on" : "off"
        }
        onSelect={selectConversation}
        onNew={newConversation}
        onDelete={handleDelete}
        onOpenDocuments={() => setDocumentsOpen(true)}
        onOpenConnections={openConnections}
        onSignOut={onSignOut}
      />
      <Chat
        key={paneKey}
        token={token}
        conversationId={selectedConvId}
        voice={voice}
        onVoiceChange={setVoice}
        onConversationActivity={onConversationActivity}
        onSessionExpired={onSessionExpired}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
      />

      {connectionsOpen && (
        <ConnectionsModal
          token={token}
          status={googleStatus}
          onClose={() => setConnectionsOpen(false)}
          onConnected={loadGoogleStatus}
          onSessionExpired={onSessionExpired}
        />
      )}

      {documentsOpen && (
        <DocumentsModal
          token={token}
          onClose={() => setDocumentsOpen(false)}
          onSessionExpired={onSessionExpired}
        />
      )}
    </div>
  );
}
