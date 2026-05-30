"""
启动 Web 看板（P1）。

    python run_web.py            # 默认 127.0.0.1:8000（仅本机）
    python run_web.py --port 9000

仅读取快照产物，无需 tushare 等重依赖。
"""
import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="A股情绪系统 · Web 看板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    uvicorn.run("web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()

##############################################################################
# [17/28] scripts/build_kb.py   (NEW)
##############################################################################

"""
回填知识库：遍历 webdata/snapshots 下全部快照灌入 kb.sqlite。

    python scripts/build_kb.py

配了 EMBEDDING_API_KEY / DASHSCOPE_API_KEY 则写入向量，否则按词法检索入库（离线可用）。
可重复运行，按日期幂等覆盖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import SNAPSHOT_DIR, KB_DB_PATH  # noqa: E402
from kb.store import KBStore  # noqa: E402
from kb.ingest import ingest_all  # noqa: E402
from kb.embeddings import get_embedder  # noqa: E402


def main() -> None:
    store = KBStore(KB_DB_PATH)
    embedder = get_embedder()
    print(f"嵌入模式: {'云向量' if embedder else '词法检索（未配置 embedding key）'}")
    result = ingest_all(SNAPSHOT_DIR, store, embedder)
    print(f"完成：{len(result)} 天，共 {store.count()} 块 → {KB_DB_PATH}")


if __name__ == "__main__":
    main()