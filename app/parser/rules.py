"""解析规则的数据结构与加载器。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import settings

FIELD_TYPES = {"text", "int", "money", "phone", "datetime", "address"}


@dataclass
class DetectRule:
    any_contains: list[str] = field(default_factory=list)
    all_contains: list[str] = field(default_factory=list)
    regex: list[str] = field(default_factory=list)


@dataclass
class FieldRule:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    default: str = ""
    labels: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    # 仅在截图 OCR 文本上匹配的正则（截图里的版式和群消息不同，单独配置更安全）
    ocr_patterns: list[str] = field(default_factory=list)
    # 取值后的标准化：strip_spaces —— 去掉所有空白（OCR 常把长数字串断成 "33169016   47870"）
    normalize: str = ""
    # 该字段以订单截图 OCR 的结果为准（截图信息比群消息完整，如商品全称）
    ocr_override: bool = False
    # 金额字段允许写成加法时相加（「130➕80」→ 210）：只对成本这类会拆着写的字段开
    sum_additive: bool = False


@dataclass
class PlatformRule:
    key: str
    label: str
    detect: DetectRule = field(default_factory=DetectRule)
    fields: list[FieldRule] = field(default_factory=list)

    @property
    def required_keys(self) -> list[str]:
        return [f.key for f in self.fields if f.required]


@dataclass
class RuleSet:
    version: int = 1
    default_platform: str = "xianyu"
    platforms: dict[str, PlatformRule] = field(default_factory=dict)
    # 常见人为错误的纠正开关：见解析规则文件里的 typos 段，可单独关某一条
    typos: dict[str, bool] = field(default_factory=dict)

    def typo_on(self, name: str, default: bool = True) -> bool:
        return bool(self.typos.get(name, default))

    def platform(self, key: str | None = None) -> PlatformRule | None:
        return self.platforms.get(key or self.default_platform)

    def platform_keys(self) -> list[str]:
        return list(self.platforms.keys())


def _parse_detect(data: dict[str, Any]) -> DetectRule:
    data = data or {}
    return DetectRule(
        any_contains=[str(x) for x in data.get("any_contains", []) or []],
        all_contains=[str(x) for x in data.get("all_contains", []) or []],
        regex=[str(x) for x in data.get("regex", []) or []],
    )


def _parse_field(data: dict[str, Any]) -> FieldRule:
    ftype = str(data.get("type", "text")).lower()
    if ftype not in FIELD_TYPES:
        ftype = "text"
    return FieldRule(
        key=str(data.get("key")),
        label=str(data.get("label", data.get("key", ""))),
        type=ftype,
        required=bool(data.get("required", False)),
        default=str(data.get("default", "") or ""),
        labels=[str(x) for x in data.get("labels", []) or []],
        patterns=[str(x) for x in data.get("patterns", []) or []],
        ocr_patterns=[str(x) for x in data.get("ocr_patterns", []) or []],
        normalize=str(data.get("normalize", "") or "").strip(),
        ocr_override=bool(data.get("ocr_override", False)),
        sum_additive=bool(data.get("sum", False)),
    )


def parse_rule_dict(data: dict[str, Any]) -> RuleSet:
    platforms: dict[str, PlatformRule] = {}
    for item in (data or {}).get("platforms", []) or []:
        key = str(item.get("key"))
        platforms[key] = PlatformRule(
            key=key,
            label=str(item.get("label", key)),
            detect=_parse_detect(item.get("detect")),
            fields=[_parse_field(f) for f in item.get("fields", []) or [] if f.get("key")],
        )
    return RuleSet(
        version=int((data or {}).get("version", 1)),
        default_platform=str((data or {}).get("default_platform", "xianyu")),
        platforms=platforms,
        typos={
            str(k): bool(v)
            for k, v in ((data or {}).get("typos") or {}).items()
        },
    )


def load_rules(path: Path | None = None) -> RuleSet:
    path = Path(path or settings.rules_path)
    if not path.exists():
        return RuleSet(platforms={})
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return parse_rule_dict(data)


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, RuleSet]] = {}


def get_rules(path: Path | None = None, force: bool = False) -> RuleSet:
    """按文件 mtime 做缓存，规则文件改动后自动生效。"""
    path = Path(path or settings.rules_path)
    key = str(path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not force and cached and cached[0] == mtime:
            return cached[1]
        rules = load_rules(path)
        _CACHE[key] = (mtime, rules)
        return rules


def dump_rules(rules: RuleSet) -> str:
    data: dict[str, Any] = {
        "version": rules.version,
        "default_platform": rules.default_platform,
    }
    if rules.typos:  # 有配过纠错开关就原样写回，页面改规则时不会把它弄丢
        data["typos"] = dict(rules.typos)
    data["platforms"] = []
    for p in rules.platforms.values():
        data["platforms"].append(
            {
                "key": p.key,
                "label": p.label,
                "detect": {
                    "any_contains": p.detect.any_contains,
                    "all_contains": p.detect.all_contains,
                    "regex": p.detect.regex,
                },
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "default": f.default,
                        "labels": f.labels,
                        "patterns": f.patterns,
                        "ocr_patterns": f.ocr_patterns,
                        "normalize": f.normalize,
                        "ocr_override": f.ocr_override,
                        "sum": f.sum_additive,
                    }
                    for f in p.fields
                ],
            }
        )
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
