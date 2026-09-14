"""外部 LLM 统一调用封装（统一外部调用、不做本地部署）。10 §三 / §七。

事实来源：10-AI 辅助能力（冷路径）设计 §三、§七（超时默认 2.0s）
          openspec/changes/ai-assist/design.md D3（`llm_provider` 空 = 不调用）
          spec `ai-assist`「脱敏白名单出站」「LLM 侧失败降级不报错」

provider 抽象：

- `llm_provider == ""` —— 未配置 = 不发起任何外部调用，返回 `provider_unconfigured`。
  这是「数据出域护栏 = 0」的第一道闸门（与 `redact.py` 的正向白名单并列）。
- `"mock"` —— 确定性回显客户端（dev / 测试专用，不外发）。
- 其余取值 —— Phase A 未接线的真实 provider，返回 `llm_unavailable`（不崩溃、不 5xx）。

超时护栏：`llm_request_timeout_s`（默认 2.0s）。阻塞调用放到后台线程执行，`future.result`
带超时；超时返回 `llm_timeout`。真实 provider 接线后，这里的超时语义直接生效，无需改
网关（`capabilities.py`）的编排。

线程说明：`ThreadPoolExecutor` 线程默认非 daemon，故超时后 `shutdown(wait=False)` 立即
返回、不 join 慢线程 —— 否则一次超时会把进程拖到慢线程跑完为止。慢线程是外部网络调用
的代价，随进程退出回收，不影响「单进程 SQLite 单写」的约束（那只管写库并发）。
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass

from app.core.config import Settings
from app.llm import DegradedReason

#: 出站后端：吃掉（已脱敏的）提示词，吐出自然语言。真实 provider 接线点。
Backend = Callable[[str], str]


@dataclass(frozen=True)
class Completion:
    """一次（或未发生的）LLM 调用结果。`text` 与 `degraded_reason` 互斥。

    `ok` 是「真的产出了文本」的判据：`text is not None and degraded_reason is None`。
    """

    text: str | None = None
    degraded_reason: DegradedReason | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None and self.degraded_reason is None


def _mock_backend(prompt: str) -> str:
    """确定性 mock：原样回显成「人话」。同输入同输出，不外发。"""
    return f"[mock] {prompt}"


def _resolve_backend(provider: str) -> Backend | None:
    """provider 名 → 出站后端；未接线 / 未知 → None。"""
    if provider == "mock":
        return _mock_backend
    return None


def complete(
    prompt: str,
    *,
    settings: Settings | None = None,
    timeout_s: float | None = None,
    backend: Backend | None = None,
) -> Completion:
    """统一入口：把（已脱敏的）`prompt` 交给 provider，返回文本或降级原因。

    `backend` 仅用于测试注入（如超时用例注入慢后端）；生产路径由 `llm_provider` 解析。
    """
    settings = settings if settings is not None else Settings()
    timeout = settings.llm_request_timeout_s if timeout_s is None else timeout_s

    if settings.llm_provider == "":
        # 未配置 = 不发起调用。backend 在此**之前**返回，故注入的 backend 不会被求值。
        return Completion(degraded_reason=DegradedReason.PROVIDER_UNCONFIGURED)

    resolved = backend if backend is not None else _resolve_backend(settings.llm_provider)
    if resolved is None:
        return Completion(degraded_reason=DegradedReason.LLM_UNAVAILABLE)

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(resolved, prompt)
        try:
            text = future.result(timeout=timeout)
        except FuturesTimeoutError:
            return Completion(degraded_reason=DegradedReason.LLM_TIMEOUT)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return Completion(text=text)
