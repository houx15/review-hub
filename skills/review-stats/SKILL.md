---
name: review-stats
description: （Phase 3）汇总自我实验的度量：人工用时、AI 轮次和审核者数、审核精确率、逃逸率、预测准确度、反馈量趋势。用户说"看看效果"、"review stats"时使用。
---

# review-stats：度量报表

> 文字版约定，Phase 3 实现。

## 输入
- 工作区所有会话的 `metrics.json`、`feedback.jsonl`、`findings.jsonl`；`ledger/incidents.jsonl`。

## 输出
- 一份中文 markdown 报表（默认只在对话里展示，用户要求时写到工作区 `reports/`）。

## 内容
| 度量 | 来源 |
| --- | --- |
| 人工用时（按类型、按档位，随时间） | `metrics.human` |
| AI 工作量：轮次、审核者数、用时 | `metrics.ai.rounds` |
| 审核精确率 = 1 − 误报 / 已回应 | 反应类别 |
| 逃逸率，按严重度和"本该在哪一步抓住" | `incidents.jsonl` |
| 预测准确度及趋势 | `metrics.predictions` |
| 注意力错配：时间花在哪些环节 vs 问题出在哪些环节 | `by_stage_minutes` 对比发现分布 |
| 每次反馈条数的趋势 | `feedback_count` |
| 质量评分：签收时 vs 一周后 | `metrics.quality` |

## 规则
- 不计算 token 或费用。
- 样本少于 5 个会话时只列数字，不画趋势、不下结论。
- n-of-1 分组（`arm`）各有 ≥10 份时才比较两组。
