const state = { token: "", actor: null, tenants: [], models: [], credentials: [], nodes: [], workerPool: null };
const titles = {
  overview: "平台概览", tenants: "租户", accounts: "管理账号", models: "模型目录",
  credentials: "模型凭据", profiles: "租户模型策略", runtime: "运行节点",
  usage: "用量账本", adapters: "IM 适配器", audit: "管理审计",
};

const {
  $, $$, escapeHTML, createApi, toast, setLoginError, badge, empty,
  formatDate, compactNumber, setOptions,
} = window.ConsoleUI;
const api = createApi(state, "interactive platform administration");

async function loadTenants() {
  const data = await api("/tenants?limit=100");
  state.tenants = data.items;
  $("#tenant-rows").innerHTML = data.items.map((item) => `
    <tr><td><span class="row-title">${escapeHTML(item.name)}</span><span class="row-subtitle mono">${escapeHTML(item.tenant_id)}</span></td>
    <td>${badge(item.status)}</td><td>${escapeHTML(item.isolation_mode)}</td><td>${formatDate(item.updated_at)}</td>
    <td><div class="actions"><button class="button secondary small tenant-toggle" data-id="${item.tenant_id}" data-status="${item.status}">${item.status === "active" ? "停用" : "启用"}</button></div></td></tr>`).join("") || empty(5);
  setOptions(".tenant-options, #profile-tenant", data.items.filter((item) => item.status === "active"), "tenant_id", (item) => item.name);
  $$(".tenant-toggle").forEach((button) => button.addEventListener("click", () => toggleTenant(button)));
  return data;
}

async function loadAccounts() {
  const data = await api("/admin/principals?limit=100");
  $("#account-rows").innerHTML = data.items.map((item) => {
    const isTenantAdmin = item.external_subject.startsWith("tenant-console:");
    const passwordAction = item.principal_type === "human"
      ? `<button class="button secondary small password-open" data-id="${item.management_principal_id}" data-subject="${escapeHTML(item.external_subject)}">重置密码</button>`
      : "";
    // A tenant has exactly one administrator identity. It may be recovered by
    // rotating its password, but disabling it would make the tenant unmanaged.
    const statusAction = isTenantAdmin
      ? `<span class="row-subtitle">唯一账号不可停用</span>`
      : `<button class="button ${item.status === "active" ? "danger" : "secondary"} small principal-toggle" data-id="${item.management_principal_id}" data-status="${item.status}">${item.status === "active" ? "停用" : "启用"}</button>`;
    return `
    <tr><td><span class="row-title">${escapeHTML(item.display_name)}</span><span class="row-subtitle mono">${escapeHTML(item.management_principal_id)}</span></td>
    <td>${isTenantAdmin ? "租户管理员" : escapeHTML(item.principal_type)}</td><td class="mono">${escapeHTML(item.external_subject)}</td><td>${badge(item.status)}</td>
    <td><div class="actions">${passwordAction}${statusAction}</div></td></tr>`;
  }).join("") || empty(5);
  $$(".password-open").forEach((button) => button.addEventListener("click", () => openPasswordDialog(button)));
  $$(".principal-toggle").forEach((button) => button.addEventListener("click", () => togglePrincipal(button)));
  return data;
}

async function loadModels() {
  const data = await api("/admin/model-catalog");
  state.models = data.items;
  $("#model-rows").innerHTML = data.items.map((item) => `
    <tr><td><span class="row-title">${escapeHTML(item.display_name)}</span><span class="row-subtitle mono">${escapeHTML(item.model_name)}</span></td><td>${escapeHTML(item.provider)}</td>
    <td>${item.platform_credential_configured ? "已配置" : "使用独立凭据"}</td><td>${badge(item.status)}</td><td><button class="button ${item.status === "active" ? "danger" : "secondary"} small model-toggle" data-id="${item.model_catalog_id}" data-status="${item.status}">${item.status === "active" ? "停用" : "启用"}</button></td></tr>`).join("") || empty(5);
  setOptions("#profile-model", data.items.filter((item) => item.status === "active"), "model_catalog_id", (item) => `${item.provider} / ${item.model_name}`);
  $$(".model-toggle").forEach((button) => button.addEventListener("click", () => toggleModel(button)));
  return data;
}

