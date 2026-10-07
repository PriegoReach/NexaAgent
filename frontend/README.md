# Nexa — Frontend

Interfaz web de NexaAgent: React 18 + Vite 6 + TypeScript 5. Es un proyecto
**separado** del backend y solo habla con la API REST (FastAPI) por HTTP/JSON y SSE.

## Requisitos
- Node 18+ (probado con Node 22).
- El backend corriendo en `http://localhost:8000` (`docker compose up` en `nexaagent/`).

## Desarrollo

```bash
npm install
npm run dev          # http://localhost:5173
npm run build        # comprobación de tipos (tsc -b) + build de producción en dist/
```

El puerto 5173 es fijo: el CORS del backend solo admite ese origen (`CORS_ORIGINS`).

## Configuración
La URL de la API se lee de `VITE_API_URL` (ver `.env`). Por defecto
`http://localhost:8000`. No hay URLs escritas en el código.

## Qué incluye

- **Sesión.** El login (`POST /auth/login`) deja una cookie de sesión `httpOnly` y
  `SameSite=Strict`: sobrevive a recargar la página y el JavaScript de la web no puede
  leerla. Al abrir, la web pregunta a `GET /auth/session` si sigue dentro; «Salir»
  llama a `POST /auth/logout`. Todas las peticiones van con `credentials: "include"`.
- **Chat en streaming** (`POST /chat/stream`, SSE): el texto llega token a token, con
  indicadores de qué herramienta está usando el agente («Buscando en los
  documentos», «Consultando tu calendario»). Las respuestas se muestran en
  **Markdown** (listas, tablas, código, enlaces que abren en otra pestaña).
- **Confirmación de acciones.** Cuando el agente propone enviar un correo, crear,
  mover o borrar un evento, o borrar una tarea, aparece una tarjeta con lo que se va a
  hacer y botones **Sí / No**. El texto de la propuesta se muestra literal, tal como
  se ejecutará.
- **Historial** en la barra lateral: conversaciones recientes, abrir una anterior y
  borrarla (con confirmación).
- **Documentos.** Subir archivos (clic o arrastrar) para el RAG: PDF (también
  escaneados), Word (.docx), imágenes (.png, .jpg) y texto (.txt, .md, .csv, .json…).
  Muestra el estado de cada uno (procesando, listo, sin texto, error) y permite
  quitarlos.
- **Memoria.** Lo que Nexa aprendió de tus conversaciones: verlo, olvidar un recuerdo
  u olvidarlo todo.
- **Conexiones.** Conectar la cuenta de Google (Calendar, Gmail, Drive) y ver si
  hay que reconectarla.
- **Voz.** Cada respuesta se puede leer en voz alta (`POST /tts`, voces Ana y Alma);
  si el servicio de voz no está levantado, se avisa sin romper el chat.

## Estructura

```
src/
├── api.ts                 → cliente de la API (fetch + SSE), tipos y errores
├── App.tsx                → comprueba la sesión y elige login o espacio de trabajo
└── components/
    ├── Login.tsx
    ├── Workspace.tsx      → barra lateral + chat + paneles
    ├── Sidebar.tsx        → historial y accesos a los paneles
    ├── Chat.tsx           → streaming, Markdown, propuestas Sí/No, voz
    ├── DocumentsModal.tsx
    ├── MemoryModal.tsx
    └── ConnectionsModal.tsx
```
