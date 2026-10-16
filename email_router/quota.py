"""Read OpenRouter free quota without inference; print only safe numeric fields."""

import json
import os
import urllib.error
import urllib.request

from .llm import http_error, load_env, network_error


def safe_quota(data):
    answer = {"status": "ok", "free_model_daily_requests": {}}
    raw = data.get("free_model_daily_requests") or {}
    for key in ("used", "limit", "remaining"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            answer["free_model_daily_requests"][key] = value
    if not answer["free_model_daily_requests"]:
        answer["free_model_daily_requests"] = None
        answer["note"] = "API did not report a daily free quota; this does not prove requests are unlimited"
    if isinstance(data.get("limit_remaining"), (int, float)):
        answer["key_spending_limit_remaining"] = data["limit_remaining"]
    return answer


def main():
    load_env()
    if os.getenv("LLM_PROVIDER") == "gemini":
        print("Gemini: проверь квоту выбранного проекта в Google AI Studio: https://aistudio.google.com/usage?tab=rate-limit")
        print("Эта команда не вызывает модель и не обращается к OpenRouter при выбранном Gemini.")
        return 0
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        print("OPENROUTER_API_KEY is not configured in .env")
        return 2
    request = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                     headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            report = safe_quota(json.load(response)["data"])
    except urllib.error.HTTPError as exc:
        print(str(http_error(exc)))
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(network_error(exc))
        return 1
    except (ValueError, KeyError, TypeError):
        print("OpenRouter quota response format was not recognized")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    remaining = (report["free_model_daily_requests"] or {}).get("remaining")
    return 1 if remaining is not None and remaining <= 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
