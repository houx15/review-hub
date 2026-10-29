---
name: review-run
description: 审核计划确认后执行 AI 自审自改循环：拆解决策点和关键主张、按审核方面分头以专家眼光评审、新上下文独立验证、给每个方面写评价；然后在副本上自己修改、再审，最多 3 轮或没有 S1/S2 为止；最后把剩下的问题和全部改动交给用户。产出 map.json、findings.jsonl、changes.jsonl、certificate.md 和 summary.md。用户说"开始自审"、"run the review"、"再审一轮"时使用。
---

# review-run：AI 自审流水线

审核的对象是产出本身。AI 的工作是像这个领域的资深审稿人一样**动脑判断**，不是把作者做过的计算再做一遍。

## 怎么调用 RH
`RH` = `uv run <engine_root>/scripts/rh.py [--workspace <工作区>] <命令> ...`
- `engine_root`：本 skill 目录解析符号链接后（`realpath`）往上两级，即 review-hub 仓库。
- `--workspace` 是全局参数，必须写在命令名**之前**；没有设置 `$REVIEW_WORKSPACE`、工作区又不在 `~/review-workspace` 时必须给。

## 输入
- 一个会话文件夹，其中 `plan.json` 的 `status` 为 `confirmed`。
- `<工作区>/types/<类型>/TYPE.md`、`rules.md`；`<工作区>/personal/blind-spots.md`。

## 输出（都在会话文件夹里）
| 文件 | schema / 格式 |
| --- | --- |
| `map.json` | `schemas/map.schema.json`：决策点、关键主张、线索（`leads`）、每个审核方面的评价（`assessments`） |
| `findings.jsonl` | `schemas/finding.schema.json`，只追加，只放验证通过的 |
| `certificate.md` | 验证清单（格式见第 6 步） |
| `metrics.json` | `schemas/metrics.schema.json`，由 RH 维护 |
| `summary.md` | 由 `RH summary` 生成，不手写 |
| `work/round-N/` | 草稿：候选、评价草稿、验证结论 |
| `artifact/` | 产出的副本；自改时直接改这里。`baseline/` 是会话开始时的原版，不要动 |
| `changes.jsonl` | `schemas/change.schema.json`，AI 自改的每一处，由 `RH add-change` 写 |

## 整体流程
```
第 1 轮自审（第 0–7 步）
  └→ RH status 输出 loop=revise：自改（第 8 步）→ 下一轮自审（"再审"一节）→ 再看 loop
     loop=handoff：交给用户（第 9 步）
```
自审最多 3 轮。每轮结束看 `RH status` 最后一行：`loop=revise` 表示还有未处理的 S1/S2、轮数没满，继续自改；`loop=handoff` 表示交给用户。用户不用在中间回应，人的注意力留给 AI 自己解决不了的问题。

## 总原则
- **写给作者看的每一句话按 `skills/WRITING.md` 写。** 先写事实，再写判断；用学术写作的正常语言；不出现工具内部的说法（"承重""环节""发现 F012"），也不写谁该做什么。所有子 agent 的提示里都要求它们先读这份文件。它们交来的文字，在交给作者之前由你逐条修改（第 9 步）。
- **两层：概览与批注。** 概览写全文的架构（研究叙事、测量、分析、结果与解读）和决定论文能否成立的几个关键问题；正文批注只处理具体段落的细节。架构层面的问题写进概览，不拆成散落在正文里的批注。
- **能改的由 AI 改，只把真正需要作者的交给作者。** 数字与结果不符、摘要或标题与结果不一致、说法强于证据，这些由 AI 直接修改。交给作者的只有两类：需要作者判断的研究决策，以及需要对照数据、代码或原文才能确认的事实。技术细节（例如生成程序的写法）只以它对结论的影响出现。
- **做专家的判断，不做计算。** 按计划里的审核方面，用每个方面的 `expert_questions`（这个领域的资深审稿人会问的问题）和 `red_flags`（常见的毛病）来审：
  - 问题对不对、值不值得；
  - 方法能不能回答这个问题；
  - 分析上的选择是否合理；
  - 换一种合理的做法，结论大概会不会变（靠推理，说出理由）；
  - 解读有没有超出证据；
  - 缺了什么。
