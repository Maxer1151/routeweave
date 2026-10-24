"use strict";

const PROBE_NAMES = {
  chatgpt_via_socks: "chatgpt.com через SOCKS",
  socks_exit_ip: "SOCKS exit IP",
  youtube_direct: "youtube (direct + zapret)",
  tg_ws_proxy: "tg-ws-proxy :1443",
  cf_worker: "CF Worker (BASE_DOMAIN)"
};

const SVC_NAMES = {
  mihomo: "mihomo (шлюз)",
  "zapret-nfqws": "zapret (DPI)",
  "routeweave-tgwsproxy": "tg-ws-proxy (docker, опционально)",
  "routeweave-firewall": "firewall",
  "routeweave-panel": "панель"
};

const RESTART_TARGET = {
  mihomo: "mihomo",
  "zapret-nfqws": "zapret",
  "routeweave-panel": "panel",
  "routeweave-tgwsproxy": "tgwsproxy"
};

const APPLY_LABELS = {
  "geo-custom": "Сохранить, обновить списки и mihomo",
  dpi: "Сохранить и перезапустить zapret",
  "ru-custom": "Сохранить, обновить списки и mihomo"
};

const STATUS_POLL_MS = 20000;
const JOB_POLL_MS = 2000;
const TOAST_MS = 5000;

let currentList = "geo-custom";
let savedContent = "";
let jobTimer = null;
let toastTimer = null;

function byId(id) {
  return document.getElementById(id);
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function toast(text, ok = true) {
  const box = byId("msg");
  box.textContent = String(text);
  box.classList.toggle("error", !ok);
  box.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => box.classList.remove("show"), TOAST_MS);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const raw = await response.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (err) {
      data = null;
    }
  }
  if (!response.ok) {
    let detail = "";
    if (data && data.detail !== undefined && data.detail !== null) {
      detail = Array.isArray(data.detail) ? data.detail.map((item) => String(item)).join("; ") : String(data.detail);
    } else {
      detail = raw.slice(0, 300);
    }
    const error = new Error("HTTP " + response.status + (detail ? ": " + detail : ""));
    error.status = response.status;
    throw error;
  }
  return data || {};
}

function dotClass(state) {
  if (state === "active" || state === "running") {
    return "dot ok";
  }
  if (state === "unknown" || state === "exited" || state === "absent") {
    return "dot warn";
  }
  return "dot bad";
}

function renderServices(services) {
  const box = byId("svc");
  box.replaceChildren();
  Object.keys(services).forEach((key) => {
    const state = String(services[key] === undefined ? "unknown" : services[key]);
    const card = make("div", "card");
    const title = make("div", "name");
    title.append(make("span", dotClass(state)), make("span", "", SVC_NAMES[key] || key));
    const status = make("div", "st", "состояние: " + state);
    const button = make("button", "", "Перезапустить");
    button.type = "button";
    const target = RESTART_TARGET[key];
    if (!target) {
      button.disabled = true;
    }
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api("/api/restart/" + encodeURIComponent(target), { method: "POST" });
        toast(result.detail ? "Перезапущено: " + result.detail : "Перезапущено", true);
      } catch (err) {
        toast("Ошибка перезапуска: " + err.message, false);
      }
      button.disabled = false;
      loadStatus();
    });
    card.append(title, status, button);
    box.append(card);
  });
}

function renderProbes(data) {
  byId("probeTs").textContent = data && data.ts ? "последний прогон: " + data.ts : "пробы ещё не запускались";
  const body = byId("probeBody");
  body.replaceChildren();
  const results = (data && data.results) || {};
  Object.keys(results).forEach((key) => {
    const item = results[key] || {};
    const row = make("tr");
    let badgeClass = "b-bad";
    let label = "FAIL";
    if (item.skipped) {
      badgeClass = "b-skip";
      label = "пропущено";
    } else if (item.ok) {
      badgeClass = "b-ok";
      label = "OK";
    }
    const badgeCell = make("td");
    badgeCell.append(make("span", "badge " + badgeClass, label));
    row.append(
      make("td", "", PROBE_NAMES[key] || key),
      badgeCell,
      make("td", "detail", item.detail || "")
    );
    body.append(row);
  });
}

function addConfigRow(box, label, value) {
  box.append(make("b", "", label), make("span", "", value));
}

function renderTgHint(config) {
  const box = byId("tgHint");
  box.replaceChildren();
  if (!config.tgws_enabled) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");

  const ip = config.tg_link_ip || config.server_ip || "LAN_IP";
  let wg = "";
  const subnet = String(config.wg_subnet || "");
  if (subnet && subnet !== "—") {
    wg = subnet.split("/")[0].replace(/\.0$/, ".1");
  }
  const lines = [
    "Telegram → Настройки → Продвинутые → Прокси (MTProto, порт 1443, секрет — из docker logs routeweave-tgwsproxy):",
    "дом — " + ip + ":1443",
    "WDTT — " + (wg || "WG_IP") + ":1443",
    "Tailscale — tailscale-имя-сервера:1443"
  ];
  if (config.tg_link) {
    lines.push("Ссылка для клиента: " + config.tg_link);
  }
  lines.forEach((line) => box.append(make("div", "", line)));
}

