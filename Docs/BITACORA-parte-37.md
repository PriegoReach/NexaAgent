# NexaAgent — Bitácora de desarrollo (Parte 37)

> **Revisión completa y cierre de pendientes.** Con las tres fases cerradas, una pregunta
> sencilla —«¿qué le falta?, ¿cumple sus funciones?»— abrió una auditoría de punta a punta.
> Salió un checklist de **23 puntos** (8 bugs, 4 de instalación, 2 de seguridad, 6 funciones
> nuevas y 3 de calidad) y esta parte los recorre todos, cada uno en su rama, con sus tests y
> su PR. Contenido propio: la **medición del enrutado a herramientas** (¿llama el agente a la
> tool correcta?), que destapó un fallo nuevo del 7B: escribir la llamada a una herramienta
> como texto en vez de hacerla.

**Estado al cierre de la Parte 37:** los 23 puntos cerrados. La CI está fusionada y probada en contenedores limpios, pero GitHub no ejecutará sus trabajos hasta que levante el bloqueo de facturación de la cuenta (ticket de soporte abierto). Lo que el sistema gana: los bugs en los que **lo que se mostraba no correspondía a lo que pasaba** (un correo «enviado» que no salió, una propuesta sin nada pendiente, «Conectado» con el acceso caducado, «Procesando…» eterno, «mañana» un día tarde) están cerrados y con tests; una instalación nueva funciona al primer `docker compose up`, con o sin GPU; los puertos internos ya no están abiertos a la red y `http_get` no puede usarse para sacar datos; y hay funciones nuevas: recordatorios diarios, Calendar completo, sesión que sobrevive a recargar, Markdown, panel de memoria, Word e imágenes con OCR, citas de la fuente y una evaluación del enrutado. La suite tiene ahora 248 tests y tarda unos 30 segundos (llegó a tardar 8,5 minutos).

---

## 1. El método de la revisión

Leer el código y ejecutarlo, no solo leerlo. Cada hallazgo se anotó con el archivo y la línea donde vive, una prioridad (alta, media, baja) y un arreglo propuesto, en un **checklist compartido** (un artifact de claude.ai con su base de datos) que se fue actualizando con cada PR: del «por hacer» al «en revisión» y al «hecho», con el commit que lo cerró.

El ciclo por punto, siempre el mismo: **rama → arreglo → tests → verificación → PR → merge**. La verificación, contra el servicio real cuando importaba: un evento creado, movido y borrado en el Google Calendar de verdad; un resumen de recordatorios que llegó por Gmail; documentos reales subidos y preguntados. Los tests sustituyen a Google y a Ollama por dobles; la verificación real comprueba que los dobles no mienten.

| PR | Rama | Qué cerró |
|---|---|---|
| #6 | `fix/acciones-confirmables` | b1 correo que no se reenviaba, b2 token de Google caducado, b3 dos acciones en un mensaje |
| #7 | `fix/propuesta-imitada-y-conexiones` | b7 propuestas imitadas por el modelo, b8 estado del panel Conexiones |
| #8 | `fix/fechas-zona-horaria` | b4 fechas en UTC |
| #9 | `fix/puertos-y-migraciones` | s1 puertos expuestos, i1 migraciones al arrancar |
| #10 | `fix/documentos-y-historial` | b5 documentos sin texto, b6 memoria al retomar una conversación |
| #11 | `fix/http-get-y-cpu` | s2 límites de `http_get`, i3 funcionamiento sin GPU |
| #12 | `fix/secretos-y-embeddings` | i2 secretos opcionales, i4 dimensión de embeddings |
| #13 | `ci/github-actions` | q2 CI (fusionada; GitHub no la ejecuta hasta desbloquear la cuenta) |
| #14 | `feat/recordatorios-calendar-tests` | f1 recordatorios, f2 Calendar completo, q1 tests (y la suite 15× más rápida) |
| — | `feat/cierre-pendientes` | f6 sesión y Markdown, f4 panel de memoria, f3 Word/OCR/citas, f5 evaluación del enrutado, q3 documentación |

---

## 2. Los bugs: lo que se muestra tiene que ser lo que pasó