- **深入、侦探式、连贯。** 不满足于"说法和结果一致"，要问"结论真的被确立了吗"（Freedman 1991，"Statistical Models and Shoe Leather"）：
  - 设计本身说明了问题，还是靠模型撑着？
  - 主结论的其他推论检验了吗？
  - 每个反常的结果怎么解释？
  - 对立解释被证据排除了吗？
  - 各部分连成一个整体吗？
- **线索交给人追问，不由 AI 修。** 反常的结果、没检验的推论、没排除的对立解释、模型代替设计的地方、前后接不上的地方，写成**线索**（`map.json` 的 `leads`）。界面会把相关段落高亮，把问题写在上面，请用户思考、回答或讨论。只有确定是错的（说法和结果矛盾、数字错）才写成发现、由 AI 修。
- **不运行，不重算。** 不运行项目的代码，不读入原始数据或日志，不重新计算结果。
  - 可以**读**项目里的文件来理解作者做了什么：设计文档、代码、已保存的结果文件。
  - 一个问题要重算才能回答时，写成给用户的问题（发现或决策点），不自己算。
- **数字只抽查关键的。** 只核对主结论依赖的几个数字（摘要、结论、标题里的），对照已保存的结果文件。其余数字不逐个核对，在验证清单里写明。
- **没有依据的发现不发。** 判断类的发现，依据是产出原文（`quote`）加上讲得通的专家理由：为什么这是问题，影响多大，怎么改。事实类的发现，依据指向具体位置：文件和行、结果文件里的值、文献原文。
- **精确率优先。** 拿不准就不报；噪声消耗的是用户的注意力。也要说优点：做得好的地方写进评价，不只挑毛病。
- **原项目只读。**
- **证据路径用绝对路径**（会话内的文件可用相对会话的路径，如 `artifact/report.md`）。

## 步骤

### 0. 开始一轮
`RH round-start <会话>`：计划未确认会拒绝并提示先用 `review-start`；记录开始时间，建好 `work/round-N/`。

### 1. 拆解 → `map.json`
读产出，写决策点图和主张清单。

**决策点**：作者做出的、真正有备选的关键选择，QOC-E 四要素齐全。备选项要具体可行，不凑数。作者没写的标准，AI 推断的标 `stated_in_artifact: false`。
- `upstream` / `downstream` 按依赖填，沿 `TYPE.md` 的环节顺序。
- **承重分**按依赖和判断给（`basis: dependency` 或 `judgement`）：换一个选项，主结论会变的给 3，某个具体结果会变的给 2。
- **状态和人的动作**：

  | 决策点 | `status` | `human_action` |
  | --- | --- | --- |
  | 承重分 ≤1 | `ai_default` | `none` |
  | 承重分 ≥2，V 类（AI 能对照真值核实），核实无误 | `ai_default` | `none` |
  | 承重分 ≥2，V 类，核实出问题（如代码没按声明的做） | `awaiting_human` | `confirm` |
  | 承重分 ≥2，J 或 J+V，有可列举的选项 | `awaiting_human` | `choose` |
  | 承重分 ≥2，答案只在用户那里 | `awaiting_human` | `answer`，写 `prompt_to_human` |

  **评判别人的作品时**（`plan.user_role` 为 `evaluator`）：作品中可选的做法不是用户的决定，而是作者的决定。这类决策点不设 `choose`，写成值得追问的线索（向作者提的问题），或保持 `ai_default`。
  **方案类作品**（类型的 `TYPE.md` 写着 `work: plan`，如课程设计、产品想法）：决策点只记方案中**尚未定下、又会影响其余部分**的选择（例如"工作坊的重心是会用还是会检验"）。同一个选择不要再写成发现或线索；发现只写有明确改法的问题。
  不设预测题，也不设任何必须先回答才能往下看的问题：页面只提供洞见、高亮和问题，用户愿意回应才回应。

**前提不是决策点**：没有备选、只能成立或不成立的前提（如"每对简历除姓名外完全相同"），写成主张。材料不在项目里、无法核实时标 `unsourced`，并写进 `certificate.md` 的"没查什么"；用户可以在摘要里对它做一键确认（`confirm`）。

