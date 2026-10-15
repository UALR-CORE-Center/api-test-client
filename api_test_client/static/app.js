/* API Test Client — no external scripts, analytics, or dependencies. */
"use strict";

const $ = (id) => document.getElementById(id);
const clone = (value) => JSON.parse(JSON.stringify(value));
const uid = () => crypto.randomUUID();
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const state = {workspace: null, token: "", kind: "request", draft: null, dirty: false, requestTab: "params", responseTab: "body", search: "", activeRequests: new Map(), startingSequences: new Set(), responses: new Map(), runs: new Map(), environmentDraft: null, variablesDraft: [], busy: false};

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("toast-region").append(node);
  setTimeout(() => node.remove(), error ? 8000 : 4000);
}

function handle(callback) {
  return async (event) => {
    try { await callback(event); }
    catch (error) { toast(error.message || String(error), true); }
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {"X-Client-Token": state.token, ...(options.body ? {"Content-Type": "application/json"} : {}), ...options.headers}
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`The local server returned an unreadable response (${response.status}).`); }
  if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : data.message || `Request failed (${response.status}).`);
  return data;
}

async function persist(workspace) {
  if (state.busy) throw new Error("A workspace save is in progress. Please try again in a moment.");
  state.busy = true;
  try {
    state.workspace = await api("/api/workspace", {method: "PUT", body: JSON.stringify(workspace)});
    renderNavigation();
    renderEnvironments();
  } finally { state.busy = false; }
}

function newRequest() {
  return {id: uid(), name: "Untitled request", method: "GET", url: "{{base_url}}/", params: [], headers: [], body_type: "none", body: "", auth: {type: "inherit"}, timeout: 30, follow_redirects: false, assertions: [], extract: []};
}

function newSequence() { return {id: uid(), name: "Untitled sequence", steps: [], stop_on_failure: true}; }
function currentEnvironment() { return state.workspace.environments.find((item) => item.id === state.workspace.active_environment_id); }
function setDirty() { state.dirty = true; updateHeading(); }

function updateHeading() {
  $("unsaved-indicator").hidden = !state.dirty;
  $("topbar-item").textContent = state.draft.name || `Untitled ${state.kind}`;
  const exists = state.workspace[state.kind === "request" ? "requests" : "sequences"].some((item) => item.id === state.draft.id);
  $("delete-item").disabled = !exists;
  $("duplicate-item").disabled = false;
  $("editor-kind").textContent = state.kind === "request" ? "REQUEST EDITOR" : "SEQUENCE BUILDER";
  $("item-name").setAttribute("aria-label", `${state.kind === "request" ? "Request" : "Sequence"} name`);
}

function canDiscard() {
  return !state.dirty || window.confirm(`Discard unsaved changes to “${state.draft.name || "this item"}”?`);
}

function selectItem(kind, item, isNew = false) {
  if (!canDiscard()) return;
  state.kind = kind;
  state.draft = clone(item);
  if (kind === "request") {
    Object.assign(state.draft, {method: state.draft.method || "GET", body_type: state.draft.body_type || "none", timeout: state.draft.timeout ?? 30, follow_redirects: state.draft.follow_redirects ?? false, params: state.draft.params || [], headers: state.draft.headers || [], auth: state.draft.auth || {type: "inherit"}, assertions: state.draft.assertions || [], extract: state.draft.extract || []});
    state.draft._testsText = JSON.stringify(state.draft.assertions, null, 2);
    state.draft._extractText = JSON.stringify(state.draft.extract, null, 2);
  } else {
    state.draft.steps = (state.draft.steps || []).map((step) => ({...step, _variablesText: JSON.stringify(step.variables || {}, null, 2)}));
  }
  state.dirty = isNew;
  $("runtime-variables").value = "{}";
  renderEditor();
  renderNavigation();
}

function renderNavigation() {
  for (const [collection, label] of [["requests", "request"], ["sequences", "sequence"]]) {
    const items = state.workspace[collection];
    $(`${label}-count`).textContent = items.length;
    const filtered = items.filter((item) => `${item.name} ${item.method || ""} ${item.url || ""}`.toLowerCase().includes(state.search));
    $(`${label}-list`).innerHTML = filtered.map((item) => `<button type="button" class="nav-item${state.kind === label && state.draft?.id === item.id ? " active" : ""}" data-kind="${label}" data-item-id="${escapeHTML(item.id)}" ${state.kind === label && state.draft?.id === item.id ? 'aria-current="page"' : ""}>${label === "request" ? `<span class="method-tag ${escapeHTML((item.method || "GET").toLowerCase())}">${escapeHTML(item.method || "GET")}</span>` : '<span class="sequence-tag" aria-hidden="true">≋</span>'}<span class="item-label" title="${escapeHTML(item.name)}">${escapeHTML(item.name || `Untitled ${label}`)}</span></button>`).join("") || `<p class="nav-empty">${state.search ? "No matches." : `No saved ${collection} yet.`}</p>`;
  }
}

