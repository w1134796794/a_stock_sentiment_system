"""
通用 JSON 序列化工具。

``data_dict`` 里混杂了 DataFrame、numpy 标量、dataclass、Enum、datetime 以及各种
业务对象（``RiskGateResult`` / ``PatternSignal`` …）。这里提供一个**容错**的
``to_jsonable``：尽最大努力转成可被 ``json.dumps`` 序列化的结构，遇到无法识别的
类型回退为 ``str(obj)``，绝不抛异常。

``tabulate`` 把任意来源（DataFrame / list / dict / 对象）规整成 ``(columns, rows)``，
供前端通用表格渲染与 sheet 浏览使用。
"""
from __future__ import annotations

import dataclasses
import datetime
import math
from enum import Enum
from typing import Any, Dict, List, Tuple

import pandas as pd

try:  # numpy 可选：缺失也不影响（pandas 已依赖 numpy，这里只是防御）
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

_MAX_DEPTH = 12


def _is_nan(value: Any) -> bool:
    """仅对标量判断 NaN/NaT，避免对数组调用 pd.isna 触发歧义异常。"""
    try:
        if value is None:
            return True
        if isinstance(value, float):
            return math.isnan(value) or math.isinf(value)
        # pandas 的 NaT / NA 单例
        if value is pd.NaT:
            return True
        if value is getattr(pd, "NA", object()):
            return True
        return False
    except Exception:
        return False


def to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """把任意对象尽力转换成 JSON 可序列化结构。"""
    if _depth > _MAX_DEPTH:
        return str(obj)

    # 基础类型
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj

    # numpy 标量 / 数组
    if np is not None:
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            v = float(obj)
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(obj, np.ndarray):
            return [to_jsonable(x, _depth + 1) for x in obj.tolist()]

    # pandas 缺失值单例
    if obj is pd.NaT or obj is getattr(pd, "NA", object()):
        return None

    # 时间类型
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else obj.isoformat()
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()

    # 枚举
    if isinstance(obj, Enum):
        return to_jsonable(obj.value, _depth + 1)

    # pandas 容器
    if isinstance(obj, pd.DataFrame):
        return [
            {str(k): to_jsonable(v, _depth + 1) for k, v in rec.items()}
            for rec in obj.to_dict(orient="records")
        ]
    if isinstance(obj, pd.Series):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}

    # dataclass 实例（注意排除 dataclass 类型本身）
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: to_jsonable(getattr(obj, f.name, None), _depth + 1)
            for f in dataclasses.fields(obj)
        }

    # 映射
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}

    # 序列 / 集合
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(x, _depth + 1) for x in obj]

    # 自带 to_dict() 的业务对象
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return to_jsonable(to_dict(), _depth + 1)
        except Exception:
            pass

    # 普通对象：取其 __dict__（跳过私有属性）
    if hasattr(obj, "__dict__"):
        try:
            return {
                str(k): to_jsonable(v, _depth + 1)
                for k, v in vars(obj).items()
                if not str(k).startswith("_")
            }
        except Exception:
            pass

    if _is_nan(obj):
        return None
    return str(obj)


def tabulate(source: Any) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    把任意来源规整成 ``(columns, rows)``：

    - DataFrame              → 原列序 + records
    - list[dict] / list[obj] → 并集列 + 每行 dict
    - dict                   → 两列「字段 / 值」键值表
    - 其它                   → 单列单元格
    """
    if source is None:
        return [], []

    if isinstance(source, pd.DataFrame):
        if source.empty:
            return [str(c) for c in source.columns], []
        columns = [str(c) for c in source.columns]
        rows = to_jsonable(source)
        return columns, rows

    if isinstance(source, dict):
        rows = [{"字段": str(k), "值": _scalarize(to_jsonable(v))} for k, v in source.items()]
        return ["字段", "值"], rows

    if isinstance(source, (list, tuple)):
        rows: List[Dict[str, Any]] = []
        columns: List[str] = []
        seen = set()
        for item in source:
            j = to_jsonable(item)
            if not isinstance(j, dict):
                j = {"值": _scalarize(j)}
            rows.append(j)
            for k in j.keys():
                if k not in seen:
                    seen.add(k)
                    columns.append(k)
        return columns, rows

    # 标量 / 其它
    return ["值"], [{"值": _scalarize(to_jsonable(source))}]


def _scalarize(value: Any) -> Any:
    """把嵌套结构压成适合单元格展示的字符串；标量原样返回。"""
    if isinstance(value, (dict, list)):
        import json

        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return value
