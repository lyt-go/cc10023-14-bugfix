"""业务场景标注领域服务：数据分析场景的业务标注、规则表达与知识管理。

链路6（聚类配置 → 分群 → 场景标注）的关联入口在本模块维护。历史上关联使用
子串匹配（``f in 特征集字符串``），导致两个同源缺陷：

* ``age`` 是 ``age_bucket`` 的子串，只含 ``age_bucket`` 的分群被错误关联；
* 标注尾部逗号拆分出的空串 ``""`` 是任意字符串的子串，空项被当成通配符，
  使更多分群错位。

现统一改为：先经字段目录（:mod:`app.services.feature_catalog`）把标注原文解析
为当前精确特征名（历史原文只允许按可验证别名解释，同名歧义保留证据待确认），
再与聚类配置的精确特征集做集合求交，结果落库为分群标注映射数据。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.feature_catalog import (
    catalog,
    split_feature_tokens,
    summarize_resolutions,
    build_label_cluster_mapping,
)
from app.store import store

MAPPING_TABLE = "segment_label_mapping"
ARTIFACT_TABLE = "segment_label_artifacts"


class ScenarioLabelService(BaseModuleService):
    MODULE = "scenario_label"
    ENTITY = "场景标注"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["标注编号", "场景名称", "关联特征", "业务规则", "标注时间", "标注人", "标注状态", "场景版本"]
    REQUIRED_FIELDS = ["标注编号", "场景名称", "关联特征"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ── 链路6: 聚类配置 → 分群 → 场景标注 ──
    def validate_label_rules(self, label_id: int) -> dict[str, Any]:
        """校验场景标注的业务规则合法性。

        以字段目录为权威：关联特征原文逐项解析为当前精确特征名，空项、
        同名歧义和无法核实的旧名都作为证据返回，不再做子串猜测。
        """
        label = store.find(self.MODULE, label_id)
        if label is None:
            return {"ok": False}

        label_no = label.get("标注编号")
        resolutions = catalog.resolve_features(label.get("关联特征", ""), label_no=label_no)
        summary = summarize_resolutions(resolutions)
        adopted = summary["adopted_features"]

        # 与特征定义表交叉核对：已解析出的当前字段必须能在特征定义中核实
        define_names = {r.get("特征名称", "") for r in store.rows("feature_define")}
        verifiable = sorted(set(adopted) & define_names) if define_names else list(adopted)
        matched = verifiable or list(adopted)

        result: dict[str, Any] = {
            "label_id": label_id,
            "rule_version": catalog.version,
            "matched_features": len(matched),
            "adopted_features": adopted,
            "token_resolutions": [r.to_dict() for r in resolutions],
            "empty_tokens": summary["empty_tokens"],
            "conflicts": summary["conflicts"],
            "unresolved": summary["unresolved"],
            "applicability": summary["applicability"],
        }

        if not adopted:
            label["status"] = "待校验"
            label["abnormal"] = True
            result.update({"ok": False,
                           "reason": "关联特征均无法按当前字段目录或可验证别名核实"})
            return result

        # 存在同名歧义或无法核实项时，规则不能直接生效
        if summary["applicability"] != "applicable":
            label["status"] = "待校验"
            label["abnormal"] = True
            result.update({"ok": False,
                           "reason": "存在同名歧义或无法核实的历史字段，等待别名确认"})
            return result

        label["status"] = "已生效"
        label["abnormal"] = False
        result["ok"] = True
        return result

    def link_to_cluster_segments(self, label_id: int) -> dict[str, Any]:
        """将场景标注与聚类分群精确关联，生成并落库分群标注映射数据。

        仅当标注解析出的当前精确字段与聚类配置的精确特征集有真实交集时
        才建立映射；同前缀不同名字段（age / age_bucket / age_band）各自
        保持原身份，分隔符空项不参与匹配。
        """
        label = store.find(self.MODULE, label_id)
        if label is None:
            return {"ok": False}

        cluster_rows = store.rows("cluster_config")
        mapping_rows, summary = build_label_cluster_mapping(label, cluster_rows, catalog)

        # 用最新规则版本覆盖该标注的旧映射，避免重复重跑产生陈旧行
        table = store.rows(MAPPING_TABLE)
        table[:] = [row for row in table if row.get("label_id") != label_id]
        table.extend(mapping_rows)

        related = [
            {
                "cluster_id": row["cluster_id"],
                "config_no": row["config_no"],
                "matched_features": row["matched_features"],
                "label_status": row["label_status"],
                "rule_version": row["rule_version"],
            }
            for row in mapping_rows
        ]
        return {
            "ok": True,
            "label_id": label_id,
            "rule_version": catalog.version,
            "related_clusters": related,
            "count": len(related),
            "adopted_features": summary["adopted_features"],
            "applicability": summary["applicability"],
            "ambiguous_evidence": summary["conflicts"] + summary["unresolved"],
            "empty_token_evidence": summary["empty_tokens"],
            "mapping": mapping_rows,
        }

    def _rebuild_all_mappings(self) -> None:
        """别名纠正导致规则版本变更后，按新版本重建全部分群标注映射。

        历史聚类产物不回填、不修改，仍保留其生成时的规则版本；本方法只
        重建映射表，保证此后标注详情、映射与新聚类产物对齐同一规则版本。
        """
        table = store.rows(MAPPING_TABLE)
        table.clear()
        cluster_rows = store.rows("cluster_config")
        for label in store.rows(self.MODULE):
            mapping_rows, _ = build_label_cluster_mapping(label, cluster_rows, catalog)
            table.extend(mapping_rows)

    def resubmit_label_rules(
        self, label_id: int, alias_corrections: list[dict[str, str]], *, note: str = ""
    ) -> dict[str, Any]:
        """纠正别名后重新提交标注，重建映射并核对实际采用的字段与原文。

        ``alias_corrections`` 形如
        ``[{"alias": "age_band", "canonical": "age_band"}, ...]``，
        每项必须指向当前字段目录中的精确字段；纠正过程保留同名歧义的历史证据。
        """
        label = store.find(self.MODULE, label_id)
        if label is None:
            return {"ok": False}

        corrections_applied: list[dict[str, str]] = []
        for item in alias_corrections or []:
            record = catalog.correct_alias(
                item["alias"], item["canonical"],
                label_no=str(label.get("标注编号")),
                note=note or item.get("note", "维护人员确认"),
            )
            corrections_applied.append({
                "alias": record.alias,
                "canonical": record.canonical or "",
                "status": record.status,
            })

        validation = self.validate_label_rules(label_id)
        # 规则版本可能因别名纠正而升级：按新版本重建全量映射，避免其他标注
        # 残留旧版本映射；历史聚类产物保持其生成时版本不变。
        self._rebuild_all_mappings()
        linking = self.link_to_cluster_segments(label_id)

        raw_tokens = split_feature_tokens(label.get("关联特征", ""), keep_empty=True)
        resolutions = catalog.resolve_features(
            label.get("关联特征", ""), label_no=label.get("标注编号"))
        return {
            "ok": validation["ok"],
            "label_id": label_id,
            "rule_version": catalog.version,
            "original_raw_features": label.get("关联特征", ""),
            "original_raw_tokens": raw_tokens,
            "corrections": corrections_applied,
            "adopted_features": [r.canonical for r in resolutions if r.canonical],
            "token_resolutions": [r.to_dict() for r in resolutions],
            "validation": validation,
            "related_clusters": linking["related_clusters"],
            "evidence": {
                "ambiguous": linking["ambiguous_evidence"],
                "empty_tokens": linking["empty_token_evidence"],
            },
        }

    def get_segment_label_mapping(self, label_id: int | None = None) -> dict[str, Any]:
        """读取分群标注映射数据，供维护人员识别当前适用范围与待确认人群。"""
        rows = store.rows(MAPPING_TABLE)
        if label_id is not None:
            rows = [row for row in rows if row.get("label_id") == label_id]
        applied = [row for row in rows if row.get("label_status") == "applied"]
        pending = [row for row in rows if row.get("label_status") == "pending_confirmation"]
        return {
            "ok": True,
            "rule_version": catalog.version,
            "total": len(rows),
            "applied": applied,
            "pending_confirmation": pending,
        }

    def export_label_knowledge_base(self) -> dict[str, Any]:
        """导出全量场景标注知识库，按业务主题聚合后供给外部系统查询。"""
        rows = store.rows(self.MODULE)
        by_status = {}
        for r in rows:
            st = r.get("status", "unknown")
            if st not in by_status:
                by_status[st] = []
            by_status[st].append({
                "id": r.get("id"),
                "scene": r.get("场景名称"),
                "features": r.get("关联特征"),
                "rules": r.get("业务规则"),
                "annotator": r.get("标注人"),
            })
        return {"ok": True, "total_labels": len(rows), "by_status": by_status}

    def standardize_label_format(self, label_id: int) -> dict[str, Any]:
        """标准化场景标注的格式，确保规则表达符合统一的标注规范。"""
        label = store.find(self.MODULE, label_id)
        if label is None:
            return {"ok": False}
        raw_rules = label.get("业务规则", "")
        standardized = raw_rules.replace(" ", "_").replace("，", ",").lower()
        label["业务规则"] = standardized
        return {"ok": True, "label_id": label_id, "raw": raw_rules, "standardized": standardized}
