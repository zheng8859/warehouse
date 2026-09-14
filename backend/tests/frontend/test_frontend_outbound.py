"""出库作业页 p4 数据层接线静态断言（outbound-domain 数据层）。

无浏览器、无业务级 /qa —— 对齐 `test_frontend_inbound.py` 的静态断言范式，核对
`outbound.html` 已把硬编码演示值换成后端真实 API 接线：

1. 无硬编码演示单号 `DO-88` / `DO-91` / `DO-95` / `MAT-50001`；
2. 含内联脚本，引用 `GET /api/jobs?type=OUTBOUND`（队列）与
   `POST /api/job/batch/pick-sequence`（顺路取派生）+ `setState` 四态渲染；
3. 二次确认逻辑只绑定「批量确认出库」按钮（写台账端点 `POST /api/job/batch/confirm`
   只在确认卡「确认出库」后调用；「批量生成顺路取」不弹卡）；
4. 出库为只读派生：confirm 体只带 `pick_path` + `lock_version`，不带 source/target。

事实来源：openspec/changes/outbound-domain/tasks.md 数据层 + design.md（出库只读派生）。
"""

from pathlib import Path

# backend/tests/frontend/test_frontend_outbound.py
#   -> parents[0] = backend/tests/frontend
#   -> parents[1] = backend/tests
#   -> parents[2] = backend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
OUTBOUND = (FRONTEND / "outbound.html").read_text(encoding="utf-8")


def test_no_hardcoded_demo_order():
    """p4 不再出现硬编码演示单号（spec「队列由真实接口渲染」）。"""
    for demo in ("DO-88", "DO-91", "DO-95", "MAT-50001"):
        assert demo not in OUTBOUND, f"outbound.html 仍含演示值 {demo}"


def test_inline_script_wired_to_backend():
    """内联脚本存在，队列 / 顺路取 / 四态都接后端（design.md）。"""
    # 内联脚本（IIFE，同 inbound.html 范式）
    assert "(function () {" in OUTBOUND
    # 队列接 GET /api/jobs?type=OUTBOUND
    assert "'/jobs?type=OUTBOUND" in OUTBOUND
    assert "window.api(buildQuery()" in OUTBOUND
    # 顺路取派生接 POST /api/job/batch/pick-sequence
    assert "window.api('/job/batch/pick-sequence'" in OUTBOUND
    # 驳回接 POST /api/job/{id}/reject
    assert "window.api('/job/' + plan.job_order_id + '/reject'" in OUTBOUND
    # 后验接 GET /api/verification/{job_id}
    assert "window.api('/verification/' + jobId" in OUTBOUND
    # 四态渲染走 setState
    assert "setState(queueEl, 'empty'" in OUTBOUND
    assert "setState(queueEl, 'error'" in OUTBOUND


def test_confirm_card_only_bound_to_batch_confirm_button():
    """二次确认卡只由「批量确认出库」打开，确认后才调写台账端点（决策权在人）。"""
    assert 'id="batchConfirmBtn"' in OUTBOUND
    assert "batchConfirmBtn.addEventListener('click'" in OUTBOUND
    # 打开二次确认卡（remove hidden）恰一次；关闭（add hidden）在取消 / 确认两处。
    assert OUTBOUND.count("confirmOverlayEl.classList.remove('hidden')") == 1
    # 「确认出库」按钮才触发 doBatchConfirm → /job/batch/confirm。
    assert "confirmOkBtn.addEventListener('click'" in OUTBOUND
    assert OUTBOUND.count("window.api('/job/batch/confirm'") == 1
    # 「批量生成顺路取」直接调 /job/batch/pick-sequence、不弹卡。
    assert OUTBOUND.count("window.api('/job/batch/pick-sequence'") == 1


def test_lock_version_refreshed_after_pick_sequence():
    """顺路取后重读队列刷新 lock_version，避免「批量确认出库」带过期版本被乐观锁 409。

    pick-sequence 把 PENDING→PLANNED 并推进 lock_version；若沿用队列读到时的旧版本提交
    confirm，`confirm._confirm_and_execute` 的乐观锁（bump_lock_version）会抛 StateConflict。
    """
    assert "async function refreshLockVersions" in OUTBOUND
    assert "await refreshLockVersions()" in OUTBOUND


def test_outbound_is_read_only_derivation():
    """出库只读派生：confirm 体只带 pick_path + lock_version，无 source/target（红线）。"""
    assert "pick_path: p.pick_sequence" in OUTBOUND
    assert "lock_version: p.lock_version" in OUTBOUND
    # 出库无落位：确认体不得出现 source_location_code / target_location_code。
    assert "source_location_code" not in OUTBOUND
    assert "target_location_code" not in OUTBOUND


def test_regions_still_present():
    """`.ord` / `.btab` / `.postbar` 区域仍在（出库无理由下钻区，无 .reason）。"""
    for cls in ('class="ord', 'class="btab"', 'class="postbar"'):
        assert cls in OUTBOUND, f"缺区域 class {cls}"
    for marker in ('id="queue"', 'id="planBody"', 'id="postbar"', 'id="confirmOverlay"'):
        assert marker in OUTBOUND, f"缺区域 id {marker}"
