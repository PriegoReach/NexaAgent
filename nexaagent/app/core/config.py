import os
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret_file(env_value: str, secret_name: str) -> str:
    """Prefiere /run/secrets/<secret_name> (Docker secret, montado como archivo)
    sobre el valor de .env. Fallback al valor de .env si el archivo no existe
    (desarrollo local y tests siguen funcionando sin secrets montados).
    Patrón lado-archivo: el mismo que un vault usaría si despliegas multi-host.
    """
    path = f"/run/secrets/{secret_name}"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return env_value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "NexaAgent"
    environment: str = "development"

    # --- CORS (P31, frontend separado) ----------------------------------
    # Orígenes del frontend en desarrollo (Vite: 5173). El navegador BLOQUEA
    # toda petición cross-origin sin estos headers. NO usar ["*"] con
    # allow_credentials=True (inválido por spec). Lista explícita.
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    # --------------------------------------------------------------------

    # --- Autenticación JWT (Parte 12) -----------------------------------
    # AUTH_PASSWORD: contraseña que el cliente envía a /auth/login.
    # JWT_SECRET: clave HS256 para firmar tokens. DEBE ser larga y aleatoria.
    # Cambiar JWT_SECRET invalida TODOS los tokens emitidos (es lo correcto).
    auth_password: str = "change-me-in-env"
    jwt_secret: str = "change-me-in-env-with-secrets-token-urlsafe-48"
    jwt_algorithm: str = "HS256"
    jwt_expires_hours: int = 24
    # --------------------------------------------------------------------

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5"
    ollama_embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # --- Conexión a Postgres por COMPONENTES (P23) ----------------------
    # La contraseña vive en un secreto (/run/secrets/postgres_password), NO
    # embebida en una DATABASE_URL del entorno. database_url se ARMA en
    # _assemble_database_url si no se pasa explícita (caso producción). Los
    # tests SÍ la pasan por env (conftest) y entonces se respeta tal cual.
    db_user: str = "nexa"
    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "nexaagent"
    db_password: str = "nexa"   # fallback dev; el secreto lo pisa si está montado
    database_url: str = ""
    # --------------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"

    # --- Servicio TTS (XTTS-v2, contenedor propio) ----------------------
    # El backend hace de proxy autenticado; el servicio tts no se expone fuera
    # de la red interna de compose. Override por env si cambia host/puerto.
    tts_base_url: str = "http://tts:8000"

    # --- OAuth Google (P25) ---------------------------------------------
    # client_id/secret de un cliente OAuth tipo "Desktop app" (estáticos ->
    # van en secrets, mismo patrón que jwt_secret/postgres_password). Los
    # TOKENS (dinámicos) NO viven aquí: van a la tabla oauth_accounts.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Zona horaria para CREAR eventos (P26). Se envía como `timeZone` junto a un
    # dateTime SIN offset -> Google lo interpreta en esta zona. Evita hacer
    # aritmética de offsets a mano (y arrastrar zoneinfo/pytz). Ajustable por env.
    calendar_timezone: str = "America/Mexico_City"
    # --------------------------------------------------------------------

    # Tras cargar de .env, prefiere el archivo de secreto montado si existe.
    # security.py NO cambia: sigue leyendo settings.jwt_secret/auth_password;
    # solo cambia de dónde se rellenan (la fuente es invisible al consumidor).
    @field_validator("jwt_secret", mode="after")
    @classmethod
    def _jwt_secret_from_file(cls, v: str) -> str:
        return _read_secret_file(v, "jwt_secret")

    @field_validator("auth_password", mode="after")
    @classmethod
    def _auth_password_from_file(cls, v: str) -> str:
        return _read_secret_file(v, "auth_password")

    @field_validator("db_password", mode="after")
    @classmethod
    def _db_password_from_file(cls, v: str) -> str:
        return _read_secret_file(v, "postgres_password")

    @field_validator("google_client_id", mode="after")
    @classmethod
    def _google_client_id_from_file(cls, v: str) -> str:
        return _read_secret_file(v, "google_client_id")

    @field_validator("google_client_secret", mode="after")
    @classmethod
    def _google_client_secret_from_file(cls, v: str) -> str:
        return _read_secret_file(v, "google_client_secret")

    @model_validator(mode="after")
    def _assemble_database_url(self) -> "Settings":
        # Si no se pasó DATABASE_URL explícita (producción), se arma desde los
        # componentes + la contraseña del secreto. Si vino explícita (tests vía
        # conftest), se respeta sin reescribir.
        if not self.database_url:
            self.database_url = (
                f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()