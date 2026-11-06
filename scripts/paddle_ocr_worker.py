#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paddle_ocr_worker.py — 在 3.11 侧车环境里跑 PaddleOCR。

两种模式：
  单次：python paddle_ocr_worker.py image.png   → 打印一行 JSON
  常驻：python paddle_ocr_worker.py --serve     → 逐行读 {"path": ...}，逐行回 JSON

常驻模式只加载一次模型——单次模式每次都要重新初始化，一页要 3 分钟，
装好模型后在常驻进程里每页约几秒。
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("FLAGS_use_mkldnn", "0")

_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from paddleocr import PaddleOCR

        kwargs = dict(lang="ch", use_doc_orientation_classify=False, use_doc_unwarping=False)
        try:
            _ENGINE = PaddleOCR(use_textline_orientation=True, enable_mkldnn=False, **kwargs)
        except TypeError:  # 老版本没有这些参数
            _ENGINE = PaddleOCR(**kwargs)
    return _ENGINE


def recognise(path: str) -> dict:
    lines: list[str] = []
    scores: list[float] = []
    for result in _engine().predict(path):
        texts = result.get("rec_texts") or []
        confidences = result.get("rec_scores") or []
        for text, score in zip(texts, confidences):
            value = str(text).strip()
            if value:
                lines.append(value)
                scores.append(round(float(score), 3))
    return {"lines": lines, "scores": scores}


def serve() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
            payload = recognise(request["path"])
            payload["ok"] = True
        except Exception as error:  # noqa: BLE001
            payload = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def ping() -> int:
    try:
        import paddleocr  # noqa: F401
        import paddle  # noqa: F401
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True}, ensure_ascii=False))
    return 0


def main() -> int:
    if "--ping" in sys.argv[1:]:
        return ping()
    if "--serve" in sys.argv[1:]:
        return serve()
    print(json.dumps(recognise(sys.argv[1]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
