// Cliente HTTP del backend. Todo el conocimiento de la API REST vive aquí: las
// rutas, los shapes de request/response y el manejo de errores. Los componentes
// no hacen fetch directo, llaman a estas funciones.
//
// Shapes verificados contra el backend (no asumidos):
//   POST /auth/login    body {password}                       -> {access_token, token_type, expires_in}
//   POST /chat/stream   body {message, conversation_id?} (Bearer) -> SSE (text/event-stream)
import { API_URL } from "./config";

// Error con el status HTTP adjunto, para que la UI distinga 401 (sesión) de
// otros fallos y reaccione (volver al login) sin parsear mensajes de texto.
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// status 0 = no hubo respuesta (red caída / API apagada / CORS bloqueado antes
// de que la respuesta llegue al JS). Útil para dar un mensaje claro al usuario.
function networkError(): ApiError {
  return new ApiError(
    0,
    `No se pudo contactar con el servidor. ¿Está corriendo la API en ${API_URL}?`,
  );
}

export async function login(password: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
  } catch {
    throw networkError();
  }

  if (res.status === 401) {
    throw new ApiError(401, "Contraseña incorrecta.");
  }
  if (!res.ok) {
    throw new ApiError(res.status, `Error del servidor (${res.status}).`);
  }

  const data = (await res.json()) as { access_token: string };
  return data.access_token;
}

// --- Streaming del chat (SSE) ----------------------------------------------
// Eventos que emite POST /chat/stream, uno por frame `data: {...}\n\n`:
//   {type:"meta", conversation_id}   <- siempre el primero
//   {type:"token", value}            <- fragmento incremental de la respuesta
//   {type:"tool_start", tool}        <- el agente empezó a usar una herramienta
//   {type:"tool_end", tool}          <- la herramienta terminó
//   {type:"proposal", action, description}  <- el turno cerró esperando confirmación
//   {type:"replace", value}          <- sustituye el texto ya recibido (los pasos de
//                                       herramienta se conservan)
//   {type:"error", message}          <- fallo del modelo a mitad del stream
//   {type:"done"}                    <- fin
export type StreamEvent =
  | { type: "meta"; conversation_id: number }
  | { type: "token"; value: string }
  | { type: "tool_start"; tool: string }
  | { type: "tool_end"; tool: string }
  | { type: "proposal"; action: string; description: string }
  | { type: "replace"; value: string }
  | { type: "error"; message: string }
  | { type: "done" };

// Async generator: el componente hace `for await (const ev of streamChat(...))`.
// Usamos fetch + ReadableStream (no EventSource) porque EventSource solo hace GET
// y no permite cabeceras propias — y aquí el JWT viaja en Authorization.
export async function* streamChat(
  token: string,
  message: string,
  conversationId: number | null,
): AsyncGenerator<StreamEvent> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ message, conversation_id: conversationId }),
    });
  } catch {
    throw networkError();
  }

  // El 401 llega como respuesta normal ANTES de abrir el stream (la dependencia
  // require_jwt corre primero). El llamador lo traduce a "volver al login".
  if (res.status === 401) {
    throw new ApiError(401, "Tu sesión expiró.");
  }
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, `Error del servidor (${res.status}).`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Los eventos SSE se separan por una línea en blanco (\n\n).
      let sep = buffer.indexOf("\n\n");
      while (sep !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data) {
          yield JSON.parse(data) as StreamEvent;
        }
        sep = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// --- Conversaciones (historial) --------------------------------------------
// Shapes verificados contra el backend (app/api/conversations.py + schemas):
//   GET    /conversations?limit&offset -> {items: ConversationSummary[], total, limit, offset}
//   GET    /conversations/{id}         -> {id, title, created_at, messages: MessageItem[]}
//   DELETE /conversations/{id}         -> {deleted, conversation_id}
export interface MessageItem {
  role: string; // "user" | "assistant"
  content: string;
  created_at: string;
}
export interface ConversationSummary {
  id: number;
  title: string | null;
  created_at: string;
  message_count: number;
  last_message: string | null;
  last_message_at: string | null;
}
export interface ConversationListResponse {
  items: ConversationSummary[];
  total: number;
  limit: number;
  offset: number;
}
export interface ConversationDetail {
  id: number;
  title: string | null;
  created_at: string;
  messages: MessageItem[];
}

