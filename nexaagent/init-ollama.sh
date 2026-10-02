#!/bin/bash
export OLLAMA_HOST=http://ollama:11434

echo "⏳ Waiting for Ollama to start..."
until ollama list >/dev/null 2>&1; do
  sleep 2
done

echo "✓ Ollama is ready"
echo "📥 Pulling models (this may take a few minutes on first run)..."

echo "  - Downloading ${OLLAMA_MODEL:-qwen2.5}..."
ollama pull ${OLLAMA_MODEL:-qwen2.5}

echo "  - Downloading ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text}..."
ollama pull ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text}

echo "✅ Models ready!"