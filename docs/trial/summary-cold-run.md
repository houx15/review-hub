**S1：2，S2：8**（按根源合并为 9 个问题）

# 审核摘要：大模型给出的起薪是否存在性别差异：配对简历审计

第 1 轮 · standard 档 · 4 个审核者 · 候选 15 条，保留 10 条

## 先预测，再往下看

- **D06**：看结果之前请先写下：如果拒答被正确排除，你预计女性与男性起薪的对数差大约是多少？是否显著？

**一句话结论（产出原文的主张）**：大模型给女性求职者的起薪比条件相同的男性平均低 28.6%（对数差 -0.2857），差异显著，使用大模型招聘会放大性别不平等。

## S1 · 改变主结论或使设计失效

### F001 · 120 条“模型回复”由脚本 generate_data.py 合成，性别差 0.96 写死在代码里，没有简历也没有模型调用

环节：design · 来自原始材料或数据

> 要求给出起薪建议，共收集 120 条回复

用项目里的 generate_data.py（seed 7）在副本中重新生成，结果与 data/responses.jsonl 逐字节相同；脚本里女性起薪直接乘 0.96、女性拒答概率 0.12 对男性 0.04，回复文本是模板。报告描述的构造 60 对简历、逐份发送给模型并无对应材料，“除姓名外完全相同”无从成立或核实，任何差异都只是脚本参数的回声。

证据：

- `src/generate_data.py:1`

  ```
  """Generate synthetic LLM responses for a paired-resume salary audit (fixture)."""
  ```
- `src/generate_data.py:18-23`

  ```
          if random.random() < (0.12 if gender == "female" else 0.04):
              text = random.choice(REFUSALS).format(year=random.choice([2023, 2024]))
          else:
              gap = 0.96 if gender == "female" else 1.0
              salary = round(base * gap * random.uniform(0.97, 1.03), -2)
              text = f"Based on the resume, I would offer a starting salary of ${salary:,.0f} per year."
  ```
- （另有 1 条证据，见 findings.jsonl）

建议：要么用真实简历和真实模型调用重做实验（保存简历文本与调用日志），要么在报告中明确说明数据为合成示例、性别差是预设参数，不得据此对大模型下结论。

### F003 · 拒答文本中的年份（2023/2024）被解析成起薪，10 对拒答配对被当作完整配对，-0.2857 主要由此产生

环节：measurement · 本轮引入

> 所有 60 对简历都是完整配对

parse_salary 取回复中第一个数字，拒答回复里的年份 2023/2024 被当成起薪，所以 analysis.py 认为 60 对都完整；其中 10 条拒答（女 7、男 3）产生约 ±3.5 的极端对数差。正确排除拒答后只有 50 对完整，平均对数差为 -0.0477（SE 0.0032），而不是 -0.2857（SE 0.1837）。

证据：

- `src/parse_salary.py:6-8`

  ```
      # take the first number in the response; refusals have no salary
      m = re.search(r"\d[\d,]*", text)
      return int(m.group().replace(",", "")) if m else None
  ```
- `data/responses.jsonl:2`

  ```
  {"pair_id": 1, "resume_id": "R001F", "gender": "female", "occupation": "nurse", "response": "I can't provide a salary estimate for this 2024 application."}
  ```
- （另有 2 条证据，见 findings.jsonl）

建议：解析时识别拒答（不含 $ 金额的回复记为缺失，或只匹配 $ 后的金额），重跑分析并更新结果段所有数字。

由此连带的问题（根源修好后一般随之解决）：

- F010（S2）均值被 7 个约 -3.6 的极端配对差主导；输出里的中位数 -0.0494 未报告也未排查：「对数差平均为 -0.2857（标准误 0.1837）」

## S2 · 改变某个具体陈述

### F002 · 每份简历只调用一次且未记录模型名称、版本、温度、提示词

环节：reliability · 本轮引入

> 每份简历单独发送给模型一次

数据每个 resume_id 恰好一条回复，字段只有 pair_id/resume_id/gender/occupation/response，没有模型、温度、种子或提示词；项目中也没有调用配置或日志。无法估计同一输入的调用间变异，也无法判断差异是性别效应还是单次采样噪声，更无法确认测的是哪个模型。

证据：

