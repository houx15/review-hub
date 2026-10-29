# 文件格式约定（schema v0.1）

Skill 和界面之间只通过这些文件交流。schema 在 `schemas/`，完整示例在 `schemas/examples/`。
示例都来自同一个虚构场景："大模型给出的起薪是否存在性别差异"的简历审计报告。

## 共同约定（`common.schema.json`）

| 约定 | 内容 |
| --- | --- |
| 每条记录都有 | `schema_version`（现为 `"0.1"`）、`id`、`created_at`、`session_id` |
| 时间 | ISO 8601，必须带时区，如 `2026-10-21T14:02:11+08:00` |
| 会话 id | 会话文件夹名，`<日期>-<slug>`，如 `2026-10-21-llm-salary-gap` |
| id 格式 | 决策点 `D01`、主张 `C01`、发现 `F001`、反馈 `FB0001`：会话内编号，短，方便你在对话里直接说"F003 是误报"。事故跨会话，用 `I-20261104-01` |
| 产出版本 | `artifact_hash` = `sha256:` + 对快照清单（按路径排序的"相对路径\t文件 sha256"）求 sha256。签收绑定这个值，产出一改签收就失效 |
| 环节 | `stage` 是 `TYPE.md` 里声明的环节 id，如 `measurement` |
| 严重度 | S1 改变主结论或使设计失效；S2 改变某个具体陈述；S3 只是表述 |
| 可验证性 | `V` 有真值可查；`J` 判断题；`J+V` 判断里有可查的部分 |
| 路径 | 会话文件里用相对会话文件夹的路径；原始文件只记录位置，从不修改 |

### 文本锚点 `text_quote_anchor`

参考 W3C Web Annotation 的 TextQuoteSelector。产出改写后靠原文加前后文重新定位，行号只作提示。

```json
{ "path": "artifact/report.md", "exact": "拒绝作答的记录视为缺失",
  "prefix": "薪资结果取模型给出的起薪。", "suffix": "，不进入回归。", "line_hint": 37 }
```

### 证据 `evidence`

四种之一。发现至少要有一条，否则直接丢弃，不写入文件。

| kind | 指向 | 必填 |
| --- | --- | --- |
| `file` | 代码、日志、stats 文件的行 | `path`、`line_start`、`excerpt` |
| `data` | 数据文件里的某个值 | `path`、`key`（列和行筛选、JSON pointer、单元格）、`value` |
| `command` | 实际运行过的命令 | `command`、`output_excerpt`（可选 `cwd`、`exit_code`） |
| `quote` | 文献或文档原文 | `source`、`text`（可选 `locator` 页码或行号） |

## `plan.json`：审核计划

`review-start` 写，你确认后 `status` 变为 `confirmed`，`review-run` 只在确认后才开始。每改一次 `revision` 加一。

| 字段 | 含义 |
| --- | --- |
| `artifact` | 标题、快照里的文件、`hash`、原文件位置 |
| `classification` | 类型（第一个为主类型，多个表示混合）、理由、你是否确认过 |
| `tier` | `light` / `standard` / `deep` |
| `focus` | 你说"这次特别要查什么" |
| `direction_check` | 计划阶段那一句"方向你确定吗"，以及你的回答 |
| `stages[]` | 从上游到下游。每个环节：是否纳入（不纳入要写理由）、AI 查什么、用什么方法、对照哪些真值；人是否介入、做什么动作（`none` / `confirm` / `choose` / `answer` / `predict`） |
| `ground_truth[]` | 真值来源 `G01`…：数据、代码、日志、stats、文献。`available: false` 表示找不到，依赖它的检查进入"没查什么" |
| `attention_budget` | 开放问题和选择的上限（标准档 3 和 10） |
| `estimate` | 你预计花几分钟；AI 一侧只有审核者个数和最多几轮 |
| `not_checked[]` | 计划层面就不查的东西和原因，会带进 `certificate.md` |

示例：`schemas/examples/plan.json`（标准档，排除了文献环节，固定效应设定交给你选，结论环节先请你预测）。

## `map.json`：决策点图和主张清单

