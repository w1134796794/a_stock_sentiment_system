"""
快照层（P0）

把喂给 Excel 报表的同一份 ``data_dict`` 落成结构化产物，供 Web 页面与知识库复用：
- ``snapshots/{date}.json``：整页 JSON 快照（前端直读）
- ``app.sqlite``：结构化索引（每日快照 / 交易计划 / 信号）
- ``factors.duckdb``：因子大表（定量查询，可选）

设计原则：**对现有分析流水线零侵入**——只在报表生成处旁挂一次 ``SnapshotWriter.write``，
且任何失败都不得影响既有 Excel 产出（调用方负责 try/except 包裹）。
"""
from snapshot.serialize import to_jsonable, tabulate
from snapshot.writer import SnapshotWriter, build_snapshot
from snapshot.reader import SnapshotReader

__all__ = [
    "to_jsonable",
    "tabulate",
    "SnapshotWriter",
    "build_snapshot",
    "SnapshotReader",
]