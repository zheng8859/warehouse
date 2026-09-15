"""GTJ10036 单厂内置字段映射（**纯函数，无 IO**）。

事实来源：16-数据衔接与 cap 自维护 §4.2（字段映射）与 §10.2（不做可视化配置）
          spec `data-import`「文件解析与字段映射」（Scenario：字段未 100% 命中阻断）
          openspec/changes/data-import/design.md D2 / D8

把源文件的**表头名**映射到内部字段名。容错四处：**别名**（`库位` 也能认到 `库位号`）、
**去首尾空格**、**全半角归一**（`ｌｏｃａｔｉｏｎ` → `location`、`数量（箱）` → `数量(箱)`）、
**大小写不敏感**（`LOCATION` → `location`）。命中率须 100%：任一**必填**列未命中即阻断
（`ensure_complete` 抛 `ValidationBlocked`）；**选填**列（品名）缺失降级不阻断。

首期不做可视化配置（16 §10.2）：别名表是**单厂内置**的常量，改映射要改本文件 ——
这正好让「改错一列」成为一件必须过代码评审的事，而不是某个运维在界面上随手一点。

## 必填 / 选填的口径

- 必填 = 各模版标 **✅** 的强必填列（16 A.1 / A.2 / A.3）。选填列缺失**不阻断**，其中只有
  「品名」在回执里显式标注降级（它是给人看的显示名，可由 `material_code` 从物料主数据派生，
  `JobOrder.material_name` 与 `InventoryItem.material_name` 也都可空，16 A.1 标选填）。
  - INV：仓库号 / 库位号 / 料号 / 批号 / 状态 / 数量 / 库存记录时间（7 列）。
  - PO / DO：单据号码 / 行号 / 仓库号 / 料号 / 数量（5 列）；类型（`order_type`，单据细分）、
    品名、生产日期都是选填。
- 「类型」（`order_type`）是**单据细分，不是路由依据**（16 A.4 #5：文件本身区分单据类型，
  文件内的「类型」字段不作为路由依据）。故 `JobOrder.job_type` 由 `FileType` 派生
  （PO→INBOUND、DO→OUTBOUND），**不读源文件的「类型」列** —— 该列只被识别为合法模版列
  （不落 `unrecognized`），不参与分流。
- 「生产日期」（`production_date`）选填：16 A.2/A.3 标选填，缺失不阻断。批号的「生产日期」
  取**入库单建立当天**（D11 现场日期，`generate_batch_no(now)`），不读本列 —— 本列只是
  源数据里保留的参考信息（如 FIFO）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.core.enums import FileType
from app.core.errors import ValidationBlocked

__all__ = [
    "FIELD_ALIASES",
    "REQUIRED_BY_TYPE",
    "MappingResult",
    "build_mapping",
    "ensure_complete",
    "normalize_header",
]

#: 内部字段 → 可接受的表头别名集。别名按**原文**写（半角、中文），匹配时两侧都经
#: `normalize_header` 归一，故这里不必预先把大小写/全半角铺平。
FIELD_ALIASES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "warehouse_no": frozenset({"仓库号", "仓库", "仓库编号", "warehouse_no", "warehouse", "wh"}),
        "location_code": frozenset({"库位号", "库位", "库位编码", "库位代码", "location_code", "location", "loc"}),
        "material_code": frozenset({"料号", "物料号", "物料编码", "物料编号", "material_code", "material", "sku"}),
        "material_name": frozenset({"品名", "物料名称", "品名规格", "material_name", "name"}),
        "batch_no": frozenset({"批号", "批次号", "batch_no", "batch"}),
        "item_status": frozenset({"状态", "库存状态", "品质状态", "item_status", "status"}),
        "qty": frozenset({"数量", "库存数量", "qty", "quantity"}),
        "snapshot_time": frozenset({"库存记录时间", "库存时间", "记录时间", "snapshot_time"}),
        "order_no": frozenset({"单据号码", "单据号", "单号", "订单号", "order_no", "order"}),
        "line_no": frozenset({"行号", "行项目", "line_no", "line"}),
        "order_type": frozenset({"类型", "单据类型", "order_type"}),
        "production_date": frozenset({"生产日期", "生产日", "production_date"}),
    }
)

#: 各文件类型的必填列（品名之外的模版列）。品名是三类共有的选填列，不进这张表。
REQUIRED_BY_TYPE: Final[Mapping[FileType, frozenset[str]]] = MappingProxyType(
    {
        FileType.INV: frozenset(
            {"warehouse_no", "location_code", "material_code", "batch_no", "item_status", "qty", "snapshot_time"}
        ),
        FileType.PO: frozenset(
            {"order_no", "line_no", "warehouse_no", "material_code", "qty"}
        ),
        FileType.DO: frozenset(
            {"order_no", "line_no", "warehouse_no", "material_code", "qty"}
        ),
    }
)


def _fullwidth_to_halfwidth(text: str) -> str:
    """全角 → 半角：U+FF01~U+FF5E 减 0xFEE0，全角空格 U+3000 转半角空格。

    中文字符（U+4E00 起）不在这个块里，原样保留 —— 「全半角归一」只影响全角拉丁 /
    数字 / 标点，这正是导入文件里会与半角模版对不上的那一类。
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize_header(name: object) -> str:
    """表头归一：去首尾空格 → 全角转半角 → casefold。空表头归一成 `""`。"""
    if name is None:
        return ""
    return _fullwidth_to_halfwidth(str(name).strip()).casefold()


