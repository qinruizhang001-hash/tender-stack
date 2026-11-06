"""规则引擎：把确定性判定插在 LLM 结论之前。

四个脚本（数值 / 时间 / 实体漏项 / 文件属性）住在 tender-stack/adapter，
本模块按 `BID_RULES_PATH` 或相对路径加载；加载不到时**不拦**，但会在返回值里
标记 available=False，调用方应记录告警——静默失效比误判更危险。

判定语义
--------
fail  ：规则能确定不满足（数值不足 / 时间逾期 / 资质漏项）→ 覆盖模型，判未响应
grey  ：规则判不了（方向不明、跨月天换算、多处数值）→ 不允许模型给「满足」
pass  ：规则没意见，交回模型
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

FAIL = "fail"
GREY = "grey"
PASS = "pass"

logger = logging.getLogger(__name__)

_LOADED: Optional[tuple[Any, Any, Any]] = None
_RULES_DIR: Optional[Path] = None
_WARNED = False

UNAVAILABLE_REASON = "规则模块未加载（BID_RULES_PATH 或 adapter 目录不可达）"


def rules_dir() -> Optional[Path]:
    """定位四个脚本所在目录：优先 BID_RULES_PATH，其次仓库内的 adapter。"""
    global _RULES_DIR
    if _RULES_DIR is not None:
        return _RULES_DIR or None
    candidates: list[Path] = []
    env = os.environ.get("BID_RULES_PATH")
    if env:
        candidates.append(Path(env))
    # app/services/rule_engine.py → parents[5] 是 tender-stack 根
    candidates.append(Path(__file__).resolve().parents[5] / "adapter")
    for path in candidates:
        if (path / "numeric_check.py").exists():
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            _RULES_DIR = path
            return path
    _RULES_DIR = Path()
    return None


def _load() -> tuple[Any, Any, Any] | tuple[()]:
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    if rules_dir() is None:
        _LOADED = ()
        return _LOADED
    try:
        import missing_item_check
        import numeric_check
        import time_check

        _LOADED = (numeric_check, time_check, missing_item_check)
    except ImportError:  # pragma: no cover - 部署缺依赖时降级
        _LOADED = ()
    return _LOADED


def available() -> bool:
    """规则脚本是否可用。健康检查与日志都用它，避免"静默降级成纯模型"。"""
    return bool(_load())


def _warn_unavailable() -> None:
    """每个进程只告警一次，避免每条要求刷一行日志。"""
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    logger.warning(
        "规则引擎未加载：%s。比对将退化为纯模型判定。请检查 BID_RULES_PATH 或 adapter 目录。",
        UNAVAILABLE_REASON,
    )


def evaluate_text(requirement: str, evidence: str) -> dict[str, Any]:
    """对「要求 × 证据文本」跑数值 / 时间 / 漏项三条规则。"""
    mods = _load()
    if not mods:
        _warn_unavailable()
        return {
            "status": PASS, "label": "", "reason": UNAVAILABLE_REASON,
            "checks": [], "available": False,
        }

    numeric, time_check, missing = mods
    checks: list[dict[str, str]] = []

    # 只有"要求里真的出现了**带单位的**数值/时间"才让规则介入：
    # · 「ISO13485」里的 13485 是证号不是数量，不能被当成数值比
    # · 「没有相关数值」也不能被误当成"判不了"，否则正常条款全被打成灰区
    parsed_numeric = numeric.parse(requirement)
    if parsed_numeric.number is not None and parsed_numeric.number.unit:
        numeric_result = numeric.explain(requirement, evidence)
        if numeric_result["result"] == numeric.FAIL:
            checks.append({"rule": "数值", "status": FAIL, "reason": numeric_result["reason"]})
        elif numeric_result["result"] == numeric.REVIEW:
            checks.append({"rule": "数值", "status": GREY, "reason": numeric_result["reason"]})

    has_time = bool(time_check.parse_date(requirement) or time_check.parse_duration(requirement))
    if has_time:
        time_result = time_check.explain(requirement, evidence)
        if time_result["result"] in (time_check.SHORT, time_check.EXPIRED):
            checks.append({"rule": "时间", "status": FAIL, "reason": time_result["reason"]})
        elif time_result["result"] == time_check.REVIEW:
            checks.append({"rule": "时间", "status": GREY, "reason": time_result["reason"]})

    entities = missing.extract_entities(requirement)
    if entities:
        checker = missing.MissingItemChecker([evidence or ""])
        results = [checker.check(entity) for entity in entities]
        absent = [item.entity for item in results if item.status == missing.MISSING]
        if absent:
            checks.append({"rule": "漏项", "status": FAIL, "reason": "证据中未出现：" + "、".join(absent)})

    failures = [item for item in checks if item["status"] == FAIL]
    greys = [item for item in checks if item["status"] == GREY]
    status = FAIL if failures else GREY if greys else PASS
    matched = failures or greys
    return {
        "status": status,
        "label": "、".join(dict.fromkeys(item["rule"] for item in matched)),
        "reason": "；".join(item["reason"] for item in matched),
        "checks": checks,
        "available": True,
    }


def evaluate_file(path: str | Path) -> Optional[dict[str, Any]]:
    """文档级检查：暗标泄露 / 修订记录。返回 None 表示无法读取或模块缺失。"""
    mods = _load()
    if not mods:
        return None
    try:
        from file_meta_check import check_file
    except ImportError:  # pragma: no cover
        return None
    try:
        return check_file(Path(path)).as_dict()
    except Exception:  # noqa: BLE001 - 文件损坏不应拖垮整个比对
        return None
