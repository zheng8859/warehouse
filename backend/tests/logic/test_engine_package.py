"""包入口 `app.engine` 的两类断言：公共面（1.2）与引擎纯净度（1.4）。

事实来源：`design.md` D1（模块间的单向依赖图）、D2（全序 / 量化比较 / **时钟注入**）、
          D11（批次号按序号排）、D14（默认值的「落地位置」列）
          `14` §四（六因子固定集，**不得增删**）、§3.2（**N = 3 天**，2026-09-12 业务方确认）、
          §3.4（分配器收 `now`）
          `CLAUDE.md` §四（同样输入必得同样输出 · 核心链路不出域 · 不用随机搜索）
          `17` §10.1（六因子的列举顺序）
          `tasks.md` 1.2（再导出七个模块的公共入口 + 默认值集中成具名常量）、
          1.4（注入式时钟 + 对 `app/engine/` 的静态断言）

## 为什么这两条合成一个文件

两条断言的主语都不是某个模块的算术，而是**这个包本身**：1.2 问「包外能拿到什么」、
1.4 问「包内不许出现什么」。它们与七个模块各自的用例（`test_factors.py` 等）是两种粒度 ——
塞进任何一个模块的文件，都会让那个文件的主语变模糊。

## 静态断言为什么先去掉注释与字符串

`scoring.py` 的 docstring 里就写着「引擎内部不调 `datetime.now()`」这一句 —— 对源文件做
裸文本搜索会**因为这句声明本身**而失败。故先用 `tokenize` 丢掉 COMMENT / STRING 两类记号，
再对剩下的**代码文本**做那三条搜索：量的是代码而不是散文，而散文里恰恰要能自由地写这些词
（「为什么禁」只能写在注释里）。

## 1.4 的另一半在 `test_allocator.py`

「传两个不同的 `now` 断言释放判定翻转」验的是**链路上**的注入（同一批、同一个单，钟点一换
结果就变），故它与分配器主循环的其他用例放在一起；本文件只收「引擎的代码里不许取钟」这条
静态面。两半缺一不可：只断静态面，一个把 `now` 收下却从不使用的分配器照样通过。
"""
from __future__ import annotations

import io
import pathlib
import re
import tokenize

import pytest

import app.engine as engine
from app.engine import (
    allocator,
    degradation,
    factors,
    priority,
    reasons,
    reserved,
    scoring,
)
from app.models.configuration import WEIGHT_FACTORS

pytestmark = pytest.mark.logic

#: 本包自己的目录 —— 静态断言的作用域（`__file__` 而不是拼路径：测试从哪个 cwd 跑都成立）。
ENGINE_DIR = pathlib.Path(engine.__file__).parent

#: 七个模块（本包的公共面由它们的 `__all__` 并集构成）。
MODULES = (factors, priority, scoring, allocator, reserved, degradation, reasons)

#: 六因子，按 `17` §10.1 的列举顺序**写死**在测试里（内容由用例守，不与被测方共用一份）。
EXPECTED_FACTORS = ("abc", "cap", "existing", "station", "batch", "continuity")

#: 引擎代码里不许出现的四处（`tasks.md` 1.4 的三个模式 + 同一族的 `utcnow`）。
FORBIDDEN_CODE = ("datetime.now(", "datetime.utcnow(", "random.", "uuid")


def _code_text(path: pathlib.Path) -> str:
    """模块的**代码文本**：丢掉注释与字符串字面量，其余原样拼接。

    用 `io.open(newline="")` 读：本仓要求 LF，若某个源文件混进 CRLF，记号文本会带上 `\\r`，
    断言会以「搜到一个看不见的字符」的形式莫名其妙地通过或失败。
    """
    pieces: list[str] = []
    with io.open(path, encoding="utf-8", newline="") as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(token.string)
    return "".join(pieces)


# --- 1.2 公共面 ---------------------------------------------------------------------------


def test_the_facade_reexports_every_module_public_name() -> None:
    """`__all__` 是七个模块 `__all__` 的并集 + `WEIGHT_FACTORS`，**逐项相等**。

    用「相等」而不是「包含」：漏掉一个名字的表现是 `ImportError`（看得见），而**多**一个
    不该公开的名字（某个模块的内部 helper）不会有任何东西报错 —— 包的公共面一旦混进这些，
    它们就成了对外承诺的一部分。重名也要挡：同名的两次再导出会让后一个静默胜出。
    """
    union = {name for module in MODULES for name in module.__all__}
    names = list(engine.__all__)

    assert len(names) == len(set(names))  # 无重名
    assert set(names) == union | {"WEIGHT_FACTORS"}
    for name in names:
        assert hasattr(engine, name), name


