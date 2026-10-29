---
name: review-setup
description: 第一次使用 Review Engine 时初始化工作区：选定位置，生成 config.yaml、个人偏好文件和默认审核类型。用户说"初始化审核工作区"、"setup review engine"，或其他 review-* skill 找不到工作区时使用。
---

# review-setup：初始化工作区

> `RH` = `uv run <engine_root>/scripts/rh.py <命令> ...`。`engine_root` 是 review-hub 仓库：本 skill 目录解析符号链接后（`realpath`）往上两级。

## 输入
- 工作区路径。默认 `~/review-workspace`；用户可以指定别的位置。

## 输出
```
<workspace>/
  config.yaml
  personal/blind-spots.md   personal/preferences.md
  types/<type>/TYPE.md  rules.md  checks/
  sessions/
  ledger/incidents.jsonl  ledger/rule-proposals.jsonl  ledger/eval/
```

## 步骤
1. 问一句工作区放哪里（给出默认值）。路径已存在且非空时，先列出里面有什么，不覆盖任何文件。
2. 运行 `RH init <路径>`。脚本只创建缺失的文件，从仓库 `types/` 复制已有的类型模板，已存在的文件原样保留。
3. 如果路径不是默认值，告诉用户在 shell 配置里加 `export REVIEW_WORKSPACE=<路径>`，其他 skill 靠它找到工作区。
4. 用中文告诉用户建好了什么，以及下一步：有产出要审时用 `review-start`。

## 不做的事
- 不写仓库外除工作区以外的任何位置。
- 不改用户其他项目。