function renderEnvironments() {
  const environments = state.workspace.environments;
  $("environment-picker").innerHTML = environments.map((env) => `<option value="${escapeHTML(env.id)}">${escapeHTML(env.name)}</option>`).join("") || '<option value="">No environment</option>';
  $("environment-picker").value = state.workspace.active_environment_id || "";
  $("edit-environment").disabled = environments.length === 0;
  const env = currentEnvironment();
  $("environment-summary").textContent = env?.base_url || "No base URL set";
  $("environment-summary").title = env?.base_url || "";
  if (state.kind === "request" && state.draft?.auth?.type === "inherit") renderAuthFields("request", state.draft.auth);
}

function renderRows(containerId, rows, variables = false) {
  const container = $(containerId);
  container.innerHTML = `<div class="kv-heading"><span></span><span>KEY</span><span>VALUE</span>${variables ? '<span>SECRET</span>' : ""}<span></span></div>` + rows.map((row, index) => `<div class="kv-row${row.enabled === false ? " disabled" : ""}" data-row-index="${index}"><label title="Enable row"><span class="sr-only">Enable ${variables ? "variable" : "row"} ${index + 1}</span><input type="checkbox" data-field="enabled" ${row.enabled !== false ? "checked" : ""}></label><input data-field="key" aria-label="${variables ? "Variable" : "Row"} ${index + 1} key" placeholder="${variables ? "variable_name" : "Key"}" value="${escapeHTML(row.key)}" spellcheck="false" autocomplete="off"><input data-field="value" aria-label="${variables ? "Variable" : "Row"} ${index + 1} value" type="${variables && row.secret ? "password" : "text"}" placeholder="Value" value="${escapeHTML(row.value)}" spellcheck="false" autocomplete="${variables && row.secret ? "new-password" : "off"}">${variables ? `<label class="secret-label" title="Remove value on export"><span class="sr-only">Mark variable ${index + 1} as secret</span><input data-field="secret" type="checkbox" ${row.secret ? "checked" : ""}></label>` : ""}<button type="button" class="icon-button" data-remove-row="${index}" aria-label="Remove ${variables ? "variable" : "row"} ${index + 1}" title="Remove row">×</button></div>`).join("") + (rows.length ? "" : `<div class="kv-empty">${variables ? "No variables yet. Add one to get started." : "No entries. Add a row above when you need one."}</div>`);
}

function bindRows(containerId, getRows, variables = false, onUpdate = () => {}) {
  const container = $(containerId);
  container.addEventListener("input", (event) => {
    const field = event.target.dataset.field;
    const row = event.target.closest("[data-row-index]");
    if (!field || !row) return;
    const item = getRows()[Number(row.dataset.rowIndex)];
    item[field] = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    if (field === "enabled") row.classList.toggle("disabled", !item.enabled);
    if (field === "secret") {
      const valueInput = row.querySelector('[data-field="value"]');
      valueInput.type = item.secret ? "password" : "text";
      valueInput.autocomplete = item.secret ? "new-password" : "off";
    }
    onUpdate();
  });
  container.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-row]");
    if (!button) return;
    getRows().splice(Number(button.dataset.removeRow), 1);
    renderRows(containerId, getRows(), variables);
    onUpdate();
  });
}

function renderCounts() {
  for (const key of ["params", "headers"]) $(`${key}-count`).textContent = state.draft[key].filter((row) => row.enabled !== false && row.key).length || "";
  for (const [key, input] of [["tests", "_testsText"], ["extract", "_extractText"]]) {
    try { const value = JSON.parse(state.draft[input]); $(`${key}-count`).textContent = Array.isArray(value) ? value.length || "" : "!"; }
    catch { $(`${key}-count`).textContent = "!"; }
  }
}

function setTab(group, tab) {
  const prefix = group === "request" ? "" : "response-";
  state[group === "request" ? "requestTab" : "responseTab"] = tab;
  $(`${group}-tabs`).querySelectorAll('[role="tab"]').forEach((button) => {
    const active = button.dataset.tab === tab;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    $(`${prefix}panel-${button.dataset.tab}`).hidden = !active;
  });
}

function bindTabs(group) {
  const tablist = $(`${group}-tabs`);
  tablist.addEventListener("click", (event) => {
    const button = event.target.closest('[role="tab"]');
    if (button) setTab(group, button.dataset.tab);
  });
  tablist.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !event.target.matches('[role="tab"]')) return;
    event.preventDefault();
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    const index = tabs.indexOf(event.target);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setTab(group, tabs[next].dataset.tab);
    tabs[next].focus();
  });
}

function authInput(field, label, value, password = false, full = false) {
  return `<label class="${full ? "full" : ""}">${label}<input data-auth-field="${field}" value="${escapeHTML(value)}" ${password ? 'type="password" autocomplete="new-password"' : 'autocomplete="off"'} spellcheck="false"></label>`;
}

