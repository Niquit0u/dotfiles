#!/usr/bin/env python3
"""OpenAI-compatible adapter in front of Ollama.

Why this exists
---------------
`qwen2.5-coder:7b` (like several small local models) does not emit native
`tool_calls`. When OpenCode offers it tools, the model answers with a bare JSON
object in the message body:

    {"name": "read", "arguments": {"path": "/tmp/x"}}

Ollama returns that as plain content, so OpenCode shows JSON in the chat and
never runs `read`, `edit`, etc.

This server sits between OpenCode and Ollama and fixes that:

  * forwards `/v1/chat/completions` to Ollama's native `/api/chat`
  * converts tool-call JSON found in the content into real OpenAI `tool_calls`
  * optionally restricts the advertised tools to a small, reliable subset
  * injects a short operating manual for small models (system.md)
  * forces a sane `num_ctx` (Ollama defaults to 4096, which truncates prompts)
  * supports both streaming and non-streaming clients
  * passes discovery endpoints (`/api/tags`, `/api/show`, `/v1/models`) through

Environment:
  ADAPTER_HOST             default 127.0.0.1
  ADAPTER_PORT             default 11435
  ADAPTER_OLLAMA_URL       default http://127.0.0.1:11434
  ADAPTER_NUM_CTX          default 32768
  ADAPTER_NUM_BATCH        default 3072 (logical batch; speeds up prompt eval)
  ADAPTER_NUM_THREAD       default 6 (physical cores; SMT/12 threads is slower)
  ADAPTER_TOOLS            default "read,edit,write,glob,grep,shell,skill"
                           use "*" to advertise every tool OpenCode sends
  ADAPTER_SYSTEM_SKIP      comma-separated model name substrings that do NOT get
                           system.md (models that follow instructions on their own)
                           default "qwen3-coder"
  ADAPTER_KEEP_ALIVE       default 30m
  ADAPTER_SYSTEM_FILE      default ./system.md next to this file
  ADAPTER_DEBUG            set to 1 for verbose logs
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("ADAPTER_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
HOST = os.environ.get("ADAPTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ADAPTER_PORT", "11435"))
NUM_CTX = int(os.environ.get("ADAPTER_NUM_CTX", "32768"))
NUM_BATCH = int(os.environ.get("ADAPTER_NUM_BATCH", "3072"))
NUM_THREAD = int(os.environ.get("ADAPTER_NUM_THREAD", "6"))
TOOLS_ENV = os.environ.get("ADAPTER_TOOLS", "read,edit,write,glob,grep,shell,skill").strip()
SYSTEM_SKIP = os.environ.get("ADAPTER_SYSTEM_SKIP", "qwen3-coder").strip()
KEEP_ALIVE = os.environ.get("ADAPTER_KEEP_ALIVE", "30m")
GUARD_WRITE = os.environ.get("ADAPTER_GUARD_WRITE", "1").lower() not in ("0", "false", "no")
SYSTEM_FILE = os.environ.get("ADAPTER_SYSTEM_FILE", os.path.join(HERE, "system.md"))
DEBUG = os.environ.get("ADAPTER_DEBUG", "").lower() not in ("", "0", "false", "no")

# Options accepted by Ollama's native API and present in OpenAI requests.
OPTION_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
    "num_batch",
    "num_thread",
)

# Small models guess these names. Normalise them to what OpenCode expects.
PATH_KEYS = ("filePath", "file_path", "filename", "file")
EDIT_ALIASES = {
    "old_string": "oldString",
    "old_text": "oldString",
    "new_string": "newString",
    "new_text": "newString",
    "replace_all": "replaceAll",
}

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.S)

log = logging.getLogger("adapter")


# --------------------------------------------------------------------------- #
# Ollama transport
# --------------------------------------------------------------------------- #


def ollama(method: str, path: str, payload=None, timeout: float = 900.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        OLLAMA + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read()


# --------------------------------------------------------------------------- #
# Request translation
# --------------------------------------------------------------------------- #


def read_system_addendum() -> str:
    if not SYSTEM_FILE:
        return ""
    try:
        with open(SYSTEM_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        log.warning("system file not found: %s", SYSTEM_FILE)
        return ""


def skip_system_for(model: str) -> bool:
    patterns = [part.strip() for part in SYSTEM_SKIP.split(",") if part.strip()]
    return any(pattern.lower() in (model or "").lower() for pattern in patterns)


def merge_system(messages: list, model: str = "") -> list:
    """Ollama renders a single system slot, so merge every system message."""
    parts = []
    rest = []
    for message in messages:
        if message.get("role") == "system":
            content = (message.get("content") or "").strip()
            if content:
                parts.append(content)
        else:
            rest.append(message)
    addendum = "" if skip_system_for(model) else read_system_addendum()
    if addendum:
        parts.append(addendum)
    if parts:
        rest.insert(0, {"role": "system", "content": "\n\n".join(parts)})
    return rest


def normalize_arguments(name: str, args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    args = dict(args)
    if name in ("read", "edit", "write"):
        if "path" not in args:
            for alias in PATH_KEYS:
                if alias in args:
                    args["path"] = args.pop(alias)
                    break
    if name == "edit":
        for old, new in EDIT_ALIASES.items():
            if old in args and new not in args:
                args[new] = args.pop(old)
    if name == "shell":
        if "command" not in args:
            for alias in ("cmd", "script"):
                if alias in args:
                    args["command"] = args.pop(alias)
                    break
    return args


def clean_messages(messages: list) -> list:
    out = []
    for message in messages:
        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        item = {"role": role, "content": message.get("content") or ""}
        if role == "tool" and message.get("tool_name"):
            item["tool_name"] = message["tool_name"]
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                args = function.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                calls.append(
                    {
                        "function": {
                            "name": function.get("name", ""),
                            "arguments": args if isinstance(args, dict) else {},
                        }
                    }
                )
            if calls:
                item["tool_calls"] = calls
        out.append(item)
    return out


def normalize_tools(tools) -> list:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name")
        if not name:
            continue
        parameters = function.get("parameters") or function.get("inputSchema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": function.get("description") or "",
                    "parameters": parameters,
                },
            }
        )
    return out


def tool_name(tool: dict) -> str:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return function.get("name") or ""


TOOL_HINTS = {
    "read": (
        "Read a file from disk. Args: path (absolute), optional offset and limit. "
        "Always read a file before editing it."
    ),
    "edit": (
        "Replace exact text in an existing file. Args: path, oldString, newString, "
        "optional replaceAll. oldString must match the file content exactly once, "
        "including indentation and line breaks. This is the correct tool to modify "
        "an existing file."
    ),
    "write": (
        "Create a NEW file, or replace one completely. WARNING: it deletes the "
        "previous content. Never use write on an existing file: use edit instead."
    ),
    "shell": (
        "Run a shell command. Args: command, optional workdir and timeout. Do not "
        "put cd inside the command; set workdir instead."
    ),
    "glob": "Find file paths by glob pattern, for example src/**/*.ts.",
    "grep": "Search file contents with a regular expression.",
    "skill": "Load the instructions of an available skill by its exact id.",
}


def describe_tools(tools: list) -> list:
    for tool in tools:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        hint = TOOL_HINTS.get(function.get("name"))
        if hint:
            function["description"] = hint
    return tools


def filter_tools(tools: list) -> list:
    if not tools or TOOLS_ENV in ("*", "all"):
        return tools
    allowed = {name.strip() for name in TOOLS_ENV.split(",") if name.strip()}
    if not allowed:
        return tools
    kept = [tool for tool in tools if tool_name(tool) in allowed]
    if not kept:
        log.warning("tool filter kept nothing; advertising all tools")
        return tools
    return kept


def build_options(body: dict) -> dict:
    options = {"num_ctx": NUM_CTX}
    if NUM_BATCH > 0:
        options["num_batch"] = NUM_BATCH
    if NUM_THREAD > 0:
        options["num_thread"] = NUM_THREAD
    for key in OPTION_KEYS:
        value = body.get(key)
        if value is not None:
            options[key] = value
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if isinstance(value, int) and value > 0:
            options["num_predict"] = value
            break
    return options


# --------------------------------------------------------------------------- #
# Tool-call recovery
# --------------------------------------------------------------------------- #


def strip_noise(text: str) -> str:
    text = text.strip()
    for suffix in ("</tool_call>", "<|im_end|>", "<|endoftext|>"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def as_call(obj, names: set):
    """Turn one decoded object into (name, arguments) or None."""
    if not isinstance(obj, dict):
        return None
    function = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = function.get("name") or function.get("tool") or function.get("tool_name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if names and name not in names:
        return None
    args = function.get("arguments")
    if args is None:
        args = function.get("args")
    if args is None:
        args = function.get("parameters")
    if args is None:
        args = function.get("input")
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"raw": args}
    if not isinstance(args, dict):
        args = {}
    return name, normalize_arguments(name, args)


def parse_block(block: str, names: set) -> list:
    """Decode one block that may hold one or several tool calls."""
    block = strip_noise(block)
    if not block:
        return []

    decoded = None
    try:
        decoded = json.loads(block)
    except ValueError:
        pass

    if isinstance(decoded, dict):
        if isinstance(decoded.get("tool_calls"), list):
            found = []
            for entry in decoded["tool_calls"]:
                call = as_call(entry.get("function", entry) if isinstance(entry, dict) else entry, names)
                if call:
                    found.append(call)
            if found:
                return found
        call = as_call(decoded, names)
        if call:
            return [call]

    # JSONL / multiple objects on separate lines.
    found = []
    for line in block.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        call = as_call(obj, names)
        if call:
            found.append(call)
    return found


def extract_tool_calls(content: str, names: set):
    """Return (calls, remaining_content)."""
    if not content or not names:
        return [], content

    for block in TOOL_CALL_RE.findall(content):
        calls = parse_block(block, names)
        if calls:
            return calls, ""

    already_fenced = FENCE_RE.findall(content)
    for block in already_fenced:
        calls = parse_block(block, names)
        if calls:
            return calls, ""

    calls = parse_block(content, names)
    if calls:
        return calls, ""

    return [], content


def call_id() -> str:
    return "call_" + uuid.uuid4().hex[:20]


def was_read(path: str, messages: list) -> bool:
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "read":
                continue
            args = function.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if isinstance(args, dict) and args.get("path") == path:
                return True
    return False


def preserved_ratio(existing: str, new: str) -> float:
    old_lines = [line.strip() for line in existing.splitlines() if line.strip()]
    if not old_lines:
        return 1.0
    kept = sum(1 for line in old_lines if line in new)
    return kept / len(old_lines)


def guard_calls(calls, messages: list):
    """Small models love `write`, which silently deletes the old content.

    Two guards for `write` on an existing file:
      1. not read yet in this conversation -> ask for `read` first
      2. the new content would drop most of the existing lines -> ask for `read`
    It can then use `edit` like a sane agent.
    """
    if not GUARD_WRITE:
        return calls
    guarded = []
    for name, args in calls:
        if name == "write":
            path = args.get("path")
            try:
                exists = bool(path) and os.path.isfile(path)
            except OSError:
                exists = False
            if exists and not was_read(path, messages):
                log.warning("write guard: %s exists and was not read; asking for read first", path)
                guarded.append(("read", {"path": path}))
                continue
            if exists:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        existing = handle.read(512 * 1024)
                except OSError:
                    existing = ""
                ratio = preserved_ratio(existing, str(args.get("content") or ""))
                if existing.strip() and ratio < 0.5:
                    log.warning(
                        "write guard: %s would lose content (%.0f%% kept); asking for read first",
                        path,
                        ratio * 100,
                    )
                    guarded.append(("read", {"path": path}))
                    continue
        guarded.append((name, args))
    return guarded


def openai_tool_calls(calls) -> list:
    out = []
    for index, (name, args) in enumerate(calls):
        out.append(
            {
                "index": index,
                "id": call_id(),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Response translation
# --------------------------------------------------------------------------- #


def usage_of(data: dict) -> dict:
    prompt = int(data.get("prompt_eval_count") or 0)
    completion = int(data.get("eval_count") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def sse(payload) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def chunk(model: str, created: int, delta: dict, finish=None, usage=None) -> dict:
    payload = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:16],
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "open-code-ollama-adapter"

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt, *args):  # noqa: A003 - match base signature
        log.debug("%s - %s", self.address_string(), fmt % args)

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status: int, message: str, kind: str = "adapter_error") -> None:
        log.warning("%s: %s", kind, message)
        self.send_json(status, {"error": {"message": message, "type": kind}})

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes ------------------------------------------------------------ #

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            try:
                status, _ = ollama("GET", "/api/tags", timeout=5)
                return self.send_json(200, {"status": "ok", "ollama": status == 200})
            except Exception as exc:  # noqa: BLE001
                return self.send_json(503, {"status": "degraded", "error": str(exc)})

        if self.path.split("?")[0] in ("/v1/models", "/api/tags"):
            try:
                _, raw = ollama("GET", self.path)
            except urllib.error.HTTPError as exc:
                return self.error(exc.code, exc.read().decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                return self.error(502, f"cannot reach Ollama: {exc}", "upstream_error")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)
            return

        return self.error(404, f"unknown path {self.path}", "not_found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            return self.chat()
        if path in ("/api/show", "/api/chat", "/api/embed", "/api/embeddings", "/v1/embeddings"):
            return self.passthrough(path)
        return self.error(404, f"unknown path {self.path}", "not_found")

    def passthrough(self, path: str) -> None:
        try:
            payload = json.loads(self.read_body() or b"{}")
        except ValueError as exc:
            return self.error(400, f"invalid JSON: {exc}", "invalid_request")
        try:
            _, raw = ollama("POST", path, payload)
        except urllib.error.HTTPError as exc:
            return self.error(exc.code, exc.read().decode("utf-8", "replace"), "upstream_error")
        except Exception as exc:  # noqa: BLE001
            return self.error(502, f"cannot reach Ollama: {exc}", "upstream_error")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    # -- chat -------------------------------------------------------------- #

    def chat(self) -> None:
        try:
            body = json.loads(self.read_body() or b"{}")
        except ValueError as exc:
            return self.error(400, f"invalid JSON: {exc}", "invalid_request")

        model = body.get("model")
        if not model:
            return self.error(400, "missing model", "invalid_request")

        tools = describe_tools(filter_tools(normalize_tools(body.get("tools"))))
        names = {tool_name(tool) for tool in tools}
        messages = merge_system(clean_messages(body.get("messages") or []), model)

        upstream = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": build_options(body),
            "keep_alive": body.get("keep_alive") or KEEP_ALIVE,
        }
        if tools:
            upstream["tools"] = tools
        for key in ("think", "format"):
            if key in body:
                upstream[key] = body[key]

        log.debug(
            "model=%s tools=%s messages=%s ctx=%s",
            model,
            sorted(names),
            len(messages),
            NUM_CTX,
        )

        try:
            _, raw = ollama("POST", "/api/chat", upstream)
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            return self.error(exc.code, exc.read().decode("utf-8", "replace"), "upstream_error")
        except Exception as exc:  # noqa: BLE001
            return self.error(502, f"cannot reach Ollama: {exc}", "upstream_error")

        if "error" in data:
            return self.error(502, str(data["error"]), "upstream_error")

        message = data.get("message") or {}
        content = message.get("content") or ""
        native_calls = message.get("tool_calls") or []

        calls = []
        for entry in native_calls:
            function = entry.get("function") if isinstance(entry, dict) else None
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not name:
                continue
            args = function.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append((name, normalize_arguments(name, args or {})))

        if not calls:
            calls, content = extract_tool_calls(content, names)
            if calls:
                log.info(
                    "recovered tool call(s) %s from plain JSON (model=%s)",
                    [name for name, _ in calls],
                    model,
                )
        calls = guard_calls(calls, messages)

        finish = (
            "tool_calls"
            if calls
            else ("length" if data.get("done_reason") == "length" else "stop")
        )
        usage = usage_of(data)
        created = int(time.time())
        tool_calls = openai_tool_calls(calls) if calls else []

        if DEBUG and not calls:
            log.debug("no tool calls; content=%r", content[:200])

        if not body.get("stream"):
            message_out = {"role": "assistant", "content": content if not calls else ""}
            if tool_calls:
                for call in tool_calls:
                    call.pop("index", None)
                message_out["tool_calls"] = tool_calls
            return self.send_json(
                200,
                {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:16],
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": message_out,
                            "finish_reason": finish,
                        }
                    ],
                    "usage": usage,
                },
            )

        # Streaming: Ollama was called non-streaming, tokens are delivered at once.
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length here, so the body must be delimited by closing the
        # connection. Without this curl/OpenCode wait forever for more bytes.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.close_connection = True
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            self.wfile.write(sse(chunk(model, created, {"role": "assistant", "content": ""})))
            if tool_calls:
                self.wfile.write(
                    sse(chunk(model, created, {"tool_calls": tool_calls}, finish=None))
                )
            elif content:
                self.wfile.write(sse(chunk(model, created, {"content": content})))
            self.wfile.write(
                sse(chunk(model, created, {}, finish=finish, usage=usage if include_usage else None))
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("client disconnected while streaming")


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log.info("adapter on http://%s:%s -> %s (num_ctx=%s, tools=%s)", HOST, PORT, OLLAMA, NUM_CTX, TOOLS_ENV)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