| Bug | Síntoma | Arreglo |
|---|---|---|
| **b1** | Un correo cuyo envío falló no se podía reenviar, y Nexa decía que ya se había enviado | El fallo se clasifica: los definitivos (sin cuenta, 4xx, sin red) quedan `failed` y se pueden reintentar; los inciertos (5xx, timeout) quedan `uncertain` y no se reenvían, pero tampoco se dan por enviados |
| **b2** | Con el refresh token de Google caducado (en modo Testing dura ~7 días), las herramientas reventaban | `invalid_grant` pide reconectar y borra el token muerto; Calendar, Gmail y Drive devuelven el mensaje en vez de fallar |
| **b3** | Dos acciones confirmables en un mensaje: se mostraba una y el «sí» ejecutaba la otra | `SET NX`: manda la primera; la segunda queda en `deferred` y la respuesta avisa de ella |
| **b4** | «Mañana» caía un día tarde a partir de las 18:00 (el contenedor corre en UTC) | `app/core/clock.py`: la fecha de hoy en `CALENDAR_TIMEZONE` |
| **b5** | Un PDF escaneado se quedaba en «Procesando…» para siempre | Estado `empty` («Sin texto»), `failed` para los fallos de Drive, 415 para los tipos que el parser no sabe leer |
| **b6** | Al retomar una conversación de ayer, el agente no recordaba nada (Redis caduca a las 24 h) | Si Redis no la tiene, el historial se reconstruye desde Postgres |
| **b7** | El modelo copiaba del historial una propuesta («Responde sí para confirmar…») sin llamar a la tool | Si la respuesta pide un sí/no y no hay nada pendiente, se sustituye por un aviso (en streaming, con un evento `replace`); un «sí» a una propuesta que ya no existe no llega al modelo |
| **b8** | El panel Conexiones mostraba el estado de cuando se cargó la página | Se vuelve a consultar al abrirlo y tras cada mensaje; «Hay que reconectar» cuando el acceso no se puede renovar |