**主张**：只拆**关键的**：主结论依赖的数字（摘要、结论、标题里的），以及承重的事实性或结论性论断。一份报告通常 15–30 条，不要把每个数字都拆成主张。`source_type` 按这句话**应当**来自哪里来标，与是否已经核实无关：
- 由数据或代码算出的数字 → `computed`；
- 引用文献的 → `literature`；
- 用户说过的 → `user_stated`；
- 作者的推理、解释 → `model_inference`；
- 应当有来源却找不到的 → `unsourced`，`sources` 为空。

全部先标 `check_status: unchecked`。

- `summary` 写产出原文的一句话主结论。
- 锚点用 `RH anchor <会话> "<逐字原文>"` 生成（自动补 `prefix`/`suffix`/`line_hint`）；原文出现多次时加长到唯一为止。
- `RH validate <会话>`。

### 2. 分头评审（并行）
按**审核方面**分组：每个查找者负责一到两个方面，相当于请一位在这些方面最在行的审稿人。组数等于 `plan.estimate.reviewer_agents − 1`（留一个给验证者；深度档再留一个给证伪者；至少 1 组）；方面比组多时，把相邻的方面合在一组。组名用方面的 id（如 `method_fit`）。每组启动一个查找者子 agent（Agent 工具，并行）；不支持子 agent 时按组依次执行。查找者对 `map.json` 和原项目都只读。

给每个查找者：
- 负责的方面在 `plan.aspects` 里的全部内容：`ask`、`covers`（看产出的哪些部分）、`criteria`、`expert_questions`、`red_flags`；
- 这些方面下各环节在 `TYPE.md` 中的段落，`rules.md` 中对应的规则，`blind-spots.md`；
- 产出快照路径、`map.json`、`plan.focus`；真值（参考材料）的绝对路径，并说明只读、不运行；
- 它的草稿目录 `work/round-N/finder-<组名>/` 和两个输出文件路径；
- 生成锚点的完整命令（`RH anchor <会话绝对路径> "<原文>"`，把 `RH` 展开成实际命令）；
- 以及下面这段要求（原文照抄进提示，把 `<engine_root>` 换成实际路径）：