// Helper para peticiones JSON autenticadas (GET/DELETE). El 401 lo traduce a
// ApiError para que el llamador vuelva al login; el resto a un error legible.
async function authed<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { ...(init?.headers ?? {}), Authorization: `Bearer ${token}` },
    });
  } catch {
    throw networkError();
  }
  if (res.status === 401) {
    throw new ApiError(401, "Tu sesión expiró.");
  }
  if (!res.ok) {
    // FastAPI devuelve {detail: "..."} en los errores; lo mostramos si está.
    let message = `Error del servidor (${res.status}).`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) message = body.detail;
    } catch {
      /* sin cuerpo JSON: nos quedamos con el mensaje genérico */
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

export function listConversations(
  token: string,
  limit = 100,
  offset = 0,
): Promise<ConversationListResponse> {
  return authed(token, `/conversations?limit=${limit}&offset=${offset}`);
}

export function getConversation(
  token: string,
  id: number,
): Promise<ConversationDetail> {
  return authed(token, `/conversations/${id}`);
}

export function deleteConversation(
  token: string,
  id: number,
): Promise<{ deleted: boolean; conversation_id: number }> {
  return authed(token, `/conversations/${id}`, { method: "DELETE" });
}

// --- OAuth de Google (conexión copiar-pegar loopback) -----------------------
// Shapes verificados (app/api/oauth.py):
//   GET  /oauth/google/start    -> {auth_url}
//   POST /oauth/google/callback {code} -> {connected, scope, refresh_token_received}
//   GET  /oauth/google/status   -> {connected:false} | {connected:true, token_valid, ...}
export interface GoogleStatus {
  connected: boolean;
  token_valid?: boolean;
  expires_at?: string;
  has_refresh_token?: boolean;
  scope?: string;
  updated_at?: string;
}

// Hay cuenta guardada, pero el acceso caducó y ya no se puede renovar (el backend
// borra el refresh token cuando Google lo rechaza): para Nexa es como no estar
// conectado hasta que se reconecte.
export function needsGoogleReconnect(status: GoogleStatus | null): boolean {
  return (
    !!status?.connected && status.token_valid === false && status.has_refresh_token === false
  );
}

export function getGoogleStatus(token: string): Promise<GoogleStatus> {
  return authed(token, "/oauth/google/status");
}

export function startGoogleAuth(token: string): Promise<{ auth_url: string }> {
  return authed(token, "/oauth/google/start");
}

export function connectGoogle(
  token: string,
  code: string,
): Promise<{ connected: boolean; scope?: string; refresh_token_received?: boolean }> {
  return authed(token, "/oauth/google/callback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
}

// --- Documentos (knowledge base / RAG) -------------------------------------
// Shapes verificados (app/api/documents.py):
//   POST /documents/upload  (multipart, campo "file") -> {document_id, filename, status}
//   GET  /documents                                    -> DocumentItem[]
export interface DocumentItem {
  id: number;
  filename: string;
  status: string; // pending | ready | failed
  created_at: string;
}

export function listDocuments(token: string): Promise<DocumentItem[]> {
  return authed(token, "/documents");
}

export function uploadDocument(
  token: string,
  file: File,
): Promise<{ document_id: number; filename: string; status: string }> {
  const form = new FormData();
  form.append("file", file);
  // Sin Content-Type a mano: el navegador pone el multipart/form-data con boundary.
  return authed(token, "/documents/upload", { method: "POST", body: form });
}

export function deleteDocument(
  token: string,
  id: number,
): Promise<{ deleted: boolean; document_id: number }> {
  return authed(token, `/documents/${id}`, { method: "DELETE" });
}

// --- TTS (lectura por voz, bajo demanda) -----------------------------------
// POST /tts {text, voice} (Bearer) -> audio/wav (blob). El backend es un proxy
// autenticado al servicio XTTS-v2; si está caído responde 503 y aquí lanzamos
// ApiError con el detalle, para que la UI muestre un error claro sin romper el chat.
export type Voice = "ana" | "alma";

export const VOICES: { id: Voice; label: string }[] = [
  { id: "ana", label: "Ana" },
  { id: "alma", label: "Alma" },
];

export async function synthesize(
  token: string,
  text: string,
  voice: Voice,
): Promise<Blob> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/tts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ text, voice }),
    });
  } catch {
    throw networkError();
  }
  if (res.status === 401) {
    throw new ApiError(401, "Tu sesión expiró.");
  }
  if (!res.ok) {
    let message = `No se pudo generar el audio (${res.status}).`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) message = body.detail;
    } catch {
      /* sin cuerpo JSON: nos quedamos con el genérico */
    }
    throw new ApiError(res.status, message);
  }
  return await res.blob();
}
