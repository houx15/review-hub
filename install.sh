#!/bin/sh
# Install the review-hub skills for an AI coding agent by linking them into its skills folder.
# The skills find their scripts by following their own link back to this repository, so keep
# the repository where it is; `git pull` here updates every installed skill.
#
#   ./install.sh                 Claude Code (~/.claude/skills)
#   ./install.sh --agent codex   Codex (~/.codex/skills)
#   ./install.sh --dir <path>    any other agent's skills folder
#   ./install.sh --uninstall     remove the links (add --agent/--dir as above)
set -eu

REPO=$(cd "$(dirname "$0")" && pwd -P)
SKILLS="review-setup review-start review-run review-open review-apply review-learn review-devil"
DIR="$HOME/.claude/skills"
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --agent)
      case "${2:-}" in
        claude) DIR="$HOME/.claude/skills" ;;
        codex) DIR="$HOME/.codex/skills" ;;
        *) echo "unknown agent: ${2:-} (use claude or codex, or --dir <path>)" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --dir) DIR="${2:?--dir needs a path}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  for s in $SKILLS; do
    if [ -L "$DIR/$s" ]; then rm "$DIR/$s"; echo "removed $DIR/$s"; fi
  done
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (the skills run their scripts with it). Install it first:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

mkdir -p "$DIR"
for s in $SKILLS; do
  target="$DIR/$s"
  if [ -L "$target" ]; then
    rm "$target"                      # an earlier install, possibly from another clone
  elif [ -e "$target" ]; then
    echo "skip $target: a file or folder that is not a link is already there; move it and run again" >&2
    continue
  fi
  ln -s "$REPO/skills/$s" "$target"
  echo "linked $target -> $REPO/skills/$s"
done

# a quick check that the helper script runs (uv fetches its dependencies on first use)
if uv run "$REPO/scripts/rh.py" --help >/dev/null 2>&1; then
  echo "ok: helper script runs"
else
  echo "warning: 'uv run $REPO/scripts/rh.py --help' failed; run it to see why" >&2
fi

echo
echo "Done. Start a new agent session and say \"初始化审核工作区\" (or \"set up the review workspace\")."
echo "The workspace defaults to ~/review-workspace."