> 先读 `<engine_root>/skills/WRITING.md`，你写的标题、说明和问题都按它写：先写事实，再写判断，用学术写作的正常语言。
> 你是这些审核方面的资深审稿人。读整份产出，用这些方面的专家问题和常见毛病来审：作者的问题、方法、选择和结论站不站得住，换一种合理做法结论大概会不会变，读者会不会被误导，缺了什么。**做判断，不做计算**：不运行项目代码，不读入原始数据或日志，不重新计算结果；可以读项目里的设计文档、代码和已保存的结果文件来理解作者做了什么。要重算才能回答的疑问，写成发现，在 `suggested_fix.text` 里说明需要作者补什么。
> 说明里用节名或小标题定位原文（例如"§2.3""'Backwards Compatibility' 一节"），不要用行号：修改之后行号会变。
> 文件类证据的摘录必须逐字出自原文；只能省略整行（单独一行写"..."），不能在一行之内省略。
> 只报告你能讲清依据的问题。判断类问题的依据：产出原文（`quote`）加上 `detail` 里讲得通的专家理由（为什么是问题、影响多大）。事实类问题的依据：指向具体位置的文件、结果文件里的值或文献原文。命令类证据只能是读文件的简单命令（如 grep、读 json 的某个键）的真实输出，附 `cwd`，不能编造。`stage` 填问题根源所属的环节；发现别的方面的问题也可以报，但 stage 要填对。
> 每条写成一行 JSON，写进候选文件：`stage`, `severity`(S1/S2/S3), `title`(≤80字), `detail`(2–3 句，中文，白话), `evidence`(数组，每条必须带 `kind`，是以下四种之一：`{"kind":"file","path":绝对路径,"line_start":n,"line_end":n,"excerpt":逐字摘录}` / `{"kind":"data","path","key","value","note"}` / `{"kind":"command","command","cwd","exit_code","output_excerpt"}` / `{"kind":"quote","source","locator","text"}`；完整定义在 `<engine_root>/schemas/common.schema.json`), `anchor`(`{"path":"artifact/<文件>","exact":逐字原文,"prefix","suffix"}`，用给你的锚点命令生成), `origin`(introduced=产出本身的错 / source_material=来自数据或原始材料 / inherited=继承自上游决策或另一个问题), `origin_decision`(继承自上游决策时填决策点 id), `decision_ids`, `claim_ids`, `problem_kind`(英文短标签，如 conclusion_exceeds_design), `suggested_fix`({"text", "replacement"可选})。S1 = 改变主结论或使设计失效；S2 = 改变某个具体陈述；S3 = 只是表述，最多 5 条。判断类问题按影响定级，不因为是判断就降成 S3。没有问题就写空文件。
> 另外给你负责的每个方面写一条评价，写进评价文件，每行 `{"aspect", "rating", "verdict", "reasons", "questions"}`：`rating` 是 strong（专家会接受）/ adequate（站得住，有能改的问题）/ weak（这里的问题动摇了整份工作）；**方案类作品**（`work: plan`）改用 clear（想清楚了）/ partial（有方向，未想透）/ open（尚未涉及）——方案本来就是没有做完的东西，评价的是想得是否清楚，不是成品是否合格；`verdict` 一句白话；`reasons` 2–5 条，优点和问题都写，每条落到产出的具体内容；`questions` 是这个方面留给作者想的重要问题（没有就空），不预设答案。
> **像侦探一样追线索**（线索的种类以 `TYPE.md` 的 `lead_kinds` 为准；没有写的按下面研究报告的五种）。**方案类作品**改为找：方案依赖、却还没有检验的假设（untested_assumption）；方案必须作出、却还没有作出的选择（open_choice）；方案需要、却还没有想到的部分（missing_piece）；前后不一致之处（incoherence）。**研究类作品**：在结果里找和主结论对不上的地方，列出主结论的其他推论、问它们检验了没有，写出最有力的对立解释、问什么证据能把它和主结论分开，问去掉模型、最朴素的比较是什么样，看各部分是否连成一体。这些不是错误，写成**线索**，每行一个写进线索文件：`{"aspect", "kind"(anomaly 反常的结果 / untested_implication 没检验的推论 / rival_explanation 没排除的另一种解释 / model_for_design 模型代替了设计 / incoherence 前后接不上), "title"(≤60字), "observation"(看到了什么，白话，带具体数), "threat"(如果它就是看上去那样，主结论会怎样), "questions"(1–3 个问用户的问题：去看什么、什么证据能定下来；不预设答案), "evidence"(格式同上), "anchor"(用锚点命令生成)}`。线索要具体、能追下去，不要泛泛的"还需要更多研究"。
> 你核对过、没有问题的关键主张写进 `<草稿目录>/checked.jsonl`，每行 `{"claim_id", "evidence": [...]}`。
> **边查边写**：每确认一条就马上追加到文件，不要攒到最后。连接可能中断，已经写下的不会丢。

查找者写三个文件：候选 `work/round-N/candidates-<组名>.jsonl`，线索 `work/round-N/leads-<组名>.jsonl`，评价草稿 `work/round-N/assess-<组名>.jsonl`。子 agent 中途失败时，用 SendMessage 让它从断点继续，不重新启动。全部完成后，由你把候选合并成 `work/round-N/candidates-all.jsonl`，每条加 `cid`（`<组名>-<序号>`）；完全没有依据的不进合并文件，计入 `dropped_no_evidence`。各查找者之间有重复是正常的。

然后运行 `RH check-candidates <会话> work/round-N/candidates-all.jsonl`，把它报出的问题（证据格式不对、摘录找不到、锚点找不到）一并交给验证者修正或丢弃。

### 3. 验证（新上下文）
启动一个验证者子 agent（新上下文），它扮演一位持怀疑态度的资深同行。给它：`candidates-all.jsonl`、全部评价草稿、各查找者的 `checked.jsonl`、产出快照、真值位置、`plan.aspects`、`TYPE.md`。**不给**查找者的回复和推理。验证者同样不运行、不重算。

对每条候选：
- 亲自打开依据指向的位置，重读产出的相关段落；
- 判断批评是否成立、是否公允：作者是否其实已经处理过（在别的段落、局限里、附录里）？专家会认同这是问题吗？
- 校正严重度和环节；修正不准的证据（行号、摘录）；
- 去重：说的是同一个问题的，只保留依据最完整的一条，其余标 `duplicate`；
- 根源：由另一条候选直接引起的，填 `caused_by`（根源的 cid），`origin` 为 `inherited`。这时不必填 `origin_decision`。

