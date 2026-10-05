import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { ApiError, getConversation, streamChat, synthesize, VOICES } from "../api";
import type { MessageItem, Voice } from "../api";

interface ChatProps {
  token: string;
  // Conversación a abrir al montar (null = conversación nueva). El padre fuerza
  // un remontaje (key) al cambiarla, así que "montar" equivale a "abrir esta".
  conversationId: number | null;
  // Voz de lectura (TTS) y su setter — viven en el padre para persistir entre
  // remontajes del panel.
  voice: Voice;
  onVoiceChange: (voice: Voice) => void;
  // Se llama al terminar cada turno con el id vigente: el padre refresca la lista
  // (orden + último mensaje) y marca la conversación activa. NO provoca remontaje.
  onConversationActivity: (id: number) => void;
  onSessionExpired: () => void;
  onToggleSidebar: () => void;
}

// --- Modelo de mensaje ------------------------------------------------------
type ToolStatus = "running" | "done";
interface ToolPart {
  kind: "tool";
  id: string;
  tool: string;
  status: ToolStatus;
}
interface TextPart {
  kind: "text";
  text: string;
}
type AgentPart = ToolPart | TextPart;

type ProposalStatus = "pending" | "confirmed" | "cancelled";
interface Proposal {
  action: string;
  description: string;
  status: ProposalStatus;
}

interface UserMessage {
  id: string;
  role: "user";
  content: string;
}
interface AgentMessage {
  id: string;
  role: "agent";
  parts: AgentPart[];
  streaming: boolean;
  error: string | null;
  proposal: Proposal | null;
}
type ChatMessage = UserMessage | AgentMessage;

// Mensaje persistido (del historial) -> modelo de la UI. El historial solo guarda
// el texto final (ni pasos de herramienta ni propuestas se persisten), así que un
// turno del agente recuperado es una única parte de texto.
function fromHistory(m: MessageItem): ChatMessage {
  if (m.role === "user") {
    return { id: crypto.randomUUID(), role: "user", content: m.content };
  }
  return {
    id: crypto.randomUUID(),
    role: "agent",
    parts: [{ kind: "text", text: m.content }],
    streaming: false,
    error: null,
    proposal: null,
  };
}

// Nombres de herramienta (del backend) -> etiqueta humana en español.
const TOOL_LABELS: Record<string, string> = {
  search_knowledge_base: "Buscando en los documentos",
  search_long_term_memory: "Recordando conversaciones pasadas",
  http_get: "Consultando una página web",
  create_task: "Creando una tarea",
  list_tasks: "Revisando tus tareas",
  update_task: "Actualizando una tarea",
  delete_task: "Preparando el borrado de una tarea",
  call_webhook: "Enviando una notificación",
  list_calendar_events: "Consultando tu calendario",
  create_calendar_event: "Preparando un evento",
  send_email: "Preparando un correo",
  list_drive_files: "Buscando en Google Drive",
  ingest_drive_file: "Ingiriendo un documento de Drive",
};

interface ProposalMeta {
  title: string;
  confirmLabel: string;
  danger: boolean;
  Icon: () => JSX.Element;
}
const PROPOSAL_META: Record<string, ProposalMeta> = {
  send_email: { title: "Confirmar envío de correo", confirmLabel: "Sí, enviar", danger: false, Icon: MailIcon },
  create_calendar_event: { title: "Confirmar evento", confirmLabel: "Sí, crear", danger: false, Icon: CalendarIcon },
  delete_task: { title: "Confirmar eliminación", confirmLabel: "Sí, eliminar", danger: true, Icon: TrashIcon },
};
const DEFAULT_PROPOSAL_META: ProposalMeta = {
  title: "Confirmar acción",
  confirmLabel: "Sí, confirmar",
  danger: false,
  Icon: SparkIcon,
};

// --- Transformaciones puras sobre las partes de un mensaje del agente -------
function appendToken(parts: AgentPart[], value: string): AgentPart[] {
  const last = parts[parts.length - 1];
  if (last && last.kind === "text") {
    return [...parts.slice(0, -1), { kind: "text", text: last.text + value }];
  }
  return [...parts, { kind: "text", text: value }];
}