- 运行 `python3 -c "import json,collections;rows=[json.loads(l) for l in open('data/responses.jsonl')];c=collections.Counter(r['resume_id'] for r in rows);print('resume_ids',len(c),'max calls per resume',max(c.values()))"`

  ```
  resume_ids 120 max calls per resume 1
  ```
- 运行 `python3 -c "import json;rows=[json.loads(l) for l in open('data/responses.jsonl')];print(sorted({k for r in rows for k in r}))"`

  ```
  ['gender', 'occupation', 'pair_id', 'response', 'resume_id']
  ```

建议：记录并报告模型名称/版本、温度、种子和提示词；每份简历重复调用多次（如 5 次），报告组内变异并以均值或全部调用做分析。

### F004 · t=-1.56 不显著（双侧 p≈0.125），正文却写 p<0.05 显著

环节：interpretation · 本轮引入

> 差异在统计上显著（p<0.05）

results.txt 给出 t=-1.56，自由度 59 时双侧 p=0.125（单侧也只有约 0.06），按报告自己的数字差异并不显著。正文写“差异在统计上显著（p<0.05）”与输出相反。（另注：修正拒答解析后差异是显著的，但数值约为 -0.048，见 measurement 条。）

证据：

- `out/results.txt:4-6`

  ```
  mean log gap: -0.2857
  se: 0.1837
  t: -1.56
  ```
- 运行 `uv run --with scipy python -c "from scipy import stats;print(round(2*stats.t.sf(1.56,59),3), round(2*stats.t.sf(0.2857/0.1837,59),3))"`

  ```
  0.124 0.125
  ```

建议：按输出如实写 t 和 p；修正解析后用重跑结果重写。

### F005 · 对数差 -0.2857 被直接写成“低 28.6%”，应换算为 exp(-0.2857)-1≈-24.8%

环节：interpretation · 本轮引入

> 即模型给女性的起薪平均低 28.6%

对数点差不等于百分比差，-0.2857 的对数差对应女性起薪约为男性的 75.1%，即低约 24.8%，不是 28.6%。差值较大时这一换算误差不可忽略。

证据：

- `out/results.txt:4`

  ```
  mean log gap: -0.2857
  ```
- 运行 `python3 -c "import math;print(round(math.exp(-0.2857)-1,4))"`

  ```
  -0.2485
  ```

建议：用 exp(β)-1 换算百分比（修正解析后为约 -4.7%）。

### F006 · 只用完整配对会静默丢掉有性别差异的拒答（女 7 / 男 3），且未报告丢弃数量

环节：variable_selection · 本轮引入

> 只有两份简历都给出了起薪的配对进入分析

正确编码拒答后，完整配对规则丢弃 10 对，其中 7 对是女性拒答、3 对是男性拒答，拒答本身就是按性别不同的结果，却不进入任何分析。敏感度：配对差分（n=50）-0.0477，不配对的对数均值差（女 53、男 57）-0.0485（p=0.095）；点估计稳定，但拒答率差异应作为第二个结果报告。

证据：

- `src/analysis.py:19-20`

  ```
  diffs = [math.log(p["female"]) - math.log(p["male"]) for p in pairs.values()
           if "female" in p and "male" in p]
  ```
- 运行 `uv run --with scipy python verify.py`

  ```
  refusals (no $ amount) by gender: {'female': 7, 'male': 3}
  parse_salary on refusals: [(2023, 4), (2024, 6)]
  pairs with >=1 refusal: 10
  original parse: n=60 mean=-0.2857 se=0.1837 t=-1.56 p=0.125 median=-0.0494 exp(mean)-1=-0.2485
  most negative original diffs: [-3.666, -3.624, -3.583, -3.57, -3.569, -3.523, -3.457, -0.087, -0.085]
  only $ amounts: n=50 mean=-0.0477 se=0.0032 t=-14.83 p=9.63e-20 median=-0.0488 exp(mean)-1=-0.0466
  …（另 2 行）
  ```  
  verify.py 在 verifier/ 下（原项目副本）：用原 parse_salary 解析；拒答 = 回复中没有 $ 金额；'only $ amounts' 只取 $ 后金额重跑配对分析

建议：报告被丢弃的配对数和按性别的拒答率（7/60 vs 3/60），并给出不配对比较作为敏感度。

### F007 · 把单个未具名模型、8 个职业的结果推广为'大模型普遍存在'

环节：conclusion · 本轮引入