async function loadCredentials() {
  const data = await api("/admin/model-credentials");
  state.credentials = data.items;
  $("#credential-rows").innerHTML = data.items.map((item) => `
    <tr><td><span class="row-title">${escapeHTML(item.name)}</span><span class="row-subtitle mono">${escapeHTML(item.model_credential_id)}</span></td><td>${escapeHTML(item.provider)}</td><td>${item.secret_configured ? "已引用" : "未配置"}</td><td>${badge(item.status)}</td><td><button class="button ${item.status === "active" ? "danger" : "secondary"} small credential-toggle" data-id="${item.model_credential_id}" data-status="${item.status}">${item.status === "active" ? "停用" : "启用"}</button></td></tr>`).join("") || empty(5);
  setOptions("#profile-credential", data.items.filter((item) => item.status === "active"), "model_credential_id", (item) => `${item.provider} / ${item.name}`);
  $$(".credential-toggle").forEach((button) => button.addEventListener("click", () => toggleCredential(button)));
  return data;
}

async function loadProfiles() {
  if (!state.tenants.length) await loadTenants();
  if (!state.models.length) await loadModels();
  if (!state.credentials.length) await loadCredentials();
  const tenantId = $("#profile-tenant").value;
  if (!tenantId) { $("#profile-rows").innerHTML = empty(6, "请先创建可用租户"); return { items: [], total: 0 }; }
  const data = await api(`/tenants/${tenantId}/model-profiles`);
  $("#profile-rows").innerHTML = data.items.map((item) => {
    const model = state.models.find((entry) => entry.model_catalog_id === item.model_catalog_id);
    return `<tr><td><span class="row-title">${escapeHTML(item.name)}</span><span class="row-subtitle mono">${escapeHTML(item.model_profile_id)}</span></td><td>${escapeHTML(model?.display_name || item.model_catalog_id)}</td><td class="mono">${escapeHTML(JSON.stringify(item.parameter_config))}</td><td class="mono">${escapeHTML(JSON.stringify(item.limits))}</td><td>${badge(item.status)}</td><td><button class="button ${item.status === "active" ? "danger" : "secondary"} small profile-toggle" data-id="${item.model_profile_id}" data-status="${item.status}">${item.status === "active" ? "停用" : "启用"}</button></td></tr>`;
  }).join("") || empty(6);
  $$(".profile-toggle").forEach((button) => button.addEventListener("click", () => toggleProfile(button)));
  return data;
}

async function loadRuntime() {
  const [data, pool] = await Promise.all([
    api("/admin/runtime-nodes"),
    api("/admin/worker-pool"),
  ]);
  state.nodes = data.items;
  state.workerPool = pool;
  $("#runtime-rows").innerHTML = data.items.map((item) => `<tr><td><span class="row-title">${escapeHTML(item.node_id)}</span><span class="row-subtitle">启动于 ${formatDate(item.started_at)}</span></td><td>${escapeHTML(item.role)}</td><td>${item.worker_concurrency}</td><td>${formatDate(item.heartbeat_at)}</td><td>${badge(item.health)}</td></tr>`).join("") || empty(5);
  $("#pool-desired").textContent = pool.desired_nodes;
  $("#pool-active").textContent = pool.active_nodes;
  $("#pool-draining").textContent = pool.draining_nodes;
  $("#pool-stale").textContent = pool.stale_nodes;
  $("#worker-pool-desired").value = pool.desired_nodes;
  $("#worker-pool-generation").textContent = `Generation ${pool.generation} · ${pool.scaler_mode}`;
  $("#worker-pool-state").textContent = pool.reconciling ? "调整中" : "已收敛";
  $("#worker-pool-state").className = `badge ${pool.reconciling ? "warning" : ""}`;
  return data;
}

