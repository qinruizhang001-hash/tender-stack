# v0.1.10 贡献词核对

来源：[issue #6](https://github.com/matongAI-lab/tender-review-kit/issues/6)、[#7](https://github.com/matongAI-lab/tender-review-kit/issues/7)、[#8](https://github.com/matongAI-lab/tender-review-kit/issues/8)。2026-09-30 读取正文；共 17 项、16 个不同短语，“视为放弃投标”重复一次。来源未附原文，因此以下是扫描分类，不是法律或废标结论。

| 短语 | 处理 | 分类 / scope 或依据 |
|---|---|---|
| 视为串通投标 | 新增 | primary / evaluation_phase；评审中串通认定信号 |
| 不予退还投标保证金 | 修正旧词末尾“的” | primary / bid_phase；保持原有类别 |
| 视为放弃投标 | 不重复 | 已有 primary“视为放弃”覆盖 |
| 取消投标资格的 | 不重复 | 已有 primary“取消投标资格”覆盖 |
| 投标保证金不予退还 | 不重复 | 已有 secondary“不予退还”覆盖；并非独立否决动作，不强升 primary |
| 视为在建工程 | 暂缓 | 可能是业绩/项目状态定义，须看上下文 |
| 视为投标无效 | 不重复 | 已有 primary“无效”覆盖 |
| 视为放弃解释权力 | 不重复 | 已有 primary“视为放弃”覆盖；命中后需辨别放弃解释权与放弃投标 |
| 视为逾期送达 | 新增 | secondary；送达程序状态，是否拒收须核原文 |
| 不予价格扣除优惠 | 新增 | secondary；评审价格优惠变化，不等于否决 |
| 视为自动放弃其投标 | 新增 | primary / bid_phase；明确放弃投标动作 |
| 视为已经包含在投标报价内 | 新增 | secondary；费用承担及报价包含提示，不等于否决 |
| 视为中标后自动放弃 | 暂缓 | 放弃对象缺失，须核是否放弃中标/某项权利 |
| 视为验收合格 | 新增 | contract；验收发生于合同履行期，不混入投标废标 |
| 按负偏离处理 | 新增 | secondary；可能扣分或影响响应，是否否决须看偏离规则 |
| 取消供应商中标人资格 | 待核原文 | 表达可能漏掉“或”等连接词，暂不固化为 primary |

保持现有词库结构及其他词的分类；不做大范围重构。自动候选仍保存在每个项目的 candidates/pending 文件中，不将未知原文导入开源资源。
