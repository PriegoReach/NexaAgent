# NexaAgent

Autonomous AI agent for business automation: memory, tools and RAG, built on
FastAPI + PostgreSQL (pgvector) + Redis + Celery + **Ollama (local LLM)**.

100% local — no API keys needed, no external LLM costs.

## Architecture

- **FastAPI** — async API layer (`/auth`, `/chat`, `/documents`, `/conversations`,
  `/memories`, `/oauth`, `/tts`, `/health`, `/metrics`).
- **Agent core (LangChain/LangGraph)** — a tool-calling agent over Ollama local models.
  - **Memory** — short-term history in Redis (rebuilt from Postgres when it expires) and
    long-term facts extracted every few turns and recalled via pgvector.
  - **Tools** — 15 of them: documents (RAG), long-term memory, tasks, Google Calendar,
    Gmail and Drive, a webhook and HTTP GET. Irreversible actions (sending an email,
    changing the calendar, deleting a task) only run after an explicit "yes".
  - **RAG** — PDF (scanned ones via OCR), Word (.docx), PNG/JPEG images (OCR) and plain
    text are chunked, embedded and stored in pgvector. Hybrid search (vector +
    full-text, RRF) re-ranked by a cross-encoder; each fragment carries its source file
    so the agent can cite it.
- **PostgreSQL + pgvector** — conversations, messages, documents, vectors, tasks, OAuth accounts.
- **Redis** — short-term memory, pending confirmations, and Celery broker.
- **Celery worker** — document ingestion and memory extraction off the request path, plus
  the daily reminder (embedded beat).
- **Ollama** — local LLM inference: qwen2.5 7B (chat with tool-calling) + nomic-embed-text (embeddings).

## Quick start

```bash
cp .env.example .env          # no need to change anything for local setup
python scripts/init_secrets.py   # creates secrets/: random keys, asks for your login password (Python 3.9+)
docker compose up --build
```

`init_secrets.py` leaves the optional integrations empty (= disabled): to use Google, write your
OAuth client credentials to `secrets/google_client_id.txt` and `secrets/google_client_secret.txt`;
for the webhook, its URL to `secrets/webhook_url.txt`. Run it **before** the first `up`: if a
secret file is missing, Docker silently creates an empty *directory* with that name instead.

**First run takes a while (around 10 minutes or more, depending on your connection)** because Ollama downloads the models (qwen2.5 ~4.7GB, nomic-embed-text ~274MB).

No NVIDIA GPU? Add the CPU override, which drops the GPU reservations (slower, but it works):
```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build
```

API docs: http://localhost:8000/docs

## Try it

Authentication is a **JWT**: log in first, then send it as `Authorization: Bearer <token>`.
(`/auth/login` also sets an httpOnly session cookie; that is what the web UI uses.)

```bash
# 1. Log in -> access_token (the password is the one in secrets/auth_password.txt)
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "my-password"}'

# 2. Chat (starts a new conversation when conversation_id is omitted)
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, what can you do?"}'

# 3. Upload a document for RAG: PDF, Word, PNG/JPEG or text (parsed + embedded by the worker)
curl -X POST http://localhost:8000/documents/upload \
  -H "Authorization: Bearer <TOKEN>" \
  -F "file=@./mydoc.pdf"
```

## Choosing models

By default NexaAgent uses:
- **Chat model**: `qwen2.5` 7B (reliable tool-calling)
- **Embedding model**: `nomic-embed-text` (768-dim, optimized for RAG)

To change models, edit `.env`:
```bash
OLLAMA_MODEL=qwen2.5:14b       # or another model with reliable tool-calling
OLLAMA_EMBEDDING_MODEL=mxbai-embed-large
EMBEDDING_DIM=1024             # must match your embedding model's output
```

> The chat model must support tool-calling reliably. In testing, llama3.1 did not invoke
> the tools consistently; that is why the project uses qwen2.5 (see [dev log, Part 2](../Docs/BITACORA-parte-2.md)).
>
> `qwen2.5:14b` takes about 9 GB of GPU memory, versus about 4.7 GB for the 7B. If the GPU also loads the
> reranker or the TTS service, it may not fit on 12 GB cards.

**Important:** If you change `EMBEDDING_DIM`, you need to recreate the database (the migrations
create the vector columns with the configured dimension):
```bash
docker compose down -v         # drops volumes (loses data!)
docker compose up --build
```
If you change it without recreating the database, the `migrate` service detects the mismatch,
explains it, and the API does not start.

See available models: https://ollama.com/library

## Evaluations

Two harnesses measure the parts a model change can break (they need Ollama running):

```bash
docker compose exec api python -m app.eval.tool_routing   # which tool the agent picks, 38 messages
docker compose exec api python -m app.eval.retrieval      # Recall@k and MRR over the loaded documents
```

With qwen2.5 7B the agent picks the right first tool in 84–90% of the routing cases (it
varies a little between sessions even at temperature 0). The misses are mostly asking
the user for an id instead of looking it up first; see the [dev log, Part 34](../Docs/BITACORA-parte-34.md).

## Pre-pulling models (optional)

To download models before first chat (faster startup):
```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5
docker compose exec ollama ollama pull nomic-embed-text
docker compose up -d
```

## Where to extend

- `app/agent/tools/` — add a file per new tool, register it in `tools/__init__.py`, and add
  cases to `app/eval/datasets/tool_routing.yaml` to measure that the agent picks it.
- `app/agent/memory_extraction.py` / `app/rag/memory_retriever.py` — what long-term memory
  keeps and how it is recalled.
- `app/rag/` — `ingest.py` decides how each file type is read and chunked; `retriever.py`
  and `reranker.py` the search.
- `app/core/security.py` — JWT auth for a single client (password login at `/auth/login`); extend it to multiple users with per-user claims.

## Migrations

The schema is managed by Alembic. Pending migrations run automatically on every
`docker compose up`: the one-shot `migrate` service runs `alembic upgrade head`, and
`api` and `worker` wait until it finishes successfully.

To add a migration (this project writes them by hand) and apply it without restarting everything:

```bash
docker compose exec api alembic revision -m "my change"
docker compose run --rm migrate
```

## Switching to OpenAI / Anthropic

If you want to use a cloud LLM instead:

1. Replace `langchain-ollama` with `langchain-openai` in `pyproject.toml`
2. Update `app/core/config.py` with OpenAI settings
3. Update `app/agent/orchestrator.py` to use `ChatOpenAI`
4. Update `app/rag/embeddings.py` to use `OpenAIEmbeddings`
5. Remove the `ollama` service from `docker-compose.yml`

## Troubleshooting

**"Connection refused" on first run**: Ollama takes a while to start and download the models. Wait until `docker compose logs init-ollama` shows "Models ready!", then retry.

**Out of memory**: Ollama needs ~5GB of (V)RAM for qwen2.5 7B. If you're on a resource-constrained machine, use a smaller model like `qwen2.5:3b` (~2GB), at the cost of less reliable tool-calling.

**Slow responses**: The first query is always slower (model and reranker loading). After that, answers take a few seconds.

**Nexa did not come back after a reboot**: the services restart on their own (`restart: unless-stopped`) once Docker is running; on Windows/macOS, enable "Start Docker Desktop when you sign in".