def test_the_six_factor_names_come_from_the_model_layer_without_a_second_copy() -> None:
    """六因子的**内容**与**来源**各断一条。

    内容：与写死在用例里的六个名字逐项相等 —— 挡的是「因子集被改过而没人发现」：多一个、
    少一个、改名之后，评分按一套键集算、契约校验按另一套，两边**看起来都正常**。
    来源：`is` 同一个对象 —— 值相等的一份副本就是第二个定义点，而 `14` §四 钉死了因子集
    不增删，正是最容易被「顺手同步一下」的地方（同步一次就有第二次）。
    """
    assert engine.WEIGHT_FACTORS == EXPECTED_FACTORS
    assert engine.WEIGHT_FACTORS is WEIGHT_FACTORS


def test_the_four_d14_defaults_are_reachable_from_the_package() -> None:
    """D14 的四项都能从包入口拿到，且**就是**各自定义点的那个对象（不是复制品）。

    1.2 要的是「集中成具名常量」：集中的是**可见性**，不复制值。复制一份的表现是 D14
    「到齐后只改一处」这句话失效 —— 改了 `priority.py` 的常量，包入口那份仍是旧值，而
    没有任何东西会报错。

    末两条断的是**取值**：N = 3 天（`14` §3.2，业务方 2026-09-12 确认）、权重等权。取值随
    业务确认而变，改它们的人必须一并改这里 —— 这条断言的作用正是让「悄悄换一个业务口径」
    必须留下一处红。

    **标量上的 `is` 是结构性无效的**（`None is None` / `3 is 3` 恒真，一份值相等的副本照样
    通过），故 N 的「单一定义点」另断一条：包入口只**绑定**它，不得再赋值一次 —— 否则
    `priority.py` 改了、入口那份没改，而两边都看着正常。函数对象那两行不受此限（`is` 为真
    即同源）。
    """
    assert engine.DEFAULT_OUTBOUND_WINDOW_DAYS is priority.DEFAULT_OUTBOUND_WINDOW_DAYS
    assert engine.DEFAULT_PRIORITY_WEIGHTS is priority.DEFAULT_PRIORITY_WEIGHTS
    assert engine.to_occupied_cells is allocator.to_occupied_cells
    assert engine.resolve_tiers is degradation.resolve_tiers

    assert engine.DEFAULT_OUTBOUND_WINDOW_DAYS == 3
    assert engine.DEFAULT_PRIORITY_WEIGHTS == (1 / 3, 1 / 3, 1 / 3)

    facade = io.open(ENGINE_DIR / "__init__.py", encoding="utf-8", newline="").read()
    assert not re.search(r"^DEFAULT_OUTBOUND_WINDOW_DAYS\s*=", facade, re.M)


def test_the_d14_todo_block_lists_exactly_the_three_remaining_items() -> None:
    """`engine/__init__.py` 的 `TODO(design.md D14 …)` 恰好三条，且都点名表格行。

    「集中可读处」的价值全在**全量**：少一条时，那一项退回「散在某个模块的注释里」，
    而没有人会发现 `grep TODO` 少了一项；多一条则说明包入口又开了一处欠账登记。

    **第 1 行（N）不在清单里，是它已确认的结果**（2026-09-12，3 天）：欠账从四条减为三条 ——
    D14 的表格五行里，第 4/5 行共用落点（故原本四项），减去已确认的那一项即三条。这条断言
    因此同时也是「已确认的项不得再挂 TODO」的守卫：把第 1 行加回去会红。
    """
    source = io.open(ENGINE_DIR / "__init__.py", encoding="utf-8", newline="").read()
    markers = re.findall(r"TODO\(design\.md D14 表格第 (\S+) 行\)", source)

    # 仍待确认的三项各自指向一行；第 4/5 行共用落点。
    assert markers == ["2", "3", "4/5"]


# --- 1.4 引擎纯净度 -----------------------------------------------------------------------


def test_no_engine_module_reaches_for_a_clock_random_or_uuid() -> None:
    """`app/engine/` 下的**代码**里不出现 `datetime.now(` / `datetime.utcnow(` / `random.` / `uuid`。

    四条各自对应一条红线或一条设计决策：前两条 = D2 的**时钟注入**（引擎自己不取钟，否则
    「同样输入必得同样输出」不成立，且同一批里的单据会各按各的钟点判预留池释放了没有）；
    `random.` = `CLAUDE.md` §四「不用随机搜索」；`uuid` = 引擎不生成随机标识 ——
    `bulk_batch_no` 由 `reasons.next_bulk_batch_no` 按**序号**排（D11），随机或时间戳型
    标识会让同一批重跑得到不同的批次号，而重跑同一批正是本阶段的常规操作。

    **先断言扫到了文件**：路径写错时下面几条搜索会一条都搜不到，而那种失败是静默的
    （七个模块 + 本包的 `__init__.py` = 8 个文件）。
    """
    scanned = 0
    for path in sorted(ENGINE_DIR.glob("*.py")):
        scanned += 1
        code = _code_text(path)
        for forbidden in FORBIDDEN_CODE:
            assert forbidden not in code, f"{path.name} 的代码里出现了 {forbidden}"

    assert scanned == len(MODULES) + 1
