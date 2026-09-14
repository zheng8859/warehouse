"""入库作业页 p3 数据层接线静态断言（inbound-domain tasks 3.1）。

无浏览器、无业务级 /qa —— 对齐 `test_frontend_foundation.py` 的静态断言范式，核对
`inbound.html` 已把硬编码演示值换成后端真实 API 接线：

1. 无硬编码演示单号 `PO-3573743144K55G`；
2. 含内联脚本，引用 `GET /api/jobs?type=INBOUND`（队列）、`GET /api/plan/{plan_id}`（理由）
   与 `setState` 四态渲染；
3. 二次确认逻辑只绑定「批量执行落位」按钮（写台账端点 `POST /api/job/batch/confirm`
   只在确认卡「确认落位」后调用；「批量分配」不弹卡，design.md D7）；
4. `.ord` / `.btab` / `.reason` / `.postbar` 四区仍在。

事实来源：openspec/changes/inbound-domain/tasks.md 3.1 + design.md D5 / D7 / D8。
"""

from pathlib import Path

# backend/tests/frontend/test_frontend_inbound.py
#   -> parents[0] = backend/tests/frontend
#   -> parents[1] = backend/tests
#   -> parents[2] = backend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
INBOUND = (FRONTEND / "inbound.html").read_text(encoding="utf-8")


def test_no_hardcoded_demo_order():
    """p3 不再出现硬编码演示单号（spec「队列由真实接口渲染」）。"""
    assert "PO-3573743144K55G" not in INBOUND


def test_inline_script_wired_to_backend():
    """内联脚本存在，且队列 / 理由 / 四态都接后端（D5 / D8）。"""
    # 内联脚本（IIFE，同 data-import.html 范式）
    assert "(function () {" in INBOUND
    # 队列接 GET /api/jobs?type=INBOUND
    assert "'/jobs?type=INBOUND" in INBOUND
    assert "window.api(buildQuery()" in INBOUND
    # 理由卡下钻接 GET /api/plan/{plan_id}
    assert "window.api('/plan/' + plan.plan_id" in INBOUND
    # 四态渲染走 setState
    assert "setState(queueEl, 'empty'" in INBOUND
    assert "setState(queueEl, 'error'" in INBOUND


def test_confirm_card_only_bound_to_batch_confirm_button():
    """二次确认卡只由「批量执行落位」打开，确认后才调写台账端点（D7）。"""
    assert 'id="batchConfirmBtn"' in INBOUND
    assert "batchConfirmBtn.addEventListener('click'" in INBOUND
    # 打开二次确认卡（remove hidden）恰一次；关闭（add hidden）在取消 / 确认两处。
    assert INBOUND.count("confirmOverlayEl.classList.remove('hidden')") == 1
    # 「确认落位」按钮才触发 doBatchConfirm → /job/batch/confirm。
    assert "confirmOkBtn.addEventListener('click'" in INBOUND
    assert INBOUND.count("window.api('/job/batch/confirm'") == 1
    # 「批量分配」直接调 /allocate/batch、不弹卡（D7 明文）。
    assert INBOUND.count("window.api('/allocate/batch'") == 1


def test_lock_version_refreshed_after_allocate():
    """分配后重读队列刷新 lock_version，避免「批量执行落位」带过期版本被乐观锁 409。

    allocate/batch 把 PENDING→PLANNED 并推进 lock_version；若沿用队列读到时的旧版本提交
    confirm，`confirm._confirm_and_execute` 的乐观锁（bump_lock_version）会抛 StateConflict。
    """
    assert "async function refreshLockVersions" in INBOUND
    # 分配成功后、组装 plans 之前调用重读，且重读不带收窄筛选（status=PENDING 会把已分配单滤掉）。
    assert "await refreshLockVersions()" in INBOUND


def test_four_regions_still_present():
    """`.ord` / `.btab` / `.reason` / `.postbar` 四区仍在。"""
    for cls in ('class="ord', 'class="btab"', 'class="reason', 'class="postbar"'):
        assert cls in INBOUND, f"缺四区之一 {cls}"
    for marker in ('id="queue"', 'id="planBody"', 'id="reasonBox"', 'id="postbar"'):
        assert marker in INBOUND, f"缺区域 id {marker}"
