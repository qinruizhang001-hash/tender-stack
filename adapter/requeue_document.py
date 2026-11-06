"""把已完成提取的文档重新排一次 process_document。

用途：真实标书 84/84 批已全部提取并缓存，但此前队列任务因瞬时错误被标记 failed，
导致 documents.metadata_json.ai_status = 'failed'，进而 create_run 被守卫拦下
（"请等待项目招标文件和模板完成 AI 入库"）。

重排后 worker 会命中批次缓存，不做任何模型调用，只把 ai_status 落到 ready。
"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_engine
from app.models import Document
from app.services.pipeline import queue_document_processing

DOC_ID = "7cc2f7a8038147b9b893dee00ece4ec8"


async def main() -> None:
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        doc = (
            await session.execute(select(Document).where(Document.id == DOC_ID))
        ).scalar_one()
        meta = dict(doc.metadata_json or {})
        print("before:", meta.get("ai_status"), "batches", len(meta.get("requirement_batches") or []))
        await queue_document_processing(session, doc)
        await session.commit()
        meta = dict(doc.metadata_json or {})
        print("after:", meta.get("ai_status"))


asyncio.run(main())
