import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, connectGoogle, startGoogleAuth } from "../api";
import type { GoogleStatus } from "../api";

interface ConnectionsModalProps {
  token: string;
  status: GoogleStatus | null;
  onClose: () => void;
  onConnected: () => Promise<void> | void; // refresca el estado en el padre
  onSessionExpired: () => void;
}

// Permisos que la app solicita (fijos en el backend). Se muestran para que el
// usuario sepa qué está autorizando.
const SCOPES = [
  "Calendario — consultar y crear eventos",
  "Gmail — enviar correos en tu nombre",
  "Drive — leer documentos para indexarlos",
];

// El usuario suele pegar la URL completa a la que Google le redirige
// (http://localhost/?code=XXXX&scope=...). Extraemos el code; si pegó solo el
// código, lo usamos tal cual.
function extractCode(raw: string): string {
  const s = raw.trim();
  const i = s.indexOf("code=");
  if (i === -1) return s;
  const code = s.slice(i + 5).split("&")[0].split("#")[0];
  try {
    return decodeURIComponent(code);
  } catch {
    return code;
  }
}

export function ConnectionsModal({
  token,
  status,
  onClose,
  onConnected,
  onSessionExpired,
}: ConnectionsModalProps) {
  const [reconnecting, setReconnecting] = useState(false);
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [code, setCode] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Cerrar con Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const showConnected = !!status?.connected && !reconnecting;

  // has_refresh_token=false: el backend lo deja en NULL cuando Google lo rechaza
  // (caducó o se revocó), así que ya no hay renovación automática posible.
  const canRenew = status?.has_refresh_token !== false;
  const accessHint = status?.token_valid
    ? canRenew
      ? "El acceso está activo."
      : "El acceso está activo, pero no se podrá renovar: cuando caduque tendrás que reconectar."
    : canRenew
      ? "El acceso caducó; se renovará solo al usarlo."
      : "El acceso caducó y ya no se puede renovar. Reconecta la cuenta.";

  async function beginAuth() {
    setOpening(true);
    setError(null);
    try {
      const { auth_url } = await startGoogleAuth(token);
      setAuthUrl(auth_url);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setError(err instanceof Error ? err.message : "No se pudo iniciar la autorización.");
    } finally {
      setOpening(false);
    }
  }

  async function submitCode(e?: FormEvent) {
    e?.preventDefault();
    const c = extractCode(code);
    if (!c || connecting) return;
    setConnecting(true);
    setError(null);
    try {
      await connectGoogle(token, c);
      await onConnected();
      // Éxito: vuelve a la vista de estado conectado.
      setReconnecting(false);
      setAuthUrl(null);
      setCode("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setError(err instanceof Error ? err.message : "No se pudo conectar la cuenta.");
    } finally {
      setConnecting(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Conexiones"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal__head">
          <h2 className="modal__title">Conexiones</h2>
          <button className="modal__close" type="button" onClick={onClose} aria-label="Cerrar">
            <CrossIcon />
          </button>
        </header>

        <div className="modal__body">
          <div className="conn">
            <div className="conn__row">
              <span className="conn__name">Google</span>
              {status === null ? (
                <span className="conn__badge conn__badge--idle">Comprobando…</span>
              ) : status.connected ? (
                <span className="conn__badge conn__badge--on">
                  <DotIcon /> Conectado
                </span>
              ) : (
                <span className="conn__badge">No conectado</span>
              )}
            </div>

            {showConnected ? (
              <div className="conn__detail">
                <p className="conn__hint">{accessHint}</p>
                <ul className="scope-list">
                  {SCOPES.map((s) => (
                    <li key={s}>
                      <CheckIcon />
                      {s}
                    </li>
                  ))}
                </ul>
                <button
                  className="btn btn--sm btn--ghost btn--bordered"
                  type="button"
                  onClick={() => {
                    setReconnecting(true);
                    setAuthUrl(null);
                    setError(null);
                  }}
                >
                  Reconectar
                </button>
              </div>
            ) : (
              <div className="conn__detail">
                <p className="conn__hint">
                  Conecta tu cuenta de Google para que Nexa pueda actuar en tu nombre:
                </p>
                <ul className="scope-list">
                  {SCOPES.map((s) => (
                    <li key={s}>
                      <CheckIcon />
                      {s}
                    </li>
                  ))}
                </ul>

                {!authUrl ? (
                  <button
                    className="btn btn--primary"
                    type="button"
                    onClick={beginAuth}
                    disabled={opening}
                  >
                    {opening ? "Generando enlace…" : "Conectar con Google"}
                  </button>
                ) : (
                  <ol className="steps">
                    <li>
                      Abre el consentimiento y autoriza:
                      <a
                        className="btn btn--sm btn--primary steps__link"
                        href={authUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        Abrir Google
                        <ExternalIcon />
                      </a>
                    </li>
                    <li>
                      Google te llevará a una página que <strong>no cargará</strong>{" "}
                      (<code>localhost</code>). Es normal.
                    </li>
                    <li>
                      Copia la URL de la barra de direcciones y pégala aquí:
                      <form className="conn-form" onSubmit={submitCode}>
                        <input
                          className="field__input"
                          value={code}
                          onChange={(e) => setCode(e.target.value)}
                          placeholder="http://localhost/?code=…"
                          autoFocus
                          spellCheck={false}
                          autoComplete="off"
                        />
                        <button
                          className="btn btn--primary"
                          type="submit"
                          disabled={connecting || !code.trim()}
                        >
                          {connecting ? "Conectando…" : "Conectar"}
                        </button>
                      </form>
                    </li>
                  </ol>
                )}

                {reconnecting && (
                  <button
                    className="btn btn--sm btn--ghost"
                    type="button"
                    onClick={() => {
                      setReconnecting(false);
                      setAuthUrl(null);
                      setCode("");
                      setError(null);
                    }}
                  >
                    Cancelar
                  </button>
                )}
              </div>
            )}

            {error && (
              <div className="banner banner--error" role="alert">
                {error}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* --- Iconos --- */

function CrossIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M18 6 6 18M6 6l12 12"
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

function DotIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" aria-hidden="true">
      <circle cx="5" cy="5" r="5" fill="currentColor" />
    </svg>
  );
}

function ExternalIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 4h6m0 0v6m0-6L10 14M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