#: 归一化别名 → 内部字段（模块加载时构建一次）。加载时校验别名无跨字段冲突 ——
#: 一个别名同时命中两个字段说明别名表写错了，宁可 import 时报错，也不让它在
#: 生产上「静默映射到后一个字段」。
_normalized_alias_to_field: dict[str, str] = {}
for _field, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _norm = normalize_header(_alias)
        _prior = _normalized_alias_to_field.setdefault(_norm, _field)
        if _prior != _field:
            raise ValueError(f"字段别名冲突：{_alias!r} 同时映射到 {_prior} 与 {_field}")
_NORMALIZED_ALIAS_TO_FIELD: Final[Mapping[str, str]] = MappingProxyType(_normalized_alias_to_field)


@dataclass(frozen=True)
class MappingResult:
    """一次映射的结果。

    - `field_to_source`：内部字段 → 原始表头（下游用它从行里取该字段的值）。
      表头重复时**先到先得**（`setdefault`），不改判。
    - `missing_required`：未命中的必填字段（内部名），非空即「未 100% 命中」。
    - `unrecognized`：匹配不到任何字段的原始表头，落回执供人工核对（不阻断）。
    """

    field_to_source: Mapping[str, str]
    missing_required: frozenset[str]
    unrecognized: frozenset[str]

    @property
    def is_complete(self) -> bool:
        """必填列全部命中。选填列（品名）缺失不影响此值。"""
        return not self.missing_required


def build_mapping(headers: Iterable[object], file_type: FileType) -> MappingResult:
    """把源表头映射到内部字段，报告必填命中缺口。纯计算，不抛异常。"""
    required = REQUIRED_BY_TYPE[file_type]

    field_to_source: dict[str, str] = {}
    unrecognized: set[str] = set()

    for header in headers:
        original = str(header).strip() if header is not None else ""
        if original == "":
            continue
        field = _NORMALIZED_ALIAS_TO_FIELD.get(normalize_header(header))
        if field is None:
            unrecognized.add(original)
            continue
        field_to_source.setdefault(field, original)

    mapped = frozenset(field_to_source)
    return MappingResult(
        field_to_source=MappingProxyType(field_to_source),
        missing_required=required - mapped,
        unrecognized=frozenset(unrecognized),
    )


def ensure_complete(result: MappingResult, *, file_type: FileType) -> None:
    """必填列未 100% 命中即阻断（16 §二：字段命中率须 100%，未命中即阻断）。

    报错信息与 spec Scenario 逐字对齐：`字段命中 n/m` + 列出缺失列。选填列缺失不走到这
    里（它不在 `missing_required` 里，降级由校验层在回执标注）。
    """
    if not result.missing_required:
        return
    total = len(REQUIRED_BY_TYPE[file_type])
    hit = total - len(result.missing_required)
    raise ValidationBlocked(
        f"字段命中 {hit}/{total}，缺失必填列：{sorted(result.missing_required)}",
        detail={"hit": hit, "total": total, "missing": sorted(result.missing_required)},
    )
