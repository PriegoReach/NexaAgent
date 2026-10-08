
## Guía para levantar NexaAgent

### En tu equipo (ya está todo configurado)

1. **Abre Docker Desktop** y espera a que diga que está corriendo.
2. **Levanta el backend** (la primera vez después de esta actualización, añade `--build`):
   ```powershell
   cd X:\Nexa\nexaagent
   docker compose up -d --build
   ```
   A partir de ahí basta con `docker compose up -d`.
3. **Comprueba que respondió:** en http://localhost:8000/health/ready debe aparecer `"status":"ready"`.
4. **Levanta la interfaz web** en otra terminal:
   ```powershell
   cd X:\Nexa\frontend
   npm run dev
   ```
   Abre **http://localhost:5173** y entra con tu contraseña, la de `nexaagent\secrets\auth_password.txt`.
5. **Para pararlo:** `docker compose stop` en `nexaagent`, y Ctrl+C en la terminal del frontend.

**Que vuelva solo tras reiniciar:** activa en Docker Desktop *Settings → General → Start Docker Desktop when you sign in*. Así el backend se levanta solo y los recordatorios siguen llegando. El frontend sí hay que arrancarlo a mano con `npm run dev`.

### En un equipo nuevo (primera vez)

**Requisitos:** Docker Desktop (mejor con GPU NVIDIA), Git, Python 3.9+ y Node 18+.

```powershell
git clone https://github.com/PriegoReach/NexaAgent.git
cd NexaAgent\nexaagent
cp .env.example .env
python scripts/init_secrets.py       # crea los secretos y te pide la contraseña para entrar
docker compose up -d --build         # la primera vez tarda: descarga ~7 GB de modelos
docker compose logs -f init-ollama   # espera a ver "Models ready!" (Ctrl+C para salir)
```

- **Sin GPU NVIDIA:** `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build`
- **Interfaz web:** `cd ..\frontend`, luego `npm install` y `npm run dev`.
- **Opcional:**
  - Google: las credenciales van en `secrets\google_client_id.txt` y `secrets\google_client_secret.txt`; después se conecta desde el panel Conexiones.
  - Recordatorios: `REMINDER_EMAIL` y `REMINDER_HOUR` en `.env`.

### Comandos útiles

| Para | Comando (desde `nexaagent`) |
|---|---|
| Correr los tests | `docker compose run --rm tests` |
| Ver qué pasa | `docker compose logs -f api worker` |
| Medir el enrutado del agente | `docker compose exec api python -m app.eval.tool_routing` |

El README tiene todo esto con más detalle. Los restos locales `_tts_probe\` y `nexaagent\.env.respaldo` siguen en tu equipo; como no me dijiste nada, no los toqué, y Git ya los ignora.

Con esto el proyecto queda cerrado.