function renderConfig(config, mihomoState) {
  const box = byId("cfg");
  box.replaceChildren();
  const data = config || {};
  addConfigRow(box, "IP сервера", data.server_ip || "—");
  addConfigRow(box, "SOCKS", data.socks || "—");
  addConfigRow(box, "BASE_DOMAIN", data.base_domain || "—");
  addConfigRow(box, "WG-подсеть (WDTT)", data.wg_subnet || "—");
  addConfigRow(box, "QUIC DROP", data.quic_drop || "—");
  addConfigRow(box, "Telegram (tgws)", data.tgws_enabled ? "включён" : "выключен");
  addConfigRow(box, "mihomo API", data.mihomo_api || mihomoState || "—");
  if (data.tg_link_ip) {
    addConfigRow(box, "TG server", data.tg_link_ip);
  }
  renderTgHint(data);
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    renderServices(status.services || {});
    renderProbes(status.probes);
    renderConfig(status.config, status.mihomo_api);
  } catch (err) {
    toast("Не удалось получить статус: " + err.message, false);
  }
}

function updateListState() {
  const area = byId("listArea");
  const rows = area.value === "" ? [] : area.value.replace(/\n$/, "").split("\n");
  const state = byId("listState");
  const dirty = area.value !== savedContent;
  state.textContent = "Строк: " + rows.length + (dirty ? " · не сохранено" : " · сохранено");
  state.classList.toggle("dirty", dirty);
}

async function loadList() {
  try {
    const data = await api("/api/lists/" + encodeURIComponent(currentList));
    const area = byId("listArea");
    area.value = data.content || "";
    savedContent = area.value;
    updateListState();
  } catch (err) {
    toast("Не удалось загрузить список: " + err.message, false);
  }
}

async function saveList(showToast) {
  const area = byId("listArea");
  const result = await api("/api/lists/" + encodeURIComponent(currentList), {
    method: "PUT",
    body: area.value,
    headers: { "Content-Type": "text/plain; charset=utf-8" }
  });
  savedContent = area.value;
  updateListState();
  if (showToast) {
    toast(result.hint || "Сохранено", true);
  }
  return result;
}

function setApplyBusy(busy, text) {
  byId("applyList").disabled = busy;
  byId("saveList").disabled = busy;
  byId("applyState").textContent = text || "";
}

function pollJob() {
  if (jobTimer !== null) {
    return;
  }
  setApplyBusy(true, "Применение идёт…");
  const tick = async () => {
    jobTimer = null;
    try {
      const job = await api("/api/jobs/apply");
      if (job.running) {
        jobTimer = window.setTimeout(tick, JOB_POLL_MS);
        return;
      }
      setApplyBusy(false, "");
      const detail = String(job.detail || "").slice(0, 300);
      if (job.ok === true) {
        byId("applyState").textContent = "Готово";
        toast("Списки применены" + (detail ? ": " + detail : ""), true);
      } else {
        byId("applyState").textContent = "Ошибка применения";
        toast("Ошибка применения: " + (detail || "нет деталей"), false);
      }
      loadStatus();
    } catch (err) {
      jobTimer = null;
      setApplyBusy(false, "");
      toast("Не удалось получить статус применения: " + err.message, false);
    }
  };
  jobTimer = window.setTimeout(tick, 300);
}

async function applyList() {
  setApplyBusy(true, "Сохранение…");
  try {
    await saveList(false);
  } catch (err) {
    setApplyBusy(false, "");
    toast("Не удалось сохранить список: " + err.message, false);
    return;
  }
  try {
    await api("/api/lists/" + encodeURIComponent(currentList) + "/apply", { method: "POST" });
  } catch (err) {
    if (err.status === 409) {
      toast(err.message, false);
      pollJob();
      return;
    }
    setApplyBusy(false, "");
    toast("Не удалось запустить применение: " + err.message, false);
    return;
  }
  pollJob();
}

function init() {
  const tabs = document.querySelectorAll(".tabs button");
  tabs.forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.list === currentList) {
        return;
      }
      if (byId("listArea").value !== savedContent && !window.confirm("Изменения не сохранены. Переключить список?")) {
        return;
      }
      tabs.forEach((item) => item.classList.remove("on"));
      button.classList.add("on");
      currentList = button.dataset.list;
      byId("applyList").textContent = APPLY_LABELS[currentList] || "Применить";
      loadList();
    });
  });

  byId("listArea").addEventListener("input", updateListState);

  byId("saveList").addEventListener("click", async () => {
    const button = byId("saveList");
    button.disabled = true;
    try {
      await saveList(true);
    } catch (err) {
      toast("Не удалось сохранить: " + err.message, false);
    }
    button.disabled = false;
  });

  byId("applyList").addEventListener("click", applyList);

  byId("runProbe").addEventListener("click", async () => {
    const button = byId("runProbe");
    button.disabled = true;
    button.textContent = "Идут пробы…";
    try {
      renderProbes(await api("/api/probe", { method: "POST" }));
    } catch (err) {
      toast("Пробы не выполнены: " + err.message, false);
    }
    button.disabled = false;
    button.textContent = "Запустить пробы";
  });

  byId("applyList").textContent = APPLY_LABELS[currentList];
  loadStatus();
  loadList();
  window.setInterval(loadStatus, STATUS_POLL_MS);
  api("/api/jobs/apply")
    .then((job) => {
      if (job && job.running) {
        pollJob();
      }
    })
    .catch(() => {});
}

init();
