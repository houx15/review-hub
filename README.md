# review-hub

> **AI agent：如果有人让你安装它，请读 [`INSTALL.md`](INSTALL.md)。** / If you were asked to install this, read `INSTALL.md`.

Review Engine：一套 agent skill 加一个本地审核界面，帮你审核 AI 产出的报告、计划、方案和文章。AI 像这个领域的资深审稿人一样按审核方面做判断，不重算结果。设计见 `docs/spec/review-engine-design.md`，开发约定见 `docs/spec/HANDOFF.md`，设计决定见 `docs/spec/DECISIONS.md`。

## 目录

| 位置 | 内容 |
| --- | --- |
| `schemas/` | 会话文件的 JSON Schema 和示例（说明见 `docs/spec/SCHEMAS.md`） |
| `types/*/TYPE.md` | 情境库：学术报告、学术写作、实验设计与研究计划、数据分析写作、产品想法与方案、博客与评论文章、个人陈述与申请文书、创意写作、会议纪要、课程设计、技术设计。每个审核方面有关键标准、专家会问的问题、常见毛病（除学术报告的七个方面外都是待你修改的草稿） |
| `skills/` | skill 文本（`SKILL.md`） |
| `scripts/rh.py` | skill 调用的确定性辅助脚本 |
| `scripts/rh_ui.py`、`ui/` | 本地审核界面：Python 标准库服务 + 不需要构建的单页应用 |
| `tests/` | 单元测试、合成测试项目和答案 |
| `docs/trial/` | 端到端试跑记录 |

## 使用

需要 [uv](https://docs.astral.sh/uv/)。

1. 安装：
   ```sh
   git clone https://github.com/houx15/review-hub.git ~/review-hub
   ~/review-hub/install.sh            # Claude Code；Codex 用 --agent codex，其他 agent 用 --dir <skills 目录>
   ```
   也可以把仓库链接发给任何 AI agent，请它安装：它会按 `INSTALL.md` 操作。装好后开一个新的 agent 对话。
2. 在 Claude Code 里说"初始化审核工作区"（review-setup），默认建在 `~/review-workspace`。
3. 有产出要审时说"审一下 <路径>"（review-start）。AI 判断情境，为这份产出设计审核方面（每个方面看哪些部分、关键标准是什么），请你确认。
4. 确认后说"开始自审"（review-run）。AI 自审，在副本上自己改，再审，最多 3 轮或没有严重问题为止。原文件不动。
5. 说"打开审核界面"（review-open）：看 AI 改了什么（可以逐处撤销）、处理剩下的问题、和 AI 讨论、写下对审核的建议。界面只监听本机，讨论靠开着的 agent 对话回复。
   也可以自己启动：`uv run ~/Codings/review-hub/scripts/rh_ui.py --open`，首页列出所有会话。
6. 说"按反馈修改"或"写回"（review-apply）：AI 按你的反馈继续改副本，最后给你看相对原版的全部差异，你同意后才写回原文件。
7. 说"总结一下经验"（review-learn）：AI 汇总你在各次审阅中的回应（不成立、无关紧要、撤销的修改、AI 遗漏的问题、建议），提出对审阅框架的具体修改，你逐条同意后写入，下一次同类审阅即按新的框架进行。
8. 说"答辩模式"或"devil mode"（review-devil）：AI 扮演这类作品最严格的内行提问者（答辩教授、投资人、首席工程师……），一次问一个决定成败的问题，根据回答追问，最后指出哪些回答站得住。

## 测试

```sh
uv run --no-project --with pytest --with jsonschema --with pyyaml --with rfc3339-validator --with playwright pytest tests
```

界面的浏览器测试用本机安装的 Google Chrome（Playwright 的 `channel="chrome"`，不另外下载浏览器）；没有 Chrome 或 Playwright 时自动跳过。