function startTool(parts: AgentPart[], tool: string): AgentPart[] {
  return [...parts, { kind: "tool", id: crypto.randomUUID(), tool, status: "running" }];
}

function endTool(parts: AgentPart[], tool: string): AgentPart[] {
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (p.kind === "tool" && p.tool === tool && p.status === "running") {
      const next = [...parts];
      next[i] = { ...p, status: "done" };
      return next;
    }
  }
  return parts;
}

// --- Reproducción por voz (TTS bajo demanda) --------------------------------
// Un solo audio sonando a la vez. `speak(id, text)` alterna: si ese mensaje ya
// está activo (generando o sonando), para; si no, genera y reproduce. Estados
// expuestos por id de mensaje para que cada fila pinte el suyo.
type SpeakStatus = "loading" | "playing";

function useSpeech(token: string, voice: Voice, onSessionExpired: () => void) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const activeRef = useRef<string | null>(null); // guarda contra respuestas tardías
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const [status, setStatus] = useState<SpeakStatus | null>(null);
  const [errorId, setErrorId] = useState<string | null>(null);

  const teardown = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.onended = null;
      audioRef.current.onerror = null;
      audioRef.current.src = "";
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    activeRef.current = null;
    teardown();
    setSpeakingId(null);
    setStatus(null);
  }, [teardown]);

  const speak = useCallback(
    async (id: string, text: string) => {
      if (activeRef.current === id) {
        stop(); // toggle: ya activo -> parar
        return;
      }
      teardown(); // corta cualquier lectura previa (una sola a la vez)
      activeRef.current = id;
      setErrorId(null);
      setSpeakingId(id);
      setStatus("loading");
      try {
        const blob = await synthesize(token, text, voice);
        if (activeRef.current !== id) return; // se canceló/cambió mientras generaba
        const url = URL.createObjectURL(blob);
        urlRef.current = url;
        const audio = new Audio(url);
        audioRef.current = audio;
        audio.onended = () => stop();
        audio.onerror = () => {
          setErrorId(id);
          stop();
        };
        await audio.play();
        if (activeRef.current === id) setStatus("playing");
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          stop();
          onSessionExpired();
          return;
        }
        if (activeRef.current === id) {
          setErrorId(id);
          stop();
        }
      }
    },
    [token, voice, stop, teardown, onSessionExpired],
  );

  // Cambiar de voz detiene lo que esté sonando (la próxima lectura usa la nueva).
  useEffect(() => {
    stop();
  }, [voice, stop]);

  // Limpieza al desmontar el panel.
  useEffect(() => () => teardown(), [teardown]);

  return { speak, speakingId, status, errorId };
}

