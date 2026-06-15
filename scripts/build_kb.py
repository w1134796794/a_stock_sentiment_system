"""回填知识库：遍历 webdata/snapshots 下全部快照灌入 kb.sqlite。

用法：
    python scripts/build_kb.py

配了 EMBEDDING_API_KEY / DASHSCOPE_API_KEY 时写入向量；否则按词法检索入库。
可重复运行，按日期幂等覆盖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import KB_DB_PATH, SNAPSHOT_DIR  # noqa: E402
from kb.embeddings import get_embedder  # noqa: E402
from kb.ingest import ingest_all  # noqa: E402
from kb.store import KBStore  # noqa: E402


def main() -> None:
    store = KBStore(KB_DB_PATH)
    embedder = get_embedder()
    print(f"嵌入模式: {'云向量' if embedder else '词法检索（未配置 embedding key）'}")
    result = ingest_all(SNAPSHOT_DIR, store, embedder)
    print(f"完成：{len(result)} 天，共 {store.count()} 块 -> {KB_DB_PATH}")


if __name__ == "__main__":
    main()
