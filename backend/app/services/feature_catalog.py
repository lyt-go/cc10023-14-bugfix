"""特征字段目录与可验证别名登记。

场景标注的历史原文可能使用旧字段名，而当前字段目录会持续演进。本模块集中维护：

1. 当前字段目录（canonical fields）：关联只承认目录中的精确特征名，
   ``age`` 与 ``age_bucket`` 等同前缀字段各自保持独立身份，不做子串推断；
2. 可验证别名（verified alias）：有明确证据指向某个当前字段的旧名，
   只能按证据解释为该字段，解释过程保留来源证据；
3. 冲突别名（conflict alias）：旧别名与新字段目录中的另一个字段同名，
   存在同名歧义，保留双方证据但不得自动采用，等待维护人员确认纠正。

另外，分隔符（含中文逗号）拆分后留下的空项不属于任何特征，绝不允许
当作"任意特征"的通配符参与关联。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 精确匹配规则版本：标注详情、分群标注映射与聚类分群产物都携带该版本，
# 保证同一次核对看到的是同一套规则。
INITIAL_RULE_VERSION = "field-catalog-2026.11"

# 解析结论取值
EXACT = "exact"                     # 当前字段目录精确名
VERIFIED_ALIAS = "verified_alias"   # 有证据的历史别名，已解释为当前字段
CONFLICT = "conflict"               # 同名歧义，证据冲突，等待人工确认
UNRESOLVED = "unresolved"           # 目录与别名登记中均不存在
EMPTY = "empty"                     # 分隔符留下的空项，不是通配符


@dataclass
class AliasRecord:
    """一条历史字段别名的可验证登记。"""

    alias: str
    canonical: str | None
    status: str                      # verified_alias | conflict
    source: str
    evidence: list[str]
    historical_labels: list[str] = field(default_factory=list)
    catalog_collision: str | None = None
    collision_evidence: list[str] = field(default_factory=list)


@dataclass
class TokenResolution:
    """标注原文中单个字段项的解析结论与证据。"""

    raw: str
    canonical: str | None
    status: str
    evidence: list[str] = field(default_factory=list)
    candidates: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "status": self.status,
            "evidence": list(self.evidence),
            "candidates": [dict(c) for c in self.candidates],
        }


class FeatureCatalog:
    """当前字段目录 + 历史别名登记，承担精确特征名校验与别名解释。"""

    def __init__(self, version: str = INITIAL_RULE_VERSION) -> None:
        self.version = version
        # 当前字段目录：精确特征名
        self.canonical_fields: set[str] = {"age", "age_bucket", "age_band", "city"}
        self._aliases: dict[str, AliasRecord] = {}
        self._bootstrap_historical_aliases()

    def _bootstrap_historical_aliases(self) -> None:
        # 已核实别名：数据字典更名公告可验证，age_group → age_bucket
        self._aliases["age_group"] = AliasRecord(
            alias="age_group",
            canonical="age_bucket",
            status=VERIFIED_ALIAS,
            source="数据字典v2《字段更名公告》",
            evidence=[
                "数据字典v2记载 age_group 于 2026-03 更名为 age_bucket，语义均为年龄分桶",
                "历史标注 SCEN-HIST-01 原文规则与 age_group 字段一一对应，可核实",
            ],
            historical_labels=["SCEN-HIST-01"],
        )
        # 同名歧义别名：历史上 age_band 指 age_bucket，但新目录里 age_band 已是独立精确字段
        self._aliases["age_band"] = AliasRecord(
            alias="age_band",
            canonical="age_bucket",
            status=CONFLICT,
            source="历史标注 SCEN-HIST-01 规则原文",
            evidence=[
                "历史标注 SCEN-HIST-01 中 age_band 与 age_group 并列描述年龄分桶规则，"
                "历史证据指向 age_bucket",
            ],
            historical_labels=["SCEN-HIST-01"],
            catalog_collision="age_band",
            collision_evidence=[
                "当前字段目录含精确字段 age_band（特征编号 FEAT-0103），"
                "同名项不能自动按旧别名解释，需人工确认",
            ],
        )

    # ── 别名登记查询 ──
    def alias_record(self, name: str) -> AliasRecord | None:
        return self._aliases.get(name)

    def list_aliases(self) -> list[AliasRecord]:
        return list(self._aliases.values())

    # ── 精确特征名校验 ──
    def is_canonical(self, name: str) -> bool:
        return name in self.canonical_fields

    def resolve_token(self, raw_name: str, *, label_no: str | None = None) -> TokenResolution:
        """解析标注原文中的单个字段项，只承认精确名与可验证别名。"""
        name = (raw_name or "").strip()
        if name == "":
            return TokenResolution(
                raw=raw_name,
                canonical=None,
                status=EMPTY,
                evidence=["分隔符拆分产生的空项，不代表任何特征，关联时忽略"],
            )

        alias = self._aliases.get(name)
        # 同名歧义：仅对登记在案的历史标注原文生效，新标注按目录精确名处理
        if alias and alias.status == CONFLICT and label_no in alias.historical_labels:
            return TokenResolution(
                raw=name,
                canonical=None,
                status=CONFLICT,
                evidence=list(alias.evidence) + list(alias.collision_evidence),
                candidates=[
                    {"field": alias.catalog_collision or name,
                     "basis": "当前字段目录中的精确同名字段"},
                    {"field": alias.canonical or "",
                     "basis": f"历史别名证据（来源：{alias.source}）"},
                ],
            )

        if name in self.canonical_fields:
            return TokenResolution(
                raw=name, canonical=name, status=EXACT,
                evidence=[f"当前字段目录精确特征名：{name}"],
            )

        if alias is not None:
            if alias.status == VERIFIED_ALIAS:
                return TokenResolution(
                    raw=name,
                    canonical=alias.canonical,
                    status=VERIFIED_ALIAS,
                    evidence=list(alias.evidence) + [
                        f"别名 {name} 按可验证证据解释为当前字段 {alias.canonical}"
                    ],
                )
            # 冲突别名出现在非历史标注中：同样不得自动采用
            return TokenResolution(
                raw=name,
                canonical=None,
                status=CONFLICT,
                evidence=list(alias.evidence) + list(alias.collision_evidence),
                candidates=[
                    {"field": alias.catalog_collision or name,
                     "basis": "当前字段目录中的精确同名字段"},
                    {"field": alias.canonical or "",
                     "basis": f"历史别名证据（来源：{alias.source}）"},
                ],
            )

        return TokenResolution(
            raw=name,
            canonical=None,
            status=UNRESOLVED,
            evidence=[f"字段 {name} 不在当前字段目录或可验证别名登记中"],
        )

    def resolve_features(
        self, raw_features: str, *, label_no: str | None = None
    ) -> list[TokenResolution]:
        """拆分整条关联特征原文并逐项解析，空项保留为证据而不是丢弃。"""
        return [
            self.resolve_token(token, label_no=label_no)
            for token in split_feature_tokens(raw_features, keep_empty=True)
        ]

    # ── 别名纠正：维护人员确认后把冲突别名转为可验证别名 ──
    def correct_alias(
        self, alias_name: str, canonical_name: str, *, label_no: str, note: str
    ) -> AliasRecord:
        alias_name = (alias_name or "").strip()
        canonical_name = (canonical_name or "").strip()
        if alias_name not in self._aliases:
            raise KeyError(f"别名 {alias_name} 未登记，无法纠正")
        if canonical_name not in self.canonical_fields:
            raise ValueError(f"目标字段 {canonical_name} 不在当前字段目录中")
        record = self._aliases[alias_name]
        if record.status != CONFLICT:
            raise ValueError(f"别名 {alias_name} 已是可验证状态，无需纠正")
        # 保留歧义证据，再追加人工确认证据
        record.evidence.append(
            f"维护人员依据标注 {label_no} 确认：{alias_name} 采用当前字段 "
            f"{canonical_name}；说明：{note}。原同名歧义证据保留备查"
        )
        record.canonical = canonical_name
        record.status = VERIFIED_ALIAS
        record.collision_evidence.append(
            f"冲突已人工裁定（{note}），目录同名字段 {record.catalog_collision} "
            "仍保持独立身份，本条登记仅解释历史原文"
        )
        if label_no not in record.historical_labels:
            record.historical_labels.append(label_no)
        # 规则修订后升版本，旧分群产物保留其生成时的版本，便于核对差异
        prefix, _, tail = self.version.rpartition(".")
        self.version = f"{prefix}.{int(tail) + 1}" if tail.isdigit() else f"{self.version}.1"
        return record


def split_feature_tokens(raw_features: Any, *, keep_empty: bool = True) -> list[str]:
    """按中英文逗号拆分关联特征；默认保留空项作为错位证据。"""
    tokens = [part.strip() for part in re.split(r"[,，]", str(raw_features or ""))]
    if keep_empty:
        return tokens
    return [token for token in tokens if token]


def summarize_resolutions(resolutions: list[TokenResolution]) -> dict[str, Any]:
    """汇总一条标注的解析结果，给出可适用/待确认结论。"""
    adopted = [r.canonical for r in resolutions if r.canonical]
    conflicts = [r.to_dict() for r in resolutions if r.status == CONFLICT]
    unresolved = [r.to_dict() for r in resolutions if r.status == UNRESOLVED]
    empties = [r.to_dict() for r in resolutions if r.status == EMPTY]
    if conflicts or unresolved:
        applicability = "pending_confirmation"
    elif adopted:
        applicability = "applicable"
    else:
        applicability = "no_resolved_feature"
    return {
        "adopted_features": sorted(set(adopted)),
        "conflicts": conflicts,
        "unresolved": unresolved,
        "empty_tokens": empties,
        "applicability": applicability,
    }


def build_label_cluster_mapping(
    label: dict[str, Any], cluster_rows: list[dict[str, Any]], catalog: FeatureCatalog
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """为一条场景标注构建分群标注映射（纯函数，不落库）。

    匹配规则：标注解析出的精确当前字段，与聚类配置特征集中的精确字段
    做集合求交；同前缀不同名（age / age_bucket）不相交，空项不参与。
    """
    label_no = label.get("标注编号")
    resolutions = catalog.resolve_features(label.get("关联特征", ""), label_no=label_no)
    summary = summarize_resolutions(resolutions)
    label_features = set(summary["adopted_features"])

    mapping_rows: list[dict[str, Any]] = []
    for cluster in cluster_rows:
        cluster_tokens = split_feature_tokens(cluster.get("特征集", ""), keep_empty=False)
        # 聚类配置代表当前字段目录，只承认其中的精确特征名
        cluster_features = {t for t in cluster_tokens if catalog.is_canonical(t)}
        matched = sorted(label_features & cluster_features)
        if not matched:
            continue
        mapping_rows.append({
            "label_id": label.get("id"),
            "label_no": label_no,
            "scene_name": label.get("场景名称"),
            "cluster_id": cluster.get("id"),
            "config_no": cluster.get("配置编号"),
            "rule_version": catalog.version,
            "label_status": (
                "applied" if summary["applicability"] == "applicable"
                else "pending_confirmation"
            ),
            "matched_features": matched,
            "label_features": sorted(label_features),
            "cluster_features": sorted(cluster_features),
            "token_resolutions": [r.to_dict() for r in resolutions],
            "ambiguous_evidence": summary["conflicts"] + summary["unresolved"],
            "empty_token_evidence": summary["empty_tokens"],
        })
    return mapping_rows, summary


# 进程内单例：字段目录与别名登记的唯一权威来源
catalog = FeatureCatalog()
