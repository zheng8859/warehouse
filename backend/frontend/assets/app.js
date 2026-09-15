/* ============================================================
 * 成品库位智能推荐 · 共享脚本
 * 交互钩子来源：24 号 §3.4
 * ============================================================ */

// 当前页标识（每个 HTML 通过 body[data-page] 声明）
const PAGE = document.body.dataset.page;

// 导航 active 高亮：根据 body[data-page] 自动给对应 nav a 加 active
document.querySelectorAll('nav a[data-tab]').forEach(a => {
  if (a.dataset.tab === PAGE) a.classList.add('active');
});

// 角色菜单可见性矩阵（13 号 §3.1：仓管员 7 / 计划员 4 / 主管 6 / 管理员 8）
// 角色取值 = 13 号 §一 枚举，不自行造名
const MENU = {
  warehouse_keeper: ['p1','p2','p3','p4','p5','p6','p8'],
  planner:         ['p1','p2','p6','p8'],
  supervisor:      ['p1','p4','p5','p6','p7','p8'],
  admin:           ['p1','p2','p3','p4','p5','p6','p7','p8']
};

// 当前角色：从 sessionStorage 读；未登录（如登录页）返回 null → 不过滤
function getRole() {
  return sessionStorage.getItem('role');
}

// 按角色过滤左侧导航（v1 前端过滤，不取代后端 RBAC，不拦截直接 URL 访问）
function applyMenuFilter() {
  const role = getRole();
  if (!role || !MENU[role]) return;   // 未登录 / 未知角色 → 显示全部 8 项
  const visible = MENU[role];
  document.querySelectorAll('nav a[data-tab]').forEach(a => {
    a.classList.toggle('hidden', !visible.includes(a.dataset.tab));
  });
  // 收起被清空的组标签（.grp / .nt），避免孤立组标签
  document.querySelectorAll('nav .grp, nav .nt').forEach(g => {
    let el = g.nextElementSibling, hasVisible = false;
    while (el && !el.classList.contains('grp') && !el.classList.contains('nt')) {
      if (el.tagName === 'A' && !el.classList.contains('hidden')) { hasVisible = true; break; }
      el = el.nextElementSibling;
    }
    g.classList.toggle('hidden', !hasVisible);
  });
}

applyMenuFilter();

// 配置页二级子导航切换（24 号 Step 7）
const cNav = document.querySelectorAll('.cf-subnav button[data-ctab]');
const cPanels = document.querySelectorAll('#p7 [id^="c"]');
cNav.forEach(b => b.addEventListener('click', () => {
  cNav.forEach(x => x.classList.remove('active'));
  cPanels.forEach(s => s.classList.add('hidden'));
  b.classList.add('active');
  document.getElementById(b.dataset.ctab).classList.remove('hidden');
}));

// 登录：接真实认证（13 §7.2 / 22 §2.1）。角色由账号决定，不再用演示下拉；
// 凭据与会话事实写入会话级存储（13 §8.1：token / role / user_id），登出即清空这几项。
const loginBtn = document.getElementById('loginBtn');
const loginErr = document.getElementById('loginErr');
const loginUser = document.getElementById('loginUser');
const loginPwd = document.getElementById('loginPwd');
if (loginBtn) {
  loginBtn.addEventListener('click', async () => {
    const u = loginUser.value.trim();
    const p = loginPwd.value.trim();
    if (!u || !p) {
      loginErr.textContent = '请输入用户名和密码';
      loginErr.classList.add('show');
      loginUser.classList.toggle('err', !u);
      loginPwd.classList.toggle('err', !p);
      return;
    }
    loginErr.classList.remove('show');
    loginUser.classList.remove('err');
    loginPwd.classList.remove('err');
    loginBtn.textContent = '登录中…';
    loginBtn.disabled = true;
    try {
      const data = await window.api('/auth/login', { method: 'POST', body: { username: u, password: p } });
      sessionStorage.setItem('token', data.access_token);
      sessionStorage.setItem('role', data.role);
      sessionStorage.setItem('user_id', String(data.user_id));
      if (data.warehouse_id) sessionStorage.setItem('warehouse_id', data.warehouse_id);
      window.location.href = 'data-import.html';
    } catch (e) {
      loginBtn.textContent = '登录';
      loginBtn.disabled = false;
      // 登录失败只有一种说法（13 §6.1 / 22 §2.1）：后端 401 的 message 即「账号或密码错误」。
      loginErr.textContent = (e && e.body && e.body.message) || '账号或密码错误';
      loginErr.classList.add('show');
    }
  });
}
const pwdToggle = document.getElementById('pwdToggle');
if (pwdToggle && loginPwd) {
  pwdToggle.addEventListener('click', () => {
    const isPwd = loginPwd.type === 'password';
    loginPwd.type = isPwd ? 'text' : 'password';
    pwdToggle.textContent = isPwd ? '🙈' : '👁';
  });
}

