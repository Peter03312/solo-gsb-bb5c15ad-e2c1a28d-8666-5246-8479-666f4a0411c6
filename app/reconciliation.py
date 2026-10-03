"""成果包交付前复核（只读）。

成果包交付前，复核人员需要知道**封存结论能否沿用**：给定成果包编号与预期的
当前正式修订号，服务在**同一只读快照视图**（`REPEATABLE READ READ ONLY`）
内把包内每个地层对的封存关系结论、最优证据路径与**当前正式结果**逐项比较，
并从修订变化台账中找出每对**自封存修订之后首次偏离封存结果**的修订号及
变化来源；即使后来已恢复一致，曾偏离记录也保留。

比较分类（每对恰好一类）：

- `conclusion_changed`（结论改变）：封存关系与当前关系不同
  （同一期 / 早于 / 晚于 / 未定之间发生变化；路径差异随之附带）；
- `path_only_changed`（仅路径改变）：关系一致，但最优证据路径不同；
- `identical`（完全一致）：关系与最优路径都与封存一致。

当前结果口径：

- 关系与最优路径由**当前全部生效断言**（`status='active'`）重新推导，
  途经未入选地层的证据同样参与推导（不按包入选集合裁剪证据图），路径选取
  规则与关系查询一致（证据数最少、并列按证据编号序列字典序最小）；
- 当前关系未定时路径为空数组 `[]`；封存未定的对，封存路径同样按空处理。

首次偏离口径：

- 自**定版封存修订之后**（`revision > 包的 snapshot_revision`）的
  `revision_changes` 台账中，取该对（按地层编号固定顺序）第一条
  **修订后状态（new_relation / new_path）与封存结果不一致**的记录；
  无论是结论偏离还是仅路径偏离都算；当前已恢复一致也保留该历史记录；
  从未偏离则 `first_deviation` 为 `null`；
- 台账由每次成功正式修订（正式批次 / 推演并入 / 野外整包汇入 / 勘误）
  原子写入并在启动时幂等回填，故历史可完整追溯。

全程只读：不写库、不递增修订号、不改变成果包、断言与变化记录。
成果包不存在（404 `PACKAGE_NOT_FOUND`）、预期修订号过期
（409 `STALE_REVISION`）明确拒绝。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .db import db_conn
from .derivation import Assertion, Derivation, RELATION_LABELS
from .errors import ApiError
from .models import PackageReconcileIn

# 比较分类
CONCLUSION_CHANGED = "conclusion_changed"
PATH_ONLY_CHANGED = "path_only_changed"
IDENTICAL = "identical"

COMPARE_LABELS = {
    CONCLUSION_CHANGED: "结论改变",
    PATH_ONLY_CHANGED: "仅路径改变",
    IDENTICAL: "完全一致",
}

# 修订变化来源（与 revision_changes.source 同口径）的中文说明
SOURCE_LABELS = {
    "formal_batch": "正式断言批次",
    "projection_merge": "推演并入",
    "field_package": "野外整包汇入",
    "field_package_correction": "野外整包勘误",
}

_PACKAGE_COLUMNS = (
    "id, version, revision, label, strata, pairs, evidence, transit_strata, content_hash, created_at"
)


def _path_detail(rows_by_id: Dict[str, Tuple[str, str, str, str]], path: List[str]) -> List[Dict[str, Any]]:
    """把当前证据编号序列展开为逐步原始断言（按路径顺序）。"""
    return [
        {
            "evidence_id": eid,
            "stratum_a": rows_by_id[eid][0],
            "stratum_b": rows_by_id[eid][1],
            "relation": rows_by_id[eid][2],
            "status": rows_by_id[eid][3],
        }
        for eid in path
        if eid in rows_by_id
    ]


def _classify(sealed_rel: str, sealed_path: List[str], current_rel: str, current_path: List[str]) -> str:
    """逐项比较：先看结论，结论一致再比最优路径。"""
    if sealed_rel != current_rel:
        return CONCLUSION_CHANGED
    if sealed_path != current_path:
        return PATH_ONLY_CHANGED
    return IDENTICAL


def _first_deviation(
    rows: List[tuple],
    sealed_rel: str,
    sealed_path: List[str],
) -> Optional[Dict[str, Any]]:
    """从该对封存修订之后的变化台账行（已按修订号升序）中找首次偏离。

    判定依据是该修订**提交后**状态（new_relation/new_path）与封存结果不同；
    只与封存基线比对，不与上一修订比对——台账无该对记录的修订间状态不变，
    故首个“新状态偏离封存”的台账记录即首次偏离修订。
    """
    # 行序：revision, stratum_a, stratum_b, old_relation, new_relation,
    #       old_path, new_path, source
    for r in rows:
        revision, old_rel, new_rel, old_path, new_path, source = (
            r[0], r[3], r[4], r[5], r[6], r[7]
        )
        new_path = list(new_path)
        if new_rel != sealed_rel or new_path != sealed_path:
            return {
                "revision": revision,
                "source": source,
                "source_label": SOURCE_LABELS.get(source, source),
                "previous_relation": old_rel,
                "previous_relation_label": RELATION_LABELS[old_rel],
                "previous_evidence_path": list(old_path),
                "relation": new_rel,
                "relation_label": RELATION_LABELS[new_rel],
                "evidence_path": new_path,
            }
    return None


def reconcile_package(package_id: int, body: PackageReconcileIn) -> Dict[str, Any]:
    """只读复核：逐项比较包内封存关系/最优路径与当前正式结果，并给首次偏离。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 整次核对在同一只读快照视图内完成：包、修订号、生效断言、变化台账
            # 取自同一事务快照，不输出混合版本数据。
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            cur.execute(
                f"SELECT {_PACKAGE_COLUMNS} FROM thematic_packages WHERE id = %s",
                (package_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "PACKAGE_NOT_FOUND",
                    f"成果包不存在: {package_id}",
                    {"package_id": package_id},
                )

            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            current_revision = cur.fetchone()[0]
            if body.expected_revision != current_revision:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current_revision}；"
                    "请取当前修订号后重新核对",
                    {
                        "current_revision": current_revision,
                        "expected_revision": body.expected_revision,
                    },
                )

            # 当前生效断言（全部，不按入选集合裁剪）：途经未入选地层的证据
            # 仍参与当前推导。
            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation, status "
                "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
            )
            active_rows = cur.fetchall()

            # 自封存修订之后的全部变化台账行（一次取回，按对在内存分组）。
            sealed_revision = row[2]
            cur.execute(
                "SELECT revision, stratum_a, stratum_b, old_relation, new_relation, "
                "old_path, new_path, source "
                "FROM revision_changes WHERE revision > %s ORDER BY revision, seq",
                (sealed_revision,),
            )
            change_rows = cur.fetchall()

    package_id = row[0]
    version = row[1]
    label = row[3]
    strata = list(row[4])
    sealed_pairs = row[5]
    transit_strata = list(row[7])
    content_hash = row[8]
    created_at = row[9].isoformat()

    rows_by_id: Dict[str, Tuple[str, str, str, str]] = {
        r[0]: (r[1], r[2], r[3], r[4]) for r in active_rows
    }
    assertions = [
        Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3])
        for r in active_rows
    ]
    derivation = Derivation(assertions)

    changes_by_pair: Dict[Tuple[str, str], List[tuple]] = {}
    for cr in change_rows:
        changes_by_pair.setdefault((cr[1], cr[2]), []).append(cr)

    pair_results: List[Dict[str, Any]] = []
    summary = {CONCLUSION_CHANGED: 0, PATH_ONLY_CHANGED: 0, IDENTICAL: 0}
    ever_deviated = 0

    for sealed in sealed_pairs:
        a, b = sealed["stratum_a"], sealed["stratum_b"]
        sealed_rel = sealed["relation"]
        # 封存未定关系无 evidence_path 字段；统一按空路径比较
        sealed_path = list(sealed.get("evidence_path") or [])

        current_rel, current_path_raw = derivation.relation(a, b)
        current_path = list(current_path_raw or [])

        compare = _classify(sealed_rel, sealed_path, current_rel, current_path)
        summary[compare] += 1

        first_dev = _first_deviation(
            changes_by_pair.get((a, b), []), sealed_rel, sealed_path
        )
        if first_dev is not None:
            ever_deviated += 1

        pair_results.append(
            {
                "stratum_a": a,
                "stratum_b": b,
                "comparison": compare,
                "comparison_label": COMPARE_LABELS[compare],
                "sealed": {
                    "relation": sealed_rel,
                    "relation_label": RELATION_LABELS[sealed_rel],
                    "evidence_path": sealed_path,
                },
                "current": {
                    "relation": current_rel,
                    "relation_label": RELATION_LABELS[current_rel],
                    "evidence_path": current_path,
                    "path_detail": _path_detail(rows_by_id, current_path),
                },
                "first_deviation": first_dev,
            }
        )

    return {
        "package_id": package_id,
        "version": version,
        "snapshot_revision": sealed_revision,
        "label": label,
        "strata": strata,
        "transit_strata": transit_strata,
        "content_hash": content_hash,
        "created_at": created_at,
        "expected_revision": body.expected_revision,
        "current_revision": current_revision,
        "revision": current_revision,
        "summary": {
            "total_pairs": len(pair_results),
            "conclusion_changed": summary[CONCLUSION_CHANGED],
            "path_only_changed": summary[PATH_ONLY_CHANGED],
            "identical": summary[IDENTICAL],
            "ever_deviated": ever_deviated,
        },
        "pairs": pair_results,
    }
