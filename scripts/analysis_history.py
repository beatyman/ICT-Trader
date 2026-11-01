"""同标的强制复盘的历史缓存读写。

每次分析：load(symbol) 读取上一次记录（不存在则返回 None → 首次分析）。
每次分析结束：save(symbol, record) 覆盖写入，供下次复盘。
"""
from __future__ import annotations

import json
from pathlib import Path

_HISTORY_DIR = Path(__file__).resolve().parents[1] / "output" / "ict" / "history"


def _path(symbol: str) -> Path:
    return _HISTORY_DIR / f"{symbol}.json"


def load(symbol: str) -> dict | None:
    p = _path(symbol)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(symbol: str, record: dict) -> Path:
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(symbol)
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# 复盘对比的固定字段（写入时尽量齐全）
REVIEW_FIELDS = [
    "symbol", "as_of", "close", "atr", "decision", "htf", "rules",
    "key_levels", "plans", "scenarios", "review_lesson",
]