每条输出一行到 `work/round-N/verdicts.jsonl`：`cid`, `verdict`(keep/drop/duplicate), `duplicate_of`, `caused_by`, `severity`, `stage`, `origin`, `origin_decision`（只在 origin 为 inherited 且源自决策点时填）, `method`（一句话：怎么核实的）, `reason`；需要改写时，再给出要替换的字段：`evidence`、`title`、`detail`、`anchor`、`suggested_fix`、`claim_ids`、`decision_ids`。

**线索**：逐条看全部 `leads-*.jsonl`：观察是否属实（打开依据核对），"为什么要紧"的推理是否站得住，问题是否值得用户花时间、是否没有预设答案；和某条发现说的是同一件事的，留一处（确定是错的留作发现，否则留作线索）；几条线索指向同一个更深的问题时合并成一条。输出到 `work/round-N/leads-kept.jsonl`，格式同线索，按对主结论的威胁从大到小排，最多 8 条。

**评价**：逐个方面校准评价草稿：评级要和保留下来的发现一致（有 S1 的方面不能是 strong），理由要落到产出，问题要真的重要。输出到 `work/round-N/assessments.jsonl`，每个方面一行，格式同草稿，另加 `cids`（这个方面相关的保留候选）。

对 `checked.jsonl` 里的主张抽查：打开依据，确认无误的输出到 `work/round-N/verified-claims.jsonl`（`{"claim_id", "evidence"}`）。只有验证者确认过的主张才能标 `verified`。

深度档另启动一个证伪者，专门尝试推翻 `map.summary` 和每个承重分为 3 的决策点；推翻成功的作为候选走同样的验证。

### 4. 换个做法会怎样（标准档和深度档，由你自己做）
对承重分 ≥2 的决策点，像专家一样推理：换成每个备选项，结论大概会怎样变，为什么。写进对应选项的 `sensitivity`：`effect` 写推理的结论和理由（例如"六组稳健性里五组方向一致，换成 O8 L0 结论大概不变，但效应会小一些"），`computed: false`，`evidence` 指向推理依据（产出原文、已保存的结果、设计文档）。不运行、不重算；推不出来的就不写。推理之后承重分变了，按第 1 步的表同步更新 `status` 和 `human_action`。
轻量档跳过此步。

### 5. 写入
把验证结论合并进候选（验证者给出的字段覆盖候选的；去掉 `cid` 和值为 null 的字段）。**先写根源，再写连带的发现**；边写边记下 `cid → F 编号`，连带发现的 `--caused-by` 填根源的 F 编号：

`RH add-finding <会话> --finder finder:<组名> --verifier verifier:<序号> --method "<验证方法>" [--caused-by <根源的 F 编号>]`（候选 JSON 从标准输入传入）

脚本会：
- 核对 `anchor.exact` 在快照里，`file` 类证据的摘录在文件对应行附近（`...` 表示省略的行），不符就拒绝写入（退出码 3）；
- 用 `stage | anchor.exact | problem_kind` 算 `fingerprint`，与已有发现重复的拒绝写入（退出码 5，计入 `dropped_duplicate`）；
- S3 每轮超过 5 条拒绝写入（退出码 6）；
- 分配 `F` 编号，校验后追加。

退出码 3 表示校验没过（证据、锚点或字段格式），按错误信息修正再写；修不了就放弃这条，计入 `dropped_by_verifier`。

把 `leads-kept.jsonl` 写进 `map.json` 的 `leads`，依次编号 `L01`、`L02`……（再审时保留已有的线索和编号，新线索接着编）；锚点用 `RH anchor` 确认唯一。

然后把 `assessments.jsonl` 写进 `map.json` 的 `assessments`：每个方面一条，`cids` 换成写入后的 F 编号放进 `finding_ids`，去掉 `cids`。

再更新 `map.json` 里主张的 `check_status`：有发现的标 `has_finding` 并填 `finding_ids`（优先于 verified）；在 `verified-claims.jsonl` 里的标 `verified` 并补 `sources`；其余保持 `unchecked`。

**注意力预算**：`awaiting_human` 中开放问题（`answer`）和选择（`choose`）超过 `plan.attention_budget` 时，按承重分保留，其余改为 `ai_default`。

