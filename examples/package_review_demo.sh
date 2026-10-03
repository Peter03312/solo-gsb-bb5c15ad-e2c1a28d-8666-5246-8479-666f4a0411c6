#!/usr/bin/env bash
# 考古地层关系核验服务 —— 专题成果包交付前复核端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 封存成果包；
#   2) 直达证据导致“仅路径改变”；
#   3) 撤回证据导致“结论改变”；
#   4) 补回证据后当前结论/路径恢复一致，但保留曾偏离记录；
#   5) 预期修订号过期与包不存在明确拒绝；核对全程只读。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
post_json() {
  local path="$1"; local body="$2"
  curl -s -X POST "$BASE$path" -H 'Content-Type: application/json' -d "$body"
}

prefix="RV$(date +%s)"
export L1="${prefix}-L1"; L2="${prefix}-L2"; L3="${prefix}-L3"; X="${prefix}-X"
EV1="${prefix}-EV1"; EV2="${prefix}-EV2"; EV_DIRECT="${prefix}-EVD"

for s in "$L1" "$L2" "$L3" "$X"; do
  post_json /strata "{\"stratum_id\":\"$s\",\"site\":\"复核演示\"}" >/dev/null
done

rev=$(curl -s "$BASE/revision" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')

echo "== 1. 建立 L1 早于 L3 的两证据路径，并让 X 与 L3 未定 =="
batch_1=$(post_json /batches "{
  \"expected_revision\":$rev,
  \"operations\":[
    {\"op\":\"assert\",\"evidence_id\":\"$EV1\",\"stratum_a\":\"$L1\",\"stratum_b\":\"$L2\",\"relation\":\"earlier_than\"},
    {\"op\":\"assert\",\"evidence_id\":\"$EV2\",\"stratum_a\":\"$L2\",\"stratum_b\":\"$L3\",\"relation\":\"earlier_than\"}
  ]}")
printf '%s\n' "$batch_1" | pp
rev=$(printf '%s' "$batch_1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')

export snapshot_file="$(mktemp)"
export package_file="$(mktemp)"
trap 'rm -f "$snapshot_file" "$package_file"' EXIT

echo "== 2. 定版并封存 L1/L3/X =="
post_json /snapshots '{"label":"交付复核演示定版"}' > "$snapshot_file"
post_json /packages "$(python3 - <<PY
import json, os
print(json.dumps({"version": json.load(open(os.environ["snapshot_file"]))["version"],
                  "strata": [os.environ["L1"], os.environ["L3"], os.environ["X"]]}, ensure_ascii=False))
PY
)" > "$package_file"
cat "$package_file" | pp
package_id=$(python3 -c 'import json, os; print(json.load(open(os.environ["package_file"]))["id"])')

echo; echo "== 3. 加入一条直达证据（更短最优路径；仅路径改变） =="
batch_2=$(post_json /batches "{
  \"expected_revision\":$rev,
  \"operations\":[
    {\"op\":\"assert\",\"evidence_id\":\"$EV_DIRECT\",\"stratum_a\":\"$L1\",\"stratum_b\":\"$L3\",\"relation\":\"earlier_than\"}
  ]}")
printf '%s\n' "$batch_2" | pp
rev=$(printf '%s' "$batch_2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
curl -s "$BASE/packages/$package_id/review?expected_revision=$rev" | pp

echo; echo "== 4. 撤回直达证据后恢复原路径（当前一致，仍保留上一修订曾偏离记录） =="
batch_3=$(post_json /batches "{
  \"expected_revision\":$rev,
  \"operations\":[
    {\"op\":\"retract\",\"evidence_id\":\"$EV_DIRECT\"}
  ]}")
printf '%s\n' "$batch_3" | pp
rev=$(printf '%s' "$batch_3" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
curl -s "$BASE/packages/$package_id/review?expected_revision=$rev" | pp

echo; echo "== 5. 撤回关键证据，L1/L3 变为未定（结论改变） =="
batch_4=$(post_json /batches "{
  \"expected_revision\":$rev,
  \"operations\":[{\"op\":\"retract\",\"evidence_id\":\"$EV2\"}]}")
printf '%s\n' "$batch_4" | pp
rev=$(printf '%s' "$batch_4" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
curl -s "$BASE/packages/$package_id/review?expected_revision=$rev" | pp

echo; echo "== 6. 拒绝：expected_revision 过期（409 STALE_REVISION） =="
curl -s -i "$BASE/packages/$package_id/review?expected_revision=$((rev - 1))" | head -20

echo; echo "== 7. 拒绝：成果包不存在（404 PACKAGE_NOT_FOUND） =="
curl -s -i "$BASE/packages/999999999/review?expected_revision=$rev" | head -20

echo; echo "全部示例执行完毕。"
