#!/usr/bin/env python3
# LAYER: compensation
# -*- coding: utf-8 -*-
"""sidecar_models.py — 可选模型侧车（默认关闭）。

三个开关（环境变量，默认 0/未设置）：
  TENDER_LAYOUT=1       版面检测（DocLayout-YOLO / PP-DocLayout）
  TENDER_TABLE_MODEL=1  表结构模型（Table Transformer / PP-Structure）
  TENDER_VLM_OCR=1      难页 VLM OCR（PaddleOCR-VL / 本地 VLM）

原则：装了才用，没装一律返回 None 并记录原因，调用方回退现有规则逻辑；
禁止把侧车当唯一解析路径。
"""
from __future__ import annotations

import importlib.util
import os

FLAG_LAYOUT = "TENDER_LAYOUT"
FLAG_TABLE = "TENDER_TABLE_MODEL"
FLAG_VLM = "TENDER_VLM_OCR"


def enabled(name: str) -> bool:
    return (os.environ.get(name) or "").strip() in {"1", "true", "yes", "on"}


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001
        return False


def layout_status() -> dict:
    """返回 {'enabled', 'available', 'reason'}。"""
    if not enabled(FLAG_LAYOUT):
        return {"enabled": False, "available": False, "reason": "flag_off"}
    for module in ("doclayout_yolo", "paddlex"):
        if _has(module):
            return {"enabled": True, "available": True, "reason": f"module:{module}"}
    return {"enabled": True, "available": False, "reason": "not_installed"}


def table_status() -> dict:
    if not enabled(FLAG_TABLE):
        return {"enabled": False, "available": False, "reason": "flag_off"}
    for module in ("transformers", "paddlex"):
        if _has(module):
            return {"enabled": True, "available": True, "reason": f"module:{module}"}
    return {"enabled": True, "available": False, "reason": "not_installed"}


def vlm_status() -> dict:
    if not enabled(FLAG_VLM):
        return {"enabled": False, "available": False, "reason": "flag_off"}
    for module in ("paddleocr", "transformers"):
        if _has(module):
            return {"enabled": True, "available": True, "reason": f"module:{module}"}
    return {"enabled": True, "available": False, "reason": "not_installed"}


def analyse_page(page) -> dict | None:
    """版面检测：返回 {'toc': bool, 'tables': [bbox]}；不可用时 None。"""
    status = layout_status()
    if not status["available"]:
        return None
    try:  # 真正接入时在这里调用模型；当前保留最小实现
        from doclayout_yolo import YOLOv10  # type: ignore  # noqa: F401

        return None
    except Exception:  # noqa: BLE001
        return None


def validate_table_columns(page, rows) -> bool | None:
    """表结构模型校验列对齐；不可用时 None（调用方保持规则结论）。"""
    status = table_status()
    if not status["available"]:
        return None
    return None


def status_summary() -> dict:
    return {"layout": layout_status(), "table": table_status(), "vlm": vlm_status()}
