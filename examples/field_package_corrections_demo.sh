#!/usr/bin/env bash
# 考古地层关系核验服务 —— 已汇入野外整包的勘误替换端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 终端 T-CORR 汇入原包 2026-10-01-01（s1 同一期 s2，s2 早于 s3）；
#   2) 对原包发起勘误：新包号 2026-10-01-01F1，内容修正为 s1 早于 s2；
#      单事务撤回原包全部证据、汇入新包、最终核验、修订恰好 +1，返回替换链；
#   3) 同 (原包, 新包) 同内容重试（预期号故意过期）幂等返回首次勘误结果；
#   4) 异内容复用 / 新旧包号相同 / 新包号已占用 / 原包不存在 / 原包已替换明确拒绝；
#   5) 勘误最终关系成环：FIELD_CORRECTION_CONFLICT 附证据路径，一切不变、不占新包号；
#   6) 另一原包证据被正式批次撤回后勘误：FIELD_PACKAGE_EVIDENCE_INACTIVE；
#   7) 原包台账 superseded 仍可查、原包号原请求重放仍返回首次汇入结果；
#      勘误台账列表 / 详情、替换链、变化追踪（source=field_package_correction）。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
T="FP-CORR-DEMO"
ORIG="2026-10-01-01"
FIX="2026-10-01-01F1"

cur_rev() { curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])'; }

echo "== 0. 健康检查 / 当前修订号 =="
curl -s "$BASE/health"; echo
echo "当前正式修订号：$(cur_rev)"

echo; echo "== 1. 汇入原包 $ORIG：s1 同一期 s2，s2 早于 s3 =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"$ORIG\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 2. 勘误前关系：CR-01 与 CR-02 应为“同一期” =="
curl -s "$BASE/relationship?a=CR-01&b=CR-02" | pp

echo; echo "== 3. 发起勘误：原包 $ORIG -> 新包 $FIX，修正为 s1 早于 s2 =="
echo "   单事务撤回原包证据、汇入新包、最终核验；成功则修订恰好 +1。"
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"$ORIG\",
  \"new_package_no\": \"$FIX\",
  \"expected_revision\": $REV,
  \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 4. 勘误后关系：CR-01 与 CR-02 应为“早于”；原包证据已撤回、新包证据生效 =="
curl -s "$BASE/relationship?a=CR-01&b=CR-02" | pp
echo "-- 生效 / 已撤回断言中的本终端证据："
curl -s "$BASE/assertions?status=active" | python3 -c 'import sys,json;print([a["evidence_id"] for a in json.load(sys.stdin) if a["evidence_id"].startswith("F:") and "RlAtQ09SUi1ERU1P" in a["evidence_id"]])' || true
curl -s "$BASE/assertions?status=retracted" | python3 -c 'import sys,json;print([a["evidence_id"] for a in json.load(sys.stdin) if a["evidence_id"].startswith("F:") and "RlAtQ09SUi1ERU1P" in a["evidence_id"]])' || true

echo; echo "== 5. 勘误幂等：同原包/同新包/同内容重试，expected_revision 故意写 0（已过期） =="
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"$ORIG\",
  \"new_package_no\": \"$FIX\",
  \"expected_revision\": 0,
  \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 6. 拒绝分支 =="
echo "-- 6.1 同原包/同新包异内容：FIELD_CORRECTION_CONTENT_MISMATCH"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"$ORIG\", \"new_package_no\": \"$FIX\",
  \"expected_revision\": $(cur_rev), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"}]}" | pp || true

echo "-- 6.2 新包号与原包号相同：FIELD_CORRECTION_SAME_PACKAGE"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"$ORIG\", \"new_package_no\": \"$ORIG\",
  \"expected_revision\": $(cur_rev), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"}]}" | pp || true

echo "-- 6.3 新包号已被使用（原包不存在也先报新包号占用）：FIELD_PACKAGE_NO_TAKEN"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"no-such-pkg\", \"new_package_no\": \"$ORIG\",
  \"expected_revision\": $(cur_rev), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"}]}" | pp || true