// 配置页滑块实时数值刷新
document.querySelectorAll('.cf-slider input[type=range]').forEach(slider => {
  const valEl = slider.parentElement.querySelector('.val');
  const update = () => {
    const v = slider.value;
    const max = parseInt(slider.max);
    if (max <= 22 && max >= 18) { valEl.textContent = v + ':00'; }
    else { valEl.textContent = v + '%'; }
  };
  slider.addEventListener('input', update);
  update();
});

// 冷路径开关（data-toggle="ai"）接 POST /api/llm/toggle（仅 admin，ai.toggle）；
// 其余开关（如预留开关）保持纯 CSS 绿/灰切换，不接端点。
document.querySelectorAll('.cf-switch').forEach(sw => {
  const isAi = sw.dataset.toggle === 'ai';
  if (!isAi) {
    sw.addEventListener('click', () => sw.classList.toggle('off'));
    return;
  }
  // 冷路径默认打开（cold_path_enabled=true），页面加载先落「开」；无只读状态端点，状态仅随切换回显。
  sw.classList.remove('off');
  if (getRole() !== 'admin') {
    sw.style.cursor = 'not-allowed';
    sw.title = '仅管理员可切换冷路径开关';
    return;
  }
  sw.addEventListener('click', async () => {
    const enabled = sw.classList.contains('off');   // 当前「关」→ 点击即「开」
    try {
      const resp = await api('/llm/toggle', { method: 'POST', body: { enabled: enabled } });
      sw.classList.toggle('off', !resp.cold_path_enabled);
      const stateEl = document.getElementById('aiSwitchState');
      if (stateEl) {
        stateEl.textContent = resp.cold_path_enabled ? '开启' : '关闭';
        stateEl.style.color = resp.cold_path_enabled ? 'var(--green)' : 'var(--muted)';
      }
    } catch (e) {
      alert((e.body && e.body.message) || e.message);
    }
  });
});

// 入库作业微调下钻：切换推荐理由卡显隐
document.querySelectorAll('[data-reason]').forEach(btn => {
  btn.addEventListener('click', () => {
    const id = btn.dataset.reason;
    document.querySelectorAll('.reason').forEach(r => r.classList.add('hidden'));
    const target = document.getElementById(id);
    if (target) { target.classList.remove('hidden'); }
  });
});

// 四态占位触发钩子（21 号 §7.11）：把容器切到 空/加载/成功/异常 之一
// 用法：setState(el, 'empty', {text:'请先导入生产订单'}); setState(el, 'error', {text:'导入失败', retry:'重试', onRetry:fn});
function setState(el, state, opts) {
  opts = opts || {};
  el.classList.remove('empty', 'loading', 'success', 'error');
  el.classList.add('state', state);
  if (state === 'loading') {
    el.innerHTML = '<span class="spin"></span>' + (opts.text || '处理中…');
    return;
  }
  el.textContent = opts.text || '';
  if (state === 'error' && opts.retry) {
    const b = document.createElement('button');
    b.className = 'btn ghost retry';
    b.textContent = opts.retry;
    if (opts.onRetry) b.addEventListener('click', opts.onRetry);
    el.appendChild(b);
  }
}
window.setState = setState;

/* ============================================================
 * fetch/API 封装 + 文件→base64（16 号附录B + spec「数据导入页数据层」）
 * 数据层原语：页面脚本只调 window.api，不各自拼 fetch。
 * ============================================================ */

