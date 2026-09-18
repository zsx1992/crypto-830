#!/bin/bash
# 等待 15m+1d 扩样本回测完成并取回结果 (结果文件: backtest/result_15m_1d.json)
cd /c/Users/regal/WorkBuddy/2026-08-30-11-33-45 || exit 1
BASE=$(git rev-parse origin-ssh/main)
echo "起始 main: $BASE"
for i in $(seq 1 100); do
  sleep 30
  git fetch origin-ssh main -q 2>/dev/null
  NEW=$(git rev-parse origin-ssh/main)
  if [ "$NEW" != "$BASE" ]; then
    MSG=$(git log origin-ssh/main -1 --oneline)
    echo "新提交: $MSG"
    if echo "$MSG" | grep -q "backtest result"; then
      echo "=== 回测完成 ==="
      git merge --ff-only origin-ssh/main 2>/dev/null || \
        git update-ref refs/heads/main "$NEW" && git reset --hard main >/dev/null 2>&1
      if [ -f backtest/result_15m_1d.json ]; then
        /c/Users/regal/.workbuddy/binaries/python/envs/default/Scripts/python.exe -c "
import json, collections
d=json.load(open('backtest/result_15m_1d.json',encoding='utf-8'))
det=d.get('detail',[])
print('样本总数:', len(det))
print('按形态:', dict(collections.Counter(x['type'] for x in det)))
print('按周期:', dict(collections.Counter(x['interval'] for x in det)))
print('结果分布:', dict(collections.Counter(x['res'] for x in det)))
"
      else
        echo "result_15m_1d.json 缺失"
      fi
      exit 0
    fi
    BASE="$NEW"
  fi
done
echo "等待超时(50分钟)"