function renderAuthFields(scope, auth) {
  let html = "";
  if (auth.type === "inherit") {
    const env = currentEnvironment();
    const names = {none: "no authentication", bearer: "bearer token", api_key: "API key", basic: "basic authentication"};
    html = `<p>Using ${escapeHTML(env?.name || "no environment")}: ${escapeHTML(names[env?.auth?.type] || "no authentication")}.</p>`;
  } else if (auth.type === "bearer") html = authInput("token", "Bearer token", auth.token || "", true, true);
  else if (auth.type === "api_key") html = authInput("header", "Header or query parameter name", auth.header || "X-API-Key") + authInput("value", "API key value", auth.value || "", true) + `<label>Send in<select data-auth-field="location"><option value="header"${auth.location !== "query" ? " selected" : ""}>Header</option><option value="query"${auth.location === "query" ? " selected" : ""}>Query parameter</option></select></label>`;
  else if (auth.type === "basic") html = authInput("username", "Username", auth.username || "") + authInput("password", "Password", auth.password || "", true);
  else html = "<p>No authentication will be added to this request.</p>";
  $(`${scope}-auth-fields`).innerHTML = html;
}

function freshAuth(type, scope) {
  if (type === "bearer") return {type, token: scope === "env" ? "{{api_key}}" : ""};
  if (type === "api_key") return {type, header: "X-API-Key", value: scope === "env" ? "{{api_key}}" : "", location: "header"};
  if (type === "basic") return {type, username: "", password: ""};
  return {type};
}

function updateBodyUI() {
  const type = state.draft.body_type;
  $("request-body").hidden = type === "none";
  $("format-body").hidden = type !== "json";
  $("body-help").textContent = {none: "This request does not include a body.", json: "Use unquoted {{value | json}} to insert a JSON value with proper escaping. Content-Type defaults to application/json.", text: "Send a plain text body, with optional {{variables}}.", form: "Use URL-encoded text, for example name={{name}}&enabled=true."}[type] || "";
}

function renderEditor() {
  updateHeading();
  $("item-name").value = state.draft.name;
  const requestMode = state.kind === "request";
  $("request-editor").hidden = !requestMode;
  $("sequence-editor").hidden = requestMode;
  if (requestMode) {
    const draft = state.draft;
    $("request-method").value = draft.method;
    $("request-url").value = draft.url;
    $("body-type").value = draft.body_type || "none";
    $("request-body").value = draft.body || "";
    $("request-auth-type").value = draft.auth.type;
    $("request-timeout").value = draft.timeout || 30;
    $("follow-redirects").checked = !!draft.follow_redirects;
    $("request-tests").value = draft._testsText;
    $("request-extract").value = draft._extractText;
    renderRows("params-rows", draft.params);
    renderRows("headers-rows", draft.headers);
    renderAuthFields("request", draft.auth);
    renderCounts();
    updateBodyUI();
    setTab("request", state.requestTab);
  } else {
    $("stop-on-failure").checked = state.draft.stop_on_failure !== false;
    renderSteps();
  }
  renderExecution();
  renderResponse(state.responses.get(`${state.kind}:${state.draft.id}`));
  if (requestMode && state.activeRequests.has(state.draft.id)) renderPendingResponse(state.activeRequests.get(state.draft.id));
  renderSequenceResults();
}

function parseJSON(text, label, shape) {
  let parsed;
  try { parsed = JSON.parse(text || (shape === "array" ? "[]" : "{}")); }
  catch (error) { throw new Error(`${label} must be valid JSON: ${error.message}`); }
  if (shape === "array" && !Array.isArray(parsed)) throw new Error(`${label} must be a JSON array.`);
  if (shape === "object" && (parsed === null || Array.isArray(parsed) || typeof parsed !== "object")) throw new Error(`${label} must be a JSON object.`);
  return parsed;
}

function readDraft() {
  const draft = clone(state.draft);
  draft.name = (draft.name || "").trim() || `Untitled ${state.kind}`;
  if (state.kind === "request") {
    draft.assertions = parseJSON(draft._testsText, "Assertions", "array");
    draft.extract = parseJSON(draft._extractText, "Extraction rules", "array");
    delete draft._testsText;
    delete draft._extractText;
    if (!draft.url.trim()) throw new Error("Enter a request URL first.");
    if (!Number.isFinite(draft.timeout) || draft.timeout < 1 || draft.timeout > 300) throw new Error("Timeout must be between 1 and 300 seconds.");
  } else {
    draft.steps = draft.steps.map((step, index) => {
      const variables = parseJSON(step._variablesText, `Step ${index + 1} variables`, "object");
      if (!state.workspace.requests.some((request) => request.id === step.request_id)) throw new Error(`Choose a saved request for step ${index + 1}.`);
      delete step._variablesText;
      return {...step, variables};
    });
  }
  return draft;
}

async function saveItem() {
  const draft = readDraft();
  const workspace = clone(state.workspace);
  const collection = state.kind === "request" ? "requests" : "sequences";
  const index = workspace[collection].findIndex((item) => item.id === draft.id);
  if (index < 0) workspace[collection].push(draft);
  else workspace[collection][index] = draft;
  $("save-item").disabled = true;
  try {
    await persist(workspace);
    state.dirty = false;
    state.draft.name = draft.name;
    $("item-name").value = draft.name;
    updateHeading();
    renderNavigation();
    toast(`${state.kind === "request" ? "Request" : "Sequence"} saved.`);
  } finally { $("save-item").disabled = false; }
}