export function Chat({
  token,
  conversationId,
  voice,
  onVoiceChange,
  onConversationActivity,
  onSessionExpired,
  onToggleSidebar,
}: ChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [convId, setConvId] = useState<number | null>(conversationId);
  const [title, setTitle] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState<boolean>(conversationId !== null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);

  const speech = useSpeech(token, voice, onSessionExpired);

  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const atBottomRef = useRef(true);

  // Carga del historial al montar (montar = abrir esta conversación).
  useEffect(() => {
    if (conversationId === null) return;
    let cancelled = false;
    (async () => {
      try {
        const detail = await getConversation(token, conversationId);
        if (cancelled) return;
        setTitle(detail.title);
        setMessages(detail.messages.map(fromHistory));
        atBottomRef.current = true;
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onSessionExpired();
          return;
        }
        if (!cancelled) {
          setHistoryError(
            err instanceof Error ? err.message : "No se pudo cargar la conversación.",
          );
        }
      } finally {
        if (!cancelled) setLoadingHistory(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, token, onSessionExpired]);

  useLayoutEffect(() => {
    if (atBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loadingHistory]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }

  const updateAgent = useCallback(
    (id: string, fn: (m: AgentMessage) => AgentMessage) => {
      setMessages((prev) =>
        prev.map((m) => (m.id === id && m.role === "agent" ? fn(m) : m)),
      );
    },
    [],
  );

  function resetTextareaHeight() {
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }

  function autoGrow() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text || sending) return;

      const userMsg: UserMessage = { id: crypto.randomUUID(), role: "user", content: text };
      const agentId = crypto.randomUUID();
      const agentMsg: AgentMessage = {
        id: agentId,
        role: "agent",
        parts: [],
        streaming: true,
        error: null,
        proposal: null,
      };

      // Título optimista para una conversación nueva (el backend hace lo mismo:
      // title = primer mensaje[:80]).
      setTitle((t) => t ?? text.slice(0, 80));
      atBottomRef.current = true;
      setMessages((prev) => [...prev, userMsg, agentMsg]);
      setSending(true);

      let turnConvId = convId;
      try {
        for await (const ev of streamChat(token, text, convId)) {
          switch (ev.type) {
            case "meta":
              turnConvId = ev.conversation_id;
              setConvId(ev.conversation_id);
              break;
            case "token":
              updateAgent(agentId, (m) => ({ ...m, parts: appendToken(m.parts, ev.value) }));
              break;
            case "tool_start":
              updateAgent(agentId, (m) => ({ ...m, parts: startTool(m.parts, ev.tool) }));
              break;
            case "tool_end":
              updateAgent(agentId, (m) => ({ ...m, parts: endTool(m.parts, ev.tool) }));
              break;
            case "proposal":
              updateAgent(agentId, (m) => ({
                ...m,
                proposal: { action: ev.action, description: ev.description, status: "pending" },
              }));
              break;
            case "error":
              updateAgent(agentId, (m) => ({ ...m, error: ev.message }));
              break;
            case "done":
              break;
          }
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onSessionExpired();
          return;
        }
        const msg = err instanceof Error ? err.message : "Error inesperado.";
        updateAgent(agentId, (m) => ({ ...m, error: msg }));
      } finally {
        updateAgent(agentId, (m) => ({ ...m, streaming: false }));
        setSending(false);
        // Avisa al padre para refrescar la lista (orden + preview) y resaltar.
        if (turnConvId !== null) onConversationActivity(turnConvId);
      }
    },
    [token, convId, sending, updateAgent, onSessionExpired, onConversationActivity],
  );

  function handleFormSubmit(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    requestAnimationFrame(resetTextareaHeight);
    void sendMessage(text);
  }

  function respondToProposal(messageId: string, confirm: boolean) {
    if (sending) return;
    updateAgent(messageId, (m) =>
      m.proposal
        ? { ...m, proposal: { ...m.proposal, status: confirm ? "confirmed" : "cancelled" } }
        : m,
    );
    void sendMessage(confirm ? "sí" : "no");
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleFormSubmit();
    }
  }

  const isEmpty = messages.length === 0;
  const displayTitle =
    title?.trim() || (convId === null ? "Nueva conversación" : "Conversación");

  return (
    <div className="chat-shell">
      <header className="chat-titlebar">
        <button
          className="chat-titlebar__menu"
          type="button"
          onClick={onToggleSidebar}
          aria-label="Mostrar conversaciones"
        >
          <MenuIcon />
        </button>
        <h1 className="chat-titlebar__title">{displayTitle}</h1>
        <VoiceSelector value={voice} onChange={onVoiceChange} />
      </header>

      <div className="messages" ref={scrollRef} onScroll={handleScroll} aria-live="polite">
        {loadingHistory ? (
          <HistorySkeleton />
        ) : historyError ? (
          <div className="thread">
            <div className="banner banner--error" role="alert">
              {historyError}
            </div>
          </div>
        ) : isEmpty ? (
          <EmptyState />
        ) : (
          <div className="thread">
            {messages.map((m) =>
              m.role === "user" ? (
                <UserRow key={m.id} content={m.content} />
              ) : (
                <AgentRow
                  key={m.id}
                  message={m}
                  sending={sending}
                  onRespond={respondToProposal}
                  onSpeak={speech.speak}
                  speakState={speech.speakingId === m.id ? speech.status : null}
                  speakError={speech.errorId === m.id}
                />
              ),
            )}
          </div>
        )}
      </div>

      <div className="composer">
        <div className="composer__inner">
          <form className="composer__bar" onSubmit={handleFormSubmit}>
            <textarea
              ref={textareaRef}
              className="composer__input"
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                autoGrow();
              }}
              onKeyDown={handleKeyDown}
              placeholder="Escribe un mensaje…"
              rows={1}
              disabled={sending}
            />
            <button
              className="btn btn--send"
              type="submit"
              disabled={sending || !input.trim()}
              aria-label="Enviar mensaje"
            >
              <SendIcon />
            </button>
          </form>
          <p className="composer__hint">
            <kbd>Enter</kbd> para enviar · <kbd>Shift</kbd>+<kbd>Enter</kbd> nueva
            línea
          </p>
        </div>
      </div>
    </div>
  );
}