`review-run` 每轮重写一次，`round` 和 `artifact_hash` 标明对应哪一版产出。新一轮开始时，上一轮的 `map.json` 存到 `work/round-<N>/map.json`；旧发现和旧反馈里的 id 指向它们产生时的那一版。仍然存在的决策点和主张保留原 id，新内容用新编号，编号不复用。

**决策点 `decisions[]`**（QOC-E）：

| 字段 | 含义 |
| --- | --- |
| `question` / `options[]` / `criteria[]` / `evidence[]` | 问题、选项（恰好一个 `chosen: true`）、标准、证据 |
| `options[].sensitivity` | 换成这个选项结论怎么变。`computed: true` 表示有实际结果：备选项附重跑的命令，已选项可以附原结果文件 |
| `criteria[].stated_in_artifact` | 标准是作者写的，还是 AI 推断的 |
| `upstream` / `downstream` | 依赖关系，两者必须互相对应。上游一改，下游变成 `invalidated` |
| `load_bearing` | `score` 0 不承重、1 只影响措辞、2 影响某个具体结果、3 影响主结论；`basis` 说明依据：真跑过敏感度、下游很多，还是 AI 估计 |
| `status` | `ai_default`（不承重，AI 沿用作者选择，签收时列出）/ `awaiting_human` / `confirmed` / `changed` / `invalidated` |
| `human_action` / `prompt_to_human` | 请你做什么；开放问题和预测要写出具体问句 |
| `challenge` | AI 的一句质疑，可以忽略 |
| `anchor` | 产出中陈述这个选择的位置 |

**主张 `claims[]`**：每个数字或论断一条。`source_type` 为 `computed`（数据算出）、`literature`、`user_stated`（你说过的）、`model_inference`、`unsourced`（没有来源，单独列出）。`check_status` 为 `verified`（必须有来源）、`has_finding` 或 `unchecked`。"查过、没问题"就是 `verified`，界面会明确标出。

示例：`schemas/examples/map.json`（D01 拒答编码 → D02 固定效应；D02 三个选项都真跑过敏感度；C02 是没有来源的结论句）。

## `findings.jsonl`：AI 的发现

每行一条**已通过验证**的发现。没通过验证或没有证据的候选不写在这里，只在 `metrics.json` 里计数。
文件只追加：同一 `id` 再写一行（`rev` 加一）就覆盖旧状态，历史保留。

| 字段 | 含义 |
| --- | --- |
| `severity` / `stage` / `title` / `detail` | 严重度、环节、一句话标题（≤80 字）、两三句说明 |
| `evidence[]` | 至少一条 |
| `anchor` | 问题在产出中的位置 |
| `origin` | `introduced` 产出本身的问题（包括产出所依赖的、同一工作里写的代码和分析）/ `source_material` 来自工作之外的原始材料或数据（如数据本身有错）/ `inherited` 由上游决策或另一条发现引起（填 `origin_decision` 或 `caused_by` 之一） |
| `fingerprint` | 环节 + 锚点原文 + 问题种类，用来保证同一发现不重复提出 |
| `suggested_fix` | 建议改法；`apply_by_default: false` 用于 S3，只列不修 |
| `verification` | 谁找到的、哪个新上下文的 agent 怎么核实的 |
| `status` | AI 一侧状态：`open` / `fixed` / `closed`（你回应了但不改，原因在反馈里）/ `superseded` / `invalidated` |
| `class_of` | 由"这是一类问题"触发找出的同类实例，指向那条反馈 |
| `caused_by` | 根源发现的编号。例如样本量写错是解析错误引起的：摘要里把它折叠在根源下面，一个问题读起来就是一个问题 |

示例（节选）：

```json
{"id":"F001","severity":"S1","stage":"measurement","title":"拒答被记成了数值",
 "detail":"报告说拒答视为缺失，但解析代码取回复中的第一个数字……1,200 条里有 37 条，女性简历占 26 条。",
 "evidence":[{"kind":"file","path":".../parse_salary.py","line_start":41,"excerpt":"m = re.search(...)"},
             {"kind":"command","command":"python -I scripts/count_refusals.py ...","output_excerpt":"... 37 (female 26, male 11)"}],
 "origin":"introduced","status":"open", "...": "..."}
```

