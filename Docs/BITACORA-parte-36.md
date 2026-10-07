# NexaAgent — Bitácora de desarrollo (Parte 36)

> Cierre de la **Fase 3 (Interfaz)**. Contenido propio: **subida de documentos al RAG** desde la
> UI — la última pieza del MVP. Y, por ser cierre de fase, un **balance del arco completo**
> (P31→P36): de un sistema que solo hablaba por `curl` a una interfaz web que cubre todo el ciclo
> del agente — login, chat en streaming, confirmación visual de acciones irreversibles, historial,
> OAuth, e ingesta de documentos. Verificada **end-to-end en el navegador**, no solo a nivel de
> protocolo. NexaAgent tiene cara.

**Estado al cierre de la Parte 36 — y de la Fase 3:** el MVP de interfaz completo y verificado en el navegador. **Subida de documentos** (`POST /documents/upload`, multipart) con dropzone, badges de estado, y sondeo automático cada 2.5s mientras haya un `pending` (la ingesta corre en Celery, asíncrona) — para de sondear cuando ninguno queda pendiente. Un añadido mínimo de backend (`GET /documents`, espejo del de conversaciones) para que la UI pudiera leer el progreso. **El recorrido completo verificado en pantalla:** login → streaming con pasos de herramienta → Conexiones verde → "envía un correo" → tarjeta → clic en "Sí, enviar" → el correo llega de verdad → subir documento → pasa a "Listo" → preguntarle por su contenido. Por primera vez, el ciclo del agente —recibir orden, mostrar trabajo, confirmar acción irreversible con un clic, ejecutarla contra Google— ocurre desde una cara, no `curl`. La Fase 3 cierra con tres cambios de backend en todo el arco, todos mínimos y aditivos.

---

## 1. La subida de documentos (contenido propio de P36)

`POST /documents/upload` es multipart (campo `file`), devuelve `{document_id, filename, status:"pending"}`, y la ingesta corre en el **worker de Celery** (parse → chunk → embed → store) — asíncrona, igual que la ingesta de la Fase 1. El parser maneja PDF y texto UTF-8 (.txt, .md, .csv, .json); binarios como .docx no. Estados: `pending` → `ready` | `failed`.

**El añadido de backend que faltaba:** no había forma de *leer* el estado de un documento, así que la UI no podría mostrar progreso. Siguiendo el patrón de toda la fase (una señal mínima y aditiva cuando la UI la necesita, como el evento `proposal` de P33), se añadió **`GET /documents`** — lista con `id, filename, status, created_at` — espejo del endpoint de conversaciones. Dos archivos.

**El frontend:**
- `uploadDocument` arma el `FormData` y **deja que el navegador ponga el `multipart/form-data` con su boundary** (sin `Content-Type` a mano — ponerlo rompería el boundary, error clásico de upload).
- `DocumentsModal`: dropzone (arrastrar o clic, varios archivos), lista con badges ("Procesando…" spinner, "Listo" check, "Error" rojo), y **sondeo automático**: mientras haya algún doc `pending`, refresca cada 2.5s; **para cuando ninguno queda pendiente**. Así la transición `pending`→`ready` se ve en vivo sin recargar — necesario porque la ingesta es asíncrona (Celery), la UI no recibe push, tiene que sondear, y hacerlo solo mientras hay trabajo es lo eficiente.
- Nota honesta de formatos en la UI ("PDF y texto").

Verificado end-to-end: subir un `.txt` → `{document_id:26, status:pending}` → `GET /documents` lo muestra `pending` → `ready` en ~2s (el worker lo parseó, generó embeddings, lo almacenó). Y el círculo RAG: preguntarle al agente por el contenido del doc subido → responde desde `search_knowledge_base`.

---

## 2. Balance del arco — la Fase 3 (Interfaz), cerrada

De `curl` a una cara, parte por parte:

