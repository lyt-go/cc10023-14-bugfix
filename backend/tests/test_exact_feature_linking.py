"""链路6 精确特征名关联验收测试（不依赖 pytest，可直接 python 运行）。

复现并验收以下问题与修复：

1. 场景标注关联特征填 age 时，只含 age_bucket 的分群配置不得被列为相关
   （根因：旧实现用子串匹配 ``f in 特征集字符串``）；
2. 关联特征尾部带逗号时，分隔符留下的空项不是通配符，不得造成更多分群错位；
3. 同前缀字段 age / age_bucket / age_band 各自保持独立身份；
4. 历史场景原文（age_group / age_band）只能按可验证字段别名解释，
   同名歧义（age_band 与新目录精确字段同名）必须保留证据、待人工确认；
5. 分群标注映射数据由关联入口落库，聚类服务读取该映射生成分群实际场景标签，
   标注详情与聚类结果对得上同一规则版本；
6. 重跑同一聚类时，已核实字段可直接适用，冲突别名对应人群进入待确认；
7. 纠正别名后重新提交，核对实际采用字段与原场景记录，旧分群产物保留旧版本。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import service_for
from app.services.feature_catalog import (
    CONFLICT,
    EMPTY,
    EXACT,
    INITIAL_RULE_VERSION,
    VERIFIED_ALIAS,
    catalog,
)
from app.services.scenario_label import ARTIFACT_TABLE, MAPPING_TABLE
from app.store import store

PASS = 0
FAIL = 0


def check(condition: bool, message: str, detail: object = None) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {message}")
    else:
        FAIL += 1
        print(f"  [FAIL] {message} :: {detail}")


def setup_data() -> dict[str, int]:
    """构造字段目录、场景标注与聚类配置（append 到内存仓库，不改动种子数据）。"""
    define_rows = store.rows("feature_define")
    base_define = max(int(r["id"]) for r in define_rows)
    feature_ids: dict[str, int] = {}
    for idx, name in enumerate(["age", "age_bucket", "age_band", "city"], start=1):
        define_rows.append({
            "id": base_define + idx,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "特征编号": f"FEAT-010{idx}",
            "特征名称": name,
            "特征类型": "数值" if name == "age" else "类别",
            "特征版本": "v2.0",
        })
        feature_ids[name] = base_define + idx

    cluster_rows = store.rows("cluster_config")
    base_cluster = max(int(r["id"]) for r in cluster_rows)
    cluster_specs = [
        ("CLUS-EXACT-AGE", "age", "kmeans", 2),          # 只有 age
        ("CLUS-EXACT-BUCKET", "age_bucket", "kmeans", 3),  # 只有 age_bucket
        ("CLUS-BAND", "age_band,city", "kmeans", 2),     # age_band 与 city
    ]
    cluster_ids: dict[str, int] = {}
    for idx, (no, feats, algo, k) in enumerate(cluster_specs, start=1):
        cid = base_cluster + idx
        cluster_rows.append({
            "id": cid,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "配置编号": no,
            "特征集": feats,
            "算法类型": algo,
            "目标簇数": str(k),
            "距离度量": "euclidean",
            "初始化方式": "random",
            "收敛阈值": "0.001",
            "配置版本": "v1.0",
        })
        cluster_ids[no] = cid

    label_rows = store.rows("scenario_label")
    base_label = max(int(r["id"]) for r in label_rows)
    label_specs = [
        ("SCEN-AGE", "年龄规则", "age"),                # 精确 age
        ("SCEN-BUCKET", "年龄分桶规则", "age_bucket"),  # 精确 age_bucket
        ("SCEN-TRAILING", "尾部逗号规则", "age,"),      # 尾部带逗号 → 空项
        ("SCEN-HIST-01", "历史年龄分桶", "age_group,age_band"),  # 历史别名 + 同名歧义
    ]
    label_ids: dict[str, int] = {}
    for idx, (no, scene, feats) in enumerate(label_specs, start=1):
        lid = base_label + idx
        label_rows.append({
            "id": lid,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "标注编号": no,
            "场景名称": scene,
            "关联特征": feats,
            "业务规则": f"{scene}业务规则",
            "标注时间": "2026-11-10",
            "标注人": "tester",
            "标注状态": "草稿",
            "场景版本": "v1.0.0",
        })
        label_ids[no] = lid

    return {"features": feature_ids, "clusters": cluster_ids, "labels": label_ids}


def main() -> int:
    ids = setup_data()
    svc_label = service_for("scenario_label")
    svc_cluster = service_for("cluster_config")
    labels = ids["labels"]
    clusters = ids["clusters"]
    cid_age = clusters["CLUS-EXACT-AGE"]
    cid_bucket = clusters["CLUS-EXACT-BUCKET"]
    cid_band = clusters["CLUS-BAND"]

    print("== 前置：字段目录与别名登记 ==")
    check(catalog.version == INITIAL_RULE_VERSION, "初始规则版本", catalog.version)
    check(catalog.is_canonical("age") and catalog.is_canonical("age_bucket"),
          "age 与 age_bucket 都是目录中的独立精确字段")
    check(catalog.alias_record("age_group").status == VERIFIED_ALIAS,
          "age_group 是可验证别名（→ age_bucket）")
    check(catalog.alias_record("age_band").status == CONFLICT,
          "age_band 存在同名歧义，登记为 conflict 待确认")

    print("== 1) 用 age 查关联：只含 age_bucket 的配置不得相关 ==")
    r_age = svc_label.link_to_cluster_segments(labels["SCEN-AGE"])
    related_age = {c["cluster_id"] for c in r_age["related_clusters"]}
    check(r_age["ok"], "age 标注关联入口执行成功", r_age)
    check(related_age == {cid_age}, "age 只关联到含 age 的分群配置", related_age)
    check(cid_bucket not in related_age,
          "只含 age_bucket 的分群不再被 age 规则错误关联（子串缺陷已修复）", related_age)
    check(r_age["rule_version"] == catalog.version, "关联结果携带规则版本")
    check(r_age["mapping"][0]["matched_features"] == ["age"],
          "映射记录的匹配字段为精确名 age", r_age["mapping"][0])

    print("== 2) 用 age_bucket 查关联：精确命中 age_bucket 分群 ==")
    r_bucket = svc_label.link_to_cluster_segments(labels["SCEN-BUCKET"])
    related_bucket = {c["cluster_id"] for c in r_bucket["related_clusters"]}
    check(related_bucket == {cid_bucket},
          "age_bucket 只关联到含 age_bucket 的分群配置", related_bucket)
    check(cid_age not in related_bucket, "age 分群不会采用 age_bucket 规则", related_bucket)

    print("== 3) 尾部带逗号的标注：空项不是通配符 ==")
    r_tail = svc_label.link_to_cluster_segments(labels["SCEN-TRAILING"])
    related_tail = {c["cluster_id"] for c in r_tail["related_clusters"]}
    empty_ev = r_tail["empty_token_evidence"]
    check(related_tail == {cid_age},
          "age, 仅按 age 精确关联，空项未制造额外分群", related_tail)
    check(cid_bucket not in related_tail and cid_band not in related_tail,
          "空项未被当作任意特征（通配符缺陷已修复）", related_tail)
    check(len(empty_ev) == 1 and empty_ev[0]["status"] == EMPTY,
          "空项作为证据保留而非丢弃", empty_ev)

    print("== 4) 历史场景原文：可验证别名与同名歧义 ==")
    r_hist = svc_label.link_to_cluster_segments(labels["SCEN-HIST-01"])
    resolutions = catalog.resolve_features("age_group,age_band", label_no="SCEN-HIST-01")
    res_by_raw = {r.raw: r for r in resolutions}
    check(res_by_raw["age_group"].status == VERIFIED_ALIAS
          and res_by_raw["age_group"].canonical == "age_bucket",
          "age_group 按可验证证据解释为 age_bucket", res_by_raw["age_group"].to_dict())
    check(res_by_raw["age_band"].status == CONFLICT
          and res_by_raw["age_band"].canonical is None,
          "age_band 同名歧义不自动采用，保留两种候选解释", res_by_raw["age_band"].to_dict())
    check(len(res_by_raw["age_band"].candidates) == 2
          and {c["field"] for c in res_by_raw["age_band"].candidates} == {"age_band", "age_bucket"},
          "同名歧义保留双方证据", res_by_raw["age_band"].candidates)
    # age_group→age_bucket 已核实，因此映射可建立；age_band 歧义使其处于待确认
    hist_related = {c["cluster_id"]: c for c in r_hist["related_clusters"]}
    check(cid_bucket in hist_related, "历史标注经 age_group 别名与 age_bucket 分群建立映射",
          hist_related)
    check(all(c["label_status"] == "pending_confirmation"
              for c in r_hist["related_clusters"]),
          "含同名歧义的标注整体标记为待确认", r_hist["related_clusters"])
    check(cid_age not in hist_related, "历史标注不会误关联到 age 分群", hist_related)

    print("== 5) 规则校验：详情与映射同一规则版本 ==")
    v_age = svc_label.validate_label_rules(labels["SCEN-AGE"])
    check(v_age["ok"] and v_age["adopted_features"] == ["age"],
          "age 标注校验生效，采用精确字段 age", v_age)
    v_hist = svc_label.validate_label_rules(labels["SCEN-HIST-01"])
    check(not v_hist["ok"] and v_hist["applicability"] == "pending_confirmation",
          "历史标注因 age_band 歧义不能生效", v_hist)
    check(v_age["rule_version"] == r_age["mapping"][0]["rule_version"],
          "标注详情与分群标注映射规则版本一致",
          (v_age["rule_version"], r_age["mapping"][0]["rule_version"]))

    print("== 6) 运行分群并回查实际标签（第一轮，旧规则版本） ==")
    run1_bucket = svc_cluster.execute_cluster_and_generate_segments(cid_bucket)
    check(run1_bucket["ok"], "age_bucket 分群聚类执行成功", run1_bucket)
    check(run1_bucket["rule_version"] == INITIAL_RULE_VERSION
          and all(a["rule_version"] == INITIAL_RULE_VERSION
                  for art in run1_bucket["segment_labels"]
                  for a in art["labels_applied"]),
          "分群产物与标注采用同一规则版本", run1_bucket["rule_version"])
    bucket_scenes = {
        a["scene_name"] for art in run1_bucket["segment_labels"] for a in art["labels_applied"]
    }
    check("年龄规则" not in bucket_scenes,
          "只有 age_bucket 的人群没有采用 age 规则", bucket_scenes)
    check("年龄分桶规则" in bucket_scenes,
          "age_bucket 人群采用 age_bucket 精确规则", bucket_scenes)
    hist_pending = {
        a["scene_name"]
        for art in run1_bucket["segment_labels"]
        for a in art["labels_pending_confirmation"]
    }
    check("历史年龄分桶" in hist_pending,
          "历史标注（age_band 歧义）进入待确认人群", hist_pending)
    check(run1_bucket["applicable_segments"] == run1_bucket["segments"]
          and run1_bucket["pending_segments"] == run1_bucket["segments"],
          "分群产物区分当前适用范围与待确认人群",
          (run1_bucket["applicable_segments"], run1_bucket["pending_segments"]))

    run1_age = svc_cluster.execute_cluster_and_generate_segments(cid_age)
    age_scenes = {
        a["scene_name"] for art in run1_age["segment_labels"] for a in art["labels_applied"]
    }
    check(age_scenes == {"年龄规则", "尾部逗号规则"},
          "age 分群只采用 age 精确规则（尾部逗号标注同样精确生效）", age_scenes)
    check(all(not art["labels_pending_confirmation"] for art in run1_age["segment_labels"]),
          "age 分群无待确认标签", run1_age["segment_labels"])

    print("== 7) 维护人员从分群产物识别范围：映射数据查询 ==")
    mapping_view = svc_label.get_segment_label_mapping()
    check(mapping_view["rule_version"] == catalog.version, "映射视图返回当前规则版本")
    applied_keys = {(r["label_no"], r["config_no"]) for r in mapping_view["applied"]}
    pending_keys = {(r["label_no"], r["config_no"]) for r in mapping_view["pending_confirmation"]}
    check(("SCEN-AGE", "CLUS-EXACT-AGE") in applied_keys, "已适用映射可识别", applied_keys)
    check(("SCEN-HIST-01", "CLUS-EXACT-BUCKET") in pending_keys,
          "待确认人群可识别", pending_keys)

    print("== 8) 重跑同一聚类：一个字段已核实，另一个别名冲突仍待确认 ==")
    run2_bucket = svc_cluster.execute_cluster_and_generate_segments(cid_bucket)
    check(run2_bucket["run_no"] == 2, "重跑生成第 2 轮产物", run2_bucket["run_no"])
    check(all("年龄规则" not in [x["scene_name"] for x in art["labels_applied"]]
              for art in run2_bucket["segment_labels"]),
          "重跑仍不会给 age_bucket 人群套用 age 规则")
    check(any(art["labels_pending_confirmation"] for art in run2_bucket["segment_labels"]),
          "age_band 别名未纠正前，历史人群仍等待确认", run2_bucket["segment_labels"])
    # 第一轮产物保留，且仍是旧规则版本
    history = svc_cluster.query_segment_labels(cid_bucket)
    check(history["historical_runs"] == [1, 2], "两轮聚类产物均可回查", history["historical_runs"])
    old_artifacts = [a for a in store.rows(ARTIFACT_TABLE)
                     if a["config_id"] == cid_bucket and a["run_no"] == 1]
    check(old_artifacts and all(a["rule_version"] == INITIAL_RULE_VERSION
                                for a in old_artifacts),
          "旧分群产物保留生成时的规则版本", old_artifacts)

    print("== 9) 纠正别名后重新提交，核对采用字段与原场景记录 ==")
    before_version = catalog.version
    resubmit = svc_label.resubmit_label_rules(
        labels["SCEN-HIST-01"],
        [{"alias": "age_band", "canonical": "age_band",
          "note": "新目录 age_band 为独立年龄段位字段，按当前目录裁定"}],
        note="维护人员依据SCEN-HIST-01规则原文与数据字典确认",
    )
    check(resubmit["ok"], "纠正后历史标注重新校验生效", resubmit["validation"])
    check(catalog.version != before_version, "别名纠正后规则版本升级",
          (before_version, catalog.version))
    check(resubmit["original_raw_features"] == "age_group,age_band",
          "原场景记录原文完整保留", resubmit["original_raw_features"])
    adopted = set(resubmit["adopted_features"])
    check(adopted == {"age_bucket", "age_band"},
          "实际采用字段：age_group→age_bucket（别名）、age_band→age_band（精确）", adopted)
    res_map = {t["raw"]: t for t in resubmit["token_resolutions"]}
    check(res_map["age_group"]["status"] == VERIFIED_ALIAS
          and res_map["age_band"]["status"] == EXACT,
          "重新提交后两个字段均可核实（别名/精确各保持身份）", resubmit["token_resolutions"])
    check(any("同名歧义证据保留" in ev or "冲突已人工裁定" in ev
              for ev in catalog.alias_record("age_band").collision_evidence),
          "同名歧义的历史证据仍保留在别名登记中",
          catalog.alias_record("age_band").collision_evidence)
    rel = {c["cluster_id"]: c for c in resubmit["related_clusters"]}
    check(cid_bucket in rel and rel[cid_bucket]["label_status"] == "applied",
          "历史标注对 age_bucket 分群转为已适用", rel)
    check(cid_band in rel, "age_band 采用精确字段后与 CLUS-BAND 分群建立关联", rel)
    check(cid_age not in rel, "纠正后仍不会误关联 age 分群", rel)

    print("== 10) 纠正后再次运行分群：待确认人群转为适用，新版本落产物 ==")
    run3_bucket = svc_cluster.execute_cluster_and_generate_segments(cid_bucket)
    check(run3_bucket["run_no"] == 3, "纠正后生成第 3 轮产物", run3_bucket["run_no"])
    check(run3_bucket["pending_segments"] == [],
          "待确认人群已清空", run3_bucket["pending_segments"])
    applied_scenes = {
        a["scene_name"] for art in run3_bucket["segment_labels"] for a in art["labels_applied"]
    }
    check("历史年龄分桶" in applied_scenes and "年龄分桶规则" in applied_scenes,
          "历史规则按纠正后的字段实际生效", applied_scenes)
    check(all(art["rule_version"] == catalog.version for art in run3_bucket["segment_labels"]),
          "新分群产物采用升级后的规则版本", run3_bucket["rule_version"])
    latest = svc_cluster.query_segment_labels(cid_bucket)
    check(latest["rule_version"] == catalog.version
          and all(a["rule_version"] == catalog.version
                  for art in latest["artifacts"]
                  for a in art["labels_applied"]),
          "回查实际标签与当前规则版本一致", latest["rule_version"])
    # 映射表中该标注不再残留 pending 行
    mapping_rows = [r for r in store.rows(MAPPING_TABLE)
                    if r["label_id"] == labels["SCEN-HIST-01"]]
    check(mapping_rows and all(r["label_status"] == "applied" for r in mapping_rows),
          "分群标注映射数据已全部转为 applied", mapping_rows)

    print(f"\n验收结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
