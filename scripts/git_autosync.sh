#!/bin/bash
# 每天半夜 00:00 把本地改动同步到 CNB（2026-10-15 用户要求）。
#
# 由 ~/Library/LaunchAgents/com.xianyu.orderhub.gitsync.plist 每天 00:00 拉起。
# 干两件事：先把 CNB 上别处推的提交拿回来，再把本地改动提交推上去。
#
# 三条底线（半夜的脚本不配替人做决定）：
#   1. 工作区干净 → 只拉不推，绝不产生空提交；
#   2. 分叉后自动 rebase 失败 → 立刻撤回到跑之前的状态，第二天手工处理；
#   3. 只走快进 / rebase，永远不 force push。
#
# 手工跑一次：bash scripts/git_autosync.sh
# 看日志：tail -f ~/Library/Logs/orderhub-gitsync.log
set -u

# launchd 给的默认 PATH 里没有 /usr/local/bin，而 git 装在这儿
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO="/Users/daiguangjiu/CodeBuddy/20260924213218/xianyu-order-hub"
REMOTE="cnb"
BRANCH="main"
LOG="$HOME/Library/Logs/orderhub-gitsync.log"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

cd "$REPO" || { say "进不去仓库目录 $REPO"; exit 1; }

git fetch "$REMOTE" "$BRANCH" >>"$LOG" 2>&1 || {
  say "取不到 CNB 上的最新提交（网络问题？），本次跳过"; exit 1; }

# 有改动就先落成提交（先落再拉，工作区是干净的才敢 rebase）
if git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  say "没有改动，跳过提交"
else
  git add -A >>"$LOG" 2>&1 || { say "git add 失败"; exit 1; }
  if ! git commit -m "chore: 每日自动同步 $(date '+%F %H:%M')" >>"$LOG" 2>&1; then
    say "提交失败（暂存区为空？）"; exit 1; fi
  say "已提交本地改动"
fi

# 把远端拿回来：能快进就快进，需要挪位置就 rebase（冲突就整体撤回）
if ! git merge --ff-only "$REMOTE/$BRANCH" >>"$LOG" 2>&1; then
  git merge --abort >>"$LOG" 2>&1
  if ! git rebase "$REMOTE/$BRANCH" >>"$LOG" 2>&1; then
    git rebase --abort >>"$LOG" 2>&1
    say "自动同步失败：本地与 CNB 分叉且 rebase 有冲突 —— 已恢复原状，请手工 pull 后再跑"
    exit 1
  fi
fi

git push "$REMOTE" "$BRANCH" >>"$LOG" 2>&1 || { say "推送 CNB 失败"; exit 1; }

say "同步完成：$(git log --oneline -1)"