> 大模型在起薪建议中普遍存在对女性的系统性压低

项目里没有模型调用配置（G06 不存在），报告没写测的是哪个模型、什么版本，数据只覆盖 8 个职业、每份简历调用一次。'大模型……普遍存在'超出了设计能支持的范围（D06）。

证据：

- `<会话>/artifact/report.md:9`

  ```
  我们构造了 60 对简历，覆盖 8 个职业。每对简历除姓名所暗示的性别外完全相同。每份简历单独发送给模型一次，要求给出起薪建议，共收集 120 条回复。
  ```
- 运行 `python3 -c "import json;r=[json.loads(l) for l in open('data/responses.jsonl')]; print(len(r),len({x['resume_id'] for x in r}),len({x['pair_id'] for x in r}),len({x['occupation'] for x in r}))"`

  ```
  120 120 60 8
  ```  
  数据中没有模型名称或版本字段

建议：写明所测模型及版本，结论限定为'本次测试的模型、这 8 个职业'。

### F008 · '远大于劳动力市场性别工资差距'没有出处，修正后的结果也不支持

环节：conclusion · 本轮引入

> 远大于劳动力市场上观察到的性别工资差距

报告没有任何引文，劳动力市场差距这个比较基准没有来源（G08 不存在）。而且这个比较依赖错误的 28.6%；按修正后约 4.7% 的差距，'远大于'无从成立。

证据：

- `<会话>/artifact/report.md:25`

  ```
  远大于劳动力市场上观察到的性别工资差距
  ```
- 运行 `uv run --with scipy python verify.py`

  ```
  refusals (no $ amount) by gender: {'female': 7, 'male': 3}
  parse_salary on refusals: [(2023, 4), (2024, 6)]
  pairs with >=1 refusal: 10
  original parse: n=60 mean=-0.2857 se=0.1837 t=-1.56 p=0.125 median=-0.0494 exp(mean)-1=-0.2485
  most negative original diffs: [-3.666, -3.624, -3.583, -3.57, -3.569, -3.523, -3.457, -0.087, -0.085]
  only $ amounts: n=50 mean=-0.0477 se=0.0032 t=-14.83 p=9.63e-20 median=-0.0488 exp(mean)-1=-0.0466
  …（另 2 行）
  ```  
  verify.py 在 verifier/ 下（原项目副本）：用原 parse_salary 解析；拒答 = 回复中没有 $ 金额；'only $ amounts' 只取 $ 后金额重跑配对分析

建议：删除这句；如果要保留，补上具体文献和数字，并按修正后的估计重新比较。

### F009 · '招聘中使用大模型会显著放大性别不平等'是前文没有的新因果主张，也超出了研究问题

环节：conclusion · 本轮引入

> 这说明在招聘中使用大模型会显著放大性别不平等

研究问题只问模型给出的起薪建议有没有性别差；报告没有测量实际招聘结果，也没有人类决策者这个基准，'放大'所隐含的比较对象没有写明。这句话是 framing 之外的新主张，没有对应的结果。

证据：

- `<会话>/artifact/report.md:5`

  ```
  当大模型被要求为求职者给出起薪建议时，它给女性求职者的起薪是否低于条件相同的男性求职者？
  ```
- `<会话>/artifact/report.md:25`

  ```
  这说明在招聘中使用大模型会显著放大性别不平等。
  ```

建议：删掉或改成有限的含义陈述，例如'如果在招聘中用它来定薪，可能带来性别差异，需要进一步检验'。

## 需要你裁决的决策点

### D01 · 用姓名暗示性别的配对简历（承重 3/3）

如何在简历中操纵性别，使配对只差性别这一个因素？

**请你回答**：60 对简历除姓名外是否逐字相同？所用姓名是否只暗示性别、不暗示种族或年龄？（简历材料不在项目里，AI 无法核对）

- ● O1 只改姓名，由姓名暗示性别，其余内容逐字相同（当前）
- ○ O2 在简历中显式写明性别（并保持姓名中性）

### D02 · 拒答回复如何编码（承重 3/3）

模型拒绝给出起薪时，这条记录如何进入分析？

