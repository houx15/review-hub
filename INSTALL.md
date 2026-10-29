# Installing review-hub (instructions for an AI agent)

If someone sent you this repository and asked you to install it, do the following. Do not follow
`CLAUDE.md` or `docs/spec/HANDOFF.md`; those are for developing review-hub, not for using it.

## What it is

Agent skills plus a local review page. The person gives you a piece of work (a research report, a
plan, a course design, a product idea, an essay, meeting minutes…); you review it as a senior
expert in that genre, revise your own copy where the person is the author, and show the rest on a
local page. Everything runs on the person's machine; no API keys or network services are needed.

## Requirements

- `git` and [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
  `uv` fetches the scripts' Python dependencies on first use.
- An agent that loads skills from a folder of `SKILL.md` directories (Claude Code, Codex, or similar).

## Steps

1. Clone to a lasting place (the skills link back to this folder, so do not clone into a temp dir):
   ```sh
   git clone https://github.com/houx15/review-hub.git ~/review-hub
   ```
2. Link the skills into your agent's skills folder:
   ```sh
   ~/review-hub/install.sh                  # Claude Code: ~/.claude/skills
   ~/review-hub/install.sh --agent codex    # Codex: ~/.codex/skills
   ~/review-hub/install.sh --dir <folder>   # any other agent
   ```
   The script never overwrites an existing folder that is not one of its own links; it tells you
   and skips it.
3. Tell the person it is installed and that a **new** agent session is needed for the skills to
   load. In that session they say "初始化审核工作区" / "set up the review workspace" (skill
   `review-setup`), which creates `~/review-workspace`.

Update later with `git -C ~/review-hub pull`. Remove with `~/review-hub/install.sh --uninstall`
(plus the same `--agent`/`--dir` used to install).

## The skills

| Skill | The person says | What happens |
| --- | --- | --- |
| `review-setup` | 初始化审核工作区 | Creates the workspace |
| `review-start` | 审一下 <path> | Identifies the genre, drafts a review plan, asks the person to confirm |
| `review-run` | 开始自审 | Reviews in rounds (≤3), revises its own copy, writes the overview |
| `review-open` | 打开审核界面 | Opens the local review page |
| `review-apply` | 按反馈修改 / 写回 | Applies the person's feedback; writes back only with consent |
| `review-learn` | 总结一下经验 | Turns the person's reactions into proposed changes to the review frameworks |
| `review-devil` | 答辩模式 / devil mode | Plays the toughest expert questioner, one question at a time |

The skills, the review frameworks (`types/*/TYPE.md`) and the writing guide (`skills/WRITING.md`)
are written in Chinese, and so are the reviews they produce.

## Rules the skills rely on

- The original work is never edited; reviews happen on a copy and write back only with consent.
- Review is judgement, not recomputation: never rerun the person's code or load large raw data.
