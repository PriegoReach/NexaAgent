import { useState } from "react";
import type { FormEvent } from "react";
import { login } from "../api";

interface LoginProps {
  onAuthenticated: () => void;
  notice: string | null;
}

export function Login({ onAuthenticated, notice }: LoginProps) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!password.trim() || loading) return;

    setLoading(true);
    setError(null);
    try {
      await login(password);
      onAuthenticated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error inesperado.");
      setLoading(false); // en éxito desmontamos, no hace falta resetear
    }
  }

  return (
    <main className="auth-screen">
      <form className="auth-card" onSubmit={handleSubmit} noValidate>
        <div className="brand brand--stacked">
          <span className="brand__mark">Nexa</span>
          <span className="brand__eyebrow">AGENTE · v0.1</span>
        </div>

        <p className="auth-card__intro">
          Tu asistente con acceso a calendario, documentos y tareas. Introduce
          la contraseña de acceso para continuar.
        </p>

        {notice && (
          <div className="banner banner--info" role="status">
            {notice}
          </div>
        )}

        <div className="field">
          <label className="field__label" htmlFor="password">
            Contraseña
          </label>
          <input
            id="password"
            className="field__input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••"
            autoComplete="current-password"
            autoFocus
            disabled={loading}
          />
          {error && (
            <p className="field__error" role="alert">
              {error}
            </p>
          )}
        </div>

        <button
          className="btn btn--primary btn--block"
          type="submit"
          disabled={loading || !password.trim()}
        >
          {loading ? "Verificando…" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
