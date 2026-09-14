"""脱敏管线：正向白名单出站（冷路径红线 ① 的物理实现）。

事实来源：10-AI 辅助能力（冷路径）设计 §五（脱敏与数据出域）
          openspec/changes/ai-assist/design.md D5（正向白名单，10 字段）
          spec `ai-assist`「脱敏白名单出站」

「数据出域护栏 = 0」的关键不是把敏感字段列一遍黑名单，而是**正向枚举**：出境 JSON
的字段集只可能落在白名单里，名单之外（哪怕是将来新增、看似无害的字段）默认不出境。
黑名单剥离的失败模式是「漏一个字段就出域」；正向白名单的失败模式是「能力缺一个字段」，
后者是功能降级、前者是安全事故，两者的严重度不可比。

两套名单：

- `ALLOWED_OUTBOUND_FIELDS` —— 出境白名单（10 字段）。`redact()` 在顶层按它过滤。
- `FORBIDDEN_OUTBOUND_FIELDS` —— 禁出字段（8 字段）。`redact()` 在**任意层级**剥除它，
  作为第二道防线：白名单键的值（`metrics` / `scores` 这类规则侧算好的对象）是原样
  透传的，禁出字段若被塞进这些嵌套值里，靠顶层白名单拦不住，靠这道深层剥除兜住。

核心链路（评分 / 落位 / 后验 / 台账）不 import 本模块；只有冷路径网关在「调用外部
LLM 之前」调用它，与 `client.py` 的 `llm_provider == ""` 守卫形成两道出站闸门。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 出境白名单（正向枚举）—— 仅这 10 类字段允许出域。D5。顺序即文档 §五 的列法。
ALLOWED_OUTBOUND_FIELDS: frozenset[str] = frozenset(
    {
        "material_code",   # 料号
        "material_name",   # 品名
        "batch_no",        # 批号
        "aisle_no",        # 巷道
        "location_code",   # 库位
        "qty",             # 数量
        "plates",          # 板数
        "metrics",         # 聚合指标（同物料跨巷道均值 / 加权集中度 / 采纳率）
        "scores",          # 6 因子分值
        "cap_cells",       # cap 格数
    }
)

#: 禁出字段（去标识化 + 从来不出）。`order_no` / 操作员姓名 / 操作员备注是 D5 的去
#: 标识化要求；客户名 / 价格 / 供应商 / 配方 / 真实产能是「从来不出」的行业敏感项。
FORBIDDEN_OUTBOUND_FIELDS: frozenset[str] = frozenset(
    {
        "order_no",        # 订单号
        "operator_name",   # 操作员姓名
        "operator_note",   # 操作员备注（v1 自由文本不出境）
        "customer_name",   # 客户名
        "price",           # 价格
        "supplier",        # 供应商
        "recipe",          # 配方
        "true_capacity",   # 真实产能
    }
)


def _strip_forbidden(value: Any) -> Any:
    """递归剥除任意层级的禁出字段；其余原样返回（不可变：返回新对象）。"""
    if isinstance(value, Mapping):
        return {
            key: _strip_forbidden(item)
            for key, item in value.items()
            if key not in FORBIDDEN_OUTBOUND_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_strip_forbidden(item) for item in value]
    return value


def redact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把任意 payload 过滤成「仅含白名单字段」的出境 JSON。

    两道处理，顺序有讲究：

    1. **顶层正向白名单**：只保留 `ALLOWED_OUTBOUND_FIELDS` 里的键。新字段默认不出境。
    2. **深层禁出剥除**：对幸存键的值递归剥除 `FORBIDDEN_OUTBOUND_FIELDS`，防敏感键被
       塞进 `metrics` / `scores` 这类原样透传的嵌套对象里。

    白名单键的值（数字 / 字符串 / 聚合对象）不被改写 —— 它们是规则侧算好的确定性
    数据，脱敏只做「截留不该出的字段」，不做「改写内容」。
    """
    allowed = {key: value for key, value in payload.items() if key in ALLOWED_OUTBOUND_FIELDS}
    return _strip_forbidden(allowed)
