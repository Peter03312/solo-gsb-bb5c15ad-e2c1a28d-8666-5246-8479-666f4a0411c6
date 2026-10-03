"""野外终端离线整包汇入正式库，及已汇入整包的勘误替换。

野外终端在无网络条件下用**地层短号**记录“同一期 / 早于”断言，回站后整包汇入：

- 请求携带采集站点、终端编号、包号、预期正式修订号、短号→全库地层编号对照、
  以及带包内唯一序号的断言；**断言端点只能引用本包短号**。
- 对照目标已登记时站点须一致（`SITE_CONFLICT`），未登记时随包建立；
  同一终端的同一短号跨包不可改绑（`SHORT_CODE_REBOUND`）。
- 正式证据编号由 (终端编号, 包号, 序号) **稳定确定**（`evidence_id_for`），
  且不得占用已有编号（`FIELD_EVIDENCE_TAKEN`）。
- 整包沿用正式批次的最终关系核验（组内早于 / 早于闭环，冲突附证据路径）：
  任一错误都不落地层、绑定或断言；成功只递增一次正式修订，与变化记录、
  包台账同事务提交。
- 相同终端 + 包号 + 内容（内容指纹不含 expected_revision）重试原样返回
  首次结果，即使正式修订已前进（`replayed=true`）；异内容复用拒绝
  （`FIELD_PACKAGE_CONTENT_MISMATCH`）。

勘误（`correct_field_package`）：对已成功汇入的整包，以 (终端, 原包号, 新包号)
为幂等键，在**同一事务**内撤回原包全部仍生效证据、按新包号汇入勘误新包
（短号 / 序号 / 端点 / 站点 / 跨包绑定 / 稳定证据编号全部沿用汇入规则）、
对最终断言集核验组内早于与闭环、把原包台账标记为 superseded 并记录替换关系。
任一错误或冲突均不改变地层、绑定、断言、台账与修订；成功只递增一次正式修订。
原包台账、首次响应与历史定版 / 推演冻结结果保持可查，原包号原请求重放仍返回
首次汇入结果；同键同内容重试即使修订已前进也返回首次勘误结果，异内容拒绝。
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple, Union
from psycopg2.extras import Json

from .changes import insert_revision_changes, relation_diffs
from .db import db_conn
from .derivation import Assertion, Derivation
from .errors import ApiError
from .models import FieldPackageCorrectionIn, FieldPackageIn

SEQ_WIDTH = 12  # 序号补零宽度：稳定且字典序友好（上限 10**12-1，由模型 ge=1 与上层约束共同保证）

# 咨询锁键空间的固定域标识，避免整包汇入 / 勘误 / 终端口径互相误撞。
_LOCK_DOMAIN_PACKAGE = 9173
_LOCK_DOMAIN_TERMINAL = 9174
_LOCK_DOMAIN_CORRECTION = 9175


# ------------------------------------------------------------------ 稳定标识
def _b64url(text: str) -> str:
    """UTF-8 字节的 URL 安全 base64（去填充）。结果只含 [A-Za-z0-9_-]，
    与证据编号的 ':' 分隔符互不冲突，且对任意终端编号 / 包号双射可逆。"""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def evidence_id_for(terminal_id: str, package_no: str, seq: int) -> str:
    """证据编号由终端、包号、包内序号稳定确定：F:<终端>:<包号>:<序号>。

    终端 / 包号段做无歧义的 base64url 编码；同输入永远得到同一编号，
    不同输入永不碰撞（编码双射 + 固定分隔符）。
    """
    return f"F:{_b64url(terminal_id)}:{_b64url(package_no)}:{seq:0{SEQ_WIDTH}d}"


# 汇入请求与勘误请求在内容口径上同构（site/mappings/assertions），统一以结构类型处理。
PackageBody = Union[FieldPackageIn, FieldPackageCorrectionIn]


def canonical_content(body: PackageBody) -> Dict[str, Any]:
    """参与幂等指纹的规范化内容（**不含 expected_revision**）。

    mappings 按短号排序去重（重复短号在结构校验阶段拒绝），
    assertions 按序号排序；指纹只依赖语义内容，与数组输入顺序无关。
    """
    mappings = sorted(
        ({"short_code": m.short_code, "stratum_id": m.stratum_id} for m in body.mappings),
        key=lambda m: m["short_code"],
    )
    assertions = sorted(
        (
            {
                "seq": a.seq,
                "stratum_a": a.stratum_a,
                "stratum_b": a.stratum_b,
                "relation": a.relation,
            }
            for a in body.assertions
        ),
        key=lambda a: a["seq"],
    )
    return {"site": body.site, "mappings": mappings, "assertions": assertions}


def content_hash_of(content: Dict[str, Any]) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------------ 结构校验（事务外）
def validate_structure(body: PackageBody, package_no: str) -> Tuple[Dict[str, str], Dict[int, Dict[str, Any]]]:
    """包内自洽性校验（不访问数据库）：短号不重复、序号不重复、端点只引本包短号。

    返回 (短号→全库编号, 序号→断言) 供汇入 / 勘误使用。package_no 仅用于错误留痕
    （勘误时为新包号）。
    """
    short_to_full: Dict[str, str] = {}
    for m in body.mappings:
        if m.short_code in short_to_full:
            raise ApiError(
                400,
                "DUPLICATE_SHORT_CODE",
                f"包内短号重复: {m.short_code}",
                {"terminal_id": body.terminal_id, "package_no": package_no, "short_code": m.short_code},
            )
        short_to_full[m.short_code] = m.stratum_id

    by_seq: Dict[int, Dict[str, Any]] = {}
    for a in body.assertions:
        if a.seq in by_seq:
            raise ApiError(
                400,
                "DUPLICATE_SEQUENCE",
                f"包内序号重复: {a.seq}",
                {"terminal_id": body.terminal_id, "package_no": package_no, "seq": a.seq},
            )
        for endpoint in (a.stratum_a, a.stratum_b):
            if endpoint not in short_to_full:
                raise ApiError(
                    400,
                    "UNKNOWN_SHORT_CODE",
                    f"断言端点引用了本包未对照的短号: {endpoint}（序号 {a.seq}）",
                    {
                        "terminal_id": body.terminal_id,
                        "package_no": package_no,
                        "seq": a.seq,
                        "short_code": endpoint,
                    },
                )
        by_seq[a.seq] = {
            "seq": a.seq,
            "short_a": a.stratum_a,
            "short_b": a.stratum_b,
            "full_a": short_to_full[a.stratum_a],
            "full_b": short_to_full[a.stratum_b],
            "relation": a.relation,
        }
    return short_to_full, by_seq


# ------------------------------------------------------------------ 咨询锁
def _lock(cur, domain: int, key: str) -> None:
    """事务级咨询锁：hashtextextended 返回 bigint，对应单键版
    pg_advisory_xact_lock(bigint)。domain 区分键空间，key 为域内逻辑键。"""
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))", (key, domain))


def _package_lock_key(terminal_id: str, package_no: str) -> str:
    return f"{terminal_id}\x1f{package_no}"


def _terminal_lock_key(terminal_id: str) -> str:
    return f"{terminal_id}\x1f\x1f"


def _correction_lock_key(terminal_id: str, original_package_no: str, new_package_no: str) -> str:
    return f"{terminal_id}\x1f{original_package_no}\x1f{new_package_no}"


# ------------------------------------------------------------------ 共享落库
def _write_new_package(
    cur,
    *,
    body: PackageBody,
    terminal_id: str,
    package_no: str,
    site: str,
    new_revision: int,
    short_to_full: Dict[str, str],
    by_seq: Dict[int, Dict[str, Any]],
    ordered_short_codes: List[str],
    retract_evidence: List[str],
    before_assertions: List[Assertion],
    conflict_code: str,
    conflict_message: str,
    conflict_extra: Dict[str, Any],
    source: str,
    audit_batch: Dict[str, Any],
    expected_revision: int,
    content_hash: str,
) -> Dict[str, Any]:
    """新包落库的共享路径（整包汇入与勘误共同使用；调用方持事务与计数器行锁）。

    顺序：站点核验 → 跨包绑定核验 → 稳定证据编号占用核验 → 建层 / 建绑定 /
    落新断言 → （勘误）撤回原包证据 → 最终断言集组内早于 / 闭环核验 →
    变化记录 → 修订号更新与审计 → 包台账写入。任一阶段抛错都由调用方事务回滚，
    地层、绑定、断言、台账与修订均不改变。成功返回汇入形态的首次响应字典。
    `before_assertions` 必须在任何写入 / 撤回前加载（由调用方提供）。
    """

    # ---- 既有地层站点一致性 ----
    cur.execute("SELECT stratum_id, site FROM strata")
    registered_site: Dict[str, Optional[str]] = {r[0]: r[1] for r in cur.fetchall()}
    new_strata: List[str] = []
    for short in ordered_short_codes:
        full = short_to_full[short]
        if full in registered_site:
            existing_site = registered_site[full]
            # 已登记目标的站点（含未知 NULL）与本包站点必须一致
            if existing_site != site:
                raise ApiError(
                    409,
                    "SITE_CONFLICT",
                    f"对照目标地层 {full} 已登记站点 {existing_site if existing_site is not None else '（未登记站点）'}，与本包站点 {site} 不一致",
                    {
                        "terminal_id": terminal_id,
                        "package_no": package_no,
                        "short_code": short,
                        "stratum_id": full,
                        "registered_site": existing_site,
                        "request_site": site,
                    },
                )
        elif full not in new_strata:
            new_strata.append(full)

    # ---- 同一终端同一短号跨包不可改绑 ----
    cur.execute(
        "SELECT short_code, stratum_id, package_no FROM field_short_bindings "
        "WHERE terminal_id = %s AND short_code = ANY(%s) FOR UPDATE",
        (terminal_id, ordered_short_codes),
    )
    bound: Dict[str, Tuple[str, str]] = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for short in ordered_short_codes:
        full = short_to_full[short]
        if short in bound and bound[short][0] != full:
            prior_full, prior_pkg = bound[short]
            raise ApiError(
                409,
                "SHORT_CODE_REBOUND",
                f"终端 {terminal_id} 的短号 {short} 已在包 {prior_pkg} 绑定 {prior_full}，不可改绑为 {full}",
                {
                    "terminal_id": terminal_id,
                    "short_code": short,
                    "bound_stratum_id": prior_full,
                    "bound_package_no": prior_pkg,
                    "requested_stratum_id": full,
                    "request_package_no": package_no,
                },
            )

    # ---- 稳定证据编号不得占用已有编号（含已撤回，永久唯一）----
    evidence_by_seq: Dict[int, str] = {}
    for seq in sorted(by_seq):
        evidence_by_seq[seq] = evidence_id_for(terminal_id, package_no, seq)
    cur.execute(
        "SELECT evidence_id FROM assertions WHERE evidence_id = ANY(%s)",
        (list(evidence_by_seq.values()),),
    )
    occupied = [r[0] for r in cur.fetchall()]
    if occupied:
        raise ApiError(
            409,
            "FIELD_EVIDENCE_TAKEN",
            f"稳定证据编号已被占用（含已撤回）: {', '.join(sorted(occupied))}",
            {
                "terminal_id": terminal_id,
                "package_no": package_no,
                "occupied_evidence_ids": sorted(occupied),
            },
        )

    # ---- 落地层（未登记随包建立；防御并发登记后再核站点）----
    for full in new_strata:
        cur.execute(
            "INSERT INTO strata (stratum_id, site) VALUES (%s, %s) ON CONFLICT (stratum_id) DO NOTHING",
            (full, site),
        )
    if new_strata:
        cur.execute(
            "SELECT stratum_id, site FROM strata WHERE stratum_id = ANY(%s)",
            (new_strata,),
        )
        rechecked = {r[0]: r[1] for r in cur.fetchall()}
        for full in new_strata:
            if rechecked.get(full) != site:
                raise ApiError(
                    409,
                    "SITE_CONFLICT",
                    f"对照目标地层 {full} 已登记站点 {rechecked.get(full)}，与本包站点 {site} 不一致",
                    {
                        "terminal_id": terminal_id,
                        "package_no": package_no,
                        "stratum_id": full,
                        "registered_site": rechecked.get(full),
                        "request_site": site,
                    },
                )

    # ---- 落绑定（首次建立的短号；ON CONFLICT 兜底并发插入后再核改绑）----
    for short in ordered_short_codes:
        if short not in bound:
            cur.execute(
                "INSERT INTO field_short_bindings (terminal_id, short_code, stratum_id, site, package_no) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (terminal_id, short, short_to_full[short], site, package_no),
            )
    if any(short not in bound for short in ordered_short_codes):
        cur.execute(
            "SELECT short_code, stratum_id, package_no FROM field_short_bindings "
            "WHERE terminal_id = %s AND short_code = ANY(%s)",
            (terminal_id, ordered_short_codes),
        )
        for s_short, s_full, s_pkg in cur.fetchall():
            if s_full != short_to_full[s_short]:
                raise ApiError(
                    409,
                    "SHORT_CODE_REBOUND",
                    f"终端 {terminal_id} 的短号 {s_short} 已在包 {s_pkg} 绑定 {s_full}，不可改绑为 {short_to_full[s_short]}",
                    {
                        "terminal_id": terminal_id,
                        "short_code": s_short,
                        "bound_stratum_id": s_full,
                        "bound_package_no": s_pkg,
                        "requested_stratum_id": short_to_full[s_short],
                        "request_package_no": package_no,
                    },
                )

    # ---- 落断言（证据编号稳定、端点已展开为全库地层编号）----
    for seq in sorted(by_seq):
        item = by_seq[seq]
        cur.execute(
            "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (evidence_by_seq[seq], item["full_a"], item["full_b"], item["relation"], new_revision),
        )

    # ---- 勘误：撤回原包全部证据（在最终核验前生效；整事务失败则一并回滚）----
    for eid in retract_evidence:
        cur.execute(
            "UPDATE assertions SET status = 'retracted', retracted_revision = %s, retracted_at = now() "
            "WHERE evidence_id = %s",
            (new_revision, eid),
        )

    # ---- 整包应用后的最终关系核验（沿用正式批次口径）----
    after_assertions = _load_active(cur)
    conflicts = Derivation(after_assertions).validate()
    if conflicts:
        raise ApiError(
            409,
            conflict_code,
            conflict_message,
            {**conflict_extra, "conflicts": conflicts},
        )

    # ---- 变化记录：与修订号递增同事务 ----
    cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
    all_strata = [r[0] for r in cur.fetchall()]
    diffs = relation_diffs(all_strata, before_assertions, after_assertions)

    cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
    cur.execute(
        "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
        (new_revision, Json(audit_batch)),
    )
    insert_revision_changes(cur, new_revision, diffs, source)

    mappings_payload = [
        {
            "short_code": short,
            "stratum_id": short_to_full[short],
            "site": site,
            "already_registered": short_to_full[short] not in new_strata,
        }
        for short in ordered_short_codes
    ]
    evidence_payload = [
        {
            "seq": seq,
            "evidence_id": evidence_by_seq[seq],
            "stratum_a": by_seq[seq]["full_a"],
            "stratum_b": by_seq[seq]["full_b"],
            "relation": by_seq[seq]["relation"],
        }
        for seq in sorted(by_seq)
    ]
    response = {
        "site": site,
        "terminal_id": terminal_id,
        "package_no": package_no,
        "revision": new_revision,
        "mappings": mappings_payload,
        "evidence": evidence_payload,
        "applied": len(by_seq),
        "relation_changes": len(diffs),
        "replayed": False,
    }

    # ---- 包台账（同包号重试据此原样返回；唯一约束 + 咨询锁保证不重复）----
    cur.execute(
        "INSERT INTO field_packages "
        "(terminal_id, package_no, site, content_hash, expected_revision, applied_revision, "
        " mappings, evidence, request, stored_response) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            terminal_id,
            package_no,
            site,
            content_hash,
            expected_revision,
            new_revision,
            Json(mappings_payload),
            Json(evidence_payload),
            Json(canonical_content(body)),
            Json(response),
        ),
    )
    return response


# ------------------------------------------------------------------ 汇入
def ingest_field_package(body: FieldPackageIn) -> Dict[str, Any]:
    """整包汇入正式库（语义见模块文档）。成功响应的 replayed 字段由路由层标注。"""
    short_to_full, by_seq = validate_structure(body, body.package_no)
    content = canonical_content(body)
    content_hash = content_hash_of(content)
    ordered_short_codes = sorted(short_to_full)

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 同一 (终端, 包号) 的请求在事务级咨询锁上串行，包台账写入无竞争。
            _lock(cur, _LOCK_DOMAIN_PACKAGE, _package_lock_key(body.terminal_id, body.package_no))

            # ---- 幂等判定：相同终端 + 包号只成功一次 ----
            cur.execute(
                "SELECT content_hash, stored_response FROM field_packages "
                "WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.package_no),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[0] != content_hash:
                    raise ApiError(
                        409,
                        "FIELD_PACKAGE_CONTENT_MISMATCH",
                        f"终端 {body.terminal_id} 的包号 {body.package_no} 已以不同内容汇入，异内容复用拒绝",
                        {
                            "terminal_id": body.terminal_id,
                            "package_no": body.package_no,
                            "original_content_hash": existing[0],
                            "request_content_hash": content_hash,
                        },
                    )
                response = dict(existing[1])
                response["replayed"] = True
                return response

            # ---- 过期修订（首次汇入才检查；幂等重试即使修订已前进也返回原结果）----
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision},
                )
            new_revision = current + 1
            before_assertions = _load_active(cur)

            audit_operations = [
                {
                    "seq": seq,
                    "evidence_id": evidence_id_for(body.terminal_id, body.package_no, seq),
                    "stratum_a": item["full_a"],
                    "stratum_b": item["full_b"],
                    "relation": item["relation"],
                }
                for seq, item in sorted(by_seq.items())
            ]
            audit_batch = {
                "source": "field_package",
                "site": body.site,
                "terminal_id": body.terminal_id,
                "package_no": body.package_no,
                "content_hash": content_hash,
                "operations": audit_operations,
            }
            return _write_new_package(
                cur,
                body=body,
                terminal_id=body.terminal_id,
                package_no=body.package_no,
                site=body.site,
                new_revision=new_revision,
                short_to_full=short_to_full,
                by_seq=by_seq,
                ordered_short_codes=ordered_short_codes,
                retract_evidence=[],
                before_assertions=before_assertions,
                conflict_code="FIELD_CONFLICT",
                conflict_message="野外整包与现有断言矛盾，整批拒绝，地层 / 绑定 / 断言均不落地",
                conflict_extra={
                    "terminal_id": body.terminal_id,
                    "package_no": body.package_no,
                    "current_revision": current,
                },
                source="field_package",
                audit_batch=audit_batch,
                expected_revision=body.expected_revision,
                content_hash=content_hash,
            )


# ------------------------------------------------------------------ 勘误
def correct_field_package(body: FieldPackageCorrectionIn) -> Dict[str, Any]:
    """已汇入整包勘误：撤回原包证据 + 新包号汇入，单事务、单次修订、幂等（见模块文档）。"""
    # 新包号须不同于原包号（结构层 400，不占事务、不占包号）
    if body.original_package_no == body.new_package_no:
        raise ApiError(
            400,
            "FIELD_CORRECTION_SAME_PACKAGE",
            f"勘误新包号须不同于原包号: {body.original_package_no}",
            {
                "terminal_id": body.terminal_id,
                "original_package_no": body.original_package_no,
                "new_package_no": body.new_package_no,
            },
        )

    short_to_full, by_seq = validate_structure(body, body.new_package_no)
    content = canonical_content(body)
    content_hash = content_hash_of(content)
    ordered_short_codes = sorted(short_to_full)

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 相关键在同一事务内按字典序加锁：勘误键 + 终端键 + 原包/新包键，
            # 与整包汇入的包键、终端口径互斥，避免并发下台账与绑定竞争。
            lock_keys = sorted(
                {
                    (_LOCK_DOMAIN_CORRECTION, _correction_lock_key(
                        body.terminal_id, body.original_package_no, body.new_package_no
                    )),
                    (_LOCK_DOMAIN_TERMINAL, _terminal_lock_key(body.terminal_id)),
                    (_LOCK_DOMAIN_PACKAGE, _package_lock_key(body.terminal_id, body.original_package_no)),
                    (_LOCK_DOMAIN_PACKAGE, _package_lock_key(body.terminal_id, body.new_package_no)),
                },
                key=lambda t: (t[0], t[1]),
            )
            for domain, key in lock_keys:
                _lock(cur, domain, key)

            # ---- 勘误幂等：同 (终端, 原包号, 新包号) 只成功一次 ----
            cur.execute(
                "SELECT content_hash, response FROM field_package_corrections "
                "WHERE terminal_id = %s AND original_package_no = %s AND new_package_no = %s",
                (body.terminal_id, body.original_package_no, body.new_package_no),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[0] != content_hash:
                    raise ApiError(
                        409,
                        "FIELD_CORRECTION_CONTENT_MISMATCH",
                        "同一原包与新包的勘误已以不同内容成功，异内容复用拒绝",
                        {
                            "terminal_id": body.terminal_id,
                            "original_package_no": body.original_package_no,
                            "new_package_no": body.new_package_no,
                            "original_content_hash": existing[0],
                            "request_content_hash": content_hash,
                        },
                    )
                response = dict(existing[1])
                response["replayed"] = True
                return response

            # ---- 过期修订（首次勘误才检查；幂等重试即使修订已前进也返回首次结果）----
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision},
                )
            new_revision = current + 1
            before_assertions = _load_active(cur)

            # ---- 新包号须未使用（失败勘误不占号；此处只可能查到历史成功行）----
            cur.execute(
                "SELECT status FROM field_packages WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.new_package_no),
            )
            new_row = cur.fetchone()
            if new_row is not None:
                raise ApiError(
                    409,
                    "FIELD_PACKAGE_NO_TAKEN",
                    f"终端 {body.terminal_id} 的新包号 {body.new_package_no} 已被使用（状态 {new_row[0]}）",
                    {
                        "terminal_id": body.terminal_id,
                        "package_no": body.new_package_no,
                        "status": new_row[0],
                    },
                )

            # ---- 原包必须存在且未被替换 ----
            cur.execute(
                "SELECT site, evidence, status, superseded_by_package_no, applied_revision "
                "FROM field_packages WHERE terminal_id = %s AND package_no = %s FOR UPDATE",
                (body.terminal_id, body.original_package_no),
            )
            original_row = cur.fetchone()
            if original_row is None:
                raise ApiError(
                    404,
                    "FIELD_PACKAGE_NOT_FOUND",
                    f"野外包不存在: 终端 {body.terminal_id} 包号 {body.original_package_no}",
                    {"terminal_id": body.terminal_id, "package_no": body.original_package_no},
                )
            original_evidence, original_status, replaced_by, original_revision = original_row[1], original_row[2], original_row[3], original_row[4]
            if original_status != "active":
                raise ApiError(
                    409,
                    "FIELD_PACKAGE_ALREADY_REPLACED",
                    f"原包 {body.original_package_no} 已被新包 {replaced_by} 替换，不可再次勘误",
                    {
                        "terminal_id": body.terminal_id,
                        "original_package_no": body.original_package_no,
                        "replacement_package_no": replaced_by,
                    },
                )

            # ---- 原包证据须全部仍生效（含已撤回；不存在也视为证据不再生效）----
            original_evidence_ids = [item["evidence_id"] for item in sorted(original_evidence, key=lambda x: x["seq"])]
            cur.execute(
                "SELECT evidence_id, status FROM assertions WHERE evidence_id = ANY(%s) FOR UPDATE",
                (original_evidence_ids,),
            )
            evidence_status = {r[0]: r[1] for r in cur.fetchall()}
            inactive = [
                {"evidence_id": eid, "status": evidence_status.get(eid)}
                for eid in original_evidence_ids
                if evidence_status.get(eid) != "active"
            ]
            if inactive:
                raise ApiError(
                    409,
                    "FIELD_PACKAGE_EVIDENCE_INACTIVE",
                    "原包证据未全部仍生效（已撤回或缺失），勘误拒绝；请先恢复或改用正式批次处理",
                    {
                        "terminal_id": body.terminal_id,
                        "original_package_no": body.original_package_no,
                        "inactive_evidence": inactive,
                    },
                )

            audit_operations = [
                {
                    "seq": seq,
                    "evidence_id": evidence_id_for(body.terminal_id, body.new_package_no, seq),
                    "stratum_a": item["full_a"],
                    "stratum_b": item["full_b"],
                    "relation": item["relation"],
                }
                for seq, item in sorted(by_seq.items())
            ]
            audit_batch = {
                "source": "field_package_correction",
                "site": body.site,
                "terminal_id": body.terminal_id,
                "original_package_no": body.original_package_no,
                "new_package_no": body.new_package_no,
                "original_revision": original_revision,
                "content_hash": content_hash,
                "retracted_evidence": original_evidence_ids,
                "operations": audit_operations,
            }

            new_package_response = _write_new_package(
                cur,
                body=body,
                terminal_id=body.terminal_id,
                package_no=body.new_package_no,
                site=body.site,
                new_revision=new_revision,
                short_to_full=short_to_full,
                by_seq=by_seq,
                ordered_short_codes=ordered_short_codes,
                retract_evidence=original_evidence_ids,
                before_assertions=before_assertions,
                conflict_code="FIELD_CORRECTION_CONFLICT",
                conflict_message="野外整包勘误：撤回原包证据并汇入新包后的最终关系核验冲突，整批拒绝，地层 / 绑定 / 断言 / 台账均不变",
                conflict_extra={
                    "terminal_id": body.terminal_id,
                    "original_package_no": body.original_package_no,
                    "new_package_no": body.new_package_no,
                    "current_revision": current,
                },
                source="field_package_correction",
                audit_batch=audit_batch,
                expected_revision=body.expected_revision,
                content_hash=content_hash,
            )

            # ---- 原包台账追加替换指向（台账内容与首次响应本身永不修改）----
            cur.execute(
                "UPDATE field_packages "
                "SET status = 'superseded', superseded_by_package_no = %s, superseded_at = now() "
                "WHERE terminal_id = %s AND package_no = %s",
                (body.new_package_no, body.terminal_id, body.original_package_no),
            )

            # ---- 先写勘误台账，再计算替换链：链的反向遍历要读勘误台账本身 ----
            # 首次响应中的链在台账行落库后计算（含本次替换边）；先插入再用链补写响应。
            correction_block = {
                "original_package_no": body.original_package_no,
                "new_package_no": body.new_package_no,
                "original_revision": original_revision,
                "revision": new_revision,
                "retracted_evidence": original_evidence_ids,
            }
            response = {
                **new_package_response,
                "original_package_no": body.original_package_no,
                "correction": dict(correction_block),
            }

            # ---- 勘误台账（重试据此原样返回；主键 + 咨询锁保证不重复）----
            cur.execute(
                "INSERT INTO field_package_corrections "
                "(terminal_id, original_package_no, new_package_no, site, content_hash, "
                " expected_revision, applied_revision, retracted_evidence, response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    body.terminal_id,
                    body.original_package_no,
                    body.new_package_no,
                    body.site,
                    content_hash,
                    body.expected_revision,
                    new_revision,
                    Json(original_evidence_ids),
                    Json(response),
                ),
            )

            chain = _replacement_chain(cur, body.terminal_id, body.new_package_no)
            correction_block["replacement_chain"] = chain
            response["correction"] = dict(correction_block)
            cur.execute(
                "UPDATE field_package_corrections SET response = %s "
                "WHERE terminal_id = %s AND original_package_no = %s AND new_package_no = %s",
                (
                    Json(response),
                    body.terminal_id,
                    body.original_package_no,
                    body.new_package_no,
                ),
            )
            return response


# ------------------------------------------------------------------ 替换链 / 查询
def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def _replacement_chain(cur, terminal_id: str, package_no: str) -> List[Dict[str, Any]]:
    """替换链：沿勘误台账从链上最早包走到现行新包（含两端）。

    向后经 field_package_corrections（new_package_no -> original_package_no）找链首，
    向前经 field_packages.superseded_by_package_no 走到现行 active 包。
    每个节点给出包号、状态、汇入修订号、替换指向；查询时刻实算（非冻结快照）。
    """
    # 向后找最早原包
    head = package_no
    while True:
        cur.execute(
            "SELECT original_package_no FROM field_package_corrections "
            "WHERE terminal_id = %s AND new_package_no = %s",
            (terminal_id, head),
        )
        row = cur.fetchone()
        if row is None:
            break
        head = row[0]

    chain: List[Dict[str, Any]] = []
    current_no = head
    seen = set()
    while current_no not in seen:
        seen.add(current_no)
        cur.execute(
            "SELECT package_no, status, applied_revision, superseded_by_package_no "
            "FROM field_packages WHERE terminal_id = %s AND package_no = %s",
            (terminal_id, current_no),
        )
        row = cur.fetchone()
        if row is None:
            break
        chain.append(
            {
                "package_no": row[0],
                "status": row[1],
                "revision": row[2],
                "superseded_by_package_no": row[3],
            }
        )
        if row[1] != "superseded" or not row[3]:
            break
        current_no = row[3]
    return chain


def list_field_packages() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT terminal_id, package_no, site, content_hash, expected_revision, applied_revision, "
                "status, superseded_by_package_no, created_at "
                "FROM field_packages ORDER BY terminal_id, package_no"
            )
            return [
                {
                    "terminal_id": r[0],
                    "package_no": r[1],
                    "site": r[2],
                    "content_hash": r[3],
                    "expected_revision": r[4],
                    "revision": r[5],
                    "status": r[6],
                    "superseded_by_package_no": r[7],
                    "created_at": r[8].isoformat(),
                }
                for r in cur.fetchall()
            ]


def get_field_package(terminal_id: str, package_no: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT site, content_hash, expected_revision, applied_revision, mappings, evidence, "
                "stored_response, status, superseded_by_package_no, superseded_at, created_at "
                "FROM field_packages WHERE terminal_id = %s AND package_no = %s",
                (terminal_id, package_no),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "FIELD_PACKAGE_NOT_FOUND",
                    f"野外包不存在: 终端 {terminal_id} 包号 {package_no}",
                    {"terminal_id": terminal_id, "package_no": package_no},
                )
            chain = _replacement_chain(cur, terminal_id, package_no)
            return {
                "terminal_id": terminal_id,
                "package_no": package_no,
                "site": row[0],
                "content_hash": row[1],
                "expected_revision": row[2],
                "revision": row[3],
                "mappings": row[4],
                "evidence": row[5],
                "response": row[6],
                "status": row[7],
                "superseded_by_package_no": row[8],
                "superseded_at": row[9].isoformat() if row[9] else None,
                "replacement_chain": chain,
                "created_at": row[10].isoformat(),
            }


# ------------------------------------------------------------------ 勘误台账
def list_field_corrections(
    terminal_id: Optional[str] = None, original_package_no: Optional[str] = None
) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            sql = (
                "SELECT terminal_id, original_package_no, new_package_no, site, content_hash, "
                "expected_revision, applied_revision, retracted_evidence, created_at "
                "FROM field_package_corrections"
            )
            params: Tuple[Any, ...] = ()
            if terminal_id is not None:
                sql += " WHERE terminal_id = %s"
                params = (terminal_id,)
                if original_package_no is not None:
                    sql += " AND original_package_no = %s"
                    params = (terminal_id, original_package_no)
            sql += " ORDER BY terminal_id, original_package_no, new_package_no"
            cur.execute(sql, params)
            return [
                {
                    "terminal_id": r[0],
                    "original_package_no": r[1],
                    "new_package_no": r[2],
                    "site": r[3],
                    "content_hash": r[4],
                    "expected_revision": r[5],
                    "revision": r[6],
                    "retracted_evidence": list(r[7]),
                    "created_at": r[8].isoformat(),
                }
                for r in cur.fetchall()
            ]


def get_field_correction(terminal_id: str, original_package_no: str, new_package_no: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT site, content_hash, expected_revision, applied_revision, retracted_evidence, "
                "response, created_at FROM field_package_corrections "
                "WHERE terminal_id = %s AND original_package_no = %s AND new_package_no = %s",
                (terminal_id, original_package_no, new_package_no),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "FIELD_CORRECTION_NOT_FOUND",
                    f"野外包勘误不存在: 终端 {terminal_id} 原包 {original_package_no} 新包 {new_package_no}",
                    {
                        "terminal_id": terminal_id,
                        "original_package_no": original_package_no,
                        "new_package_no": new_package_no,
                    },
                )
            return {
                "terminal_id": terminal_id,
                "original_package_no": original_package_no,
                "new_package_no": new_package_no,
                "site": row[0],
                "content_hash": row[1],
                "expected_revision": row[2],
                "revision": row[3],
                "retracted_evidence": list(row[4]),
                "response": row[5],
                "created_at": row[6].isoformat(),
            }
