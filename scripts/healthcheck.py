#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""healthcheck.py — 部署健康检查：OCR 侧车预热 + 可选依赖状态。

用法：
  python scripts/healthcheck.py            # OCR 预热失败则退出码 1
  python scripts/healthcheck.py --skip-ocr
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ocr", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "adapter"))
    import ocr_backend
    import sidecar_models

    report = {
        "ocr": ocr_backend.describe(),
        "sidecar": sidecar_models.status_summary(),
    }
    ok = True
    if not args.skip_ocr:
        warm = ocr_backend.warmup()
        report["ocr_warmup"] = warm
        ok = warm

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        print("OCR 预热失败：检查侧车或内置引擎", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
