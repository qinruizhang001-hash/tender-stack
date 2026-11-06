#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rt_client.py — 通过 HTTP 公开接口驱动 rag-tender，导出要求台账原始数据

设计约束（刻意为之）：
  * 只走 /api/v2/* 的 HTTP 接口，不 import rag-tender 的任何模块，不碰它的数据库文件。
  * 输出落盘为 JSON，供下游 map_to_ledger.py 消费。
  * 这样 rag-tender 升级时，只要接口契约不变，本层就不用改。

用法：
  python adapter/rt_client.py run --file samples/xxx.pdf --out out/armA --project "样本项目"
  python adapter/rt_client.py health
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

API = "/api/v2"
TERMINAL = {"completed", "needs_review", "failed", "canceled"}


class RTClient:
    def __init__(self, base: str = "http://127.0.0.1:8000", timeout: float = 60.0):
        self.base = base.rstrip("/") + API
        # 显式关闭 keep-alive：uvicorn + httpx 复用连接时偶发响应错位
        self.client = httpx.Client(timeout=timeout, headers={"Connection": "close"})

    # ---------- 基础 ----------
    def health(self) -> dict:
        return self.client.get(f"{self.base}/health").json()

    def wait_health(self, seconds: int = 60) -> dict:
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            try:
                body = self.health()
                if body.get("status") == "ok":
                    return body
            except Exception as exc:  # 服务尚未起来
                last = exc
            time.sleep(0.5)
        raise RuntimeError(f"rag-tender 未就绪: {last}")

    # ---------- 模型配置 ----------
    def ensure_model_config(
        self,
        base_url: str,
        api_key: str,
        text_model: str,
        embed_base: str = "",
        embed_model: str = "stub-embed",
        embed_dims: int = 64,
    ) -> str:
        configs = self.client.get(f"{self.base}/model-configs").json()
        for config in configs:
            if config.get("base_url") == base_url and config.get("text_model") == text_model:
                if not config.get("embedding_model"):
                    continue
                # rag-tender 同一时刻只允许一个 active 配置：切到 stub 之前，
                # 上一次真实模型运行已经把它置为 inactive，直接复用会 409
                if not config.get("is_active"):
                    self.client.patch(
                        f"{self.base}/model-configs/{config['id']}", json={"is_active": True}
                    )
                return config["id"]
        payload = {
            "name": f"{text_model}",
            "provider": "openai-compatible",
            "base_url": base_url,
            "api_key": api_key,
            "text_model": text_model,
            "vision_model": text_model,
            "embedding_model": embed_model,
            "embedding_base_url": embed_base or base_url,
            "embedding_api_key": api_key,
            "embedding_dimensions": embed_dims,
            "is_active": True,
        }
        response = self.client.post(f"{self.base}/model-configs", json=payload)
        response.raise_for_status()
        return response.json()["id"]

    # ---------- 项目 / 文档 ----------
    def create_project(self, name: str) -> str:
        response = self.client.post(
            f"{self.base}/projects",
            json={"name": name, "auto_analyze": False, "timezone": "Asia/Shanghai"},
        )
        response.raise_for_status()
        return response.json()["id"]

    def upload(self, project_id: str, path: Path, role: str = "tender") -> dict:
        with path.open("rb") as handle:
            files = {"file": (path.name, handle, "application/octet-stream")}
            response = self.client.post(
                f"{self.base}/projects/{project_id}/documents",
                data={"role": role},
                files=files,
                timeout=300.0,
            )
        response.raise_for_status()
        return response.json()

    def wait_document(self, document_id: str, seconds: int = 600) -> dict:
        deadline = time.time() + seconds
        while time.time() < deadline:
            body = self.client.get(f"{self.base}/documents/{document_id}").json()
            status = body.get("parse_status")
            ai_status = body.get("ai_status")
            if status == "failed":
                raise RuntimeError(f"解析失败: {body.get('parse_error')}")
            # 解析完成 ≠ 可分析：必须等 AI 入库（ai_status）也落到 ready/needs_review
            if (
                status in {"completed", "needs_review"}
                and ai_status in {"ready", "needs_review"}
                and body.get("current_revision_id")
            ):
                return body
            time.sleep(1.0)
        raise RuntimeError("文档解析或 AI 入库超时")

    # ---------- 分析 ----------
    def create_run(self, project_id: str, model_config_id: str | None = None) -> dict:
        response = self.client.post(
            f"{self.base}/projects/{project_id}/analysis-runs",
            json={"model_config_id": model_config_id, "mode": "model"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"创建分析失败 {response.status_code}: {response.text[:300]}")
        return response.json()

    def wait_run(self, run_id: str, seconds: int = 1800) -> dict:
        deadline = time.time() + seconds
        while time.time() < deadline:
            body = self.client.get(f"{self.base}/analysis-runs/{run_id}").json()
            if body.get("status") in TERMINAL:
                return body
            time.sleep(1.0)
        raise RuntimeError("分析超时")

    def requirements(self, run_id: str) -> list[dict]:
        return self.client.get(f"{self.base}/analysis-runs/{run_id}/requirements").json()

    def decisions(self, run_id: str) -> list[dict]:
        return self.client.get(f"{self.base}/analysis-runs/{run_id}/decisions").json()

    def pages(self, document_id: str) -> list[dict]:
        return self.client.get(f"{self.base}/documents/{document_id}/pages").json()

    def page_detail(self, page_id: str) -> dict:
        return self.client.get(f"{self.base}/pages/{page_id}").json()


def dump_run(
    client: RTClient,
    file: Path,
    outdir: Path,
    project_name: str,
    model_base: str,
    model_key: str = "stub-key",
    model_name: str = "stub-extractor-v1",
    embed_base: str = "",
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    health = client.wait_health()
    config_id = client.ensure_model_config(
        model_base, model_key, model_name, embed_base=embed_base
    )

    project_id = client.create_project(project_name)
    document = client.upload(project_id, file, role="tender")
    document = client.wait_document(document["id"])

    run = client.create_run(project_id, config_id)
    run = client.wait_run(run["id"])
    return dump_artifacts(
        client, outdir, project_id, document, run, config_id, health, str(file)
    )


def dump_artifacts(
    client: RTClient,
    outdir: Path,
    project_id: str,
    document: dict,
    run: dict,
    config_id: str,
    health: dict,
    input_file: str,
) -> dict:
    """把已有 run 的原始数据落盘（不上传、不重新解析）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    requirements = client.requirements(run["id"])
    decisions = client.decisions(run["id"])
    pages = client.pages(document["id"])
    blocks = []
    for page in pages:
        detail = client.page_detail(page["id"])
        for block in detail.get("blocks", []):
            blocks.append(
                {
                    "block_id": block["id"],
                    "page_id": page["id"],
                    "page_index": page["page_index"],
                    "kind": block["kind"],
                    "reading_order": block["reading_order"],
                    "text": block["text"],
                }
            )

    (outdir / "requirements.raw.json").write_text(
        json.dumps(requirements, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "decisions.raw.json").write_text(
        json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "blocks.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta = {
        "health": health,
        "project_id": project_id,
        "document_id": document["id"],
        "document_filename": document["filename"],
        "page_count": document.get("page_count"),
        "run_id": run["id"],
        "run_status": run.get("status"),
        "run_stage": run.get("stage"),
        "coverage_notes": run.get("coverage_notes"),
        "n_requirements": len(requirements),
        "n_decisions": len(decisions),
        "n_blocks": len(blocks),
        "model_config_id": config_id,
        "input_file": input_file,
    }
    (outdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    health = sub.add_parser("health")
    health.add_argument("--base", default="http://127.0.0.1:8000")

    run = sub.add_parser("run")
    run.add_argument("--file", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--project", default="标书解析")
    run.add_argument("--base", default="http://127.0.0.1:8000")
    run.add_argument("--model-base", default="http://127.0.0.1:8099/v1")
    run.add_argument("--model", default="stub-extractor-v1")
    run.add_argument("--embed-base", default="")
    run.add_argument("--key-env", default="", help="存放 API Key 的环境变量名，避免密钥出现在命令行")

    # 已有文档、只补一次分析 run（提取结果已缓存时秒出）
    export = sub.add_parser("export")
    export.add_argument("--project-id", required=True)
    export.add_argument("--document-id", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--base", default="http://127.0.0.1:8000")
    export.add_argument("--model-base", default="https://api.deepseek.com/v1")
    export.add_argument("--model", default="deepseek-flash")
    export.add_argument("--embed-base", default="")
    export.add_argument("--key-env", default="")
    export.add_argument("--label", default="")

    args = parser.parse_args()
    client = RTClient(args.base)

    if args.cmd == "health":
        print(json.dumps(client.wait_health(), ensure_ascii=False, indent=2))
        return

    model_key = os.environ.get(args.key_env, "stub-key") if args.key_env else "stub-key"

    if args.cmd == "export":
        health = client.wait_health()
        config_id = client.ensure_model_config(
            args.model_base, model_key, args.model, embed_base=args.embed_base
        )
        document = client.client.get(
            f"{client.base}/documents/{args.document_id}"
        ).json()
        run = client.create_run(args.project_id, config_id)
        run = client.wait_run(run["id"])
        meta = dump_artifacts(
            client,
            Path(args.out),
            args.project_id,
            document,
            run,
            config_id,
            health,
            document.get("filename", ""),
        )
        if args.label:
            meta["label"] = args.label
            (Path(args.out) / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        if meta["run_status"] == "failed":
            sys.exit(1)
        return

    meta = dump_run(
        client,
        Path(args.file),
        Path(args.out),
        args.project,
        args.model_base,
        model_key=model_key,
        model_name=args.model,
        embed_base=args.embed_base,
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if meta["run_status"] == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