echo "-- 6.4 原包不存在（新包号未使用时）：FIELD_PACKAGE_NOT_FOUND"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"no-such-pkg\", \"new_package_no\": \"fresh-no-1\",
  \"expected_revision\": $(cur_rev), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"}]}" | pp || true

echo "-- 6.5 原包已被替换，不能再次勘误：FIELD_PACKAGE_ALREADY_REPLACED"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"$ORIG\", \"new_package_no\": \"2026-10-01-01F2\",
  \"expected_revision\": $(cur_rev), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo "-- 6.6 首次勘误修订号过期（新键，不占号）：STALE_REVISION"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"$FIX\", \"new_package_no\": \"2026-10-01-01F1-stale\",
  \"expected_revision\": $(( $(cur_rev) + 5 )), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"}]}" | pp || true

echo; echo "== 7. 勘误最终关系成环：FIELD_CORRECTION_CONFLICT（附证据路径，一切不变、不占新包号） =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"$FIX\",
  \"new_package_no\": \"2026-10-01-01BAD\",
  \"expected_revision\": $REV,
  \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"},
    {\"seq\": 3, \"stratum_a\": \"s3\", \"stratum_b\": \"s1\", \"relation\": \"earlier_than\"}
  ]}" | pp || true
echo "-- 失败后 $FIX 仍为 active、新包号 2026-10-01-01BAD 不占台账（应 404）："
curl -s "$BASE/field-packages/$T/$FIX" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["terminal_id"], d["package_no"], d["status"])'
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/field-packages/$T/2026-10-01-01BAD"

echo; echo "== 8. 原包证据已被正式批次撤回时勘误：FIELD_PACKAGE_EVIDENCE_INACTIVE =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T\", \"package_no\": \"2026-10-02-01\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s4\", \"stratum_id\": \"CR-04\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s4\", \"relation\": \"same_period\"}]}" > /dev/null
R_EVID="$(curl -s "$BASE/field-packages/$T/2026-10-02-01" | python3 -c 'import sys,json;print(json.load(sys.stdin)["evidence"][0]["evidence_id"])')"
# 若证据仍生效（首次运行），先经正式批次撤回；重复运行时已撤回则跳过。
if curl -s "$BASE/assertions?status=active" | grep -q "\"$R_EVID\""; then
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
    \"expected_revision\": $(cur_rev),
    \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$R_EVID\"}]}" | pp
fi
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\", \"original_package_no\": \"2026-10-02-01\", \"new_package_no\": \"2026-10-02-01F1\",
  \"expected_revision\": $(cur_rev), \"site\": \"西山坡\",
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s4\", \"stratum_id\": \"CR-04\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s4\", \"relation\": \"earlier_than\"}]}" | pp || true

echo; echo "== 9. 台账与历史可查 =="
echo "-- 原包 $ORIG 详情：status=superseded，stored_response 仍是首次汇入结果，附 replacement_chain："
curl -s "$BASE/field-packages/$T/$ORIG" | pp
echo "-- 原包号原请求重放（POST /field-packages，预期号 999 已过期）仍返回首次汇入结果 replayed=true："
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T\", \"package_no\": \"$ORIG\",
  \"expected_revision\": 999,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"CR-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"CR-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"CR-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"}
  ]}" | pp
echo "-- 勘误台账列表 / 单条详情："
curl -s "$BASE/field-packages/corrections?terminal_id=$T" | pp
curl -s "$BASE/field-packages/corrections/$T/$ORIG/$FIX" | pp
echo "-- 变化追踪中的勘误来源 source=field_package_correction："
curl -s "$BASE/revision-changes?limit=100" \
  | python3 -c 'import sys,json;rows=[c for c in json.load(sys.stdin)["changes"] if c["source"]=="field_package_correction"];print("勘误变化记录条数:", len(rows));[print(c["revision"], c["stratum_a"], c["stratum_b"], c["old_relation"], "->", c["new_relation"]) for c in rows]'

echo; echo "全部野外整包勘误示例执行完毕。"