async function loadUsage() {
  const data = await api("/admin/usage?limit=100");
  $("#usage-summary").textContent = `共 ${data.total} 条模型调用，${Number(data.summary.total_tokens).toLocaleString()} Token，预估成本 ${data.summary.estimated_cost}`;
  $("#usage-rows").innerHTML = data.items.map((item) => `<tr><td>${formatDate(item.occurred_at)}</td><td class="mono">${escapeHTML(item.tenant_id)}</td><td>${escapeHTML(item.model_provider)} / ${escapeHTML(item.model_name)}</td><td>${Number(item.total_tokens).toLocaleString()}</td><td>${escapeHTML(item.estimated_cost)}</td><td class="mono">${escapeHTML(item.request_id)}</td></tr>`).join("") || empty(6);
  return data;
}

async function loadAdapters() {
  const data = await api("/admin/channel-adapter-types");
  $("#adapter-rows").innerHTML = data.items.map((item) => `<tr><td><span class="row-title">${escapeHTML(item.display_name)}</span><span class="row-subtitle mono">${escapeHTML(item.channel_type)}</span></td><td>${escapeHTML(item.adapter_version)}</td><td class="mono">${escapeHTML(Object.keys(item.capabilities || {}).join("、") || "—")}</td><td>${badge(item.status)}</td><td><button class="button ${item.status === "active" ? "danger" : "secondary"} small adapter-toggle" data-id="${escapeHTML(item.channel_type)}" data-status="${item.status}">${item.status === "active" ? "停用" : "启用"}</button></td></tr>`).join("") || empty(5);
  $$(".adapter-toggle").forEach((button) => button.addEventListener("click", () => toggleAdapter(button)));
  return data;
}

async function loadAudit() {
  const data = await api("/admin/audit?limit=100");
  $("#audit-rows").innerHTML = data.items.map((item) => `<tr><td>${formatDate(item.occurred_at)}</td><td><span class="row-title">${escapeHTML(item.action)}</span>${item.reason ? `<span class="row-subtitle">${escapeHTML(item.reason)}</span>` : ""}</td><td>${escapeHTML(item.resource_type)}<span class="row-subtitle mono">${escapeHTML(item.resource_id)}</span></td><td class="mono">${escapeHTML(item.actor_subject)}</td><td>${badge(item.decision)}</td></tr>`).join("") || empty(5);
  return data;
}

async function loadOverview() {
  const [tenants, models, nodes, usage, health, ready] = await Promise.all([
    loadTenants(), loadModels(), loadRuntime(), loadUsage(),
    fetch("/health").then((response) => response.json()),
    fetch("/ready").then(async (response) => ({ ok: response.ok, body: await response.json() })),
  ]);
  $("#metric-tenants").textContent = tenants.total;
  $("#metric-models").textContent = models.items.filter((item) => item.status === "active").length;
  $("#metric-workers").textContent = nodes.items.filter((item) => item.health === "active" && ["worker", "api_worker"].includes(item.role)).length;
  $("#metric-tokens").textContent = compactNumber(usage.summary.total_tokens);
  $("#health-gateway").textContent = health.status === "ok" ? "正常" : "异常";
  $("#health-database").textContent = ready.body?.checks?.database === "ok" ? "正常" : "异常";
  $("#health-worker").textContent = ready.ok ? "正常" : "需检查";
  $("#health-database").className = `badge ${ready.body?.checks?.database === "ok" ? "" : "error"}`;
  $("#health-worker").className = `badge ${ready.ok ? "" : "warning"}`;
}

const loaders = { overview: loadOverview, tenants: loadTenants, accounts: loadAccounts, models: loadModels, credentials: loadCredentials, profiles: loadProfiles, runtime: loadRuntime, usage: loadUsage, adapters: loadAdapters, audit: loadAudit };

async function refresh(view, { quiet = false } = {}) {
  try { await loaders[view]?.(); if (!quiet) toast("数据已刷新"); }
  catch (error) { toast(error.message, "error"); throw error; }
}

