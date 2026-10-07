import { useEffect, useState } from "react";
import { checkSession, logout } from "./api";
import { Login } from "./components/Login";
import { Workspace } from "./components/Workspace";

// La sesión vive en una cookie httpOnly que pone el backend al iniciar sesión:
// el JavaScript de la página nunca ve el token (ni lo guarda en localStorage) y
// la sesión sobrevive a recargar. Al cargar se pregunta si sigue viva.
type Session = "checking" | "in" | "out";

export default function App() {
  const [session, setSession] = useState<Session>("checking");
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    checkSession()
      .then((ok) => {
        if (!cancelled) setSession(ok ? "in" : "out");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setNotice(err instanceof Error ? err.message : null);
        setSession("out");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function handleSignOut(message?: string) {
    void logout();
    setSession("out");
    setNotice(message ?? null);
  }

  if (session === "checking") {
    return <main className="auth-screen" aria-busy="true" />;
  }

  if (session === "out") {
    return (
      <Login
        notice={notice}
        onAuthenticated={() => {
          setSession("in");
          setNotice(null);
        }}
      />
    );
  }

  return (
    <Workspace
      onSignOut={() => handleSignOut()}
      onSessionExpired={() =>
        handleSignOut("Tu sesión expiró. Inicia sesión de nuevo.")
      }
    />
  );
}