Y dos que aparecieron por el camino (PR #14): el **«31 de febrero»**, al escribir los tests de fechas (`_resolve_due` lanzaba `ValueError` a mitad del turno; ahora es «no entendí la fecha»), y el **webhook**, al construir los recordatorios: tenía el mismo defecto que el correo y ahora clasifica sus fallos igual.

**El hilo común:** casi todos son una distancia entre lo que el usuario ve y lo que pasó. El arreglo nunca fue «que el modelo se porte mejor», sino una comprobación determinista en el borde: el estado real en la BD, la acción pendiente real en Redis, la fecha real en la zona del usuario.

---

## 3. Instalación y seguridad: que funcione a la primera, y sin puertas abiertas

**Instalación (una máquina nueva, desde cero):**
- **i1 — Migraciones:** el arranque ya no creaba tablas y el primer chat daba 500. Un servicio `migrate` de un solo uso (`alembic upgrade head`) corre antes de `api` y `worker`, reutilizando la imagen de la API para no construir otra copia de ~10 GB.
- **i2 — Secretos:** si falta el archivo de un secreto, **Docker no falla: crea una carpeta vacía** con ese nombre y la monta. La API no arrancaba (`IsADirectoryError`) aunque fuera el secreto opcional de Google. Ahora un opcional en ese estado cuenta como no configurado, un obligatorio para el arranque con un mensaje que dice qué hacer, y `scripts/init_secrets.py` los crea todos antes del primer arranque.
- **i3 — Sin GPU:** el reranker tenía `device="cuda"` fijo. Ahora usa CPU (fp32) si no hay CUDA, y `docker-compose.cpu.yml` quita las reservas de GPU.
- **i4 — Embeddings:** las migraciones fijaban 768 dimensiones, así que cambiar de modelo de embeddings (como describía el README) rompía la base. Ahora usan `EMBEDDING_DIM`, y `app/db/schema_check.py` detecta y explica un desajuste antes de que arranque la API.

**Seguridad:**
- **s1 — Puertos:** Postgres, Redis (sin contraseña) y Ollama escuchaban en todas las interfaces, con los tokens de Google en Postgres. Ahora solo en `127.0.0.1`. Postgres se publica en el **5433**: en este Windows corre un PostgreSQL 18 nativo en el 5432, y con los dos en el mismo puerto `localhost:5432` llegaba al nativo.
- **s2 — `http_get`:** un documento con instrucciones escondidas podía hacer que el agente mandara datos a una URL cualquiera (exfiltración por *prompt injection*) o consultara servicios internos (SSRF). Ahora: solo direcciones públicas, comprobadas también tras cada redirección (que se siguen a mano); solo dominios que el usuario escribió en la conversación o que están en `HTTP_GET_ALLOWED_DOMAINS`; respuestas acotadas (200 KB leídos, 4000 caracteres, nada binario).

---

## 4. Funciones nuevas

- **f1 — Recordatorios.** «Recuérdame comprar pan el viernes» creaba la tarea, pero nada avisaba el viernes. Celery beat, embebido en el worker, comprueba cada hora y a partir de `REMINDER_HOUR` manda **un** resumen al día (tareas de hoy y vencidas) por Gmail o por el webhook. «Uno al día» aunque haya reinicios: el día se reserva en la tabla `reminder_digests` antes de enviar (escribir primero, como el correo).
- **f2 — Calendar completo.** Consultar un día concreto (antes, «¿qué tengo el jueves?» solo funcionaba si el evento estaba entre los próximos 10), invitar al crear, mover y borrar, con confirmación como el resto. Probado contra el Calendar real.
- **f6 — Sesión y Markdown.** El JWT vivía en el estado de React y se perdía al recargar. Ahora `/auth/login` deja una **cookie `httpOnly` y `SameSite=Strict`** (`/auth/session` la comprueba al abrir, `/auth/logout` la borra); la API sigue aceptando la cabecera Bearer. `localhost:5173` y `localhost:8000` son el mismo *site* (el puerto no cuenta), así que `Strict` no estorba. Las respuestas se pintan en **Markdown** (`react-markdown` + GFM), salvo las propuestas, que se muestran literales: lo que se muestra es lo que se ejecutará. La voz lee el texto sin los símbolos de Markdown.
- **f4 — Panel de memoria.** `GET /memories` y `DELETE /memories[/{id}]`, y un panel para ver lo que Nexa aprendió de las conversaciones, olvidar un recuerdo u olvidarlo todo. El panel avisa de algo que antes no se veía: borrar una conversación borra lo que se aprendió en ella.
- **f3 — Word, imágenes y citas.**
  - **Word (.docx) sin dependencias:** un .docx es un zip con el cuerpo en `word/document.xml`; se lee un párrafo por línea, también los de tablas y cuadros de texto, con una cota de tamaño contra *zip bombs*.
  - **OCR** con tesseract (español + inglés) y `pdftoppm` (poppler), instalados en la imagen. Un PDF del que pypdf saca menos de 20 caracteres se trata como escaneado y pasa por OCR (hasta 30 páginas). Sin tesseract (fuera de Docker) el documento termina en «Sin texto», sin romper nada. Drive también trae Word e imágenes.
  - **Citas:** cada fragmento que devuelve `search_knowledge_base` lleva una línea `[Fuente: archivo]`, y el prompt pide decir de qué archivo sale lo que se responde.
  - **Verificado de punta a punta:** un Word, un PNG y un PDF escaneado subidos → «Listo» → tres preguntas respondidas bien, nombrando la fuente (el archivo en el caso del Word; el título del documento en los otros dos). Una lección por el camino: la primera prueba leyó «Ibáñez» como «IbxkXez», y no era el OCR: **la fuente por defecto de Pillow con la que generé la imagen no tiene «á» ni «ñ»** (dibuja el mismo recuadro que un ☃). Con DejaVu, el OCR lee «almacén y reunión» bien. Los datos de prueba también pueden mentir.

---

## 5. La medición del enrutado (contenido propio de P37)

**La pregunta:** ante cada mensaje, ¿llama el agente a la herramienta correcta? Con 15 herramientas y un 7B, es la pieza más frágil del sistema, y hasta ahora solo se sabía por impresiones (el «quirk de routing» de P24-P27 y P33).

**El harness** (`app/eval/tool_routing.py`), como el de recuperación de la Fase 1: un dataset de 38 mensajes (`app/eval/datasets/tool_routing.yaml`) con la **primera** herramienta esperada (o `none`), preguntado al modelo con el mismo system prompt y las mismas tools que usa el agente. Mide solo esa decisión: no ejecuta nada, así que no toca Google, la BD ni el correo. Tests deterministas (la aritmética, y que el dataset solo nombra herramientas que existen) y un piso sobre el modelo real activable con `EVAL_REAL=1`.

**Primera medición: 0.816 (31/38).** Y el primer hallazgo, el importante: en 4 de los 7 fallos el modelo **sí quería usar la herramienta, pero la escribió como texto**:

```
CallChecka_webhook({"message": "El respaldo ha finalizado."})
CallChecka la herramienta `search_knowledge_base` para encontrar el documento...
```

Generando en crudo (`/api/generate` con `raw`, sin el parser de tool calls de Ollama) sale lo mismo: es el modelo, no el parser. En la app, el usuario habría visto ese texto como respuesta.

**Lo que se probó, con datos:**

| Intento | Resultado |
|---|---|
| Regla en el prompt: «nunca escribas el nombre de una herramienta» | 0.737 (peor) |
| Quitar las comillas invertidas de los nombres de tools en el prompt | 0.842 |
| Las dos cosas | 0.789 |
| Lo anterior + «si te falta el id, búscalo tú» | 0.789 |
| Reintentar con una corrección («llama a la herramienta de verdad») | arregla 3 de 5, pero repite idéntica la del webhook |

Ninguna variante mejora de forma fiable: cada una arregla unos casos y rompe otros. Es el ruido de un 7B al límite con 15 herramientas, no una mejora. Y otro dato que obliga a medir con cuidado: **con temperature 0 el modelo no es del todo determinista** en GPU. Dentro de una sesión, tres pasadas dan lo mismo; de una sesión a otra cambian uno o dos casos.

**El ejemplo del prompt se copia.** La regla de citas llevaba un ejemplo («según politica_vacaciones.pdf, …») y en una de las llamadas filtradas el modelo se inventó un `[Fuente: contratos_servicios.pdf]`. Sin el nombre de ejemplo: 0.851 de media en tres pasadas (frente a 0.816) y menos llamadas escritas como texto (1 frente a 2). Se quitó.

**La decisión: un guard en el borde, no más prompt.** Igual que con las propuestas imitadas (b7): `is_tool_call_as_text` detecta en la respuesta final una llamada escrita (`<tool_call>`, `nombre({"arg": …})`, un nombre de tool seguido de paréntesis, el artefacto `CallCheck`), ignorando los bloques de código, y la sustituye por un aviso honesto: «Intenté usar una de mis herramientas, pero escribí la llamada en vez de hacerla, así que no se hizo nada…». En streaming, con el mismo evento `replace`. El eval cuenta esas respuestas aparte («texto»).

**Medición final: 0.895 (34/38)**, igual en dos pasadas. Los fallos que quedan son siempre del mismo tipo: ante «borra la tarea de llamar al contador» o «mueve mi reunión del jueves», el modelo **pregunta cuál** en vez de listar primero, y una vez llama a `update_task` con el id vacío. Molesto, pero no dañino: nada se ejecuta mal. El piso de regresión queda en **0.75**, con margen para la deriva entre sesiones y por encima de la peor variante probada (0.737).

---

## 6. Calidad

- **q1 — Tests:** fechas y horas en lenguaje natural (`_resolve_due`, `_resolve_time`) y la heurística sí/no de las confirmaciones (ejecuta, cancela o, ante la duda, vuelve a preguntar sin ejecutar). Y una medición que cambió la suite: preparar cada test tardaba ~2,5 s y ejecutarlo casi nada. El culpable era `TRUNCATE`, que crea archivos nuevos en disco y los sincroniza (lento en Docker Desktop); con tablas casi vacías, `DELETE` es casi instantáneo. **De 8,5 minutos a 35 segundos.**
- **q2 — CI:** un workflow de GitHub Actions (ruff + pytest con un servicio pgvector y torch de CPU; build del frontend; validación del compose), probado en contenedores limpios. GitHub no ejecuta los trabajos: «account is locked due to a billing issue», en una cuenta Free sin suscripciones. Ticket de soporte abierto. Se fusionó igualmente, tras simularla otra vez en contenedores limpios (ruff sin errores, 246 tests, build del frontend y compose) y con tesseract añadido para que los tests de OCR también corran allí: cuando GitHub levante el bloqueo, arranca sola.
- **q3 — Documentación:** los tres README al día (el principal decía «Web UI mínima»; el del frontend listaba como pendiente lo que ya existía) y esta bitácora. El archivo `BITACORA-parte-34.md` se llamaba así pero era la «Parte 36»: renombrado a `BITACORA-parte-36.md`, sin tocar su contenido.

---

## 7. Aprendizajes clave

1. **Lo que se muestra tiene que ser lo que pasó.** Casi todos los bugs eran una distancia entre la interfaz (o la respuesta del modelo) y el estado real. La solución robusta es una comprobación determinista en el borde contra el estado real, no pedirle al modelo que no se equivoque.

2. **Medir antes de tocar el prompt.** Cuatro variantes «razonables» del prompt se movieron entre 0.737 y 0.842 sin mejorar de forma fiable. Sin el eval, cualquiera de ellas se habría quedado por parecer buena idea. El dato dijo «no toques el prompt; pon un guard».

3. **temperature 0 no es determinista en GPU.** Entre sesiones cambian uno o dos casos de 38. Una medición es una muestra: varias pasadas, y un piso con margen.

4. **Los modelos pequeños copian los ejemplos del prompt.** El nombre de archivo de ejemplo apareció en la salida como fuente inventada. Un ejemplo concreto en un prompt es una sugerencia fuerte; mejor describir la forma que dar un valor.

5. **Docker convierte un secreto que falta en una carpeta.** Un fallo silencioso que solo se ve al arrancar. Detectarlo y explicar qué hacer es parte de la instalación.

6. **El tiempo estaba en el setup, no en los tests.** Medir dónde se va el tiempo antes de optimizar: 8,5 min → 35 s cambiando una línea del fixture.

7. **Verificar contra lo real, y desconfiar también de los datos de prueba.** Calendar y Gmail de verdad, documentos de verdad. Y el OCR «fallido» que era una fuente sin «ñ».

8. **La sesión del navegador va en una cookie `httpOnly`, no en `localStorage`.** Un script inyectado no puede leerla, y `SameSite=Strict` evita que otros sitios la usen. Puertos distintos del mismo host son el mismo *site*.

---

## 8. Comandos de referencia (nuevos de esta parte)

```powershell
cd X:\Nexa\nexaagent

# instalación nueva: secretos antes del primer up
python scripts/init_secrets.py
docker compose up -d --build
# sin GPU NVIDIA
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build

# migraciones a mano (normalmente las aplica el servicio migrate al arrancar)
docker compose run --rm migrate

# suite (< 1 min) y ruff
docker compose run --rm tests
docker compose run --rm tests ruff check --select F,E9 app tests scripts

# evaluaciones (necesitan Ollama)
docker compose exec api python -m app.eval.tool_routing
docker compose exec api python -m app.eval.retrieval
docker compose run --rm -e EVAL_REAL=1 tests pytest tests/test_tool_routing_eval.py
```

---

## 9. Estado y siguientes pasos

### El checklist de la revisión: 23 de 23 ✅

- ✅ **Bugs (8/8):** correo, token de Google, dos acciones, fechas UTC, documentos sin texto, memoria al retomar, propuestas imitadas, Conexiones.
- ✅ **Instalación (4/4):** migraciones, secretos, sin GPU, embeddings.
- ✅ **Seguridad (2/2):** puertos, `http_get`.
- ✅ **Funciones (6/6):** recordatorios, Calendar completo, Word/OCR/citas, panel de memoria, evaluación del enrutado, sesión y Markdown.
- ✅ **Calidad (3/3):** tests, documentación y CI (fusionada; arranca cuando GitHub desbloquee la cuenta).

### Deudas / pendientes 🔜

- **CI:** comprobar su primera ejecución en GitHub cuando se levante el bloqueo de facturación de la cuenta.
- **El enrutado del 7B:** pregunta por el id en vez de listar primero; a veces llama con un id vacío. El eval ya permite comparar con datos el 14B (que no cabe bien en 12 GB con el reranker y la voz) o un modelo más nuevo: era la razón de construirlo.
- **El eval mide la herramienta, no sus argumentos.** El siguiente paso es añadir al dataset los argumentos esperados (la fecha tal como la dijo el usuario, el destinatario del correo) y comprobarlos.
- **El piso del eval de recuperación no se puede ejecutar como dice su docstring:** `docker compose exec -e EVAL_REAL=1 api pytest …`, pero la imagen de la API no tiene pytest, y el `conftest` fuerza la BD `_test`, que no tiene el corpus. El CLI `python -m app.eval.retrieval` sí funciona.
- **OCR:** tesseract sin análisis de maquetación, como mucho 30 páginas por PDF; los nombres propios pueden perder tildes («Ibáñez» → «Ibanez»). Excel, PowerPoint, Sheets y Slides siguen sin soporte.
- **Citas:** el 7B a veces nombra el título del documento en vez del archivo.
- **Heredadas:** el frontend sin dockerizar; los tokens OAuth en claro en `oauth_accounts`.
- **Bitácora:** `BITACORA-parte-33.md` y `BITACORA-parte-36.md` son dos versiones del cierre de la Fase 3 (la 33 agrupa P32 en una parte; la 36 la reparte en cinco). Decidir cuál queda.

---

## 10. Temario de estudio (Parte 37)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Auditar un proyecto propio

- **Ejecutar, no solo leer:** la mitad de los bugs solo aparecían al usar el sistema (la hora UTC después de las 18:00, el token tras 7 días, la carpeta que Docker crea en vez del secreto).
- **Un checklist con referencias y prioridades,** compartido y actualizado con cada PR, convierte una lista de quejas en un plan verificable.
- **Una rama, unos tests y un PR por punto:** cada arreglo se revisa y se puede deshacer por separado.

### B. Evaluar un agente

- **Medir la decisión, no el resultado:** la primera herramienta que elige el modelo se mide sin ejecutar nada, así que el eval es seguro y rápido (~25 s con el modelo ya cargado).
- **Varias pasadas y un piso con margen:** con temperature 0 en GPU, la salida varía entre sesiones.
- **Contar los modos de fallo por separado:** «escribió la llamada como texto» no es lo mismo que «eligió otra herramienta» ni que «preguntó en vez de actuar». Cada uno pide un arreglo distinto.
- **Guards deterministas en el borde > ingeniería de prompt** cuando el modelo es pequeño y el fallo es detectable.

### C. Seguridad práctica

- **SSRF y exfiltración por prompt injection:** una herramienta que hace peticiones HTTP necesita una lista de destinos permitidos y comprobar la IP en cada salto, también tras las redirecciones.
- **Superficie de red:** publicar los servicios internos solo en `127.0.0.1`.
- **Sesión web:** cookie `httpOnly` + `SameSite=Strict` (+ `Secure` con HTTPS).
- **Escribir primero (idempotencia):** reservar en la BD (correo, webhook, resumen diario) antes de actuar, y clasificar los fallos en definitivos (reintentables) e inciertos (no se repiten).

### D. Ingesta de documentos

- **Un .docx es un zip con XML:** se lee con la biblioteca estándar; cuidado con los párrafos anidados (cuadros de texto) y con el tamaño descomprimido (*zip bomb*).
- **OCR como respaldo:** solo cuando el PDF no trae texto; páginas a imagen (pdftoppm) y después tesseract, con límites de páginas y de tiempo.
- **Citar la fuente:** el retriever devuelve el documento de cada fragmento y la herramienta se lo da al modelo; mantener `search()` igual para no romper los harnesses que dependen de él.

---

*Cierre de la Parte 37. La revisión, cerrada: los 23 puntos, verificados contra los servicios reales. NexaAgent hace lo que dice que hace, se instala a la primera, y ahora mide también si elige bien sus herramientas.*