async function duplicateItem() {
  const draft = readDraft();
  draft.id = uid();
  draft.name += " (copy)";
  if (state.kind === "sequence") draft.steps.forEach((step) => { step.id = uid(); });
  const workspace = clone(state.workspace);
  workspace[state.kind === "request" ? "requests" : "sequences"].push(draft);
  await persist(workspace);
  state.dirty = false;
  selectItem(state.kind, draft);
  toast("Copy created.");
}

async function deleteItem() {
  const kind = state.kind;
  const id = state.draft.id;
  const collection = kind === "request" ? "requests" : "sequences";
  const references = kind === "request" ? state.workspace.sequences.filter((sequence) => sequence.steps.some((step) => step.request_id === id)) : [];
  if (references.length) throw new Error(`This request is used by ${references.map((sequence) => sequence.name).join(", ")}. Remove it from those sequences before deleting it.`);
  if (state.runs.get(id)?.state === "running") throw new Error("Cancel the running sequence before deleting it.");
  if (!window.confirm(`Delete “${state.draft.name}”? This removes it from your workspace.`)) return;
  const workspace = clone(state.workspace);
  workspace[collection] = workspace[collection].filter((item) => item.id !== id);
  await persist(workspace);
  state.dirty = false;
  const next = state.workspace[collection][0];
  selectItem(kind, next || (kind === "request" ? newRequest() : newSequence()), !next);
  toast(`${kind === "request" ? "Request" : "Sequence"} deleted.`);
}

