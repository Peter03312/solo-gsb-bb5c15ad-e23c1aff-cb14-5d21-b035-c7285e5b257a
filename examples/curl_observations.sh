#!/usr/bin/env bash
# 安全状态观察调用示例（登记 / 查询 / 取消 + 结论翻转事件分页）。
#
# 演示：
#   1. 登记一个长期生效的限行区域 A
#   2. 登记一条当前安全的未来轨迹观察（201，safe=true，返回 id/revision）
#   3. 登记一条当前不安全的未来轨迹观察（201，safe=false —— 两种结论均可登记）
#   4. 按编号查询观察（轨迹/状态/最近结论/revision）；初始事件页为空
#   5. 登记压住安全观察的新区域：观察不阻止变更（201），结论翻转产生 1 条事件
#   6. 按事件序号游标升序分页读取事件（cursor + next_cursor）
#   7. 删除压区后结论翻回安全，再产生一条事件
#   8. 取消观察（重复取消幂等）；取消后变更不再为其产生事件
#   9. 首点不晚于登记时刻的轨迹 → 422；未知编号 → 404
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_observations.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
# 生成未来时刻（登记要求首点晚于登记时刻）
T1=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00","Z"))')
T2=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=1,minutes=1)).isoformat().replace("+00:00","Z"))')

echo "== 1. 登记限行区域 A：正方形 [0,100]x[0,100]，长期生效 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }' | python3 -m json.tool

echo "== 2. 登记安全观察：轨迹从区域上方 y=200 经过（$T1 → $T2） =="
OBS_S=$(curl -fsS -X POST "$BASE_URL/api/observations" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 200, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 200, \"t\": \"$T2\"}
        ]
      }")
echo "$OBS_S" | python3 -m json.tool
OBS_S_ID=$(echo "$OBS_S" | J "['id']")
echo "观察 id=$OBS_S_ID，当前结论 safe=$(echo "$OBS_S" | J "['safe']")，revision=$(echo "$OBS_S" | J "['revision']")"

echo "== 3. 登记不安全观察：轨迹直穿 A 区（不安全同样可登记） =="
OBS_U=$(curl -fsS -X POST "$BASE_URL/api/observations" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 50, \"t\": \"$T2\"}
        ]
      }")
echo "$OBS_U" | python3 -m json.tool
OBS_U_ID=$(echo "$OBS_U" | J "['id']")
echo "观察 id=$OBS_U_ID，当前结论 safe=$(echo "$OBS_U" | J "['safe']")"

echo "== 4. 按编号查询观察 + 初始事件页（为空） =="
curl -fsS "$BASE_URL/api/observations/$OBS_S_ID" | python3 -m json.tool
curl -fsS "$BASE_URL/api/observations/$OBS_S_ID/events?limit=50" | python3 -m json.tool

echo "== 5. 登记压住安全观察的新区域：观察不阻止变更 → 201 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "B区-吊装半径",
        "polygon": [[0,150],[100,150],[100,250],[0,250]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }' | python3 -m json.tool

echo "== 6. 读取事件：safe→unsafe 一条，带旧/新结论与新 revision =="
curl -fsS "$BASE_URL/api/observations/$OBS_S_ID/events" | python3 -m json.tool
echo "结论未翻转的不安全观察没有事件："
curl -fsS "$BASE_URL/api/observations/$OBS_U_ID/events" | python3 -m json.tool

echo "== 7. 删除压区前先取首页（此时仅 1 条，next_cursor=null） =="
curl -fsS "$BASE_URL/api/observations/$OBS_S_ID/events?cursor=0&limit=1" | python3 -m json.tool

echo "== 8. 删除 B 区 → 结论 unsafe→safe 再产生一条事件，同一观察每笔变更至多一条 =="
# 通过名称拿到 B 区 id
B_ID=$(curl -fsS "$BASE_URL/api/regions" \
  | python3 -c 'import sys,json; print([r["id"] for r in json.load(sys.stdin) if r["name"]=="B区-吊装半径"][0])')
curl -fsS -X DELETE "$BASE_URL/api/regions/$B_ID" | python3 -m json.tool
PAGE1=$(curl -fsS "$BASE_URL/api/observations/$OBS_S_ID/events?cursor=0&limit=1")
echo "$PAGE1" | python3 -m json.tool
NEXT=$(echo "$PAGE1" | J "['next_cursor']")
echo "用 next_cursor=$NEXT 取下一页："
curl -fsS "$BASE_URL/api/observations/$OBS_S_ID/events?cursor=$NEXT&limit=1" | python3 -m json.tool

echo "== 9. 取消观察（幂等，两次结果相同）；取消后变更不再为其产生事件 =="
curl -fsS -X POST "$BASE_URL/api/observations/$OBS_S_ID/cancel" | python3 -m json.tool
curl -fsS -X POST "$BASE_URL/api/observations/$OBS_S_ID/cancel" | python3 -m json.tool
echo "再次登记压区，已取消观察的事件数保持不变："
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "B区-吊装半径",
        "polygon": [[0,150],[100,150],[100,250],[0,250]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }' >/dev/null
curl -fsS "$BASE_URL/api/observations/$OBS_S_ID/events" | python3 -m json.tool

echo "== 10. 非法/未知：首点不晚于登记时刻 → 422；未知编号 → 404 =="
PAST=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
curl -sS -o /tmp/obs_started.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/observations" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 500, \"t\": \"$PAST\"},
          {\"x\": 350, \"y\": 500, \"t\": \"$T2\"}
        ]
      }" || true
python3 -m json.tool /tmp/obs_started.json
curl -sS -o /tmp/obs_404.json -w 'HTTP %{http_code}\n' "$BASE_URL/api/observations/999999" || truepython3 -m json.tool /tmp/obs_404.json

echo "完成。提示：示例区域/观察会保留，可用 DELETE /api/regions/{id} 清理。"
