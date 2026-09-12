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

// 配置页二级子导航切换（24 号 Step 7）
const cNav = document.querySelectorAll('.cf-subnav button[data-ctab]');
const cPanels = document.querySelectorAll('#p7 [id^="c"]');
cNav.forEach(b => b.addEventListener('click', () => {
  cNav.forEach(x => x.classList.remove('active'));
  cPanels.forEach(s => s.classList.add('hidden'));
  b.classList.add('active');
  document.getElementById(b.dataset.ctab).classList.remove('hidden');
}));

// 登录失败演示 + 密码可见性切换
const loginBtn = document.getElementById('loginBtn');
const loginErr = document.getElementById('loginErr');
const loginUser = document.getElementById('loginUser');
const loginPwd = document.getElementById('loginPwd');
if (loginBtn) {
  loginBtn.addEventListener('click', () => {
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
    loginBtn.textContent = '登录中…';
    loginBtn.disabled = true;
    setTimeout(() => {
      window.location.href = 'data-import.html';
    }, 600);
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

// 冷路径开关 + 预留开关（绿/灰切换）
document.querySelectorAll('.cf-switch').forEach(sw => {
  sw.addEventListener('click', () => sw.classList.toggle('off'));
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
