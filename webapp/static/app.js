const COLUMNS = ["business_name", "address", "phone", "website", "email", "rating", "review_count", "score", "reasoning", "contacted"];
const SCORE_ORDER = { HIGH: 0, MEDIUM: 1, LOW: 2 };

let currentLeads = [];
let sortState = { key: null, direction: 1 };

// ---------------------------------------------------------------------------
// Auth — the backend is plain HTTP Basic Auth with no session/cookie, so we
// collect the credential once via a simple login form, keep it in
// sessionStorage for this tab, and attach it to every /api/* call ourselves
// (fetch() doesn't reliably trigger the browser's native Basic Auth prompt
// on a 401 the way a top-level page navigation does).
// ---------------------------------------------------------------------------

const loginOverlay = document.getElementById("login-overlay");
const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");

function getAuthHeader() {
  return sessionStorage.getItem("authHeader");
}

function setAuthHeader(username, password) {
  sessionStorage.setItem("authHeader", "Basic " + btoa(`${username}:${password}`));
}

function clearAuthHeader() {
  sessionStorage.removeItem("authHeader");
}

async function apiFetch(url, options = {}) {
  const headers = Object.assign({}, options.headers, { Authorization: getAuthHeader() });
  const response = await fetch(url, Object.assign({}, options, { headers }));
  if (response.status === 401) {
    clearAuthHeader();
    showLogin("Session expired or invalid credentials — please sign in again.");
  }
  return response;
}

function showLogin(message) {
  loginError.textContent = message || "";
  loginOverlay.classList.remove("hidden");
}

function hideLogin() {
  loginOverlay.classList.add("hidden");
  loadTemplates();
  loadContactedStats();
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = document.getElementById("login-username").value;
  const password = document.getElementById("login-password").value;
  setAuthHeader(username, password);

  // /api/health is unauthenticated (it's a plain liveness check), so it
  // can't be used to verify credentials — /api/history is cheap, has no
  // side effects, and still requires auth.
  const response = await fetch("/api/history", { headers: { Authorization: getAuthHeader() } });
  if (response.ok) {
    hideLogin();
  } else {
    clearAuthHeader();
    loginError.textContent = "Invalid username or password.";
  }
});

if (getAuthHeader()) {
  hideLogin();
} else {
  showLogin("");
}

// ---------------------------------------------------------------------------
// Contacted stats — how many businesses got marked Contacted today / this
// week, refreshed on load and after every checkbox toggle.
// ---------------------------------------------------------------------------

async function loadContactedStats() {
  const response = await apiFetch("/api/contacted/stats");
  if (!response.ok) return;
  const stats = await response.json();
  document.getElementById("stat-today").textContent = stats.today;
  document.getElementById("stat-week").textContent = stats.this_week;
}

// ---------------------------------------------------------------------------
// Templates — Call Script / Email Template text, shared across the team and
// persisted server-side (not just saved in this browser).
// ---------------------------------------------------------------------------

async function loadTemplates() {
  const response = await apiFetch("/api/templates");
  if (!response.ok) return;
  const templates = await response.json();
  for (const [key, data] of Object.entries(templates)) {
    const textarea = document.querySelector(`.template-text[data-key="${key}"]`);
    if (textarea) textarea.value = data.content || "";
  }
}

document.querySelectorAll(".template-save").forEach((button) => {
  button.addEventListener("click", () => saveTemplate(button.dataset.key));
});

async function saveTemplate(key) {
  const textarea = document.querySelector(`.template-text[data-key="${key}"]`);
  const statusEl = document.querySelector(`.template-status[data-key="${key}"]`);
  const button = document.querySelector(`.template-save[data-key="${key}"]`);

  button.disabled = true;
  statusEl.textContent = "Saving…";
  statusEl.className = "template-status";

  try {
    const response = await apiFetch(`/api/templates/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: textarea.value }),
    });

    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Failed to save (HTTP ${response.status})`);
    }

    statusEl.textContent = "Saved";
    statusEl.className = "template-status saved";
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.className = "template-status error";
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab-button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab-button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    button.classList.add("active");
    document.getElementById(button.dataset.tab).classList.add("active");
    if (button.dataset.tab === "history-tab") {
      loadHistory();
    }
    if (button.dataset.tab === "contacted-tab") {
      loadContactedList();
    }
  });
});

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

