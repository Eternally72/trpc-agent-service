(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function escapeHTML(value) {
    const node = document.createElement("span");
    node.textContent = String(value ?? "");
    return node.innerHTML;
  }

  function cookie(name) {
    return document.cookie.split("; ")
      .find((row) => row.startsWith(`${name}=`))?.split("=").slice(1).join("=") || "";
  }

  function errorMessage(body, status) {
    // The API exposes one stable error envelope. Keep legacy detail handling so
    // the console can also display errors from proxies and older deployments.
    if (typeof body?.error?.message === "string") return body.error.message;
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) return body.detail.map((item) => item.msg).join("；");
    if (body?.detail) return JSON.stringify(body.detail);
    return `请求失败（HTTP ${status}）`;
  }

  function createApi(state, supportReason = "") {
    return async (path, options = {}) => {
      const headers = { ...(options.headers || {}) };
      if (state.token) {
        headers.Authorization = `Bearer ${state.token}`;
      } else if (!["GET", "HEAD", "OPTIONS"].includes(options.method || "GET")) {
        headers["X-CSRF-Token"] = decodeURIComponent(cookie("trpc_management_csrf"));
      }
      // Platform operators must state a support reason regardless of whether
      // they authenticate with a bootstrap token or a browser session.
      if (supportReason) headers["X-Support-Reason"] = supportReason;
      if (typeof options.body === "string") headers["Content-Type"] = "application/json";
      const response = await fetch(`/api/v1${path}`, {
        credentials: "same-origin", ...options, headers,
      });
      const text = await response.text();
      let body = null;
      if (text) {
        try { body = JSON.parse(text); } catch { body = text; }
      }
      if (!response.ok) {
        const error = new Error(errorMessage(body, response.status));
        error.status = response.status;
        throw error;
      }
      return body;
    };
  }

  function toast(text, kind = "success") {
    const item = document.createElement("div");
    item.className = `toast ${kind}`;
    item.textContent = text;
    $("#toasts").append(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function setLoginError(text = "") {
    const error = $("#login-error");
    error.textContent = text;
    error.hidden = !text;
  }

  function badge(value) {
    const normalized = String(value || "unknown").toLowerCase();
    return `<span class="badge ${escapeHTML(normalized)}">${escapeHTML(value)}</span>`;
  }

  function empty(columns, text = "暂无数据") {
    return `<tr><td class="empty" colspan="${columns}">${escapeHTML(text)}</td></tr>`;
  }

  function formatDate(value) {
    return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
  }

  function compactNumber(value) {
    return new Intl.NumberFormat("zh-CN", {
      notation: "compact", maximumFractionDigits: 1,
    }).format(Number(value || 0));
  }

  function setOptions(selector, items, valueKey, label) {
    $$(selector).forEach((select) => {
      const previous = select.value;
      select.innerHTML = items.map((item) =>
        `<option value="${escapeHTML(item[valueKey])}">${escapeHTML(label(item))}</option>`).join("");
      if (items.some((item) => String(item[valueKey]) === previous)) select.value = previous;
    });
  }

  window.ConsoleUI = {
    $, $$, escapeHTML, createApi, toast, setLoginError, badge, empty,
    formatDate, compactNumber, setOptions,
  };
})();
