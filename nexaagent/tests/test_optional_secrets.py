"""Tests de los secretos opcionales y del script que los prepara.

Si el archivo de un secreto no existe, Docker no falla: crea una CARPETA vacía con
ese nombre y la monta en /run/secrets/. Antes, la configuración intentaba leerla
como archivo y la API no arrancaba (IsADirectoryError), aunque fuera la de Google o
la del webhook, que son opcionales. Ahora:
  - un secreto opcional en ese estado cuenta como no configurado;
  - uno obligatorio (jwt_secret, auth_password, postgres_password) detiene el
    arranque con un mensaje claro, en vez de seguir con el valor por defecto;
  - /oauth/google/start dice que Google no está configurado, en vez de devolver
    una URL rota;
  - scripts/init_secrets.py crea todos los archivos antes del primer arranque.
"""
import importlib.util
from pathlib import Path

import pytest

from app.agent.tools.call_webhook import call_webhook
from app.core import config
from app.core.config import MissingSecret, read_secret_file, settings

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "init_secrets.py"


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "_SECRETS_DIR", str(tmp_path))
    return tmp_path


def test_reads_the_mounted_file(secrets_dir):
    (secrets_dir / "jwt_secret").write_text("  valor-del-archivo \n", encoding="utf-8")

    assert read_secret_file("valor-de-env", "jwt_secret", required=True) == "valor-del-archivo"


def test_falls_back_to_env_when_not_mounted(secrets_dir):
    assert read_secret_file("valor-de-env", "jwt_secret", required=True) == "valor-de-env"


def test_optional_secret_left_as_a_directory_counts_as_not_configured(secrets_dir):
    (secrets_dir / "google_client_id").mkdir()   # lo que monta Docker si falta el archivo

    assert read_secret_file("", "google_client_id") == ""


def test_required_secret_left_as_a_directory_stops_with_a_clear_message(secrets_dir):
    (secrets_dir / "jwt_secret").mkdir()

    with pytest.raises(MissingSecret, match=r"secrets/jwt_secret\.txt.*init_secrets\.py"):
        read_secret_file("change-me-in-env", "jwt_secret", required=True)


def test_webhook_left_as_a_directory_is_not_configured(secrets_dir, monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    (secrets_dir / "webhook_url").mkdir()

    reply = call_webhook.invoke({"message": "el informe está listo"})

    assert reply == "No hay webhook configurado (falta la URL en los secretos)."


async def test_google_start_without_credentials_says_it_is_not_configured(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")

    resp = await client.get("/oauth/google/start", headers=auth_headers)

    assert resp.status_code == 503
    assert "Google no está configurado" in resp.json()["detail"]


async def test_google_start_with_credentials_returns_the_auth_url(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "mi-cliente.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "secreto")

    resp = await client.get("/oauth/google/start", headers=auth_headers)

    assert resp.status_code == 200
    assert "mi-cliente.apps.googleusercontent.com" in resp.json()["auth_url"]


# --- scripts/init_secrets.py ------------------------------------------------
def _script():
    spec = importlib.util.spec_from_file_location("init_secrets", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(directory: Path, name: str) -> str:
    return (directory / f"{name}.txt").read_text(encoding="utf-8")


def test_script_creates_every_secret_file(tmp_path):
    report = _script().init_secrets(tmp_path, ask_password=lambda: "mi-clave")

    assert report == {
        "jwt_secret": "creado",
        "postgres_password": "creado",
        "auth_password": "creado",
        "google_client_id": "vacío",
        "google_client_secret": "vacío",
        "webhook_url": "vacío",
    }
    assert len(_read(tmp_path, "jwt_secret")) >= 60
    assert _read(tmp_path, "postgres_password")
    assert _read(tmp_path, "auth_password") == "mi-clave"
    assert _read(tmp_path, "webhook_url") == ""


def test_script_generates_the_password_when_left_empty(tmp_path):
    report = _script().init_secrets(tmp_path, ask_password=lambda: "")

    generated = report["auth_password"].removeprefix("generada: ")
    assert generated and _read(tmp_path, "auth_password") == generated


def test_script_never_overwrites_existing_files(tmp_path):
    script = _script()
    script.init_secrets(tmp_path, ask_password=lambda: "mi-clave")
    (tmp_path / "webhook_url.txt").write_text("https://hooks.example.com/x", encoding="utf-8")
    before = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()}

    report = script.init_secrets(tmp_path, ask_password=lambda: "otra")

    assert set(report.values()) == {"ya existía"}
    assert {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()} == before


def test_script_replaces_the_empty_directories_docker_leaves(tmp_path):
    (tmp_path / "jwt_secret.txt").mkdir()
    (tmp_path / "webhook_url.txt").mkdir()

    report = _script().init_secrets(tmp_path, ask_password=lambda: "mi-clave")

    assert (report["jwt_secret"], report["webhook_url"]) == ("creado", "vacío")
    assert (tmp_path / "jwt_secret.txt").is_file() and (tmp_path / "webhook_url.txt").is_file()


def test_script_leaves_directories_with_content_alone(tmp_path):
    (tmp_path / "google_client_id.txt").mkdir()
    (tmp_path / "google_client_id.txt" / "algo").write_text("x", encoding="utf-8")

    report = _script().init_secrets(tmp_path, ask_password=lambda: "mi-clave")

    assert report["google_client_id"] == "carpeta con contenido"
    assert (tmp_path / "google_client_id.txt" / "algo").exists()
