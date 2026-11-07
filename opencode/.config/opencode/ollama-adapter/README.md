# OpenCode + Ollama: adaptador de tool calls para Qwen2.5 Coder 7B

## El problema

`qwen2.5-coder:7b` no emite `tool_calls` nativos. Cuando OpenCode le ofrece
herramientas, el modelo contesta con un objeto JSON en el texto:

```json
{"name": "read", "arguments": {"path": "/tmp/x"}}
```

Ollama devuelve eso como contenido normal, así que OpenCode lo muestra en el
chat y **no ejecuta** `read` ni `edit`. Además el modelo venía con `num_ctx`
de 4096, insuficiente para el prompt de OpenCode.

## Qué hace este adaptador

`server.py` es un servidor local compatible con la API de OpenAI que se coloca
entre OpenCode y Ollama (`http://127.0.0.1:11435`):

- Convierte el JSON del modelo en `tool_calls` reales.
- Filtra las herramientas a un set confiable (`read`, `edit`, `write`, `glob`,
  `grep`, `shell`, `skill`).
- Reescribe las descripciones de las herramientas para que el modelo las use
  bien (por ejemplo: "write borra el contenido, usa edit").
- Protege contra pérdida de datos: si el modelo intenta `write` sobre un
  archivo existente sin haberlo leído, o si el contenido nuevo borraría la
  mayor parte del archivo, el adaptador lo cambia por un `read`.
- Inyecta `system.md` en cada petición (reglas de operación para modelos
  pequeños).
- Fuerza `num_ctx=32768` y pasa temperatura, top_p, stop, etc.
- Soporta streaming y los endpoints de descubrimiento (`/api/tags`,
  `/api/show`, `/v1/models`).

## Instalación (ya realizada)

```bash
# 1. Modelo derivado con contexto y parámetros correctos
ollama create qwen2.5-coder:7b-opencode \
  -f ~/.config/opencode/ollama/qwen2.5-coder-opencode.Modelfile

# 2. Servicio del adaptador
systemctl --user daemon-reload
systemctl --user enable --now opencode-ollama-adapter.service

# 3. Comprobar
curl -s http://127.0.0.1:11435/health
```

## Uso

En OpenCode, selecciona el modelo con `/models`:

```
ollama-local/qwen25-coder
```

o desde la línea de comandos:

```bash
opencode run --model ollama-local/qwen25-coder "lee README.md y resume"
```

El modelo crudo (`ollama/qwen2.5-coder:7b`) queda oculto en OpenCode para no
elegirlo por error. `qwen3.5:9b` sigue disponible y no pasa por el adaptador.

## Configuración

Variables del servicio en
`~/.config/systemd/user/opencode-ollama-adapter.service`:

| Variable | Default | Uso |
| --- | --- | --- |
| `ADAPTER_PORT` | `11435` | Puerto local |
| `ADAPTER_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama de destino |
| `ADAPTER_NUM_CTX` | `32768` | Contexto forzado |
| `ADAPTER_TOOLS` | `read,edit,write,glob,grep,shell,skill` | Herramientas visibles; `*` = todas |
| `ADAPTER_GUARD_WRITE` | `1` | Protección anti-borrado de `write` |
| `ADAPTER_SYSTEM_FILE` | `system.md` | Reglas extra inyectadas |
| `ADAPTER_DEBUG` | `0` | Logs detallados |

Después de editar la unidad:

```bash
systemctl --user daemon-reload
systemctl --user restart opencode-ollama-adapter.service
```

Después de editar `system.md` o `server.py` basta con reiniciar el servicio.

## Depuración

```bash
journalctl --user -u opencode-ollama-adapter.service -f
```

Mensajes útiles:

- `recovered tool call(s) ['read'] from plain JSON` → el adaptador arregló una
  llamada.
- `write guard: ... asking for read first` → bloqueó un `write` destructivo.

Prueba manual sin OpenCode:

```bash
curl -s http://127.0.0.1:11435/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-coder:7b-opencode","stream":false,
       "messages":[{"role":"user","content":"lee /etc/hostname"}],
       "tools":[{"type":"function","function":{"name":"read",
         "parameters":{"type":"object","properties":{"path":{"type":"string"}},
         "required":["path"]}}}]}'
```

## Skill

`~/.config/opencode/skills/local-read-edit/SKILL.md` explica el flujo
`read → edit → verificar` con los parámetros exactos. El adaptador le pide al
modelo que la cargue antes de editar.

## Desinstalar

```bash
systemctl --user disable --now opencode-ollama-adapter.service
rm ~/.config/systemd/user/opencode-ollama-adapter.service
ollama rm qwen2.5-coder:7b-opencode
# y borra el bloque "ollama-local" de ~/.config/opencode/opencode.json
```