| Parte | Hito | La pieza / lección que añadió |
|---|---|---|
| **P31** | CORS + scaffold + login + chat síncrono | El MVP que prueba el camino (frontend separado habla con el backend). CORS como prerequisito absoluto. |
| **P32** | Streaming visible | El stream SSE (`fetch`+`getReader`, no `EventSource` que no manda JWT). Pasos de herramienta en vivo — se ve trabajar al agente. |
| **P33** | Confirmación visual | Tarjetas Sí/No para acciones irreversibles. El evento `proposal` (cambio mínimo de backend); el clic manda "sí" que la heurística de P24 ya interpreta — sin camino de ejecución nuevo. |
| **P34** | Historial de conversaciones | Workspace de dos columnas. La separación de estado (remontar al cambiar de conversación vs no remontar al crear al vuelo) que evita borrar el chat en curso. |
| **P35** | OAuth de Google desde la UI | El enlace `<a target="_blank">` real (no `window.open` tras await — esquiva el bloqueador). Desbloqueó probar en vivo las tarjetas externas de P33. |
| **P36** | Subida de documentos | Dropzone + sondeo de estado. `GET /documents` (cambio mínimo de backend). El RAG manual con cara. |

**Lo que la Fase 3 logró:** el sistema dejó de ser `curl`. El frontend separado (React+Vite+TS, en `X:\Nexa\frontend`, comunicación solo por REST/JSON con CORS y JWT) cubre **todo el ciclo del agente** — autenticarse, conversar viendo el trabajo en vivo, confirmar acciones irreversibles con un clic, navegar el historial, conectar Google, y alimentar el RAG. Y se verificó **end-to-end en el navegador**: el flujo completo, incluido mandar un correo real con clic en "Sí, enviar", funciona en pantalla.

**El hilo conductor de la fase, en retrospectiva — la validación del backend:** los tres cambios de backend en todo el arco (CORS en P31, el evento `proposal` en P33, `GET /documents` en P36) fueron **todos mínimos y aditivos**. Ninguno tocó la lógica del agente, las acciones, la seguridad, ni la idempotencia. Eso es la mejor validación retrospectiva de la Fase 2: **un backend bien diseñado se deja poner cara sin reescribirse** — la interfaz solo necesitó *exponer* señales que ya existían (el estado de un pending, el de un documento) y abrir CORS. Si la Fase 2 hubiera estado mal estructurada, la Fase 3 habría exigido refactors profundos; no los exigió.

**El método cruzó al frontend intacto:**
- **"Confirmado, no supuesto"**: leer los schemas del backend antes de codear cada cliente (P31 `access_token`, P32 el shape del stream, P36 el multipart) — evitó el error fácil de asumir nombres de campos.
- **Fail-safe**: `strictPort` en Vite (P31), la heurística de confirmación intacta (P33), el sondeo que para solo (P36).
- **La verificación honesta sobre su alcance**: cada parte se verificó a nivel de protocolo (lo que Claude Code podía hacer sin GUI), y la confirmación visual se reconoció como pendiente hasta hacerse de verdad en este cierre.

**El perfil del 7B, una vez más (dato para el 14B):** durante P33 el 7B a veces narró una herramienta en vez de llamarla — el mismo quirk de routing de P24-P27. La UI lo refleja fielmente (la tarjeta aparece cuando una tool confirmable *realmente* propone), no lo enmascara. Es trabajo de backend (prompt/modelo), separado de la interfaz — y suma a las señales del 14B sin resolverlo aquí.

---

## 3. Aprendizajes clave

1. **Un backend bien diseñado se deja poner cara sin reescribirse.** Toda la Fase 3 necesitó tres cambios de backend, mínimos y aditivos (CORS, un evento, un GET) — ninguno tocó la lógica del agente. La interfaz expuso señales que ya existían. La validación retrospectiva de que la Fase 2 estaba bien estructurada.