async function mutate({ button, request, success, refreshView, dialog, form }) {
  if (button) button.disabled = true;
  try {
    await request();
  } catch (error) {
    toast(error.message, "error");
    return false;
  } finally {
    if (button) button.disabled = false;
  }
  dialog?.close();
  form?.reset();
  toast(success, "success");
  if (refreshView) {
    try { await loaders[refreshView](); }
    catch (error) { toast(`${success}，但列表刷新失败：${error.message}`, "warning"); }
  }
  return true;
}

function showView(name) {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === name));
  $("#page-title").textContent = titles[name] || "系统管理";
  refresh(name, { quiet: true }).catch(() => {});
}

async function toggleTenant(button) {
  const next = button.dataset.status === "active" ? "disabled" : "active";
  if (next === "disabled" && !confirm("确认停用该租户？现有 Agent 和 IM 将不可用。")) return;
  await mutate({ button, request: () => api(`/tenants/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) }), success: next === "active" ? "租户已启用" : "租户已停用", refreshView: "tenants" });
}

async function togglePrincipal(button) {
  const next = button.dataset.status === "active" ? "disabled" : "active";
  if (next === "disabled" && !confirm("确认停用该管理身份？")) return;
  await mutate({ button, request: () => api(`/admin/principals/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) }), success: next === "active" ? "身份已启用" : "身份已停用", refreshView: "accounts" });
}

async function toggleModel(button) {
  const next = button.dataset.status === "active" ? "disabled" : "active";
  await mutate({ button, request: () => api(`/admin/model-catalog/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) }), success: `模型已${next === "active" ? "启用" : "停用"}`, refreshView: "models" });
}

async function toggleCredential(button) {
  const next = button.dataset.status === "active" ? "disabled" : "active";
  await mutate({ button, request: () => api(`/admin/model-credentials/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) }), success: `凭据已${next === "active" ? "启用" : "停用"}`, refreshView: "credentials" });
}

async function toggleProfile(button) {
  const next = button.dataset.status === "active" ? "disabled" : "active";
  const tenantId = $("#profile-tenant").value;
  await mutate({ button, request: () => api(`/tenants/${tenantId}/model-profiles/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) }), success: `策略已${next === "active" ? "启用" : "停用"}`, refreshView: "profiles" });
}

async function toggleAdapter(button) {
  const next = button.dataset.status === "active" ? "disabled" : "active";
  await mutate({ button, request: () => api(`/admin/channel-adapter-types/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) }), success: `适配器已${next === "active" ? "启用" : "停用"}`, refreshView: "adapters" });
}

function openPasswordDialog(button) {
  const form = $("#password-form");
  form.elements.principal_id.value = button.dataset.id;
  form.elements.username.value = (button.dataset.subject || "").replace(/^tenant-console:/, "");
  $("#password-dialog").showModal();
}

async function enterConsole() {
  const actor = await api("/admin/me");
  if (!actor.roles.includes("platform_admin")) {
    if (!state.token) await api("/auth/logout", { method: "POST" });
    throw new Error("该账号不是系统管理员，请使用租户管理入口。");
  }
  state.actor = actor;
  $("#actor-name").textContent = actor.subject;
  $("#login-shell").hidden = true;
  $("#app-shell").hidden = false;
  setLoginError();
  await loadOverview();
}

async function logout() {
  if (!state.token) {
    try { await api("/auth/logout", { method: "POST" }); } catch { /* Session may already expire. */ }
  }
  state.token = "";
  state.actor = null;
  $("#app-shell").hidden = true;
  $("#login-shell").hidden = false;
  $("#login-form").reset();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  setLoginError();
  try {
    const data = new FormData(form);
    state.token = "";
    await api("/auth/login", { method: "POST", body: JSON.stringify({ username: data.get("username"), password: data.get("password") }) });
    await enterConsole();
  } catch (error) { setLoginError(error.message); }
  finally { button.disabled = false; }
});

$("#token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  setLoginError();
  try { state.token = String(new FormData(form).get("token") || "").trim(); await enterConsole(); }
  catch (error) { state.token = ""; setLoginError(error.message); }
  finally { button.disabled = false; }
});

