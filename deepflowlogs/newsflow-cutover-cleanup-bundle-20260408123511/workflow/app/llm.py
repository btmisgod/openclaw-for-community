from __future__ import annotations

import json
import multiprocessing as mp
import re

from openai import OpenAI

from .config import load_settings


SETTINGS = load_settings()


def _excerpt(text: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", (text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _parse_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("no json object found in model response")
    candidate = raw[start : end + 1]
    for cleaned in [
        candidate,
        re.sub(r",(\s*[}\]])", r"\1", candidate),
    ]:
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("model response json is not an object")
    return data


def _client(*, timeout_seconds: int = 45, max_retries: int = 1) -> OpenAI:
    return OpenAI(
        base_url=SETTINGS.llm_base_url,
        api_key=SETTINGS.llm_api_key,
        timeout=timeout_seconds,
        max_retries=max_retries,
    )


def _chat_json_worker(
    queue: mp.Queue,
    system: str,
    user: str,
    timeout_seconds: int,
    max_retries: int,
    max_completion_tokens: int,
) -> None:
    last_error = "unknown llm json error"
    try:
        client = _client(timeout_seconds=timeout_seconds, max_retries=max_retries)
        base_args = {
            "model": SETTINGS.llm_model,
            "temperature": 0.0,
            "max_completion_tokens": max_completion_tokens,
            "messages": [
                {"role": "system", "content": f"{system}\n只输出一个 JSON 对象，不要输出额外解释。"},
                {"role": "user", "content": user},
            ],
        }
        for extra_args in (
            {},
            {"response_format": {"type": "json_object"}},
        ):
            try:
                resp = client.chat.completions.create(**base_args, **extra_args)
                raw = resp.choices[0].message.content or ""
                queue.put({"ok": True, "data": _parse_json_object(raw)})
                return
            except Exception as exc:
                raw = ""
                if "resp" in locals():
                    try:
                        raw = resp.choices[0].message.content or ""
                    except Exception:
                        raw = ""
                last_error = f"{exc.__class__.__name__}: {exc}"
                if raw:
                    last_error += f" | raw_excerpt={_excerpt(raw)}"
                if extra_args:
                    continue
        queue.put({"ok": False, "error": last_error})
    except Exception as exc:
        queue.put({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})


def _chat_text_worker(
    queue: mp.Queue,
    system: str,
    user: str,
    timeout_seconds: int,
    max_retries: int,
    max_completion_tokens: int,
) -> None:
    try:
        resp = _client(timeout_seconds=timeout_seconds, max_retries=max_retries).chat.completions.create(
            model=SETTINGS.llm_model,
            temperature=0.3,
            max_completion_tokens=max_completion_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        queue.put({"ok": True, "data": resp.choices[0].message.content.strip()})
    except Exception as exc:
        queue.put({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})


def _run_with_hard_timeout(target, *args, timeout_seconds: int):
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=target, args=(queue, *args))
    proc.start()
    proc.join(timeout_seconds + 5)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        raise TimeoutError(f"subprocess hard timeout after {timeout_seconds}s")
    if queue.empty():
        raise RuntimeError("llm subprocess exited without result")
    result = queue.get()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown llm subprocess error"))
    return result["data"]


def chat_json(
    system: str,
    user: str,
    *,
    timeout_seconds: int = 45,
    max_retries: int = 1,
    max_completion_tokens: int = 700,
) -> dict:
    return _run_with_hard_timeout(
        _chat_json_worker,
        system,
        user,
        timeout_seconds,
        max_retries,
        max_completion_tokens,
        timeout_seconds=timeout_seconds,
    )


def chat_text(
    system: str,
    user: str,
    *,
    timeout_seconds: int = 45,
    max_retries: int = 1,
    max_completion_tokens: int = 900,
) -> str:
    return _run_with_hard_timeout(
        _chat_text_worker,
        system,
        user,
        timeout_seconds,
        max_retries,
        max_completion_tokens,
        timeout_seconds=timeout_seconds,
    )
