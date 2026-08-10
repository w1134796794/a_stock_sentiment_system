"""Compact duplicated cross-strategy JSON artifacts.

The command is a dry run unless ``--apply`` is provided.  Full per-strategy
artifacts are not touched, so rejected rows and research diagnostics remain
available.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import WEB_DATA_DIR
from core.etl.daily_pipeline import _compact_strategy_result


def compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": payload.get("trade_date"),
        "primary": payload.get("primary"),
        "results": {
            str(strategy_id): _compact_strategy_result(result)
            for strategy_id, result in (payload.get("results") or {}).items()
            if isinstance(result, dict)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="压缩重复的策略组合汇总文件")
    parser.add_argument("--apply", action="store_true", help="实际覆盖文件；默认仅预演")
    parser.add_argument("--before", default="", help="只处理该交易日及以前，格式 YYYYMMDD")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(WEB_DATA_DIR) / "screening" / "combinations",
    )
    args = parser.parse_args()

    count = 0
    original_bytes = 0
    compact_bytes = 0
    for path in sorted(args.directory.glob("strategy_runs_*.json")):
        trade_date = path.stem.rsplit("_", 1)[-1]
        if args.before and trade_date > args.before:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            compact = compact_payload(payload)
            encoded = json.dumps(
                compact, ensure_ascii=False, separators=(",", ":"), default=str,
            ).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {path.name}: {exc}")
            continue
        current_size = path.stat().st_size
        if len(encoded) >= current_size:
            continue
        count += 1
        original_bytes += current_size
        compact_bytes += len(encoded)
        if args.apply:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    saved = original_bytes - compact_bytes
    mode = "已压缩" if args.apply else "预演"
    print(
        f"{mode}: {count} 个文件，"
        f"{original_bytes / 1024 / 1024:.1f}MB -> "
        f"{compact_bytes / 1024 / 1024:.1f}MB，"
        f"可释放 {saved / 1024 / 1024:.1f}MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