function renderExecution() {
  const sending = state.activeRequests.has(state.draft?.id);
  $("send-request").disabled = sending;
  $("send-request").innerHTML = sending ? '<span class="spinner" aria-hidden="true" style="width:15px;height:15px;border-top-color:white"></span> Sending…' : '<span aria-hidden="true">↗</span> Send <kbd>' + (/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ ↵" : "Ctrl ↵") + "</kbd>";
  const running = state.startingSequences.has(state.draft?.id) || state.runs.get(state.draft?.id)?.state === "running";
  $("run-sequence").disabled = running;
  $("run-sequence").innerHTML = running ? '<span class="spinner" aria-hidden="true" style="width:15px;height:15px;border-top-color:white"></span> Running…' : '<span aria-hidden="true">▶</span> Run sequence';
}

async function sendRequest() {
  if (state.kind !== "request" || state.activeRequests.has(state.draft.id)) return;
  const request = readDraft();
  const variables = parseJSON($("runtime-variables").value, "Run variables", "object");
  const key = `request:${request.id}`;
  state.activeRequests.set(request.id, request);
  renderExecution();
  renderPendingResponse(request);
  try {
    const response = await api("/api/run", {method: "POST", body: JSON.stringify({request, environment_id: state.workspace.active_environment_id || null, variables})});
    state.responses.set(key, response);
    if (state.kind === "request" && state.draft.id === request.id) renderResponse(response);
  } catch (error) {
    const response = {ok: false, error: error.message, body: "", headers: {}, assertions: [], extracted: {}};
    state.responses.set(key, response);
    if (state.kind === "request" && state.draft.id === request.id) renderResponse(response);
    throw error;
  } finally {
    state.activeRequests.delete(request.id);
    renderExecution();
  }
}

function renderPendingResponse(request) {
  $("response-empty").hidden = true;
  $("response-content").hidden = true;
  $("response-loading").hidden = false;
  $("response-metrics").textContent = "";
  $("response-loading-detail").textContent = `${request.method} ${request.url}`;
}

function responseBody(response) {
  if (response.json !== undefined && response.json !== null) return JSON.stringify(response.json, null, 2);
  return response.body || (response.error ? `Request failed: ${response.error}` : "(Empty response body)");
}

function renderTable(container, rows, emptyMessage) {
  container.replaceChildren();
  if (!rows.length) { const node = document.createElement("p"); node.className = "empty-copy"; node.textContent = emptyMessage; container.append(node); return; }
  const table = document.createElement("table");
  table.className = "response-table";
  const body = document.createElement("tbody");
  rows.forEach(([key, value]) => {
    const row = document.createElement("tr");
    const heading = document.createElement("th"); heading.scope = "row"; heading.textContent = key;
    const cell = document.createElement("td"); cell.textContent = typeof value === "object" ? JSON.stringify(value) : String(value ?? "");
    row.append(heading, cell); body.append(row);
  });
  table.append(body); container.append(table);
}

function renderResponse(response) {
  $("response-loading").hidden = true;
  $("response-empty").hidden = !!response;
  $("response-content").hidden = !response;
  $("response-metrics").replaceChildren();
  if (!response) return;
  const metrics = $("response-metrics");
  const status = document.createElement("span");
  status.className = `metric-status${!response.status || response.status >= 400 ? " error" : response.status >= 300 ? " warning" : ""}`;
  status.textContent = response.status ? `${response.status} ${response.reason || ""}`.trim() : "Request failed";
  metrics.append(status);
  for (const text of [response.elapsed_ms != null ? `${Number(response.elapsed_ms).toLocaleString(undefined, {maximumFractionDigits: 1})} ms` : "", response.size_bytes != null ? response.size_bytes >= 1024 ? `${(response.size_bytes / 1024).toFixed(1)} KB` : `${response.size_bytes} B` : ""]) {
    if (text) { const node = document.createElement("span"); node.textContent = text; metrics.append(node); }
  }
  const notice = [response.error, response.truncated ? "Response body was truncated by the local server." : ""].filter(Boolean).join(" ");
  $("response-notice").hidden = !notice;
  $("response-notice").textContent = notice;
  $("response-body").textContent = responseBody(response);
  renderTable($("response-headers"), Object.entries(response.headers || {}), "No response headers.");
  renderTable($("response-variables"), Object.entries(response.extracted || {}), "No values extracted. Add rules in the Extract tab to pass JSON values between requests.");
  const assertions = response.assertions || [];
  $("response-test-count").textContent = assertions.length ? `${assertions.filter((test) => test.passed).length}/${assertions.length}` : "";
  const tests = $("response-tests"); tests.replaceChildren();
  if (!assertions.length) { const node = document.createElement("p"); node.className = "empty-copy"; node.textContent = "No assertions configured. Add checks in the Tests tab."; tests.append(node); }
  for (const test of assertions) {
    const node = document.createElement("div"); node.className = `test-result${test.passed ? "" : " failed"}`;
    const icon = document.createElement("span"); icon.className = "test-icon"; icon.textContent = test.passed ? "✓" : "×";
    const description = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = `${test.passed ? "Passed" : "Failed"}: ${test.name || "Assertion"}`;
    const message = document.createElement("p"); message.textContent = test.message || "";
    description.append(name, message); node.append(icon, description); tests.append(node);
  }
  setTab("response", state.responseTab);
}

function renderSteps() {
  const steps = state.draft.steps;
  $("sequence-steps").innerHTML = steps.map((step, index) => `<article class="sequence-step" data-step-index="${index}"><div class="step-heading"><span class="step-number">${index + 1}</span><label class="sr-only" for="step-request-${index}">Saved request for step ${index + 1}</label><select id="step-request-${index}" data-step-field="request_id"><option value="">Choose a saved request…</option>${state.workspace.requests.map((request) => `<option value="${escapeHTML(request.id)}"${request.id === step.request_id ? " selected" : ""}>${escapeHTML(request.method)} · ${escapeHTML(request.name)}</option>`).join("")}</select><div class="step-buttons"><button type="button" class="icon-button" data-step-action="up" ${index === 0 ? "disabled" : ""} aria-label="Move step ${index + 1} up" title="Move up">↑</button><button type="button" class="icon-button" data-step-action="down" ${index === steps.length - 1 ? "disabled" : ""} aria-label="Move step ${index + 1} down" title="Move down">↓</button><button type="button" class="icon-button" data-step-action="remove" aria-label="Remove step ${index + 1}" title="Remove step">×</button></div></div><div class="step-requirement"><label class="checkbox-label" for="step-required-${index}"><input id="step-required-${index}" type="checkbox" data-step-field="required" aria-describedby="step-required-help-${index}"${step.required === true ? " checked" : ""}> Required step</label><span id="step-required-help-${index}">Stop the sequence if this step fails, even when continuing after other failures.</span></div><details class="step-overrides"><summary>Step variables <span>· JSON overrides</span></summary><label class="sr-only" for="step-variables-${index}">Variable overrides for step ${index + 1}</label><textarea id="step-variables-${index}" class="code-input compact" data-step-field="_variablesText" spellcheck="false">${escapeHTML(step._variablesText || "{}")}</textarea></details></article>`).join("") || `<div class="editor-card empty-copy">${state.workspace.requests.length ? "Add your first step to start building a sequence." : "Save a request first, then add it as a sequence step."}</div>`;
}

async function runSequence() {
  if (state.kind !== "sequence" || state.startingSequences.has(state.draft.id) || state.runs.get(state.draft.id)?.state === "running") return;
  const sequence = readDraft();
  if (!sequence.steps.length) throw new Error("Add at least one request to the sequence first.");
  const variables = parseJSON($("runtime-variables").value, "Run variables", "object");
  state.startingSequences.add(sequence.id);
  renderExecution();
  let run_id;
  try { ({run_id} = await api("/api/sequences/run", {method: "POST", body: JSON.stringify({sequence, environment_id: state.workspace.active_environment_id || null, variables})})); }
  finally { state.startingSequences.delete(sequence.id); renderExecution(); }
  const run = {id: run_id, state: "running", result: null, sequence};
  state.runs.set(sequence.id, run);
  state.responses.delete(`sequence:${sequence.id}`);
  if (state.kind === "sequence" && state.draft.id === sequence.id) {
    renderExecution();
    renderSequenceResults();
    renderResponse(null);
  }
  pollSequence(sequence.id, run_id);
}

async function pollSequence(sequenceId, runId) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}`);
    const run = state.runs.get(sequenceId);
    if (!run || run.id !== runId) return;
    run.state = data.state;
    run.result = data.result;
    if (state.kind === "sequence" && state.draft.id === sequenceId) { renderExecution(); renderSequenceResults(); }
    if (data.state === "running") setTimeout(() => pollSequence(sequenceId, runId), 650);
    else {
      if (data.result?.steps?.length) {
        const response = data.result.steps[data.result.steps.length - 1].response;
        state.responses.set(`sequence:${sequenceId}`, response);
        if (state.kind === "sequence" && state.draft.id === sequenceId) renderResponse(response);
      }
      toast(data.result?.cancelled ? "Sequence cancelled." : data.result?.ok ? "Sequence completed. All checks passed." : "Sequence completed with failures.", !data.result?.ok && !data.result?.cancelled);
    }
  } catch (error) {
    const run = state.runs.get(sequenceId);
    if (run) { run.state = "completed"; run.result = {ok: false, error: error.message, steps: []}; }
    if (state.kind === "sequence" && state.draft.id === sequenceId) { renderExecution(); renderSequenceResults(); }
    toast(`Could not fetch sequence result: ${error.message}`, true);
  }
}

function renderSequenceResults() {
  const run = state.kind === "sequence" ? state.runs.get(state.draft.id) : null;
  $("sequence-results").hidden = !run;
  if (!run) return;
  const running = run.state === "running";
  const result = run.result;
  $("sequence-run-state").textContent = running ? "Running…" : result?.cancelled ? "Cancelled" : result?.ok ? "Passed" : "Failed";
  $("cancel-sequence").hidden = !running;
  const output = $("sequence-run-output");
  const steps = result?.steps || [];
  output.innerHTML = steps.map((step, index) => `<button type="button" class="sequence-run-row" data-result-index="${index}"><span class="test-icon${step.response?.ok ? "" : " failed"}" style="${step.response?.ok ? "" : "color:#b64242;background:#f8e9e6"}">${step.response?.ok ? "✓" : "×"}</span><strong>${index + 1}. ${escapeHTML(step.name || "Request")}</strong><span class="metric-status${step.response?.ok ? "" : " error"}">${escapeHTML(step.response?.status || "Error")}</span><span class="step-time">${step.response?.elapsed_ms != null ? `${Number(step.response.elapsed_ms).toFixed(0)} ms` : ""}</span></button>`).join("");
  const summary = document.createElement("div");
  summary.className = `sequence-result-summary${result?.error ? " error" : ""}`;
  summary.textContent = running ? steps.length ? `${steps.length} of ${run.sequence.steps.length} steps finished. Running the next request…` : "Starting sequence. Results will appear as steps finish…" : result?.error || `${steps.length} of ${run.sequence.steps.length} steps ran.${result?.cancelled ? " The run was cancelled." : " Select a step to inspect its response."}`;
  output.append(summary);
}

