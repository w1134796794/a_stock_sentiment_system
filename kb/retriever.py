"""
混合检索：先按元数据过滤（日期/类型/个股），再做相似度排序。

- 有向量（配了嵌入 key 且库里有向量）→ 余弦 Top-K。
- 否则 → 零依赖中文词法检索（TF-IDF 余弦，CJK 字 bigram + ASCII 词）。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional

import numpy as np

from kb.store import KBStore

_ASCII = re.compile(r"[a-z0-9.]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    tokens = _ASCII.findall(text)
    cjk = _CJK.findall(text)
    tokens.extend(cjk)                                    # 单字（提召回）
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))    # 字 bigram（提精度）
    return tokens


class Retriever:
    def __init__(self, store: KBStore, embedder: Optional[Any] = None):
        self.store = store
        self.embedder = embedder

    def search(self,
               query: str,
               k: int = 8,
               date_from: Optional[str] = None,
               date_to: Optional[str] = None,
               kinds: Optional[List[str]] = None,
               stock_code: Optional[str] = None) -> List[Dict[str, Any]]:
        candidates = self.store.fetch(
            date_from=date_from, date_to=date_to, kinds=kinds, stock_code=stock_code)
        if not candidates:
            return []

        # --- 向量检索 ---
        if self.embedder is not None and self.store.has_any_embeddings():
            emb_cands = [c for c in candidates if c.get("embedding") is not None]
            if emb_cands:
                qv = self.embedder.embed([query])
                if qv is not None and qv.shape[0] == 1:
                    ranked = self._vector_rank(qv[0], emb_cands)
                    if ranked:
                        return ranked[:k]

        # --- 词法回退 ---
        return self._lexical_rank(query, candidates)[:k]

    # ------------------------------------------------------------------
    @staticmethod
    def _vector_rank(q: np.ndarray, cands: List[Dict]) -> List[Dict]:
        mat = np.vstack([c["embedding"] for c in cands]).astype(np.float32)
        qn = q / (np.linalg.norm(q) + 1e-8)
        mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
        sims = mn @ qn
        order = np.argsort(-sims)
        out = []
        for i in order:
            c = cands[int(i)]
            out.append(_result(c, float(sims[int(i)])))
        return out

    @staticmethod
    def _lexical_rank(query: str, cands: List[Dict]) -> List[Dict]:
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        doc_tokens = [_tokenize(c["text"]) for c in cands]

        # idf
        df: Counter = Counter()
        for toks in doc_tokens:
            for t in set(toks):
                df[t] += 1
        n_docs = len(cands)
        idf = {t: math.log(1 + n_docs / (1 + d)) for t, d in df.items()}

        q_tf = Counter(q_tokens)
        q_vec = {t: (q_tf[t]) * idf.get(t, 0.0) for t in q_tf}
        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

        scored: List[Dict] = []
        for c, toks in zip(cands, doc_tokens):
            if not toks:
                continue
            d_tf = Counter(toks)
            dot = 0.0
            d_sq = 0.0
            for t, f in d_tf.items():
                w = f * idf.get(t, 0.0)
                d_sq += w * w
                if t in q_vec:
                    dot += w * q_vec[t]
            if dot <= 0:
                continue
            score = dot / (q_norm * (math.sqrt(d_sq) or 1.0))
            scored.append(_result(c, round(score, 4)))
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored


def _result(c: Dict, score: float) -> Dict[str, Any]:
    return {
        "id": c["id"], "date": c["date"], "kind": c["kind"],
        "stock_code": c.get("stock_code", ""), "text": c["text"], "score": score,
    }
