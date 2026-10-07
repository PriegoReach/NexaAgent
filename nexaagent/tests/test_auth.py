"""Tests del gate de autenticación JWT (Parte 12).

Cubre el flujo /auth/login y la dependencia require_jwt sobre rutas protegidas.
No tocan Ollama ni Redis (login solo compara contraseña y firma un JWT; el GET
/conversations falla en el gate antes de tocar la BD)."""


async def test_login_success(client):
    resp = await client.post("/auth/login", json={"password": "test-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0


async def test_login_wrong_password(client):
    resp = await client.post("/auth/login", json={"password": "incorrecta"})
    assert resp.status_code == 401


async def test_login_missing_field(client):
    # Falta 'password' (requerido) → 422 de validación.
    resp = await client.post("/auth/login", json={})
    assert resp.status_code == 422


async def test_protected_route_without_token_is_401(client):
    # Sin header Authorization → 401. (El comentario de security.py dice 403,
    # pero FastAPI 0.136 devuelve 401, que además es el código correcto:
    # 401 = faltan credenciales; 403 = autenticado pero sin permiso.)
    resp = await client.get("/conversations")
    assert resp.status_code == 401


async def test_protected_route_with_bad_token_is_401(client):
    # Bearer presente pero JWT inválido → require_jwt levanta 401.
    resp = await client.get(
        "/conversations", headers={"Authorization": "Bearer no-es-un-jwt"}
    )
    assert resp.status_code == 401


async def test_protected_route_with_valid_token_passes_gate(client, auth_headers):
    # GET /conversations es de solo lectura y no toca Ollama/Redis: con token
    # válido debe responder 200 (lista vacía tras el truncate).
    resp = await client.get("/conversations", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# --- Cookie de sesión de la web -------------------------------------------
# La web no guarda el token: /auth/login lo deja en una cookie httpOnly que el
# navegador manda sola, y así la sesión sobrevive a recargar la página.

async def test_login_sets_an_httponly_session_cookie(client):
    resp = await client.post("/auth/login", json={"password": "test-password"})

    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("nexa_session=")
    for attribute in ("HttpOnly", "SameSite=strict", "Max-Age=86400", "Path=/"):
        assert attribute in cookie


async def test_the_session_cookie_authenticates(client):
    await client.post("/auth/login", json={"password": "test-password"})

    assert (await client.get("/conversations")).status_code == 200   # sin header
    session = await client.get("/auth/session")
    assert session.status_code == 200 and session.json()["authenticated"] is True


async def test_session_without_a_cookie_is_401(client):
    assert (await client.get("/auth/session")).status_code == 401


async def test_logout_clears_the_cookie(client):
    await client.post("/auth/login", json={"password": "test-password"})

    resp = await client.post("/auth/logout")

    assert resp.status_code == 204
    assert 'nexa_session=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]
    assert (await client.get("/auth/session")).status_code == 401


async def test_an_invalid_cookie_is_401(client):
    client.cookies.set("nexa_session", "no-es-un-jwt")

    assert (await client.get("/conversations")).status_code == 401