2. **Exponer una señal mínima > parsear texto en el frontend.** P33 (el evento `proposal`) y P36 (`GET /documents`) resolvieron "la UI necesita saber X" añadiendo una señal limpia en el backend, no haciendo que el frontend infiera X de forma frágil. Cuando el frontend necesita un dato que el backend tiene, exponerlo es más robusto que adivinarlo.

3. **El clic es una cara para el "sí", no un camino de ejecución nuevo.** La tarjeta de confirmación manda un `"sí"`/`"no"` que la heurística determinística de P24 ya interpreta — la UI no añadió un camino al efecto irreversible, solo hizo más claro y seguro disparar el existente. Extender un sistema sin debilitar sus garantías.

4. **Los gestos del navegador importan** (`<a>` vs `window.open` tras await). El pop-up de OAuth se bloquearía en silencio con `window.open` tras un await (ya no es "gesto directo"); un enlace real lo esquiva. Detalles del medio (el navegador) que solo se conocen por tropezar o pensar con cuidado.

5. **El sondeo para trabajo asíncrono se acota a mientras hay trabajo.** La ingesta corre en Celery (la UI no recibe push), así que sondea — pero solo mientras haya un `pending`, parando cuando ninguno queda. Mostrar progreso de un proceso asíncrono sin sondear indefinidamente.

6. **La verificación de protocolo y la visual son distintas, y el cierre necesita ambas.** Cada parte se verificó a nivel HTTP/SSE (qué llega — sólido sin GUI); el cierre de fase exigió la confirmación visual (cómo se ve, qué hace con clics reales) que solo el navegador da. Reconocer el hueco y cerrarlo antes de declarar la fase terminada.

7. **El ciclo completo solo se ve cuando las piezas se integran.** Cada parte probada aislada; el valor real —mandar un correo con un clic, ver el streaming, subir y preguntar— solo se confirma con las capas juntas en pantalla. La integración es su propia verificación.

---

## 4. Comandos de referencia (nuevos de esta parte)

### Arrancar el sistema completo (backend + frontend)

```powershell
# backend (Docker)
cd X:\Nexa\nexaagent
docker compose up -d
# frontend (local, dev)
cd X:\Nexa\frontend
npm run dev   # http://localhost:5173
```

### Verificar la subida de documentos

```powershell
# subir (multipart — el navegador pone el boundary)
curl.exe -X POST http://localhost:8000/documents/upload -H "Authorization: Bearer <TOKEN>" -F "file=@documento.txt"
# ver estado (pending -> ready)
curl.exe http://localhost:8000/documents -H "Authorization: Bearer <TOKEN>"
```

---

## 5. Estado y siguientes pasos

### La Fase 3 (Interfaz): CERRADA ✅

- ✅ **P31** — CORS + scaffold React/Vite/TS + login (JWT) + chat síncrono.
- ✅ **P32** — Streaming visible (tokens + pasos de herramienta en vivo).
- ✅ **P33** — Confirmación visual (tarjetas Sí/No para acciones irreversibles).
- ✅ **P34** — Historial de conversaciones (lista, retomar, borrar).
- ✅ **P35** — OAuth de Google desde la UI.
- ✅ **P36** — Subida de documentos al RAG.

El frontend separado cubre todo el ciclo del agente, verificado end-to-end en el navegador. Tres cambios de backend en todo el arco, todos mínimos y aditivos.

### El estado del proyecto completo

Las tres fases cerradas: **Fase 1 (saber — RAG)**, **Fase 2 (actuar — acciones con confirmación, OAuth, integraciones)**, **Fase 3 (interfaz)**. Más la memoria de largo plazo (recordar) y la extensión Drive→RAG y la recuperación cross-lingual. NexaAgent es un sistema completo de punta a punta: sabe, recuerda, actúa, y tiene una cara para usarlo.

### Deudas / pendientes 🔜

