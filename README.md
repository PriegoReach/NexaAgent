# NexaAgent

Agente de IA autónomo para automatización empresarial: **memoria, herramientas y RAG**, corriendo **100 % en local** con Ollama — sin claves de API ni costes de LLM en la nube.

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

- **Chat con un agente tool-calling** sobre un LLM local (Ollama), con memoria de corto plazo (Redis) y de largo plazo (resúmenes recuperables vía pgvector).
- **RAG híbrido**: documentos troceados, embebidos en pgvector, recuperados por búsqueda vectorial + full-text y reordenados con un **cross-encoder** (`bge-reranker-v2-m3`) en GPU.
- **Acciones del agente** mediante herramientas: tareas, webhooks, e integraciones Google (Calendar, Gmail, Drive) vía OAuth.
- **API REST** documentada (OpenAPI/Swagger) con autenticación **JWT Bearer** y streaming SSE.
- **Web UI** mínima (login + chat) que consume la API por HTTP/JSON.

### Herramientas del agente

| Herramienta | Función |
|---|---|
| `search_knowledge_base` | Búsqueda RAG sobre los documentos ingeridos |
| `search_long_term_memory` | Recupera memorias de largo plazo del usuario |
| `http_get` | Petición HTTP GET a una URL |
| `create_task` / `list_tasks` / `update_task` / `delete_task` | Gestión de tareas |
| `call_webhook` | Dispara un webhook configurado |
| `list_calendar_events` / `create_calendar_event` | Google Calendar |
| `send_email` | Gmail |
| `list_drive_files` / `ingest_drive_file` | Listar e ingerir documentos de Google Drive |

---

## Arquitectura

```
┌──────────────┐   HTTP/JSON    ┌──────────────────────────────────────────┐
│   frontend   │ ─────────────► │  FastAPI (api)                             │
│ React + Vite │   JWT Bearer   │  ├─ auth · chat · documents · conversations│
└──────────────┘                │  ├─ oauth · health · metrics               │
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
                                            │ ingesta pesada │
                                            └────────────────┘
```

| Componente | Rol |
|---|---|
| **FastAPI** (`api`) | Capa API async, agente, RAG y reranking (cross-encoder en GPU) |
| **Celery worker** | Ingesta de documentos fuera de la ruta de petición |
| **PostgreSQL + pgvector** | Conversaciones, mensajes, documentos, vectores, tareas, cuentas OAuth |
| **Redis** | Memoria de corto plazo + broker de Celery |
| **Ollama** | Inferencia local: `qwen2.5` 7B (chat con tool-calling) + `nomic-embed-text` (embeddings, 768-dim) |

---

## Requisitos

- **Docker** y **Docker Compose**.
- **GPU NVIDIA** con el runtime de Docker para GPU (el `docker-compose.yml` reserva GPU para Ollama y para el reranker dentro de `api`). Sin GPU, quita los bloques `deploy.resources.reservations.devices` del compose.
- **Node 18+** solo si vas a desarrollar el frontend (probado con Node 22).

---

## Puesta en marcha

### 1. Backend

```bash
cd nexaagent
cp .env.example .env          # ajusta modelos/zona horaria si quieres
```

**Crea los archivos de secretos** (están en `.gitignore`, nunca se versionan). El backend los lee de `nexaagent/secrets/` y `docker-compose` los monta como Docker secrets:

```bash
mkdir -p secrets
python -c "import secrets;print(secrets.token_urlsafe(48))" > secrets/jwt_secret.txt
printf 'mi-password'      > secrets/auth_password.txt      # la que envías a /auth/login
printf 'nexa'             > secrets/postgres_password.txt  # debe coincidir con el rol de la BD
# Solo si usas las integraciones Google (Calendar/Gmail/Drive):
printf 'TU_CLIENT_ID'     > secrets/google_client_id.txt
printf 'TU_CLIENT_SECRET' > secrets/google_client_secret.txt
# Solo si usas la tool call_webhook:
printf 'https://...'      > secrets/webhook_url.txt
```

Levanta el stack:

```bash
docker compose up --build
```

