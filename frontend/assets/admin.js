/* ==========================================================================
   后台管理前端逻辑（原生 JS）
   ========================================================================== */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const STATUS_TEXT = {
    pending: '排队中', running: '识别中', success: '已完成',
    failed: '失败', cancelled: '已取消',
  };
  const STATUS_CLASS = { success: 'ok', failed: 'err', cancelled: '', pending: 'warn', running: 'warn' };

  let autoTimer = null;
  let lastLogFile = 'app.log';

  function toast(msg, kind) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = 'toast show' + (kind ? ' ' + kind : '');
    setTimeout(() => { el.className = 'toast'; }, 3000);
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: {} }, options));
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('json') ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : (typeof body === 'string' ? body : 'HTTP ' + res.status);
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function fmtSize(bytes) {
    if (bytes === null || bytes === undefined) return '-';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0, v = bytes;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
  }
  function fmtDur(sec) {
    if (sec === null || sec === undefined) return '-';
    if (sec < 60) return sec.toFixed(1) + 's';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.round(sec % 60);
    return (h ? h + 'h' : '') + m + 'm' + String(s).padStart(2, '0') + 's';
  }
  function fmtTime(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function esc(t) {
    return String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---------------------------------------------------------------- 登录态
  async function checkSession() {
    try {
      await api('/api/admin/session');
      showDash();
      return true;
    } catch (err) {
      showLogin();
      return false;
    }
  }

  function showLogin() {
    $('#login-view').hidden = false;
    $('#dash-view').hidden = true;
    $('#btn-logout').hidden = true;
    stopAuto();
  }

  function showDash() {
    $('#login-view').hidden = true;
    $('#dash-view').hidden = false;
    $('#btn-logout').hidden = false;
    refreshAll();
    startAuto();
  }

  async function doLogin(evt) {
    evt.preventDefault();
    const username = $('#username').value.trim();
    const password = $('#password').value;
    try {
      await api('/api/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      $('#password').value = '';
      showDash();
      toast('登录成功', 'ok');
    } catch (err) {
      toast('登录失败：' + err.message, 'err');
    }
  }

  async function doLogout() {
    try { await api('/api/admin/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    showLogin();
  }

  // ---------------------------------------------------------------- 概览
  async function loadOverview() {
    try {
      const data = await api('/api/admin/overview');
      renderStats(data);
      renderEnv(data);
      renderDeviceControls(data);
      renderModels(data.models || {});
      const pill = $('#svc-pill');
      const engineOk = (data.engine || {}).state !== 'error';
      pill.textContent = engineOk ? '服务正常' : '产线异常';
      pill.className = 'pill ' + (engineOk ? 'ok' : 'err');
      $('#svc-hint').textContent =
        `产线 ${data.service.pipeline} · 并发 ${data.service.concurrency} · ` +
        `运行 ${fmtDur(data.service.uptime_sec)}` + (data.service.paused ? ' · 已暂停接单' : '');
      $('#btn-pause').hidden = data.service.paused;
      $('#btn-resume').hidden = !data.service.paused;
    } catch (err) {
      if (err.status === 401) { showLogin(); return; }
      const pill = $('#svc-pill');
      pill.textContent = '连接失败';
      pill.className = 'pill err';
    }
  }

  function renderStats(data) {
    const t = data.tasks || {};
    const sys = data.system || {};
    const items = [
      { k: '排队 / 运行', v: `${t.pending || 0} / ${t.running || 0}`, cls: '' },
      { k: '今日任务', v: t.today || 0, cls: '' },
      { k: '已完成', v: t.success || 0, cls: 'ok' },
      { k: '失败', v: t.failed || 0, cls: t.failed ? 'err' : '' },
      { k: '平均耗时', v: fmtDur(t.avg_duration_sec || 0), cls: '' },
      { k: '累计页数', v: t.pages_total || 0, cls: '' },
      {
        // 容器里 mem_total 是宿主机内存，容易误判；有 cgroup 限额时优先显示容器口径
        k: sys.container_mem_limit_mb ? '容器内存' : '内存占用',
        v: sys.container_mem_limit_mb
          ? (sys.container_mem_used_mb || 0) + ' <small>/ ' + sys.container_mem_limit_mb + ' MB</small>'
          : (sys.mem_used_mb || 0) + ' <small>/ ' + (sys.mem_total_mb || 0) + ' MB</small>',
        cls: (sys.container_mem_limit_mb
          ? (sys.container_mem_free_mb || 0) < 600
          : (sys.mem_percent || 0) > 88) ? 'err' : '',
      },
      { k: '磁盘可用', v: (sys.disk_free_gb || 0) + ' <small>GB</small>', cls: (sys.disk_free_gb || 99) < 5 ? 'err' : '' },
    ];
    const grid = $('#stat-grid');
    grid.innerHTML = items.map((it) =>
      `<div class="stat ${it.cls}"><div class="k">${esc(it.k)}</div><div class="v">${it.v}</div></div>`
    ).join('');
  }

  function renderEnv(data) {
    const s = data.service || {};
    const e = data.engine || {};
    const sys = data.system || {};
    const paths = data.paths || {};
    const rows = [
      ['默认产线', s.pipeline || '-'],
      ['推理设备', (() => {
        const d = (s.device_summary || {}).active || {};
        if (d.kind === 'gpu') return `${d.device} · ${d.name || 'CUDA'}`;
        return `${d.device || s.device || 'cpu'} · ${d.name || 'CPU'}`;
      })()],
      ...(function () {
        const d = s.device_summary || {};
        const a = d.active || {};
        const rows2 = [];
        if (a.fallback && a.reason) rows2.push(['设备说明', a.reason]);
        if (a.kind === 'gpu') {
          const m = d.memory || {};
          rows2.push(['GPU 显存', `${m.used_mem_mb || '?'} / ${m.total_mem_mb || '?'} MB（整卡）`
            + `，本进程池 ${m.reserved_mb || '?'} MB`]);
        }
        rows2.push(['OCR 档位 / batch', `${d.ocr_tier || '-'} / ${(d.batching || {}).text_rec_batch_size || '-'}`]);
        return rows2;
      })(),
      ['模型状态', (e.state || '-') + (e.loaded ? `（${e.instances} 实例）` : '')],
      ['加载耗时', e.load_seconds != null ? e.load_seconds + ' s' : '-'],
      ['推理次数', e.predict_count != null ? e.predict_count : '-'],
      ['最近单页耗时', e.last_predict_ms != null ? e.last_predict_ms + ' ms' : '-'],
      ['模型目录', paths.models || '-'],
      ['数据目录', paths.data || '-'],
      ['日志目录', paths.logs || '-'],
      ['架构 / Python', `${sys.arch || '-'} / ${sys.python || '-'}`],
      ['系统', sys.platform || '-'],
      ['进程内存', (sys.proc_rss_mb || 0) + ' MB'],
      ...(sys.container_mem_limit_mb
        ? [['容器内存上限', sys.container_mem_limit_mb + ' MB（已用 ' + (sys.container_mem_used_mb || 0)
           + ' MB，可用 ' + (sys.container_mem_free_mb || 0) + ' MB）']]
        : []),
      ['CPU 使用率', (sys.cpu_percent != null ? sys.cpu_percent + ' %' : '-')],
      ['负载', (sys.load_avg || []).join(' / ') || '-'],
      ['版本', s.version || '-'],
    ];
    // 各产线加载状态（多产线时逐条展示）
    const sub = (e.pipelines || {});
    Object.keys(sub).forEach((pid) => {
      const st = sub[pid] || {};
      rows.push([`产线 ${pid}`, `${st.state || '-'}${st.loaded ? `（${st.instances} 实例，${st.load_seconds || 0}s）` : ''}` + (st.label ? ` · ${st.label}` : '')]);
    });
    $('#env-kv').innerHTML = rows
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
      .join('');
    if (e.error) {
      $('#env-kv').innerHTML += `<dt style="color:var(--err)">错误</dt><dd style="color:var(--err)">${esc(e.error)}</dd>`;
    }
  }

  function renderDeviceControls(data) {
    const s = data.service || {};
    const dev = s.device_summary || {};
    const active = dev.active || {};
    const select = $('#device-select');
    const tier = $('#tier-select');
    const batch = $('#batch-input');
    const hint = $('#device-hint');
    if (!select || !tier || !batch) return;

    // GPU 选项按探测结果动态生成（没有 GPU 时只有 auto/cpu）
    const current = dev.configured || 'auto';
    const gpus = dev.gpus || [];
    const options = ['auto', 'cpu', ...gpus.map((g) => `gpu:${g.index}`)];
    select.innerHTML = options.map((v) => {
      const label = v.startsWith('gpu:')
        ? `${v}（${(gpus.find((g) => `gpu:${g.index}` === v) || {}).name || 'GPU'}）`
        : v;
      return `<option value="${esc(v)}">${esc(label)}</option>`;
    }).join('');
    select.value = options.includes(current) ? current : 'auto';

    tier.value = dev.ocr_tier || 'medium';
    batch.value = (dev.batching || {}).text_rec_batch_size || 6;

    if (hint) {
      const isApple = (dev.platform || {}).apple_silicon;
      let text = `当前：${active.device || '-'} · ${active.name || '-'}`
        + `（paddle ${(dev.capabilities || {}).version || '?'}`
        + `，CUDA=${(dev.capabilities || {}).cuda ? '是' : '否'}）`;
      if (isApple) text += ' ｜ Apple Silicon 仅支持 CPU（Paddle 无 Metal 后端）';
      else if (active.fallback && active.reason) text += ' ｜ ' + active.reason;
      hint.textContent = text;
      hint.title = text;
    }
  }

  async function applyDeviceConfig() {
    const device = $('#device-select').value;
    const tier = $('#tier-select').value;
    const batch = parseInt($('#batch-input').value, 10);
    if (!Number.isFinite(batch) || batch < 1 || batch > 256) {
      toast('batch 需在 1~256 之间', 'err');
      return;
    }
    try {
      const res = await api('/api/admin/config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ocr: { device, ocr_tier: tier, text_rec_batch_size: batch } }),
      });
      const applied = res.applied || {};
      if (!Object.keys(applied).length) {
        toast('配置未变更（或字段不可在线修改）');
        return;
      }
      // 设备/档位/批大小在建模时才生效 → 必须重建实例
      toast('配置已保存，正在重载模型…', 'ok');
      await api('/api/admin/service/reload', { method: 'POST' });
      toast(`已切换到 ${device}（OCR ${tier}，batch ${batch}）`, 'ok');
      loadOverview();
    } catch (err) {
      toast('应用失败：' + err.message, 'err');
    }
  }

  function renderModels(models) {
    const tbody = $('#models-table tbody');
    const list = models.models || [];
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="2" class="hint">未检测到模型，请检查离线模型目录</td></tr>';
    } else {
      tbody.innerHTML = list.map((m) =>
        `<tr><td class="mono">${esc(m.name)}</td><td class="mono">${m.size_mb} MB</td></tr>`
      ).join('');
    }
    $('#models-hint').textContent = `${list.length} 个模型 · ${models.exists ? '目录存在' : '目录缺失'}`;
    $('#models-dir').textContent = '路径：' + (models.dir || '-');
  }

  // ---------------------------------------------------------------- 任务
  async function loadTasks() {
    const status = $('#task-status').value;
    const keyword = $('#task-keyword').value.trim();
    const params = new URLSearchParams({ status, limit: '200' });
    if (keyword) params.set('keyword', keyword);
    try {
      const data = await api('/api/admin/tasks?' + params.toString());
      renderTasks(data.items || []);
    } catch (err) {
      if (err.status === 401) { showLogin(); return; }
      toast('加载任务失败：' + err.message, 'err');
    }
  }

  function renderTasks(items) {
    const tbody = $('#tasks-table tbody');
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="hint" style="padding:18px">没有符合条件的任务</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((t) => {
      const actions = [];
      if (t.status === 'pending' || t.status === 'running') {
        actions.push(`<button class="btn small" data-act="cancel" data-id="${t.id}">取消</button>`);
      }
      if (t.status === 'failed' || t.status === 'cancelled') {
        actions.push(`<button class="btn small" data-act="retry" data-id="${t.id}">重试</button>`);
      }
      actions.push(`<button class="btn small" data-act="log" data-id="${t.id}">日志</button>`);
      if (t.status !== 'running' && t.status !== 'pending') {
        actions.push(`<button class="btn small danger" data-act="delete" data-id="${t.id}">删除</button>`);
      }
      const err = t.error ? `<div class="hint" style="color:var(--err)">${esc(String(t.error).slice(0, 120))}</div>` : '';
      return `<tr>
        <td class="mono">${esc(t.id)}</td>
        <td>${esc(t.filename)}${err}${t.pipeline_label ? `<div class="hint">产线：${esc(t.pipeline_label)}</div>` : ''}</td>
        <td class="mono" title="浏览器会话标识">${esc(t.session || '-')}</td>
        <td><span class="badge ${STATUS_CLASS[t.status] || ''}">${esc(STATUS_TEXT[t.status] || t.status)}</span></td>
        <td class="mono">${Math.round(t.progress || 0)}%</td>
        <td class="mono">${t.pages_done || 0}/${t.pages_total || 0}</td>
        <td class="mono">${t.duration_sec != null ? fmtDur(t.duration_sec) : '-'}</td>
        <td class="mono">${fmtTime(t.created_at)}</td>
        <td style="white-space:nowrap">${actions.join(' ')}</td>
      </tr>`;
    }).join('');

    $$('#tasks-table button[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => taskAction(btn.dataset.act, btn.dataset.id));
    });
  }

  async function taskAction(act, id) {
    try {
      if (act === 'cancel') {
        await api(`/api/admin/tasks/${id}/cancel`, { method: 'POST' });
        toast('已发送取消请求', 'ok');
      } else if (act === 'retry') {
        await api(`/api/admin/tasks/${id}/retry`, { method: 'POST' });
        toast('已重新入队', 'ok');
      } else if (act === 'delete') {
        if (!window.confirm('删除该任务及其结果文件？')) return;
        await api(`/api/admin/tasks/${id}`, { method: 'DELETE' });
        toast('已删除', 'ok');
      } else if (act === 'log') {
        const data = await api(`/api/admin/tasks/${id}/log?lines=300`);
        $('#log-view').textContent = data.content || '（暂无日志）';
        $('#log-file').value = '';
        toast('已显示任务日志 ' + id, 'ok');
        return;
      }
      loadTasks();
      loadOverview();
    } catch (err) {
      toast('操作失败：' + err.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 日志
  async function loadLogs() {
    const file = $('#log-file').value || lastLogFile;
    const lines = $('#log-lines').value;
    try {
      const data = await api(`/api/admin/logs?file=${encodeURIComponent(file)}&lines=${lines}`);
      const select = $('#log-file');
      const current = select.value;
      select.innerHTML = (data.files || []).map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
      if (current && (data.files || []).includes(current)) select.value = current;
      lastLogFile = data.file || 'app.log';
      const view = $('#log-view');
      view.textContent = data.content || '（日志为空）';
      view.scrollTop = view.scrollHeight;
    } catch (err) {
      if (err.status === 401) { showLogin(); return; }
      $('#log-view').textContent = '读取日志失败：' + err.message;
    }
  }

  // ---------------------------------------------------------------- 配置
  async function loadConfig() {
    try {
      const data = await api('/api/admin/config');
      $('#config-editor').value = JSON.stringify(data.config, null, 2);
      $('#config-path').textContent = '配置文件：' + data.config_path;
    } catch (err) {
      if (err.status === 401) { showLogin(); return; }
      toast('读取配置失败：' + err.message, 'err');
    }
  }

  async function saveConfig() {
    let payload;
    try {
      payload = JSON.parse($('#config-editor').value);
    } catch (err) {
      toast('JSON 格式错误：' + err.message, 'err');
      return;
    }
    try {
      const res = await api('/api/admin/config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const keys = Object.keys(res.applied || {});
      toast(keys.length ? `已生效：${keys.join(', ')}` : '没有可在线修改的字段', keys.length ? 'ok' : '');
      loadConfig();
      loadOverview();
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 控制
  async function serviceAction(action) {
    try {
      await api(`/api/admin/service/${action}`, { method: 'POST' });
      toast('操作成功：' + action, 'ok');
      loadOverview();
    } catch (err) {
      toast('操作失败：' + err.message, 'err');
    }
  }

  async function cleanup() {
    try {
      const res = await api('/api/admin/maintenance/cleanup', { method: 'POST' });
      toast(`已清理 ${res.count} 个过期任务`, 'ok');
      loadTasks();
      loadOverview();
    } catch (err) {
      toast('清理失败：' + err.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 轮询
  function startAuto() {
    stopAuto();
    if (!$('#auto-refresh').checked) return;
    autoTimer = setInterval(() => {
      loadOverview();
      loadTasks();
    }, 5000);
  }
  function stopAuto() {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  }

  function refreshAll() {
    loadOverview();
    loadTasks();
    loadLogs();
    loadConfig();
  }

  function bind() {
    $('#login-form').addEventListener('submit', doLogin);
    $('#btn-logout').addEventListener('click', doLogout);
    $('#btn-refresh').addEventListener('click', refreshAll);
    $('#btn-pause').addEventListener('click', () => serviceAction('pause'));
    $('#btn-resume').addEventListener('click', () => serviceAction('resume'));
    $('#btn-warmup').addEventListener('click', () => serviceAction('warmup'));
    $('#btn-reload').addEventListener('click', () => serviceAction('reload'));
    $('#btn-device-apply').addEventListener('click', applyDeviceConfig);
    $('#btn-cleanup').addEventListener('click', cleanup);
    $('#btn-task-search').addEventListener('click', loadTasks);
    $('#task-status').addEventListener('change', loadTasks);
    $('#task-keyword').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadTasks(); });
    $('#btn-log-refresh').addEventListener('click', loadLogs);
    $('#log-file').addEventListener('change', () => { lastLogFile = $('#log-file').value; loadLogs(); });
    $('#log-lines').addEventListener('change', loadLogs);
    $('#btn-config-save').addEventListener('click', saveConfig);
    $('#btn-config-reload').addEventListener('click', loadConfig);
    $('#auto-refresh').addEventListener('change', () => { if ($('#auto-refresh').checked) startAuto(); else stopAuto(); });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bind();
    checkSession();
  });
})();
