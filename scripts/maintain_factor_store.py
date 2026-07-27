"""Archive or compact factor_value_long with an auditable dry-run default."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.etl.factor_store_maintenance import FactorStoreMaintenance


def main() -> int:
    parser = argparse.ArgumentParser(description="维护因子长表")
    parser.add_argument("--archive-before", default="", help="归档该日期之前的数据，YYYYMMDD")
    parser.add_argument("--compact-live", action="store_true", help="长表只保留解释/稀疏因子并去重")
    parser.add_argument("--apply", action="store_true", help="实际写入；缺省只预览")
    parser.add_argument("--prune", action="store_true", help="归档校验后删除 DuckDB 旧分区")
    args = parser.parse_args()
    service = FactorStoreMaintenance()
    if args.compact_live:
        result = service.compact_live(dry_run=not args.apply)
    elif args.archive_before:
        result = service.archive_before(
            args.archive_before, prune=bool(args.prune), dry_run=not args.apply,
        )
    else:
        parser.error("必须提供 --archive-before 或 --compact-live")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