function UserRow({ content }: { content: string }) {
  return (
    <div className="msg msg--user">
      <div className="msg__bubble">{content}</div>
    </div>
  );
}

function AgentRow({
  message,
  sending,
  onRespond,
  onSpeak,
  speakState,
  speakError,
}: {
  message: AgentMessage;
  sending: boolean;
  onRespond: (messageId: string, confirm: boolean) => void;
  onSpeak: (id: string, text: string) => void;
  speakState: SpeakStatus | null;
  speakError: boolean;
}) {
  let lastTextIndex = -1;
  message.parts.forEach((p, i) => {
    if (p.kind === "text") lastTextIndex = i;
  });
  const showLeadingThinking =
    message.streaming && message.parts.length === 0 && !message.error;

  // Texto leíble = solo las partes de texto (sin pasos de herramienta). El botón
  // de leer aparece cuando el turno terminó y hay texto, no durante el streaming.
  const speakText = message.parts
    .filter((p): p is TextPart => p.kind === "text")
    .map((p) => p.text)
    .join(" ")
    .trim();
  const canSpeak = !message.streaming && !message.error && speakText.length > 0;

  return (
    <div className="msg msg--agent">
      <span className="msg__label">Nexa</span>

      {showLeadingThinking && <ThinkingDots />}

      {message.parts.map((part, i) => {
        if (part.kind === "tool") {
          return <ToolStep key={part.id} part={part} />;
        }
        const isLastText = i === lastTextIndex;
        return (
          <p className="msg__body" key={`t${i}`}>
            {part.text}
            {isLastText && message.streaming && !message.error && (
              <span className="caret" aria-hidden="true" />
            )}
          </p>
        );
      })}

      {canSpeak && (
        <div className="msg__actions">
          <button
            className={`speak-btn${speakState ? " is-active" : ""}`}
            type="button"
            onClick={() => onSpeak(message.id, speakText)}
            aria-label={
              speakState === "loading"
                ? "Generando audio"
                : speakState === "playing"
                  ? "Detener lectura"
                  : "Leer en voz alta"
            }
            title={
              speakState === "loading"
                ? "Generando audio…"
                : speakState === "playing"
                  ? "Detener"
                  : "Leer en voz alta"
            }
          >
            {speakState === "loading" ? (
              <Spinner />
            ) : speakState === "playing" ? (
              <StopIcon />
            ) : (
              <SpeakerIcon />
            )}
          </button>
          {speakError && <span className="speak-error">No se pudo leer.</span>}
        </div>
      )}

      {message.proposal && (
        <ProposalCard
          proposal={message.proposal}
          sending={sending}
          onRespond={(confirm) => onRespond(message.id, confirm)}
        />
      )}

      {message.error && (
        <div className="banner banner--error" role="alert">
          {message.error}
        </div>
      )}
    </div>
  );
}