// 通用 API 封装：Bearer 认证（13 §六）、JSON 编解码、非 2xx 抛错（带 status/body）。
// 用法：const d = await window.api('/import/session', {method:'POST', body:{...}});
async function api(path, options) {
  options = options || {};
  const opts = Object.assign({ headers: {} }, options);
  const token = sessionStorage.getItem('token');
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  if (opts.body && typeof opts.body === 'object') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  // 后端所有业务端点都在 `/api` 前缀下（app/core/config.py 的 `api_prefix="/api"`）。
  // 页面只传相对 API 路径（如 `/jobs`、`/import/session`、`/allocate/batch`），
  // 这里统一补前缀，避免各页把 `/api` 散落成字面量；相对 `fetch` 也要求前后端同源。
  const resp = await fetch('/api' + path, opts);
  const text = await resp.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
  if (!resp.ok) {
    const err = new Error((data && data.message) || ('HTTP ' + resp.status));
    err.status = resp.status;
    err.body = data;
    throw err;
  }
  return data;
}
window.api = api;

// 转义 HTML（规则/AI 文本均来自后端，但仍按不可信处理，防注入）。
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
window.escapeHtml = escapeHtml;

// 冷路径/对话台请求都要求 warehouse_id（auth.LoginResponse 已回传，登录时已存）。
// aiBody 合并 {warehouse_id, ...payload}；缺失时抛「请重新登录」而非发请求（避免 422）。
function aiBody(payload) {
  const warehouse_id = sessionStorage.getItem('warehouse_id');
  if (!warehouse_id) {
    const err = new Error('请重新登录（缺少仓库编码）');
    err.status = 401;
    throw err;
  }
  return Object.assign({ warehouse_id: warehouse_id }, payload || {});
}
window.aiBody = aiBody;

// 降级文案（spec「双产物建议卡渲染」）：ai_generated=false 时按 degraded_reason 给「人话」，
// 不渲染 AI 叙事区、不报错（降级不静默）。
const DEGRADED_TEXT = {
  provider_unconfigured: '未配置外部 LLM，仅展示规则结果',
  budget_exhausted: '本月 AI 预算已用尽，仅展示规则结果',
  llm_timeout: 'AI 分析超时，仅展示规则结果',
  llm_unavailable: 'AI 服务暂不可用，仅展示规则结果',
  insufficient_samples: '历史批次样本不足，仅展示统计摘要',
  intent_unrecognized: '没能听懂你的问题，请换个说法或使用下方结构化入口'
};

// 双产物渲染（spec「双产物建议卡渲染」+ design D2）：rule 恒有（系统结论，常规样式）
// + ai 可选（琥珀「AI 建议」；服务端已注入标注，前端逐字渲染、不重复前置）
// + ai_generated=false 时降级说明。
function renderDualProduct(container, resp, opts) {
  opts = opts || {};
  // 卡片标题可被调用方覆盖：归因用「可能原因」而非「系统结论」措辞（spec「偏离批次归因入口」）。
  const ruleLabel = opts.ruleLabel || '规则结果（系统结论）';
  const aiLabel = opts.aiLabel || '🤖 AI 建议';
  const rule = resp.rule || {};
  let html = '<div class="ai-rule"><div class="rh">' + ruleLabel + '</div>'
    + '<div class="rb"><pre>' + escapeHtml(JSON.stringify(rule, null, 2)) + '</pre></div></div>';
  if (resp.ai) {
    html += '<div class="ai-card"><div class="rh">' + aiLabel + '</div><div class="rb">'
      + escapeHtml(resp.ai).replace(/\n/g, '<br>') + '</div></div>';
  } else if (resp.ai_generated === false) {
    html += '<div class="ai-card degraded"><div class="rh">AI 未生成（降级）</div><div class="rb">'
      + escapeHtml(DEGRADED_TEXT[resp.degraded_reason] || resp.degraded_reason || 'AI 分析不可用') + '</div></div>';
  }
  container.innerHTML = html;
}
window.renderDualProduct = renderDualProduct;

// 文件 → base64（零构建上传走 JSON 而非 multipart，design.md D7）。
// 返回不含 data: 前缀的纯 base64 串（对应 UploadRequest.content_base64）。
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1]);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}
window.readFileAsBase64 = readFileAsBase64;
