"""
知识库层（P2-P3）

把历史每日快照沉淀为「可检索 / 可问答」的记忆：
- ``store.py``      SQLite 块存储（文本 + 可选向量 BLOB）
- ``chunker.py``    快照 → 文本块（市场叙事 / 交易计划理由 / 信号 / 复盘）
- ``embeddings.py`` 可选云嵌入（OpenAI 兼容）；缺省降级为零依赖中文词法检索
- ``tools.py``      定量只读查询（基于 app.sqlite），供 LLM 调用，杜绝模型瞎算
- ``retriever.py``  元数据过滤 + 向量/词法混合检索
- ``ingest.py``     遍历快照灌库（幂等）
- ``llm_client.py`` requests 直连云 API（对话 + 嵌入，支持流式）
- ``brief.py``      每日解读（结构化 → 叙事，落 SQLite 缓存）
"""
from kb.store import KBStore
from kb.retriever import Retriever
from kb.ingest import ingest_snapshot, ingest_all

__all__ = ["KBStore", "Retriever", "ingest_snapshot", "ingest_all"]

