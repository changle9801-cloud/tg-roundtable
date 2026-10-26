"""Models reached through an API (pay per token).

DeepSeek, Grok (xAI), Gemini, Doubao (Volcengine Ark) and OpenAI speak the OpenAI chat
completions protocol; Claude has its own (see anthropic_api.py). Unlike the CLIs, an API
keeps no conversation state, so the history lives here.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Sequence

import aiohttp

from ..config import ConfigError
from .base import ActivityHook, Participant, ParticipantError, image_data

log = logging.getLogger(__name__)

# provider -> (base_url, environment variable holding the key)
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "grok": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "doubao": ("https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    # Not OpenAI-compatible: served by AnthropicAPI through the official SDK.
    "anthropic": ("https://api.anthropic.com", "ANTHROPIC_API_KEY"),
}


class HistoryParticipant(Participant):
    """An API member: resolves its key and keeps the conversation history on disk."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cfg = self.cfg
        default_url, default_env = PROVIDERS.get(cfg.provider, ("", ""))
        self.base_url = (cfg.base_url or default_url).rstrip("/")
        env = cfg.api_key_env or default_env
        self.api_key = cfg.api_key or (os.environ.get(env, "") if env else "")
        if not self.api_key:
            raise ConfigError(f"参与者 {cfg.id}：缺少 API key（填写 api_key，或设置环境变量 {env}）。")
        if not cfg.model:
            raise ConfigError(f"参与者 {cfg.id}：API 模式必须填写 model。")
        self.history_dir = self.work_dir / "history"

    # ---- history ----

    def _history_path(self, chat_key: str) -> Path:
        return self.history_dir / f"{chat_key}.json"

    def load_history(self, chat_key: str) -> list[dict]:
        path = self._history_path(chat_key)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("历史记录 %s 损坏，已忽略", path)
            return []

    def save_history(self, chat_key: str, history: list[dict]) -> None:
        # Keep the most recent exchanges, starting on a user turn.
        history = history[-self.cfg.max_history :]
        while history and history[0]["role"] != "user":
            history.pop(0)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        path = self._history_path(chat_key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def reset(self, chat_key: str) -> None:
        self._history_path(chat_key).unlink(missing_ok=True)

    async def reply(
        self, chat_key: str, prompt: str, on_activity: ActivityHook | None = None, images: Sequence[Path] = ()
    ) -> str:
        history = self.load_history(chat_key)
        history.append({"role": "user", "content": prompt})
        # Pictures go with this request only; the saved history keeps the text.
        request = history[:-1] + [{"role": "user", "content": self.with_images(prompt, images)}] if images else history
        text = await self.complete(request)
        history.append({"role": "assistant", "content": text})
        self.save_history(chat_key, history)
        return text

    def with_images(self, prompt: str, images: Sequence[Path]) -> str | list[dict]:
        """A user message holding the text and the pictures, in this API's format."""
        return prompt

    async def complete(self, history: list[dict]) -> str:
        """Send the system prompt plus ``history`` and return the reply text."""
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        raise NotImplementedError


class OpenAICompatAPI(HistoryParticipant):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.base_url:
            raise ConfigError(
                f"参与者 {self.cfg.id}：未知的 provider {self.cfg.provider!r}，"
                f"请填写 base_url，或使用 {sorted(PROVIDERS)} 之一。"
            )

    def with_images(self, prompt: str, images: Sequence[Path]) -> list[dict]:
        parts: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            mime, data = image_data(image)
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        return parts

    def build_payload(self, history: list[dict]) -> dict:
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": self.system_prompt}, *history],
        }
        if self.cfg.effort:
            payload["reasoning_effort"] = self.cfg.effort
        payload.update(self.cfg.params)
        return payload

    async def complete(self, history: list[dict]) -> str:
        data = await self._request("POST", "/chat/completions", self.build_payload(history))
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ParticipantError(f"无法解析返回结果：{str(data)[:200]}") from None
        log.debug("%s usage: %s", self.id, data.get("usage"))
        return _content_text(message.get("content"))

    async def list_models(self) -> list[str]:
        data = await self._request("GET", "/models")
        return sorted(m.get("id", "") for m in data.get("data", []))

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        # trust_env: honour HTTPS_PROXY, which some regions need for these APIs.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            try:
                async with session.request(method, self.base_url + path, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise ParticipantError(f"HTTP {resp.status}：{_error_message(body)}")
                    return json.loads(body)
            except TimeoutError:
                raise ParticipantError(f"超过 {self.timeout:.0f} 秒没有回应") from None
            except aiohttp.ClientError as e:
                raise ParticipantError(f"网络错误：{e}") from None
            except ValueError:
                raise ParticipantError(f"返回的不是 JSON：{body[:200]}") from None


def _content_text(content) -> str:
    if isinstance(content, list):  # some providers return content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body[:300]
    if isinstance(data, list) and data:  # Gemini sometimes wraps errors in a list
        data = data[0]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err)[:300]
