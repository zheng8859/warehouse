"""移库作业页 p5 数据层接线静态断言（relocate-domain 数据层）。

无浏览器、无业务级 /qa —— 对齐 `test_frontend_inbound.py` 的静态断言范式，核对
`transfer.html` 已把硬编码演示值换成后端真实 API 接线：

1. 无硬编码演示批次 `GJP2571305` / `GJP2570888`；
2. 含内联脚本，引用 `GET /api/jobs?type=RELOCATE`（队列，搜索框按 `material_code`
   精确匹配物料号）与 `POST /api/job/batch/relocate-plan`（收拢方案）+ `setState` 四态；
3. 二次确认逻辑只绑定「批量执行移库」按钮（写台账端点 `POST /api/job/batch/confirm`
   只在确认卡「确认移库」后调用；「批量生成方案」不弹卡）；
4. 移库 confirm 体带 source_locations（逐格真实源库位）+ target_location_code（目标库位），
   由方案里服务端解析的 source_locations / target_location 直传（非前端编造），且批号不变。

事实来源：openspec/changes/relocate-domain/tasks.md 数据层 + design.md（移库不改批号）。
"""

from pathlib import Path

# backend/tests/frontend/test_frontend_transfer.py
#   -> parents[0] = backend/tests/frontend
#   -> parents[1] = backend/tests
#   -> parents[2] = backend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TRANSFER = (FRONTEND / "transfer.html").read_text(encoding="utf-8")


def test_no_hardcoded_demo_batch():
    """p5 不再出现硬编码演示批次（spec「队列由真实接口渲染」）。"""
    for demo in ("GJP2571305", "GJP2570888"):
        assert demo not in TRANSFER, f"transfer.html 仍含演示值 {demo}"


def test_inline_script_wired_to_backend():
    """内联脚本存在，队列 / 收拢方案 / 四态都接后端（design.md）。"""
    # 内联脚本（IIFE，同 inbound.html 范式）
    assert "(function () {" in TRANSFER
    # 队列接 GET /api/jobs?type=RELOCATE
    assert "'/jobs?type=RELOCATE" in TRANSFER
    assert "window.api(buildQuery()" in TRANSFER
    # 收拢方案接 POST /api/job/batch/relocate-plan
    assert "window.api('/job/batch/relocate-plan'" in TRANSFER
    # 驳回接 POST /api/job/{id}/reject
    assert "window.api('/job/' + plan.job_order_id + '/reject'" in TRANSFER
    # 后验接 GET /api/verification/{job_id}
    assert "window.api('/verification/' + jobId" in TRANSFER
    # 四态渲染走 setState
    assert "setState(queueEl, 'empty'" in TRANSFER
    assert "setState(queueEl, 'error'" in TRANSFER


def test_confirm_card_only_bound_to_batch_confirm_button():
    """二次确认卡只由「批量执行移库」打开，确认后才调写台账端点（决策权在人）。"""
    assert 'id="batchConfirmBtn"' in TRANSFER
    assert "batchConfirmBtn.addEventListener('click'" in TRANSFER
    # 打开二次确认卡（remove hidden）恰一次；关闭（add hidden）在取消 / 确认两处。
    assert TRANSFER.count("confirmOverlayEl.classList.remove('hidden')") == 1
    # 「确认移库」按钮才触发 doBatchConfirm → /job/batch/confirm。
    assert "confirmOkBtn.addEventListener('click'" in TRANSFER
    assert TRANSFER.count("window.api('/job/batch/confirm'") == 1
    # 「批量生成方案」直接调 /job/batch/relocate-plan、不弹卡。
    assert TRANSFER.count("window.api('/job/batch/relocate-plan'") == 1


def test_lock_version_refreshed_after_relocate_plan():
    """收拢方案后重读队列刷新 lock_version，避免「批量执行移库」带过期版本被乐观锁 409。

    relocate-plan 把 PENDING→PLANNED 并推进 lock_version；若沿用队列读到时的旧版本提交
    confirm，`confirm._confirm_and_execute` 的乐观锁（bump_lock_version）会抛 StateConflict。
    """
    assert "async function refreshLockVersions" in TRANSFER
    assert "await refreshLockVersions()" in TRANSFER


def test_relocate_confirm_carries_source_and_target():
    """移库 confirm 体带逐格源库位 + 目标库位（_require_locations 移库两者必填）。

    源 = 方案里服务端解析的 `source_locations`（逐格真实库位，非前端编造「巷道 + 固定后缀」）、
    目标 = `target_location`（目标巷道内该物料既有库位）。移库不改批号（CLAUDE.md §四）。
    """
    assert "source_locations" in TRANSFER
    assert "target_location" in TRANSFER
    assert "target_location_code" in TRANSFER
    assert "target_aisle" in TRANSFER
    # 移库 confirm 体不含单一 source_location_code（散板跨多格，改逐格清单），
    # 也不含 pick_path（那是出库的顺路取字段，两者不混用）。
    assert "source_location_code" not in TRANSFER
    assert "pick_path" not in TRANSFER


def test_regions_still_present():
    """`.ord` / `.btab` / `.postbar` 区域仍在（偏离清单 dev-strip 已删，见下一条）。"""
    for cls in ('class="ord', 'class="btab"', 'class="postbar"'):
        assert cls in TRANSFER, f"缺区域 class {cls}"
    for marker in ('id="queue"', 'id="planBody"', 'id="postbar"', 'id="confirmOverlay"'):
        assert marker in TRANSFER, f"缺区域 id {marker}"


def test_deviation_strip_removed():
    """偏离清单 dev-strip 已删除（移库作业不再展示偏离信息，Problem 2）。"""
    assert 'id="deviationStrip"' not in TRANSFER
    assert "window.api('/deviation" not in TRANSFER


def test_search_filters_by_material_code():
    """搜索框搜物料号：buildQuery 拼 `&material_code=`（精确匹配），非单号 order_no 前缀。"""
    assert "&material_code=" in TRANSFER
    assert "&order_no=" not in TRANSFER
