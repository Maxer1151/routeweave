'use strict';
/* Routeweave panel UI.
   Ограничения: CSP без inline-скриптов и стилей, никакой вставки HTML из данных
   сервера (только createElement + textContent), относительные запросы /api/...
   и системные шрифты. */

(function () {
  /* ---------------- Константы ---------------- */

  const POLL_MS = 15000;
  const JOB_POLL_MS = 2000;
  const SYNC_TIMEOUT_MS = 180000;
  const PROBE_TIMEOUT_MS = 120000;
  const APPLY_TIMEOUT_MS = 200000;
  const THEME_KEY = 'routeweave-theme';
  const MAX_TOASTS = 3;
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const API = {
    status: '/api/status',
    probe: '/api/probe',
    job: '/api/jobs/apply',
    list: (name) => '/api/lists/' + encodeURIComponent(name),
    apply: (name) => '/api/lists/' + encodeURIComponent(name) + '/apply',
    restart: (name) => '/api/restart/' + encodeURIComponent(name)
  };

  const SERVICE_META = {
    mihomo: {
      title: 'Ядро mihomo', unit: 'mihomo.service', hint: 'TUN · DNS :53 · mixed :7890',
      api: 'mihomo', required: true, icon: 'i-routes'
    },
    'zapret-nfqws': {
      title: 'zapret / DPI', unit: 'zapret-nfqws.service', hint: 'NFQUEUE · desync для dpi.txt',
      api: 'zapret', required: true, icon: 'i-shield'
    },
    'routeweave-firewall': {
      title: 'Firewall', unit: 'routeweave-firewall.service', hint: 'ip_forward · FORWARD · QUIC',
      api: null, required: true, icon: 'i-lock'
    },
    tailscaled: {
      title: 'Tailscale', unit: 'tailscaled.service', hint: 'exit node · tailnet · опционально',
      api: null, required: false, icon: 'i-globe'
    },
    'routeweave-panel': {
      title: 'Панель', unit: 'routeweave-panel.service', hint: 'FastAPI · Basic-auth',
      api: 'panel', required: true, icon: 'i-sliders'
    },
    'routeweave-tgwsproxy': {
      title: 'Telegram-прокси', unit: 'docker · routeweave-tgwsproxy', hint: 'MTProto :1443 · опционально',
      api: 'tgwsproxy', required: false, icon: 'i-list'
    }
  };

  const SERVICE_ORDER = ['mihomo', 'zapret-nfqws', 'routeweave-firewall', 'tailscaled', 'routeweave-panel', 'routeweave-tgwsproxy'];

  const PROBE_META = {
    chatgpt_via_socks: { label: 'ChatGPT через SOCKS' },
    socks_exit_ip: { label: 'Выходной IP SOCKS' },
    youtube_direct: { label: 'YouTube, direct + zapret' },
    tg_ws_proxy: { label: 'Telegram-прокси :1443' },
    cf_worker: { label: 'CF Worker' }
  };

  const PROBE_ORDER = ['chatgpt_via_socks', 'socks_exit_ip', 'youtube_direct', 'tg_ws_proxy', 'cf_worker'];

  const LIST_META = {
    'geo-custom': {
      file: 'geo-custom.txt', route: '→ SOCKS',
      desc: 'Домены, которые идут через внешний SOCKS/HTTP-прокси.',
      applyHint: 'Применение обновляет списки и перезагружает mihomo.'
    },
    dpi: {
      file: 'dpi.txt', route: '→ zapret',
      desc: 'Домены, для которых zapret снимает DPI-блокировки.',
      applyHint: 'Применение обновляет списки и перезапускает zapret-nfqws.'
    },
    'ru-custom': {
      file: 'ru-direct-custom.txt', route: '→ direct',
      desc: 'Домены, которые должны идти напрямую, минуя прокси.',
      applyHint: 'Применение обновляет списки и перезагружает mihomo.'
    }
  };

  const LIST_ORDER = ['geo-custom', 'dpi', 'ru-custom'];

  /* ---------------- Состояние ---------------- */

  const state = {
    status: null,
    probes: null,
    probing: false,
    applying: false,
    saving: false,
    authFailed: false,
    netFailed: false,
    offline: false,
    currentList: 'geo-custom',
    lists: {},
    updatedAt: null,
    editorMessage: null,
    probeStartedAt: 0,
    applyStartedAt: 0
  };

  const refs = {
    services: new Map(),
    svcBox: null,
    probeBody: null,
    probeTable: null,
    probeTs: null,
    probeBtn: null,
    probeBtnLabel: null,
    probeBtnIcon: null,
    editor: null,
    counts: null,
    editorState: null,
    dirtyPill: null,
    listFile: null,
    listRoute: null,
    listDesc: null,
    applyBtn: null,
    applyLabel: null,
    applyIcon: null,
    saveBtn: null,
    applyHint: null,
    tgCard: null,
    tgList: null,
    tgOpen: null,
    tgSecretHint: null,
    tsCard: null,
    tsList: null,
    configList: null,
    gwIp: null,
    updatedAt: null,
    toasts: null,
    themeIcon: null,
    themeBtn: null,
    a11y: null,
    netBanner: null,
    authBanner: null
  };

  let statusTimer = null;
  let probeTicker = null;
  let applyTicker = null;
  let statusInFlight = false;

  /* ---------------- Утилиты ---------------- */

  function $(sel) { return document.querySelector(sel); }

  function h(tag, props) {
    const node = document.createElement(tag);
    if (props) {
      for (const key in props) {
        if (!Object.prototype.hasOwnProperty.call(props, key)) continue;
        const val = props[key];
        if (val === undefined || val === null || val === false) continue;
        if (key === 'class') node.className = val;
        else if (key === 'text') node.textContent = String(val);
        else if (key === 'dataset') {
          for (const dk in val) {
            if (val[dk] !== undefined && val[dk] !== null) node.dataset[dk] = String(val[dk]);
          }
        } else if (key === 'on') {
          for (const ev in val) node.addEventListener(ev, val[ev]);
        } else if (val === true) node.setAttribute(key, '');
        else node.setAttribute(key, String(val));
      }
    }
    for (let i = 2; i < arguments.length; i++) appendKid(node, arguments[i]);
    return node;
  }

  function appendKid(node, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) {
      for (const k of kid) appendKid(node, k);
      return;
    }
    if (typeof kid === 'string' || typeof kid === 'number') {
      node.appendChild(document.createTextNode(String(kid)));
      return;
    }
    node.appendChild(kid);
  }

  function icon(id, cls) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', cls || 'icon');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + id);
    svg.appendChild(use);
    return svg;
  }

  function setIcon(svg, id) {
    if (!svg) return;
    const use = svg.querySelector('use');
    if (use) use.setAttribute('href', '#' + id);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function timeNow() {
    const d = new Date();
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function plural(n, one, few, many) {
    const n10 = n % 10;
    const n100 = n % 100;
    if (n10 === 1 && n100 !== 11) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return few;
    return many;
  }

  function fmtBytes(n) {
    if (typeof n !== 'number' || !isFinite(n) || n < 0) return '';
    if (n < 1024) return Math.round(n) + ' Б';
    const units = ['КБ', 'МБ', 'ГБ', 'ТБ'];
    let value = n / 1024;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index++;
    }
    return value.toFixed(1).replace('.', ',') + ' ' + units[index];
  }

  function fmtBytesOrDash(n) {
    const text = fmtBytes(n);
    return text || '—';
  }

  function truncate(text, max) {
    const s = String(text == null ? '' : text);
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  }

  class ApiError extends Error {
    constructor(status, message, detail) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.detail = detail || message;
    }
  }

  /* ---------------- HTTP ---------------- */

  async function api(path, options) {
    const opt = options || {};
    const method = opt.method || 'GET';
    const timeout = opt.timeout === 0 ? 0 : (opt.timeout || 20000);
    const controller = new AbortController();
    let timer = null;
    if (timeout > 0) timer = setTimeout(() => controller.abort(), timeout);

    let res;
    try {
      res = await fetch(path, {
        method: method,
        body: opt.body === undefined ? undefined : opt.body,
        headers: opt.body === undefined ? undefined : { 'Content-Type': 'text/plain; charset=utf-8' },
        cache: 'no-store',
        credentials: 'same-origin',
        signal: controller.signal
      });
    } catch (err) {
      if (err && err.name === 'AbortError') {
        throw new ApiError(0, 'превышено время ожидания ответа панели');
      }
      throw new ApiError(0, 'нет связи с панелью');
    } finally {
      if (timer) clearTimeout(timer);
    }

    if (res.status === 401) {
      onUnauthorized();
      throw new ApiError(401, 'требуется авторизация');
    }

    let raw = '';
    try { raw = await res.text(); } catch (err) { raw = ''; }

    let data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (err) { data = null; }
    }

    if (!res.ok) {
      let msg = '';
      if (data && typeof data.detail === 'string') msg = data.detail;
      else if (data && data.detail) msg = JSON.stringify(data.detail);
      else if (raw) msg = raw.slice(0, 300);
      else msg = res.statusText || 'ошибка ' + res.status;
      throw new ApiError(res.status, msg.trim(), msg);
    }

    markOnline();
    return data;
  }

  /* ---------------- Соединение и авторизация ---------------- */

  function markOnline() {
    if (state.offline) {
      state.offline = false;
      refs.netBanner.hidden = true;
    }
    if (state.authFailed) {
      state.authFailed = false;
      refs.authBanner.hidden = true;
    }
  }

  function onUnauthorized() {
    state.authFailed = true;
    state.offline = false;
    refs.authBanner.hidden = false;
    refs.netBanner.hidden = true;
    stopPolling();
    renderHero();
    say('Требуется авторизация');
  }

  function markOffline() {
    state.offline = true;
    refs.netBanner.hidden = false;
    if (refs.updatedAt) refs.updatedAt.textContent = 'нет данных';
  }

  function say(text) {
    if (!refs.a11y) return;
    refs.a11y.textContent = '';
    window.setTimeout(() => { refs.a11y.textContent = text; }, 40);
  }

  /* ---------------- Тосты ---------------- */

  const TOAST_ICONS = { ok: 'i-check', bad: 'i-x', warn: 'i-alert', info: 'i-help' };

  function toast(tone, title, detail) {
    const kind = TOAST_ICONS[tone] ? tone : 'info';
    const node = h('div', { class: 'toast', dataset: { tone: kind }, role: kind === 'bad' ? 'alert' : 'status' },
      h('span', { class: 'toast-icon' }, icon(TOAST_ICONS[kind])),
      h('div', { class: 'toast-body' },
        h('p', { class: 'toast-title', text: title }),
        detail ? h('p', { class: 'toast-desc', text: String(detail) }) : null
      ),
      h('button', {
        type: 'button', class: 'toast-close', 'aria-label': 'Закрыть уведомление',
        on: { click: () => removeToast(node) }
      }, icon('i-x'))
    );

    refs.toasts.appendChild(node);
    while (refs.toasts.children.length > MAX_TOASTS) {
      removeToast(refs.toasts.firstElementChild);
    }
    const ttl = kind === 'bad' ? 9000 : 5200;
    node.dataset.timer = String(window.setTimeout(() => removeToast(node), ttl));
    say(title + (detail ? '. ' + truncate(detail, 160) : ''));
  }

  function removeToast(node) {
    if (!node || !node.parentNode) return;
    const timer = Number(node.dataset.timer || 0);
    if (timer) clearTimeout(timer);
    node.remove();
  }

  /* ---------------- Статусы ---------------- */

  const SERVICE_STATES = {
    active: { tone: 'ok', label: 'работает', severity: 'ok', icon: 'i-check' },
    running: { tone: 'ok', label: 'работает', severity: 'ok', icon: 'i-check' },
    activating: { tone: 'warn', label: 'запускается', severity: 'warn', icon: 'i-clock' },
    inactive: { tone: 'idle', label: 'остановлен', severity: 'idle', icon: 'i-minus' },
    exited: { tone: 'idle', label: 'остановлен', severity: 'idle', icon: 'i-minus' },
    dead: { tone: 'bad', label: 'ошибка', severity: 'bad', icon: 'i-x' },
    created: { tone: 'idle', label: 'создан, не запущен', severity: 'idle', icon: 'i-minus' },
    paused: { tone: 'idle', label: 'на паузе', severity: 'idle', icon: 'i-minus' },
    restarting: { tone: 'warn', label: 'перезапускается', severity: 'warn', icon: 'i-clock' },
    removing: { tone: 'warn', label: 'удаляется', severity: 'warn', icon: 'i-clock' },
    failed: { tone: 'bad', label: 'ошибка', severity: 'bad', icon: 'i-x' },
    absent: { tone: 'idle', label: 'не установлен', severity: 'absent', icon: 'i-minus' },
    'no-docker-socket': { tone: 'idle', label: 'нет Docker', severity: 'absent', icon: 'i-help' },
    unknown: { tone: 'idle', label: 'неизвестно', severity: 'unknown', icon: 'i-help' }
  };

  function serviceState(raw) {
    const key = String(raw || 'unknown');
    return SERVICE_STATES[key] || { tone: 'idle', label: key, severity: 'unknown', icon: 'i-help' };
  }

  function probeLabel(name) {
    return (PROBE_META[name] && PROBE_META[name].label) || name;
  }

  function analyze(status) {
    const problems = [];
    const warnings = [];
    const services = (status && status.services) || {};
    let okServices = 0;
    let okRequired = 0;
    let totalRequired = 0;
    let mihomoBad = false;

    for (const key of Object.keys(services)) {
      const meta = SERVICE_META[key];
      const st = serviceState(services[key]);
      if (st.severity === 'ok') okServices++;
      if (meta && meta.required) {
        totalRequired++;
        if (st.severity === 'ok') okRequired++;
      }
      if (!meta) continue;
      const isMihomo = key === 'mihomo';
      if (st.severity === 'bad') {
        if (meta.required) problems.push(meta.title + ': ' + st.label);
        else warnings.push(meta.title + ': ' + st.label);
        if (isMihomo) mihomoBad = true;
      } else if (st.severity === 'idle' && meta.required) {
        problems.push(meta.title + ': ' + st.label);
        if (isMihomo) mihomoBad = true;
      } else if (st.severity === 'warn' && meta.required) {
        warnings.push(meta.title + ': ' + st.label);
      } else if (st.severity === 'absent' && meta.required) {
        warnings.push(meta.title + ': ' + st.label);
      }
    }

    const results = (status && status.probes && status.probes.results) || {};
    let okProbes = 0;
    let failProbes = 0;
    let skipProbes = 0;
    let probesRan = 0;
    for (const name of Object.keys(results)) {
      const item = results[name] || {};
      probesRan++;
      if (item.skipped) skipProbes++;
      else if (item.ok) okProbes++;
      else {
        failProbes++;
        problems.push('проба «' + probeLabel(name) + '» не прошла');
      }
    }

    const apiOk = status && typeof status.mihomo_api === 'string' && status.mihomo_api === 'ok';
    if (status && !apiOk) warnings.push('mihomo API: ' + (status.mihomo_api || 'недоступен'));

    let tone = 'ok';
    let title = 'Шлюз работает';
    let desc = 'Все ключевые сервисы активны';

    if (problems.length) {
      tone = 'bad';
      title = mihomoBad ? 'Шлюз не работает' : 'Есть проблемы';
      desc = problems.slice(0, 3).join(' · ');
      if (problems.length > 3) desc += ' · и ещё ' + (problems.length - 3);
    } else if (warnings.length) {
      tone = 'warn';
      title = 'Шлюз работает, есть замечания';
      desc = warnings.slice(0, 3).join(' · ');
      if (warnings.length > 3) desc += ' · и ещё ' + (warnings.length - 3);
    } else if (probesRan === 0) {
      desc = 'Все ключевые сервисы активны · пробы путей ещё не запускались';
    } else {
      desc = 'Все ключевые сервисы активны · пробы путей проходят';
    }

    return {
      tone: tone, title: title, desc: desc,
      problems: problems, warnings: warnings,
      okServices: okServices, okRequired: okRequired,
      totalServices: Object.keys(services).length, totalRequired: totalRequired,
      okProbes: okProbes, failProbes: failProbes, skipProbes: skipProbes, probesRan: probesRan,
      apiOk: apiOk
    };
  }

  /* ---------------- Hero ---------------- */

  const HERO_ICONS = { ok: 'i-check', bad: 'i-x', warn: 'i-alert', idle: 'i-help', pending: 'i-spinner' };

  function renderHero() {
    const hero = $('#hero');
    const title = $('#hero-title');
    const desc = $('#hero-desc');
    const heroIcon = $('#hero-icon');
    const gwIp = refs.gwIp;

    if (state.authFailed) {
      hero.dataset.tone = 'bad';
      heroIcon.querySelector('svg').classList.remove('spin');
      setIcon(heroIcon.querySelector('svg'), 'i-lock');
      title.textContent = 'Нужна авторизация';
      desc.textContent = 'Обновите страницу и введите логин и пароль из /etc/routeweave/.env.';
      document.title = 'Routeweave — нужна авторизация';
      return;
    }

    if (!state.status) {
      hero.dataset.tone = state.offline ? 'bad' : 'pending';
      setIcon(heroIcon.querySelector('svg'), state.offline ? 'i-wifi-off' : 'i-spinner');
      heroIcon.querySelector('svg').classList.toggle('spin', !state.offline);
      title.textContent = state.offline ? 'Панель не отвечает' : 'Проверяем состояние…';
      desc.textContent = state.offline
        ? 'Не удалось получить статус. Проверьте, что панель запущена, и повторите попытку.'
        : 'Получаем данные от панели — это займёт пару секунд.';
      document.title = 'Routeweave — панель шлюза';
      return;
    }

    const info = analyze(state.status);
    hero.dataset.tone = info.tone;
    heroIcon.querySelector('svg').classList.remove('spin');
    setIcon(heroIcon.querySelector('svg'), HERO_ICONS[info.tone] || 'i-help');
    title.textContent = info.title;
    desc.textContent = state.offline ? 'Нет связи с панелью — данные могли устареть. ' + info.desc : info.desc;

    const svcFacts = info.totalRequired
      ? info.okRequired + '/' + info.totalRequired
      : info.okServices + '/' + info.totalServices;
    $('#fact-services').textContent = svcFacts;

    let probesFact = '—';
    if (info.probesRan) {
      probesFact = info.okProbes + ' OK';
      if (info.failProbes) probesFact += ' · ' + info.failProbes + ' FAIL';
      if (info.skipProbes) probesFact += ' · ' + info.skipProbes + ' проп.';
    } else if (state.probing) {
      probesFact = 'идут…';
    }
    $('#fact-probes').textContent = probesFact;

    const apiFact = $('#fact-api');
    apiFact.textContent = info.apiOk ? 'ok' : 'недоступен';
    apiFact.dataset.state = info.apiOk ? 'ok' : 'bad';

    $('#fact-updated').textContent = state.updatedAt
      ? state.updatedAt.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—';

    const cfg = state.status.config || {};
    if (cfg.server_ip) {
      gwIp.textContent = cfg.server_ip;
      gwIp.hidden = false;
    } else {
      gwIp.hidden = true;
    }

    document.title = info.tone === 'ok'
      ? 'Routeweave — панель шлюза'
      : 'Routeweave — ' + info.title.toLowerCase();
  }

  /* ---------------- Сервисы ---------------- */

  function buildServiceCard(key) {
    const meta = SERVICE_META[key] || { title: key, unit: key, hint: 'неизвестный сервис', api: null, icon: 'i-help' };
    const iconSvg = icon(meta.icon);
    const card = h('article', { class: 'svc-card', dataset: { tone: 'idle', service: key } },
      h('div', { class: 'svc-head' },
        h('span', { class: 'svc-icon' }, iconSvg),
        h('span', { class: 'svc-titles' },
          h('span', { class: 'svc-name', text: meta.title }),
          h('span', { class: 'svc-unit', text: meta.unit })
        )
      ),
      h('span', { class: 'svc-chip', dataset: { tone: 'idle' } },
        h('span', { class: 'svc-chip-icon' }, icon('i-help')),
        h('span', { class: 'svc-chip-text', text: 'неизвестно' })
      ),
      h('p', { class: 'svc-hint', text: meta.hint || '' }),
      h('div', { class: 'svc-foot' })
    );

    const foot = card.querySelector('.svc-foot');
    let btn = null;
    if (meta.api) {
      btn = h('button', {
        type: 'button', class: 'btn btn--sm',
        on: { click: () => restartService(key) }
      }, h('span', { class: 'btn-icon' }, icon('i-power')), h('span', { class: 'btn-label', text: 'Перезапустить' }));
      foot.appendChild(btn);
    }
    const note = h('span', { class: 'svc-note', hidden: true });
    foot.appendChild(note);

    return {
      card: card,
      chip: card.querySelector('.svc-chip'),
      chipIcon: card.querySelector('.svc-chip-icon svg'),
      chipText: card.querySelector('.svc-chip-text'),
      btn: btn,
      note: note,
      label: btn ? btn.querySelector('.btn-label') : null
    };
  }

  function skeletonServices(count) {
    const nodes = [];
    for (let i = 0; i < count; i++) {
      nodes.push(h('article', { class: 'svc-card' },
        h('div', { class: 'svc-head' },
          h('span', { class: 'svc-icon' }, icon('i-help')),
          h('span', { class: 'svc-titles' },
            h('span', { class: 'sk-line w-60' }),
            h('span', { class: 'sk-line w-40' })
          )
        ),
        h('span', { class: 'sk-line w-40' }),
        h('span', { class: 'sk-line w-80' })
      ));
    }
    return nodes;
  }

  function renderServices() {
    const box = refs.svcBox;
    const services = state.status && state.status.services;

    if (!services) {
      if (!box.children.length) {
        for (const node of skeletonServices(5)) box.appendChild(node);
      }
      return;
    }

    if (box.dataset.ready !== '1') {
      box.textContent = '';
      box.dataset.ready = '1';
    }

    const keys = SERVICE_ORDER.filter((k) => Object.prototype.hasOwnProperty.call(services, k));
    for (const key of Object.keys(services)) {
      if (keys.indexOf(key) === -1) keys.push(key);
    }

    const known = new Set(keys);
    for (const [key, entry] of refs.services) {
      if (!known.has(key)) {
        entry.card.remove();
        refs.services.delete(key);
      }
    }

    let previous = null;
    for (const key of keys) {
      let entry = refs.services.get(key);
      if (!entry) {
        entry = buildServiceCard(key);
        refs.services.set(key, entry);
      }
      const anchor = previous ? previous.nextSibling : box.firstChild;
      if (entry.card !== anchor) box.insertBefore(entry.card, anchor);
      previous = entry.card;

      const meta = SERVICE_META[key];
      const raw = services[key];
      const st = serviceState(raw);
      entry.card.dataset.tone = st.tone;
      entry.chip.dataset.tone = st.tone;
      setIcon(entry.chipIcon, st.icon);
      entry.chipText.textContent = st.label;
      entry.card.setAttribute('aria-label', (meta ? meta.title : key) + ': ' + st.label);

      let noteText = '';
      let canRestart = !!(meta && meta.api);
      if (key === 'routeweave-tgwsproxy') {
        if (raw === 'absent') { canRestart = false; noteText = 'контейнер не создан'; }
        else if (raw === 'no-docker-socket') { canRestart = false; noteText = 'нужен доступ к docker'; }
      } else if (key === 'tailscaled') {
        /* tailscaled нет в sudo allowlist — перезапуск из панели недоступен. */
        canRestart = false;
        const ts = state.status && state.status.tailscale;
        if (raw === 'absent') noteText = 'не установлен';
        else if (raw === 'active' && !(ts && ts.approved === true)) noteText = 'exit node не одобрен';
      } else if (meta && !meta.api) {
        noteText = 'systemctl restart ' + key;
      }

      if (entry.btn) {
        entry.btn.hidden = !canRestart;
        entry.btn.setAttribute('aria-label', 'Перезапустить: ' + (meta ? meta.title : key));
      }
      entry.note.hidden = !noteText;
      entry.note.textContent = noteText;
    }

    const active = Object.keys(services).filter((k) => serviceState(services[k]).severity === 'ok').length;
    const total = Object.keys(services).length;
    $('#services-meta').textContent = active + ' из ' + total + ' активны · systemd и docker';
  }

  async function restartService(key) {
    const meta = SERVICE_META[key];
    const entry = refs.services.get(key);
    if (!meta || !meta.api || !entry || !entry.btn) return;

    const isPanel = meta.api === 'panel';
    entry.btn.disabled = true;
    entry.btn.classList.add('is-busy');
    setIcon(entry.btn.querySelector('.icon'), 'i-spinner');
    if (entry.label) entry.label.textContent = 'Перезапуск…';

    try {
      const res = await api(API.restart(meta.api), { method: 'POST', timeout: 40000 });
      if (res && res.ok) {
        if (isPanel) {
          toast('warn', 'Панель перезапускается', 'Соединение прервётся на несколько секунд. Обновите страницу, если она не оживёт сама.');
          retryAfterPanelRestart();
        } else {
          toast('ok', meta.title + ': перезапуск выполнен', truncate(res.detail || '', 300));
        }
      } else {
        toast('bad', meta.title + ': не удалось перезапустить', truncate((res && res.detail) || 'сервис вернул ошибку', 400));
      }
    } catch (err) {
      if (isPanel && err.status === 0) {
        toast('warn', 'Панель перезапускается', 'Соединение прервалось — это ожидаемо. Подождите несколько секунд.');
        retryAfterPanelRestart();
      } else if (err.status !== 401) {
        toast('bad', meta.title + ': ошибка перезапуска', err.message);
      }
    } finally {
      entry.btn.disabled = false;
      entry.btn.classList.remove('is-busy');
      setIcon(entry.btn.querySelector('.icon'), 'i-power');
      if (entry.label) entry.label.textContent = 'Перезапустить';
      loadStatus({ quiet: true });
    }
  }

  async function retryAfterPanelRestart() {
    stopPolling();
    for (let i = 0; i < 12; i++) {
      await sleep(2500);
      if (state.authFailed) return;
      await loadStatus({ quiet: true });
      if (state.status && !state.offline) {
        startPolling();
        toast('ok', 'Панель снова доступна', 'Данные обновлены.');
        return;
      }
    }
    startPolling();
  }

  /* ---------------- Пробы ---------------- */

  function renderProbeSkeleton() {
    const body = refs.probeBody;
    body.textContent = '';
    const names = PROBE_ORDER.slice(0, 5);
    for (const name of names) {
      body.appendChild(h('tr', { 'aria-hidden': 'true' },
        h('td', null,
          h('span', { class: 'probe-name', text: probeLabel(name) }),
          h('span', { class: 'probe-key', text: name })
        ),
        h('td', null, h('span', { class: 'sk-line w-60' })),
        h('td', null, h('span', { class: 'sk-line w-80' }))
      ));
    }
    refs.probeTable.setAttribute('aria-busy', 'true');
  }

  function renderProbes() {
    const body = refs.probeBody;
    const data = state.probes;

    if (state.probing) {
      renderProbeSkeleton();
      return;
    }

    refs.probeTable.removeAttribute('aria-busy');
    body.textContent = '';

    if (!state.status && !data) {
      renderProbeSkeleton();
      return;
    }

    const results = (data && data.results) || {};
    const names = PROBE_ORDER.filter((n) => Object.prototype.hasOwnProperty.call(results, n));
    for (const n of Object.keys(results)) {
      if (names.indexOf(n) === -1) names.push(n);
    }

    if (!names.length) {
      body.appendChild(h('tr', null,
        h('td', { colspan: '3', class: 'empty' },
          h('span', { text: 'Пробы ещё не запускались — нажмите «Запустить пробы», чтобы проверить пути.' })
        )
      ));
    }

    for (const name of names) {
      const item = results[name] || {};
      const skipped = !!item.skipped;
      const ok = !!item.ok;
      const tone = skipped ? 'idle' : (ok ? 'ok' : 'bad');
      const label = skipped ? 'пропущено' : (ok ? 'OK' : 'FAIL');
      const badgeIcon = skipped ? 'i-minus' : (ok ? 'i-check' : 'i-x');

      body.appendChild(h('tr', null,
        h('td', null,
          h('span', { class: 'probe-name', text: probeLabel(name) }),
          h('span', { class: 'probe-key', text: name })
        ),
        h('td', null,
          h('span', { class: 'badge', dataset: { tone: tone } }, icon(badgeIcon), h('span', { text: label }))
        ),
        h('td', null,
          h('span', { class: 'probe-detail', text: item.detail || '—' })
        )
      ));
    }

    renderProbeTimestamp();
  }

  function renderProbeTimestamp() {
    const el = refs.probeTs;
    if (state.probing) return;
    const ts = state.probes && state.probes.ts;
    if (!ts) {
      el.textContent = 'пробы ещё не запускались';
      return;
    }
    el.textContent = 'последний прогон: ' + ts;
  }

  function startProbeTicker() {
    state.probeStartedAt = Date.now();
    stopProbeTicker();
    probeTicker = window.setInterval(() => {
      const secs = Math.round((Date.now() - state.probeStartedAt) / 1000);
      refs.probeTs.textContent = 'выполняется · ' + secs + ' с';
    }, 1000);
    refs.probeTs.textContent = 'выполняется · 0 с';
  }

  function stopProbeTicker() {
    if (probeTicker) {
      clearInterval(probeTicker);
      probeTicker = null;
    }
  }

  async function runProbes() {
    if (state.probing) return;
    state.probing = true;

    refs.probeBtn.disabled = true;
    refs.probeBtn.classList.add('is-busy');
    refs.probeBtnLabel.textContent = 'Идут пробы…';
    setIcon(refs.probeBtnIcon ? refs.probeBtnIcon.querySelector('svg') : null, 'i-spinner');
    refs.probeTable.setAttribute('aria-busy', 'true');
    renderProbes();
    startProbeTicker();
    say('Идут пробы путей');

    try {
      const res = await api(API.probe, { method: 'POST', timeout: PROBE_TIMEOUT_MS });
      if (res && res.results) {
        state.probes = { ts: res.ts, results: res.results };
      }
      const results = (state.probes && state.probes.results) || {};
      const names = Object.keys(results);
      const failed = names.filter((n) => !results[n].skipped && !results[n].ok).length;
      const okCount = names.filter((n) => !results[n].skipped && results[n].ok).length;
      if (failed) {
        toast('warn', 'Пробы завершены: ' + failed + ' ' + plural(failed, 'проблема', 'проблемы', 'проблем'),
          'Успешно: ' + okCount + ' из ' + names.length);
      } else {
        toast('ok', 'Пробы завершены', 'Все проверенные пути отвечают (' + okCount + ' OK).');
      }
    } catch (err) {
      if (err.status !== 401) {
        toast('bad', 'Не удалось выполнить пробы', err.message);
      }
    } finally {
      state.probing = false;
      refs.probeBtn.disabled = false;
      refs.probeBtn.classList.remove('is-busy');
      refs.probeBtnLabel.textContent = 'Запустить пробы';
      setIcon(refs.probeBtnIcon ? refs.probeBtnIcon.querySelector('svg') : null, 'i-play');
      stopProbeTicker();
      renderProbes();
      renderHero();
    }
  }

  /* ---------------- Редактор списков ---------------- */

  function listState(id) {
    if (!state.lists[id]) {
      state.lists[id] = { saved: '', draft: '', loaded: false, loading: false, error: null, dirty: false };
    }
    return state.lists[id];
  }

  function isDirty(id) {
    const item = state.lists[id];
    return !!item && item.loaded && item.draft !== item.saved;
  }

  function initLists() {
    for (const id of LIST_ORDER) listState(id);
  }

  function selectList(id, options) {
    if (!LIST_META[id] || id === state.currentList) return;
    state.currentList = id;
    state.editorMessage = null;
    renderTabs();
    renderEditor();
    if (!listState(id).loaded) loadList(id);
    if (options && options.focusEditor) refs.editor.focus();
  }

  function renderTabs() {
    const tabs = document.querySelectorAll('.tab');
    for (const tab of tabs) {
      const id = tab.dataset.list;
      const selected = id === state.currentList;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      const dot = document.getElementById('dirty-' + id);
      if (dot) dot.hidden = !isDirty(id);
    }
    const panel = document.getElementById('list-panel');
    if (panel) panel.setAttribute('aria-labelledby', 'tab-' + state.currentList);
  }

  function renderEditor() {
    const id = state.currentList;
    const meta = LIST_META[id];
    const item = listState(id);

    refs.listFile.textContent = meta.file;
    refs.listRoute.textContent = meta.route;
    refs.listDesc.textContent = meta.desc;
    refs.applyHint.textContent = 'Ctrl+S — сохранить. ' + meta.applyHint;

    if (refs.editor.value !== item.draft) refs.editor.value = item.draft;
    refs.editor.readOnly = state.applying || item.loading;
    if (!state.applying) refs.applyBtn.disabled = !item.loaded;

    renderEditorMeta();
  }

  function renderEditorMeta() {
    const id = state.currentList;
    const item = listState(id);
    const text = item.draft || '';
    const lines = text.length ? text.split('\n') : [];
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    const nonEmpty = lines.filter((line) => line.trim() !== '').length;
    refs.counts.textContent = 'Строк: ' + lines.length + ' · непустых: ' + nonEmpty + ' · Символов: ' + text.length;

    const dirty = isDirty(id);
    refs.dirtyPill.hidden = !dirty;
    renderTabs();

    if (state.applying) return;
    if (state.saving) {
      setEditorState('warn', 'Сохраняем…');
      return;
    }
    if (item.loading) {
      setEditorState('warn', 'Загрузка списка…');
      return;
    }
    if (item.error) {
      setEditorState('bad', 'Не удалось загрузить список: ' + item.error);
      return;
    }
    if (state.editorMessage) {
      setEditorState(state.editorMessage.tone, state.editorMessage.text);
      return;
    }
    if (dirty) {
      setEditorState('warn', 'Есть несохранённые изменения');
      return;
    }
    setEditorState('ok', item.loaded ? 'Все изменения сохранены' : 'Список не загружен');
  }

  function setEditorState(tone, text) {
    refs.editorState.dataset.tone = tone || '';
    refs.editorState.textContent = text || '';
  }

  async function loadList(id) {
    const item = listState(id);
    if (item.loading) return;
    item.loading = true;
    item.error = null;
    if (id === state.currentList) renderEditorMeta();

    try {
      const res = await api(API.list(id), { timeout: 15000 });
      const content = res && typeof res.content === 'string' ? res.content : '';
      const wasDirty = item.loaded && item.draft !== item.saved;
      item.saved = content;
      if (!wasDirty) item.draft = content;
      item.loaded = true;
    } catch (err) {
      if (err.status !== 401) {
        item.error = err.message;
        if (id === state.currentList) {
          toast('bad', 'Не удалось загрузить ' + LIST_META[id].file, err.message);
        }
      }
    } finally {
      item.loading = false;
      if (id === state.currentList) renderEditor();
    }
  }

  function onEditorInput() {
    const item = listState(state.currentList);
    item.draft = refs.editor.value;
    state.editorMessage = null;
    renderEditorMeta();
    updateBeforeUnload();
  }

  async function saveList(options) {
    if (state.applying || state.saving) return false;
    const id = state.currentList;
    const meta = LIST_META[id];
    const item = listState(id);
    if (!item.loaded) {
      toast('warn', 'Список ещё не загружен', 'Дождитесь загрузки и повторите.');
      return false;
    }

    state.saving = true;
    state.editorMessage = null;
    refs.saveBtn.disabled = true;
    renderEditorMeta();
    let ok = false;

    try {
      const res = await api(API.list(id), { method: 'PUT', body: item.draft, timeout: 30000 });
      item.saved = item.draft;
      ok = true;
      if (!(options && options.silent)) {
        toast('ok', meta.file + ' сохранён', res && res.bytes ? 'Записано ' + fmtBytes(res.bytes) + '. Не забудьте применить.' : 'Не забудьте применить изменения.');
      }
      state.editorMessage = { tone: 'ok', text: 'Сохранено · ' + timeNow() };
    } catch (err) {
      if (err.status === 401) {
        state.editorMessage = { tone: 'bad', text: 'Требуется авторизация' };
      } else {
        state.editorMessage = { tone: 'bad', text: 'Ошибка сохранения: ' + truncate(err.message, 160) };
        toast('bad', 'Не удалось сохранить ' + meta.file, err.message);
      }
      ok = false;
    } finally {
      state.saving = false;
      refs.saveBtn.disabled = false;
      renderEditorMeta();
      updateBeforeUnload();
    }

    return ok;
  }

  function startApplyTicker() {
    state.applyStartedAt = Date.now();
    stopApplyTicker();
    applyTicker = window.setInterval(() => {
      if (!state.applying) return;
      const secs = Math.round((Date.now() - state.applyStartedAt) / 1000);
      setEditorState('warn', 'Применяется… ' + secs + ' с');
    }, 1000);
  }

  function stopApplyTicker() {
    if (applyTicker) {
      clearInterval(applyTicker);
      applyTicker = null;
    }
  }

  function setApplyBusy(busy, label) {
    state.applying = busy;
    refs.applyBtn.disabled = busy || !listState(state.currentList).loaded;
    refs.saveBtn.disabled = busy;
    refs.applyBtn.classList.toggle('is-busy', busy);
    setIcon(refs.applyIcon ? refs.applyIcon.querySelector('svg') : null, busy ? 'i-spinner' : 'i-refresh');
    if (label) refs.applyLabel.textContent = label;
    refs.editor.readOnly = busy;
    const tabs = document.querySelectorAll('.tab');
    for (const tab of tabs) tab.disabled = busy;
  }

  function finishApply(ok, detail, toneOverride) {
    const meta = LIST_META[state.currentList];
    const tone = toneOverride || (ok ? 'ok' : 'bad');
    if (tone === 'ok') {
      toast('ok', 'Списки применены', detail ? truncate(detail, 400) : meta.applyHint);
      state.editorMessage = { tone: 'ok', text: 'Применено · ' + timeNow() };
    } else if (tone === 'warn') {
      toast('warn', 'Списки применены, но есть проблемы', truncate(detail || 'проверьте статус сервисов', 500));
      state.editorMessage = { tone: 'warn', text: 'Применено · есть проблемы' };
    } else {
      toast('bad', 'Не удалось применить списки', truncate(detail || 'проверьте статус сервисов', 500));
      state.editorMessage = { tone: 'bad', text: 'Ошибка применения: ' + truncate(detail || 'см. уведомление', 120) };
    }
  }

  async function pollApplyJob(baselineTs) {
    const started = Date.now();
    let jobMissing = false;
    setEditorState('warn', 'Применяется… 0 с');

    for (;;) {
      await sleep(JOB_POLL_MS);
      if (Date.now() - started > SYNC_TIMEOUT_MS) {
        finishApply(false, 'истекло время ожидания применения (3 минуты)');
        return;
      }

      let job;
      try {
        job = await api(API.job, { timeout: 10000 });
      } catch (err) {
        if (err.status === 404) {
          jobMissing = true;
          break;
        }
        if (err.status === 401) return;
        throw err;
      }

      if (job && !job.running) {
        const detail = job.detail || (
          job.started_at
            ? 'запуск ' + job.started_at + (job.finished_at ? ' → ' + job.finished_at : '')
            : ''
        );
        finishApply(job.ok !== false, detail);
        return;
      }
    }

    if (jobMissing) await syncMode(baselineTs);
  }

  /* Старый бэкенд без /api/jobs/apply: ждём, пока обновится отметка проб в /api/status. */
  async function syncMode(baselineTs) {
    const started = Date.now();
    setEditorState('warn', 'Синхронизация… 0 с');

    while (Date.now() - started < SYNC_TIMEOUT_MS) {
      await sleep(2000);
      if (Date.now() - started > SYNC_TIMEOUT_MS) break;
      await loadStatus({ quiet: true });
      if (state.authFailed) return;
      const ts = state.status && state.status.probes ? state.status.probes.ts : null;
      if (ts && ts !== baselineTs) {
        const info = analyze(state.status);
        if (info.problems.length === 0) {
          finishApply(true, 'Списки обновлены, сервисы активны (mihomo перечитал правила).');
        } else {
          finishApply(true, 'Списки обновлены, но есть проблемы: ' + info.problems.slice(0, 2).join(' · '), 'warn');
        }
        return;
      }
      const secs = Math.round((Date.now() - started) / 1000);
      setEditorState('warn', 'Синхронизация… ' + secs + ' с');
    }

    const info = analyze(state.status);
    finishApply(false, info.problems.length
      ? 'Применение не подтвердилось. Проблемы: ' + info.problems.slice(0, 2).join(' · ')
      : 'Применение не подтвердилось за 3 минуты, но сервисы активны — проверьте списки вручную.');
  }

  async function applyList() {
    if (state.applying) return;
    const id = state.currentList;
    const item = listState(id);
    if (!item.loaded) {
      toast('warn', 'Список ещё не загружен', 'Дождитесь загрузки и повторите.');
      return;
    }

    const baselineTs = (state.probes && state.probes.ts) || null;
    setApplyBusy(true, 'Сохраняем…');
    startApplyTicker();
    say('Сохранение и применение списка ' + LIST_META[id].file);

    try {
      await api(API.list(id), { method: 'PUT', body: item.draft, timeout: 30000 });
      item.saved = item.draft;
      state.editorMessage = null;
      renderEditorMeta();

      setApplyBusy(true, 'Применяется…');
      const res = await api(API.apply(id), { method: 'POST', timeout: APPLY_TIMEOUT_MS });

      if (res && res.started === true) {
        await pollApplyJob(baselineTs);
      } else if (res && typeof res.ok === 'boolean') {
        finishApply(res.ok, res.detail);
      } else {
        await syncMode(baselineTs);
      }
    } catch (err) {
      if (err.status === 401) {
        state.editorMessage = { tone: 'bad', text: 'Требуется авторизация' };
      } else {
        finishApply(false, err.message);
      }
    } finally {
      stopApplyTicker();
      setApplyBusy(false, 'Сохранить и применить');
      renderEditorMeta();
      updateBeforeUnload();
      loadStatus({ quiet: true });
    }
  }

  /* ---------------- Конфигурация ---------------- */

  function renderConfig() {
    const st = state.status;
    const cfg = ((st && st.config) || {});
    const list = refs.configList;
    if (!st) return;
    list.textContent = '';

    const rows = [
      { label: 'IP сервера', value: cfg.server_ip || '—', mono: true },
      { label: 'SOCKS-прокси', value: cfg.socks || '—' },
      { label: 'BASE_DOMAIN', value: cfg.base_domain || '—', mono: true },
      { label: 'WG-подсеть (WDTT)', value: cfg.wg_subnet || '—', mono: true },
      { label: 'QUIC DROP', value: cfg.quic_drop, bool: true },
      { label: 'Telegram-прокси', boolValue: cfg.tgws_enabled === true, yes: 'включён', no: 'выключен' },
      { label: 'mihomo API', value: (st && st.mihomo_api) || '—', tone: st && st.mihomo_api === 'ok' ? 'ok' : 'warn' }
    ];
    if (cfg.tg_link_ip) {
      rows.splice(6, 0, { label: 'IP для Telegram', value: cfg.tg_link_ip, mono: true });
    }

    for (const row of rows) {
      const dd = h('dd', { class: row.mono ? 'mono' : undefined });
      if (row.boolValue !== undefined) {
        dd.appendChild(h('span', { class: 'badge', dataset: { tone: row.boolValue ? 'ok' : 'idle' } },
          icon(row.boolValue ? 'i-check' : 'i-minus'),
          h('span', { text: row.boolValue ? row.yes : row.no })
        ));
      } else if (row.bool) {
        const isYes = String(row.value).toLowerCase() === 'yes';
        dd.appendChild(h('span', { class: 'badge', dataset: { tone: isYes ? 'ok' : 'idle' } },
          icon(isYes ? 'i-check' : 'i-minus'),
          h('span', { text: isYes ? 'включён' : (row.value === 'no' ? 'выключен' : (row.value || '—')) })
        ));
      } else if (row.tone) {
        dd.appendChild(h('span', { class: 'badge', dataset: { tone: row.tone } }, h('span', { text: row.value })));
      } else {
        dd.textContent = row.value;
      }
      list.appendChild(h('div', { class: 'kv-row' }, h('dt', { text: row.label }), dd));
    }

    renderTelegram((st && st.config) || {});
  }

  function renderTelegram(cfg) {
    if (!cfg.tgws_enabled) {
      refs.tgCard.hidden = true;
      refs.tgList.textContent = '';
      return;
    }

    const host = cfg.tg_link_ip || cfg.server_ip || '';
    const items = [];

    if (host) {
      items.push({ label: 'Домашняя сеть (LAN)', value: host + ':1443' });
    }
    const wgHost = wgHostFromSubnet(cfg.wg_subnet);
    if (wgHost) {
      items.push({ label: 'WDTT (расчётный адрес)', value: wgHost + ':1443' });
    }
    const ts = state.status && state.status.tailscale;
    const tsIp = ts && typeof ts.self_ip === 'string' ? ts.self_ip : '';
    items.push({ label: 'Tailscale', value: tsIp ? tsIp + ':1443' : 'имя-сервера-в-tailscale:1443', copy: !!tsIp });
    items.push({ label: 'Секрет', value: 'docker logs routeweave-tgwsproxy', mono: true, copy: false });

    const deepLink = typeof cfg.tg_link === 'string' && cfg.tg_link.indexOf('tg://') === 0
      ? cfg.tg_link
      : '';
    refs.tgList.textContent = '';
    for (const item of items) {
      const copyable = item.copy !== false && /^[\w.:\-\[\]]+$/.test(item.value);
      const btn = copyable
        ? h('button', {
            type: 'button', class: 'copy-btn', 'aria-label': 'Скопировать: ' + item.label,
            dataset: { copy: item.value }, on: { click: onCopyClick }
          }, icon('i-copy'))
        : null;
      refs.tgList.appendChild(h('li', { class: 'copy-item' },
        h('div', null,
          h('span', { class: 'copy-label', text: item.label }),
          h('span', { class: 'copy-value', text: item.value })
        ),
        btn
      ));
    }
    refs.tgOpen.hidden = !deepLink;
    refs.tgSecretHint.hidden = !deepLink;
    if (deepLink) {
      refs.tgOpen.setAttribute('href', deepLink);
      refs.tgOpen.setAttribute('aria-label', 'Открыть ссылку прокси в Telegram');
    } else {
      refs.tgOpen.removeAttribute('href');
    }
    refs.tgCard.hidden = false;
  }

  function wgHostFromSubnet(subnet) {
    if (!subnet || typeof subnet !== 'string') return '';
    const match = subnet.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\d{1,3}(\/\d{1,2})?$/);
    if (!match) return '';
    return match[1] + '.' + match[2] + '.' + match[3] + '.1';
  }

  /* ---------------- Tailscale ---------------- */

  /* Карточка #ts-card строится один раз: на каждом опросе обновляются только
     textContent, dataset и hidden, поэтому список не мигает и не пересоздаётся. */
  let tsView = null;

  function tsRow(list, label, mono) {
    const body = h('span', { class: mono ? 'copy-value' : undefined });
    const btn = h('button', {
      type: 'button', class: 'copy-btn', hidden: true,
      'aria-label': 'Скопировать: ' + label,
      on: { click: onCopyClick }
    }, icon('i-copy'));
    const li = h('li', { class: 'copy-item' },
      h('div', null, h('span', { class: 'copy-label', text: label }), body),
      btn
    );
    list.appendChild(li);
    return { li: li, body: body, btn: btn };
  }

  function tsBadge() {
    const badgeIcon = icon('i-help');
    const badgeText = h('span', { text: '—' });
    const node = h('span', { class: 'badge', dataset: { tone: 'idle' } }, badgeIcon, badgeText);
    return { node: node, icon: badgeIcon, text: badgeText };
  }

  function ensureTailscaleView() {
    if (tsView) return tsView;
    const list = refs.tsList;
    if (!list || !refs.tsCard) return null;

    list.textContent = '';
    const daemonRow = tsRow(list, 'tailscaled');
    const daemon = tsBadge();
    daemonRow.body.appendChild(daemon.node);

    const ipRow = tsRow(list, 'tailnet-адрес', true);

    const exitRow = tsRow(list, 'Exit node');
    const exit = tsBadge();
    exitRow.body.appendChild(exit.node);

    const traffic = tsRow(list, 'Трафик', true);
    const speed = tsRow(list, 'Скорость', true);
    const peers = tsRow(list, 'Пиры', true);
    const error = tsRow(list, 'Ошибка');

    tsView = {
      daemon: daemon,
      ip: ipRow,
      exit: exit,
      traffic: traffic,
      speed: speed,
      peers: peers,
      error: error
    };
    return tsView;
  }

  function renderTailscale() {
    const card = refs.tsCard;
    if (!card) return;

    const status = state.status;
    const ts = status && status.tailscale;
    if (!ts || ts.installed !== true) {
      card.hidden = true;
      return;
    }

    const view = ensureTailscaleView();
    if (!view) return;

    const daemonKey = typeof ts.daemon === 'string' && ts.daemon ? ts.daemon : 'unknown';
    const daemonState = serviceState(daemonKey);
    view.daemon.node.dataset.tone = daemonState.tone;
    setIcon(view.daemon.icon, daemonState.icon);
    view.daemon.text.textContent = daemonState.label;

    const selfIp = typeof ts.self_ip === 'string' ? ts.self_ip : '';
    view.ip.body.textContent = selfIp || '—';
    view.ip.btn.hidden = !selfIp;
    view.ip.btn.dataset.copy = selfIp;
    view.ip.btn.setAttribute('aria-label', selfIp ? 'Скопировать tailnet-адрес: ' + selfIp : 'Скопировать tailnet-адрес');

    const advertised = ts.advertised === true;
    const approved = ts.approved === true;
    let exitLabel = 'не анонсирован';
    let exitTone = 'idle';
    let exitIcon = 'i-minus';
    if (advertised && approved) {
      exitLabel = 'анонсирован и одобрен';
      exitTone = 'ok';
      exitIcon = 'i-check';
    } else if (advertised) {
      exitLabel = 'ждёт одобрения';
      exitTone = 'warn';
      exitIcon = 'i-clock';
    }
    view.exit.node.dataset.tone = exitTone;
    setIcon(view.exit.icon, exitIcon);
    view.exit.text.textContent = exitLabel;

    view.traffic.body.textContent = '↓ ' + fmtBytesOrDash(ts.rx_bytes) + ' · ↑ ' + fmtBytesOrDash(ts.tx_bytes);
    view.speed.body.textContent = '↓ ' + fmtBytesOrDash(ts.rx_rate) + '/с · ↑ ' + fmtBytesOrDash(ts.tx_rate) + '/с';

    const peers = typeof ts.peers === 'number' ? ts.peers : 0;
    view.peers.li.hidden = !(peers > 0);
    view.peers.body.textContent = peers > 0
      ? String(peers) + ' ' + plural(peers, 'пир', 'пира', 'пиров')
      : '';

    const errorText = typeof ts.error === 'string' ? ts.error : '';
    view.error.li.hidden = !errorText;
    view.error.body.textContent = errorText ? truncate(errorText, 300) : '';

    card.hidden = false;
  }

  function onCopyClick(event) {
    const btn = event.currentTarget;
    const value = btn.dataset.copy || '';
    copyText(value).then((ok) => {
      if (ok) toast('ok', 'Скопировано', value);
      else toast('warn', 'Не удалось скопировать', 'Выделите значение вручную.');
    });
  }

  async function copyText(value) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (err) { /* переходим к запасному способу */ }

    const area = h('textarea', { class: 'copy-helper', readonly: true, 'aria-hidden': 'true', tabindex: '-1' });
    area.value = value;
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    area.remove();
    return ok;
  }

  /* ---------------- Статус ---------------- */

  async function loadStatus(options) {
    const quiet = !!(options && options.quiet);
    if (statusInFlight) return;
    statusInFlight = true;
    if (!quiet) refs.updatedAt.textContent = 'обновляем…';

    try {
      const data = await api(API.status, { timeout: 15000 });
      state.status = data;
      state.updatedAt = new Date();
      if (!state.probing && data.probes) {
        state.probes = { ts: data.probes.ts || null, results: data.probes.results || {} };
      }
      refs.updatedAt.textContent = 'обновлено в ' + timeNow();
      renderServices();
      renderConfig();
      renderTailscale();
      renderProbes();
      renderHero();
    } catch (err) {
      if (err.status !== 401) {
        markOffline();
        renderHero();
        if (!quiet) toast('bad', 'Не удалось получить статус', err.message);
      }
    } finally {
      statusInFlight = false;
    }
  }

  function startPolling() {
    stopPolling();
    if (state.authFailed) return;
    statusTimer = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return;
      if (state.applying) return;
      loadStatus({ quiet: true });
    }, POLL_MS);
  }

  function stopPolling() {
    if (statusTimer) {
      clearInterval(statusTimer);
      statusTimer = null;
    }
  }

  /* ---------------- Тема ---------------- */

  function readStoredTheme() {
    try {
      const value = localStorage.getItem(THEME_KEY);
      if (value === 'dark' || value === 'light') return value;
    } catch (err) { /* localStorage может быть недоступен */ }
    return null;
  }

  function applyTheme(theme, persist) {
    document.documentElement.dataset.theme = theme;
    if (persist) {
      try { localStorage.setItem(THEME_KEY, theme); } catch (err) { /* игнорируем */ }
    }
    if (refs.themeIcon) refs.themeIcon.setAttribute('href', theme === 'dark' ? '#i-sun' : '#i-moon');
    if (refs.themeBtn) {
      refs.themeBtn.setAttribute('aria-label', theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему');
      refs.themeBtn.setAttribute('title', theme === 'dark' ? 'Светлая тема' : 'Тёмная тема');
    }
  }

  function toggleTheme() {
    const current = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
    applyTheme(current === 'dark' ? 'light' : 'dark', true);
  }

  function initTheme() {
    const stored = readStoredTheme();
    const systemLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    applyTheme(stored || (systemLight ? 'light' : 'dark'), false);
    if (!stored && window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: light)');
      const onChange = (event) => {
        if (readStoredTheme()) return;
        applyTheme(event.matches ? 'light' : 'dark', false);
      };
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  /* ---------------- Вкладки ---------------- */

  function bindTabs() {
    const tabs = Array.from(document.querySelectorAll('.tab'));
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => selectList(tab.dataset.list));
      tab.addEventListener('keydown', (event) => {
        let next = null;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        if (next === null) return;
        event.preventDefault();
        const target = tabs[next];
        selectList(target.dataset.list);
        target.focus();
      });
    });
  }

  /* ---------------- Прочее ---------------- */

  function updateBeforeUnload() {
    const anyDirty = LIST_ORDER.some((id) => isDirty(id));
    if (anyDirty) {
      window.onbeforeunload = function (event) {
        event.preventDefault();
        event.returnValue = '';
        return '';
      };
    } else {
      window.onbeforeunload = null;
    }
  }

  function refreshAll() {
    const btn = $('#refresh-btn');
    const svg = btn.querySelector('.icon');
    svg.classList.add('spin');
    btn.disabled = true;
    Promise.all([
      loadStatus(),
      isDirty(state.currentList) ? Promise.resolve() : loadList(state.currentList)
    ]).finally(() => {
      svg.classList.remove('spin');
      btn.disabled = false;
    });
  }

  function bindEvents() {
    refs.themeBtn.addEventListener('click', toggleTheme);
    $('#refresh-btn').addEventListener('click', refreshAll);
    $('#net-retry').addEventListener('click', () => { loadStatus(); });
    $('#auth-reload').addEventListener('click', () => { window.location.reload(); });

    refs.probeBtn.addEventListener('click', runProbes);
    refs.saveBtn.addEventListener('click', () => { saveList(); });
    refs.applyBtn.addEventListener('click', applyList);
    refs.editor.addEventListener('input', onEditorInput);

    refs.editor.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S')) {
        event.preventDefault();
        saveList();
      }
    });

    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented) return;
      if ((event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S')) {
        const tag = (event.target && event.target.tagName) || '';
        if (tag === 'TEXTAREA' || tag === 'INPUT') return;
        event.preventDefault();
        saveList();
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && !state.authFailed) {
        loadStatus({ quiet: true });
        startPolling();
      }
    });
  }

  function collectRefs() {
    refs.svcBox = $('#services');
    refs.probeBody = $('#probe-body');
    refs.probeTable = $('#probe-table');
    refs.probeTs = $('#probes-ts');
    refs.probeBtn = $('#probe-run');
    refs.probeBtnLabel = $('#probe-run-label');
    refs.probeBtnIcon = $('#probe-run-icon');
    refs.editor = $('#list-editor');
    refs.counts = $('#list-counts');
    refs.editorState = $('#editor-state');
    refs.dirtyPill = $('#list-dirty');
    refs.listFile = $('#list-file');
    refs.listRoute = $('#list-route');
    refs.listDesc = $('#list-desc');
    refs.applyBtn = $('#list-apply');
    refs.applyLabel = $('#apply-label');
    refs.applyIcon = $('#apply-icon');
    refs.saveBtn = $('#list-save');
    refs.applyHint = $('#apply-hint');
    refs.tgCard = $('#tg-card');
    refs.tgList = $('#tg-list');
    refs.tgOpen = $('#tg-open');
    refs.tgSecretHint = $('#tg-secret-hint');
    refs.tsCard = $('#ts-card');
    refs.tsList = $('#ts-list');
    refs.configList = $('#config-list');
    refs.gwIp = $('#gw-ip');
    refs.updatedAt = $('#updated-at');
    refs.toasts = $('#toasts');
    refs.themeIcon = $('#theme-icon');
    refs.themeBtn = $('#theme-toggle');
    refs.a11y = $('#a11y-status');
    refs.netBanner = $('#net-banner');
    refs.authBanner = $('#auth-banner');
  }

  function init() {
    collectRefs();
    initTheme();
    initLists();
    bindEvents();
    bindTabs();

    refs.updatedAt.textContent = 'загрузка…';
    renderServices();
    renderProbes();
    renderHero();
    renderConfig();
    renderTailscale();
    renderTabs();
    renderEditor();

    loadStatus();
    loadList(state.currentList);
    startPolling();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
