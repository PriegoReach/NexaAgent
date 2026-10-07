"""Infraestructura de tests.

Decisiones de diseño (cerradas antes de escribir nada):

  - **BD dedicada `nexaagent_test`** en el MISMO servicio `db` (pgvector real),
    no SQLite ni mocks: así se prueba el esquema real, el cascade de las FKs y
    el SQL específico de Postgres.
  - **Migrada con Alembic** (las mismas migraciones que producción) → el esquema
    de test es exactamente el de producción, no una recreación paralela.
  - **Aislamiento vaciando las tablas** antes de cada test (no rollback): los
    endpoints abren su propia sesión con `SessionLocal()` y hacen `commit()`, así
    que una transacción externa no los envolvería. Vaciar prueba el commit y el
    cascade REALES, que es justo lo que queremos cubrir.
  - **Higiene de event loop**: se dispone el engine global tras cada test para
    que el pool no reutilice una conexión entre loops distintos (el clásico
    "Future attached to a different loop" de la Parte 2; pytest-asyncio crea un
    loop por test).

Salvaguarda: la URL se fuerza SIEMPRE a `<db>_test`. Nunca se trunca la base real.
"""
import os

import psycopg2
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

# --- 1. Forzar entorno de test ANTES de importar la app ---------------------
# pydantic-settings (app.core.config) lee el entorno al construir Settings(),
# y eso ocurre al importar `app.*`. Por eso esto va arriba del todo, antes de
# cualquier `from app ...`.
_raw_url = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://nexa:nexa@db:5432/nexaagent"
)
_url = make_url(_raw_url)
if not (_url.database or "").endswith("_test"):
    _url = _url.set(database=f"{_url.database}_test")
# OJO: str(URL) enmascara la contraseña como '***' en SQLAlchemy 2.0. Hay que
# pedir render_as_string(hide_password=False) o app/Alembic/psycopg2 intentarían
# autenticarse literalmente con '***' (FATAL: password authentication failed).
TEST_DATABASE_URL = _url.render_as_string(hide_password=False)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

os.environ["ENVIRONMENT"] = "test"
os.environ["AUTH_PASSWORD"] = "test-password"  # usado por los tests de /auth/login
os.environ.setdefault(
    "JWT_SECRET", "test-secret-not-used-in-prod-0123456789abcdef0123456789abcdef"
)

# Raíz del proyecto (carpeta nexaagent/), para localizar alembic.ini sin
# depender del directorio desde el que se lance pytest.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tablas a vaciar entre tests, las hijas antes que sus padres (FKs). Todas
# explícitas para que el reinicio de los contadores de id sea total.
TABLES = [
    "messages", "long_term_memories", "conversations", "document_chunks", "documents",
    "sent_emails", "tasks", "oauth_accounts", "webhook_events", "reminder_digests",
]


def _create_test_database() -> None:
    """Crea `<db>_test` si no existe. CREATE DATABASE no puede ir en una
    transacción, así que se usa psycopg2 en autocommit contra la BD de
    mantenimiento `postgres`."""
    sync_url = make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg2")
    admin_url = sync_url.set(database="postgres")
    conn = psycopg2.connect(
        host=admin_url.host,
        port=admin_url.port,
        user=admin_url.username,
        password=admin_url.password,
        dbname=admin_url.database,
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (sync_url.database,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{sync_url.database}"')
    finally:
        conn.close()


def _run_migrations() -> None:
    """`alembic upgrade head` contra la BD de test. env.py toma la URL de
    settings (ya apuntando a `_test`) y la pasa a psycopg2."""
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _db_setup():
    """Una vez por sesión: crea la BD de test y aplica todas las migraciones."""
    assert TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith(
        "_test"
    ), "Salvaguarda: la BD de test debe terminar en _test"
    _create_test_database()
    _run_migrations()
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_db(_db_setup):
    """Antes de cada test: tablas vacías e identidades reiniciadas.
    Después: dispone el engine global (higiene de event loop)."""
    from sqlalchemy import text

    from app.db.session import engine

    # Salvaguarda dura: jamás vaciar una base que no termine en _test.
    assert engine.url.database.endswith("_test"), f"BD inesperada: {engine.url.database}"

    # DELETE y no TRUNCATE: con tablas casi vacías es casi instantáneo, mientras que
    # TRUNCATE crea archivos nuevos en disco y los sincroniza (medido: ~2,5 s por test
    # en Docker Desktop, casi todo el tiempo de la suite). Las tablas hijas van antes
    # que sus padres por las FKs. Los contadores de id se reinician como hacía
    # RESTART IDENTITY.
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))
        # Las secuencias que pertenecen a esas tablas (las de sus columnas SERIAL);
        # reminder_digests no tiene, su clave es la fecha.
        await conn.execute(
            text(
                "SELECT setval(seq.oid::regclass, 1, false) FROM pg_class seq "
                "JOIN pg_depend dep ON dep.objid = seq.oid AND dep.deptype IN ('a', 'i') "
                "JOIN pg_class tbl ON tbl.oid = dep.refobjid "
                "WHERE seq.relkind = 'S' AND tbl.relname = ANY(CAST(:tables AS text[]))"
            ),
            {"tables": TABLES},
        )
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    """Cliente HTTP async sobre la app vía ASGITransport (mismo event loop,
    sin red). El lifespan es no-op, así que no hace falta gestionarlo."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth_token(client):
    """JWT válido obtenido por el flujo real de /auth/login. Se borra la cookie de
    sesión que pone el login: los tests que usan el token prueban el header
    Authorization, no la cookie (esa la prueban sus propios tests)."""
    resp = await client.post("/auth/login", json={"password": "test-password"})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}