### 6. 验证清单 → `certificate.md`
```markdown
# 验证清单 · 第 N 轮

## 看了什么
| 方面 | 看了产出的哪些部分 | 对照了哪些材料 | 结论 |
（按方面一行；材料写文件位置；结论写评价和发现编号）

## 核对了哪些数字
（关键主张：编号、核对的结果文件位置、结果）

## 没查什么
| 内容 | 原因 |
（plan.not_checked 全部带上；"没有重算任何结果，只核对了关键数字"固定写上；本轮因材料不在项目里等跳过的也写上）

## AI 默认
（status 为 ai_default 的决策点：编号、标题、沿用的选项）
```

### 7. 收尾
`RH round-end <会话> --agents <完成工作的子 agent 个数：查找者 + 验证者 + 证伪者；中途失败又续上的只算一次> --candidates <所有查找者候选总数> --dropped-no-evidence <n> --dropped-by-verifier <n> --dropped-duplicate <验证者判重 + 脚本判重>`
`RH validate <会话>`，必须通过。
然后 `RH status <会话>`，看最后一行 `loop=`：`revise` 进入第 8 步；`handoff` 进入第 9 步。

### 8. 自改（loop=revise 时）
**先看计划里的两项设置。** `user_role` 为 `evaluator`（评判别人的作品）时，不修改原文，跳过这一步：`RH status` 会直接给出 `loop=handoff`，问题都写成给作者的意见。`revision_policy` 为 `none`（创意写作）时同样不修改原文。`revision_policy` 为 `narrow`（作者本人口吻的文本）时，只做删减、收窄说法、按材料更正事实，**不替作者新增任何论断或句子**；需要补充内容的，写成建议交给作者。

`revision_policy` 为 `full` 时，这是 AI 自己产出的第一版，自审之后**什么都可以改**：措辞、结构、定位、框架、论断的强弱都可以，只要能说清楚为什么。只在副本 `artifact/` 上改，原文件不动。**不产生新数字**：改数字只能换成已保存的结果文件里已有的数。
1. `RH revise-begin <会话>`：保存改前版本，列出待处理的发现。
2. 按发现逐条改 `artifact/` 下的文件（用 Edit 工具）：
   - 有 `suggested_fix.replacement` 的以它为准；没有的按 `suggested_fix.text` 和你自己的判断改。
   - 要重算、补实验或改代码才能修的（例如解析代码有 bug、需要补一个稳健性检验）：不自己做。先把文字改到证据能支持的程度（收窄结论、加上局限），再用 `RH handoff` 交给用户，写明需要作者补什么。
   - 靠判断的问题（定位、框架、读者解读、重点）：AI 也直接改，改法要能在 `why` 里讲清楚。用户回来时可以撤销。
   - **消除前后矛盾时，只向原文已有依据的那一种说法收拢**：两处说法不一致，保留原文其他部分支持的那一种，删去或改正另一种；原文没有依据判断哪一种对时，不替作者选择，写成待核实或建议。不新增原文没有的论断或规定（例如接口的行为、结论的范围）。
   - **加进去的每个指向都要先核对**：图号、小节号、引文、出处，改之前打开对应的材料（作图代码、图的说明、原文）确认它说的就是这件事。不能凭文件名或印象猜。
   - **一处改了，同一件事的其他说法一起改**：改之前搜一遍全文（标题、摘要、引言、讨论、结论、图表说明），同一个论断出现几次就改几次，免得前后说法打架。
   - 改了上游，要顺着改下游（数字变了，解释和结论跟着变）。
   - 只回答"只有用户知道"的问题（`human_action: answer`）不能替用户做，留着。
   - 线索不修：它们是给用户追问的问题，AI 不替用户回答，也不为了"消掉"线索去改正文。
   - 自己改不了的发现（结论取决于需要对照数据或代码才能确认的事实，或者取决于只有作者能做的研究决策）：`RH handoff <会话> F002 --note verify|suggest --reason "<事实与判断>"`。`--reason` 按 `skills/WRITING.md` 写：这件事是什么、影响哪个结论、影响有多大；不写"AI 不能做""需要你做"。需要对照数据或代码确认的用 `--note verify`，需要作者判断的用 `--note suggest`。自改循环不再算它。不要用它回避能改的问题：摘要、标题、结论与结果不一致，一律由 AI 修改。
