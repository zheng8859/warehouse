> 本 change 不落在关键路径（`F1 → F2 → F9 → F3 → …`）的任何一环上，故不按 F 编号排序；
> 它是 `26` 号地基的一处规格口径订正。评测场景编号（`SC-xxx` / `CL-xxx`）属阶段六，
> 本 change 不新增评测，相应规则不适用。

## 1. 规格

- [x] 1.1 在 `specs/auth/spec.md` 写出 `## MODIFIED Requirements`，收窄「登录端点与失败语义」并把「凭据校验的 401 区分原因」写成正向要求；验证：`openspec validate clarify-401-failure-semantics --strict` 通过，且 delta 里 `### Requirement:` 的头与 `openspec/specs/auth/spec.md` 逐字一致
- [x] 1.2 确认收窄后与事实来源对得上：`13` §6.1 第 3 步（只要求返回 401）与 `22` §2.1/§2.3（登录失败固定文案）；验证：两处引用在 delta 的 Requirement 或 design.md 中有据可查，且 `grep -rn "不区分" 产品设计/*.md` 仍为空

## 2. 实现与测试

- [x] 2.1 订正 `backend/app/api/middleware.py` 的 `AuthMiddleware` 类 docstring：删去「不区分原因（避免成为探测面）」，改为写清硬边界「一律 401，不返回 404（不泄露路由是否存在）」；验证：`grep -rn "不区分原因" backend/app/` 为空，且 `python -c` 读回该 docstring 与 `test_smoke.py` 的 401/404 断言不矛盾
- [x] 2.2 先写失败测试再加断言：在 `backend/tests/api/test_auth.py` 新增用例 —— 一份已过期凭据与一份签名被篡改的凭据分别请求受保护端点，两次均 401 且 `message` **不相同**；验证：先证明它会红（临时把 `security.py` 两处 message 改成同一句，该用例必须失败），再改回——这一步是「逻辑测试保留完整代码」的要求，不能跳过
- [x] 2.3 全量 L1 仍绿；验证：`python -m pytest tests -o addopts="--tb=short"` 报 **547 passed**（546 + 新增 1），且 pytest 自报耗时仍在 pre-commit 门禁口径内（阶段二收尾为 4.37~4.44s，余量约 12%）

## 3. 收尾

- [x] 3.1 原子提交，`scope` 必填；验证：`commit-msg` 钩子通过（Conventional Commits 格式），`pre-commit` 的 pytest L1 通过
- [x] 3.2 `/opsx:archive` 归档并把 delta 同步进 `openspec/specs/auth/spec.md`；验证：`openspec validate --specs` 3 passed / 0 failed，且 `openspec show auth --type spec` 能看到新场景「凭据校验的 401 保留失败原因」、旧表述已消失
- [x] 3.3 `git merge --no-ff` 回 `main`；验证：`git log --oneline -1` 显示 merge commit，工作区干净 —— 合并落点 `f0f591c`

> **未打标签**：`CLAUDE.md` §6.1 的标签节奏是「每个阶段一个 `v0.N.0`」，未定义非阶段变更的
> 补丁号。本 change 不改对外行为，故 `v0.2.1` 与否是发布口径问题，由项目所有者定；
> 本次决定不打，`v0.2.0` 仍是 `main` 上最新的标签。
