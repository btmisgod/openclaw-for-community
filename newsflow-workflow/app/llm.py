from __future__ import annotations

import json

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


def chat_json(
    system: str,
    user: str,
    *,
    timeout_seconds: int = 45,
    max_retries: int = 1,
    max_completion_tokens: int = 700,
) -> dict:
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
    return json.loads(resp.choices[0].message.content)


def chat_text(
    system: str,
    user: str,
    *,
    timeout_seconds: int = 45,
    max_retries: int = 1,
    max_completion_tokens: int = 900,
) -> str:
    resp = _client(timeout_seconds=timeout_seconds, max_retries=max_retries).chat.completions.create(
        model=SETTINGS.llm_model,
        temperature=0.3,
        max_completion_tokens=max_completion_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()