3. **每一处改动**马上记一条：`RH add-change <会话>`，JSON 从标准输入传入：`summary`（一行中文：改了什么）、`why`（为什么，白话）、`path`（`artifact/<文件>`）、`before`（改前的原文，逐字）、`after`（改后的原文，逐字，在新版本里必须唯一；纯删除时为空并给 `anchor_exact`：删除位置前的一段原文）、`finding_ids`、可选 `decision_ids`、`evidence`（换上的数字在结果文件里的位置，格式同发现的证据）。
4. `RH revise-end <会话>`：脚本检查每一处改动都有记录，没记的会列出来（退出码 3），补记后再运行。它还会列出锚点在新版本里找不到的未处理发现，下一轮处理。
5. 开始下一轮（见"再审"）。

### 9. 交给用户（loop=handoff 时）
**先写概览，再逐条编辑。** 这一步由你亲自做，不交给子 agent。
1. **概览**（`map.json` 的 `overview`，格式见 `schemas/map.schema.json`）：读完修改后的正文、全部发现、线索与评价，按类型 `TYPE.md` 中 `overview` 列出的几个部分（例如学术报告是研究叙事、测量、分析、结果与解读；产品想法是核心点、第一性原理、经得住推敲吗、核心挑战、现有的类似产品、最短路径）各写一段，每段三五句，概括作品在这一方面的架构和它站得住的程度，`refs` 列出相关批注。
   再写 `key_questions`：决定论文能否成立的 3–5 个问题，每个写标题和一段说明，`refs` 列出相关的批注编号。架构层面的发现写进这里；它们原来的正文批注可以保留作细节，但说明里不必重复架构层面的论证。
   **方案类作品**（`work: plan`）再写 `path`（建议的路径）：作者接下来该怎么做——先做什么、可以砍掉什么、用什么最便宜的办法先检验最没把握的那一点。`text` 一两句写排序的道理，`steps` 按先后列 3–6 步，每步写做什么、为什么，`refs` 指向相关批注。这类建议只写在这里；批注里的 `suggested_fix` 只写那一处怎么改，不再重复整条路径。
   **每件事只出现一次。** 写完概览后通读一遍全部内容：同一件事若同时出现在关键问题、批注、线索、决策点、路径或任务里，选它最合适的位置完整写一次，其他位置只用 `refs` 指过去，或者删掉。批注并入另一条批注或一个选择时，用 `RH set-finding-status <会话> F001 superseded --superseded-by D01`（不要用 closed：closed 表示用户已经回应）；删去或合并了决策点，同时用 `RH edit-finding` 改掉仍指向它的 `decision_ids`。关键问题说明"为什么这件事决定成败"，不复述批注的细节；任务和路径的步骤写"做什么"，不复述问题本身。验证者合并查找者的重复时，也按这个原则合并成一条。
2. **逐条编辑**：按 `skills/WRITING.md` 改写所有交给作者看的文字——未处理发现的标题与说明（`RH edit-finding`，JSON 只含要改的字段），线索、研究决策的问题与选项、各方面的评价（直接改 `map.json`）。删掉工具内部的说法；把直译的术语换成通行的说法；技术细节改写为它对结论的影响。
3. **判断是否可以交给作者审阅**（`map.json` 的 `readiness`）。审阅者的职责之一是判断稿件是否已经值得人花时间：
   - `ready`：剩下的都是需要人判断的问题，可以交给用户；
   - `revise_first`：还有应由稿件的作者（往往是另一个 agent）先完成的工作，例如修正材料或代码、补跑分析、核实数据。列出任务（`tasks`，每条写做什么、为什么，`refs` 指向相关批注），按 `skills/WRITING.md` 写。
   判断标准：如果某个问题决定了主要结论能否成立，而解决它不需要用户的判断，它就应当先退回作者，不必让用户面对。
   **作者自己的方案**（`work: plan`，`user_role` 为 `author`）：不存在"交给另一个作者"，`readiness` 只回答方案能否着手实施：`ready`（可以开始做，剩下的边做边定）或 `revise_first`（有几件事不先想清楚，动手会白做），`text` 写理由；`tasks` 留空，下一步写在 `overview.path`；不写 `handback.md`。
   评判别人的作品时，`readiness` 就是审稿人的结论：`ready` 表示可以接受（`tasks` 可以列出建议作者完善之处），`revise_first` 表示需要作者修改后再看。作者无法联系（例如已发表的作品）时照样写出任务，说明它们本应退回作者。