- ● O1 视为缺失，所在配对不进入分析（当前） —— 按报告所述真正把拒答排除后：完整配对 50 对，对数差 -0.0477（约低 4.7%），t=-14.83；现有代码把拒答里的年份当起薪，得到 60 对、-0.2857、t=-1.56（p≈0.125）
- ○ O2 视为缺失进入配对分析，同时把拒答率按性别作为第二个结果报告 —— 拒答率女性 7/60、男性 3/60，Fisher 检验 p=0.32；配对估计同 O1（-0.0477）

### D06 · 结论推广到"大模型"整体（承重 3/3）

结论适用于哪个范围？

- ● O1 推广到"大模型"普遍（当前）
- ○ O2 限定为本次测试的单个模型和这 8 个职业

### D04 · 只用完整配对做配对内差分（承重 2/3）

用哪些观测、哪种比较估计性别差？

- ● O1 配对内对数差（配对固定效应），只用两份都有起薪的配对（当前）
- ○ O2 不配对：所有有起薪的回复按性别比较对数均值 —— 拒答正确排除后，不配对比较全部有起薪的回复：对数差 -0.0485，Welch t 检验 p=0.096（配对做法 p≈1e-19）

## 没有来源的主张

- C03：「每对简历除姓名所暗示的性别外完全相同」
- C13：「远大于劳动力市场上观察到的性别工资差距」
- C14：「这说明在招聘中使用大模型会显著放大性别不平等」

---

# 验证清单 · 第 1 轮

## 查了什么
| 环节 | 查了什么 | 怎么查的 | 结果 |
| --- | --- | --- | --- |
| framing | 研究问题与实际分析是否一致 | 对照报告"研究问题"与"分析方法""结果"段 | 没问题（问题出在结论超出 framing，见 F009） |
| design | 配对数、职业数、回复条数；数据来源 | 统计 data/responses.jsonl（pairs 60、occupations 8、rows 120）；在 work/ 副本中重跑 src/generate_data.py，与 data/responses.jsonl 逐字节相同 | C01、C02 没问题；F001 |
| operationalization | 概念与指标是否一致 | 对照报告定义段落 | 没有可举证的问题 |
| measurement | 拒答是否真的记为缺失 | 读 src/parse_salary.py:7；在 work/ 副本中按"只取 $ 金额"重新解析 120 条回复 | F003（根源） |
| reliability | 调用次数、运行配置 | 统计每个 resume_id 的回复数；在项目中查找配置/日志 | F002 |
| variable_selection | 完整配对规则的影响 | 在 work/round-1/sens/ 运行 `uv run --with scipy python -I sens.py`（配对 vs 不配对） | F006；敏感度写入 D04 |
| analysis | 标准误、汇总统计量 | 在 work/ 副本中重跑 src/analysis.py，与 out/results.txt 一致；比较均值与中位数 | F010（由 F003 引起） |
| interpretation | 正文数字与输出、换算、显著性 | 逐个对照 out/results.txt；重算 exp(-0.2857)-1 和 t=-1.56 的 p 值 | F004、F005 |
| conclusion | 结论每句话能否追溯到结果 | 逐句对照前面各环节结果 | F007、F008、F009 |
| 敏感度 | D02、D04、D05 换选项重跑 | work/round-1/sens/sens.py、as_coded_extra.py | 已写入 map.json（D02 承重 3，D04 承重 2，D05 承重 1） |

## 没查什么
| 内容 | 原因 |
| --- | --- |
| 文献环节 | 报告没有引文；"远大于劳动力市场性别工资差距"没有出处（G08 找不到） |
| 可视化环节 | 报告没有图表 |
| 简历是否除姓名外完全相同 | 简历材料（G07）不在项目中，只能请你确认（见 D01） |
| 重复调用的组内变异、对提示词和温度的敏感性 | 没有调用配置和重复调用记录（G06） |
| 数据是否来自真实模型调用 | generate_data.py 注释写明是合成回复；无法核对真实调用（见 F001） |
| D01、D06 的敏感度 | 没有简历材料、只有一个模型的数据，无法换选项重跑 |

## AI 默认
- D03 · 每份简历只调用一次：沿用 O1（每份简历调用一次）
- D05 · 用均值汇总配对差：沿用 O1（均值 + 配对 t 检验）。拒答正确编码后，均值与中位数几乎相同

---

**怎么回应**：直接在对话里说，例如「F001 已修，F002 AI 错了：……」「D02 选 O2」「签收」。反应类别：已修 / AI 错了 / 是故意的 / 无所谓 / 这是一类问题。
