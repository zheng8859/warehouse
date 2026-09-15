"""外部 LLM 统一调用封装（统一外部调用、不做本地部署）。10 §三 / §七。

事实来源：10-AI 辅助能力（冷路径）设计 §三、§七（超时默认 10.0s，接真模型后上调）
          openspec/changes/ai-assist/design.md D3（`llm_provider` 空 = 不调用）
          spec `ai-assist`「脱敏白名单出站」「LLM 侧失败降级不报错」

provider 抽象：

- `llm_provider == ""` —— 未配置 = 不发起任何外部调用，返回 `provider_unconfigured`。
  这是「数据出域护栏 = 0」的第一道闸门（与 `redact.py` 的正向白名单并列）。
- `"mock"` —— 确定性回显客户端（dev / 测试专用，不外发）。
- `"openai"` / `"openai_compatible"` / `"deepseek"`（大小写不敏感）且 `llm_api_key`
  非空 —— 真实 OpenAI 兼容 `/chat/completions` 后端（通吃 OpenAI / DeepSeek / 通义 /
  月之暗面 / 智谱 / 本地 Ollama·vLLM，靠 `llm_base_url` + `llm_model` 区分）。
  超时 / 网络 / 上游失败都降级。
- 其余取值 / 缺 key —— 返回 `llm_unavailable`（不崩溃、不 5xx）。

超时护栏：`llm_request_timeout_s`（默认 10.0s）。阻塞调用放到后台线程执行，`future.result`
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

import httpx

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


#: 出站 system 指令：只叙事、不改数（红线「LLM 只叙事、不算数」在出站侧的第一道提示）。
_SYSTEM_PROMPT = (
    "你是成品库位智能推荐系统的仓储分析助手。用户会给你一段 JSON 数据，"
    "请用简洁的中文解读其中的事实：只叙事、不修改任何数值、不编造数据、不下唯一断言。"
)

#: OpenAI 兼容 provider 白名单（走同一个 `/chat/completions` 后端，仅 base_url/model 不同）。
#: 匹配时大小写不敏感（`_resolve_backend` 里 `.strip().lower()`）。
_OPENAI_COMPATIBLE_PROVIDERS = {"openai", "openai_compatible", "deepseek"}


def _openai_compatible_backend(settings: Settings, *, system: str | None = None) -> Backend:
    """真实 OpenAI 兼容 `/chat/completions` 后端。已脱敏提示词在此出站。

    `system` 覆盖默认叙事指令（如 L2 NLU 传「意图分类 + 只出 JSON」指令）；缺省用
    `_SYSTEM_PROMPT`。`httpx.post` 用自己的超时（`llm_request_timeout_s`）；`complete()`
    的 `future.result` 超时仍是外层兜底。网络 / 上游 4xx·5xx / 解析失败都降级，不外抛。
    """

    def call(prompt: str) -> str:
        base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base}/chat/completions"
        payload = {
            "model": settings.llm_model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system or _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
        }
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=payload,
            timeout=settings.llm_request_timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    return call


def _resolve_backend(settings: Settings, *, system: str | None = None) -> Backend | None:
    """provider → 出站后端；未接线 / 缺 key → None（`complete` 转成 `llm_unavailable`）。

    `system` 透传给 OpenAI 兼容后端作为出站 system 指令（缺省叙事、L2 NLU 覆盖）。
    """
    provider = settings.llm_provider.strip().lower()
    if provider == "mock":
        return _mock_backend
    if provider in _OPENAI_COMPATIBLE_PROVIDERS:
        if not settings.llm_api_key:
            return None  # provider 配了但没 key → 不带着空 key 出站
        return _openai_compatible_backend(settings, system=system)
    return None


def complete(
    prompt: str,
    *,
    settings: Settings | None = None,
    timeout_s: float | None = None,
    backend: Backend | None = None,
    system: str | None = None,
) -> Completion:
    """统一入口：把（已脱敏的）`prompt` 交给 provider，返回文本或降级原因。

    `backend` 仅用于测试注入（如超时用例注入慢后端）；生产路径由 `llm_provider` 解析。
    `system` 覆盖出站 system 指令（缺省叙事；L2 NLU 传「意图分类」指令）。
    LLM 侧任何失败（超时 / 网络 / 上游错误 / 解析失败）都降级，绝不外抛（D10）。
    """
    settings = settings if settings is not None else Settings()
    timeout = settings.llm_request_timeout_s if timeout_s is None else timeout_s

    if settings.llm_provider == "":
        # 未配置 = 不发起调用。backend 在此**之前**返回，故注入的 backend 不会被求值。
        return Completion(degraded_reason=DegradedReason.PROVIDER_UNCONFIGURED)

    resolved = backend if backend is not None else _resolve_backend(settings, system=system)
    if resolved is None:
        return Completion(degraded_reason=DegradedReason.LLM_UNAVAILABLE)

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(resolved, prompt)
        try:
            text = future.result(timeout=timeout)
        except FuturesTimeoutError:
            return Completion(degraded_reason=DegradedReason.LLM_TIMEOUT)
        except httpx.TimeoutException:
            return Completion(degraded_reason=DegradedReason.LLM_TIMEOUT)
        except Exception:
            # 网络 / 鉴权 / 上游 4xx·5xx / 解析失败 —— 降级「不可用」，不把 LLM 故障报成 5xx。
            return Completion(degraded_reason=DegradedReason.LLM_UNAVAILABLE)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return Completion(text=text)