4. 判断为 `revise_first` 时：`RH handback <会话>` 写出给作者的任务清单 `handback.md`（证据路径已换成原项目里的路径）。然后用 ListAgents 查看是否有正在处理原项目的 agent 会话（看它的名称和工作目录是否对应 `plan.artifact.original_location` 所在的项目）。有的话，问用户一句是否发给它；用户同意后用 SendMessage 发送 `handback.md` 的全文，并在 `readiness` 里记下 `sent_to` 和 `sent_at`。没有的话，告诉用户清单的位置。
5. `RH validate <会话>`。

`RH summary <会话>` 生成 `summary.md`，把它的内容展示给用户（第一行就是数量；有自改时列出改了什么）。用两三句话说明：
- 是否建议先退回作者修改，以及最重要的一两项任务；
- 各方面的评价一句话带过（哪几个扎实、哪几个有明显问题）；
- 有几条线索要用户追问，挑威胁最大的一两条用一句话说；
- 自审了几轮、自己改了几处、还剩什么要用户拿主意；
- 改动都只在副本里，用户可以在界面上逐处保留或撤销，最后同意了才写回原文件；
- 剩下的问题可以在界面上和 AI 讨论。
- 说明审阅完成后（页面上"定稿"或"再审一轮"，或在对话中说一声），AI 会回顾这次审阅，提出改进今后审阅的建议；
- 介绍答辩模式（`review-devil`）：用一句话说明它做什么，并点明这份作品对应的提问者（学术报告是答辩委员会的教授，产品方案是投资人，技术设计是首席工程师……），问用户是否要试。不要等用户自己发现它。
然后问一句：要在界面里看吗？要就按 `review-open` 打开；也可以直接在对话里回应。

## 通过对话给反馈（不用界面时）
用户在对话里回应时，把每个动作写成一条反馈：
`RH add-feedback <会话> --kind reaction --target F001 --reaction ai_wrong --note "..."`
（`--kind` 还可以是 `confirm --target R001 --value wrong`（撤销一处 AI 自改；`correct` = 保留）、`suggestion --text "..." --scope this_type|general|this_review`（对审核本身的建议）、`choice --target D02 --option O2`、`answer --target D01 --text`、`prediction --target D03 --text`、`confirm --target C03 --value correct|wrong`、`signoff --outcome deliver|another_round [--quality 1-5]`。脚本会从目标对象上复制锚点。）
"F001 已修、F002 无所谓"这样的一句话拆成多条。完成审阅不受未处理问题的限制，它们会一并记入记录。

## 再审（第 2 轮起）
- AI 自改之后：快照已经是新版本，直接 `RH round-start`。用户自己改了原文件之后（没有未写回的 AI 改动时）：`RH snapshot <会话>` 重新从原位置复制快照，打印新的哈希和改动的行。
- `RH round-start` 会把上一轮的 `map.json` 存到 `work/round-<N-1>/map.json`；旧发现和旧反馈里的 id 指向那一版。新一轮的 `map.json` 保留仍然存在的决策点和主张的 id，新内容用新编号（不复用旧编号）。
- 查找者仍按方面分组，只拿 diff（`work/round-<N-1>/before-revise/` 与 `artifact/` 的差异，或 snapshot 打印的改动）、`changes.jsonl` 里上一轮的改动和上一轮的发现：核查新内容（改动本身也可能引入新问题），并逐条确认旧发现是否修好。验证者确认修好的用 `RH set-finding-status <会话> F001 fixed`；没修好、锚点又变了的用 `RH reanchor <会话> F001 "<新版本里的原文>"`。
- 验证者发现某处自改是错的：作为新发现报出（`problem_kind: bad_revision`），下一次自改时改回或重改。
- 改动影响到的方面，评价跟着更新（验证者给出新的 `assessments.jsonl`，写回 `map.json`）。
- 只审改动部分及其下游决策点；上游决策点被改动时，把下游已确认的决策点标为 `invalidated`。
- 同一发现不重复提出（`fingerprint` 会拦住）。
- `RH status <会话>` 会给出收敛判断：连续两轮没有新的 S1/S2 就停；连续三轮仍有新 S1，告诉用户退一步，回到上游重新考虑设计。