async function cancelSequence() {
  const run = state.runs.get(state.draft.id);
  if (!run || run.state !== "running") return;
  $("cancel-sequence").disabled = true;
  try { await api(`/api/runs/${encodeURIComponent(run.id)}/cancel`, {method: "POST"}); toast("Cancellation requested."); }
  finally { $("cancel-sequence").disabled = false; }
}

function openEnvironment(isNew = false) {
  const env = isNew ? {id: uid(), name: "New environment", base_url: "", api_key: "", variables: [], auth: {type: "none"}} : clone(currentEnvironment());
  if (!env) return;
  state.environmentDraft = env;
  env.variables ||= [];
  env.auth ||= {type: "none"};
  $("environment-dialog-title").textContent = isNew ? "New environment" : "Edit environment";
  $("env-name").value = env.name;
  $("env-url").value = env.base_url || "";
  $("env-api-key").value = env.api_key || "";
  $("env-auth-type").value = env.auth.type;
  $("delete-environment").hidden = isNew;
  renderAuthFields("env", env.auth);
  renderRows("env-variable-rows", env.variables, true);
  $("environment-dialog").showModal();
}

async function saveEnvironment(event) {
  event.preventDefault();
  const env = clone(state.environmentDraft);
  env.name = $("env-name").value.trim();
  env.base_url = $("env-url").value.trim();
  env.api_key = $("env-api-key").value;
  if (!env.name) throw new Error("Give the environment a name.");
  const workspace = clone(state.workspace);
  const index = workspace.environments.findIndex((item) => item.id === env.id);
  if (index < 0) workspace.environments.push(env);
  else workspace.environments[index] = env;
  workspace.active_environment_id = env.id;
  await persist(workspace);
  $("environment-dialog").close();
  toast("Environment saved.");
}

async function deleteEnvironment() {
  const env = state.environmentDraft;
  if (!window.confirm(`Delete the “${env.name}” environment and its variables?`)) return;
  const workspace = clone(state.workspace);
  workspace.environments = workspace.environments.filter((item) => item.id !== env.id);
  if (workspace.active_environment_id === env.id) workspace.active_environment_id = workspace.environments[0]?.id || null;
  await persist(workspace);
  $("environment-dialog").close();
  toast("Environment deleted.");
}

