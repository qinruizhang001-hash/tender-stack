# kb/approved

人工确认后的案例卡（`cases.jsonl`）与驳回审计（`rejected.jsonl`）落在这里。

- 只能由 `python -m scripts.kb_promote --approve --by <人>` 写入；
- 补偿层检索只读本目录，不写回解析主键；
- 目录为空时检索返回空列表，属正常状态。