- **Dockerizar el frontend** (producción): hoy corre local con `npm run dev`; un servicio en compose para desplegarlo cuando se quiera.
- **Persistencia de sesión:** el JWT se pierde al recargar (estado de React); la forma correcta (httpOnly cookies) es trabajo futuro, no localStorage.
- **Borrado de documentos desde la UI:** no hay endpoint de borrado de documentos (quedan listados); un `DELETE /documents/{id}` + UI lo completaría.
- **CRUD de Calendar incompleto** (backend): solo crear eventos; `delete`/`update` pendientes.
- **El 14B:** señales en contra acumuladas (no cabe en VRAM, OOM con el 7B, routing flaky pero domado con overrides) — diferido salvo caso costoso.
- **Heredadas:** clasificador de fallos de envío (P27); harness `dual_validation.py` apunta a `FETCH_N=20`; doc #23 con chunking anómalo; tokens en claro en `oauth_accounts`; OOM del host; `request_id`, `retry_backoff`, `path_separator`, docs drift, Celery root.
- **Datos de prueba:** un doc "Zephyr" en el knowledge base (sin endpoint de borrado); conversaciones de prueba.

### Las direcciones que siguen (extensiones y pulido — ya no MVP)

- **Pulido de la UI:** detalles visuales, animaciones, accesibilidad, lo que la pasada en el navegador revele.
- **Dockerizar el frontend** y un despliegue real (la pieza que falta para "producción").
- **Completitud de integraciones** (backend): CRUD de Calendar, Sheets/Slides de Drive, borrado de documentos.
- **Robustez del modelo:** si el routing flaky del 7B molesta en uso real, el harness de medición + la decisión del 14B (con su trade-off de VRAM/OOM).

---

## 6. Temario de estudio (Parte 36)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Subida de archivos y proceso asíncrono

- **Multipart sin Content-Type a mano**: dejar que el navegador ponga `multipart/form-data` con su boundary; ponerlo manualmente lo rompe.
- **Sondeo acotado para trabajo asíncrono**: cuando el proceso corre en un worker (Celery) y la UI no recibe push, sondear el estado — pero solo mientras hay trabajo pendiente, parando cuando no queda. Mostrar progreso sin sondear indefinidamente.
- **Exponer estado para que la UI lo lea**: un `GET /documents` mínimo para que el frontend muestre `pending`→`ready`. La señal en el backend, no la inferencia en el frontend.

### B. Cierre de fase (método)

- **El balance del arco**: sintetizar qué añadió cada parte y qué hilo la atravesó (aquí: el backend bien diseñado se deja poner cara). Como P19 (Fase 1) y P27 (Fase 2).
- **La interfaz como validación retrospectiva del backend**: si poner cara necesita solo cambios mínimos y aditivos, el backend estaba bien estructurado. Si exigiera refactors profundos, no lo estaba.
- **Verificación de protocolo + visual**: el cierre de fase necesita ambas — qué llega (HTTP/SSE) y cómo se ve/qué hace con clics (navegador). Reconocer y cerrar el hueco visual antes de declarar terminado.

### C. Construir un frontend que respeta el backend

- **El clic como cara de un comando existente**: la UI dispara el camino seguro que ya existe (el "sí" que interpreta la heurística), no añade uno nuevo. Extender sin debilitar garantías.
- **Cambios de backend mínimos y aditivos**: exponer una señal (un evento, un GET), nunca tocar la lógica de negocio para acomodar la UI. La UI se adapta al backend, no al revés.
- **Los detalles del medio importan**: gestos del navegador (pop-up vs enlace), boundaries de multipart, CORS, el shape de los schemas. Conocer el medio (el navegador, HTTP) evita bugs invisibles.

---

*Cierre de la Parte 36. Fin de la Fase 3 (Interfaz) — NexaAgent tiene cara: el ciclo completo del agente, verificado end-to-end en el navegador. Las tres fases, cerradas.*
