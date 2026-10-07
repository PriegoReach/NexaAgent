"""Comprobación del esquema tras migrar: la dimensión de los vectores.

Las columnas `embedding` se crean con la dimensión de EMBEDDING_DIM del momento en
que corrió la migración. Si después se cambia EMBEDDING_DIM (otro modelo de
embeddings) sin recrear la base, cada inserción falla con un error críptico de
pgvector ("expected 768 dimensions, not 1024"). El servicio `migrate` ejecuta esto
tras `alembic upgrade head`: si no coinciden, falla con una explicación y api y
worker no arrancan.

Uso:  python -m app.db.schema_check
"""
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import settings

_TABLES = ("document_chunks", "long_term_memories")


def stored_dimensions(conn) -> dict[str, int]:
    """Dimensión de la columna `embedding` de cada tabla que exista. En pgvector,
    el atttypmod de una columna vector(n) es n."""
    rows = conn.execute(
        text(
            "SELECT c.relname, a.atttypmod FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE a.attname = 'embedding' AND NOT a.attisdropped "
            "AND c.relname = ANY(:tables)"
        ),
        {"tables": list(_TABLES)},
    )
    return {row[0]: row[1] for row in rows}


def mismatch_message(dimensions: dict[str, int], expected: int) -> str | None:
    wrong = {table: dim for table, dim in dimensions.items() if dim != expected}
    if not wrong:
        return None
    found = ", ".join(f"{table}: {dim}" for table, dim in sorted(wrong.items()))
    return (
        f"EMBEDDING_DIM={expected}, pero la base guarda vectores de otra dimensión "
        f"({found}). Pasa al cambiar de modelo de embeddings: los vectores viejos no "
        "sirven con el nuevo. Vuelve a poner el EMBEDDING_DIM anterior en .env, o "
        "recrea la base con `docker compose down -v` (borra los datos) y vuelve a "
        "levantar el stack."
    )


def main() -> int:
    engine = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            message = mismatch_message(stored_dimensions(conn), settings.embedding_dim)
    finally:
        engine.dispose()
    if message:
        print(message, file=sys.stderr)
        return 1
    print(f"Esquema correcto: vectores de {settings.embedding_dim} dimensiones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