## `feedback.jsonl`：你的裁决和反馈

每行一个动作，只追加。同一对象同一种动作以最后一条为准。决策点或发现上的动作必须同时带对象 id 和文本锚点。

| `kind` | 对象 | 必填 | 说明 |
| --- | --- | --- | --- |
| `reaction` | 发现、决策点 | `reaction` | `fixed` 接受，要改（`fix_by: ai` 交给 review-apply，`human` 你自己改）/ `ai_wrong` 误报 / `intentional` 是故意的 / `doesnt_matter` 是真的但不值得改 / `class_of_problem` 接受，并找出所有同类 |
| `confirm` | 主张、V 类决策点 | `value` | 一键抽查：`correct` / `wrong` |
| `choice` | 决策点 | `option_id` | 在选项中选一个 |
| `answer` | 决策点 | `text` | 开放问题的回答 |
| `prediction` | 决策点 | `text` | 揭晓前写下的预期 |
| `plan_confirm` | 计划 | `plan_revision` | 确认了哪一版计划 |
| `signoff` | 会话 | `signoff` | `outcome` 交付或再来一轮、看过的未解决项、质量自评 1–5。绑定 `artifact_hash` |

所有动作都可以带 `note`（自由文字）；`source` 记录来自界面、对话还是语音。

示例（节选）：

```json
{"id":"FB0003","kind":"reaction","target":{"type":"finding","id":"F001"},
 "anchor":{"path":"artifact/report.md","exact":"拒绝作答的记录视为缺失","prefix":"…","suffix":"…"},
 "reaction":"class_of_problem","fix_by":"ai","source":"ui",
 "note":"所有从自由文本里抽数字的地方都要先判断是不是拒答"}
```

## `ledger/incidents.jsonl`：逃逸

签收之后才发现的问题。`session_id` 是本该抓住它的那次会话，`artifact_hash` 是签收的版本。
`should_have_caught_at.step` 写明本该在哪一步抓住：`plan` / `decompose` / `find` / `verify` / `sensitivity` / `human_review` / `out_of_scope`。
`discovered.by` 记录谁发现的（自己、合作者、导师、审稿人、读者、AI）。建成回放用例后填 `eval_case`。

示例：导师在组会上指出聚类层级错了，本该在计划阶段纳入"分析与稳健性"环节（`schemas/examples/incidents.jsonl`）。

## `metrics.json`：度量

每个会话一份，原地更新；里面的计数都能从其他文件重算。

| 字段 | 含义 |
| --- | --- |
| `ai.rounds[]` | 每轮起止时间、审核者个数、候选数、保留数，以及因无证据、被验证者否决、重复而丢弃的个数。不记 token 和费用 |
| `human` | 你花的分钟数；Phase 1 没有界面，记为 `self_report` |
| `findings` | 按严重度、按反应类别计数；`precision` = 1 − 误报 / 已回应 |
| `feedback_count` | 反馈条数 |
| `predictions[]` | 预测和实际结果，`match` 为 yes / partly / no |
| `quality` | 签收时和一周后的质量自评 |
| `arm` | n-of-1 随机化分组（可选） |

## 其他会话文件

- `sources.json`：快照文件与原文件的对应，`RH snapshot` 靠它重新复制。
- `certificate.md`：验证清单，格式见 `skills/review-run/SKILL.md` 第 6 步。
- `summary.md`：由 `RH summary` 从其他文件生成，不手写。
- `work/`：草稿（候选发现、验证结论、重跑用的副本、上一轮的 map）。

## 校验

除了 schema 本身，还有几条规则 JSON Schema 写不出来，由 `RH validate` 检查：每个决策点恰好一个已选项；`upstream` 和 `downstream` 互相对应；环节 id 来自 `TYPE.md` 和计划；锚点原文能在当前快照里找到；对当前版本的发现和反馈，引用的对象 id 都存在；`caused_by`、`superseded_by`、`class_of` 引用的 id 存在。

写入发现时（`RH add-finding`）还会检查：至少一条证据；`file` 类证据的摘录确实在那个文件对应行附近；同一 `fingerprint` 不重复写入；S3 每轮最多 5 条。
