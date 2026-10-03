# NexaAgent

Autonomous AI agent for business automation: memory, tools and RAG, built on
FastAPI + PostgreSQL (pgvector) + Redis + Celery + **Ollama (local LLM)**.

100% local — no API keys needed, no external LLM costs.

## Architecture

- **FastAPI** — async API layer (`/chat`, `/documents`, `/health`).
- **Agent core (LangChain)** — a tool-calling agent over Ollama local models.
  - **Memory** — short-term conversation history in Redis.
  - **Tools** — knowledge-base search (RAG) and external HTTP requests.
  - **RAG** — documents are chunked, embedded, and stored in pgvector.
- **PostgreSQL + pgvector** — conversations, messages, documents and vectors.
- **Redis** — cache, short-term memory, and Celery broker.
- **Celery worker** — handles heavy document ingestion off the request path.
- **Ollama** — local LLM inference: qwen2.5 7B (chat with tool-calling) + nomic-embed-text (embeddings).

## Quick start

```bash
cp .env.example .env          # no need to change anything for local setup
docker-compose up --build
```

**First run takes a while (around 10 minutes or more, depending on your connection)** because Ollama downloads the models (qwen2.5 ~4.7GB, nomic-embed-text ~274MB).

API docs: http://localhost:8000/docs

## Try it

Authentication is **JWT Bearer**: log in first, then send the token.

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

# 3. Upload a document for RAG (parsed + embedded by the worker)
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

**Important:** If you change `EMBEDDING_DIM`, you need to recreate the database:
```bash
docker-compose down -v         # drops volumes (loses data!)
docker-compose up --build
```

See available models: https://ollama.com/library

## Pre-pulling models (optional)

To download models before first chat (faster startup):
```bash
docker-compose up -d ollama
docker exec -it nexaagent-ollama-1 ollama pull qwen2.5
docker exec -it nexaagent-ollama-1 ollama pull nomic-embed-text
docker-compose up api worker
```

## Where to extend

- `app/agent/tools/` — add a file per new tool, register it in `tools/__init__.py`.
- `app/agent/memory.py` — add long-term memory (summaries recalled via pgvector).
- `app/rag/` — tune chunking, add re-ranking or metadata filters.
- `app/core/security.py` — replace the API-key gate with OAuth2/JWT.

## Migrations (production)

Dev mode auto-creates tables on startup. For production, use Alembic:

```bash
alembic revision --autogenerate -m "init"
alembic upgrade head
```

## Switching to OpenAI / Anthropic

If you want to use a cloud LLM instead:

1. Replace `langchain-ollama` with `langchain-openai` in `pyproject.toml`
2. Update `app/core/config.py` with OpenAI settings
3. Update `app/agent/orchestrator.py` to use `ChatOpenAI`
4. Update `app/rag/embeddings.py` to use `OpenAIEmbeddings`
5. Remove the `ollama` service from `docker-compose.yml`

## Troubleshooting

**"Connection refused" on first run**: Ollama takes ~30s to start and download models. Wait for the logs to show "models downloaded successfully", then retry.

**Out of memory**: Ollama needs ~5GB of (V)RAM for qwen2.5 7B. If you're on a resource-constrained machine, use a smaller model like `qwen2.5:3b` (~2GB), at the cost of less reliable tool-calling.

**Slow responses**: First query is always slower (model loading). Subsequent queries are fast (~1-2s).
