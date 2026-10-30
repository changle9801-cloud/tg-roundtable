"""Claude through the Anthropic API (pay per token), using the official SDK."""

from __future__ import annotations

import logging

import anthropic

from .api import HistoryParticipant
from .base import ParticipantError, image_data

log = logging.getLogger(__name__)

MAX_TOKENS = 16000
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
MAX_CONTINUATIONS = 3  # server tools may pause a long turn; resume it this many times
# Models whose safety classifiers may decline a request; with "default" fallbacks the API
# re-runs a declined request on Anthropic's recommended model instead of refusing.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAPI(HistoryParticipant):
    def _client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.cfg.base_url or None,
            timeout=self.timeout,
            max_retries=2,
        )

    def uses_fallbacks(self) -> bool:
        # Only on the first-party API: a custom base_url may be a proxy without the beta.
        return self.cfg.model in FALLBACK_MODELS and not self.cfg.base_url

    def with_images(self, prompt, images) -> list[dict]:
        blocks: list[dict] = []
        for image in images:
            mime, data = image_data(image)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
        return [*blocks, {"type": "text", "text": prompt}]

    def build_request(self, history: list[dict]) -> dict:
        request = {
            "model": self.cfg.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system_prompt,
            "messages": history,
            # The system prompt is the same on every turn; cache it.
            "cache_control": {"type": "ephemeral"},
        }
        if self.cfg.effort:
            request["output_config"] = {"effort": self.cfg.effort}
        if self.cfg.web_search:
            request["tools"] = [WEB_SEARCH_TOOL]  # runs on Anthropic's side; read-only
        if self.cfg.params:
            request["extra_body"] = dict(self.cfg.params)
        return request

    async def complete(self, history: list[dict]) -> str:
        request = self.build_request(history)
        try:
            async with self._client() as client:
                response = await self._create(client, request)
                for _ in range(MAX_CONTINUATIONS):
                    if response.stop_reason != "pause_turn":
                        break
                    # A paused server-tool turn resumes by sending its content back as-is.
                    request["messages"] = [*request["messages"], {"role": "assistant", "content": response.content}]
                    response = await self._create(client, request)
        except anthropic.AuthenticationError:
            raise ParticipantError("API key 无效") from None
        except anthropic.PermissionDeniedError as e:
            raise ParticipantError(f"没有权限：{e.message}") from None
        except anthropic.NotFoundError:
            raise ParticipantError(f"找不到模型 {self.cfg.model}") from None
        except anthropic.RateLimitError:
            raise ParticipantError("请求太频繁或额度已用完，稍后再试") from None
        except anthropic.BadRequestError as e:
            raise ParticipantError(f"请求被拒绝：{e.message}") from None
        except anthropic.APIStatusError as e:
            raise ParticipantError(f"Claude API 出错（HTTP {e.status_code}）：{e.message}") from None
        except anthropic.APITimeoutError:
            raise ParticipantError(f"超过 {self.timeout:.0f} 秒没有回应") from None
        except anthropic.APIConnectionError as e:
            raise ParticipantError(f"网络错误：{e}") from None

        log.debug("%s usage: %s", self.id, response.usage)
        if response.stop_reason == "refusal":
            details = response.stop_details
            reason = (details.explanation or details.category) if details else None
            raise ParticipantError(f"Claude 拒绝回答：{reason or '未说明原因'}")
        return "".join(block.text for block in response.content if block.type == "text")

    async def _create(self, client: anthropic.AsyncAnthropic, request: dict):
        if self.uses_fallbacks():
            return await client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
        return await client.messages.create(**request)

    async def list_models(self) -> list[str]:
        try:
            async with self._client() as client:
                return sorted([m.id async for m in client.models.list()])
        except anthropic.AuthenticationError:
            raise ParticipantError("API key 无效") from None
        except anthropic.APIStatusError as e:
            raise ParticipantError(f"Claude API 出错（HTTP {e.status_code}）：{e.message}") from None
        except anthropic.APIConnectionError as e:
            raise ParticipantError(f"网络错误：{e}") from None
