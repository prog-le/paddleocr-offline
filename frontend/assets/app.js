/* ==========================================================================
   PaddleX OCR Web 前端逻辑（原生 JS，无框架、无 CDN）
   ========================================================================== */
(function () {
  'use strict';

  const state = {
    meta: null,
    files: [],
    tasks: [],
    selectedId: null,
    result: null,
    defaults: {},
    loadedTaskId: null,
    pollTimer: null,
    submitting: false,
    queue: {},
    autoSelected: false,
    pipelines: [],
    pipeline: '',
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const STATUS_TEXT = {
    pending: '排队中',
    running: '识别中',
    success: '已完成',
    failed: '失败',
    cancelled: '已取消',
  };

  // ---------------------------------------------------------------- 工具
  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return '-';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined) return '-';
    if (sec < 60) return sec.toFixed(1) + 's';
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return m + 'm' + String(s).padStart(2, '0') + 's';
  }

  function fmtTime(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  let toastTimer = null;
  function toast(msg, kind) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = 'toast show' + (kind ? ' ' + kind : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast'; }, 3200);
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    const ct = res.headers.get('content-type') || '';
    let body = null;
    if (ct.includes('application/json')) {
      body = await res.json().catch(() => null);
    } else {
      body = await res.text();
    }
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : (typeof body === 'string' ? body : `HTTP ${res.status}`);
      throw new Error(detail);
    }
    return body;
  }

  // ---------------------------------------------------------------- 初始化
  async function init() {
    bindEvents();
    try {
      state.meta = await api('/api/meta');
      applyMeta();
    } catch (err) {
      toast('无法获取服务配置：' + err.message, 'err');
    }
    refreshTasks(true);
  }

  function applyMeta() {
    const meta = state.meta || {};
    const pipeline = meta.pipeline || 'OCR';
    const dev = meta.device || {};
    const active = dev.active || {};
    const deviceLabel = active.kind === 'gpu'
      ? `GPU${active.index || 0} · ${active.name || 'CUDA'}`
      : (active.name || 'CPU');
    const pill = $('#pipeline-pill');
    pill.textContent = (meta.mock ? 'MOCK 自检模式' : pipeline) + ' · ' + deviceLabel;
    pill.className = 'pill ' + (meta.mock ? 'warn' : (active.kind === 'gpu' ? 'ok' : ''));
    pill.title = active.kind === 'gpu'
      ? `CUDA 加速：${active.name}（配置 ${dev.configured || 'auto'}）`
      : `CPU 推理（配置 ${dev.configured || 'auto'}）`;

    // 设备 pill：明确显示当前跑在 CPU 还是 GPU；Apple Silicon 额外说明"只能用 CPU"
    const devicePill = $('#device-pill');
    if (devicePill) {
      const isApple = dev.platform && dev.platform.apple_silicon;
      if (active.kind === 'gpu') {
        const mem = dev.memory || {};
        const memText = mem.total_mem_mb ? ` ${mem.total_mem_mb}MB` : '';
        devicePill.textContent = `GPU${active.index || 0}${memText}`;
        devicePill.className = 'pill ok';
        devicePill.title = `CUDA 加速：${active.name}\n整卡显存 ${mem.used_mem_mb || '?'}/${mem.total_mem_mb || '?'} MB\n识别 batch=${(dev.batching || {}).text_rec_batch_size || '-'}`;
      } else if (isApple) {
        devicePill.textContent = 'Apple Silicon · CPU';
        devicePill.className = 'pill';
        devicePill.title = 'PaddlePaddle 没有 Metal/MPS 后端，Apple Silicon 只能用 CPU 推理。'
          + '\n可调大 ocr.cpu_threads、降低 pdf_dpi/max_side_px 提速；需要 GPU 请部署到 NVIDIA 主机。';
      } else {
        devicePill.textContent = 'CPU';
        devicePill.className = 'pill';
        devicePill.title = active.reason || '当前使用 CPU 推理';
      }
      if (active.fallback && active.reason && !isApple) {
        devicePill.title = active.reason;
      }
    }

    const limits = meta.limits || {};
    $('#dz-sub').textContent =
      `单文件最大 ${limits.max_upload_mb || 200} MB · 单次最多 ${limits.max_files_per_batch || 20} 个 · 单个 PDF 最多 ${limits.max_pdf_pages || 100} 页`;
    $('#footer-version').textContent = 'v' + (meta.version || '1.0.0') +
      (meta.pdf_backend ? ' · PDF 引擎 ' + meta.pdf_backend : '');
    // 会话标识：任务按会话隔离，仅本浏览器可见
    const sid = (meta.session && meta.session.id) || '';
    const sessionPill = $('#session-pill');
    if (sessionPill && sid) {
      sessionPill.textContent = '会话 ' + sid;
      sessionPill.title = '任务按浏览器会话隔离（' + sid + '）：只能看到本浏览器上传的任务；'
        + '换浏览器、清 Cookie 或换设备后看不到这些任务，管理员后台仍可查看。';
    }

    state.defaults = Object.assign(
      {},
      (meta.ocr && meta.ocr.defaults) || {},
      (meta.indra && meta.indra.defaults) || {},
    );
    applyParams(state.defaults);
    initPipelines(meta);
  }

  // 参数面板里所有可调项（用于按产线统一开关）
  const ALL_PARAM_KEYS = [
    'use_table_recognition', 'use_formula_recognition', 'use_seal_recognition',
    'use_doc_orientation_classify', 'use_doc_unwarping', 'use_textline_orientation',
    'text_det_box_thresh', 'text_det_thresh', 'text_rec_score_thresh', 'layout_threshold',
    'text_det_limit_side_len', 'math', 'keep_furniture',
  ];

  function initPipelines(meta) {
    const list = [];
    if (meta.auto_pipeline) {
      list.push({
        id: '',
        label: meta.auto_pipeline_label || '⚡ 智能选择（推荐）',
        kind: 'auto',
        description: '上传后先探测文件类型 + （PDF）调用 pdf-inspector 轻量分类，自动选最合适的产线',
      });
    }
    (meta.pipelines || []).forEach((p) => {
      list.push({
        id: p.id,
        label: p.label || p.id,
        kind: p.kind || 'structure',
        description: p.description || '',
      });
    });
    if (!list.length) {
      list.push({ id: meta.pipeline || 'OCR', label: meta.pipeline || 'OCR', kind: 'structure', description: '' });
    }
    state.pipelines = list;
    // 有智能选择时，默认选中它；否则走后端默认产线
    state.pipeline = meta.auto_pipeline ? '' : (meta.pipeline || list[0].id);
    const select = $('#pipeline-select');
    select.innerHTML = '';
    list.forEach((p, idx) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      const isDefault = meta.auto_pipeline ? (idx === 0) : (p.id === meta.pipeline);
      opt.textContent = p.label + (isDefault ? '（默认）' : '');
      if (p.kind === 'auto') opt.dataset.auto = '1';
      opt.title = p.description || p.label;
      select.appendChild(opt);
    });
    if (list.some((p) => p.id === state.pipeline)) select.value = state.pipeline;
    select.addEventListener('change', () => {
      state.pipeline = select.value;
      applyPipelineUI();
    });
    applyPipelineUI();
  }

  function currentPipeline() {
    return state.pipelines.find((p) => p.id === state.pipeline) || state.pipelines[0] || { kind: 'structure' };
  }

  // 各产线不支持哪些参数（键 -> 原因），据此禁用控件并给出悬浮说明
  const PARAM_UNSUPPORTED = {
    ocr: {
      use_table_recognition: '纯文字识别产线不做表格结构，请改用「通用表格识别」或「通用版面解析」',
      use_formula_recognition: '纯文字识别产线不含公式识别子模型',
      use_seal_recognition: '纯文字识别产线不含印章识别子模型',
      layout_threshold: '纯文字识别产线不做版面分析，无此阈值',
    },
    table: {
      use_table_recognition: '本产线本身即表格识别，该开关始终生效',
      use_formula_recognition: '通用表格识别产线不含公式识别子模型',
      use_seal_recognition: '通用表格识别产线不含印章识别子模型',
      layout_threshold: '通用表格识别产线的版面检测只用于定位表格，无该阈值',
      use_textline_orientation: '通用表格识别产线不支持文本行方向分类',
    },
    structure: {},
    indra: {
      use_table_recognition: 'Indra OCR 按版面块输出表格，不做单元格结构还原',
      use_formula_recognition: '请使用「公式转 LaTeX」开关（Indra 自带公式识别）',
      use_seal_recognition: 'Indra OCR 不含印章识别模型',
      use_doc_orientation_classify: 'Indra OCR 自动处理页面旋转/倾斜，无需单独开关',
      use_doc_unwarping: 'Indra OCR 不支持弯曲矫正',
      use_textline_orientation: 'Indra OCR 按整页版面识别，无文本行方向开关',
      text_det_box_thresh: 'Indra OCR 不暴露文本检测框阈值',
      text_det_thresh: 'Indra OCR 不暴露文本检测像素阈值',
      text_rec_score_thresh: 'Indra OCR 不暴露识别分数阈值',
      layout_threshold: 'Indra OCR 使用模型自带版面判定，无该阈值',
      text_det_limit_side_len: 'Indra OCR 自动缩放，无该限制',
    },
  };

  // 仅 Indra OCR 支持的参数：其它产线一律禁用并提示
  const INDRA_ONLY_PARAMS = {
    math: '仅 Indra OCR 支持（公式转 LaTeX）',
    keep_furniture: '仅 Indra OCR 支持（保留页眉页脚）',
  };

  function applyPipelineUI() {
    const p = currentPipeline();
    const desc = $('#pipeline-desc');
    if (desc) desc.textContent = p.description || p.label;

    // auto kind：顶部智能提示（上传前就能看到会怎么选）
    let autoHint = $('#auto-hint');
    if (p.kind === 'auto') {
      if (!autoHint) {
        autoHint = document.createElement('p');
        autoHint.id = 'auto-hint';
        autoHint.className = 'hint';
        autoHint.style.cssText = 'color: var(--accent, #60a5fa); font-size: 11px;';
        if (desc && desc.parentNode) desc.parentNode.insertBefore(autoHint, desc.nextSibling);
      }
      const hasPdi = (state.pipelines || []).some(x => x.id === 'PDFINSPECTOR');
      const hasIndra = (state.pipelines || []).some(x => x.id === 'INDRA');
      const lines = ['上传后先按文件类型分类 → 自动选最合适的产线：'];
      if (hasPdi) lines.push('• 原生文本 PDF → PDFINSPECTOR（Rust 快抽，~150ms/页）');
      if (hasIndra) lines.push('• 扫描/图片 PDF → INDRA（轻量 OCR，~0.2-1s/页）');
      if (!hasPdi && !hasIndra) lines.push('• 统一走默认产线');
      autoHint.textContent = lines.join(' ');
      autoHint.style.display = '';
    } else if (autoHint) {
      autoHint.style.display = 'none';
    }

    // auto kind：智能路由不确定最终产线，不禁用任何参数，让用户按实际需求调
    const unsupported = p.kind === 'auto'
      ? {}
      : Object.assign(
          {},
          p.kind === 'indra' ? {} : INDRA_ONLY_PARAMS,
          PARAM_UNSUPPORTED[p.kind] || {},
        );
    ALL_PARAM_KEYS.forEach((key) => {
      const el = $(`input[data-param="${key}"]`);
      if (!el) return;
      const reason = unsupported[key] || '';
      el.disabled = Boolean(reason);
      const wrap = el.closest('.switch') || el.closest('.slider');
      if (wrap) {
        wrap.style.opacity = reason ? '.45' : '1';
        wrap.title = reason;
      }
    });
  }

  function applyParams(params) {
    $$('input[data-param]').forEach((el) => {
      const key = el.dataset.param;
      if (!(key in params)) return;
      const value = params[key];
      if (el.type === 'checkbox') {
        el.checked = !!value;
      } else {
        el.value = value;
        updateSliderLabel(el);
      }
    });
  }

  function updateSliderLabel(input) {
    const wrap = input.closest('.slider');
    if (wrap) {
      const val = wrap.querySelector('.val');
      if (val) val.textContent = Number(input.value).toFixed(2);
    }
  }

  function collectParams() {
    const out = {};
    $$('input[data-param]').forEach((el) => {
      const key = el.dataset.param;
      if (el.type === 'checkbox') {
        out[key] = el.checked;
      } else if (el.type === 'number') {
        const n = Number(el.value);
        if (!Number.isNaN(n)) out[key] = n;
      } else {
        out[key] = Number(el.value);
      }
    });
    return out;
  }

  // ---------------------------------------------------------------- 事件
  function bindEvents() {
    const dropzone = $('#dropzone');
    const input = $('#file-input');

    dropzone.addEventListener('click', () => input.click());
    dropzone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    ['dragenter', 'dragover'].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropzone.classList.add('dragover');
      })
    );
    ['dragleave', 'drop'].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        if (evt === 'dragleave' && e.relatedTarget && dropzone.contains(e.relatedTarget)) return;
        dropzone.classList.remove('dragover');
      })
    );
    dropzone.addEventListener('drop', (e) => {
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) addFiles(files);
    });
    input.addEventListener('change', () => {
      if (input.files && input.files.length) addFiles(input.files);
      input.value = '';
    });

    $('#btn-submit').addEventListener('click', submitTasks);
    $('#btn-clear').addEventListener('click', () => {
      state.files = [];
      renderFileList();
    });
    $('#btn-refresh').addEventListener('click', () => refreshTasks(true));

    $('#params-toggle').addEventListener('click', () => {
      const body = $('#params-body');
      const hidden = body.hasAttribute('hidden');
      if (hidden) body.removeAttribute('hidden'); else body.setAttribute('hidden', '');
      $('#params-toggle .chev').textContent = hidden ? '收起' : '展开';
    });

    $$('.slider input[type="range"]').forEach((el) => {
      el.addEventListener('input', () => updateSliderLabel(el));
    });
    $('#btn-reset-params').addEventListener('click', () => {
      applyParams(state.defaults);
      toast('已恢复默认参数', 'ok');
    });

    $('#btn-copy').addEventListener('click', copyText);
    $$('[data-export]').forEach((btn) => {
      btn.addEventListener('click', () => exportResult(btn.dataset.export));
    });
  }

  // ---------------------------------------------------------------- 文件
  function addFiles(fileList) {
    const meta = state.meta || {};
    const limits = meta.limits || {};
    const allowed = (meta.supported_ext || ['.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp']);
    const maxBytes = (limits.max_upload_mb || 200) * 1024 * 1024;
    const maxFiles = limits.max_files_per_batch || 20;
    const existing = new Set(state.files.map((f) => f.name + ':' + f.size));

    Array.from(fileList).forEach((file) => {
      const key = file.name + ':' + file.size;
      if (existing.has(key)) return;
      const ext = ('.' + (file.name.split('.').pop() || '')).toLowerCase();
      let error = '';
      if (!allowed.includes(ext)) error = '不支持的格式';
      else if (file.size > maxBytes) error = '超过 ' + (limits.max_upload_mb || 200) + ' MB';
      else if (file.size === 0) error = '空文件';
      const item = { file: file, name: file.name, size: file.size, error: error };
      if (!error && state.files.filter((f) => !f.error).length >= maxFiles) {
        item.error = '超过单次 ' + maxFiles + ' 个上限';
      }
      state.files.push(item);
      existing.add(key);
    });
    renderFileList();
  }

  function renderFileList() {
    const ul = $('#file-list');
    ul.innerHTML = '';
    state.files.forEach((item, index) => {
      const li = document.createElement('li');
      li.className = 'file-item' + (item.error ? ' bad' : '');
      const name = document.createElement('span');
      name.className = 'fname';
      name.textContent = item.name;
      name.title = item.name + (item.error ? '（' + item.error + '）' : '');
      const size = document.createElement('span');
      size.className = 'fsize';
      size.textContent = item.error ? item.error : fmtSize(item.size);
      const del = document.createElement('button');
      del.className = 'fdel';
      del.type = 'button';
      del.textContent = '×';
      del.title = '移除';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        state.files.splice(index, 1);
        renderFileList();
      });
      li.append(name, size, del);
      ul.appendChild(li);
    });
    const valid = state.files.filter((f) => !f.error).length;
    $('#btn-submit').disabled = valid === 0 || state.submitting;
    $('#btn-clear').disabled = state.files.length === 0 || state.submitting;
    $('#btn-submit').textContent = state.submitting
      ? '上传中…'
      : valid > 0 ? `开始识别（${valid}）` : '开始识别';
  }

  // ---------------------------------------------------------------- 提交
  async function submitTasks() {
    const valid = state.files.filter((f) => !f.error);
    if (!valid.length || state.submitting) return;
    state.submitting = true;
    renderFileList();
    const form = new FormData();
    valid.forEach((item) => form.append('files', item.file, item.name));
    form.append('params', JSON.stringify(collectParams()));
    form.append('pipeline', state.pipeline || '');
    try {
      const res = await api('/api/tasks', { method: 'POST', body: form });
      const tasks = (res.tasks || []).map((t) => ({ id: t.id }));
      toast(`已提交 ${tasks.length} 个任务，正在排队识别`, 'ok');
      if (res.errors && res.errors.length) {
        toast(`${res.errors.length} 个文件被拒绝：` + res.errors[0].error, 'err');
      }
      state.files = state.files.filter((f) => f.error);
      renderFileList();
      if (tasks.length) {
        state.selectedId = tasks[0].id;
        state.loadedTaskId = null;
      }
      await refreshTasks(true);
    } catch (err) {
      toast('提交失败：' + err.message, 'err');
    } finally {
      state.submitting = false;
      renderFileList();
    }
  }

  // ---------------------------------------------------------------- 队列
  async function refreshTasks(immediate) {
    clearTimeout(state.pollTimer);
    try {
      const data = await api('/api/tasks?limit=60');
      state.tasks = data.items || [];
      state.queue = data.queue || {};
      renderTasks(state.queue);
      // 首次进入自动展开最近一个已完成任务，省去一次点击
      if (!state.autoSelected && !state.selectedId && state.tasks.length) {
        const first = state.tasks.find((t) => t.status === 'success');
        if (first) {
          state.autoSelected = true;
          selectTask(first.id);
        }
      }
      const active = state.tasks.some((t) => t.status === 'pending' || t.status === 'running');
      maybeLoadResult();
      state.pollTimer = setTimeout(refreshTasks, active ? 1200 : 6000);
    } catch (err) {
      state.pollTimer = setTimeout(refreshTasks, 5000);
      if (immediate) toast('获取任务列表失败：' + err.message, 'err');
    }
  }

  function renderTasks(queue) {
    const list = $('#task-list');
    const summary = $('#queue-summary');
    summary.textContent = `排队 ${queue.pending || 0} · 运行 ${queue.running || 0} · 完成 ${queue.success || 0}` +
      (queue.paused ? ' · 已暂停接单' : '');

    if (!state.tasks.length) {
      list.innerHTML = '<p class="empty">本会话还没有任务，先上传文件吧</p>';
      return;
    }

    // ---- 按 batch_id 分组（同一上传的文件是同一组） ----
    const groups = {};
    const order = [];
    state.tasks.forEach((task) => {
      const key = task.batch_id || task.id;
      if (!groups[key]) { groups[key] = []; order.push(key); }
      groups[key].push(task);
    });

    const frag = document.createDocumentFragment();

    order.forEach((key) => {
      const batch = groups[key];

      // 分组 header
      const header = document.createElement('div');
      header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:8px 4px 4px;border-bottom:1px solid var(--border,#374151);margin:4px 0;';
      const left = document.createElement('div');
      left.style.cssText = 'font-size:11px;opacity:.7;';
      const ok = batch.filter(t => t.status === 'success').length;
      const run = batch.filter(t => t.status === 'running' || t.status === 'pending').length;
      const fail = batch.filter(t => t.status === 'failed' || t.status === 'cancelled').length;
      left.textContent = `${key} · ${batch.length} 文件` +
        (ok ? ` · ✅ ${ok}` : '') + (run ? ` · ⏳ ${run}` : '') + (fail ? ` · ❌ ${fail}` : '');
      header.appendChild(left);

      const terminal = batch.filter(t => t.status === 'success' || t.status === 'failed' || t.status === 'cancelled');
      if (terminal.length > 0) {
        const batchBtn = document.createElement('button');
        batchBtn.className = 'primary';
        batchBtn.style.cssText = 'font-size:11px;padding:2px 8px;';
        batchBtn.textContent = `批量换产线重跑 ${terminal.length}`;
        batchBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          openBatchRetryDialog(key, batch);
        });
        header.appendChild(batchBtn);
      }
      frag.appendChild(header);

      // 组内任务
      batch.forEach((task) => {
        const item = document.createElement('div');
        item.className = 'task-item ' + task.status + (task.id === state.selectedId ? ' active' : '');
        item.dataset.id = task.id;
        item.style.cssText = 'margin-bottom:4px;';
        item.addEventListener('click', () => selectTask(task.id));

        const dot = document.createElement('span');
        dot.className = 'dot ' + task.status;

        const main = document.createElement('div');
        main.className = 'task-main';
        const name = document.createElement('div');
        name.className = 'task-name';
        name.textContent = task.filename;
        name.title = task.filename;
        const sub = document.createElement('div');
        sub.className = 'task-sub';
        const parts = [];
        parts.push(STATUS_TEXT[task.status] || task.status);
        if (task.pipeline_label) parts.push(task.pipeline_label);
        if (task.pipeline_auto_reason) parts.push('⚡ ' + task.pipeline_auto_reason);
        parts.push(fmtSize(task.size));
        if (task.pages_total > 1) parts.push(`${task.pages_done || 0}/${task.pages_total} 页`);
        if (task.status === 'pending' && task.queue_position) parts.push('第 ' + task.queue_position + ' 位');
        if (task.status === 'success' && task.duration_sec) parts.push(fmtDuration(task.duration_sec));
        if (task.status === 'running' && task.stage) parts.push(task.stage);
        if (task.status === 'failed' && task.error) parts.push(task.error.slice(0, 60));
        parts.push(fmtTime(task.created_at));
        parts.forEach((text, i) => {
          const span = document.createElement('span');
          span.textContent = text;
          if (i === parts.length - 1) span.style.color = 'var(--text-3)';
          sub.appendChild(span);
        });

        const bar = document.createElement('div');
        bar.className = 'progress';
        const fill = document.createElement('i');
        const pct = task.status === 'success' ? 100 : (task.progress || 0);
        fill.style.width = Math.max(pct, task.status === 'pending' ? 2 : 4) + '%';
        bar.appendChild(fill);

        main.append(name, sub, bar);

        const actions = document.createElement('div');
        actions.className = 'task-actions';
        if (task.status === 'pending' || task.status === 'running') {
          const btn = document.createElement('button');
          btn.textContent = '取消';
          btn.addEventListener('click', (e) => { e.stopPropagation(); cancelTask(task.id); });
          actions.appendChild(btn);
        } else {
          const retry = document.createElement('button');
          retry.className = 'primary';
          retry.textContent = '换产线重跑';
          retry.addEventListener('click', (e) => {
            e.stopPropagation();
            openRetryDialog(task);
          });
          actions.appendChild(retry);
          const del = document.createElement('button');
          del.className = 'danger';
          del.textContent = '删除';
          del.addEventListener('click', (e) => { e.stopPropagation(); deleteTask(task.id); });
          actions.appendChild(del);
        }

        item.append(dot, main, actions);
        frag.appendChild(item);
      });
    });

    list.innerHTML = '';
    list.appendChild(frag);
  }

  async function cancelTask(id) {
    try {
      await api(`/api/tasks/${id}/cancel`, { method: 'POST' });
      toast('已发送取消请求', 'ok');
      refreshTasks();
    } catch (err) { toast('取消失败：' + err.message, 'err'); }
  }

  async function deleteTask(id) {
    if (!window.confirm('删除该任务及其结果文件？')) return;
    try {
      await api(`/api/tasks/${id}`, { method: 'DELETE' });
      if (state.selectedId === id) {
        state.selectedId = null;
        state.loadedTaskId = null;
        state.result = null;
        renderEmptyResult();
      }
      toast('已删除', 'ok');
      refreshTasks();
    } catch (err) { toast('删除失败：' + err.message, 'err'); }
  }

  // ---- 换产线重跑（简易 modal，无依赖） ----
  function openRetryDialog(task) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:9999;';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg, #1f2937);color:var(--fg, #e5e7eb);padding:24px;border-radius:10px;min-width:380px;max-width:480px;box-shadow:0 10px 40px rgba(0,0,0,.4);';

    box.innerHTML = `
      <div style="font-weight:600;margin-bottom:12px;">换产线重跑 <span style="opacity:.7;font-weight:400;">${esc(task.filename || '')}</span></div>
      <div style="font-size:12px;opacity:.7;margin-bottom:14px;">
        当前产线：${esc(task.pipeline_label || '-')}
        ${task.pipeline_auto_reason ? `<br>上次智能选择理由：${esc(task.pipeline_auto_reason)}` : ''}
      </div>
      <div style="margin-bottom:10px;">
        <label style="font-size:12px;opacity:.8;">产线</label><br>
        <select id="retry-pipeline" style="width:100%;padding:6px 8px;margin-top:4px;background:var(--panel,#111827);color:inherit;border:1px solid var(--border,#374151);border-radius:6px;">
          <option value="">⚡ 智能选择（推荐）</option>
          ${(state.pipelines || [])
            .filter(p => p.kind !== 'auto')
            .map(p => `<option value="${esc(p.id)}">${esc(p.label)}${p.id ? '' : ''}</option>`)
            .join('')}
        </select>
      </div>
      <div style="margin-bottom:14px;">
        <label style="font-size:12px;opacity:.8;">参数（JSON，可选，留空沿用上次）</label><br>
        <textarea id="retry-params" rows="3" placeholder='{"math":true}' style="width:100%;padding:6px 8px;margin-top:4px;background:var(--panel,#111827);color:inherit;border:1px solid var(--border,#374151);border-radius:6px;font-family:monospace;font-size:12px;">${task.params ? esc(JSON.stringify(task.params)) : ''}</textarea>
      </div>
      <div style="text-align:right;gap:8px;display:flex;justify-content:flex-end;">
        <button id="retry-cancel">取消</button>
        <button id="retry-ok" class="primary">重跑</button>
      </div>
    `;
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('#retry-cancel').addEventListener('click', close);
    overlay.querySelector('#retry-ok').addEventListener('click', async () => {
      const pid = overlay.querySelector('#retry-pipeline').value;
      const pRaw = overlay.querySelector('#retry-params').value.trim();
      const body = {};
      if (pid !== task.pipeline) body.pipeline = pid;
      if (pRaw) {
        try { body.params = JSON.parse(pRaw); }
        catch { toast('参数 JSON 格式错误', 'err'); return; }
      }
      close();
      await rerunTask(task.id, body);
    });
  }

  async function rerunTask(taskId, body) {
    try {
      await api(`/api/tasks/${taskId}/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      toast('已重排队，等待处理', 'ok');
      if (state.selectedId === taskId) {
        state.loadedTaskId = null;
        state.result = null;
        renderEmptyResult();
      }
      refreshTasks();
    } catch (err) { toast('重跑失败：' + err.message, 'err'); }
  }

  // ---- 批量重跑 dialog ----
  function openBatchRetryDialog(batchId, batchTasks) {
    const terminal = batchTasks.filter(t => t.status === 'success' || t.status === 'failed' || t.status === 'cancelled');
    if (!terminal.length) { toast('这个 batch 没有终态任务可重跑', 'err'); return; }

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:9999;';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg, #1f2937);color:var(--fg, #e5e7eb);padding:24px;border-radius:10px;min-width:440px;max-width:520px;box-shadow:0 10px 40px rgba(0,0,0,.4);';

    // 每种产线在 batch 中占比的快速统计（让用户知道多数选的是啥）
    const byPipeline = {};
    terminal.forEach(t => { byPipeline[t.pipeline_label || '未知'] = (byPipeline[t.pipeline_label || '未知'] || 0) + 1; });
    const pipelineStats = Object.entries(byPipeline).map(([k, v]) => `${esc(k)}: ${v}`).join('，');

    box.innerHTML = `
      <div style="font-weight:600;margin-bottom:12px;">批量换产线重跑 <span style="opacity:.7;font-weight:400;">${esc(batchId)}</span></div>
      <div style="font-size:12px;opacity:.7;margin-bottom:14px;">
        将重跑 ${terminal.length} 个终态任务（运行中/排队中的自动跳过）。<br>
        当前各产线分布：${esc(pipelineStats) || '全部手动选'}
      </div>
      <div style="margin-bottom:10px;">
        <label style="font-size:12px;opacity:.8;">产线（应用到整个 batch）</label><br>
        <select id="batch-pipeline" style="width:100%;padding:6px 8px;margin-top:4px;background:var(--panel,#111827);color:inherit;border:1px solid var(--border,#374151);border-radius:6px;">
          <option value="">⚡ 智能选择（每个文件独立路由）</option>
          ${(state.pipelines || [])
            .filter(p => p.kind !== 'auto')
            .map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`)
            .join('')}
        </select>
      </div>
      <div style="margin-bottom:14px;">
        <label style="font-size:12px;opacity:.8;">参数（JSON，可选，留空沿用上次）</label><br>
        <textarea id="batch-params" rows="3" placeholder='{"math":true}' style="width:100%;padding:6px 8px;margin-top:4px;background:var(--panel,#111827);color:inherit;border:1px solid var(--border,#374151);border-radius:6px;font-family:monospace;font-size:12px;"></textarea>
      </div>
      <div style="text-align:right;gap:8px;display:flex;justify-content:flex-end;">
        <button id="batch-cancel">取消</button>
        <button id="batch-ok" class="primary">批量重跑 ${terminal.length}</button>
      </div>
    `;
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('#batch-cancel').addEventListener('click', close);
    overlay.querySelector('#batch-ok').addEventListener('click', async () => {
      const pid = overlay.querySelector('#batch-pipeline').value;
      const pRaw = overlay.querySelector('#batch-params').value.trim();
      const body = {};
      if (pid) body.pipeline = pid;  // 智能选择时不传 pipeline（让后端不覆盖）
      if (pRaw) {
        try { body.params = JSON.parse(pRaw); }
        catch { toast('参数 JSON 格式错误', 'err'); return; }
      }
      close();
      await rerunBatch(batchId, body, terminal.length);
    });
  }

  async function rerunBatch(batchId, body, expectedTerminal) {
    try {
      const res = await api(`/api/batches/${batchId}/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      const msgs = [`已触发重跑：共 ${res.total}，成功 ${res.retried}，跳过 ${res.skipped}`];
      if (res.errors && res.errors.length) {
        msgs.push(`${res.errors.length} 个有错误：` + res.errors.slice(0, 3).map(e => `${e.id}(${e.reason})`).join('；'));
        if (res.errors.length > 3) msgs.push(`…还有 ${res.errors.length - 3} 个`);
      }
      toast(msgs.join('｜'), res.retried > 0 ? 'ok' : 'err');
      refreshTasks();
    } catch (err) { toast('批量重跑失败：' + err.message, 'err'); }
  }

  function selectTask(id) {
    state.selectedId = id;
    state.loadedTaskId = null;
    renderTasks(state.queue || {});
    const task = state.tasks.find((t) => t.id === id);
    if (!task) return;
    if (task.status === 'success') {
      loadResult(id);
    } else {
      renderPendingResult(task);
    }
  }

  function maybeLoadResult() {
    if (!state.selectedId) return;
    const task = state.tasks.find((t) => t.id === state.selectedId);
    if (!task) return;
    if (task.status === 'success' && state.loadedTaskId !== task.id) {
      loadResult(task.id);
    } else if (task.status !== 'success') {
      renderPendingResult(task);
    }
  }

  // ---------------------------------------------------------------- 结果
  function setResultEnabled(enabled) {
    $('#btn-copy').disabled = !enabled;
    $$('[data-export]').forEach((b) => { b.disabled = !enabled; });
  }

  function renderEmptyResult() {
    $('#result-title').textContent = '识别结果';
    $('#result-meta').innerHTML = '';
    $('#result-body').innerHTML = '<p class="empty">选择左侧任务查看识别结果</p>';
    setResultEnabled(false);
  }

  function renderPendingResult(task) {
    $('#result-title').textContent = task.filename;
    $('#result-meta').innerHTML = '';
    const body = $('#result-body');
    setResultEnabled(false);
    if (task.status === 'failed') {
      body.innerHTML = '';
      const p = document.createElement('p');
      p.className = 'empty';
      p.style.color = 'var(--err)';
      p.textContent = '识别失败：' + (task.error || '未知错误');
      body.appendChild(p);
      const tip = document.createElement('p');
      tip.className = 'empty';
      tip.style.paddingTop = '0';
      tip.textContent = '可联系管理员查看该任务的日志（后台 → 任务日志）排查环境或模型问题。';
      body.appendChild(tip);
      return;
    }
    body.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'empty';
    p.innerHTML = '<span class="spinner"></span> ' +
      (task.status === 'cancelled' ? '任务已取消' :
        (task.stage || (STATUS_TEXT[task.status] || '')) + `（${Math.round(task.progress || 0)}%）`);
    body.appendChild(p);
  }

  async function loadResult(id) {
    state.loadedTaskId = id;
    $('#result-body').innerHTML = '<p class="empty"><span class="spinner"></span> 正在加载识别结果…</p>';
    try {
      const doc = await api(`/api/tasks/${id}/result`);
      state.result = doc;
      renderResult(doc);
      setResultEnabled(true);
    } catch (err) {
      $('#result-body').innerHTML = '<p class="empty" style="color:var(--err)">加载失败：' +
        escapeHtml(err.message) + '</p>';
      setResultEnabled(false);
    }
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderResult(doc) {
    $('#result-title').textContent = doc.name || '识别结果';
    const stats = doc.stats || {};
    const meta = $('#result-meta');
    meta.innerHTML = '';
    const fields = [
      ['产线', doc.pipeline || '-'],
      ['页数', stats.pages || 0],
      ['文本块', stats.blocks || 0],
      ['表格', stats.tables || 0],
      ['字符', stats.chars || 0],
    ];
    fields.forEach(([k, v]) => {
      const span = document.createElement('span');
      span.innerHTML = escapeHtml(k) + ' <b>' + escapeHtml(v) + '</b>';
      meta.appendChild(span);
    });
    // 智能产线选择的理由：后端 doc.auto_pipeline_reason 透传
    if (doc.auto_pipeline_reason) {
      const span = document.createElement('span');
      span.style.color = 'var(--info, #0d6efd)';
      span.textContent = '⚡ ' + doc.auto_pipeline_reason;
      span.title = '后端 smart_router 自动选择这条产线的理由';
      meta.appendChild(span);
    }
    (doc.warnings || []).forEach((w) => {
      const span = document.createElement('span');
      span.style.color = 'var(--warn)';
      span.textContent = '⚠ ' + w;
      meta.appendChild(span);
    });

    const body = $('#result-body');
    body.innerHTML = '';
    const frag = document.createDocumentFragment();
    (doc.pages || []).forEach((page) => {
      if ((doc.pages || []).length > 1) {
        const sep = document.createElement('div');
        sep.className = 'page-sep';
        sep.textContent = `第 ${page.index} 页`;
        frag.appendChild(sep);
      }
      (page.blocks || []).forEach((block) => {
        if (block.type === 'table' && block.table) {
          const wrap = document.createElement('div');
          wrap.className = 'table-wrap';
          wrap.appendChild(buildTable(block.table));
          frag.appendChild(wrap);
          return;
        }
        const text = (block.text || '').trim();
        if (!text && block.type !== 'figure') return;
        const div = document.createElement('div');
        div.className = 'blk';
        if (block.type === 'title') {
          div.className += block.label === 'doc_title' || block.label === 'title' ? ' blk-title1' : ' blk-title2';
        } else if (block.type === 'figure') {
          div.className += ' blk-figure';
        } else if (block.type === 'caption') {
          div.className += ' blk-caption';
        } else if (block.type === 'formula') {
          div.className += ' blk-formula';
        } else {
          div.className += ' blk-text';
        }
        div.textContent = text || '[图片]';
        frag.appendChild(div);
      });
    });
    if (!frag.childNodes.length) {
      body.innerHTML = '<p class="empty">未识别到内容，请检查文件清晰度或调整识别阈值</p>';
      return;
    }
    body.appendChild(frag);
  }

  function buildTable(table) {
    const el = document.createElement('table');
    el.className = 'data';
    const tbody = document.createElement('tbody');
    const grid = [];
    for (let r = 0; r < table.n_rows; r++) {
      grid.push(new Array(table.n_cols).fill(null));
    }
    (table.cells || []).forEach((cell) => {
      if (r0ok(cell.row, table.n_rows) && cell.col < table.n_cols) {
        grid[cell.row][cell.col] = cell;
      }
    });
    function r0ok(r, n) { return r >= 0 && r < n; }

    for (let r = 0; r < table.n_rows; r++) {
      const tr = document.createElement('tr');
      for (let c = 0; c < table.n_cols; c++) {
        const cell = grid[r][c];
        if (!cell) continue;
        const td = document.createElement(cell.is_header ? 'th' : 'td');
        if (cell.rowspan > 1) td.rowSpan = Math.min(cell.rowspan, table.n_rows - r);
        if (cell.colspan > 1) td.colSpan = Math.min(cell.colspan, table.n_cols - c);
        td.textContent = cell.text || '';
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    el.appendChild(tbody);
    return el;
  }

  // ---------------------------------------------------------------- 复制/导出
  async function copyText() {
    if (!state.selectedId) return;
    try {
      const res = await fetch(`/api/tasks/${state.selectedId}/text`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const text = await res.text();
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      toast(`已复制 ${text.length} 个字符到剪贴板`, 'ok');
    } catch (err) {
      toast('复制失败：' + err.message, 'err');
    }
  }

  function exportResult(fmt) {
    if (!state.selectedId) return;
    const url = `/api/tasks/${state.selectedId}/export?fmt=${encodeURIComponent(fmt)}`;
    const a = document.createElement('a');
    a.href = url;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('正在生成 ' + fmt.toUpperCase() + ' 文件…', 'ok');
  }

  document.addEventListener('DOMContentLoaded', init);
})();
