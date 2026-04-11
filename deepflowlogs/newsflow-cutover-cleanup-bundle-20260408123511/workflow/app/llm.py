from __future__ import annotations

import json
import logging
import multiprocessing as mp
import re
import time

from openai import OpenAI

from .config import load_settings


SETTINGS = load_settings()
LOGGER = logging.getLogger("newsflow.llm")


class LLMStructuredOutputError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "technical_failure",
        raw_excerpt: str = "",
        finish_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.raw_excerpt = raw_excerpt
        self.finish_reason = finish_reason

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.error_kind:
            parts.append(f"error_kind={self.error_kind}")
        if self.finish_reason:
            parts.append(f"finish_reason={self.finish_reason}")
        if self.raw_excerpt:
            parts.append(f"raw_excerpt={self.raw_excerpt}")
        return " | ".join(parts)


def _excerpt(text: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", (text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _parse_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise LLMStructuredOutputError(
            "empty model response",
            error_kind="technical_parse_failed",
        )
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
        raise LLMStructuredOutputError(
            "no json object found in model response",
            error_kind="technical_parse_failed",
            raw_excerpt=_excerpt(raw),
        )
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
    try:
        data = json.loads(candidate)
    except Exception as exc:
        raise LLMStructuredOutputError(
            f"{exc.__class__.__name__}: {exc}",
            error_kind="technical_parse_failed",
            raw_excerpt=_excerpt(candidate),
        ) from exc
    if not isinstance(data, dict):
        raise LLMStructuredOutputError(
            "model response json is not an object",
            error_kind="technical_parse_failed",
            raw_excerpt=_excerpt(candidate),
        )
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
    start_ts = time.time()
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
        LOGGER.info(
            "chat_json.start model=%s timeout_seconds=%s max_retries=%s max_completion_tokens=%s system_len=%s user_len=%s",
            SETTINGS.llm_model,
            timeout_seconds,
            max_retries,
            max_completion_tokens,
            len(system or ""),
            len(user or ""),
        )
        resp = client.chat.completions.create(**base_args)
        raw = resp.choices[0].message.content or ""
        finish_reason = getattr(resp.choices[0], "finish_reason", "")
        LOGGER.info(
            "chat_json.response model=%s finish_reason=%s content_len=%s reasoning_present=%s elapsed_ms=%s",
            getattr(resp, "model", SETTINGS.llm_model),
            finish_reason,
            len(raw),
            bool(getattr(resp.choices[0].message, "reasoning_content", None)),
            int((time.time() - start_ts) * 1000),
        )
        queue.put({"ok": True, "data": _parse_json_object(raw)})
        return
    except Exception as exc:
        raw = ""
        finish_reason = ""
        if "resp" in locals():
            try:
                raw = resp.choices[0].message.content or ""
                finish_reason = getattr(resp.choices[0], "finish_reason", "")
            except Exception:
                raw = ""
                finish_reason = ""
        if isinstance(exc, LLMStructuredOutputError):
            error_kind = exc.error_kind
            raw_excerpt = exc.raw_excerpt or _excerpt(raw)
            error_text = str(exc)
        else:
            error_kind = "technical_transport_failed" if raw else "transport_failed"
            raw_excerpt = _excerpt(raw) if raw else ""
            error_text = f"{exc.__class__.__name__}: {exc}"
            if raw_excerpt:
                error_text += f" | raw_excerpt={raw_excerpt}"
        LOGGER.warning(
            "chat_json.failed model=%s error_kind=%s finish_reason=%s error=%s elapsed_ms=%s",
            SETTINGS.llm_model,
            error_kind,
            finish_reason,
            error_text,
            int((time.time() - start_ts) * 1000),
        )
        queue.put(
            {
                "ok": False,
                "error": error_text,
                "error_kind": error_kind,
                "raw_excerpt": raw_excerpt,
                "finish_reason": finish_reason,
            }
        )


def _chat_text_worker(
    queue: mp.Queue,
    system: str,
    user: str,
    timeout_seconds: int,
    max_retries: int,
    max_completion_tokens: int,
) -> None:
    try:
        start_ts = time.time()
        LOGGER.info(
            "chat_text.start model=%s timeout_seconds=%s max_retries=%s max_completion_tokens=%s system_len=%s user_len=%s",
            SETTINGS.llm_model,
            timeout_seconds,
            max_retries,
            max_completion_tokens,
            len(system or ""),
            len(user or ""),
        )
        resp = _client(timeout_seconds=timeout_seconds, max_retries=max_retries).chat.completions.create(
            model=SETTINGS.llm_model,
            temperature=0.3,
            max_completion_tokens=max_completion_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        LOGGER.info(
            "chat_text.response model=%s finish_reason=%s content_len=%s reasoning_present=%s elapsed_ms=%s",
            getattr(resp, "model", SETTINGS.llm_model),
            getattr(resp.choices[0], "finish_reason", ""),
            len(resp.choices[0].message.content or ""),
            bool(getattr(resp.choices[0].message, "reasoning_content", None)),
            int((time.time() - start_ts) * 1000),
        )
        queue.put({"ok": True, "data": resp.choices[0].message.content.strip()})
    except Exception as exc:
        LOGGER.exception(
            "chat_text.worker_exception model=%s timeout_seconds=%s max_completion_tokens=%s",
            SETTINGS.llm_model,
            timeout_seconds,
            max_completion_tokens,
        )
        queue.put({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})


def _run_with_hard_timeout(target, *args, timeout_seconds: int):
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=target, args=(queue, *args))
    started = time.time()
    proc.start()
    proc.join(timeout_seconds + 5)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        LOGGER.warning(
            "llm.hard_timeout timeout_seconds=%s target=%s elapsed_ms=%s",
            timeout_seconds,
            getattr(target, "__name__", str(target)),
            int((time.time() - started) * 1000),
        )
        raise TimeoutError(f"subprocess hard timeout after {timeout_seconds}s")
    if queue.empty():
        LOGGER.warning(
            "llm.empty_result timeout_seconds=%s target=%s elapsed_ms=%s",
            timeout_seconds,
            getattr(target, "__name__", str(target)),
            int((time.time() - started) * 1000),
        )
        raise RuntimeError("llm subprocess exited without result")
    result = queue.get()
    if not result.get("ok"):
        if result.get("error_kind"):
            LOGGER.warning(
                "llm.result_error timeout_seconds=%s target=%s elapsed_ms=%s error_kind=%s error=%s",
                timeout_seconds,
                getattr(target, "__name__", str(target)),
                int((time.time() - started) * 1000),
                result.get("error_kind"),
                result.get("error", "unknown llm subprocess error"),
            )
            raise LLMStructuredOutputError(
                result.get("error", "unknown llm subprocess error"),
                error_kind=result.get("error_kind") or "technical_failure",
                raw_excerpt=result.get("raw_excerpt") or "",
                finish_reason=result.get("finish_reason") or "",
            )
        LOGGER.warning(
            "llm.result_error timeout_seconds=%s target=%s elapsed_ms=%s error=%s",
            timeout_seconds,
            getattr(target, "__name__", str(target)),
            int((time.time() - started) * 1000),
            result.get("error", "unknown llm subprocess error"),
        )
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
