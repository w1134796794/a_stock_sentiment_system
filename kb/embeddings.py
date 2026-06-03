"""
可选向量嵌入。

走 OpenAI 兼容的 ``/embeddings`` 端点（用 requests 直连），默认指向 DashScope
（``text-embedding-v3``，复用已有的 DASHSCOPE_API_KEY）。未配置 key 时
``get_embedder()`` 返回 ``None`` —— 检索层据此降级为词法检索，全程不报错。
"""
from __future__ import annotations

from typing import List, Optional

import loguru
import numpy as np

logger = loguru.logger


class CloudEmbedder:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, texts: List[str], batch_size: int = 16) -> Optional[np.ndarray]:
        """返回 (n, dim) 向量矩阵；任一环节失败返回 None（触发词法回退）。"""
        if not texts:
            return None
        import requests

        vectors: List[List[float]] = []
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/embeddings"
        try:
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i + batch_size]
                resp = requests.post(
                    url, headers=headers,
                    json={"model": self.model, "input": batch},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                if len(data) != len(batch):
                    logger.warning(f"[KB] 嵌入返回数量不匹配（{len(data)}≠{len(batch)}），回退词法检索")
                    return None
                for item in sorted(data, key=lambda d: d.get("index", 0)):
                    vectors.append(item["embedding"])
            return np.asarray(vectors, dtype=np.float32)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[KB] 云嵌入调用失败，回退词法检索: {e}")
            return None


def get_embedder() -> Optional[CloudEmbedder]:
    """按配置构造嵌入器；未配置 key 返回 None。"""
    try:
        from config.settings import LLM_CONFIG
    except Exception:
        return None
    key = (LLM_CONFIG.get("embed_api_key") or "").strip()
    if not key or key == "your-api-key-here":
        return None
    return CloudEmbedder(
        api_key=key,
        base_url=LLM_CONFIG.get("embed_base_url", ""),
        model=LLM_CONFIG.get("embed_model", "text-embedding-v3"),
        timeout=int(LLM_CONFIG.get("timeout", 60)),
    )