> **El primer arranque tarda bastante (unos 10 min o más, según tu conexión)**: Ollama descarga los modelos (`qwen2.5` ~4.7 GB, `nomic-embed-text` ~274 MB). El esquema de la BD lo aplica Alembic (`alembic upgrade head`).

- API + Swagger: **http://localhost:8000/docs**
- Salud: **http://localhost:8000/health**

### 2. Frontend (opcional)

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173 (puerto fijo; el backend tiene CORS atado a él)
```

La URL de la API se lee de `VITE_API_URL` (`.env`, por defecto `http://localhost:8000`).

---

## Uso rápido (API)

La autenticación es **JWT Bearer**: primero login, luego usa el token.

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

# 3. Subir un documento para RAG (lo procesa el worker)
curl -X POST http://localhost:8000/documents/upload \
  -H "Authorization: Bearer <TOKEN>" \
  -F "file=@./midoc.pdf"
```

### Endpoints principales

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/auth/login` | Devuelve un JWT a partir de la contraseña |
| `POST` | `/chat` | Chat síncrono con el agente |
| `POST` | `/chat/stream` | Chat con streaming (SSE) |
| `POST` | `/documents/upload` · `GET` `/documents` · `DELETE` `/documents/{id}` | Gestión de documentos RAG |
| `GET` | `/conversations` · `/conversations/{id}` · `DELETE` `/conversations/{id}` | Historial de conversaciones |
| `GET` | `/oauth/google/start` · `POST` `/oauth/google/callback` · `GET` `/oauth/google/status` | OAuth con Google |
| `GET` | `/health` · `/health/ready` · `/metrics` | Salud y observabilidad |

Todos los endpoints (salvo `/auth/login`, `/health*` y `/metrics`) requieren `Authorization: Bearer <token>`.

---

## Tests

Suite de pytest en un servicio efímero con su propia BD (`nexaagent_test`):

```bash
cd nexaagent
docker compose run --rm tests
```

---

## Migraciones

El esquema lo gestiona **Alembic**. Para crear/aplicar migraciones:

```bash
docker compose exec api alembic revision --autogenerate -m "mi cambio"
docker compose exec api alembic upgrade head
```

> Si cambias `EMBEDDING_DIM` (modelo de embeddings distinto) hay que recrear la BD: `docker compose down -v` (¡borra datos!) y volver a levantar.

---

## Configuración

Todo se configura por `.env` (modelos, zona horaria, CORS) y por archivos de secretos en `nexaagent/secrets/`. Los secretos se prefieren desde `/run/secrets/<nombre>` (montados por compose) y caen al valor de `.env` solo en desarrollo. Ver [`nexaagent/app/core/config.py`](nexaagent/app/core/config.py).

Cambiar de modelo (en `nexaagent/.env`):

```bash
OLLAMA_MODEL=qwen2.5:14b             # u otro con tool-calling fiable → https://ollama.com/library
OLLAMA_EMBEDDING_MODEL=mxbai-embed-large
EMBEDDING_DIM=1024                   # debe coincidir con la salida del modelo
```

> El modelo de chat debe soportar tool-calling de forma fiable. En pruebas, llama3.1 no invocaba
> las herramientas de manera consistente; por eso el proyecto usa qwen2.5 (ver [bitácora, Parte 2](Docs/BITACORA-parte-2.md)).
>
> `qwen2.5:14b` ocupa unos 9 GB de memoria de GPU, frente a unos 4.7 GB del 7B. Si la GPU también carga el
> reranker o el servicio de voz, puede no caber en tarjetas de 12 GB.

---

## Stack

**Backend:** Python 3.11 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 + pgvector · Redis · Celery · LangChain/LangGraph · Ollama · sentence-transformers + Torch (CUDA) · Alembic · PyJWT.
**Frontend:** React 18 · Vite 6 · TypeScript 5.

---

## Seguridad

- Los **secretos reales** (`nexaagent/secrets/`, cualquier `.env`) están en `.gitignore` y **no** se versionan.
- La autenticación es JWT HS256 con caducidad; rotar `jwt_secret` invalida todos los tokens emitidos.
- Las credenciales OAuth estáticas (`client_id`/`client_secret`) van a secrets; los tokens OAuth dinámicos viven en la tabla `oauth_accounts` de Postgres.