function ProposalCard({
  proposal,
  sending,
  onRespond,
}: {
  proposal: Proposal;
  sending: boolean;
  onRespond: (confirm: boolean) => void;
}) {
  const meta = PROPOSAL_META[proposal.action] ?? DEFAULT_PROPOSAL_META;
  const Icon = meta.Icon;

  return (
    <div
      className={`proposal${meta.danger ? " proposal--danger" : ""}`}
      aria-label={`Confirmación: ${proposal.description}`}
    >
      <div className="proposal__head">
        <span className="proposal__icon" aria-hidden="true">
          <Icon />
        </span>
        <span className="proposal__title">{meta.title}</span>
      </div>

      {proposal.status === "pending" ? (
        <div className="proposal__actions">
          <button
            className={`btn btn--sm ${meta.danger ? "btn--danger" : "btn--primary"}`}
            type="button"
            onClick={() => onRespond(true)}
            disabled={sending}
          >
            {meta.confirmLabel}
          </button>
          <button
            className="btn btn--sm btn--ghost"
            type="button"
            onClick={() => onRespond(false)}
            disabled={sending}
          >
            Cancelar
          </button>
        </div>
      ) : (
        <div className={`proposal__resolved proposal__resolved--${proposal.status}`}>
          {proposal.status === "confirmed" ? (
            <>
              <CheckIcon />
              Confirmado
            </>
          ) : (
            <>
              <CrossIcon />
              Cancelado
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ToolStep({ part }: { part: ToolPart }) {
  const label = TOOL_LABELS[part.tool] ?? `Usando ${part.tool}`;
  return (
    <div className={`tool-step tool-step--${part.status}`}>
      <span className="tool-step__icon" aria-hidden="true">
        {part.status === "running" ? <Spinner /> : <CheckIcon />}
      </span>
      <span className="tool-step__label">
        {label}
        {part.status === "running" && <span className="tool-step__ellipsis">…</span>}
      </span>
    </div>
  );
}

function ThinkingDots() {
  return (
    <div className="thinking" aria-label="Nexa está pensando">
      <span className="thinking__dot" />
      <span className="thinking__dot" />
      <span className="thinking__dot" />
    </div>
  );
}

function HistorySkeleton() {
  return (
    <div className="thread" aria-label="Cargando conversación" aria-busy="true">
      <div className="msg msg--user">
        <div className="skeleton skeleton--bubble" style={{ width: "46%" }} />
      </div>
      <div className="msg msg--agent">
        <span className="msg__label">Nexa</span>
        <div className="skeleton skeleton--line" style={{ width: "92%" }} />
        <div className="skeleton skeleton--line" style={{ width: "74%" }} />
      </div>
      <div className="msg msg--user">
        <div className="skeleton skeleton--bubble" style={{ width: "38%" }} />
      </div>
      <div className="msg msg--agent">
        <span className="msg__label">Nexa</span>
        <div className="skeleton skeleton--line" style={{ width: "84%" }} />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="empty">
      <SparkIcon />
      <h1 className="empty__title">Empieza una conversación</h1>
      <p className="empty__text">
        Pídele a Nexa que revise tu calendario, busque algo en tus documentos o
        cree una tarea. Verás en vivo cada paso que da.
      </p>
    </div>
  );
}

// Selector de voz (control segmentado en el header del chat). Sobrio: dos pastillas
// Ana/Alma; la activa resalta. La elección se usa para todas las lecturas.
function VoiceSelector({
  value,
  onChange,
}: {
  value: Voice;
  onChange: (voice: Voice) => void;
}) {
  return (
    <div className="voice-select" role="group" aria-label="Voz de lectura">
      <span className="voice-select__icon" aria-hidden="true">
        <SpeakerIcon />
      </span>
      {VOICES.map((v) => (
        <button
          key={v.id}
          type="button"
          className={`voice-select__opt${value === v.id ? " is-active" : ""}`}
          onClick={() => onChange(v.id)}
          aria-pressed={value === v.id}
        >
          {v.label}
        </button>
      ))}
    </div>
  );
}

/* --- Iconos: SVG primitivos, trazo uniforme (sin librerías ni emojis) --- */

function SpeakerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M11 5 6 9H3v6h3l5 4V5z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path
        d="M15.5 8.5a5 5 0 0 1 0 7M18 6a8 8 0 0 1 0 12"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M5 12h13M13 6l6 6-6 6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M20 6 9 17l-5-5"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CrossIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M18 6 6 18M6 6l12 12"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function MailIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3" y="5" width="18" height="14" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <path d="m4 7 8 6 8-6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3" y="5" width="18" height="16" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <path d="M3 9h18M8 3v4m8-4v4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
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

function Spinner() {
  return <span className="spinner" />;
}

function SparkIcon() {
  return (
    <svg
      className="empty__icon"
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M12 3v4m0 10v4m9-9h-4M7 12H3m13.5-6.5-2.8 2.8m-5.4 5.4-2.8 2.8m11 0-2.8-2.8M8.3 8.3 5.5 5.5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
