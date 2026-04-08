from __future__ import annotations

import json
import multiprocessing as mp

from openai import OpenAI

from .config import load_settings


SETTINGS = load_settings()


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
    try:
        resp = _client(timeout_seconds=timeout_seconds, max_retries=max_retries).chat.completions.create(
            model=SETTINGS.llm_model,
            temperature=0.1,
            max_completion_tokens=max_completion_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        queue.put({"ok": True, "data": json.loads(resp.choices[0].message.content)})
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