const form = document.getElementById("search-form");
const statusEl = document.getElementById("status");
const exportButton = document.getElementById("export-button");
const searchButton = document.getElementById("search-button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await runSearch();
});

exportButton.addEventListener("click", () => exportCsv(currentLeads));

function readSearchParams() {
  const websiteValue = document.getElementById("has_website").value;
  return {
    city: document.getElementById("city").value.trim(),
    category: document.getElementById("category").value.trim(),
    min_reviews: numberOrNull(document.getElementById("min_reviews").value),
    max_reviews: numberOrNull(document.getElementById("max_reviews").value),
    min_rating: numberOrNull(document.getElementById("min_rating").value),
    max_rating: numberOrNull(document.getElementById("max_rating").value),
    has_website: websiteValue === "" ? null : websiteValue === "true",
  };
}

function numberOrNull(value) {
  if (value === "" || value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isNaN(n) ? null : n;
}

async function runSearch() {
  const params = readSearchParams();
  setStatus("Searching…", "loading");
  searchButton.disabled = true;
  exportButton.disabled = true;

  try {
    const response = await apiFetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });

    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Search failed (HTTP ${response.status})`);
    }

    const data = await response.json();
    currentLeads = data.leads;
    sortState = { key: null, direction: 1 };
    renderTable(currentLeads);
    setStatus(`Found ${currentLeads.length} lead${currentLeads.length === 1 ? "" : "s"}.`, "");
    exportButton.disabled = currentLeads.length === 0;
  } catch (err) {
    setStatus(err.message, "error");
  } finally {
    searchButton.disabled = false;
  }
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function setStatus(message, kind) {
  statusEl.textContent = message;
  statusEl.className = "status" + (kind ? ` ${kind}` : "");
}

// ---------------------------------------------------------------------------
// Results table (sortable)
// ---------------------------------------------------------------------------

const tableBody = document.querySelector("#results-table tbody");

document.querySelectorAll("#results-table th").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (sortState.key === key) {
      sortState.direction *= -1;
    } else {
      sortState = { key, direction: 1 };
    }
    renderTable(currentLeads);
  });
});

function renderTable(leads) {
  let rows = leads.slice();
  if (sortState.key) {
    rows.sort((a, b) => compareLeads(a, b, sortState.key) * sortState.direction);
  }

  tableBody.innerHTML = "";
  for (const lead of rows) {
    tableBody.appendChild(buildRow(lead));
  }

  document.querySelectorAll("#results-table th").forEach((th) => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.key === sortState.key) {
      th.classList.add(sortState.direction === 1 ? "sorted-asc" : "sorted-desc");
    }
  });
}

function compareLeads(a, b, key) {
  if (key === "score") {
    return SCORE_ORDER[a.score] - SCORE_ORDER[b.score];
  }
  const av = a[key];
  const bv = b[key];
  if (av === null || av === undefined) return bv === null || bv === undefined ? 0 : -1;
  if (bv === null || bv === undefined) return 1;
  if (typeof av === "number" && typeof bv === "number") return av - bv;
  return String(av).localeCompare(String(bv));
}

function buildRow(lead) {
  const tr = document.createElement("tr");

  tr.appendChild(cell(lead.business_name));
  tr.appendChild(cell(lead.address, "wrap"));
  tr.appendChild(cell(lead.phone));

  const websiteTd = document.createElement("td");
  if (lead.website) {
    const a = document.createElement("a");
    a.href = lead.website;
    a.textContent = lead.website;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    websiteTd.appendChild(a);
  }
  tr.appendChild(websiteTd);

  const emailTd = document.createElement("td");
  if (lead.email) {
    const a = document.createElement("a");
    a.href = `mailto:${lead.email}`;
    a.textContent = lead.email;
    emailTd.appendChild(a);
  }
  tr.appendChild(emailTd);

  tr.appendChild(cell(lead.rating === null || lead.rating === undefined ? "" : lead.rating));
  tr.appendChild(cell(lead.review_count));

  const scoreTd = document.createElement("td");
  const pill = document.createElement("span");
  pill.className = `score-pill score-${lead.score}`;
  pill.textContent = lead.score;
  scoreTd.appendChild(pill);
  tr.appendChild(scoreTd);

  tr.appendChild(cell(lead.reasoning, "wrap"));

  const contactedTd = document.createElement("td");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = !!lead.contacted;
  checkbox.title = contactedTitle(lead);
  checkbox.addEventListener("change", () => toggleContacted(lead, checkbox, tr));
  contactedTd.appendChild(checkbox);
  tr.appendChild(contactedTd);

  tr.classList.toggle("row-contacted", !!lead.contacted);

  return tr;
}

function contactedTitle(lead) {
  if (!lead.contacted_at) return "Mark as contacted";
  return `Marked contacted ${new Date(lead.contacted_at * 1000).toLocaleString()}`;
}

function contactedRequestBody(lead, contacted) {
  // Business details are only needed when checking the box — they seed the
  // Contacted list so it doesn't depend on old search history sticking
  // around. Unchecking only needs place_id/contacted; the backend keeps
  // whatever details it already has on file.
  return {
    place_id: lead.place_id,
    contacted,
    business_name: lead.business_name,
    address: lead.address,
    phone: lead.phone,
    website: lead.website,
    email: lead.email,
    city: lead.city,
    category: lead.category,
    rating: lead.rating,
    review_count: lead.review_count,
    score: lead.score,
  };
}

async function toggleContacted(lead, checkbox, tr) {
  const nextValue = checkbox.checked;
  checkbox.disabled = true;

  try {
    const response = await apiFetch("/api/contacted", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(contactedRequestBody(lead, nextValue)),
    });

    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Failed to update (HTTP ${response.status})`);
    }

    const data = await response.json();
    lead.contacted = data.contacted;
    lead.contacted_at = data.contacted_at;
    checkbox.title = contactedTitle(lead);
    tr.classList.toggle("row-contacted", lead.contacted);
    loadContactedStats();
  } catch (err) {
    checkbox.checked = !nextValue;
    setStatus(err.message, "error");
  } finally {
    checkbox.disabled = false;
  }
}

