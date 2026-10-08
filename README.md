# NexaAgent

[![CI](https://github.com/PriegoReach/NexaAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/PriegoReach/NexaAgent/actions/workflows/ci.yml)

Asistente de IA personal full-stack con **memoria, herramientas y RAG**, que corre **100 % en local** con Ollama, sin depender de APIs de LLM de pago. Responde preguntas sobre tus documentos y ejecuta acciones reales en Google Calendar, Gmail y Drive.

Este repositorio es un monorepo con el backend, el frontend y la bitácora de desarrollo:

```
Nexa/
├── nexaagent/   → Backend: FastAPI + PostgreSQL/pgvector + Redis + Celery + Ollama
├── frontend/    → Web UI: React + Vite + TypeScript
└── Docs/        → Bitácora de desarrollo (BITACORA-parte-*.md)
```

> Cada subproyecto tiene su propio README con detalle: [`nexaagent/README.md`](nexaagent/README.md) y [`frontend/README.md`](frontend/README.md).

---

## Qué hace

- **Chat con un agente tool-calling** sobre un LLM local (Ollama), con memoria de corto plazo (Redis) y de largo plazo (hechos recuperables vía pgvector, que puedes ver y olvidar desde la web).
- **RAG híbrido**: documentos troceados, embebidos en pgvector, recuperados por búsqueda vectorial + full-text y reordenados con un **cross-encoder** (`bge-reranker-v2-m3`), en GPU si la hay. Lee PDF (los escaneados, con OCR), Word (.docx), imágenes (.png, .jpg, con OCR) y texto, y al responder dice de qué documento sale cada dato.
- **Acciones del agente** mediante herramientas: tareas, webhooks, e integraciones Google (Calendar, Gmail, Drive) vía OAuth.
- **Recordatorios**: cada día, a partir de la hora que elijas, un resumen de las tareas de hoy y las vencidas, por correo (Gmail) o por el webhook.
- **Confirmación humana e idempotencia**: enviar un correo, crear, mover o borrar un evento en Calendar o borrar una tarea solo ocurre tras un "sí" explícito del usuario (antes se muestra qué se va a hacer; en un correo, destinatario, asunto y cuerpo completos). Además, el mismo correo, webhook, tarea o evento no se duplica aunque se reintente.
- **API REST** documentada (OpenAPI/Swagger) con autenticación **JWT** (cabecera Bearer o cookie de sesión httpOnly) y streaming SSE.
- **Web UI**: chat en streaming con Markdown, historial, tarjetas Sí/No para confirmar acciones, paneles de Documentos, Memoria y Conexiones (Google), lectura en voz alta y una sesión que sobrevive a recargar la página. Detalle en [`frontend/README.md`](frontend/README.md).
- **Evaluaciones**: la recuperación del RAG (Recall@k, MRR) y el enrutado a herramientas (qué herramienta elige el agente ante cada mensaje) se miden con harnesses propios; ver [Tests y evaluaciones](#tests-y-evaluaciones).

### Herramientas del agente

| Herramienta | Función |
|---|---|
| `search_knowledge_base` | Búsqueda RAG sobre los documentos ingeridos; cada fragmento lleva su archivo de origen para citarlo |
| `search_long_term_memory` | Recupera memorias de largo plazo del usuario |
| `http_get` | Petición HTTP GET a una URL pública de un dominio que hayas escrito en la conversación |
| `create_task` / `list_tasks` / `update_task` / `delete_task` | Gestión de tareas |
| `call_webhook` | Dispara un webhook configurado (un fallo definitivo se puede reintentar; uno dudoso no se repite) |
| `list_calendar_events` / `create_calendar_event` / `update_calendar_event` / `delete_calendar_event` | Google Calendar: consultar (también un día concreto), crear (con invitados), mover y borrar |
| `send_email` | Gmail |
| `list_drive_files` / `ingest_drive_file` | Listar e ingerir documentos de Google Drive (PDF, Word, imágenes, texto y Google Docs) |

---

## Arquitectura

```
┌──────────────┐   HTTP/JSON    ┌──────────────────────────────────────────┐
│   frontend   │ ─────────────► │  FastAPI (api)                             │
│ React + Vite │  cookie / JWT  │  ├─ auth · chat · documents · conversations│
└──────────────┘                │  ├─ memories · oauth · tts                 │
                                │  ├─ health · metrics                       │
                                │  └─ Agente (LangChain/LangGraph)           │
                                │      ├─ tools  ── RAG (pgvector + rerank)   │
                                │      └─ memoria corto/largo plazo           │
                                └───────┬─────────────┬───────────┬──────────┘
                                        │             │           │
                              ┌─────────▼──┐  ┌───────▼────┐  ┌───▼─────────┐
                              │ PostgreSQL │  │   Redis    │  │   Ollama    │
                              │ + pgvector │  │ memoria +  │  │ LLM local + │
                              │            │  │  broker    │  │ embeddings  │
                              └────────────┘  └─────┬──────┘  └─────────────┘
                                                    │
                                            ┌───────▼────────┐
                                            │ Celery worker  │
                                            │ ingesta, OCR,  │
                                            │ recordatorios  │
                                            └────────────────┘
```

| Componente | Rol |
|---|---|
| **FastAPI** (`api`) | Capa API async, agente, RAG y reranking (cross-encoder en GPU, o en CPU si no hay) |
| **Celery worker** | Ingesta de documentos (con OCR: tesseract + poppler), extracción de memoria y el recordatorio diario, fuera de la ruta de petición |
| **PostgreSQL + pgvector** | Conversaciones, mensajes, documentos, vectores, tareas, cuentas OAuth |
| **Redis** | Memoria de corto plazo + broker de Celery |
| **Ollama** | Inferencia local: `qwen2.5` 7B (chat con tool-calling) + `nomic-embed-text` (embeddings, 768-dim) |
| **Voz** (`tts`) | Lectura en voz alta con XTTS-v2 (voces Ana y Alma), en GPU si la hay |

---

## Requisitos

- **Docker** y **Docker Compose**.
- **GPU NVIDIA** con el runtime de Docker para GPU, recomendada (el `docker-compose.yml` reserva GPU para Ollama, el reranker dentro de `api` y la voz). **Sin GPU** también funciona, más lento: arranca con `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build`, que quita esas reservas; el reranker detecta que no hay CUDA y usa la CPU.
- **Python 3.9+**, solo para el script que crea los secretos. Si no lo tienes, ese script también se puede ejecutar con Docker (ver abajo).
- **Node 18+** para la interfaz web, que no corre en Docker (probado con Node 22).

---

## Puesta en marcha

### 1. Backend

```bash
cd nexaagent
cp .env.example .env          # ajusta modelos/zona horaria si quieres
```

**Crea los archivos de secretos** (están en `.gitignore`, nunca se versionan). El backend los lee de `nexaagent/secrets/` y `docker-compose` los monta como Docker secrets:

```bash
python scripts/init_secrets.py
```

Sin Python en el equipo, el mismo script con Docker (en PowerShell o bash, desde `nexaagent/`):

```bash
docker run --rm -it -v "${PWD}:/w" -w /w python:3.11-slim python scripts/init_secrets.py
```

El script genera `jwt_secret` y `postgres_password`, te pide la contraseña para entrar a Nexa (`auth_password`, la que envías a `/auth/login`) y deja **vacíos** los de las integraciones opcionales. Nunca sobrescribe un archivo que ya exista.

- **Google** (Calendar/Gmail/Drive): escribe las credenciales de tu cliente OAuth en `secrets/google_client_id.txt` y `secrets/google_client_secret.txt`.
- **Webhook** (`call_webhook`): escribe la URL en `secrets/webhook_url.txt`. Para probarlo sin un sistema externo, compose levanta un receptor de prueba: usa `http://webhook-receiver:9000/` y mira lo que llega con `docker compose logs webhook-receiver`.

> Créalos **antes** del primer `docker compose up`. Si falta un archivo, Docker no da error: crea una *carpeta* con ese nombre en `secrets/`. Si eso pasa, vuelve a ejecutar el script, que cambia las carpetas vacías por archivos. Con un secreto obligatorio en ese estado, la API no arranca y su log explica qué falta.

Levanta el stack:

```bash
docker compose up --build
```

> **El primer arranque tarda bastante (unos 10 min o más, según tu conexión)**: Ollama descarga los modelos (`qwen2.5` ~4.7 GB, `nomic-embed-text` ~274 MB) y el servicio de voz, XTTS-v2 (~1.8 GB). El esquema de la BD lo crea el servicio `migrate` (`alembic upgrade head`) antes de que arranquen la API y el worker.
>
> El modelo de voz XTTS-v2 tiene la licencia CPML de Coqui, que solo permite uso no comercial; compose la acepta con `COQUI_TOS_AGREED=1`.
>
> Los servicios se reinician solos si se reinicia el equipo o Docker (`restart: unless-stopped`), así que los recordatorios siguen llegando. Para pararlos: `docker compose stop`.

- API + Swagger: **http://localhost:8000/docs**
- Salud: **http://localhost:8000/health**

### 2. Interfaz web

No corre en Docker: se arranca con Node, en otra terminal y desde la raíz del repositorio.

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173 (puerto fijo; el backend tiene CORS atado a él)
```

La URL de la API se lee de `VITE_API_URL` (`.env`, por defecto `http://localhost:8000`).

---

## Uso rápido (API)

La autenticación es **JWT**: primero login, luego envía el token en la cabecera `Authorization: Bearer`. El login también deja una cookie de sesión httpOnly, que es la que usa la web.

```bash
# 1. Login → access_token
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "mi-password"}'

# 2. Chat (omite conversation_id para empezar una conversación nueva)
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hola, ¿qué puedes hacer?"}'

# 3. Subir un documento para RAG: PDF, Word, imagen o texto (lo procesa el worker)
curl -X POST http://localhost:8000/documents/upload \
  -H "Authorization: Bearer <TOKEN>" \
  -F "file=@./midoc.pdf"
```

### Endpoints principales

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/auth/login` | Devuelve un JWT a partir de la contraseña y deja la cookie de sesión de la web |
| `GET` | `/auth/session` · `POST` `/auth/logout` | Comprueba la sesión de la web · la cierra (borra la cookie) |
| `POST` | `/chat` | Chat síncrono con el agente |
| `POST` | `/chat/stream` | Chat con streaming (SSE) |
| `POST` | `/documents/upload` · `GET` `/documents` · `DELETE` `/documents/{id}` | Gestión de documentos RAG |
| `GET` | `/conversations` · `/conversations/{id}` · `DELETE` `/conversations/{id}` | Historial de conversaciones |
| `GET` | `/memories` · `DELETE` `/memories/{id}` · `DELETE` `/memories` | Ver la memoria de largo plazo, olvidar un recuerdo u olvidarlo todo |
| `POST` | `/tts` | Lectura en voz alta (proxy al servicio XTTS-v2) |
| `GET` | `/oauth/google/start` · `POST` `/oauth/google/callback` · `GET` `/oauth/google/status` | OAuth con Google |
| `GET` | `/health` · `/health/ready` · `/metrics` | Salud y observabilidad |

Todos los endpoints (salvo `/auth/login`, `/auth/logout`, `/health*` y `/metrics`) requieren el JWT: en la cabecera `Authorization: Bearer <token>` o en la cookie de sesión que deja `/auth/login`.

---

## Tests y evaluaciones

Suite de pytest en un servicio efímero con su propia BD (`nexaagent_test`), sin Ollama ni Google (los sustituye por dobles); tarda menos de un minuto:

```bash
cd nexaagent
docker compose run --rm tests
```

La **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) corre en cada pull request y en cada push a `main`: la misma suite contra un Postgres con pgvector (con torch para CPU y tesseract para los tests de OCR; no hace falta GPU ni Ollama), `ruff` para errores de código, la build del frontend y la validación de `docker-compose.yml` y del override de CPU.

Las **evaluaciones** miden lo que la suite no puede, porque dependen del modelo. Necesitan Ollama levantado:

```bash
docker compose exec api python -m app.eval.tool_routing   # qué herramienta elige el agente ante 38 mensajes
docker compose exec api python -m app.eval.retrieval      # Recall@k y MRR del RAG sobre los documentos cargados
```

Con `qwen2.5` 7B el agente elige la herramienta correcta en el 84–90 % de los casos de enrutado; varía un poco de una sesión a otra, aun con temperature 0 (detalle en la [bitácora, Parte 34](Docs/BITACORA-parte-34.md)). Los fallos típicos son preguntarle al usuario un dato que podría buscar (qué tarea, qué reunión) en vez de listarlas primero. Cuando el modelo escribe la llamada a una herramienta como texto en vez de hacerla, el agente la sustituye por un aviso. Para usar el piso del enrutado como test de regresión:

```bash
docker compose run --rm -e EVAL_REAL=1 tests pytest tests/test_tool_routing_eval.py
```

---

## Migraciones

El esquema lo gestiona **Alembic**. Las migraciones pendientes se aplican solas en cada `docker compose up`: el servicio `migrate` ejecuta `alembic upgrade head` y la API y el worker esperan a que termine bien.

Para crear una migración nueva (el proyecto las escribe a mano; ver la bitácora, Parte 6) y aplicarla sin reiniciar todo:

```bash
docker compose exec api alembic revision -m "mi cambio"
docker compose run --rm migrate
```

> Si cambias `EMBEDDING_DIM` (modelo de embeddings distinto) hay que recrear la BD: `docker compose down -v` (¡borra datos!) y volver a levantar; las migraciones crean los vectores con la dimensión de `EMBEDDING_DIM`. Si cambias el valor sin recrear la base, el servicio `migrate` lo detecta, explica qué pasa y la API no arranca.

---

## Configuración

Todo se configura por `.env` (modelos, zona horaria, CORS) y por archivos de secretos en `nexaagent/secrets/`. Los secretos se prefieren desde `/run/secrets/<nombre>` (montados por compose) y caen al valor de `.env` solo en desarrollo. Ver [`nexaagent/app/core/config.py`](nexaagent/app/core/config.py).

**Recordatorios** (en `nexaagent/.env`):

```bash
REMINDER_EMAIL=tu@correo.com   # a quién llega el resumen diario; se envía por Gmail con la cuenta de Google conectada
REMINDER_HOUR=8                # a partir de qué hora (local, la de CALENDAR_TIMEZONE)
```

El worker comprueba cada hora (Celery beat embebido, `--beat`) si hay tareas pendientes para hoy o vencidas y, a partir de esa hora, manda **un** resumen al día. Sin `REMINDER_EMAIL` usa el webhook; sin ninguno de los dos no manda nada y lo avisa en el log.

**Sesión de la web:** si sirves Nexa por HTTPS, pon `SESSION_COOKIE_SECURE=true` para que la cookie de sesión solo viaje cifrada. Con `http://localhost` debe quedarse en `false` (el valor por defecto), o el navegador no la enviaría.

Cambiar de modelo (en `nexaagent/.env`):

```bash
OLLAMA_MODEL=qwen2.5:14b             # u otro con tool-calling fiable → https://ollama.com/library
OLLAMA_EMBEDDING_MODEL=mxbai-embed-large
EMBEDDING_DIM=1024                   # debe coincidir con la salida del modelo; obliga a recrear la BD
```

> El modelo de chat debe soportar tool-calling de forma fiable. En pruebas, llama3.1 no invocaba
> las herramientas de manera consistente; por eso el proyecto usa qwen2.5 (ver [bitácora, Parte 2](Docs/BITACORA-parte-2.md)).
>
> `qwen2.5:14b` ocupa unos 9 GB de memoria de GPU, frente a unos 4.7 GB del 7B. Si la GPU también carga el
> reranker o el servicio de voz, puede no caber en tarjetas de 12 GB.

---

## Stack

**Backend:** Python 3.11 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 + pgvector · Redis · Celery · LangChain/LangGraph · Ollama · sentence-transformers + Torch (CUDA) · Tesseract + Poppler (OCR) · Alembic · PyJWT.
**Frontend:** React 18 · Vite 6 · TypeScript 5 · react-markdown.

---

## Seguridad

- Los **secretos reales** (`nexaagent/secrets/`, cualquier `.env`) están en `.gitignore` y **no** se versionan.
- **PostgreSQL, Redis y Ollama solo escuchan en `127.0.0.1`**: se pueden usar desde este equipo, pero no desde otros de la red local. PostgreSQL se publica en el **5433** (no en el 5432, para no chocar con un PostgreSQL instalado en el propio equipo). La API (`:8000`) sí escucha en todas las interfaces y exige JWT.
- **`http_get` solo consulta direcciones públicas** (nada de localhost, la red local ni los servicios internos de Docker, también tras una redirección) y **solo dominios que hayas escrito en la conversación** o que estén en `HTTP_GET_ALLOWED_DOMAINS`. Así, unas instrucciones escondidas en un documento no pueden hacer que el agente mande datos a un sitio que no pediste.
- La autenticación es JWT HS256 con caducidad; rotar `jwt_secret` invalida todos los tokens emitidos.
- La web guarda la sesión en una **cookie httpOnly y `SameSite=Strict`**: el JavaScript de la página no puede leerla (a diferencia de `localStorage`, un script inyectado no podría robarla) y el navegador no la envía en peticiones que vengan de otros sitios.
- Las credenciales OAuth estáticas (`client_id`/`client_secret`) van a secrets; los tokens OAuth dinámicos viven en la tabla `oauth_accounts` de Postgres.