$("#logout").addEventListener("click", logout);
$$('[data-open]').forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.open}`).showModal()));
$$('[data-close]').forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$$('.nav-item').forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$$('[data-view-jump]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewJump)));
$$('[data-refresh]').forEach((button) => button.addEventListener("click", () => refresh(button.dataset.refresh).catch(() => {})));
$("#profile-tenant").addEventListener("change", () => refresh("profiles", { quiet: true }).catch(() => {}));

$("#worker-pool-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const desired = Number(new FormData(form).get("desired_nodes"));
  const current = state.workerPool;
  if (!current) { toast("请先刷新 Worker Pool 状态", "error"); return; }
  if (desired < current.desired_nodes && !confirm(`确认将 Worker 从 ${current.desired_nodes} 个缩减到 ${desired} 个？多余节点会先排空当前任务。`)) return;
  await mutate({
    button: $("button[type='submit']", form),
    request: () => api("/admin/worker-pool", { method: "PUT", body: JSON.stringify({ desired_nodes: desired, expected_generation: current.generation }) }),
    success: "Worker Pool 期望容量已更新",
    refreshView: "runtime",
  });
});

$("#tenant-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
  await mutate({ button: $("button[type='submit']", form), request: () => api("/tenants", { method: "POST", body: JSON.stringify({ name: data.get("name"), isolation_mode: data.get("isolation_mode") }) }), success: "租户已创建", refreshView: "tenants", dialog: form.closest("dialog"), form });
});

$("#account-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
  await mutate({ button: $("button[type='submit']", form), request: () => api("/admin/tenant-accounts", { method: "POST", body: JSON.stringify({ tenant_id: data.get("tenant_id"), username: String(data.get("username")).trim().toLowerCase(), password: data.get("password") }) }), success: "租户管理员账号已创建", refreshView: "accounts", dialog: form.closest("dialog"), form });
});

$("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
  await mutate({ button: $("button[type='submit']", form), request: () => api(`/admin/principals/${data.get("principal_id")}/password`, { method: "PUT", body: JSON.stringify({ username: String(data.get("username")).trim().toLowerCase(), password: data.get("password") }) }), success: "登录密码已更新", dialog: form.closest("dialog"), form });
});

$("#model-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const secretRef = String(data.get("platform_secret_ref") || "").trim();
  await mutate({ button: $("button[type='submit']", form), request: () => api("/admin/model-catalog", { method: "POST", body: JSON.stringify({ provider: data.get("provider"), model_name: data.get("model_name"), display_name: data.get("display_name"), capabilities: { text: true }, ...(secretRef ? { platform_secret_ref: secretRef } : {}) }) }), success: "模型已登记", refreshView: "models", dialog: form.closest("dialog"), form });
});

$("#credential-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
  await mutate({ button: $("button[type='submit']", form), request: () => api("/admin/model-credentials", { method: "POST", body: JSON.stringify({ provider: data.get("provider"), name: data.get("name"), secret_ref: data.get("secret_ref") }) }), success: "模型凭据已登记", refreshView: "credentials", dialog: form.closest("dialog"), form });
});

$("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const limits = {};
  if (data.get("daily_tokens")) limits.daily_tokens = Number(data.get("daily_tokens"));
  if (data.get("daily_calls")) limits.daily_calls = Number(data.get("daily_calls"));
  await mutate({ button: $("button[type='submit']", form), request: () => api(`/tenants/${data.get("tenant_id")}/model-profiles`, { method: "POST", body: JSON.stringify({ name: data.get("name"), model_catalog_id: data.get("model_catalog_id"), credential_id: data.get("credential_id"), parameter_config: { temperature: Number(data.get("temperature")), max_output_tokens: Number(data.get("max_output_tokens")), context_window_tokens: Number(data.get("context_window_tokens")), timeout_seconds: Number(data.get("timeout_seconds")) }, limits }) }), success: "租户模型策略已创建", refreshView: "profiles", dialog: form.closest("dialog"), form });
});

(async () => {
  try { await enterConsole(); }
  catch (error) { if (error.status !== 401) setLoginError(error.message); }
})();