function cell(value, className) {
  const td = document.createElement("td");
  td.textContent = value === null || value === undefined ? "" : value;
  if (className) td.className = className;
  return td;
}

// ---------------------------------------------------------------------------
// CSV export (client-side, from whatever is currently in the table)
// ---------------------------------------------------------------------------

function exportCsv(leads) {
  const header = COLUMNS;
  const lines = [header.join(",")];

  for (const lead of leads) {
    lines.push(header.map((key) => csvEscape(formatForCsv(lead, key))).join(","));
  }

  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "leads.csv";
  a.click();
  URL.revokeObjectURL(url);
}

function formatForCsv(lead, key) {
  if (key === "contacted") return lead.contacted ? "Yes" : "No";
  return lead[key];
}

function csvEscape(value) {
  const str = value === null || value === undefined ? "" : String(value);
  if (/[",\n]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

const historyStatus = document.getElementById("history-status");
const historyBody = document.querySelector("#history-table tbody");

async function loadHistory() {
  historyStatus.textContent = "Loading…";
  historyStatus.className = "status loading";
  try {
    const response = await apiFetch("/api/history");
    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Failed to load history (HTTP ${response.status})`);
    }
    const entries = await response.json();
    renderHistory(entries);
    historyStatus.textContent = entries.length ? "" : "No past searches yet.";
    historyStatus.className = "status";
  } catch (err) {
    historyStatus.textContent = err.message;
    historyStatus.className = "status error";
  }
}

function renderHistory(entries) {
  historyBody.innerHTML = "";
  for (const entry of entries) {
    const tr = document.createElement("tr");
    tr.dataset.searchId = entry.id;
    tr.appendChild(cell(new Date(entry.timestamp * 1000).toLocaleString()));
    tr.appendChild(cell(entry.city));
    tr.appendChild(cell(entry.category));
    tr.appendChild(cell(entry.result_count));
    tr.addEventListener("click", () => loadHistoryEntry(entry.id));
    historyBody.appendChild(tr);
  }
}

async function loadHistoryEntry(searchId) {
  historyStatus.textContent = "Loading…";
  historyStatus.className = "status loading";
  try {
    const response = await apiFetch(`/api/history/${searchId}`);
    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Failed to load search (HTTP ${response.status})`);
    }
    const detail = await response.json();
    currentLeads = detail.results;
    sortState = { key: null, direction: 1 };
    renderTable(currentLeads);
    exportButton.disabled = currentLeads.length === 0;

    document.getElementById("city").value = detail.city;
    document.getElementById("category").value = detail.category;

    document.querySelectorAll(".tab-button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    document.querySelector('[data-tab="search-tab"]').classList.add("active");
    document.getElementById("search-tab").classList.add("active");
    setStatus(`Loaded ${currentLeads.length} lead(s) from search #${searchId}.`, "");
  } catch (err) {
    historyStatus.textContent = err.message;
    historyStatus.className = "status error";
  }
}

// ---------------------------------------------------------------------------
// Contacted list — every business currently marked Contacted, with the
// business details captured at the moment it was checked.
// ---------------------------------------------------------------------------

const contactedStatus = document.getElementById("contacted-status");
const contactedBody = document.querySelector("#contacted-table tbody");

async function loadContactedList() {
  contactedStatus.textContent = "Loading…";
  contactedStatus.className = "status loading";
  try {
    const response = await apiFetch("/api/contacted/list");
    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Failed to load contacted list (HTTP ${response.status})`);
    }
    const entries = await response.json();
    renderContactedList(entries);
    contactedStatus.textContent = entries.length ? "" : "No businesses marked Contacted yet.";
    contactedStatus.className = "status";
  } catch (err) {
    contactedStatus.textContent = err.message;
    contactedStatus.className = "status error";
  }
}

function renderContactedList(entries) {
  contactedBody.innerHTML = "";
  for (const entry of entries) {
    contactedBody.appendChild(buildContactedRow(entry));
  }
}

function buildContactedRow(entry) {
  const tr = document.createElement("tr");

  tr.appendChild(cell(entry.business_name));
  tr.appendChild(cell(entry.address, "wrap"));
  tr.appendChild(cell(entry.phone));

  const websiteTd = document.createElement("td");
  if (entry.website) {
    const a = document.createElement("a");
    a.href = entry.website;
    a.textContent = entry.website;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    websiteTd.appendChild(a);
  }
  tr.appendChild(websiteTd);

  const emailTd = document.createElement("td");
  if (entry.email) {
    const a = document.createElement("a");
    a.href = `mailto:${entry.email}`;
    a.textContent = entry.email;
    emailTd.appendChild(a);
  }
  tr.appendChild(emailTd);

  tr.appendChild(cell(entry.city));
  tr.appendChild(cell(entry.category));
  tr.appendChild(cell(entry.contacted_at ? new Date(entry.contacted_at * 1000).toLocaleString() : ""));

  const actionTd = document.createElement("td");
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.textContent = "Remove";
  removeButton.className = "remove-button";
  removeButton.addEventListener("click", () => removeContacted(entry.place_id, tr, removeButton));
  actionTd.appendChild(removeButton);
  tr.appendChild(actionTd);

  return tr;
}

async function removeContacted(placeId, tr, button) {
  button.disabled = true;
  try {
    const response = await apiFetch("/api/contacted", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ place_id: placeId, contacted: false }),
    });

    if (!response.ok) {
      const body = await safeJson(response);
      throw new Error(body?.detail || `Failed to update (HTTP ${response.status})`);
    }

    tr.remove();
    loadContactedStats();
    if (!contactedBody.children.length) {
      contactedStatus.textContent = "No businesses marked Contacted yet.";
      contactedStatus.className = "status";
    }

    // Keep the results table's checkbox in sync if this lead is showing there.
    const stale = currentLeads.find((lead) => lead.place_id === placeId);
    if (stale) {
      stale.contacted = false;
      stale.contacted_at = null;
      renderTable(currentLeads);
    }
  } catch (err) {
    contactedStatus.textContent = err.message;
    contactedStatus.className = "status error";
  } finally {
    button.disabled = false;
  }
}