function openGlobals() {
  state.variablesDraft = clone(state.workspace.variables || []);
  renderRows("global-variable-rows", state.variablesDraft, true);
  $("variables-dialog").showModal();
}

async function saveGlobals(event) {
  event.preventDefault();
  const workspace = clone(state.workspace);
  workspace.variables = clone(state.variablesDraft);
  await persist(workspace);
  $("variables-dialog").close();
  toast("Global variables saved.");
}

async function exportWorkspace() {
  const data = await api("/api/export");
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = "api-test-client.workspace.json";
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("Workspace exported with recognized secrets removed. Review custom values before sharing.");
}

async function importWorkspace(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) throw new Error("Workspace files must be smaller than 4 MiB.");
  const workspace = parseJSON(await file.text(), "Workspace file", "object");
  if (state.activeRequests.size || state.startingSequences.size) throw new Error("Wait for active requests to finish before importing a workspace.");
  if (Array.from(state.runs.values()).some((run) => run.state === "running")) throw new Error("Cancel active sequence runs before importing a workspace.");
  if (!window.confirm(`Import “${file.name}” and replace the saved workspace?${state.dirty ? " Unsaved changes will also be discarded." : ""} Export your current workspace first if you want to keep it.`)) return;
  state.workspace = await api("/api/import", {method: "POST", body: JSON.stringify({workspace})});
  state.responses.clear(); state.runs.clear(); state.dirty = false;
  renderEnvironments();
  const request = state.workspace.requests[0];
  selectItem("request", request || newRequest(), !request);
  toast("Workspace imported. Add your credentials before running requests.");
}

