"""Prepara la carpeta secrets/ antes del primer `docker compose up`.

Docker Compose monta cada archivo de secrets/ que declara docker-compose.yml. Si uno
no existe, Docker no falla: crea una CARPETA vacía con ese nombre, y la API no
arranca (o el secreto ya no se puede escribir). Este script crea todos los archivos:
  - jwt_secret y postgres_password: valores aleatorios.
  - auth_password, la contraseña para entrar a Nexa: te la pide; si la dejas
    vacía, genera una y te la muestra.
  - google_client_id, google_client_secret y webhook_url: vacíos. Vacío quiere
    decir integración desactivada; rellénalos cuando la quieras usar.
Nunca sobrescribe un archivo que ya existe. Si encuentra una carpeta vacía que dejó
Docker, la cambia por el archivo.

Uso, desde nexaagent/:  python scripts/init_secrets.py
"""
import getpass
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

SECRETS_DIR = Path(__file__).resolve().parent.parent / "secrets"

# nombre -> cómo se genera su valor si falta
GENERATED: dict[str, Callable[[], str]] = {
    "jwt_secret": lambda: secrets.token_urlsafe(48),
    "postgres_password": lambda: secrets.token_urlsafe(24),
}
OPTIONAL = ("google_client_id", "google_client_secret", "webhook_url")


def _ask_password() -> str:
    """La contraseña de acceso, o "" para generarla (también sin terminal)."""
    if not sys.stdin.isatty():
        return ""
    first = getpass.getpass("Contraseña para entrar a Nexa (vacía = generar una): ")
    if first and getpass.getpass("Repítela: ") != first:
        print("No coinciden; genero una.")
        return ""
    return first


def init_secrets(directory: Path, ask_password: Callable[[], str] = _ask_password) -> dict[str, str]:
    """Crea en `directory` los archivos de secretos que falten. Devuelve, por
    secreto, qué hizo: "creado", "generada: <valor>", "vacío", "ya existía",
    o "carpeta con contenido" (no la toca)."""
    directory.mkdir(parents=True, exist_ok=True)
    report: dict[str, str] = {}
    for name in (*GENERATED, "auth_password", *OPTIONAL):
        path = directory / f"{name}.txt"
        if path.is_dir():
            if any(path.iterdir()):
                report[name] = "carpeta con contenido"
                continue
            path.rmdir()   # la carpeta vacía que crea Docker cuando falta el archivo
        elif path.exists():
            report[name] = "ya existía"
            continue

        if name in GENERATED:
            value, action = GENERATED[name](), "creado"
        elif name == "auth_password":
            value = ask_password()
            action = "creado"
            if not value:
                value = secrets.token_urlsafe(12)
                action = f"generada: {value}"
        else:
            value, action = "", "vacío"
        path.write_text(value, encoding="utf-8")
        report[name] = action
    return report


def main() -> int:
    report = init_secrets(SECRETS_DIR)
    print(f"\nSecretos en {SECRETS_DIR}:")
    for name, action in report.items():
        print(f"  {name + '.txt':26} {action}")
    if report.get("postgres_password") == "creado":
        print("\nSi ya tenías una base de datos de Nexa (el volumen pgdata), pon en "
              "postgres_password.txt la contraseña con la que se creó.")
    if any(action == "vacío" for action in report.values()):
        print("\nLos vacíos son integraciones desactivadas. Para usar Google "
              "(Calendar, Gmail, Drive) escribe las credenciales de tu cliente OAuth en "
              "google_client_id.txt y google_client_secret.txt; para el webhook, su URL "
              "en webhook_url.txt.")
    if any(action == "carpeta con contenido" for action in report.values()):
        print("\nHay carpetas con contenido donde debería haber archivos de secretos; "
              "revísalas a mano.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
