# Skills

每个子目录是一个 Claude Code skill（`SKILL.md`）。所有确定性的工作（建会话、算哈希、校验、计数、渲染摘要）交给 `scripts/rh.py`，skill 只负责需要判断的部分。

调用方式（skill 文本里的 `RH`）：

```
uv run <仓库路径>/scripts/rh.py <命令> ...
```

`<仓库路径>` 是 skill 目录解析符号链接后往上两级。工作区不在默认的 `~/review-workspace` 时，设 `$REVIEW_WORKSPACE`，或在命令名之前加 `--workspace <路径>`。`uv` 会按脚本头部声明自动准备依赖（jsonschema、pyyaml），不装进系统 Python。

| Skill | 阶段 | 状态 |
| --- | --- | --- |
| review-setup | Phase 1 | 可用 |
| review-start | Phase 1 | 可用 |
| review-run | Phase 1 | 可用 |
| review-open | Phase 2 | 可用（`scripts/rh_ui.py` + `ui/`） |
| review-apply | Phase 2 | 可用（`RH apply-plan`） |
| review-learn | Phase 3 | 可用（`RH signals` 汇总回应；提议经同意后写入框架） |
| review-devil | — | 可用：答辩模式，在对话中扮演最严格的内行提问者 |
| review-stats | Phase 3 | 只有文字版 |

所有写给作者看的文字按 `WRITING.md` 写。

`review-aware`（生成时附来源）是否进第一版还没定，暂不写。