function wireEvents() {
  $("workspace-search").addEventListener("input", (event) => { state.search = event.target.value.toLowerCase(); renderNavigation(); });
  for (const list of ["request-list", "sequence-list"]) $(list).addEventListener("click", (event) => {
    const button = event.target.closest("[data-item-id]");
    if (!button) return;
    if (button.dataset.kind === state.kind && button.dataset.itemId === state.draft.id) return;
    const collection = button.dataset.kind === "request" ? "requests" : "sequences";
    const item = state.workspace[collection].find((item) => item.id === button.dataset.itemId);
    if (item) selectItem(button.dataset.kind, item);
  });
  $("new-request").addEventListener("click", () => selectItem("request", newRequest(), true));
  $("new-sequence").addEventListener("click", () => selectItem("sequence", newSequence(), true));
  $("save-item").addEventListener("click", handle(saveItem));
  $("duplicate-item").addEventListener("click", handle(duplicateItem));
  $("delete-item").addEventListener("click", handle(deleteItem));
  $("item-name").addEventListener("input", (event) => { state.draft.name = event.target.value; setDirty(); });
  const fields = {"request-method": "method", "request-url": "url", "request-body": "body", "request-tests": "_testsText", "request-extract": "_extractText"};
  for (const [id, key] of Object.entries(fields)) $(id).addEventListener("input", (event) => { state.draft[key] = event.target.value; setDirty(); if (key === "_testsText" || key === "_extractText") renderCounts(); });
  $("request-timeout").addEventListener("input", (event) => { state.draft.timeout = Number(event.target.value); setDirty(); });
  $("follow-redirects").addEventListener("change", (event) => { state.draft.follow_redirects = event.target.checked; setDirty(); });
  $("body-type").addEventListener("change", (event) => { state.draft.body_type = event.target.value; setDirty(); updateBodyUI(); });
  for (const [button, input, label] of [["format-body", "request-body", "Body"], ["format-tests", "request-tests", "Assertions"], ["format-extract", "request-extract", "Extraction rules"]]) $(button).addEventListener("click", handle(() => {
    const parsed = parseJSON($(input).value, label, button === "format-body" ? undefined : "array");
    $(input).value = JSON.stringify(parsed, null, 2);
    $(input).dispatchEvent(new Event("input", {bubbles: true}));
  }));
  for (const key of ["params", "headers"]) bindRows(`${key}-rows`, () => state.draft[key], false, () => { setDirty(); renderCounts(); });
  document.querySelectorAll("[data-add-row]").forEach((button) => button.addEventListener("click", () => {
    const key = button.dataset.addRow;
    state.draft[key].push({key: "", value: "", enabled: true});
    renderRows(`${key}-rows`, state.draft[key]); setDirty();
    $(`${key}-rows`).querySelectorAll('[data-field="key"]')[state.draft[key].length - 1].focus();
  }));
  for (const scope of ["request", "env"]) {
    const authType = $(scope === "request" ? "request-auth-type" : "env-auth-type");
    authType.addEventListener("change", (event) => {
      const target = scope === "request" ? state.draft : state.environmentDraft;
      target.auth = freshAuth(event.target.value, scope);
      renderAuthFields(scope, target.auth);
      if (scope === "request") setDirty();
    });
    $(`${scope}-auth-fields`).addEventListener("input", (event) => {
      if (!event.target.dataset.authField) return;
      const target = scope === "request" ? state.draft : state.environmentDraft;
      target.auth[event.target.dataset.authField] = event.target.value;
      if (scope === "request") setDirty();
    });
  }
  bindTabs("request"); bindTabs("response");
  $("send-request").addEventListener("click", handle(sendRequest));
  $("copy-response").addEventListener("click", handle(async () => {
    const response = state.responses.get(`${state.kind}:${state.draft.id}`);
    if (!response) return;
    const text = response.json !== undefined && response.json !== null ? JSON.stringify(response.json, null, 2) : response.body || "";
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else { const input = document.createElement("textarea"); input.value = text; document.body.append(input); input.select(); const copied = document.execCommand("copy"); input.remove(); if (!copied) throw new Error("Copy is unavailable in this browser. Select the response text to copy it."); }
    toast("Response body copied.");
  }));
  $("stop-on-failure").addEventListener("change", (event) => { state.draft.stop_on_failure = event.target.checked; setDirty(); });
  $("add-sequence-step").addEventListener("click", () => {
    state.draft.steps.push({id: uid(), request_id: state.workspace.requests[0]?.id || "", variables: {}, required: false, _variablesText: "{}"});
    renderSteps(); setDirty();
  });
  $("sequence-steps").addEventListener("input", (event) => {
    const stepElement = event.target.closest("[data-step-index]");
    const field = event.target.dataset.stepField;
    if (!stepElement || !field) return;
    state.draft.steps[Number(stepElement.dataset.stepIndex)][field] = event.target.type === "checkbox" ? event.target.checked : event.target.value; setDirty();
  });
  $("sequence-steps").addEventListener("click", (event) => {
    const button = event.target.closest("[data-step-action]");
    if (!button) return;
    const index = Number(button.closest("[data-step-index]").dataset.stepIndex);
    const action = button.dataset.stepAction;
    const steps = state.draft.steps;
    if (action === "remove") steps.splice(index, 1);
    else { const next = index + (action === "up" ? -1 : 1); [steps[index], steps[next]] = [steps[next], steps[index]]; }
    renderSteps(); setDirty();
  });
  $("run-sequence").addEventListener("click", handle(runSequence));
  $("cancel-sequence").addEventListener("click", handle(cancelSequence));
  $("sequence-run-output").addEventListener("click", (event) => {
    const button = event.target.closest("[data-result-index]");
    if (!button) return;
    const response = state.runs.get(state.draft.id)?.result?.steps[Number(button.dataset.resultIndex)]?.response;
    if (response) { state.responses.set(`sequence:${state.draft.id}`, response); renderResponse(response); }
  });
  $("environment-picker").addEventListener("change", handle(async (event) => {
    const workspace = clone(state.workspace); workspace.active_environment_id = event.target.value || null;
    try { await persist(workspace); }
    catch (error) { renderEnvironments(); throw error; }
  }));
  $("edit-environment").addEventListener("click", () => openEnvironment());
  $("new-environment").addEventListener("click", () => openEnvironment(true));
  $("environment-form").addEventListener("submit", handle(saveEnvironment));
  $("delete-environment").addEventListener("click", handle(deleteEnvironment));
  bindRows("env-variable-rows", () => state.environmentDraft.variables, true);
  $("add-env-variable").addEventListener("click", () => { state.environmentDraft.variables.push({key: "", value: "", enabled: true, secret: false}); renderRows("env-variable-rows", state.environmentDraft.variables, true); });
  $("global-variables").addEventListener("click", openGlobals);
  $("variables-form").addEventListener("submit", handle(saveGlobals));
  bindRows("global-variable-rows", () => state.variablesDraft, true);
  $("add-global-variable").addEventListener("click", () => { state.variablesDraft.push({key: "", value: "", enabled: true, secret: false}); renderRows("global-variable-rows", state.variablesDraft, true); });
  document.querySelectorAll(".modal-close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }));
  $("export-workspace").addEventListener("click", handle(exportWorkspace));
  $("import-workspace").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", handle(importWorkspace));
  document.addEventListener("keydown", handle(async (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !document.querySelector("dialog[open]")) { event.preventDefault(); if (state.kind === "request") await sendRequest(); else await runSequence(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && !document.querySelector("dialog[open]")) { event.preventDefault(); await saveItem(); }
  }));
  window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
}

async function boot() {
  try {
    const session = await fetch("/api/session", {credentials: "same-origin"});
    if (!session.ok) throw new Error("Cannot open a session with the local server.");
    state.token = (await session.json()).token;
    state.workspace = await api("/api/workspace");
    wireEvents();
    state.dirty = false;
    selectItem("request", state.workspace.requests[0] || newRequest(), !state.workspace.requests.length);
    renderEnvironments();
    $("shortcut-label").textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ Enter" : "Ctrl Enter";
    $("loading-state").hidden = true;
    $("app-content").hidden = false;
  } catch (error) {
    const loading = $("loading-state"); loading.replaceChildren();
    const text = document.createElement("span"); text.textContent = `Could not open workspace: ${error.message}`;
    const button = document.createElement("button"); button.className = "button secondary"; button.textContent = "Retry"; button.addEventListener("click", () => location.reload());
    loading.append(text, button);
  }
}

boot();
