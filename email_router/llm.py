"""Chat Completions adapter for OpenRouter, Google Gemini and local Ollama."""

import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_FREE_MODELS = {GEMINI_DEFAULT_MODEL, "gemini-3.1-flash-lite"}


class ModelError(RuntimeError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def http_error(exc):
    """Preserve HTTP status and numeric retry delay, never provider error bodies."""
    message = f"Model HTTP {exc.code}"
    if exc.code == 429:
        message += ": request rate limit reached; check free quota before another inference"
        retry = (exc.headers or {}).get("Retry-After", "")
        try:
            delay = float(retry)
            if 0 <= delay <= 604800:
                message += f"; retry_after_seconds={delay:g}"
        except (TypeError, ValueError):
            pass
    else:
        message += "; check key, model availability or free quota"
    return ModelError(message, http_status=exc.code)


def network_error(exc):
    """Classify transport failures without leaking URLs, proxy credentials or keys."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS_CERTIFICATE: Python could not verify the HTTPS certificate; check Python CA certificates"
    if isinstance(reason, ssl.SSLError):
        return "TLS_CONNECTION: TLS handshake failed"
    if isinstance(reason, socket.gaierror):
        return "DNS: could not resolve the model endpoint hostname"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "TIMEOUT: model connection or response timed out"
    if isinstance(reason, ConnectionRefusedError):
        return "CONNECTION_REFUSED: endpoint or configured proxy refused the connection"
    return "NETWORK: connection failed (" + type(reason).__name__ + "); run python3 -m email_router.doctor"


def load_env(path=".env"):
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip().replace("_", "").isalnum():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class ChatModel:
    is_live = True

    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "openrouter")
        defaults = {"openrouter": DEFAULT_MODEL, "gemini": GEMINI_DEFAULT_MODEL, "ollama": "qwen3:4b"}
        self.model = os.getenv("LLM_MODEL", defaults.get(self.provider, DEFAULT_MODEL))
        self.generation_config = {"temperature": 0, "max_tokens": 2400}
        if self.provider == "openrouter":
            if self.model != "openrouter/free" and not self.model.endswith(":free"):
                raise ModelError("Only openrouter/free or a specific :free model is allowed")
            self.url = "https://openrouter.ai/api/v1/chat/completions"
            self.key = os.getenv("OPENROUTER_API_KEY", "")
            if not self.key:
                raise ModelError("Set OPENROUTER_API_KEY in local .env (never in chat or source code)")
        elif self.provider == "gemini":
            if self.model not in GEMINI_FREE_MODELS:
                raise ModelError("Gemini model must be one of the configured Free Tier candidates: " + ", ".join(sorted(GEMINI_FREE_MODELS)))
            self.url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            self.key = os.getenv("GEMINI_API_KEY", "")
            if not self.key:
                raise ModelError("Set GEMINI_API_KEY in local .env using a Google AI Studio Free Tier project without billing")
            self.generation_config["reasoning_effort"] = "low"
        elif self.provider == "ollama":
            base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
            parsed = urlparse(base)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ModelError("Ollama endpoint must be local")
            self.url, self.key = base.rstrip("/") + "/chat/completions", ""
        else:
            raise ModelError("Supported backends: openrouter, gemini, ollama")
        self.label = self.provider + ":" + self.model

    def complete(self, messages, tools, timeout=30):
        payload = {"model": self.model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", **self.generation_config,
                   "stream": False}
        if self.provider == "openrouter":
            payload["provider"] = {"require_parameters": True, "max_price": {"prompt": 0, "completion": 0, "request": 0}}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not store provider error bodies or authorization headers in traces.
            raise http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError(network_error(exc)) from None
        if body.get("error") or not body.get("choices"):
            raise ModelError("Provider returned no completion")
        raw = body["choices"][0]["message"]
        # Preserve provider signatures in nested tool_calls.extra_content unchanged.
        message = {k: raw[k] for k in ("role", "content", "tool_calls", "reasoning_details", "extra_content") if k in raw}
        message.setdefault("role", "assistant")
        return {"message": message, "model": body.get("model", self.model),
                "provider": body.get("provider", self.provider), "usage": body.get("usage", {}),
                "response_id": body.get("id"), "finish_reason": body["choices"][0].get("finish_reason")}
