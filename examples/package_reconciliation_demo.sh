#!/usr/bin/env bash
# 考古地层关系核验服务 —— 成果包交付前复核（只读）端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 登记地层、提交断言、定版并封存专题成果包；
#   2) 交付前复核 POST /packages/{id}/reconcile：
#      - 完全一致（identical）；
#      - 仅路径改变（path_only_changed，新增更短直达证据）；
#      - 结论改变（conclusion_changed，新增/撤回使关系在未定与确定间变化）；
#   3) 撤回后已恢复一致，曾偏离记录（first_deviation）仍保留；
#   4) 包不存在 404 PACKAGE_NOT_FOUND、预期修订号过期 409 STALE_REVISION；
#   5) 全程只读：核对后修订号与断言不被改写。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层（已存在返回 STRATUM_EXISTS，不影响后续演示） =="
for s in L1 L2 L3 L4 L5 L6; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null || true
done
echo "已登记 L1～L6"

# 为让演示可重复执行：先探测当前修订号。以下批次仅在全新库（revision=0）时提交；
# 若库中已有本示例数据（重复运行），直接跳到查询环节。
CUR=$(curl -s "$BASE/revision" | python3 -c "import sys,json;print(json.load(sys.stdin)['revision'])")
echo "当前修订号: $CUR"

if [ "$CUR" = "0" ]; then
  echo; echo "== 2. 批次 1（revision 0->1）：L1~L2，L2<L3，L3<L4，L4~L5 =="
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 0,
    "operations": [
      {"op": "assert", "evidence_id": "RV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
      {"op": "assert", "evidence_id": "RV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "RV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "RV-004", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period"}
    ]}' | pp

  echo; echo "== 3. 定版 1（修订 1） =="
  curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
    -d '{"label": "交付前复核基线定版"}' | pp

  echo; echo "== 4. 封存成果包：定版 1，选 L1/L5/L6 =="
  echo "   L1 早于 L5（4 步链，途经未入选 L2/L3/L4）；L1-L6、L5-L6 未定"
  curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
    -d '{"version": 1, "strata": ["L1", "L5", "L6"]}' | pp
fi

PKG=$(curl -s "$BASE/packages" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p.get('snapshot_revision') == 1 and p.get('strata') == ['L1', 'L5', 'L6']:
        print(p['id']); break
")
echo; echo "成果包编号: $PKG"

if [ "$CUR" = "0" ]; then
  echo; echo "== 5. 复核 @修订1：三对全部 identical =="
  curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
    -d '{"expected_revision": 1}' | pp

  echo; echo "== 6. 批次 2（1->2）：新增直达证据 RV-010 L1 早于 L5 =="
  echo "   L1-L5 关系不变、最优路径由 4 步链变为 [RV-010] -> 仅路径改变"
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 1,
    "operations": [
      {"op": "assert", "evidence_id": "RV-010", "stratum_a": "L1", "stratum_b": "L5", "relation": "earlier_than"}
    ]}' | pp

  echo; echo "== 7. 复核 @修订2：L1-L5 仅路径改变，其余完全一致 =="
  curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
    -d '{"expected_revision": 2}' | pp

  echo; echo "== 8. 批次 3（2->3）：新增 RV-020 L5 早于 L6 =="
  echo "   L1-L6、L5-L6 由未定变为早于 -> 结论改变"
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 2,
    "operations": [
      {"op": "assert", "evidence_id": "RV-020", "stratum_a": "L5", "stratum_b": "L6", "relation": "earlier_than"}
    ]}' | pp

  echo; echo "== 9. 复核 @修订3：结论改变 2 对、仅路径改变 1 对，首次偏离修订号已标注 =="
  curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
    -d '{"expected_revision": 3}' | pp

  echo; echo "== 10. 批次 4（3->4）：撤回 RV-010 与 RV-020，当前结果恢复封存口径 =="
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 3,
    "operations": [
      {"op": "retract", "evidence_id": "RV-010"},
      {"op": "retract", "evidence_id": "RV-020"}
    ]}' | pp
fi

echo; echo "== 11. 复核 @修订4：三对当前均 identical，但曾偏离记录仍保留 =="
echo "    ever_deviated=3；L1-L5 first_deviation.revision=2，L1-L6/L5-L6 =3"
curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 4}' | pp

echo; echo "== 12. 拒绝：成果包不存在 -> 404 PACKAGE_NOT_FOUND =="
curl -s -i -X POST "$BASE/packages/999/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 4}' | head -1
curl -s -X POST "$BASE/packages/999/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 4}' | pp

echo; echo "== 13. 拒绝：预期修订号过期 -> 409 STALE_REVISION（附当前修订号） =="
curl -s -i -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1}' | head -1
curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1}' | pp

echo; echo "== 14. 只读保证：核对前后修订号不变、成果包 content_hash 不变 =="
curl -s "$BASE/revision" | pp
curl -s "$BASE/packages/$PKG" | python3 -c "
import sys, json
p = json.load(sys.stdin)
print({'id': p['id'], 'snapshot_revision': p['snapshot_revision'], 'content_hash': p['content_hash']})
"

echo; echo "全部示例执行完毕。"
