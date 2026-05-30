"""
LLM 客户端 —— requests 直连 OpenAI 兼容接口（默认 DeepSeek）。

不引入额外 SDK；支持非流式与流式（SSE）两种调用。未配置 api_key 时
``is_configured`` 为 False，调用方据此优雅降级，不发起网络请求。
"""
from __future__ import annotations

import json
from typing import Dict, Iterator, List, Optional

import loguru

logger = loguru.logger

_PLACEHOLDERS = {"", "your-api-key-here", "sk-xxx", "your_api_key"}


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 60):
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return self.api_key not in _PLACEHOLDERS

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    def chat(self, messages: List[Dict[str, str]],
             temperature: float = 0.4, max_tokens: Optional[int] = None) -> str:
        if not self.is_configured:
            return "（未配置大模型 API key：请在 .env 设置 LLM_API_KEY / DEEPSEEK_API_KEY 后启用）"
        import requests

        payload: Dict = {"model": self.model, "messages": messages,
                         "temperature": temperature, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            resp = requests.post(f"{self.base_url}/chat/completions",
                                 headers=self._headers(), json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LLM] 调用失败: {e}")
            return f"（大模型调用失败：{e}）"

    def chat_stream(self, messages: List[Dict[str, str]],
                    temperature: float = 0.4) -> Iterator[str]:
        """逐段产出文本增量。未配置或失败时产出一条说明后结束。"""
        if not self.is_configured:
            yield "（未配置大模型 API key：请在 .env 设置 LLM_API_KEY / DEEPSEEK_API_KEY 后启用）"
            return
        import requests

        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "stream": True}
        try:
            with requests.post(f"{self.base_url}/chat/completions", headers=self._headers(),
                               json=payload, timeout=self.timeout, stream=True) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    data = raw[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            yield piece
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LLM] 流式调用失败: {e}")
            yield f"（大模型调用失败：{e}）"


def get_llm_client() -> LLMClient:
    from config.settings import LLM_CONFIG
    return LLMClient(
        api_key=LLM_CONFIG.get("api_key", ""),
        base_url=LLM_CONFIG.get("base_url", "https://api.deepseek.com/v1"),
        model=LLM_CONFIG.get("model", "deepseek-chat"),
        timeout=int(LLM_CONFIG.get("timeout", 60)),
    )
