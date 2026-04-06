from __future__ import annotations

import json

from openai import OpenAI

from .config import load_settings


SETTINGS = load_settings()
CLIENT = OpenAI(base_url=SETTINGS.llm_base_url, api_key=SETTINGS.llm_api_key, timeout=45, max_retries=1)


def chat_json(system: str, user: str) -> dict:
    resp = CLIENT.chat.completions.create(
        model=SETTINGS.llm_model,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def chat_text(system: str, user: str) -> str:
    resp = CLIENT.chat.completions.create(
        model=SETTINGS.llm_model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()
