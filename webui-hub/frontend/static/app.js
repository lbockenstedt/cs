/* ═══════════════════════════════════════════════════════════════
   cs-webui — Unified Hub + Spoke Frontend
   WEBUI_MODE injected by the backend server at runtime.
   ═══════════════════════════════════════════════════════════════ */

// ── Mode detection ──────────────────────────────────────────────
let WEBUI_MODE = window.WEBUI_MODE || '';
function applyModeClass(mode) {
  const update = () => {
    if (!document.body) return;
    document.body.classList.remove('mode-hub', 'mode-spoke');
    if (mode === 'hub' || mode === 'spoke') document.body.classList.add(`mode-${mode}`);
  };
  if (document.body) { update(); return; }
  document.addEventListener('DOMContentLoaded', update, { once: true });
}

function setWebuiMode(mode) {
  WEBUI_MODE = mode === 'hub' ? 'hub' : 'spoke';
  window.WEBUI_MODE = WEBUI_MODE;
  applyModeClass(WEBUI_MODE);
  return WEBUI_MODE;
}

function consumeInitPayload() {
  const init = window.__CS_WEBUI_INIT__ || null;
  window.__CS_WEBUI_INIT__ = null;
  return init;
}

const MODE_DETECTION_TIMEOUT_MS = 1500;

async function fetchInitPayload(timeoutMs = 0) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timeoutId = controller && timeoutMs > 0
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;
  try {
    const response = await fetch('/api/init', {
      headers: { Accept: 'application/json' },
      signal: controller?.signal,
    });
    if (!response?.ok) return null;
    return await response.json().catch(() => ({}));
  } catch (_) {
    return null;
  } finally {
    if (timeoutId) window.clearTimeout(timeoutId);
  }
}

async function detectWebuiMode() {
  const init = await fetchInitPayload(MODE_DETECTION_TIMEOUT_MS);
  const mode = String(init?.mode || '').trim().toLowerCase();
  if (mode === 'hub' || mode === 'spoke') {
    window.__CS_WEBUI_INIT__ = init;
    return setWebuiMode(mode);
  }
  return setWebuiMode(WEBUI_MODE === 'hub' ? 'hub' : 'spoke');
}

applyModeClass(WEBUI_MODE);

// ════════════════════════════════════════════════════════════════
// SPOKE — booted after /api/init mode detection
// ════════════════════════════════════════════════════════════════
function startSpokeApp() {
  (function () {

const FLAG_ORDER = [
  'kill_switch',
  'dns_fail',
  'iperf',
  'download',
  'www_traffic',
  'ping_test',
  'ssidpw_fail',
  'auth_fail',
  'dhcp_fail',
  'port_flap',
  'assoc_fail'
];

const FAILURE_SIMS = new Set(['dns_fail', 'ssidpw_fail', 'auth_fail', 'dhcp_fail', 'port_flap', 'assoc_fail']);
const TRAFFIC_SIMS = new Set(['iperf', 'download', 'www_traffic', 'ping_test']);
const IMPACT_LABELS = {
  dns_fail: '⚠ DNS Failure',
  ssidpw_fail: '⚠ Auth Failure',
  auth_fail: '⚠ Auth Failure',
  dhcp_fail: '⚠ DHCP Failure',
  assoc_fail: '⚠ Assoc Failure',
  port_flap: '⚠ Port Flap',
  iperf: 'ℹ iPerf Traffic',
  download: 'ℹ Download Traffic',
  www_traffic: 'ℹ Web Traffic',
  ping_test: 'ℹ Ping Traffic'
};
// ── Dynamic simulation.conf editor helpers ────────────────────────
const BUCKET_SECTION_RE = /^s\d+$/;
const BOOL_VALUE_SET = new Set(['on', 'off', 'yes', 'no', 'true', 'false']);
const PW_KEY_RE = /pw$|password|secret/i;
const KNOWN_SECTION_LABELS = { simulation: 'Simulation', server: 'Server', address: 'IP Addresses' };
function _fmtConfigKey(k) { return k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
function _fmtSection(s) { return KNOWN_SECTION_LABELS[s] || s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' '); }
function _isBoolVal(v) { return BOOL_VALUE_SET.has(String(v ?? '').toLowerCase().trim()); }

const clients = new Map();
const rowRefs = new Map();
const tbody = document.getElementById('clients-body');
const emptyRow = document.getElementById('empty-row');
const clientCount = document.getElementById('client-count');
const wsDot = document.getElementById('ws-dot');
const wsText = document.getElementById('ws-text');
const repoDot = document.getElementById('repo-dot');
const repoText = document.getElementById('repo-text');
let socket = null;
let reconnectTimer = null;
let updateWasInProgress = false;  // track if update was running when WS dropped
let openControlHost = null;
let centralSiteDetailOpen = null;
let centralStatusData = {};
let centralWirelessClients = {};   // wsite → client count from Central API
let hwAlertsData    = [];   // latest hardware_alerts array from WS
let clientCountData = {};   // wsite → { site_name, current, hourly_avg, drop_pct, status, ts }
let _hwRowsCache    = [];   // cached hw check rows for renderHwPanel
let _ccRowsCache    = [];   // cached cc check rows for renderCcPanel
let availableChecks = { alerts: [], insights: [] };
let currentSettings = {
  repo_url: '',
  repo_branch: '',
  github_token_configured: false,
  hub_managed: false,
  central_api: {
    mode: 'classic',
    classic: { url: '', username: '', password_configured: false },
    central: { url: '', client_id: '', customer_id: '', client_secret_configured: false }
  },
  central_config: {
    api_version: 'classic',
    cluster_url: '',
    access_token_configured: false,
    refresh_token_configured: false,
    client_id: '',
    client_secret_configured: false,
    customer_id: ''
  },
  site_mappings: {},
  monitored_checks: [],
  hardware_checks: [],
  relay_enabled: 'off',
  relay_server_url: '',
  hub_tls_verify: 'off',
  relay_spoke_name: '',
  relay_tenant_hint: '',
  relay_tenant_id: '',
  relay_spoke_id: '',
  relay_poll_interval: 60,
  relay_api_key_configured: false,
  admin_password_configured: false,
  session_timeout_minutes: 30,
  auth_provider: 'local',
  auth_ldap_url: '',
  auth_ldap_bind_dn: '',
  auth_ldap_bind_password_configured: false,
  auth_ldap_user_base: '',
  auth_ldap_user_filter: '(&(objectClass=user)(sAMAccountName={username}))',
  auth_ldap_group_admin: '',
  auth_ldap_group_viewer: '',
  auth_radius_host: '',
  auth_radius_port: 1812,
  auth_radius_secret_configured: false,
  auth_radius_role_attr: 'Filter-Id',
  auth_radius_admin_val: 'admin',
  auth_tacacs_host: '',
  auth_tacacs_port: 49,
  auth_tacacs_secret_configured: false,
  auth_tacacs_admin_priv: 15,
  notifications: {
    email_enabled: false,
    smtp_host: '',
    smtp_port: 587,
    smtp_user: '',
    smtp_from: '',
    smtp_to: [],
    smtp_password_configured: false,
    teams_enabled: false,
    teams_webhook_url_configured: false,
  },
  repo_sync_interval: 300,
  usb_vidpids: '[]',
  usb_missing_timeout: '60',
  usb_max_slots: '24',
  vm_image_1_template_id: '100',
  vm_image_2_template_id: '200',
  vm_image_1_pct: '50',
  usb_auto_provision: 'off',
  usb_ignored_vidpids: '[]',
  ignored_hostnames: '["sim-rpi-0000"]',
  vm_silent_timeout: '24',
  reclone_schedule_enabled: 'off',
  reclone_schedule_cron: 'sunday 02:00',
  reclone_concurrency: '1',
  l1_vlan_start: '100',
  l1_vlan_end: '199'
};
let configData = {};
let configLoaded = false;
let centralTokenValid = null;
let centralLastSyncedTs = null;
let centralStatusInitialized = false;
let latestProxmoxData = { vms: [], usb_state: [], unknown_usb: [], reclone_state: null, vh_devices: null };  // physical_usb removed
let latestRecloneState = null;
let usbCountdownTimer = null;
let activeVmCat = 'sim';   // 'sim' | 'other' | 'containers' | 'templates'
let webuiVmid = null;      // VMID of the LXC container running this service (protected from delete)
const spokeRoot = document.getElementById('spoke-root');
const spokeTabPanels = document.querySelectorAll('#spoke-root .tab-content');
let activeSpokeTab = document.querySelector('#tab-nav .spoke-only .tab.active')?.dataset.tab || 'simulations';
let activeServerSubtab = spokeRoot?.querySelector('.server-subtab.active')?.dataset.subtab || 'server-vms';
let refreshPaused = false;
let refreshCountdownTimer = null;
let refreshSecondsLeft = 10;
let refreshIntervalSeconds = 10;
const SECRET_CONFIGURED_PLACEHOLDER = '**********';
const refreshActiveTabs = new Set(['dashboard', 'api-server']);
let clientTypeFilter = 'all';

function getSecretInputDefaultPlaceholder(input) {
  if (!input) return '';
  if (!('placeholderDefault' in input.dataset)) {
    input.dataset.placeholderDefault = input.getAttribute('placeholder') || '';
  }
  return input.dataset.placeholderDefault;
}

function isMaskedSecretValue(value) {
  return typeof value === 'string' && value.trim() !== '' && /^[*•]+$/.test(value.trim());
}

function isConfiguredSecretValue(value) {
  return value === true || value === 'true' || isMaskedSecretValue(value);
}

function setSecretInputConfigured(input, configured) {
  if (!input) return;
  const defaultPlaceholder = getSecretInputDefaultPlaceholder(input);
  input.value = '';
  input.dataset.dirty = 'false';
  input.classList.toggle('secret-configured', Boolean(configured));
  if (configured) {
    input.dataset.configured = 'true';
    input.placeholder = SECRET_CONFIGURED_PLACEHOLDER;
    return;
  }
  delete input.dataset.configured;
  input.placeholder = defaultPlaceholder;
}

function getSecretInputPayload(input) {
  if (!input) return { include: false, value: '' };
  const value = input.value ?? '';
  if (value === '' && input.dataset.configured === 'true' && input.dataset.dirty !== 'true') {
    return { include: false, value: '' };
  }
  return { include: true, value };
}

function resetSecretInput(input) {
  if (!input) return;
  input.value = '';
  input.dataset.dirty = 'false';
  input.placeholder = input.dataset.configured === 'true'
    ? SECRET_CONFIGURED_PLACEHOLDER
    : getSecretInputDefaultPlaceholder(input);
}

function bindSecretInput(input) {
  if (!input || input.dataset.secretBound === 'true') return;
  getSecretInputDefaultPlaceholder(input);
  input.dataset.secretBound = 'true';
  input.addEventListener('focus', () => {
    if (input.dataset.configured === 'true') {
      input.placeholder = getSecretInputDefaultPlaceholder(input);
    }
  });
  input.addEventListener('input', () => {
    input.dataset.dirty = 'true';
    input.placeholder = getSecretInputDefaultPlaceholder(input);
  });
  input.addEventListener('blur', () => {
    if (input.dataset.configured === 'true' && input.dataset.dirty !== 'true' && !input.value) {
      input.placeholder = SECRET_CONFIGURED_PLACEHOLDER;
    }
  });
}
const refreshActiveServerSubtabs = new Set(['server-vms', 'server-commands']);

// ── Tab navigation ────────────────────────────────────────────────
const spokeNavTabs = document.querySelectorAll('#tab-nav .spoke-only .tab');
spokeNavTabs.forEach((tab) => {
  tab.addEventListener('click', () => {
    spokeNavTabs.forEach((t) => {
      t.classList.remove('active');
      t.setAttribute('aria-selected', 'false');
    });
    spokeTabPanels.forEach((panel) => panel.classList.add('hidden'));

    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    activeSpokeTab = tab.dataset.tab;
    document.getElementById(`tab-${tab.dataset.tab}`)?.classList.remove('hidden');
    if (tab.dataset.tab === 'setup') activateSetupSubtab('setup-github');
    if (tab.dataset.tab === 'server') { activateServerSubtab('server-vms'); loadProxmoxApproved().catch(() => {}); }
    if (tab.dataset.tab === 'api-server') { renderServiceStatus().catch(() => {}); }
    if (tab.dataset.tab === 'central') { activateCentralSubtab('central-sites-panel'); }
    if (tab.dataset.tab === 'simulations') { activateSimTopTab('simtop-checks'); }
    resetTabDrilldowns(tab.dataset.tab);
    updateRefreshPausedState();
  });
});

// Reset any open drill-down panels back to the overview when the top-level
// tab is clicked — so you never land in a stale detail view.
function resetTabDrilldowns(tabName) {
  if (tabName === 'central' || tabName === 'simulations') {
    // Central site detail
    if (typeof closeSiteDetail === 'function') closeSiteDetail();
    // Sim check detail
    if (simDetail) simDetail.classList.add('hidden');
    if (simOverview) simOverview.classList.remove('hidden');
    // Sim clients panel
    if (simClientsPanel) simClientsPanel.classList.add('hidden');
    // HW alert detail
    if (hwDetailPanel) hwDetailPanel.classList.add('hidden');
    const hwOverview = document.getElementById('hw-overview');
    if (hwOverview) hwOverview.classList.remove('hidden');
    // Client count detail
    if (ccDetailPanel) ccDetailPanel.classList.add('hidden');
    const ccOverview = document.getElementById('cc-overview');
    if (ccOverview) ccOverview.classList.remove('hidden');
  }
}

async function hydrateSetupSubtab(subtabId) {
  if (!subtabId) return;

  if (subtabId === 'setup-troubleshoot') {
    await loadSystemHealth();
    return;
  }

  const loadSetupSettings = async () => {
    try {
      await loadSettings();
    } catch (error) {
      const activePanel = document.querySelector('#tab-setup .setup-subpanel:not(.hidden)');
      const existingMsg = activePanel?.querySelector('.settings-message, [role="alert"]');
      if (existingMsg) {
        existingMsg.textContent = `Could not load settings: ${error.message}`;
        existingMsg.classList.remove('hidden');
        existingMsg.classList.add('error');
      }
    }
  };

  if (subtabId === 'setup-notifications') {
    await loadSetupSettings();
    return;
  }

  await loadSetupSettings();

  if (subtabId === 'setup-central') {
    await loadCentralStatus().catch(() => {});
  }
}

function activateSetupSubtab(subtabId = 'setup-github') {
  setupSubtabButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.subtab === subtabId);
  });
  setupSubpanels.forEach((panel) => {
    const isActive = panel.id === subtabId;
    panel.classList.toggle('active', isActive);
    panel.classList.toggle('hidden', !isActive);
  });
  void hydrateSetupSubtab(subtabId);
}

function activateConfigSubtab(subtabId = 'config-general') {
  document.querySelectorAll('.config-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.subtab === subtabId);
  });
  document.querySelectorAll('.config-subpanel').forEach((panel) => {
    const isActive = panel.id === subtabId;
    panel.classList.toggle('active', isActive);
    panel.classList.toggle('hidden', !isActive);
  });
}

function activateServerSubtab(subtabId = 'server-vms') {
  activeServerSubtab = subtabId;
  document.querySelectorAll('.server-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.subtab === subtabId);
  });
  ['server-node', 'server-vms', 'server-usb', 'server-t3', 'server-vh', 'server-commands'].forEach((id) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    const isActive = id === subtabId;
    panel.classList.toggle('active', isActive);
    panel.classList.toggle('hidden', !isActive);
  });
  if (subtabId === 'server-vh') renderVhDevices(latestProxmoxData);
  updateRefreshPausedState();
}

// ── Agent Log Viewer ─────────────────────────────────────────────────────
const agentLogViewer = document.getElementById('agent-log-viewer');
const agentLogFilter = document.getElementById('agent-log-filter');
const agentLogClear  = document.getElementById('agent-log-clear');
let agentLogLines = [];   // full buffer
let agentLogAutoScroll = true;

function classifyLogLine(line) {
  const t = line.toLowerCase();
  if (/error|failed|fail|exception|critical/.test(t)) return 'log-err';
  if (/warning|warn/.test(t)) return 'log-warn';
  if (/completed|success|recloned|approved|started/.test(t)) return 'log-ok';
  return '';
}

function renderAgentLog() {
  if (!agentLogViewer) return;
  const filter = agentLogFilter ? agentLogFilter.value.toLowerCase() : '';
  const filtered = filter ? agentLogLines.filter((l) => l.toLowerCase().includes(filter)) : agentLogLines;
  agentLogViewer.textContent = '';
  for (const line of filtered) {
    const el = document.createElement('div');
    el.className = `agent-log-line ${classifyLogLine(line)}`;
    el.textContent = line;
    agentLogViewer.appendChild(el);
  }
  if (agentLogAutoScroll) agentLogViewer.scrollTop = agentLogViewer.scrollHeight;
}

async function loadAgentLogs() {
  try {
    const data = await requestJson('/api/proxmox/logs');
    agentLogLines = data.lines || [];
    if (!agentLogLines.length) {
      const hint = 'No logs yet — logs arrive on the next agent telemetry poll (≤60s after activity).';
      if (agentLogViewer) {
        agentLogViewer.textContent = '';
        const el = document.createElement('div');
        el.className = 'agent-log-line log-warn';
        el.textContent = hint;
        agentLogViewer.appendChild(el);
      }
      return;
    }
    renderAgentLog();
  } catch (e) {
    if (agentLogViewer) {
      agentLogViewer.textContent = `Failed to load logs: ${e.message}`;
    }
  }
}

function appendAgentLogLines(lines) {
  agentLogLines.push(...lines);
  if (agentLogLines.length > 500) agentLogLines.splice(0, agentLogLines.length - 500);
  // Only re-render if Logs tab is visible
  const panel = document.getElementById('server-logs');
  if (panel && !panel.classList.contains('hidden')) renderAgentLog();
}

if (agentLogFilter) agentLogFilter.addEventListener('input', renderAgentLog);
if (agentLogViewer) {
  agentLogViewer.addEventListener('scroll', () => {
    const atBottom = agentLogViewer.scrollHeight - agentLogViewer.scrollTop - agentLogViewer.clientHeight < 40;
    agentLogAutoScroll = atBottom;
  });
}
if (agentLogClear) {
  agentLogClear.addEventListener('click', async () => {
    await fetch('/api/proxmox/logs/clear', { method: 'POST' }).catch(() => {});
    agentLogLines = [];
    renderAgentLog();
  });
}

// ── Central sub-tabs ──────────────────────────────────────────
const centralSubPanels = ['central-sites-panel', 'central-alerts-panel', 'central-clients-panel', 'central-history-panel'];

function activateCentralSubtab(subtabId = 'central-sites-panel') {
  document.querySelectorAll('.central-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.subtab === subtabId);
  });
  centralSubPanels.forEach((id) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    panel.classList.toggle('active', id === subtabId);
    panel.classList.toggle('hidden', id !== subtabId);
  });
  if (subtabId === 'central-alerts-panel') renderCentralAllAlerts();
  if (subtabId === 'central-clients-panel') renderCentralClients();
  if (subtabId === 'central-history-panel') renderCentralAllHistory();
}

document.querySelectorAll('.central-subtab').forEach((btn) => {
  btn.addEventListener('click', () => activateCentralSubtab(btn.dataset.subtab));
});


let activeSimTab = 'failing';

function activateSimSubtab(tabId) {
  activeSimTab = tabId;
  document.querySelectorAll('.sim-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.simtab === tabId);
  });
  renderChecksList();
}

/** Classify a check row into failing / functional / warning based on status + staleness */
function getEffectiveTabForItem(item) {
  const now = Date.now() / 1000;
  let cls = item.dotCls;
  if (item.ts) {
    const ageMin = (now - item.ts) / 60;
    if (ageMin > 60) cls = 'dot-err';       // stale >1 h → fail
    else if (ageMin > 15) cls = 'dot-warn'; // stale 15–60 m → warning
  }
  if (cls === 'dot-err') return 'failing';
  if (cls === 'dot-ok') return 'functional';
  return 'warning'; // dot-warn, dot-unknown
}

// ── Repo sync status ──────────────────────────────────────────────
let lastKnownSyncTime = null;   // preserve across "Syncing…" broadcasts that omit last_sync

const simDisabledState = { global: false, local: false };

function renderSimDisabledBanner() {
  const banner = document.getElementById('gkill-indicator');
  if (!banner) return;
  const { global: g, local: l } = simDisabledState;
  if (!g && !l) {
    banner.style.display = 'none';
    document.title = 'Client Simulator';
    return;
  }
  const scope = g && l ? 'Globally & Locally' : g ? 'Globally' : 'Locally';
  const tip = g && l
    ? 'Kill switch active in both the global repo and local config'
    : g ? 'Global kill switch ON in solutions-hpe/main — all islands affected'
        : 'Local kill switch ON in simulation.conf — this spoke only';
  banner.textContent = `🛑 Simulation Disabled — ${scope}`;
  banner.title = tip;
  banner.style.display = '';
  document.title = `🛑 Simulation Disabled (${scope}) — Client Simulator`;
}

function applyGkillSwitch(value) {
  simDisabledState.global = value === 'on';
  renderSimDisabledBanner();
}

function setRelayStatus(data = {}) {
  updateRelayIndicatorVisibility();
  const stateText = document.getElementById('relay-state-text');
  const lastTime = document.getElementById('relay-last-time');
  const lastError = document.getElementById('relay-last-error');
  const spokeIdDisplay = document.getElementById('relay-spoke-id-display');
  const apikeyStatus = document.getElementById('relay-apikey-status');

  const isNameConflict = data.registration_status === 'name_conflict' || (data.error || '').startsWith('name_conflict:');
  const isPending = data.enabled && (data.registration_status === 'pending' || data.api_key_configured === false) && !data.connected && !isNameConflict;

  if (stateText) stateText.textContent = !data.enabled ? 'Disabled' : data.connected ? '✓ Connected' : isNameConflict ? '✗ Name conflict' : data.error ? '✗ Error' : data.registration_status === 'pending' ? 'Pending approval' : 'Enabled';
  if (lastTime) lastTime.textContent = data.last_sync ? new Date(data.last_sync * 1000).toLocaleTimeString() : '—';
  if (lastError) lastError.textContent = data.error || '—';
  if (spokeIdDisplay) spokeIdDisplay.textContent = data.spoke_id || '—';
  if (apikeyStatus) apikeyStatus.textContent = data.api_key_configured ? '✓ Received' : 'Pending approval';

  const dotEl = document.getElementById('relay-indicator-dot');
  const textEl = document.getElementById('relay-indicator-text');
  if (dotEl) {
    if (data.connected) {
      dotEl.className = 'ind-dot green';
      dotEl.title = `Hub connected — last sync: ${new Date((data.last_sync || 0) * 1000).toLocaleTimeString()}`;
    } else if (isPending) {
      dotEl.className = 'ind-dot grey';
      dotEl.title = 'Awaiting hub approval';
    } else {
      dotEl.className = 'ind-dot red';
      dotEl.title = `Hub disconnected: ${data.error || 'unknown'}`;
    }
  }
  if (textEl) {
    textEl.textContent = isPending ? 'Hub: Pending' : '';
    textEl.style.display = isPending ? '' : 'none';
  }
}

function setRepoStatus(synced, error, lastSync, repoVersion) {
  if (lastSync) lastKnownSyncTime = lastSync;   // only update when we have a real value

  repoDot.className = `status-dot ${synced ? 'online' : error ? 'offline' : 'warning'}`;
  repoText.textContent = error ? 'Error' : synced ? 'Synced' : 'Syncing…';

  // Build tooltip: show error or last-synced timestamp
  let tip = 'GitHub Sync Status';
  if (error) {
    tip = lastKnownSyncTime
      ? `Error: ${error} — last successful sync: ${new Date(lastKnownSyncTime * 1000).toLocaleTimeString()}`
      : `Error: ${error}`;
  } else if (lastKnownSyncTime) {
    const d = new Date(lastKnownSyncTime * 1000);
    tip = `Last synced: ${d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'})} on ${d.toLocaleDateString()}`;
  }
  const repoStatus = document.getElementById('repo-status');
  if (repoStatus) repoStatus.title = tip;
  repoText.title = '';

  // Update setup tab status panel
  const syncState   = document.getElementById('setup-sync-state');
  const syncError   = document.getElementById('setup-sync-error');
  const syncTime    = document.getElementById('setup-sync-time');
  const syncVersion = document.getElementById('setup-repo-version');
  if (syncState)   syncState.textContent   = synced ? '✓ Synced' : error ? '✗ Failed' : 'Syncing…';
  if (syncError)   syncError.textContent   = error || '—';
  if (syncVersion) syncVersion.textContent = repoVersion || '—';
  if (syncTime && lastKnownSyncTime) {
    const d = new Date(lastKnownSyncTime * 1000);
    syncTime.textContent = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }
  // Update footer repo pill
  if (repoVersion) {
    const fRepo = document.getElementById('footer-repo-version');
    if (fRepo) { fRepo.textContent = `GitHub Repo v${repoVersion}`; fRepo.title = `Client-sim repo version: v${repoVersion}`; }
  }
}

// ── Setup tab — settings form ─────────────────────────────────────
const branchInput = document.getElementById('branch-input');
const githubTokenInput = document.getElementById('github-token-input');
const githubTokenStatus = document.getElementById('github-token-status');
const syncNowBtn = document.getElementById('sync-now-btn');
const syncNowMsg = document.getElementById('sync-now-message');
const settingsMsg = document.getElementById('settings-message');
const settingsForm = document.getElementById('settings-form');
const hubManagedBanner = document.getElementById('hub-managed-banner');
const checkUpdateBtn = document.getElementById('check-update-btn');
const updateMsg = document.getElementById('update-message');
const versionCurrent = document.getElementById('version-current');
const versionAvailable = document.getElementById('version-available');
const versionLastChecked = document.getElementById('version-last-checked');
const setupActiveBranch = document.getElementById('setup-active-branch');
const repoUrlInput = document.getElementById('repo-url-input');
const centralTabButtons = document.querySelectorAll('#tab-nav .spoke-only .tab[data-tab="central"]');
const configTabButtons = document.querySelectorAll('#tab-nav .spoke-only .tab[data-tab="config"]');
const simTabButtons = document.querySelectorAll('#tab-nav .spoke-only .tab[data-tab="simulations"]');
const spokeSetupTabButtons = document.querySelectorAll('#tab-nav .spoke-only .tab[data-tab="setup"]');
const setupSubtabButtons = document.querySelectorAll('#tab-setup .setup-subnav .setup-subtab');
const setupSubpanels = document.querySelectorAll('#tab-setup .setup-subpanel');
const centralOverview = document.getElementById('central-overview');
const centralSitesGrid = document.getElementById('central-sites-table');
const centralEmpty = document.getElementById('central-empty');
const centralRefreshBtn = document.getElementById('central-refresh-btn');
const centralLastSynced = document.getElementById('central-last-synced');
const centralTokenDot = document.getElementById('central-token-dot');
const centralTokenText = document.getElementById('central-token-text');
const centralSiteDetail = document.getElementById('central-site-detail');
const centralDetailBack = document.getElementById('central-detail-back');
const centralDetailTitle = document.getElementById('central-detail-title');
const centralDetailSub = document.getElementById('central-detail-sub');
const centralSiteClients = document.getElementById('central-site-clients');
const centralSiteChecks = document.getElementById('central-site-checks');
const centralSiteHistory = document.getElementById('central-site-history');
const centralSiteAlerts = document.getElementById('central-site-alerts');
const centralSiteAlertsCount = document.getElementById('central-site-alerts-count');
const centralSiteDevices = document.getElementById('central-site-devices');
const centralSiteDevicesCount = document.getElementById('central-site-devices-count');
const centralClassicUrlInput = document.getElementById('central-classic-url');
const centralClassicUsernameInput = document.getElementById('central-classic-username');
const centralClassicPasswordInput = document.getElementById('central-classic-password');
const centralClassicPasswordStatus = document.getElementById('central-classic-password-status');
const centralCentralUrlInput = document.getElementById('central-central-url');
const centralClientIdInput = document.getElementById('central-client-id');
const centralClientSecretInput = document.getElementById('central-client-secret');
const centralCustomerIdInput = document.getElementById('central-customer-id');
const centralTestBtn = document.getElementById('central-test-btn');
const centralSaveBtn = document.getElementById('central-save-btn');
const centralClearBtn = document.getElementById('central-clear-btn');
const centralConfigMsg = document.getElementById('central-config-msg');
const centralClassicFields = document.getElementById('central-classic-fields');
const centralNewFields = document.getElementById('central-new-fields');
const relayEnabledSelect = document.getElementById('relay-enabled-select');
const relaySpokeName = document.getElementById('relay-spoke-name-input');
const relayServerUrlInput = document.getElementById('relay-server-url-input');
const relayHubTlsVerifyInput = document.getElementById('relay-hub-tls-verify-input');
const relayTenantIdInput = document.getElementById('relay-tenant-id-input');
const relayMsg = document.getElementById('relay-message');
const relayClearConfigBtn = document.getElementById('relay-clear-config-btn');
const adminPasswordInput = document.getElementById('admin-password-input');
const adminPasswordStatus = document.getElementById('admin-password-status');
const adminPasswordSaveBtn = document.getElementById('admin-password-save-btn');
const adminPasswordMsg = document.getElementById('admin-password-message');
const sessionTimeoutMinutesInput = document.getElementById('session-timeout-minutes-input');
const spokeUserPill = document.getElementById('spoke-user-pill');
const spokeUserName = document.getElementById('spoke-user-name');
const spokeUserRole = document.getElementById('spoke-user-role');
const spokeAuthProviderSelect = document.getElementById('spoke-auth-provider-select');
const spokeAuthLdapFields = document.getElementById('spoke-auth-ldap-fields');
const spokeAuthRadiusFields = document.getElementById('spoke-auth-radius-fields');
const spokeAuthTacacsFields = document.getElementById('spoke-auth-tacacs-fields');
const spokeLdapUrlInput = document.getElementById('spoke-ldap-url');
const spokeLdapBindDnInput = document.getElementById('spoke-ldap-bind-dn');
const spokeLdapBindPasswordInput = document.getElementById('spoke-ldap-bind-password');
const spokeLdapUserBaseInput = document.getElementById('spoke-ldap-user-base');
const spokeLdapUserFilterInput = document.getElementById('spoke-ldap-user-filter');
const spokeLdapGroupAdminInput = document.getElementById('spoke-ldap-group-admin');
const spokeLdapGroupViewerInput = document.getElementById('spoke-ldap-group-viewer');
const spokeRadiusHostInput = document.getElementById('spoke-radius-host');
const spokeRadiusPortInput = document.getElementById('spoke-radius-port');
const spokeRadiusSecretInput = document.getElementById('spoke-radius-secret');
const spokeRadiusRoleAttrInput = document.getElementById('spoke-radius-role-attr');
const spokeRadiusAdminValInput = document.getElementById('spoke-radius-admin-val');
const spokeTacacsHostInput = document.getElementById('spoke-tacacs-host');
const spokeTacacsPortInput = document.getElementById('spoke-tacacs-port');
const spokeTacacsSecretInput = document.getElementById('spoke-tacacs-secret');
const spokeTacacsAdminPrivInput = document.getElementById('spoke-tacacs-admin-priv');
const spokeAuthTestBtn = document.getElementById('spoke-auth-test-btn');
const spokeAuthSettingsSaveBtn = document.getElementById('spoke-auth-settings-save-btn');
const spokeAuthSettingsMsg = document.getElementById('spoke-auth-settings-msg');
const topbarUpdateAllBtn = document.getElementById('update-all-btn');
document.querySelectorAll('input[data-secret-field="true"]').forEach(bindSecretInput);

// Notifications + sync interval
const syncIntervalInput  = document.getElementById('sync-interval-input');
const syncIntervalMsg    = document.getElementById('sync-interval-msg');
const emailEnabledToggle = document.getElementById('email-enabled-toggle');
const smtpHost           = document.getElementById('smtp-host');
const smtpPort           = document.getElementById('smtp-port');
const smtpUser           = document.getElementById('smtp-user');
const smtpPassword       = document.getElementById('smtp-password');
const smtpFrom           = document.getElementById('smtp-from');
const smtpTo             = document.getElementById('smtp-to');
const testEmailBtn       = document.getElementById('test-email-btn');
const saveEmailBtn       = document.getElementById('save-email-btn');
const emailNotifMsg      = document.getElementById('email-notif-msg');
const teamsEnabledToggle = document.getElementById('teams-enabled-toggle');
const teamsWebhookUrl    = document.getElementById('teams-webhook-url');
const testTeamsBtn       = document.getElementById('test-teams-btn');
const saveTeamsBtn       = document.getElementById('save-teams-btn');
const teamsNotifMsg      = document.getElementById('teams-notif-msg');
const usbAutoProvisionInput = document.getElementById('usb-auto-provision');
const usbMissingTimeoutInput = document.getElementById('usb-missing-timeout');
const usbMaxSlotsInput = document.getElementById('usb-max-slots');
const vmImage1TemplateIdInput = document.getElementById('vm-image-1-template-id');
const vmImage2TemplateIdInput = document.getElementById('vm-image-2-template-id');
const vmImage1PctInput = document.getElementById('vm-image-1-pct');
const usbVidPidTbody = document.getElementById('usb-vidpid-tbody');
const newVidPidInput = document.getElementById('new-vidpid');
const newVidPidTypeInput = document.getElementById('new-vidpid-type');
const newVidPidLabelInput = document.getElementById('new-vidpid-label');
const usbIgnoredList = document.getElementById('usb-ignored-list');
const ignoredHostnamesList = document.getElementById('ignored-hostnames-list');
const newIgnoredHostnameInput = document.getElementById('new-ignored-hostname');
const addIgnoredHostnameBtn = document.getElementById('add-ignored-hostname-btn');
const vmSilentTimeoutInput = document.getElementById('vm-silent-timeout');
const recloneScheduleEnabledInput = document.getElementById('reclone-schedule-enabled');
const recloneScheduleDayInput = document.getElementById('reclone-schedule-day');
const recloneScheduleTimeInput = document.getElementById('reclone-schedule-time');
const recloneConcurrencyInput = document.getElementById('reclone-concurrency');
const l1VlanStartInput = document.getElementById('l1-vlan-start');
const l1VlanEndInput = document.getElementById('l1-vlan-end');
const l1VlanMsg = document.getElementById('l1-vlan-message');
const usbSettingsMsg = document.getElementById('usb-settings-message');
const vmMaintenanceMsg = document.getElementById('vm-maintenance-message');
const addVidPidBtn = document.getElementById('add-vidpid-btn');
const usbSummaryPanel = document.getElementById('usb-summary-panel');
const usbSummaryTbody = document.getElementById('usb-summary-tbody');
const unknownUsbSection = document.getElementById('unknown-usb-section');
const unknownUsbTbody = document.getElementById('unknown-usb-tbody');
const recloneStatusBadge = document.getElementById('reclone-status-badge');
const recloneProgressWrap = document.getElementById('reclone-progress-wrap');
const recloneProgressBar = document.getElementById('reclone-progress-bar');
const recloneProgressLabel = document.getElementById('reclone-progress-label');
const recloneVmLog = document.getElementById('reclone-vm-log');
const recloneLastRun = document.getElementById('reclone-last-run');
const recloneNowBtn = document.getElementById('reclone-now-btn');


// Event delegation for unknown USB action buttons — attached once to the static tbody element
if (unknownUsbTbody) {
  unknownUsbTbody.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const vidpid = btn.dataset.vidpid;
    const name = btn.dataset.name;
    if (action === 'certify') addUnknownToCertified(vidpid, name);
    else if (action === 'ignore') ignoreUsbDevice(vidpid);
  });
}

function defaultCentralApiSettings() {
  return {
    mode: 'classic',
    classic: { url: '', username: '', password_configured: false },
    central: { url: '', client_id: '', customer_id: '', client_secret_configured: false }
  };
}

function defaultCentralConfigSettings() {
  return {
    api_version: 'classic',
    cluster_url: '',
    access_token_configured: false,
    refresh_token_configured: false,
    client_id: '',
    client_secret_configured: false,
    customer_id: ''
  };
}

function normalizeCentralApiSettings(source = {}, fallback = null) {
  const defaults = defaultCentralApiSettings();
  const fallbackConfig = fallback || defaults;
  const raw = source.central_api || {};
  const legacy = source.central_config || {};
  const rawClassic = raw.classic || {};
  const rawCentral = raw.central || {};
  const legacyIsCentral = legacy.api_version === 'new_central';
  const mode = raw.mode || fallbackConfig.mode || (legacyIsCentral ? 'central' : 'classic');
  return {
    mode: mode === 'central' ? 'central' : 'classic',
    classic: {
      url: rawClassic.url ?? fallbackConfig.classic?.url ?? defaults.classic.url,
      username: rawClassic.username ?? fallbackConfig.classic?.username ?? defaults.classic.username,
      password_configured: rawClassic.password_configured ?? fallbackConfig.classic?.password_configured ?? defaults.classic.password_configured,
    },
    central: {
      url: rawCentral.url ?? (legacyIsCentral ? (legacy.cluster_url || '') : (fallbackConfig.central?.url ?? defaults.central.url)),
      client_id: rawCentral.client_id ?? (legacyIsCentral ? (legacy.client_id || '') : (fallbackConfig.central?.client_id ?? defaults.central.client_id)),
      customer_id: rawCentral.customer_id ?? (legacyIsCentral ? (legacy.customer_id || '') : (fallbackConfig.central?.customer_id ?? defaults.central.customer_id)),
      client_secret_configured: rawCentral.client_secret_configured ?? (legacyIsCentral ? Boolean(legacy.client_secret_configured) : (fallbackConfig.central?.client_secret_configured ?? defaults.central.client_secret_configured)),
    }
  };
}

function getCentralApiMode() {
  const active = document.querySelector('#central-api-mode-toggle button.active');
  return active ? active.dataset.mode : 'classic';
}

function applyCentralModeUI(mode) {
  document.querySelectorAll('#central-api-mode-toggle button').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  const isCentral = mode === 'central';
  if (centralClassicFields) centralClassicFields.classList.toggle('hidden', isCentral);
  if (centralNewFields) centralNewFields.classList.toggle('hidden', !isCentral);
}

document.querySelectorAll('#central-api-mode-toggle button').forEach((btn) => {
  btn.addEventListener('click', () => applyCentralModeUI(btn.dataset.mode));
});
const siteMappingsBody = document.getElementById('site-mappings-body');
const addMappingBtn = document.getElementById('add-mapping-btn');
const centralMappingsMsg = document.getElementById('central-mappings-msg');
const loadSitesBtn = document.getElementById('load-sites-btn');
const sitesLoadStatus = document.getElementById('sites-load-status');
const selectedChecksPreview = document.getElementById('selected-checks-preview');
const loadChecksBtn = document.getElementById('central-load-checks-btn');
const availableChecksContainer = document.getElementById('available-checks-container');
const centralChecksMsg = document.getElementById('central-checks-msg');
const hwLoadAlertsBtn = document.getElementById('hw-load-alerts-btn');
const hwChecksContainer = document.getElementById('hw-checks-container');
const hwChecksMsg = document.getElementById('hw-checks-msg');
const hwChecksPreview = document.getElementById('hw-checks-preview');
const configSimulationForm = document.getElementById('config-simulation-form');
const configAddressesForm  = document.getElementById('config-addresses-form');
const configSimulationMsg = document.getElementById('config-simulation-message');
const configBucketsContainer = document.getElementById('config-buckets-container');
const configBucketsMsg = document.getElementById('config-buckets-message');

function mergeSettings(next = {}) {
  const mergedCentralApi = normalizeCentralApiSettings(next, currentSettings.central_api);
  const mergedCentralConfig = {
    ...defaultCentralConfigSettings(),
    ...(currentSettings.central_config || {}),
    ...(next.central_config || {}),
    access_token_configured: next.central_config?.access_token_configured ?? currentSettings.central_config?.access_token_configured ?? false,
    refresh_token_configured: next.central_config?.refresh_token_configured ?? currentSettings.central_config?.refresh_token_configured ?? false,
    client_secret_configured: next.central_config?.client_secret_configured ?? currentSettings.central_config?.client_secret_configured ?? false,
  };
  const merged = {
    repo_url: next.repo_url ?? currentSettings.repo_url ?? repoUrlInput?.value ?? '',
    repo_branch: next.repo_branch ?? currentSettings.repo_branch ?? '',
    github_token_configured: next.github_token_configured ?? currentSettings.github_token_configured ?? false,
    hub_managed: next.hub_managed ?? currentSettings.hub_managed ?? false,
    central_api: mergedCentralApi,
    central_config: mergedCentralConfig,
    site_mappings: next.site_mappings ?? currentSettings.site_mappings ?? {},
    monitored_checks: Array.isArray(next.monitored_checks)
      ? next.monitored_checks
      : (currentSettings.monitored_checks || []),
    hardware_checks: Array.isArray(next.hardware_checks)
      ? next.hardware_checks
      : (currentSettings.hardware_checks || []),
    relay_enabled: next.relay_enabled ?? currentSettings.relay_enabled ?? 'off',
    relay_server_url: next.relay_server_url ?? currentSettings.relay_server_url ?? '',
    hub_tls_verify: next.hub_tls_verify ?? currentSettings.hub_tls_verify ?? 'off',
    relay_spoke_name: next.relay_spoke_name ?? currentSettings.relay_spoke_name ?? '',
    relay_tenant_hint: next.relay_tenant_hint ?? next.relay_tenant_id ?? currentSettings.relay_tenant_hint ?? currentSettings.relay_tenant_id ?? '',
    relay_tenant_id: next.relay_tenant_id ?? next.relay_tenant_hint ?? currentSettings.relay_tenant_id ?? currentSettings.relay_tenant_hint ?? '',
    relay_spoke_id: next.relay_spoke_id ?? currentSettings.relay_spoke_id ?? '',
    relay_poll_interval: next.relay_poll_interval ?? currentSettings.relay_poll_interval ?? 60,
    relay_api_key_configured: next.relay_api_key_configured ?? currentSettings.relay_api_key_configured ?? false,
    admin_password_configured: next.admin_password_configured ?? currentSettings.admin_password_configured ?? false,
    session_timeout_minutes: next.session_timeout_minutes ?? currentSettings.session_timeout_minutes ?? 30,
    auth_provider: next.auth_provider ?? currentSettings.auth_provider ?? 'local',
    auth_ldap_url: next.auth_ldap_url ?? currentSettings.auth_ldap_url ?? '',
    auth_ldap_bind_dn: next.auth_ldap_bind_dn ?? currentSettings.auth_ldap_bind_dn ?? '',
    auth_ldap_bind_password_configured: next.auth_ldap_bind_password_configured ?? currentSettings.auth_ldap_bind_password_configured ?? false,
    auth_ldap_user_base: next.auth_ldap_user_base ?? currentSettings.auth_ldap_user_base ?? '',
    auth_ldap_user_filter: next.auth_ldap_user_filter ?? currentSettings.auth_ldap_user_filter ?? '(&(objectClass=user)(sAMAccountName={username}))',
    auth_ldap_group_admin: next.auth_ldap_group_admin ?? currentSettings.auth_ldap_group_admin ?? '',
    auth_ldap_group_viewer: next.auth_ldap_group_viewer ?? currentSettings.auth_ldap_group_viewer ?? '',
    auth_radius_host: next.auth_radius_host ?? currentSettings.auth_radius_host ?? '',
    auth_radius_port: next.auth_radius_port ?? currentSettings.auth_radius_port ?? 1812,
    auth_radius_secret_configured: next.auth_radius_secret_configured ?? currentSettings.auth_radius_secret_configured ?? false,
    auth_radius_role_attr: next.auth_radius_role_attr ?? currentSettings.auth_radius_role_attr ?? 'Filter-Id',
    auth_radius_admin_val: next.auth_radius_admin_val ?? currentSettings.auth_radius_admin_val ?? 'admin',
    auth_tacacs_host: next.auth_tacacs_host ?? currentSettings.auth_tacacs_host ?? '',
    auth_tacacs_port: next.auth_tacacs_port ?? currentSettings.auth_tacacs_port ?? 49,
    auth_tacacs_secret_configured: next.auth_tacacs_secret_configured ?? currentSettings.auth_tacacs_secret_configured ?? false,
    auth_tacacs_admin_priv: next.auth_tacacs_admin_priv ?? currentSettings.auth_tacacs_admin_priv ?? 15,
    notifications: {
      email_enabled: next.notifications?.email_enabled ?? currentSettings.notifications?.email_enabled ?? false,
      smtp_host: next.notifications?.smtp_host ?? currentSettings.notifications?.smtp_host ?? '',
      smtp_port: next.notifications?.smtp_port ?? currentSettings.notifications?.smtp_port ?? 587,
      smtp_user: next.notifications?.smtp_user ?? currentSettings.notifications?.smtp_user ?? '',
      smtp_from: next.notifications?.smtp_from ?? currentSettings.notifications?.smtp_from ?? '',
      smtp_to: next.notifications?.smtp_to ?? currentSettings.notifications?.smtp_to ?? [],
      smtp_password_configured: next.notifications?.smtp_password_configured ?? currentSettings.notifications?.smtp_password_configured ?? false,
      teams_enabled: next.notifications?.teams_enabled ?? currentSettings.notifications?.teams_enabled ?? false,
      teams_webhook_url_configured: next.notifications?.teams_webhook_url_configured ?? currentSettings.notifications?.teams_webhook_url_configured ?? false,
    },
    repo_sync_interval: next.repo_sync_interval ?? currentSettings.repo_sync_interval ?? 300,
    usb_vidpids: next.usb_vidpids ?? currentSettings.usb_vidpids ?? '[]',
    usb_missing_timeout: next.usb_missing_timeout ?? currentSettings.usb_missing_timeout ?? '60',
    usb_max_slots: next.usb_max_slots ?? currentSettings.usb_max_slots ?? '24',
    vm_image_1_template_id: next.vm_image_1_template_id ?? currentSettings.vm_image_1_template_id ?? '100',
    vm_image_2_template_id: next.vm_image_2_template_id ?? currentSettings.vm_image_2_template_id ?? '200',
    vm_image_1_pct: next.vm_image_1_pct ?? currentSettings.vm_image_1_pct ?? '50',
    usb_auto_provision: next.usb_auto_provision ?? currentSettings.usb_auto_provision ?? 'off',
    usb_ignored_vidpids: next.usb_ignored_vidpids ?? currentSettings.usb_ignored_vidpids ?? '[]',
    ignored_hostnames: next.ignored_hostnames ?? currentSettings.ignored_hostnames ?? '["sim-rpi-0000"]',
    vm_silent_timeout: next.vm_silent_timeout ?? currentSettings.vm_silent_timeout ?? '24',
    reclone_schedule_enabled: next.reclone_schedule_enabled ?? currentSettings.reclone_schedule_enabled ?? 'off',
    reclone_schedule_cron: next.reclone_schedule_cron ?? currentSettings.reclone_schedule_cron ?? 'sunday 02:00',
    reclone_concurrency: next.reclone_concurrency ?? currentSettings.reclone_concurrency ?? '1',
    l1_vlan_start: next.l1_vlan_start ?? currentSettings.l1_vlan_start ?? '100',
    l1_vlan_end: next.l1_vlan_end ?? currentSettings.l1_vlan_end ?? '199'
  };
  currentSettings = merged;
  return merged;
}

function isCentralApiConfigured(settings = currentSettings, tokenState = null) {
  const centralApi = settings.central_api || defaultCentralApiSettings();
  const centralConfig = settings.central_config || defaultCentralConfigSettings();
  const mode = centralApi.mode === 'central' || centralConfig.api_version === 'new_central' ? 'central' : 'classic';

  if (mode === 'central') {
    const url = String(centralApi.central?.url ?? centralConfig.cluster_url ?? '').trim();
    const clientId = String(centralApi.central?.client_id ?? centralConfig.client_id ?? '').trim();
    return Boolean(url && clientId && (centralApi.central?.client_secret_configured || centralConfig.client_secret_configured));
  }

  const url = String(centralApi.classic?.url ?? centralConfig.cluster_url ?? '').trim();
  const hasCredential = Boolean(
    centralApi.classic?.password_configured
    || centralConfig.access_token_configured
    || centralConfig.refresh_token_configured
    || (tokenState && tokenState.state && tokenState.state !== 'not_configured')
  );
  return Boolean(url && hasCredential);
}

function updateCentralApiVisibility(tokenState = null) {
  const indicator = document.getElementById('central-api-status');
  if (!indicator) return;
  indicator.classList.toggle('hidden', !isCentralApiConfigured(currentSettings, tokenState));
}

function updateRelayIndicatorVisibility(settings = currentSettings) {
  const indicator = document.getElementById('relay-indicator');
  if (!indicator) return;
  indicator.classList.toggle('hidden', !String(settings.relay_server_url || '').trim());
}

function showHubManagedBanner() {
  if (!hubManagedBanner || WEBUI_MODE !== 'spoke') return;
  hubManagedBanner.classList.remove('d-none');
}

function hideHubManagedBanner() {
  if (!hubManagedBanner) return;
  hubManagedBanner.classList.add('d-none');
}

function lockSettingsInputs() {
  if (!settingsForm || WEBUI_MODE !== 'spoke') return;
  settingsForm.querySelectorAll('input, select, textarea').forEach((el) => {
    if (el.dataset.relay === 'true') return;
    el.disabled = true;
    el.classList.add('hub-locked');
  });
}

function unlockSettingsInputs() {
  if (!settingsForm) return;
  settingsForm.querySelectorAll('input, select, textarea').forEach((el) => {
    el.disabled = false;
    el.classList.remove('hub-locked');
  });
}

function setInputValueIfIdle(input, value) {
  if (input && !input.matches(':focus')) input.value = value || '';
}

function showInlineMessage(element, text, isError, timeout = 5000) {
  if (!element) return;
  clearTimeout(element._timer);
  if (!text) {
    element.textContent = '';
    element.className = 'settings-message hidden';
    return;
  }
  element.textContent = text;
  element.className = `settings-message ${isError ? 'error' : 'success'}`;
  if (timeout > 0) {
    element._timer = setTimeout(() => {
      element.className = 'settings-message hidden';
    }, timeout);
  }
}

function normalizeSpokeAuthProvider(provider) {
  return ['local', 'ldap', 'radius', 'tacacs'].includes(provider) ? provider : 'local';
}

function updateSpokeLoginProviderUi(provider = window.__SPOKE_AUTH_PROVIDER__ || currentSettings.auth_provider || 'local') {
  const nextProvider = normalizeSpokeAuthProvider(String(provider || 'local').trim().toLowerCase());
  const usernameGroup = document.getElementById('spoke-login-username-group');
  const usernameInput = document.getElementById('spoke-login-username');
  const subtitle = document.querySelector('#spoke-login-overlay .hub-login-subtitle');
  // Always show username field — local auth uses "admin" as the fixed username
  // but external providers (ldap/radius/tacacs) let users type their own
  const needsUsername = true;
  usernameGroup?.classList.toggle('hidden', false);
  if (usernameInput) {
    usernameInput.required = true;
    if (nextProvider === 'local') usernameInput.value = usernameInput.value || 'admin';
  }
  if (subtitle) {
    subtitle.textContent = 'Enter your username and password to continue.';
  }
}

function updateSpokeAuthProviderVisibility(provider = currentSettings.auth_provider || 'local') {
  const nextProvider = normalizeSpokeAuthProvider(String(provider || 'local').trim().toLowerCase());
  if (spokeAuthProviderSelect && !spokeAuthProviderSelect.matches(':focus')) {
    spokeAuthProviderSelect.value = nextProvider;
  }
  spokeAuthLdapFields?.classList.toggle('hidden', nextProvider !== 'ldap');
  spokeAuthRadiusFields?.classList.toggle('hidden', nextProvider !== 'radius');
  spokeAuthTacacsFields?.classList.toggle('hidden', nextProvider !== 'tacacs');
  updateSpokeLoginProviderUi(nextProvider);
}

function updateSpokeUserUi() {
  if (!spokeUserPill) return;
  const username = spokeCurrentUser?.username || '';
  const role = spokeCurrentUser?.role || '';
  const shouldShow = WEBUI_MODE === 'spoke' && Boolean(window.__SPOKE_AUTH_REQUIRED__ && username);
  spokeUserPill.classList.toggle('hidden', !shouldShow);
  if (spokeUserName) spokeUserName.textContent = username || '—';
  if (spokeUserRole) {
    spokeUserRole.textContent = role || '';
    spokeUserRole.classList.toggle('hidden', !role);
  }
}

function applySpokeViewerMode() {
  const isViewer = WEBUI_MODE === 'spoke' && spokeCurrentUser?.role === 'viewer';
  spokeSetupTabButtons.forEach((button) => button.classList.toggle('hidden', isViewer));
  if (isViewer && activeSpokeTab === 'setup') {
    simTabButtons[0]?.click();
  }
  topbarUpdateAllBtn?.classList.toggle('hidden', isViewer);
  document.getElementById('reclone-now-btn')?.classList.toggle('hidden', isViewer);
  document.getElementById('autoprov-reset-btn')?.classList.toggle('hidden', isViewer);
  document.getElementById('vm-bulk-bar')?.classList.toggle('hidden', isViewer || activeVmCat === 'templates');
  document.querySelectorAll('.vm-action-btn').forEach((button) => {
    button.classList.toggle('hidden', isViewer);
    if (isViewer) button.disabled = true;
  });
  document.querySelectorAll('.vm-check, #server-select-all, [id^="server-th-check-"]').forEach((input) => {
    if (input instanceof HTMLInputElement && isViewer) {
      input.checked = false;
      input.disabled = true;
    }
  });
}

function applySpokeAuthSettingsToUI(settings = currentSettings) {
  if (WEBUI_MODE !== 'spoke') return;
  const provider = normalizeSpokeAuthProvider(String(settings.auth_provider || 'local').trim().toLowerCase());
  window.__SPOKE_AUTH_PROVIDER__ = provider;
  setInputValueIfIdle(spokeLdapUrlInput, settings.auth_ldap_url || '');
  setInputValueIfIdle(spokeLdapBindDnInput, settings.auth_ldap_bind_dn || '');
  if (sessionTimeoutMinutesInput && !sessionTimeoutMinutesInput.matches(':focus')) {
    sessionTimeoutMinutesInput.value = settings.session_timeout_minutes ?? 30;
  }
  setSecretInputConfigured(spokeLdapBindPasswordInput, settings.auth_ldap_bind_password_configured);
  setInputValueIfIdle(spokeLdapUserBaseInput, settings.auth_ldap_user_base || '');
  setInputValueIfIdle(spokeLdapUserFilterInput, settings.auth_ldap_user_filter || '(&(objectClass=user)(sAMAccountName={username}))');
  setInputValueIfIdle(spokeLdapGroupAdminInput, settings.auth_ldap_group_admin || '');
  setInputValueIfIdle(spokeLdapGroupViewerInput, settings.auth_ldap_group_viewer || '');
  setInputValueIfIdle(spokeRadiusHostInput, settings.auth_radius_host || '');
  if (spokeRadiusPortInput && !spokeRadiusPortInput.matches(':focus')) spokeRadiusPortInput.value = settings.auth_radius_port ?? 1812;
  setSecretInputConfigured(spokeRadiusSecretInput, settings.auth_radius_secret_configured);
  setInputValueIfIdle(spokeRadiusRoleAttrInput, settings.auth_radius_role_attr || 'Filter-Id');
  setInputValueIfIdle(spokeRadiusAdminValInput, settings.auth_radius_admin_val || 'admin');
  setInputValueIfIdle(spokeTacacsHostInput, settings.auth_tacacs_host || '');
  if (spokeTacacsPortInput && !spokeTacacsPortInput.matches(':focus')) spokeTacacsPortInput.value = settings.auth_tacacs_port ?? 49;
  setSecretInputConfigured(spokeTacacsSecretInput, settings.auth_tacacs_secret_configured);
  if (spokeTacacsAdminPrivInput && !spokeTacacsAdminPrivInput.matches(':focus')) spokeTacacsAdminPrivInput.value = settings.auth_tacacs_admin_priv ?? 15;
  updateSpokeAuthProviderVisibility(provider);
}

async function loadSpokeAuthSettings(settingsData = null) {
  if (WEBUI_MODE !== 'spoke') return settingsData || null;
  const settings = settingsData || await requestJson('/api/settings');
  applySpokeAuthSettingsToUI(mergeSettings(settings || {}));
  return settings;
}

async function saveSpokeAuthSettings() {
  const provider = normalizeSpokeAuthProvider(String(spokeAuthProviderSelect?.value || currentSettings.auth_provider || 'local').trim().toLowerCase());
  const sessionTimeoutMinutes = Number.parseInt(sessionTimeoutMinutesInput?.value || currentSettings.session_timeout_minutes || 30, 10);
  if (!Number.isFinite(sessionTimeoutMinutes) || sessionTimeoutMinutes < 5 || sessionTimeoutMinutes > 1440) {
    const error = new Error('Session timeout must be between 5 and 1440 minutes.');
    error.handled = true;
    showInlineMessage(spokeAuthSettingsMsg, error.message, true, 7000);
    throw error;
  }
  const payload = {
    auth_provider: provider,
    session_timeout_minutes: sessionTimeoutMinutes,
    auth_ldap_url: spokeLdapUrlInput?.value?.trim() || '',
    auth_ldap_bind_dn: spokeLdapBindDnInput?.value?.trim() || '',
    auth_ldap_user_base: spokeLdapUserBaseInput?.value?.trim() || '',
    auth_ldap_user_filter: spokeLdapUserFilterInput?.value?.trim() || '(&(objectClass=user)(sAMAccountName={username}))',
    auth_ldap_group_admin: spokeLdapGroupAdminInput?.value?.trim() || '',
    auth_ldap_group_viewer: spokeLdapGroupViewerInput?.value?.trim() || '',
    auth_radius_host: spokeRadiusHostInput?.value?.trim() || '',
    auth_radius_port: Number.parseInt(spokeRadiusPortInput?.value || currentSettings.auth_radius_port || 1812, 10) || 1812,
    auth_radius_role_attr: spokeRadiusRoleAttrInput?.value?.trim() || 'Filter-Id',
    auth_radius_admin_val: spokeRadiusAdminValInput?.value?.trim() || 'admin',
    auth_tacacs_host: spokeTacacsHostInput?.value?.trim() || '',
    auth_tacacs_port: Number.parseInt(spokeTacacsPortInput?.value || currentSettings.auth_tacacs_port || 49, 10) || 49,
    auth_tacacs_admin_priv: Number.parseInt(spokeTacacsAdminPrivInput?.value || currentSettings.auth_tacacs_admin_priv || 15, 10) || 15,
  };
  const ldapSecret = getSecretInputPayload(spokeLdapBindPasswordInput);
  if (ldapSecret.include) payload.auth_ldap_bind_password = ldapSecret.value;
  const radiusSecret = getSecretInputPayload(spokeRadiusSecretInput);
  if (radiusSecret.include) payload.auth_radius_secret = radiusSecret.value;
  const tacacsSecret = getSecretInputPayload(spokeTacacsSecretInput);
  if (tacacsSecret.include) payload.auth_tacacs_secret = tacacsSecret.value;

  const response = await requestJson('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  applySettingsToUI(response.settings || payload);
  resetSecretInput(spokeLdapBindPasswordInput);
  resetSecretInput(spokeRadiusSecretInput);
  resetSecretInput(spokeTacacsSecretInput);
  showInlineMessage(spokeAuthSettingsMsg, 'Authentication settings saved.', false, 5000);
  return response;
}

async function testSpokeAuthConnection() {
  const provider = normalizeSpokeAuthProvider(String(spokeAuthProviderSelect?.value || currentSettings.auth_provider || 'local').trim().toLowerCase());
  const response = await requestJson('/api/auth/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider }),
  });
  showInlineMessage(spokeAuthSettingsMsg, response.detail || (response.ok ? 'Connection OK.' : 'Connection failed.'), !response.ok, 7000);
  return response;
}

async function refreshSpokeAuthState() {
  if (WEBUI_MODE !== 'spoke' || !window.__SPOKE_AUTH_REQUIRED__) {
    spokeCurrentUser = null;
    updateSpokeUserUi();
    applySpokeViewerMode();
    return null;
  }
  const response = await fetch('/api/auth/check', { headers: { Accept: 'application/json' } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  if (data.authenticated) {
    spokeCurrentUser = {
      username: data.username || 'admin',
      role: data.role || 'admin',
      auth_provider: data.auth_provider || normalizeSpokeAuthProvider(window.__SPOKE_AUTH_PROVIDER__ || 'local'),
    };
  } else {
    spokeCurrentUser = null;
  }
  updateSpokeUserUi();
  applySpokeViewerMode();
  return data;
}

function showNotification(message, level = 'info') {
  let notice = document.getElementById('app-notification');
  if (!notice) {
    notice = document.createElement('div');
    notice.id = 'app-notification';
    document.body.appendChild(notice);
  }
  clearTimeout(notice._timer);
  notice.textContent = message;
  notice.className = `app-notification settings-message ${level === 'error' ? 'error' : 'success'}`;
  notice._timer = setTimeout(() => {
    notice.className = 'app-notification settings-message hidden';
  }, 4000);
}

function formatRelativeTime(ts) {
  if (!ts) return '—';
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// Format a byte value into the most readable unit (MB / GB / TB)
function fmtSize(bytes) {
  const b = Number(bytes) || 0;
  if (b >= 1024 ** 4) return (b / 1024 ** 4).toFixed(1) + ' TB';
  if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(1) + ' GB';
  if (b >= 1024 ** 2) return (b / 1024 ** 2).toFixed(0) + ' MB';
  return b + ' B';
}

// Format a KB value into the most readable unit
function fmtSizeKB(kb) { return fmtSize(Number(kb) * 1024); }

function normalizeCommandAction(action) {
  return String(action || '').trim().replace(/-/g, '_');
}

function sendProxmoxCommand(action, vmidOrArgs, extraArgs = {}) {
  let args = {};
  if (typeof vmidOrArgs === 'object' && vmidOrArgs !== null) {
    args = { ...vmidOrArgs };
  } else if (vmidOrArgs != null && vmidOrArgs !== '') {
    args = { vmid: parseInt(vmidOrArgs, 10), ...extraArgs };
  } else {
    args = { ...extraArgs };
  }
  return requestJson('/api/commands', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: 'proxmox', action: normalizeCommandAction(action), args }),
  });
}

function describeProxmoxGuest(entry = {}) {
  const vmid = entry.vmid ?? '—';
  const vmType = String(entry.vmType || entry.vm_type || 'qemu').toLowerCase();
  const kind = vmType === 'lxc' ? 'container' : 'VM';
  const name = String(entry.name || '').trim();
  return name ? `${kind} ${name} (${vmid})` : `${kind} ${vmid}`;
}

function confirmVmDelete(entries) {
  const list = entries.slice(0, 8).map((entry) => `• ${describeProxmoxGuest(entry)}`).join('\n');
  const remainder = entries.length > 8 ? `\n• …and ${entries.length - 8} more` : '';
  const lead = entries.length === 1
    ? `Delete ${describeProxmoxGuest(entries[0])}?`
    : `Delete ${entries.length} selected guests?`;
  return confirm(`${lead}\n\n${list}${remainder}\n\nThis permanently destroys the guest in Proxmox and cannot be undone.`);
}

function deleteProxmoxVm(vmid) {
  return requestJson(`/api/proxmox/vms/${encodeURIComponent(vmid)}`, { method: 'DELETE' });
}

/**
 * Handle the "Reclone" action for a single VM.
 * If auto-provisioning is enabled, just delete — auto-prov will redeploy automatically,
 * avoiding a race where both reclone and auto-prov try to deploy the same client.
 * If auto-provisioning is disabled, queue the full delete+reclone.
 */
async function handleRecloneAction(vmid, entry = {}, extraArgs = {}) {
  if (currentSettings.usb_auto_provision === 'on') {
    await deleteProxmoxVm(vmid);
    showNotification(`Deleted ${describeProxmoxGuest(entry)} — auto-provisioning will redeploy`, 'info');
    scheduleProxmoxRefresh();
  } else {
    await sendProxmoxCommand('reclone_vm', vmid, extraArgs);
    showNotification(`Reclone queued for ${describeProxmoxGuest(entry)}`, 'info');
  }
}

function scheduleProxmoxRefresh(delayMs = 4000) {
  window.setTimeout(() => {
    requestJson('/api/proxmox/status').then(renderServerTab).catch(() => {});
  }, delayMs);
}

function normalizeProxmoxHostname(hostname) {
  return String(hostname || '').trim().replace(/\.+$/, '').toLowerCase();
}

function proxmoxHostnameMatches(left, right) {
  const a = normalizeProxmoxHostname(left);
  const b = normalizeProxmoxHostname(right);
  if (!a || !b) return false;
  return a === b || a.split('.', 1)[0] === b.split('.', 1)[0];
}

function syncAgentUpdateButtonState(data = latestProxmoxData) {
  const btn = document.getElementById('agent-update-btn');
  if (!btn || btn.dataset.busy === 'true') return;
  const host = String(data?.node?.hostname || '').trim();
  const approved = Array.isArray(data?.approved_proxmox) ? data.approved_proxmox : [];
  const hostReady = Boolean(host) && approved.some((entry) => proxmoxHostnameMatches(entry?.hostname, host));
  btn.disabled = !hostReady;
  btn.title = hostReady
    ? 'Reinstall the Proxmox host agent from GitHub and restart it'
    : 'Approve and connect the Proxmox host before updating the agent';
}

function syncGithubGatedButtons() {
  const hasToken = Boolean(currentSettings.github_token_configured);
  // Update All button
  const updateAllBtn = document.getElementById('update-all-btn');
  if (updateAllBtn && updateAllBtn.dataset.busy !== 'true') {
    updateAllBtn.disabled = !hasToken;
    updateAllBtn.title = hasToken
      ? 'Update all Proxmox agents then the WebUI server'
      : 'GitHub token required — configure it in the GitHub settings tab';
  }
  // Repo Sync button (hub command to spoke)
  const repoSyncBtn = document.querySelector('[onclick*="repo_sync"]');
  if (repoSyncBtn) {
    repoSyncBtn.disabled = !hasToken;
    repoSyncBtn.title = hasToken
      ? 'Trigger a repo sync on this spoke'
      : 'GitHub token required — configure it in the GitHub settings tab';
  }
}

async function triggerAgentUpdate() {
  const btn = document.getElementById('agent-update-btn');
  if (btn) {
    btn.disabled = true;
    btn.dataset.busy = 'true';
    btn.textContent = '⏳ Updating…';
  }
  try {
    const result = await requestJson('/api/proxmox/update-agent', { method: 'POST' });
    if (btn) { btn.textContent = '✓ Queued'; }
    showToast(`Queued agent update for ${result.target || 'Proxmox host'} (${result.branch || 'current branch'})`, 'info');
  } catch (e) {
    showToast('Failed to queue agent update: ' + e.message, 'error');
    if (btn) {
      btn.textContent = '⬆ Update Agent';
      delete btn.dataset.busy;
    }
    syncAgentUpdateButtonState();
    return;
  }
  setTimeout(() => {
    if (btn) {
      btn.textContent = '⬆ Update Agent';
      delete btn.dataset.busy;
    }
    syncAgentUpdateButtonState();
  }, 5000);
}

function handleUpdateAllProgress(state) {
  const btn = document.getElementById('update-all-btn');
  if (!btn) return;
  if (!state.running && state.phase === 'idle') {
    btn.textContent = '⬆ Update All';
    btn.disabled = false;
    return;
  }
  if (state.phase === 'agents') {
    btn.textContent = `⏳ Agents ${state.completed_agents}/${state.total_agents}…`;
    btn.disabled = true;
  } else if (state.phase === 'webui') {
    btn.textContent = '⏳ Updating Server…';
    btn.disabled = true;
  } else if (state.phase === 'done') {
    btn.textContent = '✓ Done';
    btn.disabled = true;
    setTimeout(() => {
      btn.textContent = '⬆ Update All';
      btn.disabled = false;
    }, 5000);
  } else if (state.phase === 'failed') {
    showToast('Update All failed: ' + (state.error || 'unknown error'), 'error');
    btn.textContent = '⬆ Update All';
    btn.disabled = false;
  }
}

async function triggerUpdateAll() {
  const btn = document.getElementById('update-all-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ Starting…';
  }
  try {
    const res = await fetch('/api/update-all', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  } catch (e) {
    showToast('Failed to start Update All: ' + e.message, 'error');
    if (btn) {
      btn.textContent = '⬆ Update All';
      btn.disabled = false;
    }
  }
}

function renderServerTab(data) {
  latestProxmoxData = data || latestProxmoxData;
  if (data?.reclone_state) latestRecloneState = data.reclone_state;
  if (clients.size) refreshClientWatchdogBadges();
  renderProxmoxApproveState(
    Array.isArray(latestProxmoxData.pending_proxmox) ? latestProxmoxData.pending_proxmox : [],
    Array.isArray(latestProxmoxData.approved_proxmox) ? latestProxmoxData.approved_proxmox : []
  );

  // Update footer agent version pill
  const agentVer = latestProxmoxData.agent_version;
  const fAgent = document.getElementById('footer-agent-version');
  if (fAgent) {
    if (agentVer) {
      fAgent.textContent = `PXMX Agent v${agentVer}`;
      fAgent.title = `Proxmox agent version: v${agentVer}`;
      fAgent.style.display = '';
    } else {
      fAgent.style.display = 'none';
    }
  }

  const tabBtn = document.getElementById('tab-server-btn');
  const tabPanel = document.getElementById('tab-server');
  if (tabBtn) tabBtn.style.display = '';
  if (tabPanel) tabPanel.style.display = '';

  const updateBtn = document.getElementById('agent-update-btn');
  if (updateBtn && !updateBtn._bound) {
    updateBtn.addEventListener('click', triggerAgentUpdate);
    updateBtn._bound = true;
  }
  syncAgentUpdateButtonState(latestProxmoxData);
  syncSpokeClientTypeFilter();

  const node = latestProxmoxData.node || {};
  const setEl = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  setEl('server-node-name', node.hostname || 'Proxmox');
  updateCmdTargetDropdown();
  if (Array.isArray(window._lastCommands)) renderCommandTable(window._lastCommands);
  setEl('server-cpu', node.cpu_percent != null && !Number.isNaN(Number(node.cpu_percent)) ? Number(node.cpu_percent).toFixed(1) : '—');
  const ramUsed  = node.mem_used_kb  ? fmtSizeKB(node.mem_used_kb)  : '—';
  const ramTotal = node.mem_total_kb ? fmtSizeKB(node.mem_total_kb) : '—';
  setEl('server-ram', `${ramUsed} / ${ramTotal}`);
  // Cache last_seen to localStorage so we can show accurate "X ago" after restarts
  const PROXMOX_LS_KEY = 'proxmox_last_seen';
  if (latestProxmoxData.last_seen) {
    try { localStorage.setItem(PROXMOX_LS_KEY, String(latestProxmoxData.last_seen)); } catch (_) {}
  }
  const displayLastSeen = latestProxmoxData.last_seen
    || (() => { try { return parseFloat(localStorage.getItem(PROXMOX_LS_KEY) || ''); } catch (_) { return null; } })();
  setEl('server-last-seen', formatRelativeTime(displayLastSeen || null));

  const storagePills = document.getElementById('server-storage-pills');
  if (storagePills && Array.isArray(node.storage)) {
    const networkTypes = new Set(['nfs', 'cifs', 'glusterfs', 'cephfs', 'rbd', 'iscsi', 'pbs']);
    storagePills.innerHTML = node.storage.map((s) => {
      const icon = networkTypes.has(s.type) ? '🌐' : '🗄️';
      const storageName = escHtml(s.name || '—');
      const storageType = escHtml(s.type || 'dir');
      return `<span class="server-stat-pill" title="${storageName} (${storageType})">${icon} ${storageName}: ${fmtSizeKB(s.used)} / ${fmtSizeKB(s.total)}</span>`;
    }).join('');
  }

  renderUsbSummary(latestProxmoxData);
  renderRecloneStatus(latestRecloneState || latestProxmoxData.reclone_state || {});
  renderAutoProvisionStatus();
  renderVhDevices(latestProxmoxData);

  const vms = Array.isArray(latestProxmoxData.vms) ? latestProxmoxData.vms : [];
  const autoRecoveryPending = new Set(
    Array.isArray(latestProxmoxData.auto_recovery_pending) ? latestProxmoxData.auto_recovery_pending : []
  );

  const configuredTemplateIds = new Set([
    String(currentSettings.vm_image_1_template_id || '100'),
    String(currentSettings.vm_image_2_template_id || '200'),
  ]);

  // Categorise VMs: templates → sim clients (vmid > 90000, qemu) → containers (lxc) → other clients
  const templateVms = vms.filter((v) =>
    v.is_template === true || v.is_template === 'true' ||
    configuredTemplateIds.has(String(v.vmid))
  );
  const nonTemplateVms = vms.filter((v) => !templateVms.includes(v));
  const containerVms = nonTemplateVms.filter((v) => v.type === 'lxc');
  const qemuVms      = nonTemplateVms.filter((v) => v.type !== 'lxc');
  const simVms       = qemuVms.filter((v) => Number(v.vmid) > 90000);
  const otherVms     = qemuVms.filter((v) => !simVms.includes(v));

  // Update count badges
  const countSim = document.getElementById('vm-count-sim');
  const countOther = document.getElementById('vm-count-other');
  const countContainers = document.getElementById('vm-count-containers');
  const countTpl = document.getElementById('vm-count-tpl');
  if (countSim)        countSim.textContent        = simVms.length;
  if (countOther)      countOther.textContent      = otherVms.length;
  if (countContainers) countContainers.textContent = containerVms.length;
  if (countTpl)        countTpl.textContent        = templateVms.length;

  // Render templates (read-only)
  const templateTbody = document.getElementById('server-template-tbody');
  const emptyTpl = document.getElementById('server-empty-tpl');
  if (templateTbody) {
    templateTbody.innerHTML = templateVms.map((vm) => {
      const isProvisioning = vm.prov_status === 'provisioning' || vm.pending_checkin === true;
      const statusDot = isProvisioning ? '🔵' : vm.status === 'running' ? '🟢' : vm.status === 'paused' ? '🟡' : '⚫';
      const statusText = isProvisioning ? 'provisioning' : (vm.status || 'unknown');
      const memUsed  = vm.mem    ? fmtSize(Number(vm.mem)    * 1024 * 1024) : '—';
      const memTotal = vm.maxmem ? fmtSize(Number(vm.maxmem) * 1024 * 1024) : '—';
      const cpu = vm.cpu != null && !Number.isNaN(Number(vm.cpu)) ? Number(vm.cpu).toFixed(1) + '%' : '—';
      return `<tr class="vm-row-template">
        <td>${vm.vmid}</td>
        <td>${escHtml(vm.name || '—')}</td>
        <td>${cpu}</td>
        <td>${memUsed} / ${memTotal}</td>
        <td>${statusDot} ${statusText}</td>
      </tr>`;
    }).join('');
    if (emptyTpl) emptyTpl.style.display = templateVms.length ? 'none' : '';
  }

  // Helper: render one category of regular VMs into a tbody
  const VM_ACTIONS = [
    { action: 'start_vm',    label: '▶',  title: 'Start'    },
    { action: 'stop_vm',     label: '■',  title: 'Stop'     },
    { action: 'reboot_vm',   label: '↺',  title: 'Reboot'   },
    { action: 'snapshot_vm', label: '📷', title: 'Snapshot' },
    { action: 'reclone_vm',  label: '⎘',  title: currentSettings.usb_auto_provision === 'on' ? 'Reclone (delete only — auto-prov will redeploy)' : 'Reclone'  },
    { action: 'delete_vm',   label: '✕',  title: 'Delete'   },
  ];

  const recloningVmids = new Set();
  if (latestRecloneState?.status === 'running') {
    if (latestRecloneState.current_vm != null) recloningVmids.add(Number(latestRecloneState.current_vm));
    (latestRecloneState.log || []).forEach((e) => {
      if (e.status === 'queued' || e.status === 'in_progress') recloningVmids.add(Number(e.vmid));
    });
  }

  function _renderVmGroup(catKey, vmList) {
    const tbody  = document.getElementById(`server-vm-tbody-${catKey}`);
    const empty  = document.getElementById(`server-empty-${catKey}`);
    const thChk  = document.getElementById(`server-th-check-${catKey}`);
    if (!tbody) return;

    // Sort: stopped/paused first, then by VMID ascending
    const sorted = [...vmList].sort((a, b) => {
      const aRunning = a.status === 'running' ? 1 : 0;
      const bRunning = b.status === 'running' ? 1 : 0;
      if (aRunning !== bRunning) return aRunning - bRunning;
      return Number(a.vmid) - Number(b.vmid);
    });

    tbody.innerHTML = '';
    if (thChk) { thChk.disabled = sorted.length === 0; thChk.checked = false; }
    if (empty) empty.style.display = sorted.length ? 'none' : '';
    if (!sorted.length) return;

    sorted.forEach((vm) => {
      const isRecloning  = recloningVmids.has(Number(vm.vmid));
      const isDeleting   = vm.status === 'deleting';
      const isWebui      = webuiVmid != null && Number(vm.vmid) === webuiVmid;
      const isProvisioning = vm.prov_status === 'provisioning' || vm.pending_checkin === true;
      const baseStatusText = isDeleting
        ? '🔴 deleting…'
        : isProvisioning
        ? '🔵 provisioning'
        : `${vm.status === 'running' ? '🟢' : vm.status === 'paused' ? '🟡' : '⚫'} ${vm.status || 'unknown'}`;
      const statusLabel  = isRecloning ? '🟡 recloning…' : baseStatusText;
      const memUsed  = vm.mem    ? fmtSize(Number(vm.mem)    * 1024 * 1024) : '—';
      const memTotal = vm.maxmem ? fmtSize(Number(vm.maxmem) * 1024 * 1024) : '—';
      // Show CPU only for running VMs — stopped VMs always report 0 which is misleading
      const cpuVal = (vm.status === 'running') && vm.cpu != null && !Number.isNaN(Number(vm.cpu))
        ? Number(vm.cpu).toFixed(1) + '%' : '—';
      const recoveryBadge = autoRecoveryPending.has(Number(vm.vmid))
        ? ' <span class="badge badge-yellow" title="Auto-recovery reclone queued">↺ auto-recovery</span>'
        : '';
      const webuiBadge = isWebui
        ? ' <span class="badge badge-grey" title="This is the container running the dashboard — cannot be deleted">🔒 webui</span>'
        : '';

      const recloneSupported = vm.reclone_supported !== false && !isWebui && !isDeleting;
      const recloneReason = isWebui
        ? 'Cannot reclone the container running this service'
        : isDeleting
        ? 'VM is being deleted'
        : (vm.reclone_reason || 'This guest cannot be recloned from the current configuration');
      const actionBtns = VM_ACTIONS.map((a) => {
        if (isDeleting) {
          return `<button class="btn-icon vm-action-btn" data-action="${a.action}" data-vmid="${vm.vmid}" disabled title="VM is being deleted">${a.label}</button>`;
        }
        if (a.action === 'delete_vm' && isWebui) {
          return `<button class="btn-icon vm-action-btn" data-action="${a.action}" data-vmid="${vm.vmid}" disabled title="Cannot delete the container running this service">${a.label}</button>`;
        }
        if (a.action === 'reclone_vm' && !recloneSupported) {
          return `<button class="btn-icon vm-action-btn" data-action="${a.action}" data-vmid="${vm.vmid}" disabled title="${escHtml(recloneReason)}">${a.label}</button>`;
        }
        return `<button class="btn-icon vm-action-btn" data-action="${a.action}" data-vmid="${vm.vmid}" title="${a.title}">${a.label}</button>`;
      }).join(' ');

      const tr = document.createElement('tr');
      tr.dataset.vmid = vm.vmid;
      tr.dataset.status = baseStatusText;
      tr.dataset.vmName = vm.name || '';
      tr.dataset.vmType = vm.type || 'qemu';
      tr.dataset.recloneSupported = recloneSupported ? 'true' : 'false';
      tr.dataset.recloneReason = recloneReason;
      tr.dataset.recloneSourceVmid = vm.reclone_source_vmid != null ? String(vm.reclone_source_vmid) : '';
      tr.innerHTML = `
        <td><input type="checkbox" class="vm-check" data-vmid="${vm.vmid}" data-vm-name="${escHtml(vm.name || '')}" data-vm-type="${vm.type || 'qemu'}" data-reclone-supported="${recloneSupported ? 'true' : 'false'}" data-reclone-reason="${escHtml(recloneReason)}" data-reclone-source-vmid="${vm.reclone_source_vmid != null ? escHtml(String(vm.reclone_source_vmid)) : ''}"${isWebui ? ' disabled' : ''}></td>
        <td class="vm-status-cell">${statusLabel}</td>
        <td>${vm.vmid}</td>
        <td>${escHtml(vm.name || '—')}${recoveryBadge}${webuiBadge}</td>
        <td>${cpuVal}</td>
        <td>${memUsed} / ${memTotal}</td>
        <td>${actionBtns}</td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('.vm-action-btn:not([disabled])').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const row = btn.closest('tr');
        const entry = {
          vmid: btn.dataset.vmid,
          name: row?.dataset.vmName || '',
          vmType: row?.dataset.vmType || 'qemu',
        };
        try {
          if (btn.dataset.action === 'delete_vm') {
            if (catKey !== 'sim' && !confirmVmDelete([entry])) return;
            await deleteProxmoxVm(btn.dataset.vmid);
            showNotification(`Delete queued for ${describeProxmoxGuest(entry)}`, 'info');
            scheduleProxmoxRefresh();
            return;
          }
          if (btn.dataset.action === 'reclone_vm') {
            await handleRecloneAction(btn.dataset.vmid, entry, {
              type: row?.dataset.vmType || 'qemu',
              source_vmid: row?.dataset.recloneSourceVmid ? parseInt(row.dataset.recloneSourceVmid, 10) : undefined,
            });
            return;
          }
          await sendProxmoxCommand(btn.dataset.action, btn.dataset.vmid, {
            type: row?.dataset.vmType || 'qemu',
            source_vmid: row?.dataset.recloneSourceVmid ? parseInt(row.dataset.recloneSourceVmid, 10) : undefined,
          });
          showNotification(`${btn.title} command sent for ${describeProxmoxGuest(entry)}`, 'info');
        } catch (err) {
          showNotification(`Error: ${err.message}`, 'error');
        }
      });
    });

    // Per-category th-check handler
    if (thChk && !thChk._vmBound) {
      thChk._vmBound = true;
      thChk.addEventListener('change', (e) => {
        tbody.querySelectorAll('.vm-check:not([disabled])').forEach((cb) => { cb.checked = e.target.checked; });
        const sa = document.getElementById('server-select-all');
        if (sa) sa.checked = e.target.checked;
      });
    }
  }

  // Snapshot checked VM IDs before rebuild so WS-driven re-renders don't clear selections
  const _checkedVmids = new Set(
    [...document.querySelectorAll('.vm-check:checked')].map((cb) => cb.dataset.vmid)
  );
  const _selectAllWasChecked = document.getElementById('server-select-all')?.checked ?? false;

  _renderVmGroup('sim', simVms);
  _renderVmGroup('other', otherVms);
  _renderVmGroup('containers', containerVms);

  // Restore checked state preserved from before the rebuild
  if (_checkedVmids.size) {
    ['sim', 'other', 'containers'].forEach((cat) => {
      const tbody = document.getElementById(`server-vm-tbody-${cat}`);
      if (!tbody) return;
      let allChecked = true;
      const boxes = [...tbody.querySelectorAll('.vm-check:not([disabled])')];
      boxes.forEach((cb) => {
        if (_checkedVmids.has(cb.dataset.vmid)) {
          cb.checked = true;
        } else {
          allChecked = false;
        }
      });
      // Sync per-category th-check
      const thChk = document.getElementById(`server-th-check-${cat}`);
      if (thChk && boxes.length) thChk.checked = allChecked && boxes.some((b) => b.checked);
    });

    // Restore global select-all checkbox
    const selectAll = document.getElementById('server-select-all');
    if (selectAll) {
      const allBoxes = [...document.querySelectorAll('.vm-check:not([disabled])')];
      selectAll.checked = allBoxes.length > 0 && allBoxes.every((cb) => cb.checked);
    }
  } else {
    // Nothing was checked — reset to clean state
    const selectAll = document.getElementById('server-select-all');
    if (selectAll) selectAll.checked = false;
  }
  applySpokeViewerMode();
}

function renderProxmoxApproveState(pending, approved) {
  const btn = document.getElementById('agent-approve-btn');
  const extraCard = document.getElementById('proxmox-extra-pending');
  const extraList = document.getElementById('proxmox-extra-pending-list');
  const extraCount = document.getElementById('proxmox-extra-pending-count');
  if (!btn) return;

  const currentHostname = (document.getElementById('server-node-name') || {}).textContent || '';

  // Determine if current tile host is pending or approved
  const isPending = pending.some((a) => proxmoxHostnameMatches(a.hostname, currentHostname));
  const isApproved = approved.some((a) => proxmoxHostnameMatches(a.hostname, currentHostname));

  // Other pending agents (not the one shown in the tile)
  const otherPending = pending.filter((a) => !proxmoxHostnameMatches(a.hostname, currentHostname));

  btn._approveHostname = null;

  if (isPending) {
    const match = pending.find((a) => proxmoxHostnameMatches(a.hostname, currentHostname));
    btn.textContent = '✓ Approve Agent';
    btn.style.display = '';
    btn._approveHostname = match?.hostname || currentHostname;
    btn._action = 'approve';
  } else if (isApproved) {
    const match = approved.find((a) => proxmoxHostnameMatches(a.hostname, currentHostname));
    btn.textContent = '✕ Revoke Agent';
    btn.style.display = '';
    btn._approveHostname = match?.hostname || currentHostname;
    btn._action = 'revoke';
  } else if (pending.length > 0) {
    // No connected agent yet — show Approve for the first pending
    const first = pending[0];
    btn.textContent = `✓ Approve ${first.hostname}`;
    btn.style.display = '';
    btn._approveHostname = first.hostname;
    btn._action = 'approve';
    otherPending.shift(); // already showing first one inline
  } else {
    btn.style.display = 'none';
  }

  if (!btn._bound) {
    btn.addEventListener('click', async () => {
      if (!btn._approveHostname) return;
      if (btn._action === 'approve') {
        await approveProxmoxAgent(btn._approveHostname);
      } else {
        await revokeProxmoxAgent(btn._approveHostname);
      }
    });
    btn._bound = true;
  }

  // Show strip for any other pending agents
  if (extraCount) extraCount.textContent = String(pending.length);
  if (extraCard && extraList) {
    if (otherPending.length) {
      extraCard.classList.remove('hidden');
      extraList.innerHTML = otherPending.map((a) => {
        const enc = encodeURIComponent(String(a.hostname || ''));
        return `<strong>${escHtml(a.hostname)}</strong> `
          + `<button class="btn btn-primary" style="font-size:11px;padding:2px 8px;" onclick="approveProxmoxAgent(decodeURIComponent('${enc}'))">Approve</button> `;
      }).join(' &nbsp; ');
    } else {
      extraCard.classList.toggle('hidden', pending.length === 0);
      extraList.innerHTML = pending.length ? '<span class="muted">Waiting for the connected host to be approved.</span>' : '';
    }
  }
}

function renderProxmoxPending(pending) {
  latestProxmoxData.pending_proxmox = pending;
  renderProxmoxApproveState(
    pending,
    Array.isArray(latestProxmoxData.approved_proxmox) ? latestProxmoxData.approved_proxmox : []
  );
}

function renderProxmoxApproved(approved) {
  latestProxmoxData.approved_proxmox = approved;
  renderProxmoxApproveState(
    Array.isArray(latestProxmoxData.pending_proxmox) ? latestProxmoxData.pending_proxmox : [],
    approved
  );
}

async function approveProxmoxAgent(hostname) {
  await requestJson(`/api/proxmox/approve/${encodeURIComponent(hostname)}`, { method: 'POST' });
}

async function rejectProxmoxAgent(hostname) {
  await requestJson(`/api/proxmox/reject/${encodeURIComponent(hostname)}`, { method: 'POST' });
}

async function revokeProxmoxAgent(hostname) {
  if (!confirm(`Revoke key for ${hostname}?`)) return;
  await requestJson(`/api/proxmox/approved/${encodeURIComponent(hostname)}`, { method: 'DELETE' });
  loadProxmoxApproved().catch(() => {});
}

async function loadProxmoxApproved() {
  const approved = await requestJson('/api/proxmox/approved');
  renderProxmoxApproved(Array.isArray(approved) ? approved : []);
}

function applySettingsToUI(s) {
  const settings = mergeSettings(s);
  if (WEBUI_MODE === 'spoke') {
    if (settings.hub_managed) {
      showHubManagedBanner();
      lockSettingsInputs();
    } else {
      hideHubManagedBanner();
      unlockSettingsInputs();
    }
  }
  // Local kill switch is driven by /api/init local_kill_switch (from simulation.conf),
  // NOT from WebUI settings — the settings object never contains kill_switch.
  if (repoUrlInput) repoUrlInput.value = settings.repo_url || repoUrlInput.value;
  if (branchInput && !branchInput.matches(':focus')) branchInput.value = settings.repo_branch || '';
  if (setupActiveBranch) setupActiveBranch.textContent = settings.repo_branch || '—';
  setSecretInputConfigured(githubTokenInput, settings.github_token_configured);
  if (githubTokenStatus) githubTokenStatus.textContent = settings.github_token_configured ? '✓ Token configured' : 'Not configured';
  syncGithubGatedButtons();
  const centralApi = settings.central_api || defaultCentralApiSettings();
  setInputValueIfIdle(centralClassicUrlInput, centralApi.classic.url || '');
  setInputValueIfIdle(centralClassicUsernameInput, centralApi.classic.username || '');
  setInputValueIfIdle(centralCentralUrlInput, centralApi.central.url || '');
  setInputValueIfIdle(centralClientIdInput, centralApi.central.client_id || '');
  setInputValueIfIdle(centralCustomerIdInput, centralApi.central.customer_id || '');
  applyCentralModeUI(centralApi.mode || 'classic');

  const csStatus = document.getElementById('central-client-secret-status');
  setSecretInputConfigured(centralClassicPasswordInput, centralApi.classic.password_configured);
  setSecretInputConfigured(centralClientSecretInput, centralApi.central.client_secret_configured);
  if (centralClassicPasswordStatus) centralClassicPasswordStatus.textContent = centralApi.classic.password_configured ? '✓ Password configured' : '';
  if (csStatus) csStatus.textContent = centralApi.central.client_secret_configured ? '✓ Secret configured' : '';
  if (relayEnabledSelect && !relayEnabledSelect.matches(':focus')) relayEnabledSelect.value = settings.relay_enabled || 'off';
  setInputValueIfIdle(relayServerUrlInput, settings.relay_server_url || '');
  if (relayHubTlsVerifyInput) relayHubTlsVerifyInput.checked = settings.hub_tls_verify === 'on';
  setInputValueIfIdle(relaySpokeName, settings.relay_spoke_name || '');
  setInputValueIfIdle(relayTenantIdInput, settings.relay_tenant_id || settings.relay_tenant_hint || '');
  const spokeIdDisplay = document.getElementById('relay-spoke-id-display');
  if (spokeIdDisplay) spokeIdDisplay.textContent = settings.relay_spoke_id || '—';
  const apikeyStatus = document.getElementById('relay-apikey-status');
  if (apikeyStatus) apikeyStatus.textContent = settings.relay_api_key_configured ? '✓ Received' : 'Pending approval';
  if (adminPasswordInput && !adminPasswordInput.matches(':focus')) {
    adminPasswordInput.value = '';
  }
  if (adminPasswordInput) {
    adminPasswordInput.dataset.configured = settings.admin_password_configured ? 'true' : 'false';
    adminPasswordInput.placeholder = 'Leave blank to disable';
  }
  if (adminPasswordStatus) {
    adminPasswordStatus.textContent = settings.admin_password_configured
      ? 'Password configured. Set a new password to rotate it, or leave blank and click Save to disable login.'
      : 'Set a password to require login when accessing this dashboard. Leave blank to allow open access.';
  }
  applySpokeAuthSettingsToUI(settings);
  updateCentralApiVisibility();
  updateRelayIndicatorVisibility(settings);
  if (usbAutoProvisionInput) usbAutoProvisionInput.checked = settings.usb_auto_provision === 'on';
  if (usbMissingTimeoutInput && !usbMissingTimeoutInput.matches(':focus')) usbMissingTimeoutInput.value = settings.usb_missing_timeout ?? '60';
  if (usbMaxSlotsInput && !usbMaxSlotsInput.matches(':focus')) usbMaxSlotsInput.value = settings.usb_max_slots ?? '24';
  if (vmImage1TemplateIdInput && !vmImage1TemplateIdInput.matches(':focus')) vmImage1TemplateIdInput.value = settings.vm_image_1_template_id ?? '100';
  if (vmImage2TemplateIdInput && !vmImage2TemplateIdInput.matches(':focus')) vmImage2TemplateIdInput.value = settings.vm_image_2_template_id ?? '200';
  if (vmImage1PctInput && !vmImage1PctInput.matches(':focus')) vmImage1PctInput.value = settings.vm_image_1_pct ?? '50';
  if (vmSilentTimeoutInput && !vmSilentTimeoutInput.matches(':focus')) vmSilentTimeoutInput.value = settings.vm_silent_timeout ?? '24';
  const schedule = parseScheduleCron(settings.reclone_schedule_cron);
  if (recloneScheduleEnabledInput) recloneScheduleEnabledInput.checked = settings.reclone_schedule_enabled === 'on';
  if (recloneConcurrencyInput) recloneConcurrencyInput.value = settings.reclone_concurrency ?? '1';
  if (l1VlanStartInput && !l1VlanStartInput.matches(':focus')) l1VlanStartInput.value = settings.l1_vlan_start ?? '100';
  if (l1VlanEndInput && !l1VlanEndInput.matches(':focus')) l1VlanEndInput.value = settings.l1_vlan_end ?? '199';
  if (recloneScheduleDayInput && !recloneScheduleDayInput.matches(':focus')) recloneScheduleDayInput.value = schedule.day;
  if (recloneScheduleTimeInput && !recloneScheduleTimeInput.matches(':focus')) recloneScheduleTimeInput.value = schedule.time;
  try { renderUsbVidPidTable(); } catch (_) {}
  try { renderIgnoredUsbList(); } catch (_) {}
  try { renderIgnoredHostnamesList(); } catch (_) {}
  try { renderSiteMappingsTable(); } catch (_) {}
  try { renderSelectedChecksPreview(); } catch (_) {}
  try { renderHwChecksPreview(); } catch (_) {}
  if ((availableChecks.alerts.length || availableChecks.insights.length) && availableChecksContainer) {
    try { renderAvailableChecks(); } catch (_) {}
  }
  try { renderCentralOverview(); } catch (_) {}
  try { renderChecksList(); } catch (_) {} // Refresh sim tab whenever settings change (monitored checks may have changed)
  try { renderUsbSummary(latestProxmoxData); } catch (_) {}
  try { renderRecloneStatus(latestRecloneState || latestProxmoxData.reclone_state || {}); } catch (_) {}
  try { renderAutoProvisionStatus(); } catch (_) {}
  if (centralSiteDetailOpen) {
    try { renderSiteClients(centralSiteDetailOpen); } catch (_) {}
    try { renderSiteChecks(centralSiteDetailOpen, centralStatusData[centralSiteDetailOpen] || {}); } catch (_) {}
  }

  // Sync interval
  if (syncIntervalInput && !syncIntervalInput.matches(':focus'))
    syncIntervalInput.value = settings.repo_sync_interval ?? 300;

  // Email notifications
  const notif = settings.notifications || {};
  if (emailEnabledToggle) emailEnabledToggle.checked = !!notif.email_enabled;
  setInputValueIfIdle(smtpHost, notif.smtp_host || '');
  if (smtpPort && !smtpPort.matches(':focus')) smtpPort.value = notif.smtp_port ?? 587;
  setInputValueIfIdle(smtpUser, notif.smtp_user || '');
  setInputValueIfIdle(smtpFrom, notif.smtp_from || '');
  setInputValueIfIdle(smtpTo, Array.isArray(notif.smtp_to) ? notif.smtp_to.join(', ') : (notif.smtp_to || ''));
  setSecretInputConfigured(smtpPassword, notif.smtp_password_configured);

  // Teams
  if (teamsEnabledToggle) teamsEnabledToggle.checked = !!notif.teams_enabled;
  setSecretInputConfigured(teamsWebhookUrl, notif.teams_webhook_url_configured);
}

function showSettingsMessage(text, isError) {
  settingsMsg.textContent = text;
  settingsMsg.className = `settings-message ${isError ? 'error' : 'success'}`;
  clearTimeout(settingsMsg._timer);
  settingsMsg._timer = setTimeout(() => {
    settingsMsg.className = 'settings-message hidden';
  }, 5000);
}

async function clearSettingsProvider(provider, { button, messageEl, successText, extraDataHandler } = {}) {
  const originalText = button?.textContent || 'Clear Config';
  if (button) {
    button.disabled = true;
    button.textContent = 'Clearing…';
  }
  try {
    const data = await requestJson(`/api/settings/clear/${encodeURIComponent(provider)}`, { method: 'POST' });
    applySettingsToUI(data.settings || {});
    if (typeof extraDataHandler === 'function') extraDataHandler(data);
    if (messageEl === settingsMsg) showSettingsMessage(successText, false);
    else showInlineMessage(messageEl, successText, false);
  } catch (error) {
    await loadSettings().catch(() => {});
    if (messageEl === settingsMsg) showSettingsMessage(`Error: ${error.message}`, true);
    else showInlineMessage(messageEl, `Error: ${error.message}`, true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

if (branchInput) {
  branchInput.addEventListener('blur', async () => {
    const branch = branchInput.value.trim();
    if (!branch) return;
    try {
      const data = await requestJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_branch: branch })
      });
      showSettingsMessage(`Branch set to "${data.settings.repo_branch}".`, false);
      applySettingsToUI(data.settings);
    } catch (err) {
      showSettingsMessage(`Error: ${err.message}`, true);
    }
  });
}

if (githubTokenInput) {
  githubTokenInput.addEventListener('blur', async () => {
    const { include, value } = getSecretInputPayload(githubTokenInput);
    if (!include) return;
    const token = value.trim();
    try {
      const response = await requestJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ github_token: token })
      });
      applySettingsToUI(response.settings || { github_token_configured: Boolean(token) });
      resetSecretInput(githubTokenInput);
      if (token) {
        if (githubTokenStatus) githubTokenStatus.textContent = '⏳ Validating token…';
        try {
          const check = await requestJson('/api/test-github');
          if (check.valid) {
            if (githubTokenStatus) githubTokenStatus.textContent = `✓ Token valid — authenticated as ${check.username}`;
            showSettingsMessage('GitHub token saved and validated.', false);
          } else {
            if (githubTokenStatus) githubTokenStatus.textContent = `✗ Token saved but invalid: ${check.error}`;
            showSettingsMessage(`Token saved but validation failed: ${check.error}`, true);
          }
        } catch {
          if (githubTokenStatus) githubTokenStatus.textContent = '✓ Token saved (validation unavailable)';
          showSettingsMessage('GitHub token saved.', false);
        }
      } else {
        showSettingsMessage('GitHub token cleared.', false);
      }
    } catch (err) {
      showSettingsMessage(`Error: ${err.message}`, true);
    }
  });
}

if (adminPasswordSaveBtn) {
  adminPasswordSaveBtn.addEventListener('click', async () => {
    const originalLabel = adminPasswordSaveBtn.textContent;
    const password = adminPasswordInput?.value?.trim() || '';
    adminPasswordSaveBtn.disabled = true;
    adminPasswordSaveBtn.textContent = 'Saving…';
    showInlineMessage(adminPasswordMsg, '', false, 0);
    try {
      const response = await requestJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ admin_password: password })
      });
      applySettingsToUI(response.settings || { admin_password_configured: Boolean(password) });
      if (adminPasswordInput) adminPasswordInput.value = '';
      showInlineMessage(
        adminPasswordMsg,
        password ? 'Dashboard password saved. Existing sessions were signed out.' : 'Dashboard password setting cleared. Existing sessions were signed out.',
        false,
        5000,
      );
    } catch (error) {
      showInlineMessage(adminPasswordMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      adminPasswordSaveBtn.disabled = false;
      adminPasswordSaveBtn.textContent = originalLabel;
    }
  });
}

if (spokeAuthProviderSelect) {
  spokeAuthProviderSelect.addEventListener('change', () => {
    updateSpokeAuthProviderVisibility(spokeAuthProviderSelect.value);
    showInlineMessage(spokeAuthSettingsMsg, '', false, 0);
  });
}

if (spokeAuthSettingsSaveBtn) {
  spokeAuthSettingsSaveBtn.addEventListener('click', async () => {
    const originalLabel = spokeAuthSettingsSaveBtn.textContent;
    spokeAuthSettingsSaveBtn.disabled = true;
    spokeAuthSettingsSaveBtn.textContent = 'Saving…';
    try {
      await saveSpokeAuthSettings();
    } catch (error) {
      if (!error?.handled) showInlineMessage(spokeAuthSettingsMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      spokeAuthSettingsSaveBtn.disabled = false;
      spokeAuthSettingsSaveBtn.textContent = originalLabel;
    }
  });
}

if (spokeAuthTestBtn) {
  spokeAuthTestBtn.addEventListener('click', async () => {
    const originalLabel = spokeAuthTestBtn.textContent;
    spokeAuthTestBtn.disabled = true;
    spokeAuthTestBtn.textContent = 'Testing…';
    try {
      await testSpokeAuthConnection();
    } catch (error) {
      showInlineMessage(spokeAuthSettingsMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      spokeAuthTestBtn.disabled = false;
      spokeAuthTestBtn.textContent = originalLabel;
    }
  });
}

syncNowBtn.addEventListener('click', async () => {
  syncNowBtn.disabled = true;
  syncNowBtn.textContent = '⬇ Syncing…';
  syncNowMsg.textContent = 'GitHub sync started…';
  syncNowMsg.className = 'settings-message success';
  try {
    const res = await fetch('/api/sync-now', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    syncNowMsg.textContent = 'Sync triggered — status will update below when complete.';
  } catch (err) {
    syncNowMsg.textContent = `Error: ${err.message}`;
    syncNowMsg.className = 'settings-message error';
  } finally {
    syncNowBtn.disabled = false;
    syncNowBtn.textContent = '⬇ Sync from GitHub Now';
    clearTimeout(syncNowMsg._timer);
    syncNowMsg._timer = setTimeout(() => {
      syncNowMsg.className = 'settings-message hidden';
    }, 6000);
  }
});

function applyVersionStatus(data) {
  if (versionCurrent) versionCurrent.textContent = data.current_version ?? data.cswebui_current ?? '—';
  if (versionAvailable) versionAvailable.textContent = data.available_version ?? data.cswebui_available ?? '—';
  if (versionLastChecked) versionLastChecked.textContent = data.last_checked ?? '—';

  const inProgress = !!data.update_in_progress;
  updateWasInProgress = inProgress;
  if (checkUpdateBtn) {
    checkUpdateBtn.disabled = inProgress;
    checkUpdateBtn.textContent = inProgress ? '🔄 Updating…' : '🔄 Check & Update Now';
  }

  if (!updateMsg) return;

  const logDetails = document.getElementById('update-log-details');
  const logOutput  = document.getElementById('update-log-output');

  if (data.update_error) {
    updateMsg.textContent = `Update failed: ${data.update_error}`;
    updateMsg.className = 'settings-message error';
    updateMsg.classList.remove('hidden');
    // Show captured install output in the collapsible panel
    if (logDetails && logOutput && data.update_log?.length) {
      logOutput.textContent = data.update_log.join('\n');
      logDetails.classList.remove('hidden');
      logDetails.open = true;
    }
  } else if (inProgress) {
    const lastLine = data.update_log?.length ? ` — ${data.update_log[data.update_log.length - 1]}` : '';
    updateMsg.textContent = `Installing v${data.available_version}… service will restart.${lastLine}`;
    updateMsg.className = 'settings-message success';
    updateMsg.classList.remove('hidden');
    // Keep log panel updated live
    if (logDetails && logOutput && data.update_log?.length) {
      logOutput.textContent = data.update_log.join('\n');
      logDetails.classList.remove('hidden');
      logOutput.scrollTop = logOutput.scrollHeight;
    }
  }
}

if (checkUpdateBtn) {
  checkUpdateBtn.addEventListener('click', async () => {
    checkUpdateBtn.disabled = true;
    checkUpdateBtn.textContent = '🔄 Checking…';
    updateMsg.textContent = 'Checking for updates…';
    updateMsg.className = 'settings-message success';
    updateMsg.classList.remove('hidden');
    clearTimeout(updateMsg._timer);
    try {
      const res = await fetch('/api/self-update', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      updateMsg.textContent = data.message;
      updateMsg.className = data.message.includes('up to date') ? 'settings-message success' : 'settings-message success';
      if (!data.message.includes('started')) {
        checkUpdateBtn.disabled = false;
        checkUpdateBtn.textContent = '🔄 Check & Update Now';
        updateMsg._timer = setTimeout(() => { updateMsg.className = 'settings-message hidden'; }, 8000);
      }
    } catch (err) {
      updateMsg.textContent = `Error: ${err.message}`;
      updateMsg.className = 'settings-message error';
      checkUpdateBtn.disabled = false;
      checkUpdateBtn.textContent = '🔄 Check & Update Now';
      updateMsg._timer = setTimeout(() => { updateMsg.className = 'settings-message hidden'; }, 10000);
    }
  });
}

// Load initial version status on page load
fetch('/api/version').then(r => r.json()).then(applyVersionStatus).catch(() => {});

function normalizeFlagValue(value) {
  return String(value ?? 'off').toLowerCase() === 'on' ? 'on' : 'off';
}

function formatLastSeen(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function proxmoxVmForHostname(hostname) {
  const normalized = String(hostname || '').trim().toLowerCase();
  if (!normalized) return null;
  const vms = Array.isArray(latestProxmoxData?.vms) ? latestProxmoxData.vms : [];
  return vms.find((vm) => String(vm?.name || '').trim().toLowerCase() === normalized) || null;
}

function spokeUsbVmids() {
  return new Set(
    (Array.isArray(latestProxmoxData?.usb_state) ? latestProxmoxData.usb_state : [])
      .map((device) => String(device?.vmid ?? '').trim())
      .filter(Boolean)
  );
}

function clientVmid(client = {}) {
  const directVmid = client?.vmid ?? client?.proxmox_vmid;
  if (directVmid != null && String(directVmid).trim() !== '') return String(directVmid).trim();
  const proxmoxVm = proxmoxVmForHostname(client?.hostname);
  const proxmoxVmid = proxmoxVm?.vmid;
  return proxmoxVmid != null && String(proxmoxVmid).trim() !== '' ? String(proxmoxVmid).trim() : '';
}

function classifyClient(client = {}, usbVmids = spokeUsbVmids()) {
  if (client.has_usb != null) return client.has_usb ? 't2' : 't1';
  const proxmoxVm = proxmoxVmForHostname(client?.hostname);
  if (proxmoxVm?.reclone_bus_path) return 't2';
  return usbVmids.has(clientVmid(client)) ? 't2' : 't1';
}

function syncSpokeClientTypeTabs() {
  document.querySelectorAll('[data-clienttype]').forEach((button) => {
    button.classList.toggle('active', button.dataset.clienttype === clientTypeFilter);
  });
}

function updateSpokeClientTypeCounts(allClients = [...clients.values()]) {
  const usbVmids = spokeUsbVmids();
  const counts = { all: allClients.length, t1: 0, t2: 0 };
  allClients.forEach((client) => {
    counts[classifyClient(client, usbVmids)] += 1;
  });
  const countAll = document.getElementById('client-type-count-all');
  const countT1 = document.getElementById('client-type-count-t1');
  const countT2 = document.getElementById('client-type-count-t2');
  const countT3 = document.getElementById('client-type-count-t3');
  if (countAll) countAll.textContent = String(counts.all);
  if (countT1) countT1.textContent = String(counts.t1);
  if (countT2) countT2.textContent = String(counts.t2);
  if (countT3) countT3.textContent = '—';
  return counts;
}

function syncSpokeClientTypeFilter() {
  syncSpokeClientTypeTabs();
  const allClients = [...clients.values()];
  const usbVmids = spokeUsbVmids();
  const counts = updateSpokeClientTypeCounts(allClients);
  let visibleCount = 0;
  allClients.forEach((client) => {
    const refs = rowRefs.get(client.hostname);
    if (!refs) return;
    const matches = clientTypeFilter === 'all' || classifyClient(client, usbVmids) === clientTypeFilter;
    if (matches) visibleCount += 1;
    refs.mainRow.classList.toggle('hidden', !matches);
    refs.detailRow.classList.toggle('hidden', !matches || openControlHost !== client.hostname);
  });
  updateClientCount(visibleCount, counts.all);
}

function setClientTypeFilter(nextFilter = 'all') {
  clientTypeFilter = nextFilter === 't1' || nextFilter === 't2' ? nextFilter : 'all';
  syncSpokeClientTypeFilter();
}

function clientHasPendingCheckin(hostname) {
  return Boolean(proxmoxVmForHostname(hostname)?.pending_checkin);
}

function renderClientHostname(cell, hostname) {
  cell.textContent = '';
  const label = document.createElement('span');
  label.textContent = hostname || '—';
  cell.appendChild(label);
  if (!hostname || !clientHasPendingCheckin(hostname)) return;
  const badge = document.createElement('span');
  badge.className = 'badge bg-warning text-dark ms-1';
  badge.textContent = 'Awaiting Check-in';
  cell.appendChild(badge);
}

function impactSummary(activeSimulations = []) {
  const labels = [...new Set(activeSimulations.map((sim) => IMPACT_LABELS[sim]).filter(Boolean))];
  return labels.length ? labels.join(' · ') : '— Normal';
}

function renderImpactCell(cell, activeSimulations = []) {
  cell.textContent = '';
  const labels = [...new Set(activeSimulations.map((sim) => IMPACT_LABELS[sim]).filter(Boolean))];
  if (!labels.length) {
    cell.textContent = '— Normal';
    return;
  }
  const dot = document.createElement('span');
  dot.className = 'ind-dot red';
  dot.style.cssText = 'display:inline-block;vertical-align:middle;margin-right:5px;flex-shrink:0;';
  const text = document.createElement('span');
  text.textContent = labels.join(' · ');
  cell.appendChild(dot);
  cell.appendChild(text);
}

function badgeClass(simulation) {
  if (FAILURE_SIMS.has(simulation)) return 'badge badge-failure';
  if (TRAFFIC_SIMS.has(simulation)) return 'badge badge-traffic';
  return 'badge badge-neutral';
}

function setWsStatus(connected, label) {
  wsDot.className = `status-dot ${connected ? 'online' : 'offline'}`;
  wsText.textContent = label;
}

function setCentralApiStatus(valid, tokenState) {
  updateCentralApiVisibility(tokenState);
  const dot = document.getElementById('central-api-dot');
  const text = document.getElementById('central-api-text');
  const indicator = document.getElementById('central-api-status');
  if (!dot || !text) return;

  const state = tokenState?.state;
  const detail = tokenState?.detail || '';

  if (state === 'connected' || valid === true) {
    dot.className = 'status-dot online';
    text.textContent = 'Connected';
    if (indicator) indicator.title = `Central API: connected — ${detail}`;
  } else if (state === 'not_configured') {
    dot.className = 'status-dot offline';
    text.textContent = 'Not Configured';
    if (indicator) indicator.title = `Central API: ${detail}`;
  } else if (state === 'auth_failed') {
    dot.className = 'status-dot offline';
    text.textContent = 'Auth Failed';
    if (indicator) indicator.title = `Central API: ${detail}`;
  } else if (state === 'token_expired') {
    dot.className = 'status-dot warning';
    text.textContent = 'Token Expired';
    if (indicator) indicator.title = `Central API: ${detail}`;
  } else if (valid === false) {
    dot.className = 'status-dot offline';
    text.textContent = 'No Token';
    if (indicator) indicator.title = 'Central API: token missing or invalid — check Setup tab';
  } else {
    dot.className = 'status-dot warning';
    text.textContent = 'Unknown';
    if (indicator) indicator.title = 'Central API: status not yet checked';
  }
}

function updateClientCount(visibleCount = clients.size, totalCount = clients.size) {
  if (clientCount) {
    const visible = Number.isFinite(visibleCount) ? visibleCount : clients.size;
    const total = Number.isFinite(totalCount) ? totalCount : clients.size;
    clientCount.textContent = visible !== total
      ? `${visible} / ${total} clients`
      : `${visible} client${visible === 1 ? '' : 's'}`;
  }
  if (!emptyRow) return;
  const visible = Number.isFinite(visibleCount) ? visibleCount : clients.size;
  const total = Number.isFinite(totalCount) ? totalCount : clients.size;
  emptyRow.style.display = visible > 0 ? 'none' : '';
  const emptyCell = emptyRow.querySelector('td');
  if (emptyCell) {
    emptyCell.textContent = total > 0
      ? 'No clients match the current filter.'
      : 'No clients connected — waiting for beacons…';
  }
}

function createCell(className = '') {
  const cell = document.createElement('td');
  if (className) cell.className = className;
  return cell;
}

function ensureRow(hostname) {
  if (rowRefs.has(hostname)) {
    return rowRefs.get(hostname);
  }

  const mainRow = document.createElement('tr');
  mainRow.dataset.hostname = hostname;
  mainRow.className = 'client-row';

  const detailRow = document.createElement('tr');
  detailRow.className = 'control-row hidden';
  const detailCell = document.createElement('td');
  detailCell.colSpan = 8;
  detailRow.appendChild(detailCell);

  const statusCell = createCell('status-cell');
  const statusDot = document.createElement('span');
  statusDot.className = 'status-dot offline';
  statusCell.appendChild(statusDot);

  const hostnameCell = createCell('hostname-cell');
  const platformCell = createCell();
  const ssidCell = createCell();
  const activeCell = createCell('badge-cell');
  const lastSeenCell = createCell('nowrap-cell');
  const actionsCell = createCell();

  // Error count badge cell — shows a red badge when the client has reported errors.
  // WHY: Operators need to see at a glance which clients are having problems
  // without clicking into each one individually.
  const errorCell = createCell('error-cell');
  const errorBadge = document.createElement('span');
  errorBadge.className = 'error-badge hidden';
  errorBadge.title = 'Click Actions → Control to see error log';
  errorCell.appendChild(errorBadge);

  const controlButton = document.createElement('button');
  controlButton.type = 'button';
  controlButton.className = 'btn btn-small';
  controlButton.textContent = 'Control';
  controlButton.addEventListener('click', () => toggleControlRow(hostname));
  actionsCell.appendChild(controlButton);

  [
    statusCell,
    hostnameCell,
    platformCell,
    ssidCell,
    activeCell,
    lastSeenCell,
    errorCell,
    actionsCell
  ].forEach((cell) => mainRow.appendChild(cell));

  tbody.appendChild(mainRow);
  tbody.appendChild(detailRow);

  const refs = {
    mainRow,
    detailRow,
    detailCell,
    statusDot,
    hostnameCell,
    platformCell,
    ssidCell,
    activeCell,
    lastSeenCell,
    errorCell,
    errorBadge,
    controlButton
  };

  rowRefs.set(hostname, refs);
  return refs;
}

function renderBadges(container, activeSimulations) {
  container.textContent = '';
  const inner = document.createElement('div');
  inner.className = 'badge-cell-inner';
  if (!activeSimulations || !activeSimulations.length) {
    inner.textContent = '—';
    container.appendChild(inner);
    return;
  }
  activeSimulations.forEach((simulation) => {
    const badge = document.createElement('span');
    badge.className = badgeClass(simulation);
    badge.textContent = simulation;
    inner.appendChild(badge);
  });
  container.appendChild(inner);
}

function refreshClientWatchdogBadges() {
  Array.from(clients.values()).forEach((client) => upsertClient(client));
}

function upsertClient(client) {
  const existing = clients.get(client.hostname) || {};
  const merged = {
    ...existing,
    ...client,
    config: client.config || existing.config || {},
    effective_config: client.effective_config || existing.effective_config || client.config || {},
    overrides: client.overrides || existing.overrides || {},
    active_simulations: client.active_simulations || existing.active_simulations || [],
    // Merge recent_errors: always use the server-provided list which is the authoritative
    // circular buffer. If not present in the update, keep the existing list.
    recent_errors: client.recent_errors || existing.recent_errors || [],
    error_count: client.error_count ?? existing.error_count ?? 0
  };

  clients.set(client.hostname, merged);
  const refs = ensureRow(client.hostname);

  refs.statusDot.className = `status-dot ${merged.online ? 'online' : 'offline'}`;
  refs.mainRow.classList.toggle('client-offline', !merged.online);
  renderClientHostname(refs.hostnameCell, merged.hostname);
  refs.platformCell.textContent = merged.platform || '—';
  refs.ssidCell.textContent = merged.connected_ssid || '—';
  renderBadges(refs.activeCell, merged.active_simulations || []);
  refs.lastSeenCell.textContent = formatLastSeen(merged.last_seen);
  refs.controlButton.textContent = openControlHost === merged.hostname ? 'Close' : 'Control';

  // Update error badge — show count if there are any errors, hide if clean.
  // WHY: Red number in the Errors column is the fastest way to spot a problem
  // on a table with many clients without reading every row in detail.
  const errCount = merged.error_count || 0;
  if (errCount > 0) {
    refs.errorBadge.textContent = errCount > 99 ? '99+' : String(errCount);
    refs.errorBadge.className = 'error-badge';
    refs.errorBadge.title = `${errCount} error(s) reported — open Control to see log`;
  } else {
    refs.errorBadge.className = 'error-badge hidden';
  }

  if (openControlHost === merged.hostname) {
    renderControlPanel(merged.hostname);
  }

  syncSpokeClientTypeFilter();
  if (centralSiteDetailOpen) {
    renderSiteClients(centralSiteDetailOpen);
  }
}

function collectPanelState(panel) {
  const state = {};
  FLAG_ORDER.forEach((flag) => {
    const input = panel.querySelector(`input[data-flag="${flag}"]`);
    state[flag] = input && input.checked ? 'on' : 'off';
  });
  return state;
}

async function sendJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(errorText || `Request failed: ${response.status}`);
  }

  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return response.json();
  }
  return null;
}


async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get('content-type') || '';
  let payload = null;
  if (contentType.includes('application/json')) {
    payload = await response.json();
  } else {
    const text = await response.text();
    payload = text ? { detail: text } : null;
  }
  if (!response.ok) {
    throw new Error(payload?.detail || payload?.message || `HTTP ${response.status}`);
  }
  return payload;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\x22/g, '&quot;')
    .replace(/\x27/g, '&#39;');
}

function parseJsonList(value) {
  if (Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(value || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function serializeJsonList(value) {
  return JSON.stringify(Array.isArray(value) ? value : []);
}

function parseScheduleCron(cronValue = 'sunday 02:00') {
  const [day = 'sunday', time = '02:00'] = String(cronValue || '').trim().toLowerCase().split(/\s+/, 2);
  return { day, time: /^\d{2}:\d{2}$/.test(time || '') ? time : '02:00' };
}

function formatUiDate(value) {
  if (!value) return '—';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function renderUsbVidPidTable() {
  if (!usbVidPidTbody) return;
  usbVidPidTbody.innerHTML = '';
  const devices = parseJsonList(currentSettings.usb_vidpids);
  devices.forEach((device) => {
    const tr = document.createElement('tr');
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'btn-icon';
    removeBtn.textContent = '✕';
    removeBtn.addEventListener('click', () => removeVidPid(device.vidpid));
    tr.innerHTML = `<td>${escHtml(device.vidpid || '—')}</td><td>${escHtml(device.type || 'wireless')}</td><td>${escHtml(device.label || '—')}</td>`;
    const actionTd = document.createElement('td');
    actionTd.appendChild(removeBtn);
    tr.appendChild(actionTd);
    usbVidPidTbody.appendChild(tr);
  });
}

function renderIgnoredUsbList() {
  if (!usbIgnoredList) return;
  usbIgnoredList.innerHTML = '';
  const ignored = parseJsonList(currentSettings.usb_ignored_vidpids);
  if (!ignored.length) {
    usbIgnoredList.textContent = 'No ignored devices.';
    return;
  }
  ignored.forEach((vidpid) => {
    const badge = document.createElement('span');
    badge.className = 'badge badge-grey';
    badge.textContent = vidpid;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = ' ✕';
    button.addEventListener('click', async () => {
      // Re-read from currentSettings each click to avoid stale closure
      const current = parseJsonList(currentSettings.usb_ignored_vidpids);
      currentSettings.usb_ignored_vidpids = serializeJsonList(current.filter((item) => item !== vidpid));
      renderIgnoredUsbList();
      renderUsbSummary(latestProxmoxData);
      try {
        await requestJson('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(collectUsbSettingsPayload()),
        });
        showNotification(`${vidpid} removed from ignored devices`, 'success');
      } catch (err) {
        showNotification(`Error saving: ${err.message}`, 'error');
      }
    });
    badge.appendChild(button);
    usbIgnoredList.appendChild(badge);
  });
}

function renderIgnoredHostnamesList() {
  if (!ignoredHostnamesList) return;
  ignoredHostnamesList.innerHTML = '';
  const hostnames = parseJsonList(currentSettings.ignored_hostnames);
  if (!hostnames.length) {
    ignoredHostnamesList.textContent = 'No ignored hostnames.';
    return;
  }
  hostnames.forEach((hostname) => {
    const badge = document.createElement('span');
    badge.className = 'badge badge-grey';
    badge.textContent = hostname;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = ' ✕';
    button.addEventListener('click', async () => {
      const current = parseJsonList(currentSettings.ignored_hostnames);
      currentSettings.ignored_hostnames = serializeJsonList(current.filter((h) => h !== hostname));
      renderIgnoredHostnamesList();
      try {
        await requestJson('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ignored_hostnames: currentSettings.ignored_hostnames }),
        });
        showNotification(`${hostname} removed from ignored clients`, 'success');
      } catch (err) {
        showNotification(`Error saving: ${err.message}`, 'error');
      }
    });
    badge.appendChild(button);
    ignoredHostnamesList.appendChild(badge);
  });
}

async function loadUsbConfig() {
  const data = await requestJson('/api/proxmox/usb-config');
  currentSettings.usb_vidpids = serializeJsonList(data.vidpids || []);
  currentSettings.usb_ignored_vidpids = serializeJsonList(data.ignored_vidpids || []);
  currentSettings.usb_missing_timeout = String(data.missing_timeout ?? currentSettings.usb_missing_timeout ?? '60');
  currentSettings.usb_max_slots = String(data.max_slots ?? currentSettings.usb_max_slots ?? '24');
  currentSettings.vm_image_1_template_id = String(data.image1_template_id ?? currentSettings.vm_image_1_template_id ?? '100');
  currentSettings.vm_image_2_template_id = String(data.image2_template_id ?? currentSettings.vm_image_2_template_id ?? '200');
  currentSettings.vm_image_1_pct = String(data.image1_pct ?? currentSettings.vm_image_1_pct ?? '50');
  currentSettings.usb_auto_provision = data.auto_provision || 'off';
  if (usbAutoProvisionInput) usbAutoProvisionInput.checked = currentSettings.usb_auto_provision === 'on';
  if (usbMissingTimeoutInput && !usbMissingTimeoutInput.matches(':focus')) usbMissingTimeoutInput.value = currentSettings.usb_missing_timeout;
  if (usbMaxSlotsInput && !usbMaxSlotsInput.matches(':focus')) usbMaxSlotsInput.value = currentSettings.usb_max_slots ?? '24';
  if (vmImage1TemplateIdInput && !vmImage1TemplateIdInput.matches(':focus')) vmImage1TemplateIdInput.value = currentSettings.vm_image_1_template_id;
  if (vmImage2TemplateIdInput && !vmImage2TemplateIdInput.matches(':focus')) vmImage2TemplateIdInput.value = currentSettings.vm_image_2_template_id;
  if (vmImage1PctInput && !vmImage1PctInput.matches(':focus')) vmImage1PctInput.value = currentSettings.vm_image_1_pct;
  renderUsbVidPidTable();
  renderIgnoredUsbList();
}

function addVidPid() {
  const vidpid = newVidPidInput?.value.trim().toLowerCase() || '';
  const type = newVidPidTypeInput?.value || 'wireless';
  const label = newVidPidLabelInput?.value.trim() || '';
  if (!/^[0-9a-f]{4}:[0-9a-f]{4}$/i.test(vidpid)) {
    showNotification('Enter VID:PID as ####:####', 'error');
    return;
  }
  const devices = parseJsonList(currentSettings.usb_vidpids).filter((item) => item?.vidpid !== vidpid);
  devices.push({ vidpid, type, label });
  devices.sort((a, b) => String(a.vidpid).localeCompare(String(b.vidpid)));
  currentSettings.usb_vidpids = serializeJsonList(devices);
  renderUsbVidPidTable();
  if (newVidPidInput) newVidPidInput.value = '';
  if (newVidPidLabelInput) newVidPidLabelInput.value = '';
}

function removeVidPid(vidpid) {
  currentSettings.usb_vidpids = serializeJsonList(parseJsonList(currentSettings.usb_vidpids).filter((item) => item?.vidpid !== vidpid));
  renderUsbVidPidTable();
}

function collectUsbSettingsPayload() {
  return {
    usb_vidpids: currentSettings.usb_vidpids,
    usb_missing_timeout: String(usbMissingTimeoutInput?.value || currentSettings.usb_missing_timeout || '60'),
    usb_max_slots: String(usbMaxSlotsInput?.value || currentSettings.usb_max_slots || '24'),
    vm_image_1_template_id: String(vmImage1TemplateIdInput?.value || currentSettings.vm_image_1_template_id || '100'),
    vm_image_2_template_id: String(vmImage2TemplateIdInput?.value || currentSettings.vm_image_2_template_id || '200'),
    vm_image_1_pct: String(vmImage1PctInput?.value ?? currentSettings.vm_image_1_pct ?? '50'),
    usb_auto_provision: usbAutoProvisionInput?.checked ? 'on' : 'off',
    usb_ignored_vidpids: currentSettings.usb_ignored_vidpids,
    vm_silent_timeout: String(vmSilentTimeoutInput?.value || currentSettings.vm_silent_timeout || '24'),
    reclone_schedule_enabled: recloneScheduleEnabledInput?.checked ? 'on' : 'off',
    reclone_schedule_cron: `${recloneScheduleDayInput?.value || 'sunday'} ${recloneScheduleTimeInput?.value || '02:00'}`,
    reclone_concurrency: String(recloneConcurrencyInput?.value ?? '1'),
    l1_vlan_start: String(l1VlanStartInput?.value ?? currentSettings.l1_vlan_start ?? '100'),
    l1_vlan_end: String(l1VlanEndInput?.value ?? currentSettings.l1_vlan_end ?? '199'),
  };
}

async function ignoreUsbDevice(vidpid) {
  const ignored = new Set(parseJsonList(currentSettings.usb_ignored_vidpids));
  ignored.add(String(vidpid || '').toLowerCase());
  currentSettings.usb_ignored_vidpids = serializeJsonList([...ignored].sort());
  // Optimistically remove from local unknown_usb so the device disappears immediately
  if (Array.isArray(latestProxmoxData.unknown_usb)) {
    latestProxmoxData.unknown_usb = latestProxmoxData.unknown_usb.filter(
      (d) => String(d.vidpid || '').toLowerCase() !== String(vidpid || '').toLowerCase()
    );
  }
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectUsbSettingsPayload())
    });
    renderIgnoredUsbList();
    renderUsbSummary(latestProxmoxData);
    showNotification(`${vidpid} added to ignored devices`, 'success');
  } catch (error) {
    showNotification(`Error saving: ${error.message}`, 'error');
  }
}

async function addUnknownToCertified(vidpid, name) {
  if (!vidpid) {
    showNotification('Could not certify: device has no VID:PID', 'error');
    return;
  }
  const type = 'wireless'; // default; user can change in the certified table after
  const devices = parseJsonList(currentSettings.usb_vidpids).filter((item) => item?.vidpid !== vidpid);
  devices.push({ vidpid: vidpid.toLowerCase(), type, label: name || vidpid });
  devices.sort((a, b) => String(a.vidpid).localeCompare(String(b.vidpid)));
  currentSettings.usb_vidpids = serializeJsonList(devices);
  // Optimistically remove from local unknown_usb so the device disappears immediately
  if (Array.isArray(latestProxmoxData.unknown_usb)) {
    latestProxmoxData.unknown_usb = latestProxmoxData.unknown_usb.filter(
      (d) => String(d.vidpid || '').toLowerCase() !== String(vidpid || '').toLowerCase()
    );
  }
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectUsbSettingsPayload())
    });
    renderUsbVidPidTable();
    renderUsbSummary(latestProxmoxData);
    showNotification(`${name || vidpid} added to certified devices`, 'success');
  } catch (error) {
    showNotification(`Error saving: ${error.message}`, 'error');
  }
}

function updateUsbCountdowns() {
  document.querySelectorAll('[data-missing-until]').forEach((node) => {
    const until = Number(node.dataset.missingUntil || 0) * 1000;
    const remaining = Math.max(0, Math.floor((until - Date.now()) / 1000));
    node.textContent = remaining > 0 ? `${Math.ceil(remaining / 60)}m remaining` : 'Ready to destroy';
  });
}

function renderVhDevices(proxmoxData = latestProxmoxData) {
  const pills = document.getElementById('vh-stat-pills');
  const list = document.getElementById('vh-device-list');
  if (!pills || !list) return;

  const vh = proxmoxData?.vh_devices || {};
  const devices = Array.isArray(vh.devices) ? vh.devices : [];
  const svcActive = vh.vh_service_active;
  const connected = vh.vh_connected;
  const autoUseAll = vh.auto_use_all;
  const count = vh.count ?? devices.length;
  const inUse = devices.filter(d => d.auto_use).length;
  const available = devices.filter(d => !d.auto_use).length;

  const svcLabel = svcActive ? '🟢 Service running' : '🔴 Service stopped';
  const autoLabel = autoUseAll ? '⚡ Auto-Use All: ON' : '⚫ Auto-Use All: OFF';
  const countLabel = count > 0
    ? `🔌 ${count} device${count !== 1 ? 's' : ''} — ${inUse} in use, ${available} available`
    : '⚫ No VH devices detected';
  pills.innerHTML = [svcLabel, autoLabel, connected ? countLabel : '⚫ Not connected to VH server']
    .map(l => `<span class="server-stat-pill">${l}</span>`).join('');

  if (!devices.length) {
    list.innerHTML = '<p class="muted" style="padding:8px 0;">No VirtualHere adapters found. Ensure the VH client service is running and connected to a server.</p>';
  } else {
    const byServer = new Map();
    devices.forEach(d => {
      const srv = d.server || 'Unknown Server';
      if (!byServer.has(srv)) byServer.set(srv, []);
      byServer.get(srv).push(d);
    });
    let html = '';
    byServer.forEach((devs, server) => {
      html += `<div style="margin-bottom:8px;">
        <div style="font-size:0.8rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:6px;">Server: ${escHtml(server)}</div>
        <table class="data-table">
          <thead><tr><th>Adapter</th><th>Address</th><th>Vendor</th><th>VID:PID</th><th>Serial</th><th>Status</th></tr></thead>
          <tbody>${devs.map(d => `<tr>
            <td><strong>${escHtml(d.name || 'Unknown')}</strong></td>
            <td><code>${escHtml(d.address || '—')}</code></td>
            <td>${escHtml(d.vendor || '—')}</td>
            <td>${d.vendor_id && d.product_id ? `<code>${escHtml(d.vendor_id)}:${escHtml(d.product_id)}</code>` : '—'}</td>
            <td><code>${escHtml(d.serial || '—')}</code></td>
            <td>${d.auto_use
              ? `<span class="badge badge-green">In Use${d.in_use_by ? ` by ${escHtml(d.in_use_by)}` : ''}</span>`
              : '<span class="badge badge-grey">Available</span>'}</td>
          </tr>`).join('')}</tbody>
        </table></div>`;
    });
    list.innerHTML = html;
  }
}

function renderUsbSummary(proxmoxData = latestProxmoxData) {
  latestProxmoxData = proxmoxData || latestProxmoxData;
  if (!usbSummaryPanel || !usbSummaryTbody || !unknownUsbSection || !unknownUsbTbody) return;

  const certified = parseJsonList(currentSettings.usb_vidpids);
  const usbState = Array.isArray(latestProxmoxData.usb_state) ? latestProxmoxData.usb_state : [];
  const missingTimeoutSeconds = (parseInt(currentSettings.usb_missing_timeout, 10) || 60) * 60;

  // Compute present bus set first so the stat pill count is accurate
  const presentUsb = Array.isArray(latestProxmoxData.present_usb) ? latestProxmoxData.present_usb : [];
  const presentBusSet = new Set(presentUsb.map((item) => String(item?.bus_path || '').trim()).filter(Boolean));

  // VMs whose dongles are missing (removed) — exclude from running count since they
  // are being torn down and no longer represent an active provisioned client
  const missingVmids = new Set(
    usbState
      .filter((item) => item.missing_since && !presentBusSet.has(String(item?.bus_path || '').trim()))
      .map((item) => Number(item.vmid))
      .filter(Boolean)
  );

  // Running VM stats pill — only count provisioned VMs with their dongles still present
  const allVms = Array.isArray(latestProxmoxData.vms) ? latestProxmoxData.vms : [];
  const runningVms = allVms.filter((v) => v.status === 'running' && !v.is_template && !missingVmids.has(Number(v.vmid)));
  const usbStatPills = document.getElementById('usb-vm-stat-pills');
  if (usbStatPills) {
    const simRunning = runningVms.filter((v) => v.name && v.name.startsWith('client-sim-')).length;
    const totalRunning = runningVms.length;
    usbStatPills.innerHTML = `<span class="server-stat-pill" title="Running VMs with dongles present">🟢 ${totalRunning} running VM${totalRunning !== 1 ? 's' : ''}</span>`
      + (simRunning > 0 ? `<span class="server-stat-pill" title="client-sim-* VMs running">${simRunning} sim client${simRunning !== 1 ? 's' : ''}</span>` : '');
  }

  // Client-side filter: remove devices that are now certified or ignored, and skip empty vidpids.
  // This prevents stale server broadcasts from restoring a device the user just acted on.
  const certifiedSet = new Set(certified.map((d) => String(d?.vidpid || '').toLowerCase()).filter(Boolean));
  const ignoredSet = new Set(parseJsonList(currentSettings.usb_ignored_vidpids).map((v) => String(v || '').toLowerCase()).filter(Boolean));
  const unknownUsb = (Array.isArray(latestProxmoxData.unknown_usb) ? latestProxmoxData.unknown_usb : [])
    .filter((d) => {
      const v = String(d.vidpid || '').toLowerCase().trim();
      return v && !certifiedSet.has(v) && !ignoredSet.has(v);
    });

  usbSummaryTbody.innerHTML = '';
  certified.forEach((device) => {
    const entries = usbState.filter((item) => (item.vidpid || '').toLowerCase() === String(device.vidpid || '').toLowerCase());
    const missingEntries = entries.filter((item) => item.missing_since && !presentBusSet.has(String(item?.bus_path || '').trim()));
    const activeEntries = entries.filter((item) => !missingEntries.includes(item));
    const missing = missingEntries.length;
    const total = presentUsb.filter((item) => (item.vidpid || '').toLowerCase() === String(device.vidpid || '').toLowerCase()).length;

    // Build VM name list for active entries
    const vmMap = new Map(allVms.map((v) => [Number(v.vmid), v]));
    const activeVmHtml = activeEntries.length === 0 ? '—' : activeEntries.map((e) => {
      const vm = vmMap.get(Number(e.vmid));
      const name = escHtml(vm?.name || `VM ${e.vmid}`);
      const dot = vm?.status === 'running' ? '🟢' : '⚫';
      return `<div style="white-space:nowrap">${dot} ${name}</div>`;
    }).join('');

    // Look up hardware-detected name from usb_state or present_usb
    const hwName = entries.find(e => e.name)?.name
      || presentUsb.find(p => (p.vidpid || '').toLowerCase() === String(device.vidpid || '').toLowerCase())?.name
      || '';

    const tr = document.createElement('tr');
    const available = Math.max(0, total - activeEntries.length);
    const missingHtml = missing
      ? `<div class="usb-missing-list">${missingEntries.map((item) => {
          const mvm = vmMap.get(Number(item.vmid));
          const mname = escHtml(mvm?.name || `VM ${item.vmid}`);
          return `<div class="usb-missing-item">🔴 ${mname} · <span data-missing-until="${Number(item.missing_since) + missingTimeoutSeconds}"></span></div>`;
        }).join('')}</div>`
      : '—';
    const vidpidHtml = hwName
      ? `${escHtml(device.vidpid || '—')}<div class="muted" style="font-size:0.78rem;margin-top:2px;">${escHtml(hwName)}</div>`
      : escHtml(device.vidpid || '—');
    const usbType = escHtml(device.type || 'wireless');
    tr.innerHTML = `
      <td>${escHtml(device.label || device.vidpid || '—')}</td>
      <td>${vidpidHtml}</td>
      <td class="usb-type-${usbType}">${usbType}</td>
      <td>${activeVmHtml}</td>
      <td>${missingHtml}</td>
      <td>${available > 0 ? `<span class="badge badge-green">${available}</span>` : '<span class="muted">—</span>'}</td>
      <td>${total}</td>
    `;
    usbSummaryTbody.appendChild(tr);
  });

  unknownUsbTbody.innerHTML = unknownUsb.map((device) => {
    const vid = escHtml(device.vidpid || '');
    const nameLabel = escHtml(device.name || device.bus_path || 'Unknown device');
    return `<tr>
      <td>${nameLabel}</td>
      <td>${vid || '—'}</td>
      <td class="usb-actions">
        <button type="button" class="btn btn-primary btn-small" data-action="certify" data-vidpid="${vid}" data-name="${nameLabel}">Add to certified</button>
        <button type="button" class="btn btn-primary btn-small" data-action="ignore" data-vidpid="${vid}">Ignore</button>
      </td>
    </tr>`;
  }).join('');
  // Use event delegation — one listener on the static tbody handles all button clicks
  unknownUsbTbody._delegated = true;

  unknownUsbSection.classList.toggle('hidden', unknownUsb.length === 0);
  // Show the panel whenever Proxmox is connected; hide only before any data has arrived
  usbSummaryPanel.classList.toggle('hidden', !latestProxmoxData.connected && certified.length === 0 && unknownUsb.length === 0 && usbState.length === 0);

  if (usbCountdownTimer) window.clearInterval(usbCountdownTimer);
  updateUsbCountdowns();
  if (usbState.some((item) => item.missing_since && !presentBusSet.has(String(item?.bus_path || '').trim()))) {
    usbCountdownTimer = window.setInterval(updateUsbCountdowns, 1000);
  }
}

async function triggerRecloneAll() {
  if (recloneNowBtn) {
    recloneNowBtn.disabled = true;
    recloneNowBtn.textContent = '⟳ Starting…';
  }
  try {
    const result = await requestJson('/api/proxmox/reclone-all', { method: 'POST' });
    showNotification(`Fleet reclone started for ${result.vm_count} VM(s).`, 'info');
  } catch (error) {
    showNotification(`Reclone error: ${error.message}`, 'error');
  } finally {
    if (recloneNowBtn) {
      recloneNowBtn.disabled = false;
      recloneNowBtn.textContent = '⟳ Reclone All Now';
    }
  }
}
window.triggerRecloneAll = triggerRecloneAll;

async function resetAutoprovStatus() {
  if (!confirm('Clear the auto-provisioning status panel?')) return;
  await fetch('/api/proxmox/autoprov/reset', { method: 'POST' });
}
window.resetAutoprovStatus = resetAutoprovStatus;

function recloneLogStatusMeta(status) {
  switch (String(status || '').toLowerCase()) {
    case 'completed':
      return { label: 'Done', className: 'status-online' };
    case 'failed':
      return { label: 'Failed', className: 'status-offline' };
    case 'queued':
      return { label: 'Queued', className: 'status-pending' };
    case 'in_progress':
      return { label: 'Running', className: 'status-pending' };
    default:
      return { label: 'Pending', className: 'status-pending' };
  }
}

function renderRecloneLogItems(entries = [], emptyHtml = '') {
  if (!entries.length) return emptyHtml;
  return entries.slice().reverse().map((entry) => {
    const meta = recloneLogStatusMeta(entry.status);
    return `
      <div class="autoprov-live-item reclone-live-item" title="${escHtml(entry.message || '')}">
        <div class="autoprov-live-item-main">
          <div class="autoprov-live-item-name">${escHtml(entry.name || `VM ${entry.vmid}`)}</div>
        </div>
        <span class="status-badge ${meta.className}">${meta.label}</span>
      </div>
    `;
  }).join('');
}

function renderRecloneStatus(recloneState = latestRecloneState || {}) {
  latestRecloneState = recloneState || latestRecloneState || {};
  if (!recloneStatusBadge || !recloneProgressWrap || !recloneProgressBar || !recloneProgressLabel || !recloneVmLog || !recloneLastRun) return;

  const state = latestRecloneState || {};
  const status = state.status || 'idle';
  const isCloning = status === 'running' && state.current_vm;
  const badgeClass = status === 'running'
    ? 'badge-blue'
    : status === 'completed'
      ? 'badge-green'
      : status === 'failed'
        ? 'badge-red'
        : 'badge-grey';
  recloneStatusBadge.className = `badge ${badgeClass}`;
  if (isCloning) {
    const phaseMap = { stopping: 'Stopping', cloning: 'Cloning', starting: 'Starting' };
    const phaseLabel = phaseMap[state.phase] || 'Cloning';
    recloneStatusBadge.textContent = `${phaseLabel} VM ${state.current_vm}`;
  } else if (status === 'idle') {
    recloneStatusBadge.textContent = 'Stopped';
  } else {
    recloneStatusBadge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
  }

  const total = Number(state.total || 0);
  const done = Number(state.completed || 0) + Number(state.failed || 0);
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const isActive = status === 'running' || done > 0;
  recloneProgressWrap.classList.toggle('hidden', !isActive);

  // Type label
  const typeEl = document.getElementById('reclone-type-label');
  if (typeEl) {
    const typeMap = { scheduled: 'Scheduled', manual: 'Manual', 'auto-recovery': 'Auto-Recovery' };
    typeEl.textContent = typeMap[state.type] || (state.type || '');
    typeEl.className = `badge ${state.type === 'auto-recovery' ? 'badge-yellow' : state.type === 'scheduled' ? 'badge-blue' : 'badge-grey'}`;
    typeEl.classList.toggle('hidden', !state.type || status === 'idle');
  }

  // Current VM
  const currentVmEl = document.getElementById('reclone-current-vm');
  if (currentVmEl) {
    currentVmEl.textContent = status === 'running' && state.current_vm
      ? `Recloning VM ${state.current_vm}…`
      : '';
  }

  // ETA
  const etaEl = document.getElementById('reclone-eta');
  if (etaEl) {
    let etaText = '';
    if (status === 'running' && done > 0 && total > done && state.started_at) {
      const elapsed = (Date.now() - new Date(state.started_at).getTime()) / 1000;
      const avgSec = elapsed / done;
      const remaining = (total - done) * avgSec;
      etaText = `~${Math.ceil(remaining / 60)} min remaining`;
    }
    etaEl.textContent = etaText;
  }

  recloneProgressBar.style.width = `${pct}%`;
  recloneProgressLabel.textContent = total ? `${done} / ${total} VMs (${pct}%)` : '';

  const logEntries = Array.isArray(state.log) ? state.log : [];
  if (logEntries.length === 0 && status !== 'idle') {
    recloneVmLog.innerHTML = `<div class="muted" style="padding:8px 0;font-size:13px;">No VMs processed yet.</div>`;
  } else {
    recloneVmLog.innerHTML = renderRecloneLogItems(logEntries);
  }

  if (state.last_run) {
    const typeLabel = state.last_run.type ? ` · ${state.last_run.type}` : '';
    recloneLastRun.textContent = `Last run: ${formatUiDate(state.last_run.timestamp)} · ${state.last_run.completed || 0} completed · ${state.last_run.failed || 0} failed${typeLabel}`;
  } else {
    recloneLastRun.textContent = 'Last run: —';
  }

  // Auto-recovery log
  const arSection = document.getElementById('reclone-auto-recovery-section');
  const arLog = document.getElementById('reclone-auto-recovery-log');
  const autoLog = Array.isArray(state.auto_recovery_log) ? state.auto_recovery_log : [];
  if (arSection) arSection.classList.toggle('hidden', autoLog.length === 0);
  if (arLog) {
    arLog.innerHTML = renderRecloneLogItems(autoLog, `<div class="muted" style="padding:8px 0;font-size:13px;">No auto-recovery activity.</div>`);
  }

  updateVmRecloneIcons();
}

// Patch VM status icons in the server tab without a full re-render.
// Called whenever reclone state changes (reclone_update WS message).
function updateVmRecloneIcons() {
  const state = latestRecloneState || {};
  const tbodies = document.querySelectorAll('[id^="server-vm-tbody-"]');
  if (!tbodies.length) return;

  const recloningVmids = new Set();
  if (state.status === 'running') {
    if (state.current_vm != null) recloningVmids.add(Number(state.current_vm));
    (state.log || []).forEach((e) => {
      if (e.status === 'queued' || e.status === 'in_progress') recloningVmids.add(Number(e.vmid));
    });
  }

  tbodies.forEach((tbody) => {
    tbody.querySelectorAll('tr[data-vmid]').forEach((row) => {
      const vmid = Number(row.dataset.vmid);
      const cell = row.querySelector('.vm-status-cell');
      if (!cell) return;
      if (recloningVmids.has(vmid)) {
        cell.textContent = '🟡 recloning…';
      } else if (row.dataset.status) {
        cell.textContent = row.dataset.status;
      }
    });
  });
}

function getAutoProvisionRunState(proxmoxData = latestProxmoxData) {
  const run = proxmoxData?.prov_run;
  if (run && Array.isArray(run.items)) {
    const concurrency = Math.max(1, parseInt(currentSettings.reclone_concurrency, 10) || 1);
    const allItems = run.items
      .filter((item) => item && item.vmid != null)
      .map((item) => ({
        ...item,
        status: String(item.status || 'pending').toLowerCase(),
      }));
    // Cap active (non-terminal) items to the concurrency limit; always keep failed/done
    const terminalItems = allItems.filter(i => ['done', 'failed'].includes(i.status));
    const activeItems = allItems.filter(i => !['done', 'failed'].includes(i.status));
    const items = [...activeItems.slice(0, concurrency), ...terminalItems];
    return {
      running: Boolean(run.running),
      total: Number.isFinite(Number(run.total)) ? Number(run.total) : allItems.length,
      completed: Number.isFinite(Number(run.completed)) ? Number(run.completed) : allItems.filter((item) => item.status === 'done').length,
      failed: Number.isFinite(Number(run.failed)) ? Number(run.failed) : allItems.filter((item) => item.status === 'failed').length,
      startedAt: run.started_at || null,
      updatedAt: run.updated_at || null,
      completedAt: run.completed_at || null,
      items,
    };
  }

  const usbState = Array.isArray(proxmoxData?.usb_state) ? proxmoxData.usb_state : [];
  const vms = Array.isArray(proxmoxData?.vms) ? proxmoxData.vms : [];
  const vmByVmid = new Map(vms.map((vm) => [String(vm?.vmid), vm || {}]));
  // Respect the configured concurrency limit — only show VMs that are actively
  // being worked on (up to reclone_concurrency at a time). Prefer running VMs
  // first (configuring) so they're not displaced by queued clones.
  const concurrency = Math.max(1, parseInt(currentSettings.reclone_concurrency, 10) || 1);
  const allProvisioningEntries = usbState
    .filter((entry) => entry && entry.vmid != null && entry.prov_status === 'provisioning')
    .map((entry) => {
      const vm = vmByVmid.get(String(entry.vmid)) || {};
      return {
        vmid: entry.vmid,
        vm_name: vm.name || null,
        usb_name: entry.name || null,
        bus_path: entry.bus_path || null,
        vidpid: entry.vidpid || null,
        status: vm.status === 'running' ? 'configuring' : 'cloning',
      };
    });
  // Sort: configuring (VM running) first, then cloning — then cap to concurrency
  allProvisioningEntries.sort((a, b) => (a.status === 'configuring' ? -1 : b.status === 'configuring' ? 1 : 0));
  const provisioningItems = allProvisioningEntries.slice(0, concurrency);

  // Also include VMs that finished cloning but haven't checked into the API yet.
  // This keeps the live panel and "provisioning" badge visible through the
  // boot-up gap between clone-complete and first API check-in.
  const provisioningVmids = new Set(provisioningItems.map((i) => String(i.vmid)));
  const pendingCheckinItems = vms
    .filter((vm) => vm && vm.pending_checkin === true && !provisioningVmids.has(String(vm.vmid)))
    .map((vm) => ({
      vmid: vm.vmid,
      vm_name: vm.name || null,
      usb_name: null,
      bus_path: null,
      vidpid: null,
      status: 'pending_checkin',
    }));

  const items = [...provisioningItems, ...pendingCheckinItems];

  return {
    running: items.length > 0,
    total: items.length,
    completed: 0,
    failed: 0,
    startedAt: null,
    updatedAt: null,
    completedAt: null,
    items,
  };
}

function autoProvisionStatusMeta(status) {
  switch (String(status || '').toLowerCase()) {
    case 'cloning':
      return { label: 'Cloning', className: 'autoprov-phase-cloning' };
    case 'configuring':
      return { label: 'Configuring', className: 'autoprov-phase-configuring' };
    case 'pending_checkin':
      return { label: 'Waiting for check-in', className: 'autoprov-phase-configuring' };
    case 'done':
      return { label: 'Done', className: 'autoprov-phase-done' };
    case 'failed':
      return { label: 'Failed', className: 'autoprov-phase-failed' };
    default:
      return { label: 'Pending', className: 'autoprov-phase-pending' };
  }
}

async function toggleAutoProvisioning() {
  const nowOn = currentSettings.usb_auto_provision === 'on';
  const newVal = nowOn ? 'off' : 'on';
  currentSettings.usb_auto_provision = newVal;
  if (usbAutoProvisionInput) usbAutoProvisionInput.checked = newVal === 'on';
  renderAutoProvisionStatus();
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectUsbSettingsPayload()),
    });
    showNotification(`VM Auto-Provisioning ${newVal === 'on' ? 'enabled' : 'disabled'}`, 'success');
  } catch (e) {
    // Revert on failure
    currentSettings.usb_auto_provision = nowOn ? 'on' : 'off';
    if (usbAutoProvisionInput) usbAutoProvisionInput.checked = nowOn;
    renderAutoProvisionStatus();
    showNotification('Failed to save setting', 'error');
  }
}

function renderAutoProvisionStatus() {
  const autoProv = currentSettings.usb_auto_provision === 'on';
  const run = getAutoProvisionRunState(latestProxmoxData);
  const total = Math.max(run.total || 0, run.items.length);
  const completed = Math.min(run.completed || 0, total);
  const failed = Math.min(run.failed || 0, Math.max(0, total - completed));

  // ── VM page status bar (right side of tab nav) ─────────────────────────────
  const bar = document.getElementById('autoprov-status-bar');
  if (bar) {
    const iconEl = document.getElementById('autoprov-status-icon');
    const textEl = document.getElementById('autoprov-status-text');
    if (iconEl) iconEl.className = 'autoprov-status-icon';
    bar.classList.remove('hidden', 'is-active', 'is-idle', 'is-disabled');

    if (!autoProv) {
      bar.classList.add('is-disabled');
      if (iconEl) iconEl.innerHTML = '<span class="autoprov-dot" aria-hidden="true"></span>';
      if (textEl) textEl.textContent = 'VM Auto-Provisioning: Off';
    } else if (run.running && total > 0) {
      bar.classList.add('is-active');
      if (iconEl) iconEl.innerHTML = '<span class="autoprov-spinner" aria-hidden="true"></span>';
      if (textEl) {
        const text = [`VM Auto-Provisioning: Provisioning… ${completed}/${total}`];
        if (failed > 0) text.push(`${failed} failed`);
        textEl.textContent = text.join(' · ');
      }
    } else {
      bar.classList.add('is-idle');
      if (iconEl) iconEl.innerHTML = '<span class="autoprov-dot" aria-hidden="true"></span>';
      if (textEl) textEl.textContent = 'VM Auto-Provisioning: Idle';
    }

    // Make the status bar a clickable toggle (attach once)
    if (!bar._autoProvClickAttached) {
      bar._autoProvClickAttached = true;
      bar.style.cursor = 'pointer';
      bar.title = 'Click to enable/disable VM Auto-Provisioning';
      bar.addEventListener('click', toggleAutoProvisioning);
    }
  }

  // ── Right-side live panel ───────────────────────────────────────────────────
  const livePanel = document.getElementById('autoprov-live-panel');
  const liveSummary = document.getElementById('autoprov-live-summary');
  const logEl = document.getElementById('auto-prov-log');
  if (!livePanel || !liveSummary || !logEl) return;

  // Panel is always visible — show idle/off state when not provisioning
  livePanel.classList.remove('hidden');

  const showPanel = run.running && total > 0;
  const resetBtn = document.getElementById('autoprov-reset-btn');
  if (resetBtn) resetBtn.style.display = run.running ? '' : 'none';
  if (!showPanel) {
    liveSummary.innerHTML = `<div class="muted" style="padding:6px 0;">${
      autoProv
        ? 'No provisioning in progress. Dongles inserted will trigger auto-provisioning.'
        : 'Auto-provisioning is disabled. Enable it in the USB settings below.'
    }</div>`;
    logEl.innerHTML = '';
    return;
  }

  const completedPct = total > 0 ? Math.round((completed / total) * 100) : 0;
  const startedText = run.startedAt ? `Started ${formatRelativeTime(run.startedAt)}` : 'Provisioning in progress';
  const summaryBits = [`${completed} of ${total} complete`];
  if (failed > 0) summaryBits.push(`${failed} failed`);
  liveSummary.innerHTML = `
    <div class="autoprov-live-summary-main">
      <strong>${summaryBits.join(' · ')}</strong>
      <span class="muted">${completedPct}%</span>
    </div>
    <div class="progress-bar-wrap autoprov-progress-wrap"><div class="progress-bar" style="width:${completedPct}%;"></div></div>
    <div class="autoprov-live-summary-sub">${escHtml(startedText)}</div>
  `;

  // Show only actively in-progress items (cloning / configuring / waiting / failed)
  // Done items drop off the list. Cap in-progress items to the configured concurrency
  // limit so we never display more simultaneous slots than the setting allows.
  const concurrency = Math.max(1, parseInt(currentSettings.reclone_concurrency, 10) || 1);
  const failedItems = run.items.filter(item => item.status === 'failed');
  const inProgressItems = run.items.filter(item => !['done', 'failed'].includes(item.status));
  const activeItems = [...inProgressItems.slice(0, concurrency), ...failedItems];
  logEl.innerHTML = activeItems.map((item) => {
    const meta = autoProvisionStatusMeta(item.status);
    // vm_name is the Proxmox VM name which equals the assigned hostname (e.g. amoran-90014).
    // Fall back to the slot number only before the clone command has named the VM.
    const hostname = item.vm_name || `Slot ${item.vmid ?? '—'}`;
    return `
      <div class="autoprov-live-item">
        <span class="autoprov-live-item-name">${escHtml(hostname)}</span>
        <span class="autoprov-phase ${meta.className}">${meta.label}</span>
      </div>
    `;
  }).join('');
}

function formatCentralDate(value) {
  if (value == null || value === '') return '—';
  const date = new Date(value > 1e12 ? value : value * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function updateCentralToolbar() {
  if (centralLastSynced) {
    centralLastSynced.textContent = centralLastSyncedTs
      ? `Last synced: ${formatCentralDate(centralLastSyncedTs / 1000)}`
      : 'Last synced: —';
  }
  if (centralTokenDot) {
    centralTokenDot.className = `status-dot ${centralTokenValid ? 'online' : 'offline'}`;
  }
  if (centralTokenText) {
    if (centralTokenValid === null) {
      centralTokenText.textContent = 'Token status unknown';
    } else {
      centralTokenText.textContent = centralTokenValid ? 'Token valid' : 'Token unavailable';
    }
  }
}

function monitoredCheckKey(check) {
  return `${check.type}:${check.id}`;
}

function currentCheckSelectionSet() {
  return new Set((currentSettings.monitored_checks || []).map(monitoredCheckKey));
}

function buildCheckBadge(label, kind) {
  const badge = document.createElement('span');
  badge.className = `check-badge ${kind}`;
  badge.textContent = label;
  return badge;
}

function buildCentralApiPayload() {
  const payload = {
    mode: getCentralApiMode(),
    classic: {
      url: centralClassicUrlInput?.value.trim() || '',
      username: centralClassicUsernameInput?.value.trim() || '',
    },
    central: {
      url: centralCentralUrlInput?.value.trim() || '',
      client_id: centralClientIdInput?.value.trim() || '',
      customer_id: centralCustomerIdInput?.value.trim() || '',
    }
  };
  const classicPassword = getSecretInputPayload(centralClassicPasswordInput);
  if (classicPassword.include) payload.classic.password = classicPassword.value;
  const clientSecret = getSecretInputPayload(centralClientSecretInput);
  if (clientSecret.include) payload.central.client_secret = clientSecret.value;
  return payload;
}

function resetCentralSecretInputs() {
  resetSecretInput(centralClassicPasswordInput);
  resetSecretInput(centralClientSecretInput);
}

async function persistCentralApiConfig() {
  const configPayload = buildCentralApiPayload();
  const response = await requestJson('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ central_api: configPayload })
  });
  applySettingsToUI(response.settings || { central_api: configPayload });
  resetCentralSecretInputs();
  return response;
}

// Site mapping source lists (populated by Load Sites)
let localWsites = [];
let centralSites = [];

function buildMappingSelect(options, selected, placeholder) {
  const sel = document.createElement('select');
  sel.className = 'mapping-val form-control';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = placeholder;
  sel.appendChild(blank);
  options.forEach((val) => {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = val;
    opt.selected = val === selected;
    sel.appendChild(opt);
  });
  return sel;
}

function buildMappingInput(value, placeholder) {
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.className = 'mapping-val';
  inp.value = value;
  inp.placeholder = placeholder;
  return inp;
}

function addMappingRow(wsite = '', centralSite = '') {
  if (!siteMappingsBody) return;
  const row = document.createElement('tr');

  const wsiteCell = document.createElement('td');
  wsiteCell.appendChild(
    localWsites.length
      ? buildMappingSelect(localWsites, wsite, '— select wsite —')
      : buildMappingInput(wsite, 'e.g. MIA')
  );

  const centralCell = document.createElement('td');
  centralCell.appendChild(
    centralSites.length
      ? buildMappingSelect(centralSites, centralSite, '— select Central site —')
      : buildMappingInput(centralSite, 'Central site name')
  );

  const removeCell = document.createElement('td');
  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'btn btn-danger btn-small';
  removeBtn.textContent = 'Remove';
  removeBtn.addEventListener('click', () => {
    row.remove();
    _autoSaveSiteMappings();
  });
  removeCell.appendChild(removeBtn);

  row.appendChild(wsiteCell);
  row.appendChild(centralCell);
  row.appendChild(removeCell);
  siteMappingsBody.appendChild(row);
}

function renderSiteMappingsTable() {
  if (!siteMappingsBody) return;
  siteMappingsBody.textContent = '';
  const entries = Object.entries(currentSettings.site_mappings || {});
  entries.forEach(([wsite, centralSite]) => addMappingRow(wsite, centralSite));
}

function renderSelectedChecksPreview() {
  if (!selectedChecksPreview) return;
  const checks = currentSettings.monitored_checks || [];
  if (!checks.length) {
    selectedChecksPreview.textContent = 'No checks selected yet.';
    return;
  }
  selectedChecksPreview.textContent = `Currently selected: ${checks.map((check) => `${check.name || check.id} (${check.type})`).join(', ')}`;
}

function renderHwChecksPreview() {
  if (!hwChecksPreview) return;
  const checks = currentSettings.hardware_checks || [];
  if (!checks.length) {
    hwChecksPreview.textContent = 'No hardware checks selected yet.';
    return;
  }
  hwChecksPreview.textContent = `Currently selected: ${checks.map((c) => c.name || c.id).join(', ')}`;
}

function renderAvailableChecks() {
  if (!availableChecksContainer) return;
  availableChecksContainer.textContent = '';
  const selection = currentCheckSelectionSet();
  const groups = [
    { key: 'alerts', title: 'Alerts' },
    { key: 'insights', title: 'AI Insights' }
  ];
  if (!availableChecks.alerts.length && !availableChecks.insights.length) {
    const empty = document.createElement('div');
    empty.className = 'form-hint';
    empty.textContent = 'No checks returned by Aruba Central.';
    availableChecksContainer.appendChild(empty);
    return;
  }
  groups.forEach(({ key, title }) => {
    const items = availableChecks[key] || [];
    if (!items.length) return;
    const group = document.createElement('div');
    group.className = 'checks-group';

    const heading = document.createElement('h3');
    heading.className = 'checks-group-title';
    heading.textContent = title;
    group.appendChild(heading);

    const list = document.createElement('div');
    list.className = 'check-checkbox-list';
    items.forEach((item) => {
      const label = document.createElement('label');
      label.className = 'check-checkbox-item';

      const input = document.createElement('input');
      input.type = 'checkbox';
      input.dataset.type = key === 'alerts' ? 'alert' : 'insight';
      input.dataset.id = item.id;
      input.dataset.name = item.name || item.id;
      input.checked = selection.has(`${input.dataset.type}:${item.id}`);

      const text = document.createElement('span');
      text.textContent = item.name || item.id;

      label.appendChild(input);
      label.appendChild(text);
      list.appendChild(label);
    });
    group.appendChild(list);
    availableChecksContainer.appendChild(group);
  });
}

function renderCentralOverview() {
  const tbody = document.getElementById('central-sites-tbody');
  const centralEmpty = document.getElementById('central-empty');
  if (!centralOverview || !tbody || !centralEmpty) return;
  updateCentralToolbar();
  tbody.textContent = '';

  const mappings = currentSettings.site_mappings || {};
  const entries = Object.entries(mappings);
  if (!entries.length) {
    centralEmpty.textContent = 'No Aruba Central site mappings configured yet.';
    centralEmpty.classList.remove('hidden');
    return;
  }

  centralEmpty.classList.add('hidden');
  const monitoredChecks = currentSettings.monitored_checks || [];

  entries.forEach(([wsite, centralSite]) => {
    const siteChecks = centralStatusData[wsite] || {};
    const okCount = monitoredChecks.filter((c) => siteChecks[c.id]?.status === 'OK').length;
    const errorCount = monitoredChecks.filter((c) => siteChecks[c.id]?.status === 'ERROR').length;
    const unknownCount = Math.max(monitoredChecks.length - okCount - errorCount, 0);
    const wirelessCount = centralWirelessClients[wsite] ?? '—';
    const simCount = [...(clients instanceof Map ? clients.values() : Object.values(clients || {}))]
      .filter((cl) => (cl.config?.wsite || cl.effective_config?.wsite || '') === wsite).length;

    const tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    tr.title = `Open ${wsite} detail`;
    tr.innerHTML = `
      <td><strong>${escHtml(wsite)}</strong></td>
      <td>${escHtml(centralSite || '—')}</td>
      <td style="color:var(--hpe-green-dark);">${monitoredChecks.length ? okCount : '—'}</td>
      <td style="color:${errorCount ? '#c0392b' : 'inherit'};">${monitoredChecks.length ? errorCount : '—'}</td>
      <td style="color:var(--muted);">${monitoredChecks.length ? unknownCount : '—'}</td>
      <td>${wirelessCount}</td>
      <td><button class="btn btn-small btn-secondary" data-wsite="${escHtml(wsite)}">View →</button></td>
    `;
    tr.querySelector('button').addEventListener('click', (e) => {
      e.stopPropagation();
      openSiteDetail(wsite);
    });
    tr.addEventListener('click', () => openSiteDetail(wsite));
    tbody.appendChild(tr);
  });
}

async function renderCentralAllAlerts() {
  const body = document.getElementById('central-all-alerts-body');
  const countBadge = document.getElementById('central-all-alerts-count');
  if (!body) return;
  body.textContent = 'Loading alerts…';
  const mappings = currentSettings.site_mappings || {};
  const entries = Object.entries(mappings);
  if (!entries.length) { body.innerHTML = '<p class="form-hint">No sites configured.</p>'; return; }

  const allAlerts = [];
  await Promise.all(entries.map(async ([wsite, centralSite]) => {
    try {
      const site = centralSite || wsite;
      const data = await requestJson(`/api/central/site-alerts?site=${encodeURIComponent(site)}`);
      (data.alerts || []).forEach((a) => allAlerts.push({ ...a, wsite }));
    } catch (_) { /* skip */ }
  }));

  body.textContent = '';
  if (countBadge) countBadge.textContent = allAlerts.length ? `(${allAlerts.length})` : '';
  if (!allAlerts.length) { body.innerHTML = '<p class="form-hint">No active alerts across any site.</p>'; return; }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `<thead><tr><th>Site</th><th>Time</th><th>Type</th><th>Severity</th><th>State</th><th>Device</th><th>Message</th></tr></thead>`;
  const tbody = document.createElement('tbody');
  allAlerts.sort((a, b) => (b.ts || 0) - (a.ts || 0)).forEach((alert) => {
    const tr = document.createElement('tr');
    [alert.wsite, formatCentralDate(alert.ts), alert.name || alert.type || '—',
      alert.severity || '—', alert.state || '—', alert.device || '—', alert.message || '—'
    ].forEach((val, i) => {
      const td = document.createElement('td');
      td.textContent = val;
      if (i === 3 && (val === 'CRITICAL' || val === 'MAJOR')) td.style.color = '#c0392b';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

function renderCentralClients() {
  const tbody = document.getElementById('central-clients-tbody');
  const empty = document.getElementById('central-clients-empty');
  if (!tbody) return;
  tbody.textContent = '';
  const mappings = currentSettings.site_mappings || {};
  const entries = Object.entries(mappings);
  if (!entries.length) { if (empty) { empty.classList.remove('hidden'); empty.textContent = 'No sites configured.'; } return; }
  if (empty) empty.classList.add('hidden');

  entries.forEach(([wsite, centralSite]) => {
    const wirelessCount = centralWirelessClients[wsite] ?? '—';
    const simCount = [...(clients instanceof Map ? clients.values() : Object.values(clients || {}))]
      .filter((cl) => (cl.config?.wsite || cl.effective_config?.wsite || '') === wsite).length;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${escHtml(wsite)}</td><td>${escHtml(centralSite || '—')}</td><td>${wirelessCount}</td><td>${simCount}</td>`;
    tbody.appendChild(tr);
  });
}

async function renderCentralAllHistory() {
  const body = document.getElementById('central-all-history-body');
  if (!body) return;
  body.textContent = 'Loading history…';
  const mappings = currentSettings.site_mappings || {};
  const entries = Object.entries(mappings);
  if (!entries.length) { body.innerHTML = '<p class="form-hint">No sites configured.</p>'; return; }

  const allRecords = [];
  await Promise.all(entries.map(async ([wsite]) => {
    try {
      const data = await requestJson(`/api/central/history?site=${encodeURIComponent(wsite)}&hours=24`);
      (data.records || []).forEach((r) => allRecords.push({ ...r, wsite }));
    } catch (_) { /* skip */ }
  }));

  body.textContent = '';
  if (!allRecords.length) { body.innerHTML = '<p class="form-hint">No history in the last 24 hours.</p>'; return; }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `<thead><tr><th>Site</th><th>Time</th><th>Check</th><th>Status</th><th>Count</th></tr></thead>`;
  const tbody = document.createElement('tbody');
  allRecords.sort((a, b) => (b.ts || 0) - (a.ts || 0)).slice(0, 200).forEach((r) => {
    const tr = document.createElement('tr');
    [r.wsite, formatCentralDate(r.ts), r.check_name || r.check_id || '—', r.status || '—', String(r.count ?? '—')]
      .forEach((val) => { const td = document.createElement('td'); td.textContent = val; tr.appendChild(td); });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

async function loadSiteHistory(wsite) {
  if (!centralSiteHistory) return;
  centralSiteHistory.textContent = 'Loading history…';
  try {
    const data = await requestJson(`/api/central/history?site=${encodeURIComponent(wsite)}&hours=24`);
    renderSiteHistory(data.records || []);
  } catch (error) {
    centralSiteHistory.textContent = `Could not load history: ${error.message}`;
  }
}

function renderSiteClients(wsite) {
  if (!centralSiteClients) return;
  centralSiteClients.textContent = '';
  const siteClients = [...clients.values()]
    .filter((client) => (client.config?.wsite || client.effective_config?.wsite || '') === wsite)
    .sort((a, b) => (a.hostname || '').localeCompare(b.hostname || ''));

  if (!siteClients.length) {
    const empty = document.createElement('div');
    empty.className = 'form-hint';
    empty.textContent = 'No connected or known clients for this site.';
    centralSiteClients.appendChild(empty);
    return;
  }

  siteClients.forEach((client) => {
    const row = document.createElement('div');
    row.className = 'client-mini-row';

    const dot = document.createElement('span');
    dot.className = `status-dot ${client.online ? 'online' : 'offline'}`;

    const host = document.createElement('span');
    host.className = 'client-mini-host';
    host.textContent = client.hostname || '—';

    const meta = document.createElement('span');
    meta.className = 'client-mini-sim';
    const active = (client.active_simulations || []).join(', ') || 'No active simulations';
    meta.textContent = `${client.simulation_id || '—'} · ${active}`;

    row.appendChild(dot);
    row.appendChild(host);
    row.appendChild(meta);
    centralSiteClients.appendChild(row);
  });
}

function renderSiteChecks(wsite, checkStatusMap) {
  if (!centralSiteChecks) return;
  centralSiteChecks.textContent = '';
  const monitoredChecks = currentSettings.monitored_checks || [];
  if (!monitoredChecks.length) {
    const empty = document.createElement('div');
    empty.className = 'form-hint';
    empty.textContent = 'No monitored checks configured.';
    centralSiteChecks.appendChild(empty);
    return;
  }

  monitoredChecks.forEach((check) => {
    const status = checkStatusMap[check.id] || null;
    const row = document.createElement('div');
    row.className = 'check-status-row';

    const left = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'check-status-name';
    name.textContent = check.name || check.id;
    const meta = document.createElement('div');
    meta.className = 'check-status-count';
    meta.textContent = `${check.type} · ${status ? `Updated ${formatCentralDate(status.ts)}` : 'Not yet polled'}`;
    left.appendChild(name);
    left.appendChild(meta);

    const right = document.createElement('div');
    right.style.display = 'flex';
    right.style.alignItems = 'center';
    right.style.gap = '8px';
    right.appendChild(buildCheckBadge(
      status ? status.status : 'UNKNOWN',
      status?.status === 'OK' ? 'check-badge-ok' : status?.status === 'ERROR' ? 'check-badge-error' : 'check-badge-unknown'
    ));

    const count = document.createElement('span');
    count.className = 'check-status-count';
    count.textContent = status ? `Count ${status.count ?? 0}` : 'Count —';
    right.appendChild(count);

    row.appendChild(left);
    row.appendChild(right);
    centralSiteChecks.appendChild(row);
  });
}

function renderSiteHistory(records) {
  if (!centralSiteHistory) return;
  centralSiteHistory.textContent = '';
  const sorted = [...records]
    .sort((a, b) => (b.ts || 0) - (a.ts || 0))
    .slice(0, 100);

  if (!sorted.length) {
    centralSiteHistory.textContent = 'No history records in the last 24 hours.';
    return;
  }

  const table = document.createElement('table');
  table.className = 'history-table';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['Time', 'Check', 'Status', 'Count'].forEach((label) => {
    const th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);

  const tbodyEl = document.createElement('tbody');
  sorted.forEach((record) => {
    const row = document.createElement('tr');
    const values = [
      formatCentralDate(record.ts),
      record.check_name || record.check_id || '—',
      record.status || '—',
      String(record.count ?? '—')
    ];
    values.forEach((value) => {
      const td = document.createElement('td');
      td.textContent = value;
      row.appendChild(td);
    });
    tbodyEl.appendChild(row);
  });

  table.appendChild(thead);
  table.appendChild(tbodyEl);
  centralSiteHistory.appendChild(table);
}

async function loadSiteAlerts(wsite) {
  if (!centralSiteAlerts) return;
  centralSiteAlerts.textContent = 'Loading alerts…';
  if (centralSiteAlertsCount) centralSiteAlertsCount.textContent = '';
  const centralSite = currentSettings.site_mappings?.[wsite] || wsite;
  try {
    const data = await requestJson(`/api/central/site-alerts?site=${encodeURIComponent(centralSite)}`);
    renderSiteAlerts(data.alerts || [], data.warning);
    if (centralSiteAlertsCount) {
      centralSiteAlertsCount.textContent = data.count ? `(${data.count})` : '';
    }
  } catch (err) {
    centralSiteAlerts.textContent = `Could not load alerts: ${err.message}`;
  }
}

function renderSiteAlerts(alerts, warning) {
  if (!centralSiteAlerts) return;
  centralSiteAlerts.textContent = '';

  if (warning && !alerts.length) {
    const msg = document.createElement('div');
    msg.className = 'form-hint';
    msg.textContent = warning;
    centralSiteAlerts.appendChild(msg);
    return;
  }

  if (warning) {
    const msg = document.createElement('div');
    msg.className = 'form-hint';
    msg.textContent = `⚠ ${warning}`;
    centralSiteAlerts.appendChild(msg);
  }

  const table = document.createElement('table');
  table.className = 'history-table';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['Time', 'Type', 'Severity', 'State', 'Device', 'Message'].forEach((label) => {
    const th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);

  const tbody = document.createElement('tbody');
  alerts.forEach((alert) => {
    const row = document.createElement('tr');
    [
      formatCentralDate(alert.ts),
      alert.name || alert.type || '—',
      alert.severity || '—',
      alert.state || '—',
      alert.device || '—',
      alert.message || '—',
    ].forEach((val) => {
      const td = document.createElement('td');
      td.textContent = val;
      if (val === 'CRITICAL' || val === 'MAJOR') td.style.color = 'var(--color-error, #c0392b)';
      row.appendChild(td);
    });
    tbody.appendChild(row);
  });

  table.appendChild(thead);
  table.appendChild(tbody);
  centralSiteAlerts.appendChild(table);
}


async function loadSiteDevices(wsite) {
  if (!centralSiteDevices) return;
  const isCnx = (currentSettings.central_api?.mode === 'central') ||
                (currentSettings.central_config?.api_version === 'new_central');
  // Show/hide the devices section based on mode
  document.querySelectorAll('.cnx-only').forEach((el) => {
    el.classList.toggle('hidden', !isCnx);
  });
  if (!isCnx) return;

  centralSiteDevices.textContent = 'Loading devices…';
  if (centralSiteDevicesCount) centralSiteDevicesCount.textContent = '';
  const centralSite = currentSettings.site_mappings?.[wsite] || wsite;
  try {
    const data = await requestJson(`/api/central/devices?site=${encodeURIComponent(centralSite)}`);
    renderSiteDevices(data.devices || [], data.warning, data.count || 0);
  } catch (err) {
    centralSiteDevices.textContent = `Could not load devices: ${err.message}`;
  }
}

function renderSiteDevices(devices, warning, total) {
  if (!centralSiteDevices) return;
  centralSiteDevices.textContent = '';
  if (centralSiteDevicesCount) {
    centralSiteDevicesCount.textContent = total ? `(${total})` : '';
  }

  if (warning && !devices.length) {
    const msg = document.createElement('div');
    msg.className = 'form-hint';
    msg.textContent = warning;
    centralSiteDevices.appendChild(msg);
    return;
  }
  if (warning) {
    const msg = document.createElement('div');
    msg.className = 'form-hint';
    msg.textContent = `⚠ ${warning}`;
    centralSiteDevices.appendChild(msg);
  }

  const table = document.createElement('table');
  table.className = 'history-table';
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['Status', 'Name', 'Type', 'Model', 'IP', 'Version'].forEach((label) => {
    const th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);

  const tbody = document.createElement('tbody');
  devices.forEach((dev) => {
    const row = document.createElement('tr');
    const statusUp = ['UP', 'ONLINE'].includes((dev.status || '').toUpperCase());
    [
      null,  // status dot handled separately
      dev.name || '—',
      (dev.type || '—').replace('_', ' '),
      dev.model || '—',
      dev.ip || '—',
      dev.version || '—',
    ].forEach((val, i) => {
      const td = document.createElement('td');
      if (i === 0) {
        const dot = document.createElement('span');
        dot.className = `status-dot ${statusUp ? 'online' : 'offline'}`;
        dot.title = dev.status || 'Unknown';
        td.appendChild(dot);
        td.appendChild(document.createTextNode(' ' + (dev.status || '—')));
        if (!statusUp) td.style.color = 'var(--color-error, #c0392b)';
      } else {
        td.textContent = val;
      }
      row.appendChild(td);
    });
    tbody.appendChild(row);
  });

  table.appendChild(thead);
  table.appendChild(tbody);
  centralSiteDevices.appendChild(table);
}

function openSiteDetail(wsite) {
  centralSiteDetailOpen = wsite;
  if (centralOverview) centralOverview.classList.add('hidden');
  if (centralSiteDetail) centralSiteDetail.classList.remove('hidden');
  if (centralDetailTitle) centralDetailTitle.textContent = wsite;
  if (centralDetailSub) {
    centralDetailSub.textContent = `Central site: ${currentSettings.site_mappings?.[wsite] || 'Unmapped'}`;
  }
  renderSiteClients(wsite);
  renderSiteChecks(wsite, centralStatusData[wsite] || {});
  loadSiteHistory(wsite);
  loadSiteAlerts(wsite);
  loadSiteDevices(wsite);
}

function closeSiteDetail() {
  centralSiteDetailOpen = null;
  if (centralSiteDetail) centralSiteDetail.classList.add('hidden');
  if (centralOverview) centralOverview.classList.remove('hidden');
}

function handleCentralUpdate(status, ts, wirelessClients, hwAlerts, ccStatus) {
  centralStatusData = status || {};
  if (wirelessClients) centralWirelessClients = wirelessClients;
  if (hwAlerts) hwAlertsData = hwAlerts;
  if (ccStatus) clientCountData = ccStatus;
  centralLastSyncedTs = ts ? ts * 1000 : Date.now();
  renderCentralOverview();
  renderChecksList();
  if (centralSiteDetailOpen) {
    renderSiteClients(centralSiteDetailOpen);
    renderSiteChecks(centralSiteDetailOpen, centralStatusData[centralSiteDetailOpen] || {});
    loadSiteHistory(centralSiteDetailOpen);
  }
}

async function loadSettings() {
  try {
    const settings = await requestJson('/api/settings');
    applySettingsToUI(settings || {});
    await loadSpokeAuthSettings(settings).catch(() => {});
    await loadUsbConfig().catch(() => {});
    await loadSpokeAcmeSettings().catch(() => {});
  } catch (error) {
    showSettingsMessage(`Error loading settings: ${error.message}`, true);
  }
}



function spokeAcmeBadgeClass(daysRemaining) {
  if (typeof daysRemaining !== 'number' || Number.isNaN(daysRemaining)) return 'badge-grey';
  if (daysRemaining > 30) return 'badge-green';
  if (daysRemaining >= 10) return 'badge-yellow';
  return 'badge-red';
}

function toggleSpokeAcmeDnsSection() {
  const provider = document.getElementById('spoke-acme-dns-provider')?.value || 'cloudflare';
  const allFields = ['cloudflare', 'he', 'godaddy', 'do', 'porkbun', 'gcloud', 'dnsimple', 'azure', 'route53', 'namecheap'];
  allFields.forEach((p) => document.getElementById(`spoke-acme-${p}-fields`)?.classList.add('hidden'));
  const map = {
    cloudflare: 'spoke-acme-cloudflare-fields',
    hurricane_electric: 'spoke-acme-he-fields',
    godaddy: 'spoke-acme-godaddy-fields',
    digitalocean: 'spoke-acme-do-fields',
    porkbun: 'spoke-acme-porkbun-fields',
    gcloud: 'spoke-acme-gcloud-fields',
    dnsimple: 'spoke-acme-dnsimple-fields',
    azure_dns: 'spoke-acme-azure-fields',
    route53: 'spoke-acme-route53-fields',
    namecheap: 'spoke-acme-namecheap-fields',
  };
  const target = map[provider];
  if (target) document.getElementById(target)?.classList.remove('hidden');
}

function renderSpokeAcmeStatus(certInfo = {}, cfg = {}) {
  const container = document.getElementById('spoke-acme-cert-status');
  if (!container) return;
  if (!certInfo || certInfo.source === 'none') {
    container.innerHTML = `
      <div class="setup-status-item"><span class="setup-status-label">Certificate</span><span class="setup-status-value">Not configured</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Challenge</span><span class="setup-status-value">DNS-01</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Authority</span><span class="setup-status-value">${escapeHtml(cfg.ca || 'letsencrypt')}</span></div>
      <div class="setup-status-item"><span class="setup-status-label">HTTPS Mode</span><span class="setup-status-value">${cfg.spoke_tls === 'on' ? 'Enabled on restart' : 'Disabled'}</span></div>
    `;
    return;
  }
  const days = Number(certInfo.days_remaining ?? 0);
  container.innerHTML = `
    <div class="setup-status-item"><span class="setup-status-label">Domain</span><span class="setup-status-value">${escapeHtml(certInfo.domain || cfg.domain || '—')}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">Expires</span><span class="setup-status-value">${escapeHtml(certInfo.expires || '—')} <span class="badge ${spokeAcmeBadgeClass(days)}">${Number.isFinite(days) ? `${days} days` : 'unknown'}</span></span></div>
    <div class="setup-status-item"><span class="setup-status-label">Issuer</span><span class="setup-status-value">${escapeHtml(certInfo.issuer || '—')}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">HTTPS Mode</span><span class="setup-status-value">${cfg.spoke_tls === 'on' ? 'Enabled on restart' : 'Disabled'}</span></div>
  `;
}

async function loadSpokeAcmeSettings() {
  const data = await requestJson('/api/acme');
  const creds = data.dns_credentials || {};
  const configured = data.dns_credentials_configured || {};
  const setValue = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.value = value || '';
  };
  setValue('spoke-acme-domain', data.domain || '');
  setValue('spoke-acme-email', data.email || '');
  setValue('spoke-acme-ca', data.ca || 'letsencrypt');
  setValue('spoke-acme-dns-provider', data.dns_provider || 'cloudflare');
  setValue('spoke-acme-gcloud-sa-json', configured.gcloud_service_account_json || creds.gcloud_service_account_json || '');
  setValue('spoke-acme-gcloud-zone', creds.gcloud_zone_name || '');
  setValue('spoke-acme-dnsimple-account-id', creds.dnsimple_account_id || '');
  setValue('spoke-acme-azure-tenant', creds.azure_tenant_id || '');
  setValue('spoke-acme-azure-client-id', creds.azure_client_id || '');
  setValue('spoke-acme-azure-sub', creds.azure_subscription_id || '');
  setValue('spoke-acme-azure-rg', creds.azure_resource_group || '');
  setValue('spoke-acme-azure-zone', creds.azure_zone_name || '');
  setValue('spoke-acme-r53-key', creds.route53_access_key || '');
  setValue('spoke-acme-r53-zone-id', creds.route53_zone_id || '');
  setValue('spoke-acme-nc-user', creds.namecheap_username || '');
  setValue('spoke-acme-nc-ip', creds.namecheap_client_ip || '');
  const enabled = document.getElementById('spoke-acme-enabled');
  if (enabled) enabled.checked = !!data.enabled;
  const tlsEnabled = document.getElementById('spoke-tls-enabled');
  if (tlsEnabled) tlsEnabled.checked = data.spoke_tls === 'on';
  setSecretInputConfigured(document.getElementById('spoke-acme-cf-token'), isConfiguredSecretValue(data.cf_api_token_set ?? configured.cf_api_token ?? creds.cf_api_token));
  setSecretInputConfigured(document.getElementById('spoke-acme-he-ddns-key'), isConfiguredSecretValue(data.he_ddns_key_set ?? configured.he_ddns_key ?? creds.he_ddns_key));
  setSecretInputConfigured(document.getElementById('spoke-acme-godaddy-key'), isConfiguredSecretValue(configured.godaddy_api_key ?? creds.godaddy_api_key));
  setSecretInputConfigured(document.getElementById('spoke-acme-godaddy-secret'), isConfiguredSecretValue(configured.godaddy_api_secret ?? creds.godaddy_api_secret));
  setSecretInputConfigured(document.getElementById('spoke-acme-do-token'), isConfiguredSecretValue(configured.do_token ?? creds.do_token));
  setSecretInputConfigured(document.getElementById('spoke-acme-porkbun-key'), isConfiguredSecretValue(configured.porkbun_api_key ?? creds.porkbun_api_key));
  setSecretInputConfigured(document.getElementById('spoke-acme-porkbun-secret'), isConfiguredSecretValue(configured.porkbun_secret_key ?? creds.porkbun_secret_key));
  setSecretInputConfigured(document.getElementById('spoke-acme-dnsimple-token'), isConfiguredSecretValue(configured.dnsimple_token ?? creds.dnsimple_token));
  setSecretInputConfigured(document.getElementById('spoke-acme-azure-client-secret'), isConfiguredSecretValue(configured.azure_client_secret ?? creds.azure_client_secret));
  setSecretInputConfigured(document.getElementById('spoke-acme-r53-secret'), isConfiguredSecretValue(configured.route53_secret_key ?? creds.route53_secret_key));
  setSecretInputConfigured(document.getElementById('spoke-acme-nc-key'), isConfiguredSecretValue(configured.namecheap_api_key ?? creds.namecheap_api_key));
  toggleSpokeAcmeDnsSection();
  renderSpokeAcmeStatus(data.cert_info || {}, data);
}

async function saveSpokeAcmeConfig() {
  const payload = {
    enabled: !!document.getElementById('spoke-acme-enabled')?.checked,
    domain: document.getElementById('spoke-acme-domain')?.value.trim() || '',
    email: document.getElementById('spoke-acme-email')?.value.trim() || '',
    ca: document.getElementById('spoke-acme-ca')?.value || 'letsencrypt',
    challenge: 'dns-01',
    dns_provider: document.getElementById('spoke-acme-dns-provider')?.value || '',
    dns_credentials: {},
    spoke_tls: document.getElementById('spoke-tls-enabled')?.checked ? 'on' : 'off'
  };
  const addSecret = (key, id) => {
    const secret = getSecretInputPayload(document.getElementById(id));
    if (secret.include) payload.dns_credentials[key] = secret.value;
  };
  const addValue = (key, id) => {
    const value = document.getElementById(id)?.value.trim();
    if (value) payload.dns_credentials[key] = value;
  };
  addSecret('cf_api_token', 'spoke-acme-cf-token');
  addSecret('he_ddns_key', 'spoke-acme-he-ddns-key');
  addSecret('godaddy_api_key', 'spoke-acme-godaddy-key');
  addSecret('godaddy_api_secret', 'spoke-acme-godaddy-secret');
  addSecret('do_token', 'spoke-acme-do-token');
  addSecret('porkbun_api_key', 'spoke-acme-porkbun-key');
  addSecret('porkbun_secret_key', 'spoke-acme-porkbun-secret');
  addValue('gcloud_service_account_json', 'spoke-acme-gcloud-sa-json');
  addValue('gcloud_zone_name', 'spoke-acme-gcloud-zone');
  addSecret('dnsimple_token', 'spoke-acme-dnsimple-token');
  addValue('dnsimple_account_id', 'spoke-acme-dnsimple-account-id');
  addValue('azure_tenant_id', 'spoke-acme-azure-tenant');
  addValue('azure_client_id', 'spoke-acme-azure-client-id');
  addSecret('azure_client_secret', 'spoke-acme-azure-client-secret');
  addValue('azure_subscription_id', 'spoke-acme-azure-sub');
  addValue('azure_resource_group', 'spoke-acme-azure-rg');
  addValue('azure_zone_name', 'spoke-acme-azure-zone');
  addValue('route53_access_key', 'spoke-acme-r53-key');
  addSecret('route53_secret_key', 'spoke-acme-r53-secret');
  addValue('route53_zone_id', 'spoke-acme-r53-zone-id');
  addSecret('namecheap_api_key', 'spoke-acme-nc-key');
  addValue('namecheap_username', 'spoke-acme-nc-user');
  addValue('namecheap_client_ip', 'spoke-acme-nc-ip');
  try {
    const data = await requestJson('/api/acme', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const msg = document.getElementById('spoke-acme-msg');
    if (msg) {
      msg.textContent = 'TLS certificate settings saved.';
      msg.className = 'form-msg msg-ok';
    }
    renderSpokeAcmeStatus(data.cert_info || {}, data);
    const creds = data.dns_credentials || {};
    const configured = data.dns_credentials_configured || {};
    setSecretInputConfigured(document.getElementById('spoke-acme-cf-token'), isConfiguredSecretValue(data.cf_api_token_set ?? configured.cf_api_token ?? creds.cf_api_token));
    setSecretInputConfigured(document.getElementById('spoke-acme-he-ddns-key'), isConfiguredSecretValue(data.he_ddns_key_set ?? configured.he_ddns_key ?? creds.he_ddns_key));
    setSecretInputConfigured(document.getElementById('spoke-acme-godaddy-key'), isConfiguredSecretValue(configured.godaddy_api_key ?? creds.godaddy_api_key));
    setSecretInputConfigured(document.getElementById('spoke-acme-godaddy-secret'), isConfiguredSecretValue(configured.godaddy_api_secret ?? creds.godaddy_api_secret));
    setSecretInputConfigured(document.getElementById('spoke-acme-do-token'), isConfiguredSecretValue(configured.do_token ?? creds.do_token));
    setSecretInputConfigured(document.getElementById('spoke-acme-porkbun-key'), isConfiguredSecretValue(configured.porkbun_api_key ?? creds.porkbun_api_key));
    setSecretInputConfigured(document.getElementById('spoke-acme-porkbun-secret'), isConfiguredSecretValue(configured.porkbun_secret_key ?? creds.porkbun_secret_key));
    setSecretInputConfigured(document.getElementById('spoke-acme-dnsimple-token'), isConfiguredSecretValue(configured.dnsimple_token ?? creds.dnsimple_token));
    setSecretInputConfigured(document.getElementById('spoke-acme-azure-client-secret'), isConfiguredSecretValue(configured.azure_client_secret ?? creds.azure_client_secret));
    setSecretInputConfigured(document.getElementById('spoke-acme-r53-secret'), isConfiguredSecretValue(configured.route53_secret_key ?? creds.route53_secret_key));
    setSecretInputConfigured(document.getElementById('spoke-acme-nc-key'), isConfiguredSecretValue(configured.namecheap_api_key ?? creds.namecheap_api_key));
  } catch (error) {
    const msg = document.getElementById('spoke-acme-msg');
    if (msg) {
      msg.textContent = `Error: ${error.message}`;
      msg.className = 'form-msg msg-error';
    }
  }
}

let spokeAcmePoller = null;

async function pollSpokeAcmeStatus() {
  try {
    const status = await requestJson('/api/acme/status');
    if (!status.running) {
      if (spokeAcmePoller) {
        clearInterval(spokeAcmePoller);
        spokeAcmePoller = null;
      }
      const btn = document.getElementById('spoke-acme-request-btn');
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Request Certificate Now';
      }
      const msg = document.getElementById('spoke-acme-msg');
      if (status.last_result?.success) {
        if (msg) {
          msg.textContent = `Certificate issued for ${status.last_result.domain} — restart the spoke to enable HTTPS.`;
          msg.className = 'form-msg msg-ok';
        }
        await loadSpokeAcmeSettings();
      } else if (status.last_error && msg) {
        msg.textContent = status.last_error;
        msg.className = 'form-msg msg-error';
      }
    }
  } catch (error) {
    console.warn('ACME status poll failed', error);
  }
}

async function requestSpokeAcmeCert() {
  const btn = document.getElementById('spoke-acme-request-btn');
  const msg = document.getElementById('spoke-acme-msg');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Requesting certificate…';
  }
  if (msg) {
    msg.textContent = 'Requesting certificate… (this may take 60-90 seconds)';
    msg.className = 'form-msg msg-ok';
  }
  try {
    await requestJson('/api/acme/request', { method: 'POST' });
    if (spokeAcmePoller) clearInterval(spokeAcmePoller);
    spokeAcmePoller = setInterval(pollSpokeAcmeStatus, 2000);
    await pollSpokeAcmeStatus();
  } catch (error) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Request Certificate Now';
    }
    if (msg) {
      msg.textContent = `Error: ${error.message}`;
      msg.className = 'form-msg msg-error';
    }
  }
}

window.saveSpokeAcmeConfig = saveSpokeAcmeConfig;
window.requestSpokeAcmeCert = requestSpokeAcmeCert;


async function loadCentralStatus() {
  centralStatusInitialized = true;
  const centralStatusErrorPrefix = 'Could not load Central status:';
  try {
    const data = await requestJson('/api/central/status');
    applySettingsToUI({
      site_mappings: data.site_mappings || {},
      monitored_checks: data.monitored_checks || [],
      central_api: data.central_api || currentSettings.central_api || defaultCentralApiSettings(),
    });
    if (centralConfigMsg?.textContent?.startsWith(centralStatusErrorPrefix)) {
      showInlineMessage(centralConfigMsg, '', false, 0);
    }
    centralTokenValid = Boolean(data.token_valid);
    setCentralApiStatus(centralTokenValid, data.token_state);
    handleCentralUpdate(
      data.status || {},
      Date.now() / 1000,
      data.wireless_clients || {},
      data.hardware_alerts || [],
      data.client_count_status || {}
    );
    renderSelectedChecksPreview();
    renderSiteMappingsTable();
  } catch (error) {
    centralTokenValid = false;
    setCentralApiStatus(false);
    updateCentralToolbar();
    showInlineMessage(centralConfigMsg, `${centralStatusErrorPrefix} ${error.message}`, true, 0);
    if (centralEmpty) {
      centralEmpty.classList.add('hidden');
    }
  }
}

function buildToggle(flag, checked) {
  const wrapper = document.createElement('label');
  wrapper.className = 'toggle-item';

  const text = document.createElement('span');
  text.className = 'toggle-label';
  text.textContent = flag;

  const switchLabel = document.createElement('span');
  switchLabel.className = 'switch';

  const input = document.createElement('input');
  input.type = 'checkbox';
  input.dataset.flag = flag;
  input.checked = checked;

  const slider = document.createElement('span');
  slider.className = 'slider';

  switchLabel.appendChild(input);
  switchLabel.appendChild(slider);
  wrapper.appendChild(text);
  wrapper.appendChild(switchLabel);
  return wrapper;
}

function renderControlPanel(hostname) {
  const client = clients.get(hostname);
  const refs = rowRefs.get(hostname);
  if (!client || !refs) return;

  const baseConfig = client.effective_config || client.config || {};
  refs.detailCell.textContent = '';

  const panel = document.createElement('div');
  panel.className = 'control-panel';

  const header = document.createElement('div');
  header.className = 'control-panel-header';
  const title = document.createElement('h2');
  title.textContent = client.hostname;
  const subtitle = document.createElement('p');
  subtitle.textContent = 'Set per-client or global simulation overrides.';
  header.appendChild(title);
  header.appendChild(subtitle);

  const toggles = document.createElement('div');
  toggles.className = 'toggle-grid';
  FLAG_ORDER.forEach((flag) => {
    toggles.appendChild(buildToggle(flag, normalizeFlagValue(baseConfig[flag]) === 'on'));
  });

  const actions = document.createElement('div');
  actions.className = 'panel-actions';

  const applyButton = document.createElement('button');
  applyButton.type = 'button';
  applyButton.className = 'btn btn-primary';
  applyButton.textContent = 'Apply';
  applyButton.addEventListener('click', async () => {
    try {
      const nextState = collectPanelState(panel);
      const diff = {};
      FLAG_ORDER.forEach((flag) => {
        if (normalizeFlagValue(baseConfig[flag]) !== nextState[flag]) {
          diff[flag] = nextState[flag];
        }
      });
      if (!Object.keys(diff).length) return;
      const result = await sendJson(`/api/clients/${encodeURIComponent(hostname)}/control`, {
        method: 'POST',
        body: JSON.stringify(diff)
      });
      if (result?.client) upsertClient(result.client);
    } catch (error) {
      window.alert(`Apply failed: ${error.message}`);
    }
  });

  const clearButton = document.createElement('button');
  clearButton.type = 'button';
  clearButton.className = 'btn btn-secondary';
  clearButton.textContent = 'Clear Overrides';
  clearButton.addEventListener('click', async () => {
    try {
      const result = await sendJson(`/api/clients/${encodeURIComponent(hostname)}/control`, {
        method: 'DELETE'
      });
      if (result?.client) upsertClient(result.client);
    } catch (error) {
      window.alert(`Clear failed: ${error.message}`);
    }
  });

  const applyAllButton = document.createElement('button');
  applyAllButton.type = 'button';
  applyAllButton.className = 'btn btn-danger';
  applyAllButton.textContent = 'Apply to ALL';
  applyAllButton.addEventListener('click', async () => {
    try {
      const nextState = collectPanelState(panel);
      await sendJson('/api/clients/all/control', {
        method: 'POST',
        body: JSON.stringify(nextState)
      });
    } catch (error) {
      window.alert(`Apply to ALL failed: ${error.message}`);
    }
  });

  const saveOverridesButton = document.createElement('button');
  saveOverridesButton.type = 'button';
  saveOverridesButton.className = 'btn btn-primary';
  saveOverridesButton.style.marginLeft = 'auto';
  saveOverridesButton.textContent = 'Save to user-overrides';
  saveOverridesButton.addEventListener('click', async () => {
    try {
      const username = hostname.split('-')[0] || hostname;
      const flags = collectPanelState(panel);
      const result = await requestJson('/api/config/overrides/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, flags })
      });
      window.alert(result?.pushed ? 'Saved to user-overrides and pushed to GitHub.' : 'Saved to user-overrides locally.');
    } catch (error) {
      window.alert(`Save to user-overrides failed: ${error.message}`);
    }
  });

  actions.appendChild(applyButton);
  actions.appendChild(clearButton);
  actions.appendChild(applyAllButton);
  if (currentSettings.github_token_configured) {
    actions.appendChild(saveOverridesButton);
  }

  panel.appendChild(header);
  panel.appendChild(toggles);
  panel.appendChild(actions);

  // Error log section — shows the rolling buffer of errors reported by this client.
  // WHY: Operators need to diagnose why a client isn't connecting. Rather than
  // SSH-ing into the client to read log files, the error messages are surfaced here
  // directly in the dashboard so the problem can be identified remotely.
  const recentErrors = client.recent_errors || [];
  const errorSection = document.createElement('div');
  errorSection.className = 'error-log-section';
  const errorTitle = document.createElement('h3');
  const errCount = client.error_count || 0;
  errorTitle.textContent = `Error Log (${recentErrors.length} shown, ${errCount} total)`;
  errorSection.appendChild(errorTitle);

  if (recentErrors.length === 0) {
    const none = document.createElement('p');
    none.className = 'error-log-empty';
    none.textContent = 'No errors reported.';
    errorSection.appendChild(none);
  } else {
    const ul = document.createElement('ul');
    ul.className = 'error-log-list';
    // Show newest errors first so the operator sees the latest problem immediately
    [...recentErrors].reverse().forEach(({ ts, msg }) => {
      const li = document.createElement('li');
      const time = document.createElement('span');
      time.className = 'error-ts';
      time.textContent = ts || '';
      const message = document.createElement('span');
      message.className = 'error-msg';
      message.textContent = msg || '';
      li.appendChild(time);
      li.appendChild(message);
      ul.appendChild(li);
    });
    errorSection.appendChild(ul);
  }

  panel.appendChild(errorSection);
  refs.detailCell.appendChild(panel);
}

function toggleControlRow(hostname) {
  if (openControlHost && openControlHost !== hostname) {
    const currentRefs = rowRefs.get(openControlHost);
    if (currentRefs) {
      currentRefs.detailRow.classList.add('hidden');
      currentRefs.mainRow.classList.remove('expanded');
      currentRefs.controlButton.textContent = 'Control';
    }
  }

  const refs = rowRefs.get(hostname);
  if (!refs) return;

  const shouldOpen = openControlHost !== hostname || refs.detailRow.classList.contains('hidden');
  refs.detailRow.classList.toggle('hidden', !shouldOpen);
  refs.mainRow.classList.toggle('expanded', shouldOpen);
  refs.controlButton.textContent = shouldOpen ? 'Close' : 'Control';
  openControlHost = shouldOpen ? hostname : null;

  if (shouldOpen) {
    renderControlPanel(hostname);
  }
}

function buildConfigInput(field, value = '') {
  const group = document.createElement('div');
  group.className = 'form-group';

  const label = document.createElement('label');
  label.className = 'form-label';
  label.textContent = field.key;

  const input = document.createElement('input');
  input.className = 'form-input';
  input.type = field.type || 'text';
  input.value = value || '';
  input.dataset.configSection = field.section;
  input.dataset.configKey = field.key;

  group.appendChild(label);
  group.appendChild(input);
  return { group, input };
}

function buildConfigSelect(section, key, options, value = '') {
  const group = document.createElement('div');
  group.className = 'form-group';

  const label = document.createElement('label');
  label.className = 'form-label';
  label.textContent = key;

  const select = document.createElement('select');
  select.className = 'form-input';
  select.dataset.configSection = section;
  select.dataset.configKey = key;

  options.forEach((optionValue) => {
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = optionValue;
    option.selected = optionValue === value;
    select.appendChild(option);
  });

  group.appendChild(label);
  group.appendChild(select);
  return { group, select };
}

function buildConfigToggle(field, value) {
  const toggle = buildToggle(field.key, normalizeFlagValue(value) === 'on');
  const input = toggle.querySelector('input');
  if (input) {
    input.dataset.configSection = field.section;
    input.dataset.configKey = field.key;
  }
  return toggle;
}

function collectSectionedConfigState(root) {
  const payloads = {};
  if (!root) return payloads;
  root.querySelectorAll('[data-config-section][data-config-key]').forEach((input) => {
    const section = input.dataset.configSection;
    const key = input.dataset.configKey;
    if (!section || !key) return;
    if (!payloads[section]) payloads[section] = {};
    payloads[section][key] = input.type === 'checkbox' ? (input.checked ? 'on' : 'off') : input.value;
  });
  return payloads;
}

function buildBucketSummary(section, values = {}) {
  return `${section} — ${values.name || values.wsite || 'Unnamed bucket'}`;
}

const ADDRESS_SECTION_RE = /^(server|address)$/i;

function _buildSectionCard(section, values, container) {
  const card = document.createElement('div');
  card.className = 'setup-card setup-section-gap';

  const hdr = document.createElement('div');
  hdr.className = 'setup-card-header';
  const h2 = document.createElement('h2');
  h2.textContent = `[${_fmtSection(section)}]`;
  hdr.appendChild(h2);
  card.appendChild(hdr);

  const form = document.createElement('div');
  form.className = 'setup-form';

  const textPairs = [], boolPairs = [];
  Object.entries(values).forEach(([key, val]) => {
    (_isBoolVal(val) ? boolPairs : textPairs).push([key, val]);
  });

  const fieldGrid = document.createElement('div');
  fieldGrid.className = 'config-field-grid';

  textPairs.forEach(([key, val]) => {
    if (key === 'sim_load') {
      const simLoadOptions = ['100', '75', '50', '25', '0'];
      const { group, select } = buildConfigSelect(section, key, simLoadOptions, String(val));
      const lbl = group.querySelector('label');
      if (lbl) lbl.textContent = 'Sim Load %';
      // Replace option text with descriptive labels
      select.options[0].textContent = '100% — Full load (all simulations)';
      select.options[1].textContent = '75% — 3/4 simulations';
      select.options[2].textContent = '50% — Half simulations';
      select.options[3].textContent = '25% — 1/4 simulations';
      select.options[4].textContent = '0% — No simulations (stay associated)';
      fieldGrid.appendChild(group);
      return;
    }
    const { group } = buildConfigInput({ section, key, type: PW_KEY_RE.test(key) ? 'password' : 'text' }, val);
    const lbl = group.querySelector('label');
    if (lbl) lbl.textContent = _fmtConfigKey(key);
    fieldGrid.appendChild(group);
  });

  if (textPairs.length) form.appendChild(fieldGrid);

  if (boolPairs.length) {
    const h3 = document.createElement('h3');
    h3.textContent = 'Flags';
    form.appendChild(h3);
    const grid = document.createElement('div');
    grid.className = 'toggle-grid';
    boolPairs.forEach(([key, val]) => grid.appendChild(buildConfigToggle({ section, key }, val)));
    form.appendChild(grid);
  }

  const actions = document.createElement('div');
  actions.className = 'form-actions';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = `Save [${section}] to GitHub`;
  actions.appendChild(saveBtn);
  form.appendChild(actions);

  const msg = document.createElement('div');
  msg.className = 'settings-message hidden';
  form.appendChild(msg);

  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const updates = collectSectionedConfigState(form)[section] || {};
      const result = await requestJson('/api/config/simulation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section, updates }),
      });
      showInlineMessage(msg, result?.pushed ? `[${section}] saved and pushed to GitHub.` : `[${section}] saved. GitHub push skipped.`, false, 7000);
      await loadConfigEditor(true);
    } catch (error) {
      showInlineMessage(msg, `Error: ${error.message}`, true, 7000);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = `Save [${section}] to GitHub`;
    }
  });

  card.appendChild(form);
  container.appendChild(card);
}

function renderSimulationConfigForm() {
  if (!configSimulationForm) return;
  configSimulationForm.textContent = '';

  const sections = Object.keys(configData).filter(
    s => !BUCKET_SECTION_RE.test(s) && !ADDRESS_SECTION_RE.test(s)
  );
  if (sections.length === 0) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = 'No configuration sections found — sync from GitHub first.';
    configSimulationForm.appendChild(p);
    return;
  }

  sections.forEach(section => _buildSectionCard(section, configData[section] || {}, configSimulationForm));
}

function renderAddressesForm() {
  if (!configAddressesForm) return;
  configAddressesForm.textContent = '';

  const sections = Object.keys(configData).filter(s => ADDRESS_SECTION_RE.test(s));
  if (sections.length === 0) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = 'No [server] or [address] sections found — sync from GitHub first.';
    configAddressesForm.appendChild(p);
    return;
  }

  sections.forEach(section => _buildSectionCard(section, configData[section] || {}, configAddressesForm));
}

function renderBucketEditors() {
  if (!configBucketsContainer) return;
  configBucketsContainer.textContent = '';

  const buckets = Object.keys(configData)
    .filter(s => BUCKET_SECTION_RE.test(s))
    .sort((a, b) => parseInt(a.slice(1), 10) - parseInt(b.slice(1), 10));

  if (buckets.length === 0) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = 'No bucket sections (s0–s9) found in simulation.conf.';
    configBucketsContainer.appendChild(p);
    return;
  }

  buckets.forEach((section, idx) => {
    const values = configData[section] || {};
    const details = document.createElement('details');
    details.className = 'setup-card setup-section-gap';
    if (idx === 0) details.open = true;

    const summary = document.createElement('summary');
    summary.textContent = buildBucketSummary(section, values);
    details.appendChild(summary);

    const body = document.createElement('div');
    body.className = 'setup-form';

    const tracked = { ...values };

    const fieldGrid = document.createElement('div');
    fieldGrid.className = 'config-field-grid';

    // Text inputs (preserve file order, skip booleans and sim_phy)
    Object.entries(values).forEach(([key, val]) => {
      if (key === 'sim_phy' || _isBoolVal(val)) return;
      const { group, input } = buildConfigInput(
        { section, key, type: PW_KEY_RE.test(key) ? 'password' : 'text' },
        val,
      );
      const lbl = group.querySelector('label');
      if (lbl) lbl.textContent = _fmtConfigKey(key);
      input.addEventListener('input', () => {
        tracked[key] = input.value.trim();
        summary.textContent = buildBucketSummary(section, tracked);
      });
      fieldGrid.appendChild(group);
    });

    // sim_phy select (if present)
    if ('sim_phy' in values) {
      const { group } = buildConfigSelect(section, 'sim_phy', HUB_SIM_SELECT_FIELDS.sim_phy, values.sim_phy || 'wireless');
      const lbl = group.querySelector('label');
      if (lbl) lbl.textContent = 'Sim Phy';
      fieldGrid.appendChild(group);
    }

    body.appendChild(fieldGrid);

    // Toggle flags
    const boolPairs = Object.entries(values).filter(([k, v]) => k !== 'sim_phy' && _isBoolVal(v));
    if (boolPairs.length) {
      const h3 = document.createElement('h3');
      h3.textContent = 'Flags';
      body.appendChild(h3);
      const grid = document.createElement('div');
      grid.className = 'toggle-grid';
      boolPairs.forEach(([key, val]) => grid.appendChild(buildConfigToggle({ section, key }, val)));
      body.appendChild(grid);
    }

    const actions = document.createElement('div');
    actions.className = 'form-actions';
    const saveButton = document.createElement('button');
    saveButton.type = 'button';
    saveButton.className = 'btn btn-primary';
    saveButton.textContent = 'Save Bucket';
    actions.appendChild(saveButton);
    body.appendChild(actions);

    const message = document.createElement('div');
    message.className = 'settings-message hidden';
    body.appendChild(message);

    saveButton.addEventListener('click', async () => {
      saveButton.disabled = true;
      saveButton.textContent = 'Saving…';
      try {
        const updates = collectSectionedConfigState(body)[section] || {};
        const result = await requestJson('/api/config/simulation', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ section, updates }),
        });
        showInlineMessage(message, result?.pushed ? `Saved ${section} and pushed to GitHub.` : `Saved ${section}. GitHub push skipped.`, false, 7000);
        await loadConfigEditor(true);
      } catch (error) {
        showInlineMessage(message, `Error: ${error.message}`, true, 7000);
      } finally {
        saveButton.disabled = false;
        saveButton.textContent = 'Save Bucket';
      }
    });

    details.appendChild(body);
    configBucketsContainer.appendChild(details);
  });
}

async function loadConfigEditor(force = false) {
  if (!force && configLoaded) return configData;
  try {
    const data = await requestJson('/api/config/parsed');
    configData = data || {};
    configLoaded = true;
    renderSimulationConfigForm();
    renderAddressesForm();
    renderBucketEditors();
    return configData;
  } catch (error) {
    configLoaded = false;
    showInlineMessage(configSimulationMsg, `Error: ${error.message}`, true, 7000);
    showInlineMessage(configBucketsMsg, `Error: ${error.message}`, true, 7000);
    throw error;
  }
}

// ── Command Inbox ─────────────────────────────────────────────────────────────

const cmdTarget    = document.getElementById('cmd-target');
const cmdAction    = document.getElementById('cmd-action');
const cmdSendBtn   = document.getElementById('cmd-send-btn');
const cmdClearBtn  = document.getElementById('cmd-clear-btn');
const cmdMsg       = document.getElementById('cmd-msg');
const cmdTbody     = document.getElementById('cmd-tbody');
const cmdEmpty     = document.getElementById('cmd-empty');

const CMD_STATUS_LABELS = {
  pending:   { text: 'Pending',   cls: 'badge-yellow' },
  delivered: { text: 'Delivered', cls: 'badge-blue' },
  completed: { text: 'Completed', cls: 'badge-green' },
  failed:    { text: 'Failed',    cls: 'badge-red' },
  expired:   { text: 'Expired',   cls: 'badge-grey' },
};

function proxmoxTargetLabel() {
  const hostname = String(latestProxmoxData?.hostname || latestProxmoxData?.node?.hostname || '').trim();
  return hostname || 'Proxmox Host';
}

function updateCmdTargetDropdown(clientList = [...clients.values()]) {
  if (!cmdTarget) return;
  const proxmoxOption = [...cmdTarget.options].find((option) => option.value === 'proxmox');
  if (proxmoxOption) proxmoxOption.textContent = proxmoxTargetLabel();
  [...cmdTarget.options].forEach((option) => {
    if (option.value !== 'all' && option.value !== 'proxmox') option.remove();
  });
  clientList.forEach((client) => {
    if (!client?.hostname) return;
    const option = document.createElement('option');
    option.value = client.hostname;
    option.textContent = client.hostname;
    cmdTarget.appendChild(option);
  });
}

// ── Command description helpers ───────────────────────────────────────────
function vmNameFromId(vmid) {
  if (!vmid) return null;
  const vms = (latestProxmoxData && latestProxmoxData.vms) || [];
  const found = vms.find((v) => String(v.vmid) === String(vmid));
  return found ? found.name : null;
}

const CMD_ACTION_LABELS = {
  restart_sim:          'Restarting simulation',
  reboot:               'Rebooting device',
  update_now:           'Forcing update',
  kill_switch:          'Kill switch',
  reclone_vms:          'Recloning all VMs',
  snapshot_vms:         'Snapshotting all VMs',
  start_vms:            'Starting all VMs',
  stop_vms:             'Stopping all VMs',
  reclone_vm:           'Recloning VM',
  delete_vm:            'Deleting VM',
  start_vm:             'Starting VM',
  stop_vm:              'Stopping VM',
  reboot_vm:            'Rebooting VM',
  snapshot_vm:          'Snapshotting VM',
  provision_unassigned: 'Provisioning unassigned dongles',
  update_agent:         'Updating Proxmox agent',
};

function cmdDescription(cmd) {
  const base = CMD_ACTION_LABELS[cmd.action] || cmd.action.replace(/_/g, ' ');
  const vmid = cmd.args && cmd.args.vmid;
  if (vmid) {
    const name = vmNameFromId(vmid);
    return name ? `${base}: ${name}` : `${base}: VM ${vmid}`;
  }
  return base;
}

function cmdTargetLabel(cmd) {
  const target = cmd.target || '—';
  const vmid = cmd.args && cmd.args.vmid;
  if (vmid) {
    const name = vmNameFromId(vmid);
    return name || `VM ${vmid}`;
  }
  if (target === 'all') return 'All Clients';
  if (target === 'proxmox') return proxmoxTargetLabel();
  return target;
}

function renderCommandTable(cmds) {
  if (!cmdTbody || !cmdEmpty) return;
  cmdTbody.innerHTML = '';
  if (!cmds || cmds.length === 0) {
    cmdEmpty.style.display = '';
    return;
  }
  cmdEmpty.style.display = 'none';
  [...cmds].reverse().forEach((cmd) => {
    const info = CMD_STATUS_LABELS[cmd.status] || { text: cmd.status, cls: 'badge-grey' };
    const age = cmd.age_secs != null ? `${Math.floor(cmd.age_secs / 60)}m ${cmd.age_secs % 60}s` : '—';
    const tr = document.createElement('tr');

    const targetTd = document.createElement('td');
    targetTd.textContent = cmdTargetLabel(cmd);
    tr.appendChild(targetTd);

    const actionTd = document.createElement('td');
    const code = document.createElement('code');
    code.textContent = cmd.action;
    actionTd.appendChild(code);
    tr.appendChild(actionTd);

    const descTd = document.createElement('td');
    descTd.textContent = cmdDescription(cmd);
    tr.appendChild(descTd);

    const statusTd = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `badge ${info.cls}`;
    badge.textContent = info.text;
    statusTd.appendChild(badge);
    tr.appendChild(statusTd);

    const ageTd = document.createElement('td');
    ageTd.textContent = age;
    tr.appendChild(ageTd);

    const messageTd = document.createElement('td');
    messageTd.style.maxWidth = '220px';
    messageTd.style.overflow = 'hidden';
    messageTd.style.textOverflow = 'ellipsis';
    messageTd.style.whiteSpace = 'nowrap';
    messageTd.textContent = cmd.message || '—';
    tr.appendChild(messageTd);

    const deleteTd = document.createElement('td');
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn-icon';
    deleteBtn.dataset.id = cmd.id;
    deleteBtn.title = 'Remove';
    deleteBtn.type = 'button';
    deleteBtn.textContent = '✕';
    deleteBtn.addEventListener('click', async (event) => {
      try {
        await fetch(`/api/commands/${event.currentTarget.dataset.id}`, { method: 'DELETE' });
      } catch (_) { /* silent */ }
    });
    deleteTd.appendChild(deleteBtn);
    tr.appendChild(deleteTd);

    cmdTbody.appendChild(tr);
  });
}

if (cmdSendBtn) {
  cmdSendBtn.addEventListener('click', async () => {
    const target = cmdTarget?.value || '';
    const action = cmdAction?.value || '';
    if (cmdMsg) {
      cmdMsg.textContent = '';
      cmdMsg.className = 'form-msg';
    }
    try {
      const data = await requestJson('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, action })
      });
      if (cmdMsg) {
        cmdMsg.textContent = `✓ Queued ${data.queued} command(s)`;
        cmdMsg.classList.add('msg-ok');
      }
    } catch (err) {
      if (cmdMsg) {
        cmdMsg.textContent = `✗ ${err.message}`;
        cmdMsg.classList.add('msg-error');
      }
    }
  });
}

if (cmdClearBtn) {
  cmdClearBtn.addEventListener('click', async () => {
    const ids = [...cmdTbody.querySelectorAll('[data-id]')].map((button) => button.dataset.id);
    await Promise.all(ids.map((id) => fetch(`/api/commands/${id}`, { method: 'DELETE' })));
  });
}

requestJson('/api/commands').then((commands) => {
  window._lastCommands = commands || [];
  renderCommandTable(commands);
}).catch(() => {});

async function renderServiceStatus() {
  const tbody = document.getElementById('services-tbody');
  if (!tbody) return;
  try {
    const data = await requestJson('/api/services/status');
    const tasks = data.tasks || {};
    const names = data.task_names || Object.keys(tasks);

    const LABELS = {
      sync_repo: 'Repo Sync',
      heartbeat: 'Heartbeat Check',
      central_token: 'Aruba Central Token',
      central_poller: 'Aruba Central Poller',
      update_checker: 'Update Checker',
      relay: 'Hub Loop',
      client_history_saver: 'Client History Save',
      command_expiry: 'Command Expiry',
      auto_recovery: 'Auto Recovery',
      schedule_check: 'Schedule Check',
      gkill_switch: 'Global Kill Switch',
      baseline_saver: 'Baseline Saver',
    };

    const rows = names.map((name) => {
      const t = tasks[name] || {};
      const status = t.status || 'pending';
      const dot = status === 'ok' ? '🟢' : status === 'error' ? '🔴' : status === 'warning' ? '🟡' : '⚪';
      const lastRun = t.last_run ? new Date(t.last_run).toLocaleTimeString() : '—';
      const runCount = t.run_count ?? '—';
      const consec = t.consecutive_errors || 0;
      const errorText = String(t.last_error_msg || '');
      const errMsg = errorText
        ? `<span title="${escHtml(errorText)}" style="color:var(--hpe-red);cursor:help">${escHtml(errorText.substring(0, 60))}${errorText.length > 60 ? '…' : ''}</span>`
        : '—';
      const label = LABELS[name] || name;
      return `<tr>
        <td>${label}</td>
        <td>${dot} ${status}</td>
        <td>${lastRun}</td>
        <td>${runCount}</td>
        <td>${consec > 0 ? `<span style="color:var(--hpe-red)">${consec}</span>` : '0'}</td>
        <td>${errMsg}</td>
      </tr>`;
    });

    tbody.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="6" class="empty-msg">No service data yet</td></tr>';
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-msg">Failed to load: ${escHtml(e.message || String(e))}</td></tr>`;
  }
}

function handleMessage(message) {
  if (document.getElementById('tab-api-server')?.classList.contains('active')) {
    renderServiceStatus().catch(() => {});
  }

  if (message.type === 'full_state') {
    (message.clients || []).forEach((client) => upsertClient(client));
    updateCmdTargetDropdown(message.clients || []);
    return;
  }

  if (message.type === 'repo_status') {
    setRepoStatus(message.synced, message.error, message.last_sync, message.repo_version);
    return;
  }

  if (message.type === 'relay_status') {
    setRelayStatus(message);
    return;
  }

  if (message.type === 'proxmox_update') {
    if (message.webui_vmid != null) webuiVmid = message.webui_vmid;
    if (message.pending_proxmox !== undefined) renderProxmoxPending(message.pending_proxmox || []);
    if (message.approved_proxmox !== undefined) renderProxmoxApproved(message.approved_proxmox || []);
    renderServerTab(message);
    return;
  }

  if (message.type === 'proxmox_pending_update') {
    renderProxmoxPending(message.pending || []);
    return;
  }

  if (message.type === 'reclone_update') {
    const previousStatus = latestRecloneState?.status;
    renderRecloneStatus(message);
    if (previousStatus === 'running' && message.status && message.status !== 'running') {
      scheduleProxmoxRefresh(1000);
    }
    return;
  }

  if (message.type === 'version_status') {
    applyVersionStatus(message);
    return;
  }

  if (message.type === 'update_all_progress') {
    handleUpdateAllProgress(message);
    return;
  }

  if (message.type === 'settings_update') {
    applySettingsToUI(message.settings);
    return;
  }

  if (message.type === 'central_update') {
    handleCentralUpdate(message.status, message.ts, message.wireless_clients, message.hardware_alerts, message.client_count_status);
    if (message.token_state) {
      const ts = message.token_state;
      setCentralApiStatus(ts.state === 'connected', ts);
    }
    return;
  }

  if (message.type === 'proxmox_log_update') {
    if (message.cleared) {
      agentLogLines = [];
    } else if (message.lines && message.lines.length) {
      appendAgentLogLines(message.lines);
    }
    return;
  }

  if (message.type === 'commands_update') {
    // Surface failures as toasts so the user knows something went wrong
    const prev = new Map((window._lastCommands || []).map((c) => [c.id, c.status]));
    (message.commands || []).forEach((c) => {
      if (c.status === 'failed' && prev.get(c.id) !== 'failed') {
        const label = c.action ? c.action.replace(/_/g, ' ') : 'command';
        const vmNote = c.args?.vmid ? ` (VM ${c.args.vmid})` : '';
        showToast(`⚠ ${label}${vmNote} failed — check Proxmox agent log`, 'error');
      }
    });
    window._lastCommands = message.commands || [];
    renderCommandTable(message.commands);
    return;
  }

  if (message.type === 'cert_renewed') {
    showToast(`TLS certificate renewed — expires ${message.expires || 'unknown'}`, 'success');
    loadSpokeAcmeSettings().catch(() => {});
    return;
  }

  if (message.type === 'acme_status') {
    if (!message.running) pollSpokeAcmeStatus();
    return;
  }

  if (message.type === 'notification') {
    if (cmdMsg && message.message) {
      cmdMsg.textContent = message.message;
      cmdMsg.className = 'form-msg';
      cmdMsg.classList.add(message.level === 'warning' ? 'msg-error' : 'msg-ok');
    }
    return;
  }

  if (message.type === 'gkill_switch_update') {
    applyGkillSwitch(message.value);
    return;
  }

  if (['status_update', 'overrides_update', 'overrides_cleared'].includes(message.type) && message.client) {
    upsertClient(message.client);
    updateCmdTargetDropdown();
    return;
  }

  if (message.type === 'clients_purged') {
    clients.clear();
    document.querySelectorAll('#clients-body tr:not(#empty-row)').forEach(r => r.remove());
    const emptyRow = document.getElementById('empty-row');
    if (emptyRow) emptyRow.classList.remove('hidden');
    syncSpokeClientTypeFilter();
    updateCmdTargetDropdown([]);
  }
}

function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const spokeWsToken = (typeof window.__SPOKE_WS_TOKEN__ !== 'undefined' && window.__SPOKE_WS_TOKEN__)
    ? `?token=${encodeURIComponent(window.__SPOKE_WS_TOKEN__)}` : '';
  socket = new WebSocket(`${protocol}://${window.location.host}/ws${spokeWsToken}`);
  setWsStatus(false, 'Connecting');

  socket.addEventListener('open', () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    setWsStatus(true, 'Connected');
    // Fetch initial repo status via HTTP in case WS message races or was missed
    requestJson('/api/repo/status').then(d => {
      setRepoStatus(d.synced, d.error, d.last_sync, d.repo_version);
    }).catch(() => {});
    // If we showed "restarting" during an update, confirm success on reconnect
    if (updateMsg && updateMsg.textContent.includes('restarting')) {
      updateMsg.textContent = '✅ Update complete — service restarted successfully.';
      updateMsg.className = 'settings-message success';
      updateMsg.classList.remove('hidden');
      clearTimeout(updateMsg._timer);
      updateMsg._timer = setTimeout(() => { updateMsg.classList.add('hidden'); }, 10000);
    }
  });

  socket.addEventListener('message', (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (error) {
      console.error('Invalid WS message', error);
    }
  });

  socket.addEventListener('close', () => {
    setWsStatus(false, 'Disconnected');
    // If an update was running, the service is restarting — don't show an error
    if (updateWasInProgress && updateMsg) {
      updateWasInProgress = false;
      updateMsg.textContent = '🔄 Service restarting — reconnecting…';
      updateMsg.className = 'settings-message success';
      updateMsg.classList.remove('hidden');
    }
    if (!reconnectTimer) {
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectWebSocket();
      }, 5000);
    }
  });

  socket.addEventListener('error', () => {
    socket.close();
  });
}

// ── Simulations top-level tabs ──────────────────────────────────────────
const simTopPanels = ['simtop-checks', 'simtop-hardware', 'simtop-clients'];

function activateSimTopTab(tabId = 'simtop-checks') {
  document.querySelectorAll('.simtop-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.simtop === tabId);
  });
  simTopPanels.forEach((id) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    panel.classList.toggle('active', id === tabId);
    panel.classList.toggle('hidden', id !== tabId);
  });
  if (tabId === 'simtop-hardware') renderHwPanel();
  if (tabId === 'simtop-clients') renderCcPanel();
}

document.querySelectorAll('.simtop-subtab').forEach((btn) => {
  btn.addEventListener('click', () => activateSimTopTab(btn.dataset.simtop));
});

// ── Simulations tab ───────────────────────────────────────────────
const simChecksList   = document.getElementById('sim-checks-list');
const simEmpty        = document.getElementById('sim-checks-empty');
const simOverview     = document.getElementById('sim-overview');
const simDetail       = document.getElementById('sim-detail');
const simDetailBack   = document.getElementById('sim-detail-back');
const simDetailTitle  = document.getElementById('sim-detail-title');
const simDetailSub    = document.getElementById('sim-detail-sub');
const simDetailBadge  = document.getElementById('sim-detail-badge');
const simLastRefreshed = document.getElementById('sim-last-refreshed');
const simRefreshBtn   = document.getElementById('sim-refresh-btn');
const simClientsPanel  = document.getElementById('sim-clients-panel');
const simClientsBack   = document.getElementById('sim-clients-back');
const simClientsTitle  = document.getElementById('sim-clients-title');
const simClientsSub    = document.getElementById('sim-clients-sub');
const simClientsBadge  = document.getElementById('sim-clients-central-badge');
const simClientsList   = document.getElementById('sim-clients-list');
const hwDetailPanel  = document.getElementById('hw-detail');
const hwDetailBack   = document.getElementById('hw-detail-back');
const hwDetailTitle  = document.getElementById('hw-detail-title');
const hwDetailSub    = document.getElementById('hw-detail-sub');
const hwDetailBadge  = document.getElementById('hw-detail-badge');
const hwSiteList     = document.getElementById('hw-site-list');
const ccDetailPanel  = document.getElementById('cc-detail');
const ccDetailBack   = document.getElementById('cc-detail-back');
const ccDetailTitle  = document.getElementById('cc-detail-title');
const ccDetailSub    = document.getElementById('cc-detail-sub');
const ccDetailBadge  = document.getElementById('cc-detail-badge');
const ccSiteDetail   = document.getElementById('cc-site-detail');

let simulationsData = [];
let openSimId = null;   // key into getSimGroups() map

function simStatusBadge(pf) {
  if (!pf) return { label: 'No Check', cls: 'sim-unknown' };
  if (pf.firing) return { label: '✓ Firing', cls: 'sim-pass' };
  return { label: '✗ Not Firing', cls: 'sim-fail' };
}

// Canonical order and labels for simulation test types
const SIM_TEST_ORDER = [
  'dns_fail', 'assoc_fail', 'dhcp_fail', 'port_flap',
  'iperf', 'www_traffic', 'download', 'ping_test',
  'ssidpw_fail', 'auth_fail',
];
const SIM_TEST_LABELS = {
  dns_fail:    'DNS Fail',
  assoc_fail:  'Association Fail',
  dhcp_fail:   'DHCP Fail',
  port_flap:   'Port Flap',
  iperf:       'iPerf',
  www_traffic: 'Web Traffic',
  download:    'Download',
  ping_test:   'Ping Test',
  ssidpw_fail: 'Bad SSID Password',
  auth_fail:   'Auth Fail',
};

/** Build a map of testKey → { label, sims[], aggLabel, aggCls }
 *  One tile per simulation type (test flag) that is enabled in at least one bucket. */
function getSimGroups() {
  const groups = new Map();

  for (const testKey of SIM_TEST_ORDER) {
    for (const sim of simulationsData) {
      const tests = sim.tests || {};
      if (!tests[testKey]) continue;
      if (!groups.has(testKey)) {
        groups.set(testKey, {
          key: testKey,
          label: SIM_TEST_LABELS[testKey] || testKey,
          sims: [],
        });
      }
      groups.get(testKey).sims.push(sim);
    }
  }

  // Compute aggregate Central firing status per group
  for (const group of groups.values()) {
    let anyFiring = false, anyFail = false, anyConfigured = false;
    for (const sim of group.sims) {
      const pf = sim.central_pass_fail;
      if (pf) { anyConfigured = true; if (pf.firing) anyFiring = true; else anyFail = true; }
    }
    if (!anyConfigured) {
      group.aggLabel = 'No Check'; group.aggCls = 'sim-unknown';
    } else if (anyFiring && !anyFail) {
      group.aggLabel = '✓ Firing'; group.aggCls = 'sim-pass';
    } else if (anyFail && !anyFiring) {
      group.aggLabel = '✗ Not Firing'; group.aggCls = 'sim-fail';
    } else {
      group.aggLabel = '⚠ Partial'; group.aggCls = 'sim-warn';
    }
  }
  return groups;
}

/** Build client rows into a container element */
function buildClientRows(sim, container) {
  container.textContent = '';
  const clients = sim.configured_clients || [];
  if (!clients.length) {
    const empty = document.createElement('div');
    empty.className = 'sim-client-row';
    empty.textContent = 'No clients configured.';
    container.appendChild(empty);
    return;
  }
  clients.forEach((c) => {
    const row = document.createElement('div');
    const statusCls = c.online ? 'online' : c.reporting ? 'offline' : 'not-reporting';
    row.className = `sim-client-row ${statusCls}`;

    const hostname = document.createElement('span');
    hostname.className = 'sim-client-hostname';
    hostname.textContent = c.hostname;

    const statusSpan = document.createElement('span');
    statusSpan.className = 'sim-client-status';
    if (c.online) {
      statusSpan.textContent = '● Online'; statusSpan.style.color = 'var(--hpe-green-dark)';
    } else if (c.reporting) {
      statusSpan.textContent = '○ Offline'; statusSpan.style.color = '#999';
    } else {
      statusSpan.textContent = '⚠ Not Reporting'; statusSpan.style.color = '#e67e22';
    }

    const lastSeen = document.createElement('span');
    lastSeen.className = 'sim-client-lastseen';
    if (c.last_seen) {
      const ago = Math.round((Date.now() - new Date(c.last_seen).getTime()) / 60000);
      lastSeen.textContent = ago < 2 ? 'just now' : `${ago}m ago`;
    } else {
      lastSeen.textContent = 'never seen';
    }

    row.appendChild(hostname);
    row.appendChild(statusSpan);
    row.appendChild(lastSeen);
    container.appendChild(row);
  });
}

function formatClientCountDelta(dropPct) {
  if (!Number.isFinite(dropPct) || Math.abs(dropPct) < 0.05) return '0.0%';
  return dropPct > 0
    ? `-${dropPct.toFixed(1)}%`
    : `+${Math.abs(dropPct).toFixed(1)}%`;
}

function renderChecksList() {
  const list = simChecksList;
  const emptyEl = simEmpty;
  const filterInput = document.getElementById('checks-filter');
  const countBadge = document.getElementById('checks-count');
  if (!list) return;

  const filterText = filterInput ? filterInput.value.trim().toLowerCase() : '';

  list.textContent = '';
  if (emptyEl) {
    emptyEl.textContent = 'No checks configured — sync simulation.conf and configure hardware alerts.';
    emptyEl.classList.add('hidden');
    list.appendChild(emptyEl);
  }

  const groups = getSimGroups();

  const simRows = [];
  for (const [key, group] of groups) {
    const dotCls = group.aggCls === 'sim-pass' ? 'dot-ok'
      : group.aggCls === 'sim-fail' ? 'dot-err'
      : group.aggCls === 'sim-warn' ? 'dot-warn' : 'dot-unknown';
    const sites = [...new Set(group.sims.map((s) => s.wsite).filter(Boolean))];
    let latestTs = null;
    for (const sim of group.sims) {
      const pf = sim.central_pass_fail;
      if (pf && pf.ts && (!latestTs || pf.ts > latestTs)) latestTs = pf.ts;
    }
    simRows.push({
      key,
      label: group.label,
      dotCls,
      badge: 'SIM',
      badgeCls: 'check-badge-sim',
      detail: sites.length ? sites.join(' · ') : '— no sites',
      ts: latestTs,
      priority: dotCls === 'dot-err' ? 0 : dotCls === 'dot-warn' ? 1 : dotCls === 'dot-ok' ? 2 : 3,
      onClick: () => openSimGroup(key),
    });
  }
  simRows.sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label));

  const hwRows = [];
  for (const hw of hwAlertsData) {
    const affected = hw.total || 0;
    const dotCls = affected > 0 ? 'dot-err' : 'dot-ok';
    const siteNames = Object.values(hw.sites || {}).map((s) => s.site_name || '').filter(Boolean);
    hwRows.push({
      key: hw.id,
      label: hw.name || hw.id,
      dotCls,
      badge: (hw.device_type || 'HW').toUpperCase(),
      badgeCls: 'check-badge-hw',
      detail: affected > 0
        ? `${affected} device${affected !== 1 ? 's' : ''} affected${siteNames.length ? ` — ${siteNames.slice(0, 3).join(', ')}` : ''}`
        : 'No active alerts',
      ts: null,
      priority: affected > 0 ? 0 : 2,
      onClick: () => openHwDetail(hw.id),
    });
  }
  hwRows.sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label));
  _hwRowsCache = hwRows;
  const ccRows = [];
  for (const [wsite, info] of Object.entries(clientCountData)) {
    const degraded = info.status === 'DEGRADED';
    const noData = info.status === 'NO_DATA';
    const stale = info.baseline_stale;
    const dotCls = noData ? 'dot-unknown' : degraded ? 'dot-err' : 'dot-ok';
    const staleLabel = stale
      ? ` ⏱ last baseline ${info.baseline_recorded_at ? new Date(info.baseline_recorded_at * 1000).toLocaleTimeString() : 'saved'}`
      : '';
    ccRows.push({
      key: wsite,
      label: info.site_name || wsite,
      dotCls,
      badge: 'CC',
      badgeCls: 'check-badge-cc',
      detail: noData
        ? 'Collecting baseline…'
        : `Current: ${info.current} / Avg: ${Math.round(info.hourly_avg)} (${formatClientCountDelta(info.drop_pct)})${staleLabel}`,
      ts: info.ts,
      priority: degraded ? 0 : noData ? 3 : 2,
      onClick: () => openCcDetail(wsite),
    });
  }
  ccRows.sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label));
  _ccRowsCache = ccRows;

  // ── Monitored Central checks (alerts / insights from Settings) ────────────
  const monRows = [];
  const monChecks = currentSettings.monitored_checks || [];
  for (const mc of monChecks) {
    const checkId = mc.id;
    const checkName = mc.name || checkId;
    const checkType = (mc.type || 'alert').toUpperCase().slice(0, 3); // ALT / INS
    let anyOk = false, anyFail = false, latestTs = null;
    const firingAt = [], missingAt = [];
    for (const [wsite, checks] of Object.entries(centralStatusData)) {
      if (!(checkId in checks)) continue;
      const info = checks[checkId];
      if (info.status === 'OK') { anyOk = true; firingAt.push(wsite); }
      else { anyFail = true; missingAt.push(wsite); }
      if (info.ts && (!latestTs || info.ts > latestTs)) latestTs = info.ts;
    }
    const hasData = anyOk || anyFail;
    const dotCls = !hasData ? 'dot-unknown' : anyFail ? 'dot-err' : 'dot-ok';
    const detail = !hasData
      ? 'Not yet polled'
      : anyFail && !anyOk
        ? `Not detected at: ${missingAt.join(', ')}`
        : anyOk && !anyFail
          ? `Detected at: ${firingAt.join(', ')}`
          : `Partial — OK: ${firingAt.join(', ')} · Missing: ${missingAt.join(', ')}`;
    monRows.push({
      key: `mon-${checkId}`,
      label: checkName,
      dotCls,
      badge: checkType,
      badgeCls: 'check-badge-mon',
      detail,
      ts: latestTs,
      priority: dotCls === 'dot-err' ? 0 : dotCls === 'dot-warn' ? 1 : dotCls === 'dot-ok' ? 2 : 3,
      onClick: () => {},  // no drill-down for now
    });
  }
  monRows.sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label));

  // Tab count badges reflect only Checks-tab rows (SIM + MON)
  const allRowsFlat = [...simRows, ...monRows];
  let failCount = 0, funcCount = 0, warnCount = 0;
  for (const item of allRowsFlat) {
    item.effectiveTab = getEffectiveTabForItem(item);
    if (item.effectiveTab === 'failing') failCount++;
    else if (item.effectiveTab === 'functional') funcCount++;
    else warnCount++;
  }
  // Also include hw/cc in totals for the badge display
  const allHwCc = [...hwRows, ...ccRows];
  for (const item of allHwCc) {
    item.effectiveTab = getEffectiveTabForItem(item);
    if (item.effectiveTab === 'failing') failCount++;
    else if (item.effectiveTab === 'functional') funcCount++;
    else warnCount++;
  }
  const elFail = document.getElementById('sim-tab-failing-count');
  const elFunc = document.getElementById('sim-tab-functional-count');
  const elWarn = document.getElementById('sim-tab-warning-count');
  if (elFail) elFail.textContent = failCount;
  if (elFunc) elFunc.textContent = funcCount;
  if (elWarn) elWarn.textContent = warnCount;

  const tabTotal = activeSimTab === 'failing' ? failCount : activeSimTab === 'functional' ? funcCount : warnCount;
  const totalChecksAll = simRows.length + hwRows.length + ccRows.length + monRows.length;  if (countBadge) countBadge.textContent = `${tabTotal} of ${totalChecksAll} check${totalChecksAll !== 1 ? 's' : ''}`;

  if (!totalChecksAll) {
    if (emptyEl) emptyEl.classList.remove('hidden');
    return;
  }

  function makeRow(item) {
    // Filter by active sub-tab
    if (item.effectiveTab !== activeSimTab) return null;

    const matchesFilter = !filterText
      || item.label.toLowerCase().includes(filterText)
      || item.detail.toLowerCase().includes(filterText);
    if (!matchesFilter) return null;

    const row = document.createElement('div');
    row.className = 'check-row';
    row.dataset.key = item.key;

    const dot = document.createElement('span');
    dot.className = `check-dot ${item.dotCls}`;

    const name = document.createElement('span');
    name.className = 'check-name';
    name.textContent = item.label;

    const badge = document.createElement('span');
    badge.className = `check-badge ${item.badgeCls}`;
    badge.textContent = item.badge;

    const detail = document.createElement('span');
    detail.className = 'check-detail';
    detail.textContent = item.detail;

    const tsEl = document.createElement('span');
    tsEl.className = 'check-ts';
    if (item.ts) {
      const ago = Math.round((Date.now() / 1000 - item.ts) / 60);
      tsEl.textContent = ago < 2 ? 'just now' : `${ago}m ago`;
    } else {
      tsEl.textContent = '—';
    }

    row.appendChild(dot);
    row.appendChild(name);
    row.appendChild(badge);
    row.appendChild(detail);
    row.appendChild(tsEl);
    row.addEventListener('click', item.onClick);
    return row;
  }

  function appendSection(title, rows) {
    const visibleRows = rows.map(makeRow).filter(Boolean);
    if (!visibleRows.length) return;
    const hdr = document.createElement('div');
    hdr.className = 'checks-section-header';
    hdr.textContent = title;
    list.appendChild(hdr);
    visibleRows.forEach((row) => list.appendChild(row));
  }

  appendSection('Simulation Checks', simRows);
  appendSection('Monitored Central Checks', monRows);

  // HW and CC rows live in their own top-level tabs — trigger re-render if visible
  const hwPanel = document.getElementById('simtop-hardware');
  if (hwPanel && !hwPanel.classList.contains('hidden')) renderHwPanel();
  const ccPanel = document.getElementById('simtop-clients');
  if (ccPanel && !ccPanel.classList.contains('hidden')) renderCcPanel();

  const visibleCount = list.querySelectorAll('.check-row').length;
  if (!visibleCount && emptyEl) {
    const tabLabel = activeSimTab === 'failing' ? 'failing' : activeSimTab === 'functional' ? 'functional' : 'warning';
    emptyEl.textContent = filterText
      ? `No ${tabLabel} checks match the current filter.`
      : `No ${tabLabel} checks.`;
    emptyEl.classList.remove('hidden');
  }
}

function openSimGroup(key) {
  const groups = getSimGroups();
  const group = groups.get(key);
  if (!group || !simOverview || !simDetail) return;
  openSimId = key;
  simOverview.classList.add('hidden');
  simDetail.classList.remove('hidden');

  if (simDetailTitle) simDetailTitle.textContent = group.label;
  if (simDetailSub) {
    const uniqueSites = [...new Set(group.sims.map(s => s.wsite).filter(Boolean))];
    simDetailSub.textContent = uniqueSites.length
      ? `Running at: ${uniqueSites.join(', ')}`
      : 'No sites configured';
  }
  if (simDetailBadge) {
    simDetailBadge.textContent = group.aggLabel;
    simDetailBadge.className = `sim-status-badge ${group.aggCls}`;
  }

  const siteList = document.getElementById('sim-site-list');
  if (!siteList) return;
  siteList.textContent = '';

  // Aggregate buckets by site — user only cares about sites + reporting count
  const siteMap = new Map();
  for (const sim of group.sims) {
    const site = sim.wsite || '(no site)';
    if (!siteMap.has(site)) {
      siteMap.set(site, { active: 0, centralPf: null, simId: sim.id });
    }
    const entry = siteMap.get(site);
    entry.active += sim.active_client_count || 0;
    // Use Central pass/fail if any bucket at this site has it configured
    if (sim.central_pass_fail && !entry.centralPf) entry.centralPf = sim.central_pass_fail;
  }

  for (const [site, { active, centralPf, simId }] of siteMap) {
    const { label, cls } = simStatusBadge(centralPf);

    const siteRow = document.createElement('div');
    siteRow.className = 'sim-site-row';
    siteRow.style.cursor = 'pointer';
    siteRow.title = 'Click to see clients at this site';

    const siteName = document.createElement('span');
    siteName.className = 'sim-site-name';
    siteName.textContent = site;

    const siteCount = document.createElement('span');
    siteCount.className = 'sim-site-count';
    siteCount.textContent = `${active} reporting`;

    const siteBadge = document.createElement('span');
    siteBadge.className = `sim-status-badge ${cls}`;
    siteBadge.textContent = label;

    const arrow = document.createElement('span');
    arrow.style.cssText = 'margin-left:auto;color:var(--muted);font-size:0.85rem;';
    arrow.textContent = '›';

    siteRow.appendChild(siteName);
    siteRow.appendChild(siteCount);
    siteRow.appendChild(siteBadge);
    siteRow.appendChild(arrow);
    siteRow.addEventListener('click', () => openSimClients(simId, site, group.key, centralPf, group.label));
    siteList.appendChild(siteRow);
  }
}

async function openSimClients(simId, wsite, testKey, alertPf, checkLabel) {
  if (!simClientsPanel || !simDetail) return;
  simDetail.classList.add('hidden');
  simClientsPanel.classList.remove('hidden');

  if (simClientsTitle) simClientsTitle.textContent = checkLabel || 'Clients';
  if (simClientsSub)  simClientsSub.textContent  = `Site: ${wsite}`;
  if (simClientsList) simClientsList.innerHTML = '<div class="sim-clients-loading">Loading…</div>';

  // Alert polarity: alert PRESENT in Central = GREEN (sim is working)
  const alertMonitored = alertPf !== null && alertPf !== undefined;
  const alertFiring    = alertMonitored && alertPf.firing === true;

  try {
    const data = await requestJson(`/api/simulations/${encodeURIComponent(simId)}/clients`);
    const clientList = data.clients || [];
    if (!simClientsList) return;
    simClientsList.textContent = '';

    if (!clientList.length) {
      simClientsList.innerHTML = '<div class="sim-client-card" style="color:var(--muted)">No clients configured for this simulation.</div>';
      return;
    }

    for (const c of clientList) {
      const card = document.createElement('div');
      card.className = 'sim-client-card';

      // Hostname
      const hostname = document.createElement('span');
      hostname.className = 'sim-client-card-hostname';
      hostname.textContent = c.hostname;

      // Last seen
      const lastSeen = document.createElement('span');
      lastSeen.style.cssText = 'font-size:0.78rem;color:var(--muted);';
      if (c.api_last_seen) {
        const ago = Math.round((Date.now() - new Date(c.api_last_seen).getTime()) / 60000);
        lastSeen.textContent = ago < 2 ? 'just now' : `${ago}m ago`;
      } else {
        lastSeen.textContent = 'never seen';
      }

      // Indicators container
      const indicators = document.createElement('div');
      indicators.className = 'sim-client-indicators';

      // --- Icon 1: SIM RUNNING ---
      const activeSims = Array.isArray(c.active_simulations) ? c.active_simulations : [];
      const simRunning = activeSims.includes(testKey);
      const simInd = document.createElement('div');
      simInd.className = 'sim-client-indicator';
      const simDot = document.createElement('span');
      simDot.className = `ind-dot ${simRunning ? 'green' : c.api_online ? 'yellow' : 'grey'}`;
      const simLabel = document.createElement('span');
      simLabel.className = 'ind-label';
      simLabel.textContent = 'SIM';
      simInd.title = simRunning ? 'Simulation running'
                   : c.api_online ? 'Online — sim not active'
                   : 'Client offline';
      simInd.appendChild(simDot);
      simInd.appendChild(simLabel);

      // --- Icon 2: ALERT / INSIGHT ---
      const alertInd = document.createElement('div');
      alertInd.className = 'sim-client-indicator';
      const alertDot = document.createElement('span');
      const alertLabelEl = document.createElement('span');
      alertLabelEl.className = 'ind-label';
      alertLabelEl.textContent = 'ALERT';
      if (!alertMonitored) {
        alertDot.className = 'ind-dot unknown';
        alertLabelEl.style.color = 'var(--muted)';
        alertLabelEl.textContent = 'N/A';
        alertInd.title = 'No Central check configured for this simulation';
      } else if (alertFiring) {
        alertDot.className = 'ind-dot green';
        alertInd.title = `Alert detected in Central: ${alertPf.check_name || testKey}`;
      } else {
        alertDot.className = 'ind-dot red';
        alertInd.title = `Alert NOT seen in Central: ${alertPf.check_name || testKey}`;
      }
      alertInd.appendChild(alertDot);
      alertInd.appendChild(alertLabelEl);

      indicators.appendChild(simInd);
      indicators.appendChild(alertInd);

      card.appendChild(hostname);
      card.appendChild(lastSeen);
      card.appendChild(indicators);
      simClientsList.appendChild(card);
    }
  } catch (err) {
    if (simClientsList) simClientsList.innerHTML = `<div class="sim-client-card" style="color:#e74c3c">Error loading clients: ${escHtml(err.message || String(err))}</div>`;
  }
}

function closeSimDetail() {
  openSimId = null;
  if (simDetail) simDetail.classList.add('hidden');
  if (simOverview) simOverview.classList.remove('hidden');
}

// ── Hardware panel renderer ───────────────────────────────────────────────
function renderHwPanel() {
  const container = document.getElementById('hw-checks-list');
  if (!container) return;
  container.textContent = '';
  const rows = _hwRowsCache;
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'central-empty';
    empty.textContent = 'No hardware alerts configured.';
    container.appendChild(empty);
    return;
  }
  for (const item of rows) {
    const row = document.createElement('div');
    row.className = 'check-row';
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.innerHTML = `
      <span class="check-dot ${item.dotCls}"></span>
      <span class="check-label">${escHtml(item.label || '')}</span>
      <span class="check-badge ${item.badgeCls}">${escHtml(item.badge || '')}</span>
      <span class="check-detail">${escHtml(item.detail || '')}</span>
      <span class="check-ts">${item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : ''}</span>
    `;
    row.addEventListener('click', item.onClick);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') item.onClick(); });
    container.appendChild(row);
  }
}

// ── Client Count panel renderer ───────────────────────────────────────────
function renderCcPanel() {
  const container = document.getElementById('cc-checks-list');
  if (!container) return;
  container.textContent = '';
  const rows = _ccRowsCache;
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'central-empty';
    empty.textContent = 'No client count data yet.';
    container.appendChild(empty);
    return;
  }
  for (const item of rows) {
    const row = document.createElement('div');
    row.className = 'check-row';
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.innerHTML = `
      <span class="check-dot ${item.dotCls}"></span>
      <span class="check-label">${escHtml(item.label || '')}</span>
      <span class="check-badge ${item.badgeCls}">${escHtml(item.badge || '')}</span>
      <span class="check-detail">${escHtml(item.detail || '')}</span>
      <span class="check-ts">${item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : ''}</span>
    `;
    row.addEventListener('click', item.onClick);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') item.onClick(); });
    container.appendChild(row);
  }
}

function openHwDetail(checkId) {
  const hw = hwAlertsData.find((item) => item.id === checkId);
  if (!hw || !hwDetailPanel) return;
  const hwOverview = document.getElementById('hw-overview');
  if (hwOverview) hwOverview.classList.add('hidden');
  hwDetailPanel.classList.remove('hidden');

  if (hwDetailTitle) hwDetailTitle.textContent = hw.name || hw.id;
  const totalDevices = hw.total || 0;
  if (hwDetailSub) hwDetailSub.textContent = totalDevices > 0 ? `${totalDevices} device(s) affected` : 'No active alerts';
  if (hwDetailBadge) {
    hwDetailBadge.textContent = totalDevices > 0 ? `${totalDevices} DOWN` : '✓ Clear';
    hwDetailBadge.className = `sim-status-badge ${totalDevices > 0 ? 'sim-fail' : 'sim-pass'}`;
  }

  if (!hwSiteList) return;
  hwSiteList.textContent = '';
  const sites = Object.entries(hw.sites || {});
  if (!sites.length) {
    const empty = document.createElement('div');
    empty.className = 'sim-site-row';
    empty.textContent = 'No sites with active alerts.';
    hwSiteList.appendChild(empty);
    return;
  }
  for (const [wsite, info] of sites) {
    const row = document.createElement('div');
    row.className = 'sim-site-row';
    row.style.flexDirection = 'column';
    row.style.gap = '6px';
    row.style.alignItems = 'flex-start';
    row.style.cursor = 'default';

    const top = document.createElement('div');
    top.style.cssText = 'display:flex;justify-content:space-between;width:100%;align-items:center;';
    const siteName = document.createElement('span');
    siteName.className = 'sim-site-name';
    siteName.textContent = info.site_name || wsite;
    const siteBadge = document.createElement('span');
    siteBadge.className = 'sim-status-badge sim-fail';
    siteBadge.textContent = `${(info.devices || []).length} device(s)`;
    top.appendChild(siteName);
    top.appendChild(siteBadge);

    const deviceList = document.createElement('ul');
    deviceList.style.cssText = 'margin:0;padding-left:1.2rem;font-size:0.82rem;color:var(--muted);';
    for (const dev of (info.devices || [])) {
      const li = document.createElement('li');
      li.textContent = dev;
      deviceList.appendChild(li);
    }

    row.appendChild(top);
    row.appendChild(deviceList);
    hwSiteList.appendChild(row);
  }
}

function closeHwDetail() {
  if (hwDetailPanel) hwDetailPanel.classList.add('hidden');
  const hwOverview = document.getElementById('hw-overview');
  if (hwOverview) hwOverview.classList.remove('hidden');
}

function openCcDetail(wsite) {
  const info = clientCountData[wsite];
  if (!info || !ccDetailPanel) return;
  const ccOverview = document.getElementById('cc-overview');
  if (ccOverview) ccOverview.classList.add('hidden');
  ccDetailPanel.classList.remove('hidden');

  if (ccDetailTitle) ccDetailTitle.textContent = info.site_name || wsite;
  const degraded = info.status === 'DEGRADED';
  const noData = info.status === 'NO_DATA';
  const stale = info.baseline_stale;
  if (ccDetailSub) ccDetailSub.textContent = `Client count monitoring — ${info.status}${stale ? ' (last session baseline)' : ''}`;
  if (ccDetailBadge) {
    ccDetailBadge.textContent = noData ? 'Collecting baseline' : degraded ? `${info.drop_pct.toFixed(1)}% drop` : '✓ OK';
    ccDetailBadge.className = `sim-status-badge ${noData ? 'sim-unknown' : degraded ? 'sim-fail' : 'sim-pass'}`;
  }
  if (!ccSiteDetail) return;
  ccSiteDetail.textContent = '';
  const row = document.createElement('div');
  row.className = 'sim-site-row';
  row.style.cursor = 'default';
  const staleNote = stale && info.baseline_recorded_at
    ? `<span style="font-size:0.8rem;color:var(--muted)"> ⏱ Baseline from ${new Date(info.baseline_recorded_at * 1000).toLocaleString()} — rebuilding live average</span>`
    : '';
  row.innerHTML = `
    <span class="sim-site-name">${info.site_name || wsite}</span>
    <span style="font-size:0.85rem;color:var(--muted)">
      Current: <strong>${info.current}</strong> &nbsp;|&nbsp;
      60-min avg: <strong>${Math.round(info.hourly_avg)}</strong> &nbsp;|&nbsp;
      Δ: <strong style="color:${degraded ? '#e74c3c' : 'var(--hpe-green-dark)'}">${noData ? '—' : formatClientCountDelta(info.drop_pct)}</strong>
    </span>${staleNote}
  `;
  ccSiteDetail.appendChild(row);
}

function closeCcDetail() {
  if (ccDetailPanel) ccDetailPanel.classList.add('hidden');
  const ccOverview = document.getElementById('cc-overview');
  if (ccOverview) ccOverview.classList.remove('hidden');
}

async function loadSimulations() {
  try {
    const data = await requestJson('/api/simulations');
    simulationsData = (data.simulations || []).sort((a, b) => a.id.localeCompare(b.id));
    renderChecksList();
    if (simLastRefreshed) {
      simLastRefreshed.textContent = `Last refreshed: ${new Date().toLocaleTimeString()}`;
    }
    if (openSimId) {
      openSimGroup(openSimId);
    }
  } catch (err) {
    const emptyEl = simEmpty;
    if (emptyEl) {
      emptyEl.textContent = `Error loading simulations: ${err.message}`;
      emptyEl.classList.remove('hidden');
    }
  }
}

const checksFilterInput = document.getElementById('checks-filter');
if (checksFilterInput) {
  checksFilterInput.addEventListener('input', renderChecksList);
}

if (simDetailBack) simDetailBack.addEventListener('click', closeSimDetail);
if (simClientsBack) simClientsBack.addEventListener('click', () => {
  if (simClientsPanel) simClientsPanel.classList.add('hidden');
  if (simDetail) simDetail.classList.remove('hidden');
});
if (hwDetailBack) hwDetailBack.addEventListener('click', closeHwDetail);
if (ccDetailBack) ccDetailBack.addEventListener('click', closeCcDetail);

// ── Purge client history ───────────────────────────────────────────────────
const purgeHistoryBtn = document.getElementById('purge-history-btn');
if (purgeHistoryBtn) {
  purgeHistoryBtn.addEventListener('click', async () => {
    if (!confirm('Clear all client history? Records on disk will also be deleted. This cannot be undone.')) return;
    purgeHistoryBtn.disabled = true;
    purgeHistoryBtn.textContent = '⏳ Purging…';
    try {
      const resp = await fetch('/api/clients/history', { method: 'DELETE' });
      if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
    } catch (err) {
      alert(`Purge failed: ${err.message}`);
    } finally {
      purgeHistoryBtn.disabled = false;
      purgeHistoryBtn.textContent = '🗑 Purge Clients';
    }
  });
}

simTabButtons.forEach((button) => {
  button.addEventListener('click', () => {
    loadSimulations();
  });
});

if (simRefreshBtn) {
  simRefreshBtn.addEventListener('click', async () => {
    const orig = simRefreshBtn.textContent;
    simRefreshBtn.disabled = true;
    simRefreshBtn.textContent = 'Refreshing…';
    try {
      await loadSimulations();
    } finally {
      simRefreshBtn.disabled = false;
      simRefreshBtn.textContent = orig;
    }
  });
}

if (centralDetailBack) {
  centralDetailBack.addEventListener('click', closeSiteDetail);
}

centralTabButtons.forEach((button) => {
  button.addEventListener('click', () => {
    if (!centralStatusInitialized || !Object.keys(centralStatusData).length) {
      loadCentralStatus().catch(() => {});
    } else {
      renderCentralOverview();
    }
  });
});

configTabButtons.forEach((button) => {
  button.addEventListener('click', () => {
    loadConfigEditor().catch(() => {});
  });
});

// configSimulationSaveBtn removed — each section now has its own per-section Save button

document.querySelectorAll('.config-subtab').forEach((btn) => {
  btn.addEventListener('click', () => activateConfigSubtab(btn.dataset.subtab));
});

document.querySelectorAll('.server-subtab').forEach((btn) => {
  btn.addEventListener('click', () => activateServerSubtab(btn.dataset.subtab));
});

document.querySelectorAll('.sim-subtab').forEach((btn) => {
  btn.addEventListener('click', () => activateSimSubtab(btn.dataset.simtab));
});

if (setupSubtabButtons.length) {
  setupSubtabButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      activateSetupSubtab(btn.dataset.subtab);
    });
  });
}

async function _autoSaveRelay() {
  const tenantId = relayTenantIdInput?.value?.trim() || '';
  const payload = {
    relay_enabled: relayEnabledSelect?.value || 'off',
    relay_server_url: relayServerUrlInput?.value?.trim() || '',
    hub_tls_verify: relayHubTlsVerifyInput?.checked ? 'on' : 'off',
    relay_spoke_name: relaySpokeName?.value?.trim() || '',
    relay_tenant_hint: tenantId,
    relay_tenant_id: tenantId,
  };
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    showInlineMessage(relayMsg, 'Hub settings saved.', false);
    await loadSettings();
    await requestJson('/api/relay/status').then(setRelayStatus).catch(() => {});
  } catch (error) {
    showInlineMessage(relayMsg, `Error: ${error.message}`, true);
  }
}

if (relayEnabledSelect) relayEnabledSelect.addEventListener('change', _autoSaveRelay);
if (relayHubTlsVerifyInput) relayHubTlsVerifyInput.addEventListener('change', _autoSaveRelay);
[relayServerUrlInput, relaySpokeName, relayTenantIdInput].forEach((el) => {
  if (el) el.addEventListener('blur', _autoSaveRelay);
});
if (relayClearConfigBtn) {
  relayClearConfigBtn.addEventListener('click', () => clearSettingsProvider('relay', {
    button: relayClearConfigBtn,
    messageEl: relayMsg,
    successText: 'Hub config cleared.',
    extraDataHandler: (data) => setRelayStatus(data.relay_status || { enabled: false, connected: false, last_sync: null, error: null, registration_status: 'unregistered' })
  }));
}

// Registration diagnostics button
const relayDiagBtn = document.getElementById('relay-diag-btn');
const relayDiagPanel = document.getElementById('relay-diag-panel');
const serviceLogsRefreshBtn = document.getElementById('service-logs-refresh');
const serviceLogsCountEl = document.getElementById('service-logs-count');
const serviceLogsOutputEl = document.getElementById('service-logs-output');

async function loadSetupServiceLogs(lines = 50) {
  const requestedLines = Number.parseInt(lines, 10) || 50;
  if (serviceLogsRefreshBtn) serviceLogsRefreshBtn.disabled = true;
  if (serviceLogsOutputEl) serviceLogsOutputEl.textContent = 'Loading service logs…';
  try {
    const data = await requestJson(`/api/logs/service?lines=${encodeURIComponent(requestedLines)}`);
    const logLines = Array.isArray(data?.lines) ? data.lines : [];
    const returnedCount = Number.isFinite(data?.count) ? data.count : logLines.length;
    if (serviceLogsCountEl) {
      serviceLogsCountEl.textContent = `Last ${requestedLines} ${requestedLines === 1 ? 'line' : 'lines'}`;
      serviceLogsCountEl.title = `${returnedCount} ${returnedCount === 1 ? 'line' : 'lines'} returned`;
    }
    if (serviceLogsOutputEl) {
      serviceLogsOutputEl.textContent = logLines.length ? logLines.join('\n') : '(no service logs available)';
      serviceLogsOutputEl.scrollTop = serviceLogsOutputEl.scrollHeight;
    }
  } catch (err) {
    if (serviceLogsCountEl) serviceLogsCountEl.textContent = `Last ${requestedLines} ${requestedLines === 1 ? 'line' : 'lines'}`;
    if (serviceLogsOutputEl) serviceLogsOutputEl.textContent = `Error fetching service logs: ${err}`;
  } finally {
    if (serviceLogsRefreshBtn) serviceLogsRefreshBtn.disabled = false;
  }
}

if (serviceLogsRefreshBtn) {
  serviceLogsRefreshBtn.addEventListener('click', () => {
    loadSetupServiceLogs();
  });
}

if (relayDiagBtn) {
  relayDiagBtn.addEventListener('click', async () => {
    relayDiagBtn.disabled = true;
    relayDiagBtn.textContent = '⏳ Running…';
    if (relayDiagPanel) relayDiagPanel.classList.remove('hidden');
    try {
      const [diagResult] = await Promise.allSettled([
        requestJson('/api/relay/diag'),
        loadSetupServiceLogs(),
      ]);
      if (diagResult.status !== 'fulfilled') throw diagResult.reason;
      const d = diagResult.value;

      // Config check
      const cfg = d.config || {};
      const cfgEl = document.getElementById('relay-diag-config');
      if (cfgEl) {
        const rows = [
          ['relay_enabled', cfg.relay_enabled],
          ['server_url',    cfg.server_url],
          ['spoke_name',    cfg.spoke_name],
          ['hostname',      cfg.hostname],
          ['spoke_id',      cfg.spoke_id],
          ['api_key',       cfg.api_key_configured ? '✅ set' : '❌ not set'],
          ['tenant_id',     cfg.tenant_id || '(none)'],
        ];
        cfgEl.innerHTML = rows.map(([k, v]) =>
          `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v ?? ''))}</dd>`
        ).join('');
      }

      // Reachability
      const reach = d.reachability || {};
      const reachEl = document.getElementById('relay-diag-reach');
      if (reachEl) {
        const icon = reach.ok ? '✅' : '❌';
        const urlColor = reach.ok ? 'var(--success,#22c55e)' : 'var(--error,#ef4444)';
        let bodyHtml = '';
        if (reach.detail) {
          try {
            const pretty = JSON.stringify(JSON.parse(reach.detail), null, 2);
            bodyHtml = `<pre class="diag-code">${escHtml(pretty)}</pre>`;
          } catch {
            bodyHtml = `<pre class="diag-code">${escHtml(reach.detail)}</pre>`;
          }
        }
        reachEl.innerHTML =
          `<div class="diag-reach-row">
             <span>${icon}</span>
             <span class="diag-reach-url" style="color:${urlColor}">${escHtml(reach.tested_url || '')}</span>
           </div>${bodyHtml}`;
      }

      // Log
      const log = d.log || [];
      const logCountEl = document.getElementById('relay-diag-log-count');
      if (logCountEl) logCountEl.textContent = String(log.length);
      const logEl = document.getElementById('relay-diag-log');
      if (logEl) {
        if (!log.length) {
          logEl.innerHTML = `<div class="diag-log-entry"><span class="diag-log-attrs">(no registration attempts recorded yet)</span></div>`;
        } else {
          logEl.innerHTML = log.map(e => {
            const attrs = Object.entries(e)
              .filter(([k]) => k !== 'ts' && k !== 'event')
              .map(([k, v]) => `${escHtml(k)}=${escHtml(JSON.stringify(v))}`)
              .join(' &nbsp;');
            const ev = e.event || '';
            const cls = /error|fail/i.test(ev) ? 'is-error' : /ok|approved|received/i.test(ev) ? 'is-ok' : '';
            return `<div class="diag-log-entry ${cls}">
              <span class="diag-log-ts">[${escHtml(e.ts || '')}]</span>
              <span class="diag-log-event">${escHtml(ev)}</span>
              ${attrs ? `<span class="diag-log-attrs">${attrs}</span>` : ''}
            </div>`;
          }).join('');
        }
      }
    } catch (err) {
      const logEl = document.getElementById('relay-diag-log');
      if (logEl) logEl.textContent = `Error fetching diagnostics: ${err}`;
    } finally {
      relayDiagBtn.disabled = false;
      relayDiagBtn.textContent = '🔍 Run Registration Diagnostics';
    }
  });
}

if (addVidPidBtn) {
  addVidPidBtn.addEventListener('click', addVidPid);
}

if (addIgnoredHostnameBtn) {
  addIgnoredHostnameBtn.addEventListener('click', async () => {
    const hostname = (newIgnoredHostnameInput?.value || '').trim();
    if (!hostname) return;
    const current = parseJsonList(currentSettings.ignored_hostnames);
    if (current.includes(hostname)) {
      showNotification(`${hostname} is already in the list`, 'error');
      return;
    }
    current.push(hostname);
    currentSettings.ignored_hostnames = serializeJsonList(current);
    if (newIgnoredHostnameInput) newIgnoredHostnameInput.value = '';
    renderIgnoredHostnamesList();
    try {
      await requestJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ignored_hostnames: currentSettings.ignored_hostnames }),
      });
      showNotification(`${hostname} added to ignored clients`, 'success');
    } catch (err) {
      showNotification(`Error saving: ${err.message}`, 'error');
    }
  });
}

// ── Auto-save: USB settings & VM Maintenance ─────────────────────────────────
// Checkboxes / selects → save immediately on change.
// Text / number inputs → save on blur (when user clicks/tabs away).

async function _autoSaveUsb(msgEl) {
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectUsbSettingsPayload()),
    });
    showInlineMessage(msgEl, 'Saved.', false);
  } catch (err) {
    showInlineMessage(msgEl, `Error: ${err.message}`, true);
  }
}

async function _autoSaveVmMaintenance(msgEl) {
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        vm_silent_timeout: String(vmSilentTimeoutInput?.value || '24'),
        reclone_schedule_enabled: recloneScheduleEnabledInput?.checked ? 'on' : 'off',
        reclone_schedule_cron: `${recloneScheduleDayInput?.value || 'sunday'} ${recloneScheduleTimeInput?.value || '02:00'}`,
        reclone_concurrency: String(recloneConcurrencyInput?.value ?? '1'),
      }),
    });
    showInlineMessage(msgEl, 'Saved.', false);
  } catch (err) {
    showInlineMessage(msgEl, `Error: ${err.message}`, true);
  }
}

// USB — save checkbox changes immediately; number inputs on blur.
if (usbAutoProvisionInput) usbAutoProvisionInput.addEventListener('change', () => _autoSaveUsb(usbSettingsMsg));
[usbMissingTimeoutInput, usbMaxSlotsInput, vmImage1TemplateIdInput, vmImage2TemplateIdInput, vmImage1PctInput].forEach((el) => {
  if (el) el.addEventListener('blur', () => _autoSaveUsb(usbSettingsMsg));
});

// Layer 1 VLAN — save on blur
[l1VlanStartInput, l1VlanEndInput].forEach((el) => {
  if (el) el.addEventListener('blur', () => _autoSaveUsb(l1VlanMsg));
});

// VM Maintenance — checkboxes/selects: save on change; number/time inputs: save on blur.
if (recloneScheduleEnabledInput) recloneScheduleEnabledInput.addEventListener('change', () => _autoSaveVmMaintenance(vmMaintenanceMsg));
if (recloneScheduleDayInput)     recloneScheduleDayInput.addEventListener('change',  () => _autoSaveVmMaintenance(vmMaintenanceMsg));
[vmSilentTimeoutInput, recloneScheduleTimeInput, recloneConcurrencyInput].forEach((el) => {
  if (el) el.addEventListener('blur', () => _autoSaveVmMaintenance(vmMaintenanceMsg));
});

if (centralRefreshBtn) {
  centralRefreshBtn.addEventListener('click', async () => {
    const originalLabel = centralRefreshBtn.textContent;
    centralRefreshBtn.disabled = true;
    centralRefreshBtn.textContent = 'Refreshing…';
    try {
      await requestJson('/api/central/poll', { method: 'POST' });
      await loadCentralStatus();
    } catch (error) {
      if (centralLastSynced) centralLastSynced.textContent = `Refresh failed: ${error.message}`;
    } finally {
      centralRefreshBtn.disabled = false;
      centralRefreshBtn.textContent = originalLabel;
    }
  });
}

if (centralSaveBtn) {
  centralSaveBtn.addEventListener('click', async () => {
    const originalLabel = centralSaveBtn.textContent;
    centralSaveBtn.disabled = true;
    centralSaveBtn.textContent = 'Saving…';
    showInlineMessage(centralConfigMsg, '', false, 0);
    try {
      await persistCentralApiConfig();
      showInlineMessage(centralConfigMsg, 'Saved.', false, 2000);
    } catch (error) {
      showInlineMessage(centralConfigMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      centralSaveBtn.disabled = false;
      centralSaveBtn.textContent = originalLabel;
    }
  });
}

if (centralTestBtn) {
  centralTestBtn.addEventListener('click', async () => {
    const originalLabel = centralTestBtn.textContent;
    centralTestBtn.disabled = true;
    centralTestBtn.textContent = 'Testing…';
    showInlineMessage(centralConfigMsg, '', false, 0);
    try {
      await persistCentralApiConfig();
      const result = await requestJson('/api/central/test-connection', { method: 'POST' });
      centralTokenValid = true;
      setCentralApiStatus(true);
      updateCentralToolbar();
      showInlineMessage(centralConfigMsg, result.message || 'Connected to Central API successfully.', false);
    } catch (error) {
      centralTokenValid = false;
      setCentralApiStatus(false);
      updateCentralToolbar();
      showInlineMessage(centralConfigMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      centralTestBtn.disabled = false;
      centralTestBtn.textContent = originalLabel;
    }
  });
}

if (centralClearBtn) {
  centralClearBtn.addEventListener('click', async () => {
    const originalLabel = centralClearBtn.textContent;
    centralClearBtn.disabled = true;
    centralClearBtn.textContent = 'Clearing…';
    showInlineMessage(centralConfigMsg, '', false, 0);
    try {
      const response = await requestJson('/api/settings/clear/central', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: getCentralApiMode() })
      });
      applySettingsToUI(response.settings || {});
      resetCentralSecretInputs();
      centralTokenValid = false;
      setCentralApiStatus(false);
      updateCentralToolbar();
      showInlineMessage(centralConfigMsg, 'Config cleared', false, 3000);
    } catch (error) {
      showInlineMessage(centralConfigMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      centralClearBtn.disabled = false;
      centralClearBtn.textContent = originalLabel;
    }
  });
}

async function loadSiteMappingSources() {
  if (loadSitesBtn) { loadSitesBtn.disabled = true; loadSitesBtn.textContent = 'Loading…'; }
  if (sitesLoadStatus) sitesLoadStatus.textContent = '';
  const [wsiteResult, centralResult] = await Promise.allSettled([
    requestJson('/api/local-wsites'),
    requestJson('/api/central/sites'),
  ]);

  localWsites = wsiteResult.status === 'fulfilled' ? (wsiteResult.value.wsites || []) : [];
  centralSites = centralResult.status === 'fulfilled' ? (centralResult.value.sites || []) : [];
  renderSiteMappingsTable();

  const msgs = [];
  if (wsiteResult.status === 'rejected') msgs.push(`Local: ${wsiteResult.reason?.message}`);
  else msgs.push(`${localWsites.length} local wsite(s)`);

  if (centralResult.status === 'rejected') {
    msgs.push(`Central: ${centralResult.reason?.message}`);
  } else {
    const warn = centralResult.value?.warning;
    msgs.push(warn ? `Central: ⚠ ${warn}` : `${centralSites.length} Central site(s)`);
  }

  if (sitesLoadStatus) sitesLoadStatus.textContent = msgs.join(' | ');
  if (loadSitesBtn) { loadSitesBtn.disabled = false; loadSitesBtn.textContent = '🔄 Load Sites'; }
}

if (loadSitesBtn) {
  loadSitesBtn.addEventListener('click', loadSiteMappingSources);
}

if (addMappingBtn) {
  addMappingBtn.addEventListener('click', () => addMappingRow());
}

async function _autoSaveSiteMappings() {
  const rows = siteMappingsBody ? [...siteMappingsBody.querySelectorAll('tr')] : [];
  const siteMappings = {};
  rows.forEach((row) => {
    const cells = row.querySelectorAll('td');
    const wsite = cells[0]?.querySelector('.mapping-val')?.value?.trim() || '';
    const centralSite = cells[1]?.querySelector('.mapping-val')?.value?.trim() || '';
    if (wsite && centralSite) siteMappings[wsite] = centralSite;
  });
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ site_mappings: siteMappings })
    });
    applySettingsToUI({ site_mappings: siteMappings });
    showInlineMessage(centralMappingsMsg, 'Site mappings saved.', false, 2000);
    renderCentralOverview();
  } catch (error) {
    showInlineMessage(centralMappingsMsg, `Error: ${error.message}`, true, 7000);
  }
}

if (siteMappingsBody) {
  siteMappingsBody.addEventListener('change', _autoSaveSiteMappings);
}

if (loadChecksBtn) {
  loadChecksBtn.addEventListener('click', async () => {
    const originalLabel = loadChecksBtn.textContent;
    loadChecksBtn.disabled = true;
    loadChecksBtn.textContent = 'Loading…';
    if (availableChecksContainer) availableChecksContainer.textContent = 'Loading available checks…';
    try {
      const data = await requestJson('/api/central/available');
      availableChecks = {
        alerts: data.alerts || [],
        insights: data.insights || []
      };
      renderAvailableChecks();
      const total = availableChecks.alerts.length + availableChecks.insights.length;
      const warn = data.warning ? ` ⚠ ${data.warning}` : '';
      showInlineMessage(centralChecksMsg, `${total} check(s) loaded.${warn}`, !!data.warning, data.warning ? 10000 : 3000);
    } catch (error) {
      availableChecks = { alerts: [], insights: [] };
      if (availableChecksContainer) {
        availableChecksContainer.textContent = `Unable to load checks: ${error.message}`;
      }
      showInlineMessage(centralChecksMsg, `Error: ${error.message}`, true, 7000);
    } finally {
      loadChecksBtn.disabled = false;
      loadChecksBtn.textContent = originalLabel;
    }
  });
}

async function _autoSaveMonitoredChecks() {
  const allInputs = availableChecksContainer
    ? [...availableChecksContainer.querySelectorAll('input[type="checkbox"]')]
    : [];
  const monitoredChecks = allInputs.length
    ? allInputs.filter((cb) => cb.checked).map((cb) => ({
        type: cb.dataset.type,
        id: cb.dataset.id,
        name: cb.dataset.name || cb.dataset.id
      }))
    : (currentSettings.monitored_checks || []);
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ monitored_checks: monitoredChecks })
    });
    applySettingsToUI({ monitored_checks: monitoredChecks });
    showInlineMessage(centralChecksMsg, 'Saved.', false, 1500);
  } catch (error) {
    showInlineMessage(centralChecksMsg, `Error: ${error.message}`, true, 7000);
  }
}

if (availableChecksContainer) {
  availableChecksContainer.addEventListener('change', (e) => {
    if (e.target?.type === 'checkbox') _autoSaveMonitoredChecks();
  });
}

// ── Hardware Checks ────────────────────────────────────────────────────────
let availableAlertTypes = []; // loaded from /api/central/available

function renderHwChecksList() {
  if (!hwChecksContainer) return;
  hwChecksContainer.textContent = '';
  if (!availableAlertTypes.length) return;

  const selectedIds = new Set((currentSettings.hardware_checks || []).map((c) => c.id));
  const deviceTypeIcons = { ap: '📡', gateway: '🌐', switch: '🔀' };

  availableAlertTypes.forEach((alert) => {
    const row = document.createElement('label');
    row.className = 'hw-check-row';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.id = alert.id;
    cb.dataset.name = alert.name || alert.id;
    cb.dataset.deviceType = alert.device_type || '';
    cb.checked = selectedIds.has(alert.id);

    const icon = document.createElement('span');
    icon.className = 'hw-check-icon';
    const dtype = (alert.device_type || '').toLowerCase();
    icon.textContent = deviceTypeIcons[dtype] || '⚠';

    const nameSpan = document.createElement('span');
    nameSpan.className = 'hw-check-name';
    nameSpan.textContent = alert.name || alert.id;

    const idSpan = document.createElement('span');
    idSpan.className = 'hw-check-id';
    idSpan.textContent = alert.id;

    row.appendChild(cb);
    row.appendChild(icon);
    row.appendChild(nameSpan);
    row.appendChild(idSpan);
    hwChecksContainer.appendChild(row);
  });
}

if (hwLoadAlertsBtn) {
  hwLoadAlertsBtn.addEventListener('click', async () => {
    hwLoadAlertsBtn.disabled = true;
    hwLoadAlertsBtn.textContent = 'Loading…';
    if (hwChecksContainer) hwChecksContainer.textContent = 'Loading available alert types…';
    try {
      const data = await requestJson('/api/central/available');
      availableAlertTypes = data.alerts || [];
      renderHwChecksList();
      const warn = data.warning ? ` ⚠ ${data.warning}` : '';
      showInlineMessage(hwChecksMsg, `${availableAlertTypes.length} alert type(s) loaded.${warn}`, !!data.warning, data.warning ? 10000 : 3000);
    } catch (err) {
      availableAlertTypes = [];
      if (hwChecksContainer) hwChecksContainer.textContent = '';
      showInlineMessage(hwChecksMsg, `Error: ${err.message}`, true, 7000);
    } finally {
      hwLoadAlertsBtn.disabled = false;
      hwLoadAlertsBtn.textContent = 'Load Available Alert Types';
    }
  });
}

async function _autoSaveHwChecks() {
  const allInputs = hwChecksContainer
    ? [...hwChecksContainer.querySelectorAll('input[type="checkbox"]')]
    : [];
  const hardwareChecks = allInputs.length
    ? allInputs.filter((cb) => cb.checked).map((cb) => ({
        id: cb.dataset.id,
        name: cb.dataset.name || cb.dataset.id,
        device_type: cb.dataset.deviceType || ''
      }))
    : (currentSettings.hardware_checks || []);
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hardware_checks: hardwareChecks })
    });
    currentSettings.hardware_checks = hardwareChecks;
    renderHwChecksPreview();
    if (availableAlertTypes.length) renderHwChecksList();
    showInlineMessage(hwChecksMsg, 'Saved.', false, 1500);
  } catch (err) {
    showInlineMessage(hwChecksMsg, `Error: ${err.message}`, true, 7000);
  }
}

if (hwChecksContainer) {
  hwChecksContainer.addEventListener('change', (e) => {
    if (e.target?.type === 'checkbox') _autoSaveHwChecks();
  });
}

// ── Sync interval ──────────────────────────────────────────────────────────
if (syncIntervalInput) {
  syncIntervalInput.addEventListener('blur', async () => {
    const val = parseInt(syncIntervalInput.value, 10);
    if (!val || val < 60 || val > 86400) {
      showInlineMessage(syncIntervalMsg, 'Enter a value between 60 and 86400 seconds.', true);
      return;
    }
    try {
      const response = await requestJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_sync_interval: val })
      });
      applySettingsToUI(response.settings || { repo_sync_interval: val });
      showInlineMessage(syncIntervalMsg, `Sync interval set to ${val}s.`, false);
    } catch (err) {
      showInlineMessage(syncIntervalMsg, `Error: ${err.message}`, true);
    }
  });
}

// ── Email notifications ────────────────────────────────────────────────────
function collectEmailPayload() {
  const payload = {
    email_enabled: emailEnabledToggle?.checked ?? false,
    smtp_host:     smtpHost?.value.trim() || '',
    smtp_port:     parseInt(smtpPort?.value, 10) || 587,
    smtp_user:     smtpUser?.value.trim() || '',
    smtp_from:     smtpFrom?.value.trim() || '',
    smtp_to:       (smtpTo?.value || '').split(',').map(s => s.trim()).filter(Boolean),
  };
  const smtpSecret = getSecretInputPayload(smtpPassword);
  if (smtpSecret.include) payload.smtp_password = smtpSecret.value;
  return payload;
}

async function _autoSaveEmail() {
  const payload = collectEmailPayload();
  try {
    const response = await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notifications: payload })
    });
    applySettingsToUI(response.settings || {});
    resetSecretInput(smtpPassword);
    showInlineMessage(emailNotifMsg, 'Saved.', false, 1500);
  } catch (err) {
    showInlineMessage(emailNotifMsg, `Error: ${err.message}`, true);
  }
}

if (emailEnabledToggle) emailEnabledToggle.addEventListener('change', _autoSaveEmail);
if (saveEmailBtn) saveEmailBtn.addEventListener('click', _autoSaveEmail);
[smtpHost, smtpPort, smtpUser, smtpPassword, smtpFrom, smtpTo].forEach((el) => {
  if (el) el.addEventListener('blur', _autoSaveEmail);
});

if (testEmailBtn) {
  testEmailBtn.addEventListener('click', async () => {
    const payload = { channel: 'email', ...collectEmailPayload() };
    testEmailBtn.disabled = true;
    testEmailBtn.textContent = 'Sending…';
    try {
      await requestJson('/api/notifications/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showInlineMessage(emailNotifMsg, 'Test email sent — check your inbox.', false);
    } catch (err) {
      showInlineMessage(emailNotifMsg, `Failed: ${err.message}`, true, 8000);
    } finally {
      testEmailBtn.disabled = false;
      testEmailBtn.textContent = 'Send Test';
    }
  });
}

// ── Teams webhook ──────────────────────────────────────────────────────────
async function _autoSaveTeams() {
  const secret = getSecretInputPayload(teamsWebhookUrl);
  const payload = {
    teams_enabled:     teamsEnabledToggle?.checked ?? false,
  };
  if (secret.include) payload.teams_webhook_url = secret.value.trim();
  try {
    const response = await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notifications: payload })
    });
    applySettingsToUI(response.settings || {});
    resetSecretInput(teamsWebhookUrl);
    showInlineMessage(teamsNotifMsg, 'Saved.', false, 1500);
  } catch (err) {
    showInlineMessage(teamsNotifMsg, `Error: ${err.message}`, true);
  }
}

if (teamsEnabledToggle) teamsEnabledToggle.addEventListener('change', _autoSaveTeams);
if (saveTeamsBtn) saveTeamsBtn.addEventListener('click', _autoSaveTeams);
if (teamsWebhookUrl) teamsWebhookUrl.addEventListener('blur', _autoSaveTeams);

if (testTeamsBtn) {
  testTeamsBtn.addEventListener('click', async () => {
    const url = teamsWebhookUrl?.value.trim() || '';
    if (!url) {
      showInlineMessage(teamsNotifMsg, 'Enter a webhook URL first.', true);
      return;
    }
    testTeamsBtn.disabled = true;
    testTeamsBtn.textContent = 'Sending…';
    try {
      await requestJson('/api/notifications/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: 'teams', teams_webhook_url: url })
      });
      showInlineMessage(teamsNotifMsg, 'Test card posted to Teams.', false);
    } catch (err) {
      showInlineMessage(teamsNotifMsg, `Failed: ${err.message}`, true, 8000);
    } finally {
      testTeamsBtn.disabled = false;
      testTeamsBtn.textContent = 'Send Test';
    }
  });
}

// ── Clear Cache buttons ────────────────────────────────────────────────────────

// ── Troubleshooting tab — system health, service control, WiFi fix ─────────────

function fmtBytes(bytes) {
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
  return (bytes / 1e3).toFixed(0) + ' KB';
}
function fmtUptime(secs) {
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600),
        m = Math.floor((secs % 3600) / 60);
  return [d && `${d}d`, h && `${h}h`, `${m}m`].filter(Boolean).join(' ');
}

async function loadSystemHealth() {
  const systemHealthMsg = document.getElementById('syshealth-msg');
  try {
    const r = await fetch('/api/system/health');
    if (!r.ok) {
      throw new Error(`HTTP ${r.status}${r.statusText ? ` ${r.statusText}` : ''}`);
    }
    const d = await r.json();
    showInlineMessage(systemHealthMsg, '', false, 0);

    // Service status dot
    const dot = document.getElementById('svc-status-dot');
    const lbl = document.getElementById('svc-status-label');
    if (dot && lbl) {
      const active = d.service_status === 'active';
      dot.style.background = active ? '#6fcf97' : '#eb5757';
      lbl.textContent = d.service_status || '—';
    }

    // Uptime
    const up = document.getElementById('syshealth-uptime');
    if (up) up.textContent = d.uptime_secs ? fmtUptime(d.uptime_secs) : '—';

    // Disk bar
    if (d.disk && d.disk.total) {
      const pct = Math.round(d.disk.used / d.disk.total * 100);
      const bar = document.getElementById('syshealth-disk-bar');
      const lbl2 = document.getElementById('syshealth-disk-label');
      if (bar) { bar.style.width = pct + '%'; bar.style.background = pct > 85 ? '#eb5757' : '#6fcf97'; }
      if (lbl2) lbl2.textContent = `${fmtBytes(d.disk.used)} / ${fmtBytes(d.disk.total)} (${pct}%)`;
    }

    // RAM bar
    if (d.memory && d.memory.total_kb) {
      const pct = Math.round(d.memory.used_kb / d.memory.total_kb * 100);
      const bar = document.getElementById('syshealth-ram-bar');
      const lbl3 = document.getElementById('syshealth-ram-label');
      if (bar) { bar.style.width = pct + '%'; bar.style.background = pct > 85 ? '#eb5757' : '#56ccf2'; }
      if (lbl3) lbl3.textContent = `${fmtBytes(d.memory.used_kb * 1024)} / ${fmtBytes(d.memory.total_kb * 1024)} (${pct}%)`;
    }

    // Load
    const loadEl = document.getElementById('syshealth-load');
    if (loadEl && d.load) loadEl.textContent = d.load.join('  /  ');

    // Proxmox install command
    const cmdEl = document.getElementById('proxmox-install-cmd');
    if (cmdEl && d.proxmox_install_cmd) cmdEl.textContent = d.proxmox_install_cmd;
  } catch (error) {
    showInlineMessage(systemHealthMsg, `Could not load system health: ${error.message}`, true, 0);
  }
}

document.getElementById('syshealth-refresh-btn')?.addEventListener('click', loadSystemHealth);

// Service control
['restart', 'start', 'stop'].forEach((action) => {
  document.getElementById(`svc-${action}-btn`)?.addEventListener('click', async () => {
    const msg = document.getElementById('svc-control-msg');
    if (action === 'stop' && !confirm(
      'Stop the WebUI service?\n\nThis will take the dashboard offline. You will need to restart it from the Proxmox host console or via SSH.\n\nProceed?')) return;
    try {
      const r = await fetch(`/api/service/${action}`, { method: 'POST' });
      const d = await r.json();
      if (msg) {
        msg.textContent = d.message || (r.ok ? 'Done' : 'Error');
        msg.className = `settings-message ${r.ok && d.status === 'ok' ? '' : 'error'}`;
        msg.classList.remove('hidden');
        setTimeout(() => msg.classList.add('hidden'), 5000);
      }
      if (r.ok && (action === 'restart' || action === 'start')) {
        setTimeout(() => loadSystemHealth(), 3000);
      }
    } catch (err) {
      if (msg) { msg.textContent = `Error: ${err.message}`; msg.className = 'settings-message error'; msg.classList.remove('hidden'); }
    }
  });
});

// Copy proxmox install command
document.getElementById('proxmox-install-copy-btn')?.addEventListener('click', () => {
  const cmd = document.getElementById('proxmox-install-cmd')?.textContent || '';
  if (!cmd) return;
  navigator.clipboard.writeText(cmd).then(() => showToast('Command copied to clipboard', 'success'))
    .catch(() => showToast('Copy failed — select and copy manually', 'error'));
});

// WiFi auth fix — dispatch update_now to all clients
document.getElementById('wifi-fix-btn')?.addEventListener('click', async () => {
  if (!confirm('Push WiFi Auth Fix to all clients?\n\nThis queues an Update Now command for every registered client. Each client will re-deploy the polkit rule and restart nm-applet.')) return;
  try {
    const r = await fetch('/api/commands', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: 'all', action: 'update_now' }),
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    showToast(`WiFi fix queued for ${d.queued} client(s)`, 'success');
  } catch (err) {
    showToast(`Failed: ${err.message}`, 'error');
  }
});

document.getElementById('server-clear-cache-btn')?.addEventListener('click', async () => {
  if (!confirm('Clear all server-side cache?\n\nThis resets Proxmox state, VM list, pending auto-provisioning queue, command history, and reclone logs. No restart is required.')) return;
  try {
    const r = await fetch('/api/server/clear-cache', { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    showToast('Server cache cleared.', 'success');
  } catch (err) {
    showToast(`Failed: ${err.message}`, 'error');
  }
});

document.getElementById('setup-clear-cache-btn')?.addEventListener('click', async () => {
  if (!confirm('Clear all cache and re-clone?\n\nThis will:\n• Remove git lock files\n• Wipe and re-clone the repo from GitHub\n• Delete client history, state cache, and central history files\n• Restart the WebUI service\n\nThe page will reload automatically once the service is back up.')) return;
  try {
    const r = await fetch('/api/setup/clear-cache', { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    showToast('Cache cleared — restarting service, reloading in 10s…', 'info');
    setTimeout(() => location.reload(), 10000);
  } catch (err) {
    // Service may have restarted before responding
    showToast('Cache cleared — service restarting, reloading in 10s…', 'info');
    setTimeout(() => location.reload(), 10000);
  }
});

// ── VM category inner tab nav ──────────────────────────────────────────────────
const vmCatTabs = Array.from(document.querySelectorAll('.vm-cat-tab'));
vmCatTabs.forEach((btn) => {
  btn.addEventListener('click', () => {
    activeVmCat = btn.dataset.cat;
    vmCatTabs.forEach((button) => button.classList.toggle('active', button.dataset.cat === activeVmCat));
    ['sim', 'other', 'containers', 'templates'].forEach((cat) => {
      document.getElementById(`vm-cat-panel-${cat}`)?.classList.toggle('hidden', cat !== activeVmCat);
    });
    // Bulk bar hidden for templates (read-only) and viewer sessions
    const bulkBar = document.getElementById('vm-bulk-bar');
    if (bulkBar) bulkBar.classList.toggle('hidden', activeVmCat === 'templates' || spokeCurrentUser?.role === 'viewer');
    // Reset select-all
    const sa = document.getElementById('server-select-all');
    if (sa) sa.checked = false;
  });
});

document.getElementById('server-select-all')?.addEventListener('change', (e) => {
  // Only select checkboxes within the active category panel
  const panel = document.getElementById(`vm-cat-panel-${activeVmCat}`);
  if (panel) panel.querySelectorAll('.vm-check:not([disabled])').forEach((cb) => { cb.checked = e.target.checked; });
  const thCheck = document.getElementById(`server-th-check-${activeVmCat}`);
  if (thCheck) thCheck.checked = e.target.checked;
});

['start', 'stop', 'reclone', 'delete'].forEach((op) => {
  document.getElementById(`server-bulk-${op}`)?.addEventListener('click', async () => {
    if (activeVmCat === 'templates') return; // no bulk ops on templates
    const panel = document.getElementById(`vm-cat-panel-${activeVmCat}`);
    if (!panel) return;
    const selected = [...panel.querySelectorAll('.vm-check:checked')].map((cb) => ({
      vmid: cb.dataset.vmid,
      name: cb.dataset.vmName || '',
      vmType: cb.dataset.vmType || 'qemu',
      recloneSupported: cb.dataset.recloneSupported === 'true',
      sourceVmid: cb.dataset.recloneSourceVmid || '',
    }));
    if (!selected.length) return;

    try {
      if (op === 'delete') {
        if (activeVmCat !== 'sim' && !confirmVmDelete(selected)) return;
        const results = await Promise.allSettled(selected.map((entry) => deleteProxmoxVm(entry.vmid)));
        const failed = results.filter((result) => result.status === 'rejected');
        const successCount = results.length - failed.length;
        if (successCount) {
          showNotification(`Delete queued for ${successCount} guest(s)`, 'info');
          scheduleProxmoxRefresh();
        }
        if (failed.length) {
          throw new Error(failed[0].reason?.message || 'One or more deletes failed');
        }
        return;
      }

      const action = op === 'reclone' ? 'reclone_vm' : `${op}_vm`;
      const eligible = op === 'reclone'
        ? selected.filter((entry) => entry.recloneSupported)
        : selected;
      if (!eligible.length) {
        showNotification('No selected guests can be recloned with the current configuration.', 'warning');
        return;
      }

      if (op === 'reclone') {
        const autoProvOn = currentSettings.usb_auto_provision === 'on';
        if (autoProvOn) {
          // Auto-provisioning will redeploy — just delete to avoid race conditions
          const results = await Promise.allSettled(eligible.map((entry) => deleteProxmoxVm(entry.vmid)));
          const failed = results.filter((r) => r.status === 'rejected');
          const successCount = results.length - failed.length;
          if (successCount) {
            showNotification(`Deleted ${successCount} guest(s) — auto-provisioning will redeploy`, 'info');
            scheduleProxmoxRefresh();
          }
          if (failed.length) throw new Error(failed[0].reason?.message || 'One or more deletes failed');
        } else {
          await Promise.all(eligible.map((entry) => sendProxmoxCommand('reclone_vm', entry.vmid, {
            type: entry.vmType || 'qemu',
            source_vmid: entry.sourceVmid ? parseInt(entry.sourceVmid, 10) : undefined,
          })));
          const skipped = selected.length - eligible.length;
          showNotification(`Reclone sent for ${eligible.length} VM(s)${skipped ? ` (${skipped} skipped)` : ''}`, 'info');
        }
        return;
      }

      await Promise.all(eligible.map((entry) => sendProxmoxCommand(action, entry.vmid, {
        type: entry.vmType || 'qemu',
        source_vmid: entry.sourceVmid ? parseInt(entry.sourceVmid, 10) : undefined,
      })));
      const skipped = selected.length - eligible.length;
      showNotification(`${op} sent for ${eligible.length} VM(s)${skipped ? ` (${skipped} skipped)` : ''}`, 'info');
    } catch (err) {
      showNotification(`Error: ${err.message}`, 'error');
    }
  });
});

updateCentralToolbar();
activateSetupSubtab('setup-github');
const updateAllBtn = document.getElementById('update-all-btn');
if (updateAllBtn && !updateAllBtn._bound) {
  updateAllBtn.addEventListener('click', triggerUpdateAll);
  updateAllBtn._bound = true;
}

// ── Spoke authentication ───────────────────────────────────────────────────────
let spokeCurrentUser = null;
let _spokeBooted = false;

async function bootSpokeRuntime() {
  if (_spokeBooted || WEBUI_MODE !== 'spoke') return;
  _spokeBooted = true;
  connectWebSocket();
  loadSimulations();
  try {
    const init = consumeInitPayload() || await requestJson('/api/init');
    if (init.proxmox) {
      if (init.proxmox.webui_vmid != null) webuiVmid = init.proxmox.webui_vmid;
      if (init.proxmox.connected || (init.proxmox.vms || []).length || (init.proxmox.usb_state || []).length || (init.proxmox.unknown_usb || []).length || (init.proxmox.pending_proxmox || []).length || (init.proxmox.approved_proxmox || []).length) {
        renderServerTab(init.proxmox);
      }
    }
    if (init.reclone) renderRecloneStatus(init.reclone);
    if (init.update_all) handleUpdateAllProgress(init.update_all);
    if (init.settings) applySettingsToUI(init.settings);
    if (init.central) {
      centralTokenValid = Boolean(init.central.token_valid);
      setCentralApiStatus(centralTokenValid, init.central.token_state);
      handleCentralUpdate(init.central.status || {}, Date.now() / 1000, init.central.wireless_clients || {}, init.central.hardware_alerts || [], init.central.client_count_status || {});
    }
    if (init.relay) setRelayStatus(init.relay);
    if (init.kill_switch !== undefined) applyGkillSwitch(init.kill_switch);
    if (init.local_kill_switch !== undefined) {
      simDisabledState.local = init.local_kill_switch === 'on';
      renderSimDisabledBanner();
    }
    const fWebui = document.getElementById('footer-cswebui-version');
    const fRepo  = document.getElementById('footer-repo-version');
    if (fWebui) {
      const ver = init.app_version || init.installer_version || '—';
      fWebui.textContent = `CS-WebUI v${ver}`;
      fWebui.title = `cs-webui frontend version: v${ver}`;
    }
    if (fRepo) {
      const rver = init.installer_version || '—';
      fRepo.textContent = `GitHub Repo v${rver}`;
      fRepo.title = `Installer/repo version: v${rver}`;
    }
    applySpokeViewerMode();
  } catch (_) { /* silent — WS will provide live state */ }
}

(function initSpokeAuth() {
  if (WEBUI_MODE !== 'spoke') return;

  const overlay = document.getElementById('spoke-login-overlay');
  const loginBtn = document.getElementById('spoke-login-btn');
  const logoutBtn = document.getElementById('spoke-logout-btn');
  const usernameInput = document.getElementById('spoke-login-username');
  const pwInput = document.getElementById('spoke-login-password');
  const errEl = document.getElementById('spoke-login-error');
  const authRequired = !!window.__SPOKE_AUTH_REQUIRED__;
  const provider = normalizeSpokeAuthProvider(String(window.__SPOKE_AUTH_PROVIDER__ || 'local').trim().toLowerCase());

  updateSpokeLoginProviderUi(provider);

  async function doSpokeLogin() {
    const username = usernameInput?.value?.trim() || '';
    const password = pwInput?.value || '';
    if (errEl) errEl.textContent = '';
    if (loginBtn) loginBtn.disabled = true;
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        spokeCurrentUser = {
          username: data.username || username || 'admin',
          role: data.role || 'admin',
          auth_provider: provider,
        };
        window.__SPOKE_AUTHENTICATED__ = true;
        updateSpokeUserUi();
        applySpokeViewerMode();
        if (overlay) overlay.classList.add('hidden');
        if (pwInput) pwInput.value = '';
        await refreshSpokeAuthState().catch(() => {});
        await bootSpokeRuntime();
      } else {
        if (errEl) errEl.textContent = data.detail || (provider === 'local' ? 'Invalid password' : 'Invalid username or password');
        if (pwInput) pwInput.value = '';
        (provider === 'local' ? pwInput : usernameInput)?.focus();
      }
    } catch (_) {
      if (errEl) errEl.textContent = 'Login failed — try again';
    } finally {
      if (loginBtn) loginBtn.disabled = false;
    }
  }

  if (loginBtn && !loginBtn._spokeBound) {
    loginBtn._spokeBound = true;
    loginBtn.addEventListener('click', doSpokeLogin);
  }
  [usernameInput, pwInput].forEach((input) => {
    if (!input || input._spokeBound) return;
    input._spokeBound = true;
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') doSpokeLogin();
    });
  });

  if (logoutBtn && !logoutBtn._spokeBound) {
    logoutBtn._spokeBound = true;
    logoutBtn.addEventListener('click', async () => {
      await fetch('/api/auth/logout', { method: 'POST' });
      spokeCurrentUser = null;
      updateSpokeUserUi();
      window.location.reload();
    });
  }

  if (!authRequired) {
    updateSpokeUserUi();
    applySpokeViewerMode();
    bootSpokeRuntime();
    return;
  }

  if (!window.__SPOKE_AUTHENTICATED__) {
    if (overlay) overlay.classList.remove('hidden');
    setTimeout(() => (provider === 'local' ? pwInput : usernameInput)?.focus(), 100);
    return;
  }

  refreshSpokeAuthState().catch(() => {});
  bootSpokeRuntime();
})();

const _origFetch = window.fetch;
if (WEBUI_MODE === 'spoke' && window.__SPOKE_AUTH_REQUIRED__) {
  window.fetch = async function (...args) {
    const resp = await _origFetch(...args);
    if (resp.status === 401) {
      const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
      if (!url.includes('/api/auth/')) {
        spokeCurrentUser = null;
        updateSpokeUserUi();
        applySpokeViewerMode();
        window.location.reload();
      }
    }
    return resp;
  };
}

// ── Auto-refresh ──────────────────────────────────────────────────────────────
let _refreshTimer = null;

async function refreshAll() {
  try {
    const init = await requestJson('/api/init');
    if (init.proxmox) {
      if (init.proxmox.webui_vmid != null) webuiVmid = init.proxmox.webui_vmid;
      if (init.proxmox.connected || (init.proxmox.vms || []).length || (init.proxmox.usb_state || []).length || (init.proxmox.unknown_usb || []).length || (init.proxmox.pending_proxmox || []).length || (init.proxmox.approved_proxmox || []).length) {
        renderServerTab(init.proxmox);
      }
    }
    if (init.reclone) renderRecloneStatus(init.reclone);
    if (init.update_all) handleUpdateAllProgress(init.update_all);
    if (init.settings) applySettingsToUI(init.settings);
    if (init.central) {
      centralTokenValid = Boolean(init.central.token_valid);
      setCentralApiStatus(centralTokenValid, init.central.token_state);
      handleCentralUpdate(init.central.status || {}, Date.now() / 1000, init.central.wireless_clients || {}, init.central.hardware_alerts || [], init.central.client_count_status || {});
    }
    if (init.relay) setRelayStatus(init.relay);
    if (init.kill_switch !== undefined) applyGkillSwitch(init.kill_switch);
    if (init.local_kill_switch !== undefined) {
      simDisabledState.local = init.local_kill_switch === 'on';
      renderSimDisabledBanner();
    }
  } catch (_) { /* silent */ }
  // Also refresh simulations tab data so it stays current on auto-refresh
  try { await loadSimulations(); } catch (_) { /* silent */ }
}

function updateRefreshCountdownDisplay(text, paused = false) {
  const countdown = document.getElementById('refresh-countdown');
  if (!countdown) return;
  countdown.textContent = text;
  countdown.classList.toggle('paused', paused);
}

function stopRefreshTimers() {
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  if (refreshCountdownTimer) { clearInterval(refreshCountdownTimer); refreshCountdownTimer = null; }
}

function computeRefreshPaused() {
  if (document.hidden) return true;
  if (activeSpokeTab === 'server') {
    return !refreshActiveServerSubtabs.has(activeServerSubtab);
  }
  return !refreshActiveTabs.has(activeSpokeTab);
}

function restartRefreshTimer() {
  stopRefreshTimers();
  if (refreshIntervalSeconds <= 0) {
    updateRefreshCountdownDisplay('Off');
    return;
  }
  if (refreshPaused) {
    updateRefreshCountdownDisplay('Paused', true);
    return;
  }
  refreshSecondsLeft = refreshIntervalSeconds;
  updateRefreshCountdownDisplay(String(refreshSecondsLeft) + 's');
  refreshCountdownTimer = setInterval(() => {
    refreshSecondsLeft = Math.max(0, refreshSecondsLeft - 1);
    updateRefreshCountdownDisplay(String(refreshSecondsLeft) + 's');
  }, 1000);
  _refreshTimer = setInterval(async () => {
    refreshSecondsLeft = refreshIntervalSeconds;
    updateRefreshCountdownDisplay(String(refreshSecondsLeft) + 's');
    await refreshAll();
  }, refreshIntervalSeconds * 1000);
}

function updateRefreshPausedState() {
  const wasPaused = refreshPaused;
  refreshPaused = computeRefreshPaused();
  restartRefreshTimer();
  if (wasPaused && !refreshPaused) {
    refreshAll().catch(() => {});
  }
}

function applyRefreshInterval(seconds) {
  refreshIntervalSeconds = Number.isFinite(seconds) ? seconds : 0;
  localStorage.setItem('refreshInterval', String(refreshIntervalSeconds));
  updateRefreshPausedState();
}

const refreshSelect = document.getElementById('refresh-interval-select');
if (refreshSelect) {
  const saved = localStorage.getItem('refreshInterval');
  const defaultInterval = 10;
  const initial = saved !== null ? Number(saved) : defaultInterval;
  const opt = refreshSelect.querySelector(`option[value="${initial}"]`);
  if (opt) opt.selected = true;
  applyRefreshInterval(initial);
  refreshSelect.addEventListener('change', () => applyRefreshInterval(Number(refreshSelect.value)));
}

document.addEventListener('visibilitychange', () => {
  updateRefreshPausedState();
});

// ── Log viewer ────────────────────────────────────────────────────────────────
let loadServiceLogs = () => {};

(function initLogViewer() {
  const output       = document.getElementById('logs-output');
  const tailBtn      = document.getElementById('logs-tail-btn');
  const stopBtn      = document.getElementById('logs-stop-btn');
  const refreshBtn   = document.getElementById('logs-refresh-btn');
  const clearBtn     = document.getElementById('logs-clear-btn');
  const filterInput  = document.getElementById('logs-filter');
  const linesSelect  = document.getElementById('logs-lines-select');
  const sourceSelect = document.getElementById('logs-source-select');
  const autoScroll   = document.getElementById('logs-autoscroll');

  if (!output) return;

  let evtSource = null;
  let tailConnected = false;
  let tailRequested = false;
  let historyLoaded = false;
  const MAX_LINES = 2000;

  function classify(text) {
    const t = text.toLowerCase();
    if (/\b(error|err|exception|traceback|critical)\b/.test(t)) return 'log-err';
    if (/\b(warning|warn)\b/.test(t)) return 'log-warn';
    if (/\b(info)\b/.test(t)) return 'log-info';
    if (/\b(debug)\b/.test(t)) return 'log-debug';
    return '';
  }

  function highlight(text, filter) {
    if (!filter) return escHtml(text);
    const re = new RegExp(`(${filter.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
    return escHtml(text).replace(re, '<mark class="log-hi">$1</mark>');
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function debounce(fn, delay) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), delay);
    };
  }

  function applyLogFilter() {
    const filter = filterInput.value.trim();
    const normalizedFilter = filter.toLowerCase();
    output.querySelectorAll('.log-line').forEach((line) => {
      const rawText = line._rawText ?? line.textContent ?? '';
      const match = !normalizedFilter || rawText.toLowerCase().includes(normalizedFilter);
      line.style.display = match ? '' : 'none';
      if (match) line.innerHTML = highlight(rawText, filter) + '\n';
    });
  }

  function appendLine(text) {
    const filter = filterInput.value.trim();
    const normalizedText = text.toLowerCase();
    const match = !filter || normalizedText.includes(filter.toLowerCase());

    const span = document.createElement('span');
    span.className = 'log-line ' + classify(text);
    span._rawText = text;
    span.style.display = match ? '' : 'none';
    span.innerHTML = highlight(text, match ? filter : '') + '\n';
    output.appendChild(span);

    while (output.children.length > MAX_LINES) output.removeChild(output.firstChild);
    if (autoScroll.checked) output.scrollTop = output.scrollHeight;
  }

  function clearOutput() { output.innerHTML = ''; }

  async function loadHistory() {
    const lines  = linesSelect.value;
    const source = sourceSelect ? sourceSelect.value : 'journal';
    clearOutput();
    try {
      const resp = await fetch(`/api/logs/history?lines=${lines}&source=${source}`);
      const text = await resp.text();
      text.split('\n').forEach(l => { if (l) appendLine(l); });
    } catch (e) {
      appendLine(`[ERROR] Could not load logs: ${e}`);
    }
  }

  function tailStreamUrl() {
    const source = sourceSelect ? sourceSelect.value : 'journal';
    return `/api/logs/stream?source=${encodeURIComponent(source)}`;
  }

  function startTail() {
    if (evtSource) return;
    tailRequested = true;
    tailConnected = false;
    evtSource = new EventSource(tailStreamUrl());
    evtSource.onopen = () => {
      tailConnected = true;
    };
    evtSource.onmessage = (e) => {
      if (!e.data) return;
      try {
        const line = JSON.parse(e.data);
        if (line) appendLine(line);
      } catch {
        appendLine(e.data);
      }
    };
    evtSource.onerror = () => {
      if (!evtSource || !tailRequested) return;
      if (!tailConnected || evtSource.readyState === EventSource.CLOSED) {
        appendLine('[stream disconnected — click Start Tail to reconnect]');
        stopTail();
      }
    };
    tailBtn.classList.add('hidden');
    stopBtn.classList.remove('hidden');
  }

  function stopTail() {
    tailRequested = false;
    tailConnected = false;
    if (evtSource) { evtSource.close(); evtSource = null; }
    tailBtn.classList.remove('hidden');
    stopBtn.classList.add('hidden');
  }

  tailBtn.addEventListener('click', startTail);
  stopBtn.addEventListener('click', stopTail);
  refreshBtn.addEventListener('click', loadHistory);
  clearBtn.addEventListener('click', clearOutput);
  if (sourceSelect) sourceSelect.addEventListener('change', async () => {
    const restartTail = Boolean(evtSource);
    if (restartTail) stopTail();
    await loadHistory();
    if (restartTail) startTail();
  });

  // Re-apply filter live
  filterInput.addEventListener('input', debounce(applyLogFilter, 150));

  loadServiceLogs = () => {
    if (!historyLoaded) {
      historyLoaded = true;
      loadHistory();
    }
  };

  // Expose loadHistory so update handler can switch to install log after failure
  window._logsLoadHistory = loadHistory;
  window._logsSetSource   = (src) => { if (sourceSelect) sourceSelect.value = src; };
})();

document.getElementById('spoke-acme-dns-provider')?.addEventListener('change', toggleSpokeAcmeDnsSection);

document.querySelectorAll('[data-clienttype]').forEach(button => {
  button.addEventListener('click', () => setClientTypeFilter(button.dataset.clienttype || 'all'));
});


  })();
}

// ════════════════════════════════════════════════════════════════
// HUB — booted after /api/init mode detection
// ════════════════════════════════════════════════════════════════
function startHubApp() {
  (function () {

"use strict";

let authToken = sessionStorage.getItem("hub_token") || null;
let currentUser = null;
let currentTenantId = null;
let tenants = [];
let spokeCache = {};
let activeSpokeModal = null;
let ws = null;
let wsReconnectTimer = null;
let activeTab = "dashboard";
let autoRefreshTimer = null;
let autoRefreshCountdownTimer = null;
let autoRefreshSecondsLeft = 10;
let refreshPaused = false;
let tenantContextActive = false;
const autoRefreshActiveTabs = new Set(["dashboard", "simulations", "clients", "central", "vm-server", "api-server", "spokes"]);
let tenantDetailState = { open: false, tenantId: null, activeTab: "dashboard", data: {} };
const hubAdminTabIds = new Set(["dashboard", "spokes", "setup", "superadmin"]);
let tenantUserCounts = {};
let dashboardTenantRows = [];
let aggregateDashboardData = null;
let aggregateClientRows = [];
let aggregateProxmoxHosts = [];
let _hubNodeRefreshTimer = null;
let aggregateFleetRecloneStatus = null;
let aggregateUsbProvisioningStatus = null;
let aggregateApiServerRows = [];
let aggregateCentralData = null;
let hubCentralData = null;
let hubCentralSiteStatus = {};
let hubCentralWirelessClients = {};
let hubCentralHardwareAlerts = [];
let hubClientCountStatus = {};
let hubCentralAvailableChecks = { alerts: [], insights: [], hardware: [] };
let hubCentralActiveSubtab = "hcs-sites";
let hubTenantSetupActiveSubtab = "ts-setup";
let hubCentralSiteOpen = null;
let hubConfigDraft = "";
let hubCentralSitesConfigDraft = null;
let hubCentralSitesConfigSavedKey = "";
let hubConfigActiveSubtab = "api";
let hubSimulationConfState = { tenantId: null, loaded: false, loading: false, rawContent: "", sha: "", fetchedAt: "", sections: {}, sectionOrder: [], keyOrder: {}, error: "" };
let hubSimActiveTab = "hub-simtop-checks";
let hubSimChecksFilter = "failing";
let hubSimChecksSearch = "";
let hubSimOpenCheckId = null;
let hubHwOpenCheckId = null;
let hubCcOpenWsite = null;
const hubClientUiState = { search: "", status: "all", expandedByTenant: {}, seenSitesByTenant: {} };
let hubVmServerSelectedSpoke = null;
let hubVmServerFleetPollTimer = null;
let hubVmServerFleetConcurrencyDraft = 3;
let hubVmServerFleetConcurrencyTenant = null;
let hubVmConsoleState = { spokeId: null, vmid: null, vmtype: "qemu", vmname: "", sessionId: "", status: "idle", error: "", rfb: null, connectToken: 0, missingCredentials: false };
let hubNoVNCRfbPromise = null;
let hubSpokeShellState = { spokeId: null, socket: null, terminal: null, fitAddon: null, resizeObserver: null, status: "idle", error: "", connectToken: 0 };
let hubSpokeShellDepsPromise = null;
const HUB_SPOKE_SHELL_XTERM_JS = 'https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js';
const HUB_SPOKE_SHELL_XTERM_CSS = 'https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css';
const HUB_SPOKE_SHELL_FIT_JS = 'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js';
let hubProxmoxCredentialsModalState = { spokeId: null, fallbackHost: "" };
let hubClientTypeFilter = "all";
const tenantDashboardSort = { key: "name", direction: "asc" };

const PROCESSING_FEATURES = ["aruba_polling", "teams_webhook", "email", "heartbeat", "gkill", "schedules", "repo_sync"];
const SPOKE_CONFIG_FIELD_GROUPS = [
  {
    title: "Proxmox Settings",
    fields: [
      { id: "proxmox_host", label: "Proxmox host" },
      { id: "proxmox_user", label: "Proxmox user" },
      { id: "proxmox_node", label: "Proxmox node" },
      { id: "vm_template_id", label: "VM Template ID", keys: ["vm_template_id", "vm_image_1_template_id"], saveKey: "vm_image_1_template_id" },
      { id: "proxmox_password", label: "Proxmox password", type: "secret" },
    ],
  },
  {
    title: "USB / Device Settings",
    fields: [
      { id: "usb_vidpids", label: "USB certified VID:PIDs", type: "vidpid-list", help: "One entry per line: vidpid type label" },
      { id: "usb_ignored_vidpids", label: "USB ignored VID:PIDs", type: "vidpid-list", help: "One entry per line: vidpid type label" },
      { id: "usb_auto_provision", label: "USB auto-provision", type: "toggle" },
    ],
  },
  {
    title: "Reclone Settings",
    fields: [
      { id: "reclone_target_count", label: "Reclone target count", type: "number", keys: ["reclone_target_count", "reclone_concurrency"], saveKey: "reclone_concurrency" },
      { id: "reclone_schedule_enabled", label: "Reclone schedule enabled", type: "toggle" },
      { id: "reclone_schedule", label: "Reclone schedule", keys: ["reclone_schedule", "reclone_schedule_cron"], saveKey: "reclone_schedule_cron" },
    ],
  },
  {
    title: "System",
    fields: [
      { id: "label", label: "Spoke label/name", fallback: spoke => spoke?.label || spoke?.spoke_name || spoke?.hostname || "" },
      { id: "spoke_tls", label: "TLS enabled", type: "toggle" },
      { id: "relay_enabled", label: "Relay enabled", type: "toggle" },
    ],
  },
];
const SPOKE_CONFIG_FIELDS = SPOKE_CONFIG_FIELD_GROUPS.flatMap(group => group.fields);
let activeSpokeConfigRequestId = 0;
const spokeUiState = { expandedByTenant: {}, search: "" };
const renderTokens = {};
const scheduledReloads = {};
let superadminBackupConfig = null;
const superadminBackupState = {
  open: false,
  activeTab: "backup",
  step: "confirm",
  loading: false,
  configLoading: false,
  configSaving: false,
  configMessage: "",
  configMessageOk: true,
  backupError: "",
  selectedSpokeId: "",
  jobId: "",
  vmProgress: {},
  completionNotified: false,
};
const HUB_AUTH_DEFAULTS = {
  auth_provider: "local",
  auth_ldap_url: "",
  auth_ldap_bind_dn: "",
  auth_ldap_bind_password_configured: false,
  auth_ldap_user_base: "",
  auth_ldap_user_filter: "(&(objectClass=user)(sAMAccountName={username}))",
  auth_ldap_group_superadmin: "",
  auth_ldap_group_tenant_admin: "",
  auth_ldap_tenant_id: "",
  auth_radius_host: "",
  auth_radius_port: 1812,
  auth_radius_secret_configured: false,
  auth_radius_role_attr: "Filter-Id",
  auth_radius_superadmin_val: "superadmin",
  auth_tacacs_host: "",
  auth_tacacs_port: 49,
  auth_tacacs_secret_configured: false,
  auth_tacacs_superadmin_priv: 15,
  auth_default_role: "superadmin",
};
let hubAuthConfig = { ...HUB_AUTH_DEFAULTS };
let hubAuthConfigLoaded = false;

const HUB_RESEED_VM_ID = 100;
const hubReseedState = {
  tenantId: null,
  loading: false,
  templatesLoading: false,
  templates: [],
  templatesError: "",
  selectedTemplateKey: "",
  selectedSpokeIds: [],
  submitting: false,
  error: "",
  step: "select",
  jobId: "",
  progressTemplateName: "",
  progressRows: {},
  completionNotified: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function hubAuthEl(id) {
  return document.getElementById(id);
}

function escHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

function relativeTime(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Math.max(0, Date.now() - then);
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return fmtDate(iso);
}

function showToast(message, level = "ok") {
  const container = $("#toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  const cls = level === "ok" ? "success" : level === "warn" ? "error" : "error";
  toast.className = `settings-message ${cls}`;
  toast.textContent = message;
  toast.style.cssText = "min-width:240px;max-width:420px;box-shadow:0 4px 16px rgba(0,0,0,0.15);cursor:pointer;";
  toast.addEventListener("click", () => toast.remove());
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

function isOnline(lastSeenIso) {
  if (!lastSeenIso) return false;
  const ts = new Date(lastSeenIso).getTime();
  return !Number.isNaN(ts) && Date.now() - ts < 300000;
}

function statusDot(online) {
  return `<span class="status-dot ${online ? "online" : "offline"}"></span>`;
}

function updateGkillBadge(value) {
  const badge = $("#gkill-badge");
  if (!badge) return;
  const on = ["on", "true", "1", "enabled"].includes(String(value || "").toLowerCase());
  badge.classList.toggle("hidden", !on);
  badge.textContent = on ? "⚠ GKILL ON" : "GKILL OFF";
}

function updateApiStatus(online, text) {
  const dot = $("#api-dot");
  const label = $("#api-text");
  if (dot) dot.className = `status-dot ${online ? "online" : "offline"}`;
  if (label) label.textContent = text;
}

function setFormMessage(id, message, ok = true) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message || "";
  el.className = `form-msg ${message ? (ok ? "msg-ok" : "msg-error") : ""}`.trim();
}

function tenantName(tenantId) {
  const tenant = tenants.find(item => item.id === tenantId);
  if (tenant?.name) return tenant.name;
  const roleTenant = currentUser?.tenant_roles?.find(role => role.tenant_id === tenantId);
  return roleTenant?.tenant_name || roleTenant?.name || tenantId;
}

function normalizeTenantRole(role) {
  const value = String(role || "").toLowerCase();
  return value === "operator" ? "viewer" : value;
}

function currentRoleForTenant(tenantId = currentTenantId) {
  if (!currentUser) return "";
  if (currentUser.is_superadmin) return "superadmin";
  return normalizeTenantRole(currentUser.tenant_roles.find(role => role.tenant_id === tenantId)?.role || "");
}

function canManageTenant(tenantId = currentTenantId) {
  if (!currentUser || !tenantId) return false;
  return currentUser.is_superadmin || currentRoleForTenant(tenantId) === "admin";
}

function getTenantMeta(tenantId) {
  return tenants.find(item => item.id === tenantId) || { id: tenantId, name: tenantId };
}

function hasMeaningfulValue(value) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function formatInlineValue(value) {
  if (!hasMeaningfulValue(value)) return "—";
  if (Array.isArray(value)) return value.map(item => formatInlineValue(item)).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function uniqueValues(values = []) {
  const seen = new Set();
  return values.filter(value => {
    const key = JSON.stringify(value);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function getSpokeClients(spoke) {
  return Array.isArray(spoke?.telemetry?.clients) ? spoke.telemetry.clients : [];
}

function getSpokeProxmoxSummary(spoke) {
  const telemetry = spoke?.telemetry || {};
  return telemetry.proxmox || telemetry.proxmox_summary || telemetry.proxmox_status || telemetry.server || {};
}

function getSpokeVmCount(spoke) {
  const proxmox = getSpokeProxmoxSummary(spoke);
  const vmCount = Number(proxmox.vm_count);
  if (Number.isFinite(vmCount)) return vmCount;
  return Array.isArray(proxmox.vms) ? proxmox.vms.length : 0;
}

function getSpokeRunningVmCount(spoke) {
  const proxmox = getSpokeProxmoxSummary(spoke);
  const runningCount = Number(proxmox.running_count);
  if (Number.isFinite(runningCount)) return runningCount;
  return Array.isArray(proxmox.vms) ? proxmox.vms.filter(vm => vm.status === "running").length : 0;
}

function getSpokeUsbCount(spoke) {
  const proxmox = getSpokeProxmoxSummary(spoke);
  const usbCount = Number(proxmox.usb_count);
  if (Number.isFinite(usbCount)) return usbCount;
  return Array.isArray(proxmox.usb_state) ? proxmox.usb_state.length : 0;
}

function summarizeTenantSpokes(spokes = []) {
  const approved = spokes.filter(spoke => spoke.status === "approved");
  const onlineCount = approved.filter(spoke => isOnline(spoke.last_seen)).length;
  const lastSeenTimes = approved
    .map(spoke => new Date(spoke.last_seen).getTime())
    .filter(value => Number.isFinite(value));
  return {
    totalCount: spokes.length,
    approvedCount: approved.length,
    pendingCount: Math.max(0, spokes.length - approved.length),
    onlineCount,
    offlineCount: Math.max(0, approved.length - onlineCount),
    clientCount: approved.reduce((sum, spoke) => sum + getSpokeClients(spoke).length, 0),
    vmCount: approved.reduce((sum, spoke) => sum + getSpokeVmCount(spoke), 0),
    runningVmCount: approved.reduce((sum, spoke) => sum + getSpokeRunningVmCount(spoke), 0),
    usbCount: approved.reduce((sum, spoke) => sum + getSpokeUsbCount(spoke), 0),
    lastSync: lastSeenTimes.length ? new Date(Math.max(...lastSeenTimes)).toISOString() : null,
  };
}

function renderTenantSummaryPills(summary) {
  return `
    <span class="stat-pill">${summary.approvedCount} spokes</span>
    <span class="stat-pill">${summary.onlineCount} online</span>
    <span class="stat-pill">${summary.clientCount} clients</span>
    <span class="stat-pill">${summary.vmCount} VMs</span>
    <span class="stat-pill">${summary.runningVmCount} running</span>
  `;
}

function getActiveTenantId() {
  return tenantDetailState.open ? tenantDetailState.tenantId : currentTenantId;
}

function aggregateEndpoint(path) {
  const tenantId = getActiveTenantId();
  return tenantId
    ? `/api/aggregate/${path}?tenant_id=${encodeURIComponent(tenantId)}`
    : `/api/aggregate/${path}`;
}

async function loadAggregateData(path) {
  if (!currentUser || !getActiveTenantId()) return null;
  const res = await apiFetch(aggregateEndpoint(path));
  if (!res || !res.ok) return null;
  return res.json();
}

async function loadAggregateDataForTenant(tenantId, path) {
  if (!currentUser || !tenantId) return null;
  const res = await apiFetch(`/api/aggregate/${path}?tenant_id=${encodeURIComponent(tenantId)}`);
  if (!res || !res.ok) return null;
  return res.json();
}

function summarizeTenantAlerts(summary, aggregate) {
  const checks = aggregate?.checks_summary || {};
  const alertCount = Number(checks.fail || 0) + Number(checks.warning || 0);
  if (alertCount > 0) {
    return { tone: "alert", text: `${alertCount} active ${alertCount === 1 ? "alert" : "alerts"}` };
  }
  if (summary.offlineCount > 0) {
    return { tone: "alert", text: `${summary.offlineCount} offline ${summary.offlineCount === 1 ? "spoke" : "spokes"}` };
  }
  return { tone: "ok", text: "OK" };
}

function renderTenantCard(cardData) {
  const { id, name, summary, alert } = cardData;
  const row = document.createElement("tr");
  row.className = "tenant-list-row";
  row.dataset.enterTenant = id;
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.innerHTML = `
    <td>${statusDot(summary.onlineCount > 0 || summary.approvedCount === 0)}</td>
    <td><strong>${escHtml(name || id)}</strong><div class="tenant-card-subtitle">${escHtml(id)}</div></td>
    <td>${summary.approvedCount}</td>
    <td>${summary.clientCount}</td>
    <td>${summary.vmCount}</td>
    <td>${escHtml(relativeTime(summary.lastSync))}</td>
    <td><span class="tenant-alert-pill ${alert.tone}">${escHtml(alert.text)}</span></td>
    <td class="tenant-card-cta">Open →</td>
  `;
  return row;
}

function renderTenantDashboardEmptyState() {
  const canAddTenant = Boolean(currentUser?.is_superadmin);
  return canAddTenant
    ? 'No tenants yet. Create your first tenant to get started.<div class="tenant-empty-action"><button class="btn btn-primary btn-small" data-add-tenant type="button">Add Tenant</button></div>'
    : 'No tenants are available yet. Contact a hub administrator to add one.';
}

function hubSimulationBadgeClass(simulation) {
  if (FAILURE_SIMS.has(simulation)) return "badge badge-failure";
  if (TRAFFIC_SIMS.has(simulation)) return "badge badge-traffic";
  return "badge badge-grey";
}

function hubImpactSummary(activeSimulations = []) {
  const labels = [...new Set((activeSimulations || []).map(sim => IMPACT_LABELS[sim]).filter(Boolean))];
  return labels.length ? labels.join(" · ") : "— Normal";
}

function simulationStatusBadge(status) {
  const normalized = String(status || "").toLowerCase();
  const cls = normalized.includes("running") ? "online" : normalized.includes("offline") ? "offline" : "warning";
  return `<span class="site-status-pill ${cls}">${escHtml(status || "Unknown")}</span>`;
}

function simulationStatusSortValue(status) {
  return String(status || "").toLowerCase().includes("running") ? 0 : 1;
}

function renderHubSimulationBadges(simulations = [], emptyLabel = "—") {
  const uniqueSimulations = [...new Set((simulations || []).filter(Boolean))]
    .sort((left, right) => String(left).localeCompare(String(right), undefined, { sensitivity: "base" }));
  if (uniqueSimulations.length) {
    return `<div class="badge-list">${uniqueSimulations.map(sim => `<span class="${hubSimulationBadgeClass(sim)}">${escHtml(sim)}</span>`).join("")}</div>`;
  }
  return emptyLabel ? `<span class="muted">${escHtml(emptyLabel)}</span>` : "";
}

function spokeDisplayName(spoke = {}, fallback = "—") {
  return String(spoke?.spoke_name || spoke?.spoke_hostname || spoke?.hostname || spoke?.id || spoke?.spoke_id || fallback);
}

function hubClientSiteName(row = {}) {
  return spokeDisplayName(row, "Unknown site");
}

function hubClientSiteKey(row = {}) {
  return String(row.spoke_id || row.id || row.spoke_hostname || row.spoke_name || "unknown-site");
}

function primeHubClientExpandedSet(siteKeys = [], tenantId = currentTenantId) {
  if (!tenantId) return;
  if (!hubClientUiState.seenSitesByTenant[tenantId]) hubClientUiState.seenSitesByTenant[tenantId] = new Set();
  const seen = hubClientUiState.seenSitesByTenant[tenantId];
  const expanded = getHubClientExpandedSet(tenantId);
  // Auto-expand any site that hasn't been seen before (preserves user's explicit collapses)
  siteKeys.filter(Boolean).forEach(key => {
    if (!seen.has(key)) {
      expanded.add(key);
      seen.add(key);
    }
  });
}

function normalizeAggregateClientRows(data) {
  if (Array.isArray(data?.clients)) return data.clients;
  if (Array.isArray(data?.rows)) return data.rows;
  return Array.isArray(data) ? data : [];
}

function getHubClientExpandedSet(tenantId = currentTenantId) {
  if (!tenantId) return new Set();
  if (!hubClientUiState.expandedByTenant[tenantId]) hubClientUiState.expandedByTenant[tenantId] = new Set();
  return hubClientUiState.expandedByTenant[tenantId];
}

function classifyHubClient(client = {}) {
  if (client?.has_usb) return 't2';
  return Array.isArray(client?.usb_devices) && client.usb_devices.length ? 't2' : 't1';
}

function syncHubClientTypeTabs() {
  document.querySelectorAll('[data-hubclienttype]').forEach((button) => {
    button.classList.toggle('active', button.dataset.hubclienttype === hubClientTypeFilter);
  });
}

function updateHubClientTypeCounts(allClients = aggregateClientRows) {
  const counts = { all: allClients.length, t1: 0, t2: 0 };
  allClients.forEach((client) => {
    counts[classifyHubClient(client)] += 1;
  });
  const countAll = document.getElementById('hub-client-type-count-all');
  const countT1 = document.getElementById('hub-client-type-count-t1');
  const countT2 = document.getElementById('hub-client-type-count-t2');
  const countT3 = document.getElementById('hub-client-type-count-t3');
  if (countAll) countAll.textContent = String(counts.all);
  if (countT1) countT1.textContent = String(counts.t1);
  if (countT2) countT2.textContent = String(counts.t2);
  if (countT3) countT3.textContent = '—';
}

function setHubClientTypeFilter(nextFilter = 'all') {
  hubClientTypeFilter = nextFilter === 't1' || nextFilter === 't2' ? nextFilter : 'all';
  syncHubClientTypeTabs();
  renderClientRowsForHub();
}

function toggleHubSiteExpand(siteKey) {
  const normalizedKey = String(siteKey || "");
  if (!normalizedKey) return;
  const expanded = getHubClientExpandedSet();
  if (expanded.has(normalizedKey)) expanded.delete(normalizedKey);
  else expanded.add(normalizedKey);
  renderClientRowsForHub();
}
window.toggleHubSiteExpand = toggleHubSiteExpand;

function renderDashboardAggregate(data) {
  const hardwareRows = Object.entries(data.hardware_breakdown || {}).map(([name, count]) => `
    <tr><td>${escHtml(name)}</td><td>${count}</td></tr>
  `).join("");
  const checks = data.checks_summary || {};
  return `
    <div class="tenant-metrics-grid">
      <article class="tenant-metric-card"><span class="tenant-metric-label">Total Clients</span><strong class="tenant-metric-value">${data.client_count ?? 0}</strong><span class="tenant-metric-hint">Across approved spokes</span></article>
      <article class="tenant-metric-card"><span class="tenant-metric-label">Spoke Health</span><strong class="tenant-metric-value">${data.spokes_online ?? 0} / ${data.spokes_total ?? 0}</strong><span class="tenant-metric-hint">Online within last 300s</span></article>
      <article class="tenant-metric-card"><span class="tenant-metric-label">Checks Passing</span><strong class="tenant-metric-value">${checks.pass ?? 0}</strong><span class="tenant-metric-hint">Warnings ${checks.warning ?? 0} · Failing ${checks.fail ?? 0}</span></article>
      <article class="tenant-metric-card"><span class="tenant-metric-label">Hardware Types</span><strong class="tenant-metric-value">${Object.keys(data.hardware_breakdown || {}).length}</strong><span class="tenant-metric-hint">Observed client platforms</span></article>
    </div>
    <div class="tenant-detail-grid">
      <section class="setup-card">
        <div class="setup-card-header"><h2>Hardware Breakdown</h2><p>Client totals per reported hardware type for this tenant.</p></div>
        <table class="data-table">
          <thead><tr><th>Hardware Type</th><th>Count</th></tr></thead>
          <tbody>${hardwareRows || '<tr><td colspan="2" class="empty-state">No client hardware reported.</td></tr>'}</tbody>
        </table>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Checks Summary</h2><p>Roll-up across stored spoke telemetry in this tenant.</p></div>
        <table class="data-table">
          <tbody>
            <tr><td>Pass</td><td>${checks.pass ?? 0}</td></tr>
            <tr><td>Fail</td><td>${checks.fail ?? 0}</td></tr>
            <tr><td>Warning</td><td>${checks.warning ?? 0}</td></tr>
            <tr><td>Spokes Online</td><td>${data.spokes_online ?? 0} of ${data.spokes_total ?? 0}</td></tr>
          </tbody>
        </table>
      </section>
    </div>
  `;
}

const HUB_STATUS_PRIORITY = { fail: 3, warning: 2, degraded: 3, no_data: 2, pass: 1, ok: 1 };
const hubSimTopPanels = ["hub-simtop-checks", "hub-simtop-hardware", "hub-simtop-clients"];

function hubWorstStatus(a, b) {
  const pa = HUB_STATUS_PRIORITY[String(a || "").toLowerCase()] || 0;
  const pb = HUB_STATUS_PRIORITY[String(b || "").toLowerCase()] || 0;
  if (pb > pa) return b;
  return a || b || "unknown";
}

function hubStatusLabel(status) {
  return String(status || "unknown").replace(/_/g, " ").toUpperCase();
}

function hubCheckStatusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "fail" || s === "degraded") return "sim-fail";
  if (s === "warning" || s === "no_data") return "sim-warn";
  if (s === "pass" || s === "ok") return "sim-pass";
  return "sim-unknown";
}

function hubCheckDotClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "fail" || s === "degraded") return "dot-err";
  if (s === "warning" || s === "no_data") return "dot-warn";
  if (s === "pass" || s === "ok") return "dot-ok";
  return "dot-unknown";
}

function hubFormatCheckTs(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "";
}

function hubAggregateChecks(spokes) {
  const byCheckId = new Map();
  for (const spoke of (spokes || [])) {
    const statusMap = spoke?.central_status?.status || {};
    for (const [wsite, checks] of Object.entries(statusMap)) {
      if (!checks || typeof checks !== "object") continue;
      for (const [checkId, info] of Object.entries(checks)) {
        if (!info || typeof info !== "object") continue;
        if (!byCheckId.has(checkId)) {
          byCheckId.set(checkId, {
            check_id: checkId,
            check_name: info.check_name || checkId,
            check_type: info.check_type || "simulation",
            worst_status: info.status || "unknown",
            ts: info.ts || null,
            details: [],
          });
        }
        const agg = byCheckId.get(checkId);
        agg.worst_status = hubWorstStatus(agg.worst_status, info.status);
        if (!agg.ts || (info.ts && info.ts > agg.ts)) agg.ts = info.ts;
        agg.details.push({
          spoke_id: spoke.spoke_id,
          spoke_name: spoke.spoke_name || spoke.spoke_id || "Unknown spoke",
          spoke_online: spoke.spoke_online,
          wsite,
          status: info.status || "unknown",
          count: Number(info.count || 0),
          ts: info.ts || null,
        });
      }
    }
  }
  return [...byCheckId.values()].sort((left, right) => {
    const statusDiff = (HUB_STATUS_PRIORITY[String(right.worst_status || "").toLowerCase()] || 0)
      - (HUB_STATUS_PRIORITY[String(left.worst_status || "").toLowerCase()] || 0);
    if (statusDiff) return statusDiff;
    return String(left.check_name || left.check_id).localeCompare(String(right.check_name || right.check_id), undefined, { sensitivity: "base" });
  });
}

function hubAggregateHardware(spokes) {
  const byId = new Map();
  for (const spoke of (spokes || [])) {
    const hwAlerts = spoke?.central_status?.hardware_alerts || [];
    for (const alert of hwAlerts) {
      if (!alert || !alert.id) continue;
      if (!byId.has(alert.id)) {
        byId.set(alert.id, {
          id: alert.id,
          name: alert.name || alert.id,
          device_type: alert.device_type || "",
          total: 0,
          spoke_breakdown: [],
        });
      }
      const agg = byId.get(alert.id);
      agg.total += Number(alert.total || 0);
      agg.spoke_breakdown.push({
        spoke_id: spoke.spoke_id,
        spoke_name: spoke.spoke_name || spoke.spoke_id || "Unknown spoke",
        spoke_online: spoke.spoke_online,
        total: Number(alert.total || 0),
        sites: alert.sites || {},
      });
    }
  }
  return [...byId.values()].sort((left, right) => right.total - left.total || String(left.name || left.id).localeCompare(String(right.name || right.id), undefined, { sensitivity: "base" }));
}

function hubAggregateClientCount(spokes) {
  const byWsite = new Map();
  for (const spoke of (spokes || [])) {
    const ccStatus = spoke?.central_status?.client_count_status || {};
    for (const [wsite, info] of Object.entries(ccStatus)) {
      if (!info || typeof info !== "object") continue;
      if (!byWsite.has(wsite)) {
        byWsite.set(wsite, {
          wsite,
          site_name: info.site_name || wsite,
          worst_status: info.status || "unknown",
          ts: info.ts || null,
          spoke_breakdown: [],
        });
      }
      const agg = byWsite.get(wsite);
      agg.worst_status = hubWorstStatus(agg.worst_status, info.status);
      if (!agg.ts || (info.ts && info.ts > agg.ts)) agg.ts = info.ts;
      agg.spoke_breakdown.push({
        spoke_id: spoke.spoke_id,
        spoke_name: spoke.spoke_name || spoke.spoke_id || "Unknown spoke",
        spoke_online: spoke.spoke_online,
        current: info.current,
        hourly_avg: info.hourly_avg,
        drop_pct: info.drop_pct,
        status: info.status || "unknown",
        ts: info.ts || null,
        baseline_stale: Boolean(info.baseline_stale),
      });
    }
  }
  return [...byWsite.values()].sort((left, right) => {
    const statusDiff = (HUB_STATUS_PRIORITY[String(right.worst_status || "").toLowerCase()] || 0)
      - (HUB_STATUS_PRIORITY[String(left.worst_status || "").toLowerCase()] || 0);
    if (statusDiff) return statusDiff;
    return String(left.site_name || left.wsite).localeCompare(String(right.site_name || right.wsite), undefined, { sensitivity: "base" });
  });
}

function activateHubSimTopTab(tabId = "hub-simtop-checks") {
  document.querySelectorAll(".hub-simtop-subtab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.hubsimtop === tabId);
  });
  hubSimTopPanels.forEach((id) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    panel.classList.toggle("active", id === tabId);
    panel.classList.toggle("hidden", id !== tabId);
  });
  hubSimActiveTab = tabId;
  if (tabId === "hub-simtop-checks") renderHubSimChecksList();
  if (tabId === "hub-simtop-hardware") renderHubHwPanel();
  if (tabId === "hub-simtop-clients") renderHubCcPanel();
}

function renderHubSimChecksList() {
  const container = document.getElementById("hub-sim-checks-list");
  const emptyEl = document.getElementById("hub-sim-checks-empty");
  if (!container) return;

  container.textContent = "";
  if (emptyEl) {
    emptyEl.textContent = "No check data reported by any spoke.";
    emptyEl.classList.add("hidden");
    container.appendChild(emptyEl);
  }

  const allChecks = hubAggregateChecks(hubCentralData?.spokes || []);
  const failing = allChecks.filter((check) => String(check.worst_status || "").toLowerCase() === "fail").length;
  const warning = allChecks.filter((check) => String(check.worst_status || "").toLowerCase() === "warning").length;
  const functional = allChecks.filter((check) => ["pass", "ok"].includes(String(check.worst_status || "").toLowerCase())).length;
  const countById = (id, count) => {
    const el = document.getElementById(id);
    if (el) el.textContent = count;
  };
  countById("hub-sim-tab-failing-count", failing);
  countById("hub-sim-tab-warning-count", warning);
  countById("hub-sim-tab-functional-count", functional);

  let filtered = allChecks;
  if (hubSimChecksFilter === "failing") filtered = allChecks.filter((check) => String(check.worst_status || "").toLowerCase() === "fail");
  else if (hubSimChecksFilter === "warning") filtered = allChecks.filter((check) => String(check.worst_status || "").toLowerCase() === "warning");
  else if (hubSimChecksFilter === "functional") filtered = allChecks.filter((check) => ["pass", "ok"].includes(String(check.worst_status || "").toLowerCase()));

  if (hubSimChecksSearch) {
    const q = hubSimChecksSearch.toLowerCase();
    filtered = filtered.filter((check) => {
      const detailText = check.details.map((detail) => `${detail.spoke_name} ${detail.wsite}`).join(" ").toLowerCase();
      return String(check.check_name || check.check_id).toLowerCase().includes(q)
        || String(check.check_type || "").toLowerCase().includes(q)
        || detailText.includes(q);
    });
  }

  const totalCount = document.getElementById("hub-checks-count");
  if (totalCount) totalCount.textContent = `${filtered.length} check${filtered.length === 1 ? "" : "s"}`;

  if (!filtered.length) {
    if (emptyEl) {
      emptyEl.textContent = allChecks.length ? "No checks match the current filter." : "No check data reported by any spoke.";
      emptyEl.classList.remove("hidden");
    }
    return;
  }

  for (const check of filtered) {
    const row = document.createElement("div");
    const uniqueSpokes = [...new Set(check.details.map((detail) => detail.spoke_name).filter(Boolean))];
    row.className = "check-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.innerHTML = `
      <span class="check-dot ${hubCheckDotClass(check.worst_status)}"></span>
      <span class="check-name">${escHtml(check.check_name || check.check_id)}</span>
      <span class="check-badge ${hubCheckStatusClass(check.worst_status)}">${escHtml(hubStatusLabel(check.worst_status))}</span>
      <span class="check-detail">${uniqueSpokes.length} spoke${uniqueSpokes.length === 1 ? "" : "s"} · ${check.details.length} site${check.details.length === 1 ? "" : "s"}</span>
      <span class="check-ts">${escHtml(hubFormatCheckTs(check.ts))}</span>
    `;
    row.addEventListener("click", () => openHubSimDetail(check.check_id));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openHubSimDetail(check.check_id);
      }
    });
    container.appendChild(row);
  }
}

function openHubSimDetail(checkId) {
  const check = hubAggregateChecks(hubCentralData?.spokes || []).find((item) => item.check_id === checkId);
  const overview = document.getElementById("hub-sim-overview");
  const detail = document.getElementById("hub-sim-detail");
  const title = document.getElementById("hub-sim-detail-title");
  const sub = document.getElementById("hub-sim-detail-sub");
  const badge = document.getElementById("hub-sim-detail-badge");
  const siteList = document.getElementById("hub-sim-site-list");
  if (!check || !overview || !detail || !siteList) return;
  hubSimOpenCheckId = checkId;
  overview.classList.add("hidden");
  detail.classList.remove("hidden");
  if (title) title.textContent = check.check_name || check.check_id;
  if (sub) sub.textContent = `${check.details.length} spoke/site result${check.details.length === 1 ? "" : "s"}`;
  if (badge) {
    badge.textContent = hubStatusLabel(check.worst_status);
    badge.className = `sim-status-badge ${hubCheckStatusClass(check.worst_status)}`;
  }
  siteList.textContent = "";
  check.details
    .slice()
    .sort((left, right) => {
      const statusDiff = (HUB_STATUS_PRIORITY[String(right.status || "").toLowerCase()] || 0)
        - (HUB_STATUS_PRIORITY[String(left.status || "").toLowerCase()] || 0);
      if (statusDiff) return statusDiff;
      return `${left.spoke_name} ${left.wsite}`.localeCompare(`${right.spoke_name} ${right.wsite}`, undefined, { sensitivity: "base" });
    })
    .forEach((detailItem) => {
      const row = document.createElement("div");
      row.className = "sim-site-row";
      row.style.display = "flex";
      row.style.alignItems = "center";
      row.style.justifyContent = "space-between";
      row.style.gap = "8px";
      row.style.padding = "8px 12px";
      row.style.cursor = "default";
      row.innerHTML = `
        <span class="sim-site-name">${escHtml(detailItem.spoke_name)} — ${escHtml(detailItem.wsite)}</span>
        <span style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;">
          <span style="font-size:0.82rem;color:var(--muted);">${detailItem.count} event${detailItem.count === 1 ? "" : "s"}</span>
          <span class="sim-status-badge ${hubCheckStatusClass(detailItem.status)}">${escHtml(hubStatusLabel(detailItem.status))}</span>
        </span>
      `;
      siteList.appendChild(row);
    });
}

function renderHubHwPanel() {
  const container = document.getElementById("hub-hw-checks-list");
  if (!container) return;
  container.textContent = "";
  const hwChecks = hubAggregateHardware(hubCentralData?.spokes || []);
  if (!hwChecks.length) {
    container.innerHTML = '<div class="central-empty">No hardware alerts data from any spoke.</div>';
    return;
  }
  for (const hw of hwChecks) {
    const row = document.createElement("div");
    const siteCount = hw.spoke_breakdown.reduce((sum, spoke) => sum + Object.keys(spoke.sites || {}).length, 0);
    row.className = "check-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.innerHTML = `
      <span class="check-dot ${hw.total > 0 ? "dot-err" : "dot-ok"}"></span>
      <span class="check-name">${escHtml(hw.name)}</span>
      <span class="check-badge ${hw.total > 0 ? "sim-fail" : "sim-pass"}">${escHtml(hw.total > 0 ? `${hw.total} DOWN` : "CLEAR")}</span>
      <span class="check-detail">${hw.spoke_breakdown.length} spoke${hw.spoke_breakdown.length === 1 ? "" : "s"} · ${siteCount} site${siteCount === 1 ? "" : "s"}</span>
      <span class="check-ts"></span>
    `;
    row.addEventListener("click", () => openHubHwDetail(hw.id));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openHubHwDetail(hw.id);
      }
    });
    container.appendChild(row);
  }
}

function openHubHwDetail(checkId) {
  const hw = hubAggregateHardware(hubCentralData?.spokes || []).find((item) => item.id === checkId);
  const overview = document.getElementById("hub-hw-overview");
  const detail = document.getElementById("hub-hw-detail");
  const title = document.getElementById("hub-hw-detail-title");
  const sub = document.getElementById("hub-hw-detail-sub");
  const badge = document.getElementById("hub-hw-detail-badge");
  const siteList = document.getElementById("hub-hw-site-list");
  if (!hw || !overview || !detail || !siteList) return;
  hubHwOpenCheckId = checkId;
  overview.classList.add("hidden");
  detail.classList.remove("hidden");
  if (title) title.textContent = hw.name;
  if (sub) sub.textContent = hw.total > 0 ? `${hw.total} device(s) affected across ${hw.spoke_breakdown.length} spoke${hw.spoke_breakdown.length === 1 ? "" : "s"}` : "No active alerts";
  if (badge) {
    badge.textContent = hw.total > 0 ? `${hw.total} DOWN` : "CLEAR";
    badge.className = `sim-status-badge ${hw.total > 0 ? "sim-fail" : "sim-pass"}`;
  }
  siteList.textContent = "";
  if (!hw.spoke_breakdown.length) {
    siteList.innerHTML = '<div class="central-empty">No device breakdown available.</div>';
    return;
  }
  hw.spoke_breakdown
    .slice()
    .sort((left, right) => Number(right.total || 0) - Number(left.total || 0) || String(left.spoke_name || "").localeCompare(String(right.spoke_name || ""), undefined, { sensitivity: "base" }))
    .forEach((spoke) => {
      const spokeRow = document.createElement("div");
      spokeRow.className = "sim-site-row";
      spokeRow.style.display = "flex";
      spokeRow.style.flexDirection = "column";
      spokeRow.style.gap = "6px";
      spokeRow.style.padding = "8px 12px";
      spokeRow.style.cursor = "default";
      spokeRow.innerHTML = `<strong>${escHtml(spoke.spoke_name)}</strong><span style="font-size:0.82rem;color:var(--muted);">${spoke.total} device(s) affected</span>`;
      siteList.appendChild(spokeRow);
      const siteEntries = Object.entries(spoke.sites || {});
      if (!siteEntries.length) {
        const empty = document.createElement("div");
        empty.className = "sim-site-row";
        empty.style.display = "flex";
        empty.style.justifyContent = "space-between";
        empty.style.padding = "8px 12px 8px 24px";
        empty.style.cursor = "default";
        empty.innerHTML = '<span class="sim-site-name">No site breakdown</span><span style="font-size:0.82rem;color:var(--muted);">—</span>';
        siteList.appendChild(empty);
        return;
      }
      siteEntries
        .sort((left, right) => String(left[1]?.site_name || left[0]).localeCompare(String(right[1]?.site_name || right[0]), undefined, { sensitivity: "base" }))
        .forEach(([wsite, info]) => {
          const devices = Array.isArray(info?.devices) ? info.devices : [];
          const siteRow = document.createElement("div");
          siteRow.className = "sim-site-row";
          siteRow.style.display = "flex";
          siteRow.style.justifyContent = "space-between";
          siteRow.style.alignItems = "center";
          siteRow.style.gap = "8px";
          siteRow.style.padding = "8px 12px 8px 24px";
          siteRow.style.cursor = "default";
          siteRow.innerHTML = `
            <span class="sim-site-name">${escHtml(info?.site_name || wsite)}</span>
            <span class="sim-status-badge sim-fail">${devices.length} device${devices.length === 1 ? "" : "s"}</span>
          `;
          siteList.appendChild(siteRow);
        });
    });
}

function renderHubCcPanel() {
  const container = document.getElementById("hub-cc-checks-list");
  if (!container) return;
  container.textContent = "";
  const ccData = hubAggregateClientCount(hubCentralData?.spokes || []);
  if (!ccData.length) {
    container.innerHTML = '<div class="central-empty">No client count data from any spoke.</div>';
    return;
  }
  for (const site of ccData) {
    const row = document.createElement("div");
    row.className = "check-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.innerHTML = `
      <span class="check-dot ${hubCheckDotClass(site.worst_status)}"></span>
      <span class="check-name">${escHtml(site.site_name || site.wsite)}</span>
      <span class="check-badge ${hubCheckStatusClass(site.worst_status)}">${escHtml(hubStatusLabel(site.worst_status))}</span>
      <span class="check-detail">${site.spoke_breakdown.length} spoke${site.spoke_breakdown.length === 1 ? "" : "s"} reporting</span>
      <span class="check-ts">${escHtml(hubFormatCheckTs(site.ts))}</span>
    `;
    row.addEventListener("click", () => openHubCcDetail(site.wsite));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openHubCcDetail(site.wsite);
      }
    });
    container.appendChild(row);
  }
}

function openHubCcDetail(wsite) {
  const site = hubAggregateClientCount(hubCentralData?.spokes || []).find((item) => item.wsite === wsite);
  const overview = document.getElementById("hub-cc-overview");
  const detail = document.getElementById("hub-cc-detail");
  const title = document.getElementById("hub-cc-detail-title");
  const sub = document.getElementById("hub-cc-detail-sub");
  const badge = document.getElementById("hub-cc-detail-badge");
  const siteDetail = document.getElementById("hub-cc-site-detail");
  if (!site || !overview || !detail || !siteDetail) return;
  hubCcOpenWsite = wsite;
  overview.classList.add("hidden");
  detail.classList.remove("hidden");
  if (title) title.textContent = site.site_name || site.wsite;
  if (sub) sub.textContent = `${site.spoke_breakdown.length} spoke${site.spoke_breakdown.length === 1 ? "" : "s"} reporting`;
  if (badge) {
    badge.textContent = hubStatusLabel(site.worst_status);
    badge.className = `sim-status-badge ${hubCheckStatusClass(site.worst_status)}`;
  }
  siteDetail.textContent = "";
  site.spoke_breakdown
    .slice()
    .sort((left, right) => {
      const statusDiff = (HUB_STATUS_PRIORITY[String(right.status || "").toLowerCase()] || 0)
        - (HUB_STATUS_PRIORITY[String(left.status || "").toLowerCase()] || 0);
      if (statusDiff) return statusDiff;
      return String(left.spoke_name || "").localeCompare(String(right.spoke_name || ""), undefined, { sensitivity: "base" });
    })
    .forEach((spoke) => {
      const dropValue = Number(spoke.drop_pct);
      const avgValue = Number(spoke.hourly_avg);
      const drop = Number.isFinite(dropValue) ? formatClientCountDelta(dropValue) : "—";
      const avg = Number.isFinite(avgValue) ? avgValue.toFixed(1) : "—";
      const row = document.createElement("div");
      row.className = "sim-site-row";
      row.style.display = "flex";
      row.style.justifyContent = "space-between";
      row.style.alignItems = "center";
      row.style.gap = "8px";
      row.style.padding = "8px 12px";
      row.style.cursor = "default";
      row.innerHTML = `
        <span>
          <span class="sim-site-name">${escHtml(spoke.spoke_name)}</span>
          <span style="display:block;font-size:0.82rem;color:var(--muted);margin-top:4px;">Current: ${spoke.current ?? "—"} / Avg: ${avg} / Δ: ${drop}${spoke.baseline_stale ? " · baseline stale" : ""}</span>
        </span>
        <span class="sim-status-badge ${hubCheckStatusClass(spoke.status)}">${escHtml(hubStatusLabel(spoke.status))}</span>
      `;
      siteDetail.appendChild(row);
    });
}

function updateClientSpokeFilterOptions() {
  const select = $("#hub-clients-spoke-filter");
  if (!select) return;
  const currentValue = select.value || "all";
  const options = [...new Set(aggregateClientRows.map(row => hubClientSiteName(row)).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
  select.innerHTML = '<option value="all">All spokes</option>' + options.map(name => `<option value="${escHtml(name)}">${escHtml(name)}</option>`).join("");
  select.value = options.includes(currentValue) ? currentValue : "all";
}

function renderClientRowsForHub() {
  const container = $("#hub-clients-sites-list");
  if (!container) return;
  syncHubClientTypeTabs();
  updateHubClientTypeCounts(aggregateClientRows);
  const search = hubClientUiState.search.trim().toLowerCase();
  const rows = aggregateClientRows.filter(client => {
    const typeMatch = hubClientTypeFilter === "all" || classifyHubClient(client) === hubClientTypeFilter;
    if (!typeMatch) return false;
    const statusMatch = hubClientUiState.status === "all"
      || (hubClientUiState.status === "online" && client.online)
      || (hubClientUiState.status === "offline" && !client.online);
    if (!statusMatch) return false;
    if (!search) return true;
    const haystack = [
      client.hostname,
      hubClientSiteName(client),
      client.platform,
      client.hw_type,
      client.simulation_id,
      client.connected_ssid,
      ...(client.active_simulations || []),
    ].join(" ").toLowerCase();
    return haystack.includes(search);
  });
  const onlineCount = rows.filter(client => client.online).length;
  const groupedRows = new Map();
  rows.forEach(client => {
    const siteKey = hubClientSiteKey(client);
    if (!groupedRows.has(siteKey)) groupedRows.set(siteKey, { name: hubClientSiteName(client), clients: [] });
    groupedRows.get(siteKey).clients.push(client);
  });
  const sites = [...groupedRows.entries()]
    .map(([siteKey, group]) => {
      const clients = [...group.clients].sort((left, right) => {
        const onlineDiff = Number(Boolean(right.online)) - Number(Boolean(left.online));
        if (onlineDiff) return onlineDiff;
        return String(left.hostname || "").localeCompare(String(right.hostname || ""), undefined, { sensitivity: "base" });
      });
      return {
        siteKey,
        name: group.name,
        clients,
        onlineCount: clients.filter(client => client.online).length,
        errorCount: clients.reduce((sum, client) => sum + Number(client.error_count || 0), 0),
        activeSimulations: [...new Set(clients.flatMap(client => client.active_simulations || []).filter(Boolean))].sort((left, right) => left.localeCompare(right)),
      };
    })
    .sort((left, right) => left.name.localeCompare(right.name, undefined, { sensitivity: "base" }));
  $("#hub-clients-pill") && ($("#hub-clients-pill").textContent = `${rows.length} clients`);
  $("#hub-clients-online-pill") && ($("#hub-clients-online-pill").textContent = `${onlineCount} online`);
  $("#hub-clients-spokes-pill") && ($("#hub-clients-spokes-pill").textContent = `${sites.length} spokes`);
  if (!sites.length) {
    container.innerHTML = `<div class="empty-state">${aggregateClientRows.length ? "No clients match the current filters." : "No client telemetry reported for this tenant."}</div>`;
    return;
  }
  primeHubClientExpandedSet(sites.map(site => site.siteKey));
  const expanded = getHubClientExpandedSet();
  container.innerHTML = sites.map(site => {
    const isExpanded = expanded.has(site.siteKey);
    const encodedSiteKey = encodeURIComponent(site.siteKey);
    return `
      <section class="hub-client-site-group">
        <button class="hub-client-site-header" type="button" onclick="toggleHubSiteExpand(decodeURIComponent('${encodedSiteKey}'))" aria-expanded="${isExpanded ? "true" : "false"}">
          <span class="hub-client-site-name">${escHtml(site.name)}</span>
          <span class="badge badge-grey">${site.clients.length} clients</span>
          <span class="badge badge-green">${site.onlineCount} online</span>
          ${site.errorCount > 0 ? `<span class="badge badge-red">${site.errorCount} errors</span>` : ""}
          <span class="hub-client-site-simulations">${renderHubSimulationBadges(site.activeSimulations, "")}</span>
          <span class="hub-client-site-chevron" aria-hidden="true">${isExpanded ? "▼" : "▶"}</span>
        </button>
        ${isExpanded ? `
          <div class="hub-client-site-rows">
            <div class="table-scroll">
              <table class="data-table hub-client-site-table">
                <thead><tr><th>Status</th><th>Hostname</th><th>Platform</th><th>SSID</th><th>Active Simulations</th><th style="white-space:nowrap">Last Seen</th><th>Errors</th></tr></thead>
                <tbody>
                  ${site.clients.map(client => `
                    <tr>
                      <td class="status-cell">${statusDot(Boolean(client.online))}</td>
                      <td class="hostname-cell">${escHtml(client.hostname || "—")}</td>
                      <td>${escHtml(client.platform || client.hw_type || "—")}</td>
                      <td>${escHtml(client.connected_ssid || "—")}</td>
                      <td>${renderHubSimulationBadges(client.active_simulations || [])}</td>
                      <td class="nowrap-cell"><span title="${escHtml(fmtDate(client.last_seen))}">${escHtml(relativeTime(client.last_seen))}</span></td>
                      <td>${Number(client.error_count || 0)}</td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
            </div>
          </div>
        ` : ""}
      </section>
    `;
  }).join("");
}

function buildTenantUserCounts(users = []) {
  return users.reduce((counts, user) => {
    (user.tenant_roles || []).forEach(role => {
      counts[role.tenant_id] = (counts[role.tenant_id] || 0) + 1;
    });
    return counts;
  }, {});
}

function getTenantUserCount(tenantId) {
  return Object.prototype.hasOwnProperty.call(tenantUserCounts, tenantId) ? tenantUserCounts[tenantId] : null;
}

function compareTenantDashboardValues(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a || "").localeCompare(String(b || ""), undefined, { numeric: true, sensitivity: "base" });
}

function tenantDashboardSortValue(row, key) {
  if (key === "approvedCount") return row.summary?.approvedCount ?? row.approvedSpokes ?? -1;
  if (key === "clientCount") return row.summary?.clientCount ?? -1;
  if (key === "vmCount") return row.summary?.vmCount ?? -1;
  if (key === "approvedSpokes" || key === "userCount") return row[key] ?? -1;
  if (key === "lastSync") {
    const v = row.summary?.lastSync ?? row.lastSync;
    return v ? new Date(v).getTime() : 0;
  }
  if (key === "createdAt") return row.createdAt ? new Date(row.createdAt).getTime() : 0;
  return String(row[key] || "").toLowerCase();
}

function sortDashboardTenantRows(rows) {
  const { key, direction } = tenantDashboardSort;
  const sorted = [...rows].sort((left, right) => compareTenantDashboardValues(
    tenantDashboardSortValue(left, key),
    tenantDashboardSortValue(right, key),
  ));
  return direction === "desc" ? sorted.reverse() : sorted;
}

function renderDashboardTenantSortHeader(label, key) {
  const active = tenantDashboardSort.key === key;
  const indicator = active ? (tenantDashboardSort.direction === "asc" ? "▲" : "▼") : "↕";
  const ariaSort = active ? (tenantDashboardSort.direction === "asc" ? "ascending" : "descending") : "none";
  return `<button class="tenant-table-sort${active ? " active" : ""}" data-dashboard-tenant-sort="${escHtml(key)}" aria-sort="${ariaSort}" type="button"><span>${escHtml(label)}</span><span class="tenant-table-sort-indicator" aria-hidden="true">${indicator}</span></button>`;
}

function renderDashboardTenantTable(rows) {
  const sortedRows = sortDashboardTenantRows(rows);
  const body = sortedRows.length ? sortedRows.map(row => {
    const summary = row.summary || {};
    const alert = row.alert || {};
    const lastSync = summary.lastSync ?? row.lastSync;
    return `
    <tr class="tenant-list-row" data-enter-tenant="${escHtml(row.id)}" tabindex="0" role="button">
      <td>${statusDot((summary.onlineCount ?? 0) > 0 || (summary.approvedCount ?? 0) === 0)}</td>
      <td><strong>${escHtml(row.name || row.id)}</strong><div class="tenant-card-subtitle">${escHtml(row.id)}</div></td>
      <td>${summary.approvedCount ?? row.approvedSpokes ?? '—'}</td>
      <td>${summary.clientCount ?? '—'}</td>
      <td>${summary.vmCount ?? '—'}</td>
      <td>${lastSync ? escHtml(relativeTime(lastSync)) : '<span class="muted">—</span>'}</td>
      <td>${alert.text ? `<span class="tenant-alert-pill ${alert.tone}">${escHtml(alert.text)}</span>` : ''}</td>
      <td class="tenant-card-cta">Open →</td>
    </tr>
  `;}).join("") : '<tr><td colspan="8" class="empty-state">No tenants available.</td></tr>';
  return `
    <section class="setup-card tenant-list-card">
      <div class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th></th>
              <th>${renderDashboardTenantSortHeader("Tenant", "name")}</th>
              <th>${renderDashboardTenantSortHeader("Spokes", "approvedCount")}</th>
              <th>${renderDashboardTenantSortHeader("Clients", "clientCount")}</th>
              <th>${renderDashboardTenantSortHeader("VMs", "vmCount")}</th>
              <th>${renderDashboardTenantSortHeader("Last Sync", "lastSync")}</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </section>
  `;
}

function setTenantDetailVisible(open) {
  $("#hub-dashboard-overview")?.classList.toggle("hidden", open);
  $("#hub-tenant-detail")?.classList.toggle("hidden", !open);
}

function resetTenantDetail() {
  tenantDetailState.open = false;
  tenantDetailState.tenantId = null;
  tenantDetailState.activeTab = "dashboard";
  setTenantDetailVisible(false);
  updateHubRefreshPausedState();
}

function scheduleReload(key, callback, delay = 250) {
  if (scheduledReloads[key]) clearTimeout(scheduledReloads[key]);
  scheduledReloads[key] = window.setTimeout(() => {
    scheduledReloads[key] = null;
    callback();
  }, delay);
}

async function refreshAfterSpokeApproval(tenantId = currentTenantId) {
  const refresh = async () => {
    if (currentUser?.is_superadmin) {
      await loadSuperadmin();
    } else if (tenantId && canManageTenant(tenantId)) {
      await loadTenantPendingSpokes();
    }

    if (tenantId) {
      await ensureTenantSpokesFor(tenantId, true);
    }
    await loadDashboard(true);
    if (tenantId === currentTenantId) {
      await loadHubSimulations(true);
      await loadClients(true);
      await loadVmServer(true);
      await loadApiServer(true);
      await loadCentral(true);
      await loadSpokes(true);
      await loadTenantSetup(true);
      await loadConfig(true);
      if (activeTab === "reseed") await loadHubReseedPanel(true);
    }
    if (tenantDetailState.open && tenantDetailState.tenantId === tenantId) {
      const data = await loadTenantDetailData(true);
      renderTenantDetail(data);
    }
    if (activeSpokeModal?.tenant_id === tenantId) {
      const latest = getSpokeFromCache(tenantId, activeSpokeModal.spoke.id);
      if (latest) activeSpokeModal.spoke = latest;
      renderSpokeClientsTab();
    }
  };

  await refresh();
  [1500, 5000].forEach((delay, index) => {
    scheduleReload(`approval-refresh-${tenantId || "all"}-${index}`, () => {
      refresh().catch(() => {});
    }, delay);
  });
}

async function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  const init = { ...options, headers };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  if (init.body && !(init.body instanceof FormData) && typeof init.body !== "string") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(url, init).catch(() => null);
  if (!response) {
    updateApiStatus(false, "Disconnected");
    return null;
  }
  updateApiStatus(true, "Connected");
  if (response.status === 401 && authToken) {
    logout(false);
    return null;
  }
  return response;
}

async function readJson(response) {
  if (!response) return null;
  return response.json().catch(() => null);
}

function formatBackupBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "-";
  if (value >= 1024 ** 4) return `${(value / (1024 ** 4)).toFixed(1)} TB`;
  if (value >= 1024 ** 3) return `${(value / (1024 ** 3)).toFixed(1)} GB`;
  if (value >= 1024 ** 2) return `${(value / (1024 ** 2)).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function parseBackupVmIds(value) {
  return Array.from(new Set(String(value || "")
    .split(",")
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => Number.parseInt(item, 10))
    .filter(item => Number.isFinite(item))));
}

function listSuperadminBackupSpokes() {
  return tenants.flatMap(tenant => (spokeCache[tenant.id] || [])
    .filter(spoke => spoke.status === "approved")
    .map(spoke => ({ ...spoke, tenant_id: spoke.tenant_id || tenant.id })))
    .sort((left, right) => {
      const tenantCmp = tenantName(left.tenant_id).localeCompare(tenantName(right.tenant_id), undefined, { numeric: true, sensitivity: "base" });
      if (tenantCmp !== 0) return tenantCmp;
      return spokePrimaryLabel(left).localeCompare(spokePrimaryLabel(right), undefined, { numeric: true, sensitivity: "base" });
    });
}

function listScopedSuperadminBackupSpokes(tenantId = currentTenantId) {
  const allSpokes = listSuperadminBackupSpokes();
  if (!tenantId) return allSpokes;
  const scoped = allSpokes.filter(spoke => spoke.tenant_id === tenantId);
  return scoped.length ? scoped : allSpokes;
}

function getSelectedSuperadminBackupSpoke() {
  const spokes = listScopedSuperadminBackupSpokes();
  if (superadminBackupState.selectedSpokeId) {
    const selected = spokes.find(spoke => spoke.id === superadminBackupState.selectedSpokeId);
    if (selected) return selected;
  }
  const preferred = spokes.find(spoke => spoke.tenant_id === currentTenantId) || spokes[0] || null;
  superadminBackupState.selectedSpokeId = preferred?.id || "";
  return preferred;
}

function getSuperadminBackupVmIds(spokeId = superadminBackupState.selectedSpokeId) {
  const values = superadminBackupConfig?.spokes?.[spokeId]?.vm_ids;
  return Array.isArray(values) ? values.map(value => Number.parseInt(value, 10)).filter(value => Number.isFinite(value)) : [];
}

function getSuperadminBackupRows() {
  const configured = getSuperadminBackupVmIds();
  const knownIds = Object.keys(superadminBackupState.vmProgress);
  const ordered = Array.from(new Set([...configured.map(String), ...knownIds])).sort((left, right) => Number(left) - Number(right));
  return ordered.map(id => {
    const row = superadminBackupState.vmProgress[id] || {};
    return {
      vm_id: row.vm_id ?? (Number.parseInt(id, 10) || id),
      status: String(row.status || "queued").toLowerCase(),
      pct: Number.isFinite(Number(row.pct)) ? Number(row.pct) : null,
      size: row.size,
      file: row.file || "",
    };
  });
}

function isSuperadminBackupComplete() {
  const rows = getSuperadminBackupRows();
  return rows.length > 0 && rows.every(row => ["done", "error"].includes(row.status));
}

function backupStatusMeta(status) {
  const value = String(status || "queued").toLowerCase();
  if (value === "done") return { label: "✅ done", className: "status-online" };
  if (value === "error") return { label: "❌ error", className: "status-offline" };
  if (value === "running") return { label: "⏳ running", className: "status-unknown" };
  return { label: "⌛ queued", className: "status-unknown" };
}

function ensureSuperadminBackupUi() {
  if (!document.getElementById("hub-superadmin-btn")) {
    document.body.insertAdjacentHTML("beforeend", `<button id="hub-superadmin-btn" title="Superadmin" style="
  position:fixed; bottom:18px; right:18px; z-index:9999;
  background:none; border:none; cursor:pointer; font-size:18px; opacity:0.3;
  transition:opacity 0.2s;
" onmouseenter="this.style.opacity='1'" onmouseleave="this.style.opacity='0.3'">🔒</button>`);
  }
  if (!document.getElementById("sa-backup-modal")) {
    document.body.insertAdjacentHTML("beforeend", `
      <div id="sa-backup-modal" class="modal-overlay hidden" role="dialog" aria-modal="true" aria-labelledby="sa-backup-modal-title">
        <div class="modal-box modal-box-large">
          <div class="modal-header">
            <h2 id="sa-backup-modal-title">Upload Template to Azure</h2>
            <button id="sa-backup-modal-x" class="btn btn-secondary btn-small" type="button">✕ Close</button>
          </div>
          <nav class="setup-subnav" style="margin-top:8px;">
            <button class="setup-subtab sa-backup-tab active" data-sa-backup-tab="backup" type="button">Upload</button>
            <button class="setup-subtab sa-backup-tab" data-sa-backup-tab="config" type="button">⚙️ Config</button>
          </nav>
          <div id="sa-backup-modal-body" class="setup-subpanel" style="margin-top:8px;"></div>
        </div>
      </div>
    `);
  }
}

function syncSuperadminBackupAccess() {
  const btn = document.getElementById("hub-superadmin-btn");
  if (!currentUser?.is_superadmin || !authToken) {
    btn?.remove();
    if (document.getElementById("sa-backup-modal")) closeSuperadminBackupModal(true);
    return;
  }
  ensureSuperadminBackupUi();
}

function renderSuperadminBackupModal() {
  ensureSuperadminBackupUi();
  const modal = document.getElementById("sa-backup-modal");
  const body = document.getElementById("sa-backup-modal-body");
  const title = document.getElementById("sa-backup-modal-title");
  const closeX = document.getElementById("sa-backup-modal-x");
  if (!modal || !body || !title || !closeX) return;

  modal.classList.toggle("hidden", !superadminBackupState.open);
  if (!superadminBackupState.open) return;

  $$(".sa-backup-tab").forEach(button => button.classList.toggle("active", button.dataset.saBackupTab === superadminBackupState.activeTab));
  const canClose = superadminBackupState.step !== "status" || isSuperadminBackupComplete();
  closeX.disabled = !canClose;

  if (superadminBackupState.activeTab === "config") {
    title.textContent = "Upload Configuration";
    const configRows = listSuperadminBackupSpokes();
    body.innerHTML = `
      <div class="setup-card">
        <div class="setup-card-header">
          <h3>Superadmin Upload Settings</h3>
          <p>Configure VM IDs per spoke and Azure destination settings for template uploads.</p>
        </div>
        ${superadminBackupState.configLoading ? '<div class="empty-state">Loading upload configuration…</div>' : `
          <div class="form-group">
            <label class="form-label" for="sa-backup-config-retention">Retention count</label>
            <input id="sa-backup-config-retention" type="number" min="1" class="form-input" value="${escHtml(superadminBackupConfig?.retention ?? 3)}">
          </div>
          <div class="form-group">
            <label class="form-label" for="sa-backup-config-account">Azure account name</label>
            <input id="sa-backup-config-account" type="text" class="form-input" value="${escHtml(superadminBackupConfig?.azure_account || "")}">
          </div>
          <div class="form-group">
            <label class="form-label" for="sa-backup-config-container">Azure container</label>
            <input id="sa-backup-config-container" type="text" class="form-input" value="${escHtml(superadminBackupConfig?.azure_container || "")}">
          </div>
          <div class="table-scroll-v" style="max-height:45vh;margin-top:8px;">
            <table class="data-table">
              <thead><tr><th>Tenant</th><th>Spoke</th><th>VM IDs</th></tr></thead>
              <tbody>${configRows.length ? configRows.map(spoke => `
                <tr>
                  <td>${escHtml(tenantName(spoke.tenant_id))}</td>
                  <td><strong>${escHtml(spokePrimaryLabel(spoke))}</strong><div class="tenant-card-subtitle">${escHtml(spoke.id)}</div></td>
                  <td><input class="form-input form-input-sm sa-backup-config-vms" data-spoke-id="${escHtml(spoke.id)}" type="text" value="${escHtml(getSuperadminBackupVmIds(spoke.id).join(", "))}" placeholder="100, 101, 102"></td>
                </tr>
              `).join("") : '<tr><td colspan="3" class="empty-state">No approved spokes found.</td></tr>'}</tbody>
            </table>
          </div>
          <div id="sa-backup-config-msg" class="form-msg ${superadminBackupState.configMessage ? (superadminBackupState.configMessageOk ? "msg-ok" : "msg-error") : ""}">${escHtml(superadminBackupState.configMessage)}</div>
          <div class="form-actions" style="margin-top:10px;">
            <button id="sa-backup-config-save-btn" class="btn btn-primary" type="button"${superadminBackupState.configSaving ? " disabled" : ""}>Save</button>
          </div>
        `}
      </div>
    `;
    return;
  }

  if (superadminBackupState.step === "status") {
    const rows = getSuperadminBackupRows();
    title.textContent = "Upload in Progress";
    body.innerHTML = `
      <div class="setup-card">
        <div class="setup-card-header">
          <h3>Upload in Progress</h3>
          <p>Job ID: ${escHtml(superadminBackupState.jobId || "pending")}</p>
        </div>
        <div class="table-scroll-v" style="margin-top:8px;">
          <table class="data-table">
            <thead><tr><th>VM ID</th><th>Status</th><th>Progress</th><th>Size</th><th>File</th></tr></thead>
            <tbody>${rows.length ? rows.map(row => {
              const meta = backupStatusMeta(row.status);
              return `<tr>
                <td>${escHtml(row.vm_id)}</td>
                <td><span class="status-badge ${meta.className}">${meta.label}</span></td>
                <td>${row.pct === null ? "-" : `${escHtml(row.pct)}%`}</td>
                <td>${escHtml(formatBackupBytes(row.size))}</td>
                <td>${escHtml(row.file || "-")}</td>
              </tr>`;
            }).join("") : '<tr><td colspan="5" class="empty-state">Waiting for upload updates…</td></tr>'}</tbody>
          </table>
        </div>
        <div class="form-actions" style="margin-top:10px;">
          <button id="sa-backup-close-btn" class="btn btn-secondary" type="button"${canClose ? "" : " disabled"}>Close</button>
        </div>
      </div>
    `;
    return;
  }

  const spokes = listScopedSuperadminBackupSpokes();
  const selectedSpoke = getSelectedSuperadminBackupSpoke();
  const vmIds = getSuperadminBackupVmIds(selectedSpoke?.id);
  const proceedDisabled = !selectedSpoke || !vmIds.length || superadminBackupState.loading || superadminBackupState.configLoading;
  if (superadminBackupState.step === "key") {
    title.textContent = "Enter Azure Storage Key";
    body.innerHTML = `
      <div class="setup-card">
        <div class="setup-card-header">
          <h3>Enter Azure Storage Key</h3>
          <p>${selectedSpoke ? `${escHtml(tenantName(selectedSpoke.tenant_id))} — ${escHtml(spokePrimaryLabel(selectedSpoke))}` : "No spoke selected."}</p>
        </div>
        <p>Key will be used for this upload only and never stored.</p>
        <input type="password" id="sa-azure-key" placeholder="Azure storage account key" class="form-input" style="width:100%">
        <div class="form-actions" style="margin-top:10px;">
          <button id="sa-backup-back-btn" class="btn btn-secondary" type="button">← Back</button>
          <button id="sa-backup-start-btn" class="btn btn-primary" type="button"${superadminBackupState.loading ? " disabled" : ""}>Start Upload</button>
        </div>
        <div class="form-msg ${superadminBackupState.backupError ? "msg-error" : ""}">${escHtml(superadminBackupState.backupError)}</div>
      </div>
    `;
    document.getElementById("sa-azure-key")?.focus();
    return;
  }

  title.textContent = "Upload Template to Azure";
  body.innerHTML = `
    <div class="setup-card">
      <div class="setup-card-header">
        <h3>Upload Template to Azure</h3>
        <p>Select source spoke to upload:</p>
      </div>
      <div class="form-group">
        <label class="form-label" for="sa-spoke-select">Spoke</label>
        <select id="sa-spoke-select" class="form-input">
          ${spokes.length ? spokes.map(spoke => `<option value="${escHtml(spoke.id)}"${selectedSpoke?.id === spoke.id ? " selected" : ""}>${escHtml(tenantName(spoke.tenant_id))} — ${escHtml(spokePrimaryLabel(spoke))}</option>`).join("") : '<option value="">No approved spokes available</option>'}
        </select>
      </div>
      <div class="setup-card" style="margin-top:8px;">
        <div><strong>VMs configured:</strong> ${vmIds.length ? escHtml(vmIds.join(", ")) : "—"}</div>
        <div style="margin-top:6px;"><strong>Azure:</strong> ${escHtml(superadminBackupConfig?.azure_account || "—")} / ${escHtml(superadminBackupConfig?.azure_container || "—")}</div>
        <div style="margin-top:6px;"><strong>Retention:</strong> keep last ${escHtml(superadminBackupConfig?.retention ?? "—")}</div>
      </div>
      <div class="form-actions" style="margin-top:10px;">
        <button id="sa-backup-cancel-btn" class="btn btn-secondary" type="button">Cancel</button>
        <button id="sa-backup-proceed-btn" class="btn btn-primary" type="button"${proceedDisabled ? " disabled" : ""}>Proceed →</button>
      </div>
      <div class="form-msg ${superadminBackupState.backupError ? "msg-error" : ""}">${escHtml(superadminBackupState.backupError || (!vmIds.length && selectedSpoke ? "No VM IDs configured for this spoke." : ""))}</div>
    </div>
  `;
}

async function loadSuperadminBackupConfig(force = false) {
  if (!currentUser?.is_superadmin) return superadminBackupConfig;
  if (superadminBackupConfig && !force) return superadminBackupConfig;
  superadminBackupState.configLoading = true;
  superadminBackupState.configMessage = "";
  if (superadminBackupState.open) renderSuperadminBackupModal();
  const res = await apiFetch("/api/backup/config");
  const data = await readJson(res);
  if (!res || !res.ok) {
    superadminBackupConfig = superadminBackupConfig || { spokes: {}, retention: 3, azure_account: "", azure_container: "" };
    superadminBackupState.configMessage = data?.detail || "Failed to load upload configuration.";
    superadminBackupState.configMessageOk = false;
  } else {
    superadminBackupConfig = {
      spokes: data?.spokes || {},
      retention: data?.retention ?? 3,
      azure_account: data?.azure_account || "",
      azure_container: data?.azure_container || "",
    };
  }
  superadminBackupState.configLoading = false;
  if (superadminBackupState.open) renderSuperadminBackupModal();
  return superadminBackupConfig;
}

async function openSuperadminBackupModal() {
  if (!currentUser?.is_superadmin) return;
  ensureSuperadminBackupUi();
  superadminBackupState.open = true;
  superadminBackupState.activeTab = "backup";
  superadminBackupState.step = "confirm";
  superadminBackupState.loading = true;
  superadminBackupState.backupError = "";
  superadminBackupState.jobId = "";
  superadminBackupState.vmProgress = {};
  superadminBackupState.completionNotified = false;
  renderSuperadminBackupModal();
  await Promise.all([
    Promise.all(tenants.map(tenant => ensureTenantSpokesFor(tenant.id, false))),
    loadSuperadminBackupConfig(true),
  ]);
  getSelectedSuperadminBackupSpoke();
  superadminBackupState.loading = false;
  renderSuperadminBackupModal();
}

function closeSuperadminBackupModal(force = false) {
  if (!force && superadminBackupState.step === "status" && !isSuperadminBackupComplete()) return;
  superadminBackupState.open = false;
  superadminBackupState.activeTab = "backup";
  superadminBackupState.step = "confirm";
  superadminBackupState.loading = false;
  superadminBackupState.backupError = "";
  superadminBackupState.jobId = "";
  superadminBackupState.vmProgress = {};
  superadminBackupState.completionNotified = false;
  document.getElementById("sa-backup-modal")?.classList.add("hidden");
}

async function saveSuperadminBackupConfig() {
  if (!currentUser?.is_superadmin || superadminBackupState.configSaving) return;
  const retention = Number.parseInt(document.getElementById("sa-backup-config-retention")?.value || "", 10);
  if (!Number.isFinite(retention) || retention < 1) {
    superadminBackupState.configMessage = "Retention count must be at least 1.";
    superadminBackupState.configMessageOk = false;
    renderSuperadminBackupModal();
    return;
  }
  const payload = {
    spokes: JSON.parse(JSON.stringify(superadminBackupConfig?.spokes || {})),
    retention,
    azure_account: document.getElementById("sa-backup-config-account")?.value.trim() || "",
    azure_container: document.getElementById("sa-backup-config-container")?.value.trim() || "",
  };
  document.querySelectorAll(".sa-backup-config-vms").forEach(input => {
    const spokeId = input.dataset.spokeId;
    if (!spokeId) return;
    payload.spokes[spokeId] = { ...(payload.spokes[spokeId] || {}), vm_ids: parseBackupVmIds(input.value) };
  });
  superadminBackupState.configSaving = true;
  superadminBackupState.configMessage = "Saving…";
  superadminBackupState.configMessageOk = true;
  renderSuperadminBackupModal();
  const res = await apiFetch("/api/backup/config", { method: "POST", body: payload });
  const data = await readJson(res);
  superadminBackupState.configSaving = false;
  if (!res || !res.ok) {
    superadminBackupState.configMessage = data?.detail || "Failed to save upload configuration.";
    superadminBackupState.configMessageOk = false;
    renderSuperadminBackupModal();
    return;
  }
  superadminBackupConfig = {
    spokes: data?.spokes || payload.spokes,
    retention: data?.retention ?? payload.retention,
    azure_account: data?.azure_account ?? payload.azure_account,
    azure_container: data?.azure_container ?? payload.azure_container,
  };
  superadminBackupState.configMessage = "Upload configuration saved.";
  superadminBackupState.configMessageOk = true;
  renderSuperadminBackupModal();
}

function updateSuperadminBackupProgress(message) {
  if (!superadminBackupState.jobId || message.job_id !== superadminBackupState.jobId) return;
  const vmId = String(message.vm_id ?? "");
  if (!vmId) return;
  const current = superadminBackupState.vmProgress[vmId] || { vm_id: Number.parseInt(vmId, 10) || vmId, status: "queued", pct: 0, size: null, file: "" };
  superadminBackupState.vmProgress[vmId] = {
    ...current,
    vm_id: message.vm_id ?? current.vm_id,
    status: message.status || current.status,
    pct: Number.isFinite(Number(message.pct)) ? Number(message.pct) : current.pct,
    size: message.size ?? current.size,
    file: message.file ?? current.file,
  };
  if (superadminBackupState.open && superadminBackupState.step === "status") renderSuperadminBackupModal();
  if (!superadminBackupState.completionNotified && isSuperadminBackupComplete()) {
    superadminBackupState.completionNotified = true;
    showToast("Template upload finished.", "ok");
  }
}

async function startSuperadminBackup() {
  const spoke = getSelectedSuperadminBackupSpoke();
  if (!spoke) {
    superadminBackupState.backupError = "Select a spoke first.";
    renderSuperadminBackupModal();
    return;
  }
  const configuredVmIds = getSuperadminBackupVmIds(spoke.id);
  if (!configuredVmIds.length) {
    superadminBackupState.backupError = "No VM IDs configured for this spoke.";
    superadminBackupState.step = "confirm";
    renderSuperadminBackupModal();
    return;
  }
  let keyValue = document.getElementById("sa-azure-key")?.value || "";
  if (!keyValue) {
    superadminBackupState.backupError = "Azure storage account key is required.";
    renderSuperadminBackupModal();
    return;
  }
  superadminBackupState.loading = true;
  superadminBackupState.backupError = "";
  const responsePromise = apiFetch(`/api/backup/trigger/${encodeURIComponent(spoke.tenant_id)}/${encodeURIComponent(spoke.id)}`, {
    method: "POST",
    body: { azure_key: keyValue },
  });
  const keyInput = document.getElementById("sa-azure-key");
  if (keyInput) keyInput.value = "";
  keyValue = "";
  renderSuperadminBackupModal();
  const res = await responsePromise;
  const data = await readJson(res);
  superadminBackupState.loading = false;
  if (!res || !res.ok) {
    superadminBackupState.backupError = data?.detail || "Unable to start upload.";
    renderSuperadminBackupModal();
    return;
  }
  superadminBackupState.step = "status";
  superadminBackupState.jobId = data?.job_id || "";
  superadminBackupState.vmProgress = Object.fromEntries(configuredVmIds.map(vmId => [String(vmId), { vm_id: vmId, status: "queued", pct: 0, size: null, file: "" }]));
  superadminBackupState.completionNotified = false;
  renderSuperadminBackupModal();
  showToast(`Upload started for ${spokePrimaryLabel(spoke)}.`, "ok");
}

function resetHubReseedState({ preserveTemplates = true } = {}) {
  hubReseedState.tenantId = currentTenantId || null;
  hubReseedState.loading = false;
  hubReseedState.submitting = false;
  hubReseedState.error = "";
  hubReseedState.step = "select";
  hubReseedState.jobId = "";
  hubReseedState.progressTemplateName = "";
  hubReseedState.progressRows = {};
  hubReseedState.completionNotified = false;
  hubReseedState.selectedSpokeIds = [];
  if (!preserveTemplates) {
    hubReseedState.templatesLoading = false;
    hubReseedState.templates = [];
    hubReseedState.templatesError = "";
    hubReseedState.selectedTemplateKey = "";
  }
}

function syncHubReseedTenantState() {
  if (hubReseedState.tenantId !== currentTenantId) resetHubReseedState({ preserveTemplates: true });
}

function ensureHubReseedUi() {
  const tenantNav = document.querySelector("#tenant-context-nav .tenant-context-nav-row2");
  if (tenantNav && !tenantNav.querySelector('[data-tab="hub-reseed"]')) {
    const button = document.createElement("button");
    button.className = "tab";
    button.type = "button";
    button.dataset.tab = "hub-reseed";
    button.textContent = "Reseed";
    const anchor = tenantNav.querySelector(".tab-nav-sep");
    tenantNav.insertBefore(button, anchor || null);
  }
  const hubRoot = document.getElementById("hub-root");
  if (hubRoot && !document.getElementById("tab-hub-reseed")) {
    const panel = document.createElement("main");
    panel.id = "tab-hub-reseed";
    panel.className = "page tab-content hidden";
    panel.innerHTML = '<div id="hub-reseed-panel"><div class="empty-state">Loading reseed options…</div></div>';
    const anchor = document.getElementById("tab-hub-config") || document.getElementById("tab-hub-tenant-setup");
    hubRoot.insertBefore(panel, anchor || null);
  }
}

function titleCaseWords(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase()) || "Unknown";
}

function formatBackupTemplateUpdatedAt(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function normalizeBackupTemplates(payload) {
  const items = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.templates)
      ? payload.templates
      : Array.isArray(payload?.items)
        ? payload.items
        : [];
  return items.map((item, index) => {
    const value = typeof item === "string" ? { template_name: item, latest_blob: item } : (item || {});
    const templateName = value.template_name || value.name || value.template || value.label || value.blob_name || `template-${index + 1}`;
    const latestBlob = value.latest_blob || value.blob_name || value.blob || templateName;
    const sizeValue = value.size_bytes ?? value.latest_size_bytes ?? value.latest_blob_size ?? value.bytes ?? value.size ?? null;
    const updatedAt = value.updated_at || value.last_updated || value.modified || value.last_modified || value.created_at || value.timestamp || "";
    return {
      template_name: String(templateName),
      latest_blob: String(latestBlob),
      size_bytes: Number.isFinite(Number(sizeValue)) ? Number(sizeValue) : null,
      updated_at: updatedAt,
    };
  }).filter(item => item.template_name && item.latest_blob).sort((left, right) => {
    const updatedCmp = String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
    if (updatedCmp !== 0) return updatedCmp;
    return left.template_name.localeCompare(right.template_name, undefined, { numeric: true, sensitivity: "base" });
  });
}

function backupTemplateKey(template) {
  if (!template) return "";
  return String(template.latest_blob || template.template_name || "");
}

function renderBackupTemplateOptionLabel(template) {
  const size = template.size_bytes ? ` (${formatBackupBytes(template.size_bytes)})` : "";
  const updated = formatBackupTemplateUpdatedAt(template.updated_at);
  return `${template.template_name}${size}${updated ? ` — updated ${updated}` : ""}`;
}

function getSelectedHubReseedTemplate() {
  if (!hubReseedState.templates.length) return null;
  const selected = hubReseedState.templates.find(template => backupTemplateKey(template) === hubReseedState.selectedTemplateKey);
  if (selected) return selected;
  const fallback = hubReseedState.templates[0] || null;
  hubReseedState.selectedTemplateKey = backupTemplateKey(fallback);
  return fallback;
}

function listTenantReseedSpokes() {
  return getTenantSpokes()
    .filter(spoke => spoke.status === "approved")
    .sort((left, right) => spokePrimaryLabel(left).localeCompare(spokePrimaryLabel(right), undefined, { numeric: true, sensitivity: "base" }));
}

function setHubReseedSelectedSpokeIds(ids = []) {
  const allowed = new Set(listTenantReseedSpokes().map(spoke => spoke.id));
  hubReseedState.selectedSpokeIds = Array.from(new Set(ids.filter(id => allowed.has(id))));
}

function getHubReseedRows() {
  return Object.values(hubReseedState.progressRows).sort((left, right) => {
    const orderDiff = (left.order ?? 0) - (right.order ?? 0);
    if (orderDiff !== 0) return orderDiff;
    return String(left.spoke_name || left.spoke_id || "").localeCompare(String(right.spoke_name || right.spoke_id || ""), undefined, { numeric: true, sensitivity: "base" });
  });
}

function isHubReseedComplete() {
  const rows = getHubReseedRows();
  return rows.length > 0 && rows.every(row => ["done", "error"].includes(String(row.status || "").toLowerCase()));
}

function reseedStatusMeta(status) {
  const value = String(status || "queued").toLowerCase();
  if (value === "done") return { icon: "✅", label: "Done", className: "status-online" };
  if (value === "error") return { icon: "❌", label: "Error", className: "status-offline" };
  if (["queued", "pending", "offline", "offline_queued"].includes(value)) return { icon: "⏱", label: "Queued", className: "status-unknown" };
  if (["downloading", "restoring", "recloning", "running", "in_progress"].includes(value)) {
    return { icon: "⏳", label: titleCaseWords(value), className: "status-unknown" };
  }
  return { icon: "⏳", label: titleCaseWords(value), className: "status-unknown" };
}

async function loadHubReseedTemplates(force = false) {
  if (!currentUser) return hubReseedState.templates;
  if (hubReseedState.templatesLoading) return hubReseedState.templates;
  if (!force && hubReseedState.templates.length) return hubReseedState.templates;
  hubReseedState.templatesLoading = true;
  hubReseedState.templatesError = "";
  if (activeTab === "reseed") renderHubReseedPanel();
  const res = await apiFetch("/api/backup/templates");
  const data = await readJson(res);
  if (!res || !res.ok) {
    hubReseedState.templates = [];
    hubReseedState.selectedTemplateKey = "";
    hubReseedState.templatesError = data?.detail || "Unable to load Azure templates.";
  } else {
    hubReseedState.templates = normalizeBackupTemplates(data);
    const selectedKey = hubReseedState.selectedTemplateKey;
    if (!hubReseedState.templates.find(template => backupTemplateKey(template) === selectedKey)) {
      hubReseedState.selectedTemplateKey = backupTemplateKey(hubReseedState.templates[0]);
    }
  }
  hubReseedState.templatesLoading = false;
  if (activeTab === "reseed") renderHubReseedPanel();
  return hubReseedState.templates;
}

function renderHubReseedPanel() {
  ensureHubReseedUi();
  const container = document.getElementById("hub-reseed-panel");
  if (!container) return;
  if (!currentUser) {
    container.innerHTML = '<div class="empty-state">Sign in to reseed spokes.</div>';
    return;
  }
  if (!currentTenantId) {
    container.innerHTML = '<div class="empty-state">Select a tenant to reseed spokes.</div>';
    return;
  }
  syncHubReseedTenantState();
  const tenantLabel = tenantName(currentTenantId) || currentTenantId;
  const spokes = listTenantReseedSpokes();
  const selectedTemplate = getSelectedHubReseedTemplate();
  if (hubReseedState.step === "progress") {
    const rows = getHubReseedRows();
    const canClose = isHubReseedComplete();
    container.innerHTML = `
      <div class="setup-card">
        <div class="setup-card-header">
          <h2>Reseed Template</h2>
          <p>${escHtml(tenantLabel)}</p>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
          <span class="stat-pill">Template ${escHtml(hubReseedState.progressTemplateName || "—")}</span>
          <span class="stat-pill">Job ${escHtml(hubReseedState.jobId || "pending")}</span>
        </div>
      </div>
      <div class="setup-card" style="margin-top:10px;">
        <div class="setup-card-header">
          <h3>Step 3 — Progress</h3>
          <p>Tracking reseed progress for selected spokes.</p>
        </div>
        <div class="table-scroll-v" style="margin-top:8px;">
          <table class="data-table">
            <thead><tr><th>Spoke</th><th>Template</th><th>Step</th><th>Status</th></tr></thead>
            <tbody>${rows.length ? rows.map(row => {
              const meta = reseedStatusMeta(row.status);
              return `<tr>
                <td><strong>${escHtml(row.spoke_name || row.spoke_id)}</strong><div class="tenant-card-subtitle">${escHtml(row.spoke_id || "")}</div></td>
                <td>${escHtml(row.template_name || "—")}</td>
                <td>${escHtml(row.step || meta.label)}</td>
                <td><span class="status-badge ${meta.className}">${meta.icon} ${escHtml(meta.label)}</span></td>
              </tr>`;
            }).join("") : '<tr><td colspan="4" class="empty-state">Waiting for reseed updates…</td></tr>'}</tbody>
          </table>
        </div>
        <div class="form-actions" style="margin-top:10px;">
          <button id="reseed-close-btn" class="btn btn-secondary" type="button"${canClose ? "" : " disabled"}>Close</button>
        </div>
      </div>
    `;
    return;
  }

  const selectedCount = hubReseedState.selectedSpokeIds.length;
  const startDisabled = !selectedTemplate || !selectedCount || hubReseedState.submitting || hubReseedState.loading;
  container.innerHTML = `
    <div class="setup-card">
      <div class="setup-card-header">
        <h2>Reseed Template</h2>
        <p>${escHtml(tenantLabel)}</p>
      </div>
      <div class="form-group" style="margin-top:8px;">
        <label class="form-label" for="reseed-template-select">Step 1 — Pick template</label>
        <div style="font-size:0.92rem;color:var(--muted);margin-bottom:6px;">Available templates in Azure:</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <select id="reseed-template-select" class="form-input" style="flex:1;min-width:280px;">
            ${hubReseedState.templatesLoading ? '<option value="">Loading…</option>' : hubReseedState.templates.length ? hubReseedState.templates.map(template => `<option value="${escHtml(backupTemplateKey(template))}"${selectedTemplate && backupTemplateKey(template) === backupTemplateKey(selectedTemplate) ? ' selected' : ''}>${escHtml(renderBackupTemplateOptionLabel(template))}</option>`).join("") : '<option value="">No templates available</option>'}
          </select>
          <button id="reseed-refresh-templates-btn" class="btn btn-secondary btn-small" type="button"${hubReseedState.templatesLoading ? " disabled" : ""}>Refresh</button>
        </div>
        <div class="form-msg ${hubReseedState.templatesError ? "msg-error" : ""}">${escHtml(hubReseedState.templatesError)}</div>
      </div>
    </div>
    <div class="setup-card" style="margin-top:10px;">
      <div class="setup-card-header">
        <h3>Step 2 — Pick target spokes</h3>
        <p>Select one or more spokes in this tenant. Offline spokes will queue automatically.</p>
      </div>
      <div class="form-actions" style="justify-content:flex-start;margin-top:12px;">
        <button id="reseed-select-all-btn" class="btn btn-secondary btn-small" type="button">Select All</button>
        <button id="reseed-clear-btn" class="btn btn-secondary btn-small" type="button">Clear</button>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:8px;">
        ${spokes.length ? spokes.map(spoke => {
          const online = isOnline(spoke.last_seen);
          const checked = hubReseedState.selectedSpokeIds.includes(spoke.id);
          return `<label style="border:1px solid var(--border);border-radius:8px;padding:8px 10px;display:flex;gap:8px;align-items:flex-start;background:var(--card-bg,#fff);">
            <input type="checkbox" data-reseed-spoke-id="${escHtml(spoke.id)}"${checked ? " checked" : ""}>
            <span>
              <strong>${escHtml(spokePrimaryLabel(spoke))}</strong>${online ? "" : ' <span style="color:var(--muted);">(offline — queued)</span>'}
              <div class="tenant-card-subtitle">${escHtml(spoke.id)} · ${escHtml(relativeTime(spoke.last_seen))}</div>
            </span>
          </label>`;
        }).join("") : '<div class="empty-state" style="grid-column:1 / -1;">No approved spokes found for this tenant.</div>'}
      </div>
      <div class="form-actions" style="margin-top:10px;">
        <button id="reseed-cancel-btn" class="btn btn-secondary" type="button">Cancel</button>
        <button id="reseed-start-btn" class="btn btn-primary" type="button"${startDisabled ? " disabled" : ""}>Reseed Selected Spokes →</button>
      </div>
      <div class="form-msg ${hubReseedState.error ? "msg-error" : ""}">${escHtml(hubReseedState.error || (!selectedCount ? "Select at least one spoke." : ""))}</div>
    </div>
  `;
}

async function loadHubReseedPanel(force = false) {
  ensureHubReseedUi();
  syncHubReseedTenantState();
  if (!currentUser || !currentTenantId) {
    renderHubReseedPanel();
    return;
  }
  hubReseedState.loading = true;
  if (activeTab === "reseed") renderHubReseedPanel();
  await Promise.all([
    ensureSpokes(force),
    loadHubReseedTemplates(force),
  ]);
  hubReseedState.loading = false;
  if (activeTab === "reseed") renderHubReseedPanel();
}

async function startHubReseed() {
  if (hubReseedState.submitting) return;
  const template = getSelectedHubReseedTemplate();
  const spokeIds = hubReseedState.selectedSpokeIds.slice();
  if (!currentTenantId) {
    hubReseedState.error = "Select a tenant first.";
    renderHubReseedPanel();
    return;
  }
  if (!template) {
    hubReseedState.error = "Select an Azure template first.";
    renderHubReseedPanel();
    return;
  }
  if (!spokeIds.length) {
    hubReseedState.error = "Select at least one spoke.";
    renderHubReseedPanel();
    return;
  }
  hubReseedState.submitting = true;
  hubReseedState.error = "";
  renderHubReseedPanel();
  const res = await apiFetch("/api/backup/reseed", {
    method: "POST",
    body: {
      tenant_id: currentTenantId,
      template_name: template.template_name,
      latest_blob: template.latest_blob,
      spoke_ids: spokeIds,
      vm_id: HUB_RESEED_VM_ID,
    },
  });
  const data = await readJson(res);
  hubReseedState.submitting = false;
  if (!res || !res.ok) {
    hubReseedState.error = data?.detail || "Unable to start reseed.";
    renderHubReseedPanel();
    return;
  }
  const spokeById = Object.fromEntries(listTenantReseedSpokes().map((spoke, index) => [spoke.id, { spoke, index }]));
  hubReseedState.step = "progress";
  hubReseedState.jobId = data?.job_id || "";
  hubReseedState.progressTemplateName = template.template_name;
  hubReseedState.progressRows = Object.fromEntries(spokeIds.map((spokeId, order) => {
    const match = spokeById[spokeId];
    const spoke = match?.spoke;
    const online = isOnline(spoke?.last_seen);
    return [spokeId, {
      order: match?.index ?? order,
      spoke_id: spokeId,
      spoke_name: spoke ? spokePrimaryLabel(spoke) : spokeId,
      template_name: template.template_name,
      step: online ? "Queued" : "Queued (offline)",
      status: "queued",
      retries: 0,
    }];
  }));
  hubReseedState.completionNotified = false;
  renderHubReseedPanel();
  showToast(`Reseed started for ${spokeIds.length} spoke${spokeIds.length === 1 ? "" : "s"}.`, "ok");
}

function updateHubReseedProgress(message) {
  const spokeId = String(message.spoke_id || "");
  if (!spokeId) return;
  if (hubReseedState.jobId && message.job_id && message.job_id !== hubReseedState.jobId) return;
  if (!hubReseedState.jobId && message.job_id) hubReseedState.jobId = message.job_id;
  const knownSpoke = listTenantReseedSpokes().find(spoke => spoke.id === spokeId);
  const current = hubReseedState.progressRows[spokeId] || {
    order: getHubReseedRows().length,
    spoke_id: spokeId,
    spoke_name: knownSpoke ? spokePrimaryLabel(knownSpoke) : spokeId,
    template_name: hubReseedState.progressTemplateName || getSelectedHubReseedTemplate()?.template_name || "—",
    step: "Queued",
    status: "queued",
    retries: 0,
  };
  hubReseedState.step = "progress";
  hubReseedState.progressRows[spokeId] = {
    ...current,
    spoke_name: knownSpoke ? spokePrimaryLabel(knownSpoke) : current.spoke_name,
    template_name: message.template_name || current.template_name,
    step: message.step || current.step,
    status: message.status || current.status,
    retries: Number.isFinite(Number(message.retries)) ? Number(message.retries) : current.retries,
  };
  if (message.template_name) hubReseedState.progressTemplateName = message.template_name;
  if (activeTab === "reseed") renderHubReseedPanel();
  if (!hubReseedState.completionNotified && isHubReseedComplete()) {
    hubReseedState.completionNotified = true;
    showToast("Reseed job finished.", "ok");
  }
}

function renderInBatches(key, container, items, renderItem, batchSize = 40) {
  renderTokens[key] = (renderTokens[key] || 0) + 1;
  const token = renderTokens[key];
  container.innerHTML = "";
  let index = 0;
  function draw() {
    if (renderTokens[key] !== token) return;
    const fragment = document.createDocumentFragment();
    for (let count = 0; count < batchSize && index < items.length; count += 1, index += 1) {
      fragment.appendChild(renderItem(items[index], index));
    }
    container.appendChild(fragment);
    if (index < items.length) window.requestAnimationFrame(draw);
  }
  window.requestAnimationFrame(draw);
}

async function pingApi() {
  const res = await fetch("/api/health").catch(() => null);
  const ok = Boolean(res && res.ok);
  updateApiStatus(ok, ok ? "Connected" : "Disconnected");
  if (ok) {
    const data = await res.json().catch(() => null);
    const footerVersion = $("#footer-cswebui-version");
    if (footerVersion && data?.version) {
      // Prefer semver (e.g. "1.00") over raw git SHA for display
      const displayVer = data.semver || (data.version && data.version.length > 8 ? data.version : null) || data.version;
      footerVersion.title = `cs-webui version: v${displayVer} | Branch: ${data.branch || "?"} | SHA: ${data.sha || data.version || "?"}`;
    }
  }
}

function getExpandedSet(tenantId = currentTenantId) {
  if (!tenantId) return new Set();
  if (!spokeUiState.expandedByTenant[tenantId]) spokeUiState.expandedByTenant[tenantId] = new Set();
  return spokeUiState.expandedByTenant[tenantId];
}

function syncRoleBadge() {
  const badge = $("#topbar-role-badge");
  if (!badge || !currentUser) return;
  badge.textContent = currentUser.is_superadmin ? "SUPERADMIN" : (currentRoleForTenant() || "user").toUpperCase();
}

function setLoginOverlayVisible(visible) {
  $("#hub-login-overlay")?.classList.toggle("hidden", !visible);
  document.body.classList.toggle("hub-logged-out", visible);
  document.body.classList.toggle("hub-logged-in", !visible);
}

function openLoginModal() {
  setLoginOverlayVisible(true);
  setFormMessage("login-error", "", false);
  const password = $("#login-password");
  if (password) password.value = "";
  const username = $("#login-username");
  if (username && !username.value.trim()) {
    username.focus();
  } else {
    password?.focus();
  }
}

function closeLoginModal() {
  setLoginOverlayVisible(false);
  setFormMessage("login-error", "", false);
}

function buildTenantSelector() {}

function clearDynamicTenantTabs() {}

function buildSuperadminTenantTabs() {}

function syncTenantContextChrome() {
  const active = Boolean(currentUser && authToken && tenantContextActive && currentTenantId);
  $("#hub-admin-nav")?.classList.toggle("hidden", active);
  $("#tenant-context-nav")?.classList.toggle("hidden", !active);
  $("#tenant-context-indicator")?.classList.toggle("hidden", !active);
  $("#tenant-context-name") && ($("#tenant-context-name").textContent = tenantName(currentTenantId) || currentTenantId || "—");
  document.body.classList.toggle("tenant-context-active", active);
}

function syncHubPermissionUI() {
  ensureHubReseedUi();
  const isSuperadmin = Boolean(currentUser?.is_superadmin);
  [
    '#hub-admin-nav .tab[data-tab="hub-setup"]',
    '#tenant-context-nav .tab-back[data-tab="hub-setup"]',
  ].forEach(selector => {
    $$(selector).forEach(el => el.classList.toggle("hidden", !isSuperadmin));
  });
  $("#dashboard-add-tenant-btn")?.classList.toggle("hidden", !isSuperadmin);
  syncSuperadminBackupAccess();
}

function exitTenantContext() {
  tenantContextActive = false;
  resetTenantDetail();
  syncTenantContextChrome();
}

async function enterTenantContext(tenantId, tabId = "simulations", force = true) {
  if (!tenantId) return;
  if (!currentUser) {
    openLoginModal();
    return;
  }
  tenantContextActive = true;
  await setCurrentTenant(tenantId, false);
  syncTenantContextChrome();
  showTab(tabId, { source: "tenant" });
  if (force) refreshCurrentView(true).catch(() => {});
}

function applyAuthUI() {
  const loggedIn = Boolean(currentUser && authToken);
  const userPill = $("#hub-user-pill");
  if (userPill) userPill.style.display = loggedIn ? "flex" : "none";
  $("#hub-user-name") && ($("#hub-user-name").textContent = currentUser?.username || "—");
  $$(".auth-tab").forEach(tab => tab.classList.toggle("hidden", !loggedIn));
  $$(".superadmin-tab").forEach(tab => tab.classList.toggle("hidden", !(loggedIn && currentUser?.is_superadmin)));
  syncSuperadminBackupAccess();
  if (!loggedIn) {
    openLoginModal();
    currentTenantId = null;
    tenantContextActive = false;
    tenants = [];
    spokeCache = {};
    tenantUserCounts = {};
    dashboardTenantRows = [];
    aggregateDashboardData = null;
    hubCentralData = null;
    hubCentralSiteOpen = null;
    hubHwOpenCheckId = null;
    hubCcOpenWsite = null;
    aggregateClientRows = [];
    aggregateProxmoxHosts = [];
    aggregateApiServerRows = [];
    aggregateCentralData = null;
    hubConfigDraft = "";
    hubCentralSitesConfigDraft = null;
    hubCentralSitesConfigSavedKey = "";
    resetHubSimulationConfState(null);
    hubClientUiState.expandedByTenant = {};
    hubClientUiState.seenSitesByTenant = {};
    hubVmServerSelectedSpoke = null;
    resetTenantDetail();
    syncTenantContextChrome();
    if (activeTab !== "dashboard") showTab("dashboard");
    return;
  }
  closeLoginModal();
  syncRoleBadge();
  syncTenantContextChrome();
  syncHubPermissionUI();
  syncHubSpokeConfigSetupUI();
}

async function loadUserContext() {
  if (!authToken) {
    currentUser = null;
    applyAuthUI();
    return;
  }
  const meRes = await apiFetch("/api/auth/me");
  if (!meRes || !meRes.ok) {
    logout(false);
    return;
  }
  currentUser = await meRes.json();
  if (currentUser.is_superadmin) {
    const [tenantsRes, usersRes] = await Promise.all([
      apiFetch("/api/superadmin/tenants"),
      apiFetch("/api/superadmin/users"),
    ]);
    tenants = tenantsRes && tenantsRes.ok ? (await tenantsRes.json()).map(item => ({ id: item.id, name: item.name || item.id, raw: item })) : [];
    tenantUserCounts = usersRes && usersRes.ok ? buildTenantUserCounts(await usersRes.json()) : {};
  } else {
    tenants = (currentUser.tenant_roles || []).map(role => ({
      id: role.tenant_id,
      name: role.tenant_name || role.name || role.tenant_id,
      raw: role,
    }));
    tenantUserCounts = {};
  }
  if (tenants.length && !tenants.some(tenant => tenant.id === currentTenantId)) {
    currentTenantId = tenants[0].id;
  }
  // Non-superadmin users go straight into tenant context — they have no dashboard to select from
  if (!currentUser.is_superadmin && currentTenantId) {
    tenantContextActive = true;
  }
  // Non-superadmin users go straight into tenant context — they have no dashboard to select from
  if (!currentUser.is_superadmin && currentTenantId) {
    tenantContextActive = true;
  }
  applyAuthUI();
  syncHubPermissionUI();
  populateCommandSpokeSelect();
  // Eagerly load pending spokes for tenant admins so approval notice
  // appears immediately on login without needing to open Hub Setup first.
  if (!currentUser?.is_superadmin && currentTenantId && canManageTenant()) {
    loadTenantPendingSpokes();
  }
  // For tenant users, load spokes immediately and navigate to spokes tab
  if (!currentUser.is_superadmin && currentTenantId) {
    ensureTenantSpokesFor(currentTenantId, true).then(() => {
      if (activeTab === "dashboard") showTab("spokes");
      else refreshCurrentView(true).catch(() => {});
    }).catch(() => {});
  }
}

async function submitLogin() {
  const username = $("#login-username")?.value.trim();
  const password = $("#login-password")?.value || "";
  if (!username || !password) {
    setFormMessage("login-error", "Enter username and password.", false);
    return;
  }
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }).catch(() => null);
  if (!res || !res.ok) {
    const data = res ? await res.json().catch(() => null) : null;
    const detail = data?.detail;
    setFormMessage("login-error", detail?.message || detail || "Invalid credentials.", false);
    return;
  }
  const payload = await res.json();
  authToken = payload.access_token;
  sessionStorage.setItem("hub_token", authToken);
  await loadUserContext();
  connectHubWebSocket();
  await refreshCurrentView(true);
  showToast("Signed in successfully.", "ok");
}

function disconnectWebSocket() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  if (ws) {
    const socket = ws;
    ws = null;
    socket.onclose = null;
    socket.close();
  }
  updateApiStatus(false, "Disconnected");
}

function logout(showMessage = true) {
  closeSuperadminBackupModal(true);
  resetHubReseedState({ preserveTemplates: false });
  authToken = null;
  currentUser = null;
  currentTenantId = null;
  tenants = [];
  spokeCache = {};
  aggregateDashboardData = null;
  hubCentralData = null;
  hubSimOpenCheckId = null;
  hubHwOpenCheckId = null;
  hubCcOpenWsite = null;
  aggregateClientRows = [];
  aggregateProxmoxHosts = [];
  aggregateApiServerRows = [];
  aggregateCentralData = null;
  hubConfigDraft = "";
  hubCentralSitesConfigDraft = null;
  hubCentralSitesConfigSavedKey = "";
  resetHubSimulationConfState(currentTenantId);
  hubClientUiState.expandedByTenant = {};
  hubClientUiState.seenSitesByTenant = {};
  hubVmServerSelectedSpoke = null;
  resetTenantDetail();
  // Clear all per-tenant localStorage caches on logout
  try {
    Object.keys(localStorage).filter(k => k.startsWith("hub_central_") || k.startsWith("hub_clients_") || k.startsWith("hub_sites_") || k.startsWith("hub_vmserver_"))
      .forEach(k => localStorage.removeItem(k));
  } catch (_) {}
  sessionStorage.removeItem("hub_token");
  disconnectWebSocket();
  applyAuthUI();
  closeSpokeModal();
  if (showMessage) showToast("Signed out.", "ok");
}

async function setCurrentTenant(tenantId, reload = true) {
  currentTenantId = tenantId;
  aggregateDashboardData = null;
  hubCentralData = null;
  hubSimOpenCheckId = null;
  hubHwOpenCheckId = null;
  hubCcOpenWsite = null;
  aggregateClientRows = [];
  aggregateProxmoxHosts = [];
  aggregateApiServerRows = [];
  aggregateCentralData = null;
  hubConfigDraft = "";
  hubCentralSitesConfigDraft = null;
  hubCentralSitesConfigSavedKey = "";
  resetHubSimulationConfState(tenantId);
  delete hubClientUiState.expandedByTenant[tenantId];
  delete hubClientUiState.seenSitesByTenant[tenantId];
  hubVmServerSelectedSpoke = null;
  syncRoleBadge();
  syncTenantContextChrome();
  syncHubPermissionUI();
  populateCommandSpokeSelect();
  if (reload && ["simulations", "clients", "vm-server", "api-server", "central", "spokes", "reseed", "setup", "tenant-setup", "config", "commands"].includes(activeTab)) await refreshCurrentView(true);
}

function showTab(rawTabId, opts = {}) {
  const tabId = rawTabId.startsWith('hub-') ? rawTabId.slice(4) : rawTabId;
  if (["simulations", "clients", "vm-server", "api-server", "central", "spokes", "reseed", "setup", "tenant-setup", "config", "commands", "superadmin"].includes(tabId) && !currentUser) {
    openLoginModal();
    return;
  }
  if (opts.source === "admin") {
    tenantContextActive = false;
    resetTenantDetail();
  } else if (opts.source === "tenant") {
    tenantContextActive = true;
  }
  // Clear node-detail refresh timer when leaving vm-server tab
  if (tabId !== "vm-server" && _hubNodeRefreshTimer) {
    clearInterval(_hubNodeRefreshTimer);
    _hubNodeRefreshTimer = null;
  }
  if (tabId !== "vm-server" && hubVmConsoleState.rfb) {
    disconnectHubVmConsole({ rerender: false, preserveSelection: true });
  }
  if (tabId !== "vm-server" && hubSpokeShellState.socket) {
    disconnectHubSpokeShell({ rerender: false, preserveSelection: true });
  }
  activeTab = tabId;
  $("#hub-root")?.querySelectorAll(".tab-content").forEach(panel => panel.classList.add("hidden"));
  const panel = $("#hub-root")?.querySelector(`#tab-hub-${CSS.escape(tabId)}`);
  if (panel) panel.classList.remove("hidden");
  $$("#tab-nav .hub-only .tab").forEach(button => {
    button.classList.remove("active");
    if (button.hasAttribute("role")) button.setAttribute("aria-selected", "false");
  });
  if (opts.button) {
    opts.button.classList.add("active");
    if (opts.button.hasAttribute("role")) opts.button.setAttribute("aria-selected", "true");
  } else {
    const selector = hubAdminTabIds.has(tabId) && !tenantContextActive
      ? `#hub-admin-nav .tab[data-tab="hub-${tabId}"]`
      : `#tenant-context-nav .tab[data-tab="hub-${tabId}"]`;
    const activeButton = $(selector);
    activeButton?.classList.add("active");
    if (activeButton?.hasAttribute("role")) activeButton.setAttribute("aria-selected", "true");
  }
  syncTenantContextChrome();
  syncHubPermissionUI();
  updateHubRefreshPausedState();
  refreshCurrentView();
}

async function refreshCurrentView(force = false) {
  if (activeTab === "dashboard") {
    await loadDashboard(force);
  } else if (activeTab === "simulations") {
    await loadHubSimulations(force);
  } else if (activeTab === "clients") {
    await loadClients(force);
  } else if (activeTab === "vm-server") {
    await loadVmServer(force);
  } else if (activeTab === "api-server") {
    await loadApiServer(force);
  } else if (activeTab === "central") {
    await loadHubCentralData(force);
  } else if (activeTab === "spokes") {
    await loadSpokes(force);
  } else if (activeTab === "reseed") {
    await loadHubReseedPanel(force);
  } else if (activeTab === "commands") {
    await loadCommands();
  } else if (activeTab === "setup") {
    await loadSetup(force);
  } else if (activeTab === "tenant-setup") {
    await loadTenantSetup(force);
  } else if (activeTab === "config") {
    await loadConfig(force);
  } else if (activeTab === "superadmin") {
    await loadSuperadmin();
  }
}

function getTenantSpokes() {
  return currentTenantId ? (spokeCache[currentTenantId] || []) : [];
}

function spokeLabel(count) {
  return `${count} ${count === 1 ? "spoke" : "spokes"}`;
}

function spokePrimaryLabel(spoke) {
  return spokeDisplayName(spoke, "—");
}

function spokeSecondaryLabel(spoke, fallback = "—") {
  const primary = spokePrimaryLabel(spoke);
  const parts = [];
  const hostname = String(spoke?.hostname || "").trim();
  const label = String(spoke?.label || "").trim();
  if (hostname && hostname !== primary) parts.push(hostname);
  if (label && label !== primary && label !== hostname) parts.push(label);
  return parts.join(" · ") || fallback;
}

function spokeCommandLabel(spoke) {
  const primary = spokePrimaryLabel(spoke);
  const hostname = String(spoke?.hostname || "").trim();
  return hostname && hostname !== primary ? `${primary} (${hostname})` : primary;
}

function spokeSearchText(spoke) {
  return [
    spokePrimaryLabel(spoke),
    String(spoke?.hostname || ""),
    String(spoke?.label || ""),
    String(spoke?.id || ""),
  ].join(" ").toLowerCase();
}

function updateSpokeStatPills(spokes) {
  const approved = spokes.filter(spoke => spoke.status === "approved");
  const onlineCount = approved.filter(spoke => isOnline(spoke.last_seen)).length;
  const clientCount = approved.reduce((sum, spoke) => sum + ((spoke.telemetry?.clients || []).length), 0);
  $("#spokes-count-pill") && ($("#spokes-count-pill").textContent = spokeLabel(approved.length));
  $("#spokes-online-pill") && ($("#spokes-online-pill").textContent = `${onlineCount} online`);
  $("#spokes-clients-pill") && ($("#spokes-clients-pill").textContent = `${clientCount} clients`);
}

async function ensureTenantSpokesFor(tenantId, force = false) {
  if (!tenantId) return [];
  if (!force && spokeCache[tenantId]) return spokeCache[tenantId];
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes`);
  if (!res || !res.ok) return spokeCache[tenantId] || [];
  const spokes = await res.json();
  spokeCache[tenantId] = spokes;
  if (tenantId === currentTenantId) populateCommandSpokeSelect();
  return spokes;
}

function summarizeConfigField(spokes, key, formatter = formatInlineValue) {
  const approved = spokes.filter(spoke => spoke.status === "approved");
  const populated = approved
    .map(spoke => spoke.config?.[key] ?? spoke.seed_config?.[key])
    .filter(hasMeaningfulValue);
  const values = uniqueValues(populated);
  return {
    value: values.length === 1 ? formatter(values[0]) : values.length ? `${values.length} values` : "—",
    detail: values.length > 1
      ? values.map(item => formatter(item)).join(" • ")
      : `${populated.length}/${approved.length || 0} spokes`,
  };
}

function renderConfigSummaryRow(label, summary) {
  return `
    <tr>
      <td>${escHtml(label)}</td>
      <td>${escHtml(summary.value)}</td>
      <td>${escHtml(summary.detail)}</td>
    </tr>
  `;
}

const HUB_SIM_BOOL_VALUES = new Set(["on", "off", "yes", "no", "true", "false"]);
const HUB_SIM_PASSWORD_KEY_RE = /pw$|password|secret/i;
const HUB_SIM_FIXED_SECTION_ORDER = ["simulation", "server", "address", ...Array.from({ length: 10 }, (_, idx) => `s${idx}`)];
const HUB_SIM_SLOT_KEYS = ["central_check", "wsite", "ssid", "ssidpw", "dhcp_fail", "dns_fail", "assoc_fail", "port_flap", "ping_test", "download", "www_traffic", "iperf", "sim_phy", "l1"];
const HUB_SIM_SELECT_FIELDS = { sim_phy: ["wireless", "ethernet", "any"] };

function resetHubSimulationConfState(tenantId = currentTenantId) {
  hubSimulationConfState = { tenantId, loaded: false, loading: false, rawContent: "", sha: "", fetchedAt: "", sections: {}, sectionOrder: [], keyOrder: {}, error: "" };
}

function hubSimFieldLabel(key) {
  return String(key || "").replace(/_/g, " ").replace(/\b\w/g, ch => ch.toUpperCase());
}

function hubSimIsBoolValue(value) {
  return HUB_SIM_BOOL_VALUES.has(String(value ?? "").trim().toLowerCase());
}

function hubSimBoolPair(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (normalized === "yes" || normalized === "no") return ["yes", "no"];
  if (normalized === "true" || normalized === "false") return ["true", "false"];
  return ["on", "off"];
}

function parseHubSimulationIni(content = "") {
  const sections = {};
  const sectionOrder = [];
  const keyOrder = {};
  let currentSection = null;
  String(content || "").split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith(";") || trimmed.startsWith("#")) return;
    const sectionMatch = trimmed.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      currentSection = sectionMatch[1].trim();
      if (!sections[currentSection]) {
        sections[currentSection] = {};
        sectionOrder.push(currentSection);
        keyOrder[currentSection] = [];
      }
      return;
    }
    if (!currentSection) return;
    const eqIdx = line.indexOf("=");
    if (eqIdx < 0) return;
    const key = line.slice(0, eqIdx).trim();
    const value = line.slice(eqIdx + 1).trim();
    if (!key) return;
    if (!Object.prototype.hasOwnProperty.call(sections[currentSection], key)) keyOrder[currentSection].push(key);
    sections[currentSection][key] = value;
  });
  return { sections, sectionOrder, keyOrder };
}

function serializeHubSimulationIni(sections = {}, sectionOrder = [], keyOrder = {}) {
  const seen = new Set();
  const orderedSections = [];
  HUB_SIM_FIXED_SECTION_ORDER.forEach((section) => {
    if (Object.prototype.hasOwnProperty.call(sections, section)) {
      orderedSections.push(section);
      seen.add(section);
    }
  });
  sectionOrder.forEach((section) => {
    if (!seen.has(section) && Object.prototype.hasOwnProperty.call(sections, section)) {
      orderedSections.push(section);
      seen.add(section);
    }
  });
  Object.keys(sections).forEach((section) => {
    if (!seen.has(section)) orderedSections.push(section);
  });
  return orderedSections.map((section) => {
    const values = sections[section] || {};
    const keys = [];
    const seenKeys = new Set();
    (keyOrder[section] || []).forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        keys.push(key);
        seenKeys.add(key);
      }
    });
    Object.keys(values).forEach((key) => {
      if (!seenKeys.has(key)) keys.push(key);
    });
    return `[${section}]\n${keys.map((key) => `${key}=${values[key] ?? ""}`).join("\n")}`;
  }).join("\n\n");
}

function hubSimulationSectionKeys(section, values = {}) {
  const ordered = hubSimulationConfState.keyOrder?.[section] || [];
  if (String(section).match(/^s\d+$/)) {
    const seen = new Set();
    const keys = [];
    HUB_SIM_SLOT_KEYS.forEach((key) => {
      keys.push(key);
      seen.add(key);
    });
    [...ordered, ...Object.keys(values || {})].forEach((key) => {
      if (!seen.has(key)) {
        keys.push(key);
        seen.add(key);
      }
    });
    return keys;
  }
  return ordered.length ? ordered : Object.keys(values || {});
}

function renderHubSimulationField(section, key, rawValue = "") {
  const value = String(rawValue ?? "");
  const label = hubSimFieldLabel(key);
  if (hubSimIsBoolValue(value)) {
    const [onValue, offValue] = hubSimBoolPair(value);
    return `
      <label class="toggle-label" style="justify-content:space-between;align-items:center;padding:8px 10px;border:1px solid var(--line);border-radius:10px;gap:8px;">
        <span>${escHtml(label)}</span>
        <input type="checkbox" data-section="${escHtml(section)}" data-key="${escHtml(key)}" data-on="${escHtml(onValue)}" data-off="${escHtml(offValue)}"${value.toLowerCase() === onValue ? " checked" : ""}>
      </label>
    `;
  }
  if (HUB_SIM_SELECT_FIELDS[key]) {
    return `
      <label class="form-group">
        <span class="form-label">${escHtml(label)}</span>
        <select class="form-input" data-section="${escHtml(section)}" data-key="${escHtml(key)}">
          ${HUB_SIM_SELECT_FIELDS[key].map((option) => `<option value="${escHtml(option)}"${option === value ? " selected" : ""}>${escHtml(option)}</option>`).join("")}
        </select>
      </label>
    `;
  }
  return `
    <label class="form-group">
      <span class="form-label">${escHtml(label)}</span>
      <input class="form-input" type="${HUB_SIM_PASSWORD_KEY_RE.test(key) ? "password" : "text"}" value="${escHtml(value)}" data-section="${escHtml(section)}" data-key="${escHtml(key)}">
    </label>
  `;
}

function renderHubSimulationSection(section, values = {}, { open = false } = {}) {
  const keys = hubSimulationSectionKeys(section, values);
  const title = String(section).match(/^s\d+$/) ? `Slot [${section}]` : `[${section}]`;
  const fields = keys.length
    ? keys.map((key) => renderHubSimulationField(section, key, values[key] ?? "")).join("")
    : '<div class="muted">No fields found in this section.</div>';
  return `
    <details class="setup-card setup-section-gap"${open ? " open" : ""}>
      <summary style="cursor:pointer;font-weight:600;">${escHtml(title)}</summary>
      <div class="setup-form setup-section-gap">
        <div class="form-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));">${fields}</div>
      </div>
    </details>
  `;
}

function renderHubSimulationConfigPanel() {
  const container = $("#hub-sim-config-panel");
  if (!container) return;
  const disabled = canManageTenant() ? "" : " disabled";
  const fetched = hubSimulationConfState.fetchedAt ? fmtDate(hubSimulationConfState.fetchedAt) : "—";
  const infoBar = `
    <section class="setup-card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
        <div>
          <div style="font-weight:600;">configs/simulation.conf</div>
          <div class="muted" style="font-size:0.85rem;">Last fetched from GitHub: ${escHtml(fetched)}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <button id="hub-sim-config-refresh-btn" class="btn btn-secondary btn-small" type="button">Refresh</button>
          <button id="hub-sim-config-save-btn" class="btn btn-primary btn-small" type="button"${disabled}>Save to GitHub</button>
        </div>
      </div>
      <div id="hub-sim-config-msg" class="form-msg" style="margin-top:10px;"></div>
    </section>
  `;
  if (hubSimulationConfState.loading) {
    container.innerHTML = `${infoBar}<div class="empty-state">Loading simulation.conf from GitHub…</div>`;
    return;
  }
  if (hubSimulationConfState.error) {
    container.innerHTML = `
      ${infoBar}
      <section class="setup-card">
        <div class="empty-state">${escHtml(hubSimulationConfState.error)}</div>
        <div style="margin-top:8px;display:flex;gap:8px;justify-content:center;">
          <button class="btn btn-secondary btn-small" type="button" data-open-tenant-setup="true">Open Setup</button>
        </div>
      </section>
    `;
    return;
  }
  const sections = hubSimulationConfState.sections || {};
  const orderedSections = ["simulation", "server", "address"].filter((section) => Object.prototype.hasOwnProperty.call(sections, section));
  const slotSections = HUB_SIM_FIXED_SECTION_ORDER.filter((section) => /^s\d+$/.test(section));
  container.innerHTML = `
    ${infoBar}
    <div id="hub-sim-config-form">
      ${orderedSections.map((section, index) => renderHubSimulationSection(section, sections[section] || {}, { open: index === 0 })).join("")}
      ${slotSections.map((section, index) => renderHubSimulationSection(section, sections[section] || {}, { open: index === 0 && orderedSections.length === 0 })).join("")}
    </div>
  `;
}

async function loadHubSimulationConf(force = false) {
  const tenantId = getActiveTenantId();
  if (!tenantId || !currentUser) return null;
  if (hubSimulationConfState.tenantId !== tenantId) resetHubSimulationConfState(tenantId);
  if (!force && hubSimulationConfState.loaded) {
    renderHubSimulationConfigPanel();
    return hubSimulationConfState;
  }
  hubSimulationConfState.loading = true;
  hubSimulationConfState.error = "";
  renderHubSimulationConfigPanel();
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/config/simulation-conf`);
  const data = await readJson(res);
  if (!res || !res.ok) {
    hubSimulationConfState.loading = false;
    hubSimulationConfState.loaded = false;
    hubSimulationConfState.error = data?.detail || "Unable to load simulation.conf from GitHub.";
    renderHubSimulationConfigPanel();
    return null;
  }
  const parsed = parseHubSimulationIni(data?.content || "");
  hubSimulationConfState = {
    tenantId,
    loaded: true,
    loading: false,
    rawContent: data?.content || "",
    sha: data?.sha || "",
    fetchedAt: data?.fetched_at || "",
    sections: parsed.sections,
    sectionOrder: parsed.sectionOrder,
    keyOrder: parsed.keyOrder,
    error: "",
  };
  renderHubSimulationConfigPanel();
  return hubSimulationConfState;
}

function collectHubSimulationConfContent() {
  const form = $("#hub-sim-config-form");
  if (!form) return hubSimulationConfState.rawContent || "";
  const sections = JSON.parse(JSON.stringify(hubSimulationConfState.sections || {}));
  form.querySelectorAll("[data-section][data-key]").forEach((input) => {
    const section = input.dataset.section;
    const key = input.dataset.key;
    if (!sections[section]) sections[section] = {};
    if (input.type === "checkbox") {
      sections[section][key] = input.checked ? (input.dataset.on || "on") : (input.dataset.off || "off");
      return;
    }
    sections[section][key] = input.value.trim();
  });
  return serializeHubSimulationIni(sections, hubSimulationConfState.sectionOrder, hubSimulationConfState.keyOrder);
}

async function saveHubSimulationConf() {
  if (!canManageTenant()) {
    setFormMessage("hub-sim-config-msg", "Tenant Viewer access is read-only.", false);
    return;
  }
  const tenantId = getActiveTenantId();
  if (!tenantId) return;
  setFormMessage("hub-sim-config-msg", "Saving to GitHub…", true);
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/config/simulation-conf`, {
    method: "PUT",
    body: { content: collectHubSimulationConfContent() },
  });
  const data = await readJson(res);
  if (!res || !res.ok) {
    setFormMessage("hub-sim-config-msg", data?.detail || "Unable to save simulation.conf.", false);
    return;
  }
  await loadHubSimulationConf(true);
  setFormMessage("hub-sim-config-msg", `Saved to GitHub. Repo sync queued for ${data?.synced_spokes ?? 0} spoke(s).`, true);
}

async function saveTenantGithubSettings() {
  if (!canManageTenant()) {
    setFormMessage("tenant-github-msg", "Tenant Viewer access is read-only.", false);
    return;
  }
  const tenantId = currentTenantId;
  if (!tenantId) return;
  const payload = {
    sim_repo_url: $("#tenant-sim-repo-url")?.value.trim() || "",
    sim_repo_branch: $("#tenant-sim-repo-branch")?.value.trim() || "main",
  };
  const tokenSecret = getSecretInputPayload($("#tenant-github-token"));
  if (tokenSecret.include) payload.github_token = tokenSecret.value;
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/settings/github`, { method: "POST", body: payload });
  const data = await readJson(res);
  if (!res || !res.ok) {
    setFormMessage("tenant-github-msg", data?.detail || "Unable to save GitHub settings.", false);
    return;
  }
  if (tenantDetailState.data[tenantId]?.settings) tenantDetailState.data[tenantId].settings.github = data;
  setFormMessage("tenant-github-msg", "GitHub settings saved.", true);
  hydrateTenantSetupPanel({ settings: { github: data } });
  resetHubSimulationConfState(tenantId);
}

function hydrateTenantSetupPanel(data = {}, root = document) {
  const scope = root && typeof root.querySelector === "function" ? root : document;
  const query = (selector) => scope.querySelector(selector);
  try {
    const github = data?.settings?.github || {};
    const configured = isConfiguredSecretValue(github.github_token_configured);
    setSecretInputConfigured(query("#tenant-github-token"), configured);
    query("#tenant-github-token-status") && (query("#tenant-github-token-status").textContent = configured ? "Token configured" : "Token not configured");
  } catch (_) { /* non-critical — PSK wiring continues below */ }

  const tenantId = data.tenantId || currentTenantId;
  const useAllDonglesToggle = query("#ts-use-all-dongles");
  if (tenantId && canManageTenant(tenantId) && useAllDonglesToggle && !useAllDonglesToggle._bound) {
    useAllDonglesToggle._bound = true;
    useAllDonglesToggle.addEventListener("change", async (e) => {
      const nextValue = !!e.target.checked;
      e.target.disabled = true;
      try {
        const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/settings`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ use_all_dongles: nextValue })
        });
        const result = await readJson(res);
        if (!res || !res.ok) {
          e.target.checked = !nextValue;
          return;
        }
        const appliedValue = !!(result && Object.prototype.hasOwnProperty.call(result, 'use_all_dongles') ? result.use_all_dongles : nextValue);
        e.target.checked = appliedValue;
        if (tenantDetailState.data[tenantId]?.settings) tenantDetailState.data[tenantId].settings.use_all_dongles = appliedValue;
      } finally {
        e.target.disabled = false;
      }
    });
  }
  const generateBtn = query("#ts-onboarding-generate-btn");
  if (tenantId && canManageTenant(tenantId) && generateBtn) {
    void _loadTsOnboardingStatus(tenantId, scope);
    if (!generateBtn._bound) {
      generateBtn._bound = true;
      generateBtn.addEventListener("click", () => _generateTsOnboardingPsk(tenantId, scope));
    }
  }
}

async function _loadTsOnboardingStatus(tenantId, root = document) {
  const query = (sel) => (root?.querySelector ? root : document).querySelector(sel);
  const listEl = query("#ts-onboarding-psk-list");
  const statusEl = query("#ts-onboarding-status");
  const res = await apiFetch(`/api/tenant/${encodeURIComponent(tenantId)}/onboarding-psk`);
  if (!res?.ok) {
    const detail = await readJson(res);
    if (statusEl) statusEl.textContent = detail?.detail || "Unable to load PSK status.";
    return;
  }
  const d = await res.json();
  const psks = d.psks || [];
  if (statusEl) statusEl.textContent = psks.length ? "" : "No PSKs configured — spokes require manual approval.";
  if (!listEl) return;
  if (psks.length === 0) {
    listEl.innerHTML = "";
    return;
  }
  listEl.innerHTML = psks.map((psk) => `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
      <code style="flex:1;font-size:12px;word-break:break-all;background:var(--bg-2,#f4f4f4);padding:4px 6px;border-radius:4px;">${escHtml(psk)}</code>
      <button class="btn btn-sm btn-secondary ts-psk-copy-btn" data-psk="${escHtml(psk)}" type="button">Copy</button>
      <button class="btn btn-sm btn-danger ts-psk-revoke-btn" data-psk="${escHtml(psk)}" type="button">Revoke</button>
    </div>
    <div class="form-hint" style="margin-bottom:8px;font-size:11px;">sudo bash &lt;(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --hub-url ${escHtml(window.location.origin)} --hub-tenant ${escHtml(getTenantMeta(tenantId).name || tenantId)} --hub-psk ${escHtml(psk)}</div>
  `).join("");
  listEl.querySelectorAll(".ts-psk-copy-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(btn.dataset.psk).then(() => {
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = "Copy"; }, 2000);
      });
    });
  });
  listEl.querySelectorAll(".ts-psk-revoke-btn").forEach((btn) => {
    btn.addEventListener("click", () => _revokeTsOnboardingPsk(tenantId, btn.dataset.psk, root));
  });
}

async function _generateTsOnboardingPsk(tenantId, root = document) {
  const query = (sel) => (root?.querySelector ? root : document).querySelector(sel);
  const btn = query("#ts-onboarding-generate-btn");
  const statusEl = query("#ts-onboarding-status");
  if (btn) { btn.disabled = true; btn.textContent = "Adding…"; }
  try {
    const res = await apiFetch(`/api/tenant/${encodeURIComponent(tenantId)}/onboarding-psk`, { method: "POST" });
    if (!res?.ok) {
      const detail = await readJson(res);
      if (statusEl) statusEl.textContent = detail?.detail || "Unable to generate PSK.";
      return;
    }
    await _loadTsOnboardingStatus(tenantId, root);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Add PSK"; }
  }
}

async function _revokeTsOnboardingPsk(tenantId, psk, root = document) {
  const query = (sel) => (root?.querySelector ? root : document).querySelector(sel);
  const statusEl = query("#ts-onboarding-status");
  try {
    const res = await apiFetch(`/api/tenant/${encodeURIComponent(tenantId)}/onboarding-psk`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ psk }),
    });
    if (!res?.ok) {
      const d = await readJson(res);
      if (statusEl) statusEl.textContent = d?.detail || "Failed to revoke PSK.";
      return;
    }
    await _loadTsOnboardingStatus(tenantId, root);
  } catch (e) {
    if (statusEl) statusEl.textContent = "Failed to revoke PSK.";
  }
}

async function loadTenantDetailData(force = false) {
  const tenantId = tenantDetailState.open ? tenantDetailState.tenantId : currentTenantId;
  if (!tenantId || !currentUser) return null;
  if (!force && tenantDetailState.data[tenantId]) return tenantDetailState.data[tenantId];

  const [spokesRes, commandsRes, processingRes, settingsRes] = await Promise.all([
    apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes`),
    apiFetch(`/api/${encodeURIComponent(tenantId)}/commands`),
    apiFetch(`/api/${encodeURIComponent(tenantId)}/processing-summary`),
    canManageTenant(tenantId) ? apiFetch(`/api/${encodeURIComponent(tenantId)}/settings`) : Promise.resolve(null),
  ]);

  const data = {
    tenantId,
    spokes: [],
    commands: [],
    processing: null,
    settings: null,
    settingsError: canManageTenant(tenantId) ? null : "Admin access required to view tenant setup settings.",
  };

  if (spokesRes?.ok) {
    data.spokes = await spokesRes.json();
    spokeCache[tenantId] = data.spokes;
  }
  if (commandsRes?.ok) data.commands = await commandsRes.json();
  if (processingRes?.ok) data.processing = await processingRes.json();
  if (settingsRes) {
    if (settingsRes.ok) {
      data.settings = await settingsRes.json();
      data.settingsError = null;
    } else {
      const err = await readJson(settingsRes);
      data.settingsError = err?.detail || `Unable to load tenant settings (${settingsRes.status}).`;
    }
  }

  tenantDetailState.data[tenantId] = data;
  return data;
}

function renderTenantDashboardPanel(data, summary) {
  const healthRows = data.spokes
    .filter(spoke => spoke.status === "approved")
    .sort((a, b) => spokePrimaryLabel(a).localeCompare(spokePrimaryLabel(b)))
    .map(spoke => `
      <tr>
        <td><strong>${escHtml(spokePrimaryLabel(spoke))}</strong>${(() => { const s = spokeSecondaryLabel(spoke, ""); return s ? `<div class="muted">${escHtml(s)}</div>` : ""; })()}</td>
        <td><span class="tenant-status-badge ${isOnline(spoke.last_seen) ? "online" : "offline"}">${isOnline(spoke.last_seen) ? "Online" : "Offline"}</span></td>
        <td>${getSpokeClients(spoke).length}</td>
        <td>${getSpokeVmCount(spoke)}</td>
        <td>${getSpokeRunningVmCount(spoke)}</td>
        <td title="${escHtml(fmtDate(spoke.last_seen))}">${escHtml(relativeTime(spoke.last_seen))}</td>
      </tr>
    `)
    .join("");
  const recentCommands = [...(data.commands || [])]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 8)
    .map(command => {
      const spoke = data.spokes.find(item => item.id === command.spoke_id);
      return `
        <tr>
          <td>${escHtml(fmtDate(command.created_at))}</td>
          <td>${escHtml(spoke ? spokeCommandLabel(spoke) : command.spoke_id)}</td>
          <td>${escHtml(command.type)}</td>
          <td><span class="badge cmd-status-${escHtml(command.status)}">${escHtml(command.status)}</span></td>
        </tr>
      `;
    })
    .join("");

  return `
    <div class="tenant-metrics-grid">
      <article class="tenant-metric-card"><span class="tenant-metric-label">Approved Spokes</span><strong class="tenant-metric-value">${summary.approvedCount}</strong><span class="tenant-metric-hint">${summary.pendingCount} pending</span></article>
      <article class="tenant-metric-card"><span class="tenant-metric-label">Online / Offline</span><strong class="tenant-metric-value">${summary.onlineCount} / ${summary.offlineCount}</strong><span class="tenant-metric-hint">Based on 300s heartbeat</span></article>
      <article class="tenant-metric-card"><span class="tenant-metric-label">Sim Clients</span><strong class="tenant-metric-value">${summary.clientCount}</strong><span class="tenant-metric-hint">Across approved spokes</span></article>
      <article class="tenant-metric-card"><span class="tenant-metric-label">VM Footprint</span><strong class="tenant-metric-value">${summary.vmCount}</strong><span class="tenant-metric-hint">${summary.runningVmCount} running · ${summary.usbCount} USB devices</span></article>
    </div>
    <div class="tenant-detail-grid">
      <section class="setup-card">
        <div class="setup-card-header"><h2>Spoke Health</h2><p>Aggregated view of each spoke in this tenant.</p></div>
        <table class="data-table">
          <thead><tr><th>Spoke</th><th>Status</th><th>Clients</th><th>VMs</th><th>Running</th><th>Last Sync</th></tr></thead>
          <tbody>${healthRows || '<tr><td colspan="6" class="empty-state">No approved spokes in this tenant.</td></tr>'}</tbody>
        </table>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Recent Commands</h2><p>Latest tenant-wide commands sent from the hub.</p></div>
        <table class="data-table">
          <thead><tr><th>Created</th><th>Spoke</th><th>Command</th><th>Status</th></tr></thead>
          <tbody>${recentCommands || '<tr><td colspan="4" class="empty-state">No commands recorded for this tenant.</td></tr>'}</tbody>
        </table>
      </section>
    </div>
  `;
}

function renderTenantSpokesPanel(data) {
  const rows = [...data.spokes]
    .sort((a, b) => spokePrimaryLabel(a).localeCompare(spokePrimaryLabel(b)))
    .map(spoke => `
      <tr>
        <td><strong>${escHtml(spokePrimaryLabel(spoke))}</strong>${(() => { const s = spokeSecondaryLabel(spoke, ""); return s ? `<div class="muted">${escHtml(s)}</div>` : ""; })()}</td>
        <td><span class="tenant-status-badge ${spoke.status === "approved" && isOnline(spoke.last_seen) ? "online" : "offline"}">${escHtml(spoke.status === "approved" ? (isOnline(spoke.last_seen) ? "online" : "offline") : spoke.status)}</span></td>
        <td><code>${escHtml(spoke.id)}</code></td>
        <td title="${escHtml(fmtDate(spoke.last_seen))}">${escHtml(relativeTime(spoke.last_seen))}</td>
        <td>${getSpokeClients(spoke).length}</td>
        <td>${getSpokeVmCount(spoke)}</td>
        <td>${spoke.status === "approved" ? `<button class="btn btn-primary btn-small" data-open-spoke-modal="${escHtml(spoke.id)}" type="button">Open Detail</button>` : '<span class="muted">—</span>'}</td>
      </tr>
    `)
    .join("");

  return `
    <section class="setup-card">
      <div class="setup-card-header"><h2>Tenant Spokes</h2><p>Status, heartbeat, and telemetry summary for every spoke assigned to this tenant.</p></div>
      <table class="data-table">
        <thead><tr><th>Spoke</th><th>Status</th><th>Spoke ID</th><th>Last Sync</th><th>Clients</th><th>VMs</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="empty-state">No spokes assigned to this tenant.</td></tr>'}</tbody>
      </table>
    </section>
  `;
}

function renderTenantCommandsPanel(data) {
  const commands = [...(data.commands || [])].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  const queuedCount = commands.filter(command => command.status === "queued").length;
  const rows = commands.slice(0, 50).map(command => {
    const spoke = data.spokes.find(item => item.id === command.spoke_id);
    return `
      <tr>
        <td>${escHtml(fmtDate(command.created_at))}</td>
        <td>${escHtml(spoke ? spokeCommandLabel(spoke) : command.spoke_id)}</td>
        <td>${escHtml(command.type)}</td>
        <td><span class="badge cmd-status-${escHtml(command.status)}">${escHtml(command.status)}</span></td>
        <td>${escHtml(fmtDate(command.expires_at))}</td>
      </tr>
    `;
  }).join("");

  return `
    <div class="tenant-detail-inline-stats">
      <span class="stat-pill">${queuedCount} queued</span>
      <span class="stat-pill">${commands.length} recent commands</span>
      <span class="stat-pill">${data.spokes.filter(spoke => spoke.status === "approved").length} targetable spokes</span>
    </div>
    <section class="setup-card">
      <div class="setup-card-header"><h2>Recent Tenant Commands</h2><p>Hub command history across all spokes in this tenant.</p></div>
      <table class="data-table">
        <thead><tr><th>Created</th><th>Spoke</th><th>Command</th><th>Status</th><th>Expires</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="empty-state">No commands recorded for this tenant.</td></tr>'}</tbody>
      </table>
    </section>
  `;
}

function renderTenantSetupPanel(data) {
  const tenantId = data.tenantId;
  const settings = data.settings || {};
  const tenant = settings.tenant || getTenantMeta(tenantId);
  const aruba = settings.aruba || {};
  const notifications = settings.notifications || {};
  const github = settings.github || {};
  const processingModes = {
    central_api: data.settings?.processing_modes?.central_api || 'centralized',
    teams: data.settings?.processing_modes?.teams || 'centralized',
    email: data.settings?.processing_modes?.email || 'centralized',
  };
  const disabled = canManageTenant(tenantId) ? '' : ' disabled';
  const apiBase = `${window.location.origin}/api/${tenantId}/spokes/{spoke_id}`;
  const accessNote = data.settingsError ? `<div class="tenant-detail-note">${escHtml(data.settingsError)}</div>` : "";

  return `
    ${accessNote}
    <div class="tenant-detail-grid">
      <section class="setup-card">
        <div class="setup-card-header"><h2>Tenant Setup</h2><p>Hub-managed settings for this tenant.</p></div>
        <div class="setup-status-grid">
          <div class="setup-status-item"><span class="setup-status-label">Tenant Name</span><span class="setup-status-value">${escHtml(tenant.name || tenantId)}</span></div>
          <div class="setup-status-item"><span class="setup-status-label">Tenant ID</span><span class="setup-status-value"><code>${escHtml(tenant.id || tenantId)}</code></span></div>
          <div class="setup-status-item"><span class="setup-status-label">Aruba CID</span><span class="setup-status-value">${escHtml(tenant.aruba_cid || "—")}</span></div>
          <div class="setup-status-item"><span class="setup-status-label">Created</span><span class="setup-status-value">${escHtml(fmtDate(tenant.created_at))}</span></div>
        </div>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>GitHub / Repo</h2><p>Credentials and source repo used for simulation.conf editing.</p></div>
        <div class="setup-form">
          <div class="form-group"><label class="form-label" for="tenant-sim-repo-url">Simulation Repo URL</label><input id="tenant-sim-repo-url" type="url" class="form-input" value="${escHtml(github.sim_repo_url || "")}" placeholder="https://github.com/owner/repo.git"${disabled}></div>
          <div class="form-group"><label class="form-label" for="tenant-sim-repo-branch">Simulation Repo Branch</label><input id="tenant-sim-repo-branch" type="text" class="form-input" value="${escHtml(github.sim_repo_branch || "main")}" placeholder="main"${disabled}></div>
          <div class="form-group"><label class="form-label" for="tenant-github-token">GitHub Token</label><input id="tenant-github-token" type="password" class="form-input" placeholder="Leave blank to keep existing" data-secret-field="true"${disabled}><span class="form-hint" id="tenant-github-token-status">${escHtml(github.github_token_configured ? "Token configured" : "Token not configured")}</span></div>
          <div class="form-actions">
            <button id="save-tenant-github-btn" class="btn btn-primary" type="button"${disabled}>Save GitHub Settings</button>
            <span id="tenant-github-msg" class="form-msg"></span>
          </div>
        </div>
      </section>
      <section class="setup-card" id="ts-dongle-card">
        <div class="setup-card-header"><h2>Dongle Allocation</h2><p>Configure how certified dongles are allocated when preferred hardware runs out.</p></div>
        <p class="setup-card-desc">When enabled, if the preferred dongle type (wireless or ethernet) runs out, remaining certified dongles of the other type will be provisioned automatically using their own sim profile.</p>
        <label class="toggle-label">
          <input type="checkbox" id="ts-use-all-dongles"${settings.use_all_dongles ? ' checked' : ''}${disabled}>
          Use All Available Dongles
        </label>
        <p class="setup-hint">Also adds <code>any</code> as a valid sim_phy option — provisions any certified dongle and sets sim_phy to match its actual type.</p>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Aruba Central</h2></div>
        <table class="data-table">
          <tbody>
            <tr><td>Configured</td><td>${escHtml(aruba.configured ? "Yes" : "No")}</td></tr>
            <tr><td>Cluster URL</td><td>${escHtml(aruba.cluster_url || "—")}</td></tr>
            <tr><td>Client ID</td><td>${escHtml(aruba.client_id || "—")}</td></tr>
            <tr><td>Customer ID</td><td>${escHtml(aruba.customer_id || tenant.aruba_cid || "—")}</td></tr>
            <tr><td>API Version</td><td>${escHtml(aruba.api_version || "—")}</td></tr>
          </tbody>
        </table>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Notifications</h2></div>
        <table class="data-table">
          <tbody>
            <tr><td>Enabled</td><td>${escHtml(notifications.enabled ? "Yes" : "No")}</td></tr>
            <tr><td>Teams Webhook</td><td>${escHtml(notifications.teams_webhook_configured ? "Configured" : "Not configured")}</td></tr>
            <tr><td>SMTP Host</td><td>${escHtml(notifications.smtp_host || "—")}</td></tr>
            <tr><td>SMTP User</td><td>${escHtml(notifications.smtp_user || "—")}</td></tr>
            <tr><td>Recipients</td><td>${escHtml((notifications.to_emails || []).join(", ") || "—")}</td></tr>
          </tbody>
        </table>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>GitHub</h2><p>Saving these settings pushes the repo branch and token to approved spokes.</p></div>
        <table class="data-table">
          <tbody>
            <tr><td>Repo URL</td><td>${escHtml(github.sim_repo_url || "—")}</td></tr>
            <tr><td>Repo Branch</td><td>${escHtml(github.sim_repo_branch || "—")}</td></tr>
            <tr><td>GitHub Token</td><td>${escHtml(github.github_token_configured ? "Configured" : "Not configured")}</td></tr>
          </tbody>
        </table>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Processing Modes</h2><p>Choose which credentials stay centralized on the hub versus distributed to spokes.</p></div>
        <div class="setup-form processing-modes-section mt-3">
          <div class="row g-2">
            <div class="col-md-4">
              <label class="form-label small">Central API</label>
              <select class="form-input" id="pm-central-api" onchange="saveProcessingMode('${escHtml(tenantId)}', 'central_api', this.value)"${disabled}>
                <option value="centralized"${processingModes.central_api === 'centralized' ? ' selected' : ''}>Centralized</option>
                <option value="distributed"${processingModes.central_api === 'distributed' ? ' selected' : ''}>Distributed</option>
              </select>
            </div>
            <div class="col-md-4">
              <label class="form-label small">Teams Webhook</label>
              <select class="form-input" id="pm-teams" onchange="saveProcessingMode('${escHtml(tenantId)}', 'teams', this.value)"${disabled}>
                <option value="centralized"${processingModes.teams === 'centralized' ? ' selected' : ''}>Centralized</option>
                <option value="distributed"${processingModes.teams === 'distributed' ? ' selected' : ''}>Distributed</option>
              </select>
            </div>
            <div class="col-md-4">
              <label class="form-label small">Email / SMTP</label>
              <select class="form-input" id="pm-email" onchange="saveProcessingMode('${escHtml(tenantId)}', 'email', this.value)"${disabled}>
                <option value="centralized"${processingModes.email === 'centralized' ? ' selected' : ''}>Centralized</option>
                <option value="distributed"${processingModes.email === 'distributed' ? ' selected' : ''}>Distributed</option>
              </select>
            </div>
          </div>
          <div id="processing-modes-msg" class="form-msg"></div>
        </div>
      </section>
      ${canManageTenant(tenantId) ? `
      <section class="setup-card" id="ts-onboarding-card">
        <div class="setup-card-header">
          <h2>Spoke Onboarding</h2>
          <p>Generate a Pre-Shared Key to allow spokes to self-register without manual approval.</p>
        </div>
        <div id="ts-onboarding-psk-list"></div>
        <div class="form-actions" style="margin-top:8px;">
          <button id="ts-onboarding-generate-btn" class="btn btn-sm btn-secondary" type="button">Add PSK</button>
        </div>
        <p id="ts-onboarding-status" class="form-hint" style="margin-top:6px;"></p>
      </section>` : ""}
    </div>
  `;
}

function renderTenantConfigPanel(data) {
  const spokes = data.spokes || [];
  const approved = spokes.filter(spoke => spoke.status === "approved");
  const siteMappings = uniqueValues(approved.map(spoke => Object.keys(spoke.config?.site_mappings || {})).flat());
  const relayRows = [
    renderConfigSummaryRow("Relay Enabled", summarizeConfigField(spokes, "relay_enabled")),
    renderConfigSummaryRow("Relay Server URL", summarizeConfigField(spokes, "relay_server_url")),
    renderConfigSummaryRow("Relay Poll Interval", summarizeConfigField(spokes, "relay_poll_interval")),
    renderConfigSummaryRow("Relay Tenant Hint", summarizeConfigField(spokes, "relay_tenant_hint")),
    renderConfigSummaryRow("Repo URL", summarizeConfigField(spokes, "repo_url")),
    renderConfigSummaryRow("Repo Branch", summarizeConfigField(spokes, "repo_branch")),
    renderConfigSummaryRow("Site Mappings", { value: siteMappings.length ? `${siteMappings.length} mapped sites` : "—", detail: `${approved.filter(spoke => Object.keys(spoke.config?.site_mappings || {}).length > 0).length}/${approved.length || 0} spokes` }),
  ].join("");

  const processingRows = data.processing?.islands?.length ? PROCESSING_FEATURES.map(feature => {
    const counts = data.processing.islands.reduce((acc, item) => {
      const mode = item.effective_modes?.[feature] || item.global_mode || data.processing.default_mode || "centralized";
      acc[mode] = (acc[mode] || 0) + 1;
      return acc;
    }, {});
    return `
      <tr>
        <td>${escHtml(feature.replace(/_/g, " "))}</td>
        <td>${escHtml(data.processing.default_mode || "centralized")}</td>
        <td>${escHtml(Object.entries(counts).map(([mode, count]) => `${mode}:${count}`).join(" • "))}</td>
      </tr>
    `;
  }).join("") : '<tr><td colspan="3" class="empty-state">No processing summary available.</td></tr>';

  return `
    <div class="tenant-detail-grid">
      <section class="setup-card">
        <div class="setup-card-header"><h2>Aggregated Spoke Config</h2><p>Common runtime configuration across spokes in this tenant.</p></div>
        <table class="data-table">
          <thead><tr><th>Setting</th><th>Observed Value</th><th>Coverage</th></tr></thead>
          <tbody>${relayRows}</tbody>
        </table>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Processing Defaults</h2><p>Tenant default mode plus effective spoke distribution per feature.</p></div>
        <table class="data-table">
          <thead><tr><th>Feature</th><th>Tenant Default</th><th>Effective Modes</th></tr></thead>
          <tbody>${processingRows}</tbody>
        </table>
      </section>
    </div>
  `;
}

function renderTenantDetail(data = tenantDetailState.data[tenantDetailState.tenantId]) {
  if (!tenantDetailState.open || !tenantDetailState.tenantId || !data) return;
  const summary = summarizeTenantSpokes(data.spokes || []);
  const meta = getTenantMeta(tenantDetailState.tenantId);

  $("#tenant-detail-title") && ($("#tenant-detail-title").textContent = meta.name || tenantDetailState.tenantId);
  $("#tenant-detail-subtitle") && ($("#tenant-detail-subtitle").textContent = `Tenant ID: ${tenantDetailState.tenantId} · Last sync ${relativeTime(summary.lastSync)}`);
  $("#tenant-detail-pills") && ($("#tenant-detail-pills").innerHTML = renderTenantSummaryPills(summary));

  $("#tenant-detail-dashboard-panel") && ($("#tenant-detail-dashboard-panel").innerHTML = renderTenantDashboardPanel(data, summary));
  $("#tenant-detail-spokes-panel") && ($("#tenant-detail-spokes-panel").innerHTML = renderTenantSpokesPanel(data));
  $("#tenant-detail-commands-panel") && ($("#tenant-detail-commands-panel").innerHTML = renderTenantCommandsPanel(data));
  $("#tenant-detail-setup-panel") && ($("#tenant-detail-setup-panel").innerHTML = renderTenantSetupPanel(data));
  $("#tenant-detail-config-panel") && ($("#tenant-detail-config-panel").innerHTML = renderTenantConfigPanel(data));
  hydrateTenantSetupPanel(data, $("#tenant-detail-setup-panel"));

  ["dashboard", "spokes", "commands", "setup", "config"].forEach(tabId => {
    $(`.tenant-detail-tab[data-tenant-detail-tab="${tabId}"]`)?.classList.toggle("active", tenantDetailState.activeTab === tabId);
    $("#tenant-detail-" + tabId + "-panel")?.classList.toggle("hidden", tenantDetailState.activeTab !== tabId);
  });
  setTenantDetailVisible(true);
  updateHubRefreshPausedState();
}

async function openTenantDetail(tenantId, tabId = "dashboard", force = false) {
  if (!tenantId || !currentUser) return;
  await setCurrentTenant(tenantId, false);
  tenantDetailState.open = true;
  tenantDetailState.tenantId = tenantId;
  tenantDetailState.activeTab = tabId;
  setTenantDetailVisible(true);
  updateHubRefreshPausedState();
  ["dashboard", "spokes", "commands", "setup", "config"].forEach(panelId => {
    const panel = $("#tenant-detail-" + panelId + "-panel");
    if (panel) panel.innerHTML = '<div class="empty-state">Loading…</div>';
  });
  const data = await loadTenantDetailData(force);
  if (!tenantDetailState.open || tenantDetailState.tenantId !== tenantId) return;
  renderTenantDetail(data);
}

async function loadDashboard(force = false) {
  const grid = $("#dashboard-grid");
  const empty = $("#dashboard-empty");
  if (!currentUser) {
    if (grid) grid.innerHTML = "";
    if (empty) empty.innerHTML = "";
    return;
  }
  if (currentUser) {
    dashboardTenantRows = [];
    $("#dash-tenants-pill") && ($("#dash-tenants-pill").textContent = `${tenants.length} tenants`);
    $("#dashboard-add-tenant-btn")?.classList.toggle("hidden", !currentUser?.is_superadmin);
    if (!grid || !empty) return;
    if (!tenants.length) {
      grid.innerHTML = "";
      $("#dash-spokes-pill") && ($("#dash-spokes-pill").textContent = '0 spokes');
      $("#dash-clients-pill") && ($("#dash-clients-pill").textContent = '0 clients');
      $("#dash-online-pill") && ($("#dash-online-pill").textContent = '0 alerts');
      empty.innerHTML = renderTenantDashboardEmptyState();
      empty.classList.remove("hidden");
      return;
    }
    const rows = await Promise.all(tenants.map(async tenant => {
      const [spokes, aggregate] = await Promise.all([
        ensureTenantSpokesFor(tenant.id, force),
        loadAggregateDataForTenant(tenant.id, "dashboard"),
      ]);
      const summary = summarizeTenantSpokes(spokes || []);
      return {
        id: tenant.id,
        name: tenant.name || tenant.id,
        summary,
        alert: summarizeTenantAlerts(summary, aggregate),
      };
    }));
    rows.sort((left, right) => String(left.name || left.id).localeCompare(String(right.name || right.id), undefined, { numeric: true, sensitivity: "base" }));
    dashboardTenantRows = rows;
    // Poll pending spokes on every dashboard refresh for tenant admins
    if (!currentUser?.is_superadmin && canManageTenant()) loadTenantPendingSpokes();
    const totalSpokes = rows.reduce((sum, row) => sum + row.summary.approvedCount, 0);
    const totalClients = rows.reduce((sum, row) => sum + row.summary.clientCount, 0);
    const totalAlerts = rows.filter(row => row.alert.tone === "alert").length;
    $("#dash-spokes-pill") && ($("#dash-spokes-pill").textContent = `${totalSpokes} spokes`);
    $("#dash-clients-pill") && ($("#dash-clients-pill").textContent = `${totalClients} clients`);
    $("#dash-online-pill") && ($("#dash-online-pill").textContent = totalAlerts ? `${totalAlerts} tenants need attention` : 'All tenants OK');
    grid.classList.remove("spoke-grid", "tenant-card-grid");
    empty.classList.toggle("hidden", rows.length > 0);
    empty.innerHTML = rows.length ? "" : renderTenantDashboardEmptyState();
    grid.innerHTML = renderDashboardTenantTable(rows);
    return;
  }
}

async function loadHubSimulations(force = false) {
  if (!currentTenantId || !currentUser) {
    hubCentralData = null;
    const refreshEl = document.getElementById("hub-sim-last-refreshed");
    if (refreshEl) refreshEl.textContent = "Last refreshed: —";
    renderHubSimChecksList();
    renderHubHwPanel();
    renderHubCcPanel();
    return;
  }
  try {
    const cached = hubCentralData || aggregateCentralData;
    const data = force || !cached ? await loadAggregateData("central") : cached;
    hubCentralData = data || { mode: "distributed", hub_central_config: {}, spokes: [] };
    aggregateCentralData = hubCentralData;
    const refreshEl = document.getElementById("hub-sim-last-refreshed");
    if (refreshEl) refreshEl.textContent = `Last refreshed: ${new Date().toLocaleTimeString()}`;
    if (hubSimActiveTab === "hub-simtop-hardware") {
      renderHubHwPanel();
      if (hubHwOpenCheckId) openHubHwDetail(hubHwOpenCheckId);
    } else if (hubSimActiveTab === "hub-simtop-clients") {
      renderHubCcPanel();
      if (hubCcOpenWsite) openHubCcDetail(hubCcOpenWsite);
    } else {
      renderHubSimChecksList();
      if (hubSimOpenCheckId) openHubSimDetail(hubSimOpenCheckId);
    }
  } catch (err) {
    const emptyEl = document.getElementById("hub-sim-checks-empty");
    if (emptyEl) {
      emptyEl.textContent = `Error loading simulation data: ${err.message}`;
      emptyEl.classList.remove("hidden");
    }
  }
}

// ── Hub Clients localStorage cache helpers ─────────────────────────────────
function hubClientsCacheKey() { return `hub_clients_${currentTenantId}`; }

function saveHubClientsCache(rows) {
  try { localStorage.setItem(hubClientsCacheKey(), JSON.stringify(rows)); } catch (_) {}
}

function loadHubClientsCache() {
  try { const s = localStorage.getItem(hubClientsCacheKey()); return s ? JSON.parse(s) : null; }
  catch (_) { return null; }
}

async function loadClients(force = false) {
  if (!currentTenantId) {
    aggregateClientRows = [];
    renderClientRowsForHub();
    return;
  }

  // In-memory cache still valid — render immediately, revalidate silently.
  if (!force && aggregateClientRows.length) {
    renderClientRowsForHub();
    loadAggregateData("clients").then(data => {
      if (data) {
        aggregateClientRows = normalizeAggregateClientRows(data);
        primeHubClientExpandedSet([...new Set(aggregateClientRows.map(hubClientSiteKey))]);
        saveHubClientsCache(aggregateClientRows);
        renderClientRowsForHub();
      }
    }).catch(() => {});
    return;
  }

  // Show localStorage cache immediately while fetching fresh data.
  const cached = loadHubClientsCache();
  if (cached && cached.length) {
    aggregateClientRows = cached;
    primeHubClientExpandedSet([...new Set(aggregateClientRows.map(hubClientSiteKey))]);
    renderClientRowsForHub();
    loadAggregateData("clients").then(data => {
      if (data) {
        aggregateClientRows = normalizeAggregateClientRows(data);
        primeHubClientExpandedSet([...new Set(aggregateClientRows.map(hubClientSiteKey))]);
        saveHubClientsCache(aggregateClientRows);
        renderClientRowsForHub();
      }
    }).catch(() => {});
    return;
  }

  // No cache — blocking fetch.
  const data = await loadAggregateData("clients");
  aggregateClientRows = normalizeAggregateClientRows(data);
  primeHubClientExpandedSet([...new Set(aggregateClientRows.map(hubClientSiteKey))]);
  if (aggregateClientRows.length) saveHubClientsCache(aggregateClientRows);
  renderClientRowsForHub();
}

function summarizeHubConfigState(spoke) {
  if (!spoke || spoke.status !== "approved") return { label: spoke?.status || "pending", className: "pending" };
  const desired = Number(spoke.config_version || 0);
  const applied = Number(spoke.applied_config_version || 0);
  if (desired > applied) return { label: `Pending v${desired}`, className: "pending" };
  if (desired > 0) return { label: `Applied v${applied}`, className: "approved" };
  return { label: "No push yet", className: "offline" };
}

function defaultFleetRecloneStatus() {
  return { any_running: false, total_vms: 0, completed: 0, failed: 0, default_concurrency: 3, spokes: [] };
}

function defaultUsbProvisioningStatus() {
  return { total_slots: 0, used_slots: 0, auto_provision_on: false, spokes: [] };
}

function normalizeHubVmServerConcurrency(value, fallback = 3) {
  const parsed = Number.parseInt(String(value ?? fallback), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1, Math.min(10, parsed));
}

function syncHubVmServerConcurrencyFromStatus() {
  const tenantId = getActiveTenantId();
  if (!tenantId) return;
  const status = aggregateFleetRecloneStatus || defaultFleetRecloneStatus();
  if (hubVmServerFleetConcurrencyTenant !== tenantId) {
    hubVmServerFleetConcurrencyTenant = tenantId;
    hubVmServerFleetConcurrencyDraft = normalizeHubVmServerConcurrency(status.default_concurrency, 3);
  }
}

function hubVmServerFleetStatusMeta(status) {
  if (status?.any_running) return { label: "Running", className: "badge-blue" };
  if (Number(status?.total_vms || 0) > 0 && Number(status?.completed || 0) + Number(status?.failed || 0) >= Number(status?.total_vms || 0)) {
    return { label: "Done", className: "badge-grey" };
  }
  return { label: "Idle", className: "badge-grey" };
}

function scheduleHubVmServerFleetPoll() {
  if (hubVmServerFleetPollTimer) {
    clearTimeout(hubVmServerFleetPollTimer);
    hubVmServerFleetPollTimer = null;
  }
  if (activeTab !== "vm-server" || hubVmServerSelectedSpoke || !(aggregateFleetRecloneStatus?.any_running)) return;
  hubVmServerFleetPollTimer = window.setTimeout(async () => {
    hubVmServerFleetPollTimer = null;
    if (activeTab !== "vm-server" || hubVmServerSelectedSpoke) return;
    await loadHubVmServerAggregateStatus();
    renderHubVmServer();
  }, 10000);
}

async function loadHubVmServerAggregateStatus() {
  const tenantId = getActiveTenantId();
  if (!currentUser || !tenantId) {
    aggregateFleetRecloneStatus = defaultFleetRecloneStatus();
    aggregateUsbProvisioningStatus = defaultUsbProvisioningStatus();
    return;
  }
  const [fleetRes, usbRes] = await Promise.all([
    apiFetch(`/api/${encodeURIComponent(tenantId)}/aggregate/fleet-reclone-status`),
    apiFetch(`/api/${encodeURIComponent(tenantId)}/aggregate/usb-provisioning-status`),
  ]);
  aggregateFleetRecloneStatus = fleetRes?.ok ? ((await fleetRes.json()) || defaultFleetRecloneStatus()) : (aggregateFleetRecloneStatus || defaultFleetRecloneStatus());
  aggregateUsbProvisioningStatus = usbRes?.ok ? ((await usbRes.json()) || defaultUsbProvisioningStatus()) : (aggregateUsbProvisioningStatus || defaultUsbProvisioningStatus());
  syncHubVmServerConcurrencyFromStatus();
}

async function startHubFleetReclone() {
  const tenantId = getActiveTenantId();
  if (!tenantId || !canManageTenant(tenantId)) {
    showToast("Tenant Viewer access is read-only.", "warn");
    return;
  }
  const input = $("#hub-fleet-reclone-concurrency");
  const concurrency = normalizeHubVmServerConcurrency(input?.value, hubVmServerFleetConcurrencyDraft || 3);
  hubVmServerFleetConcurrencyDraft = concurrency;
  if (input) input.value = String(concurrency);
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/aggregate/fleet-reclone`, {
    method: "POST",
    body: { concurrency },
  });
  const data = await readJson(res);
  if (!res?.ok) {
    showToast(data?.detail || "Unable to queue fleet reclone.", "error");
    return;
  }
  showToast(`Queued fleet reclone for ${data?.queued || 0} spoke(s).`, "ok");
  await loadVmServer(true);
}

function renderHubVmServer() {
  const container = $("#hub-vm-server-content");
  if (!container) return;
  const tenantId = getActiveTenantId();
  const hosts = aggregateProxmoxHosts || [];
  const fleet = aggregateFleetRecloneStatus || defaultFleetRecloneStatus();
  const usbProvisioning = aggregateUsbProvisioningStatus || defaultUsbProvisioningStatus();
  const vmCount = hosts.reduce((sum, h) => sum + Number(h.vm_count || 0), 0);
  const usbCount = hosts.reduce((sum, h) => sum + Number(h.usb_count || 0), 0);
  if (!hubVmServerSelectedSpoke && _hubNodeRefreshTimer) {
    clearInterval(_hubNodeRefreshTimer);
    _hubNodeRefreshTimer = null;
  }
  $("#hub-vm-hosts-pill") && ($("#hub-vm-hosts-pill").textContent = `${hosts.length} hosts`);
  $("#hub-vm-vms-pill") && ($("#hub-vm-vms-pill").textContent = `${vmCount} VMs`);
  $("#hub-vm-usb-pill") && ($("#hub-vm-usb-pill").textContent = `${usbCount} USB devices`);

  if (hubVmServerSelectedSpoke) {
    const host = hosts.find(h => h.spoke_id === hubVmServerSelectedSpoke) || hubVmServerSelectedSpoke;
    if (hubVmServerActiveSubtab === "console" && hubVmConsoleState.rfb && hubVmConsoleState.spokeId === hubVmServerSelectedSpoke && container.querySelector("#hub-vm-console-screen")) {
      return;
    }
    if (hubVmServerActiveSubtab === "terminal" && hubSpokeShellState.socket && hubSpokeShellState.spokeId === hubVmServerSelectedSpoke && container.querySelector("#hub-spoke-shell-screen")) {
      return;
    }
    renderHubVmServerDetail(container, host);
    return;
  }

  const fleetMeta = hubVmServerFleetStatusMeta(fleet);
  const fleetPct = Number(fleet.total_vms || 0) > 0 ? Math.max(0, Math.min(100, Math.round((Number(fleet.completed || 0) / Number(fleet.total_vms || 0)) * 100))) : 0;
  const usbPct = Number(usbProvisioning.total_slots || 0) > 0 ? Math.max(0, Math.min(100, Math.round((Number(usbProvisioning.used_slots || 0) / Number(usbProvisioning.total_slots || 0)) * 100))) : 0;
  const disabled = fleet.any_running || !canManageTenant(tenantId);
  const readonlyNote = canManageTenant(tenantId) ? "" : '<div class="tenant-detail-note">Tenant Viewer access: fleet controls are read-only.</div>';

  // Compute active provisioning counts (cloning + pending check-in) per host and fleet-wide
  function hostActiveProvCount(host) {
    const usbDevices = host.usb_devices || [];
    const proxmoxVms = host.proxmox_vms || [];
    const cloningVmids = new Set(
      usbDevices.filter(e => e && e.prov_status === "provisioning").map(e => String(e.vmid))
    );
    const cloning = cloningVmids.size;
    const pendingCheckin = proxmoxVms.filter(vm => vm && vm.pending_checkin === true && !cloningVmids.has(String(vm.vmid))).length;
    return cloning + pendingCheckin;
  }
  const hostProvCounts = hosts.map(h => ({ host: h, count: hostActiveProvCount(h) }));
  const totalActiveProv = hostProvCounts.reduce((sum, x) => sum + x.count, 0);
  const totalCapacity = Number(usbProvisioning.total_slots || usbProvisioning.total_dongles || 0);
  const activeProvPct = totalCapacity > 0 ? Math.max(0, Math.min(100, Math.round((totalActiveProv / totalCapacity) * 100))) : 0;
  const spokesWithActiveProv = hostProvCounts.filter(x => x.count > 0).length;
  container.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-bottom:12px;">
      <section class="setup-card">
        <div class="setup-card-header" style="display:flex;align-items:center;gap:8px;justify-content:space-between;">
          <div><h2>Fleet Reclone</h2><p>Queue a rolling reclone on every approved spoke.</p></div>
          <span class="badge ${fleetMeta.className}">${escHtml(fleetMeta.label)}</span>
        </div>
        <div style="font-weight:600;margin-bottom:6px;">${escHtml(String(fleet.completed || 0))} / ${escHtml(String(fleet.total_vms || 0))} VMs recloned</div>
        <div class="progress-bar-wrap" style="margin-bottom:8px;"><div class="progress-bar" style="width:${fleetPct}%"></div></div>
        <div class="muted" style="font-size:0.82rem;margin-bottom:8px;">${fleet.any_running ? "Polling every 10s while fleet reclone is running." : `Failed: ${escHtml(String(fleet.failed || 0))}`}</div>
        ${readonlyNote}
        <div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;">
          <label class="form-group" style="margin:0;min-width:100px;">
            <span class="form-label">Concurrency</span>
            <input id="hub-fleet-reclone-concurrency" class="form-input" type="number" min="1" max="10" value="${escHtml(String(hubVmServerFleetConcurrencyDraft || 3))}"${canManageTenant(tenantId) ? "" : " disabled"}>
          </label>
          <button id="hub-fleet-reclone-btn" class="btn btn-primary" type="button"${disabled ? " disabled" : ""}>🔄 Reclone All Spokes</button>
        </div>
      </section>
      <section class="setup-card">
        <div class="setup-card-header" style="display:flex;align-items:center;gap:8px;justify-content:space-between;">
          <div><h2>Auto-Provisioning</h2><p>USB dongle capacity and active VM cloning across approved spokes.</p></div>
          <span class="badge ${usbProvisioning.auto_provision_on ? "badge-blue" : "badge-grey"}" id="hub-autoprovisioning-toggle" style="cursor:pointer;" title="Click to toggle">${usbProvisioning.auto_provision_on ? "On" : "Off"}</span>
        </div>
        <div style="font-weight:600;margin-bottom:4px;">${escHtml(String(usbProvisioning.used_slots || 0))} deployed VMs / ${escHtml(String(usbProvisioning.total_dongles || usbProvisioning.total_slots || 0))} USB dongles</div>
        <div class="progress-bar-wrap" style="margin-bottom:8px;"><div class="progress-bar" style="width:${usbPct}%"></div></div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;">
          <span class="muted" style="font-size:0.82rem;">${escHtml(String((usbProvisioning.spokes || []).filter(spoke => spoke.auto_provision).length))} spoke(s) enabled</span>
          ${totalActiveProv > 0 ? `<span class="muted" style="font-size:0.82rem;">🔵 ${escHtml(String(totalActiveProv))} VM(s) cloning / checking in</span>` : `<span class="muted" style="font-size:0.82rem;">⚫ No active cloning</span>`}
        </div>
      </section>
      <section class="setup-card">
        <div class="setup-card-header">
          <div><h2>Update All</h2><p>Update Proxmox agents first, then each spoke automatically once its agent confirms updated.</p></div>
        </div>
        <div class="muted" style="font-size:0.82rem;margin-bottom:10px;">Agents update immediately via the live connection. Each spoke restarts as soon as its agent confirms the new version.</div>
        ${readonlyNote}
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button id="hub-update-all-btn" class="btn btn-primary" type="button"${canManageTenant(tenantId) ? "" : " disabled"}>⬆️ Update All</button>
          <button id="hub-update-spokes-btn" class="btn btn-secondary" type="button"${canManageTenant(tenantId) ? "" : " disabled"}>↻ Update Spokes</button>
        </div>
      </section>
    </div>
    ${hosts.length ? `
      <div class="hub-vmserver-list">
        ${hosts.map(host => {
          const online = host.spoke_online;
          const reclone = host.reclone_state || {};
          const recloneStatus = reclone.status || "idle";
          return `
            <div class="setup-card hub-vmserver-spoke-card" role="button" tabindex="0"
                 data-spoke-id="${escHtml(host.spoke_id)}" style="cursor:pointer;">
              <div class="panel-header">
                <span class="server-node-name">${escHtml(spokeDisplayName(host, "Spoke"))}</span>
                <span class="stat-pill ${online ? "online" : "offline"}">${online ? "Online" : "Offline"}</span>
                <span class="stat-pill">${escHtml(String(host.vm_count || 0))} VMs</span>
                <span class="stat-pill">${escHtml(String(host.usb_count || 0))} USB</span>
                <span class="stat-pill${hostActiveProvCount(host) > 0 ? " badge-blue" : ""}">${escHtml(String(hostActiveProvCount(host)))} provisioning</span>
                ${recloneStatus !== "idle" ? `<span class="stat-pill badge-${recloneStatus === "running" ? "blue" : "grey"}">${escHtml(recloneStatus)}</span>` : ""}
                <span style="margin-left:auto;color:var(--color-muted);font-size:1rem;line-height:1;">›</span>
              </div>
              <div class="spoke-meta-line">
                Agent ${escHtml(host.proxmox?.agent_version || "—")} &nbsp;·&nbsp;
                PVE ${escHtml(host.proxmox?.pve_version || "—")} &nbsp;·&nbsp;
                ${host.proxmox?.connected
                  ? "🟢 Proxmox connected"
                  : (host.spoke_online ? "⚠️ Proxmox agent not reporting" : "⚫ Proxmox disconnected")}
              </div>
            </div>`;
        }).join("")}
      </div>`
      : '<div class="empty-state">No Proxmox telemetry reported for this tenant.</div>'}
  `;

  $("#hub-fleet-reclone-concurrency", container)?.addEventListener("input", event => {
    hubVmServerFleetConcurrencyDraft = normalizeHubVmServerConcurrency(event.target.value, hubVmServerFleetConcurrencyDraft || 3);
  });
  $("#hub-fleet-reclone-btn", container)?.addEventListener("click", () => {
    startHubFleetReclone().catch(err => showToast(err?.message || "Unable to queue fleet reclone.", "error"));
  });
  const autoProvToggle = $("#hub-autoprovisioning-toggle", container);
  if (autoProvToggle) {
    autoProvToggle.addEventListener("click", async () => {
      const currentlyOn = autoProvToggle.textContent.trim() === "On";
      const enable = !currentlyOn;
      try {
        const res = await apiFetch(`/api/${tenantId}/aggregate/toggle-auto-provision`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enable }),
        });
        if (!res.ok) throw new Error(await res.text());
        autoProvToggle.textContent = enable ? "On" : "Off";
        autoProvToggle.className = `badge ${enable ? "badge-blue" : "badge-grey"}`;
        autoProvToggle.style.cursor = "pointer";
        showToast(`Auto-provisioning turned ${enable ? "on" : "off"} for all spokes.`, "success");
      } catch (err) {
        showToast(err?.message || "Failed to toggle auto-provisioning.", "error");
      }
    });
  }
  $("#hub-update-spokes-btn", container)?.addEventListener("click", async () => {
    const btn = $("#hub-update-spokes-btn", container);
    if (btn) { btn.disabled = true; btn.textContent = "Queuing…"; }
    try {
      const res = await apiFetch(`/api/${tenantId}/update-spoke-servers`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      showToast("Spoke server updates queued — spokes will restart momentarily.", "ok");
    } catch (err) {
      showToast(err?.message || "Failed to queue spoke updates.", "error");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "↻ Update Spokes"; }
    }
  });
  $("#hub-update-all-btn", container)?.addEventListener("click", async () => {
    const btn = $("#hub-update-all-btn", container);
    if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
    try {
      const res = await apiFetch(`/api/${tenantId}/update-all`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      showUpdateProgressModal(tenantId, data.job_id, data.spokes);
    } catch (err) {
      showToast(err?.message || "Update All failed.", "error");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "⬆️ Update All"; }
    }
  });
  container.querySelectorAll(".hub-vmserver-spoke-card").forEach(card => {
    const drillIn = () => {
      const spokeId = card.dataset.spokeId;
      hubVmServerSelectedSpoke = spokeId;
      renderHubVmServer();
    };
    card.addEventListener("click", drillIn);
    card.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); drillIn(); }
    });
  });
  scheduleHubVmServerFleetPoll();
}

// ── Hub VM Server drill-in view ──────────────────────────────────────────────
let hubVmServerActiveSubtab = "vms";

function renderHubVmServerDetail(container, host) {
  const spokeId = host.spoke_id;
  const spokeName = escHtml(spokeDisplayName(host, "Spoke"));
  const vms = Array.isArray(host.proxmox_vms) ? host.proxmox_vms : [];
  const px = host.proxmox || {};
  const node = host.node || px.node || {};
  const usb = Array.isArray(host.usb_devices) ? host.usb_devices : (Array.isArray(px.usb_state) ? px.usb_state : []);
  const usbState = Array.isArray(px.usb_state) ? px.usb_state : usb;
  const presentUsb = Array.isArray(px.present_usb) ? px.present_usb : [];
  const unknownUsb = Array.isArray(px.unknown_usb) ? px.unknown_usb : [];
  const vhDevices = px.vh_devices || {};
  const reclone = host.reclone_state || {};
  const spokeCfg = host.spoke_config || {};
  const certifiedUsb = parseJsonList(spokeCfg.usb_vidpids || "[]");

  const hostCfg = host.telemetry?.config || {};
  const configuredTemplateIds = new Set([
    String(hostCfg.vm_image_1_template_id || 100),
    String(hostCfg.vm_image_2_template_id || 200),
  ]);
  const templateVms = vms.filter(v =>
    v.is_template === true || v.is_template === "true" || configuredTemplateIds.has(String(v.vmid))
  );
  const nonTpl = vms.filter(v => !templateVms.includes(v));
  const containerVms = nonTpl.filter(v => v.type === "lxc");
  const qemuVms = nonTpl.filter(v => v.type !== "lxc");
  const simVms = qemuVms.filter(v => Number(v.vmid) > 90000);
  const otherVms = qemuVms.filter(v => !simVms.includes(v));

  const telem = host.telemetry || {};
  const clients = Array.isArray(telem.clients) ? telem.clients : [];
  const apiServer = telem.api_server || {};
  const central = telem.central || {};

  const isOnline = !!host.spoke_online;
  const statusCls = isOnline ? "online" : "offline";
  const statusLabel = isOnline ? "Online" : "Offline";
  const canUseTerminal = canManageTenant(getActiveTenantId());

  if (!canUseTerminal && hubVmServerActiveSubtab === 'terminal') {
    hubVmServerActiveSubtab = 'vms';
  }

  if (_hubNodeRefreshTimer) clearInterval(_hubNodeRefreshTimer);
  _hubNodeRefreshTimer = setInterval(() => {
    if (activeTab !== "vm-server" || hubVmServerSelectedSpoke !== spokeId) {
      clearInterval(_hubNodeRefreshTimer);
      _hubNodeRefreshTimer = null;
      return;
    }
    if (["console", "terminal"].includes(hubVmServerActiveSubtab)) return;
    loadVmServer();
  }, 10000);

  const mainTabs = [
    { id: "vms",         label: "VMs",           badge: vms.length },
    { id: "console",     label: "Console",       badge: null },
    ...(canUseTerminal ? [{ id: "terminal", label: "Terminal", badge: null }] : []),
    { id: "usb",         label: "USB (T2)",      badge: usbState.length },
    { id: "t3",          label: "IoT (T3)",      badge: null },
    { id: "virtualhere", label: "VirtualHere",   badge: null },
    { id: "commands",    label: "Command Queue", badge: null },
    { id: "details",     label: "Details",       badge: null },
  ];
  const extraTabs = [
    { id: "clients",    label: "Clients",    badge: clients.length },
    { id: "central",    label: "Central",    badge: null },
    { id: "api-server", label: "API Server", badge: null },
  ];

  const tabBtn = (t) => {
    const bdg = t.badge !== null ? ` <span class="badge-count">${t.badge}</span>` : "";
    return `<button class="tab hub-vmserver-nodetab ${t.id === hubVmServerActiveSubtab ? "active" : ""}" data-hvmsubtab="${t.id}" type="button">${escHtml(t.label)}${bdg}</button>`;
  };

  const hwFaults = Array.isArray(px.hw_faults?.faults) ? px.hw_faults.faults : [];

  container.innerHTML = `
    <div class="hub-node-ctx">
      <div class="hub-node-ctx-row1">
        <button class="tab tab-back" id="hub-vmserver-back-btn" type="button">← VM Server</button>
        <span class="tab-nav-sep" aria-hidden="true"></span>
        <span class="hub-node-ctx-row1-title">${spokeName}</span>
        <span class="hub-node-ctx-row1-status ${statusCls}">${statusLabel}</span>
        ${host.vm_count != null ? `<span class="hub-node-ctx-row1-pill">${escHtml(String(host.vm_count))} VMs</span>` : ""}
        ${host.usb_count != null ? `<span class="hub-node-ctx-row1-pill">· ${escHtml(String(host.usb_count))} USB</span>` : ""}
        ${px.agent_version ? `<span class="hub-node-ctx-row1-pill">· Agent v${escHtml(px.agent_version)}</span>` : ""}
        ${px.pve_version ? `<span class="hub-node-ctx-row1-pill">· PVE ${escHtml(px.pve_version)}</span>` : ""}
        <span class="hub-node-ctx-row1-actions">
          <button class="btn btn-secondary btn-small" id="hub-vmserver-update-agent-btn" type="button" title="Update proxmox agent on this node">⬆️ Update Agent</button>
        </span>
      </div>
      <nav class="hub-node-ctx-row2" role="tablist">
        ${mainTabs.map(tabBtn).join("")}
        <span class="tab-nav-sep" aria-hidden="true"></span>
        ${extraTabs.map(tabBtn).join("")}
      </nav>
      ${hwFaults.length ? `
      <div style="padding:8px 16px;background:rgba(243,156,18,0.1);border-bottom:1px solid var(--border);">
        <strong style="font-size:0.85rem;">⚠️ Hardware Faults (${hwFaults.length})</strong>
        <ul style="margin:4px 0 0;padding-left:18px;font-size:0.82rem;">
          ${hwFaults.slice(-5).map(f => `<li>${escHtml(f.type || f.check || "fault")} — ${escHtml(f.message || f.detail || JSON.stringify(f))}</li>`).join("")}
        </ul>
      </div>` : ""}
      <div id="hub-vmserver-subpanel" class="hub-node-ctx-subpanel"></div>
    </div>`;

  document.getElementById("hub-vmserver-back-btn")?.addEventListener("click", () => {
    if (_hubNodeRefreshTimer) {
      clearInterval(_hubNodeRefreshTimer);
      _hubNodeRefreshTimer = null;
    }
    disconnectHubVmConsole({ rerender: false, preserveSelection: false });
    disconnectHubSpokeShell({ rerender: false, preserveSelection: false });
    hubVmServerSelectedSpoke = null;
    renderHubVmServer();
  });

  document.getElementById("hub-vmserver-update-agent-btn")?.addEventListener("click", async () => {
    const btn = document.getElementById("hub-vmserver-update-agent-btn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Updating…";
    }
    try {
      await sendHubProxmoxCommand(getActiveTenantId(), spokeId, "update_agent", {});
      showToast("Agent update queued for this node.", "ok");
    } catch (err) {
      showToast(`Update failed: ${err.message}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "⬆️ Update Agent";
      }
    }
  });

  document.querySelectorAll(".hub-vmserver-nodetab").forEach(btn => {
    btn.addEventListener("click", () => {
      const nextSubtab = btn.dataset.hvmsubtab;
      if (hubVmServerActiveSubtab === "console" && nextSubtab !== "console" && hubVmConsoleState.rfb) {
        disconnectHubVmConsole({ rerender: false, preserveSelection: true });
      }
      if (hubVmServerActiveSubtab === "terminal" && nextSubtab !== "terminal" && hubSpokeShellState.socket) {
        disconnectHubSpokeShell({ rerender: false, preserveSelection: true });
      }
      hubVmServerActiveSubtab = nextSubtab;
      document.querySelectorAll(".hub-vmserver-nodetab").forEach(b => b.classList.toggle("active", b === btn));
      renderHubVmServerSubpanel(spokeId, hubVmServerActiveSubtab, {
        simVms, otherVms, containerVms, templateVms, usb, usbState, presentUsb, unknownUsb, vhDevices,
        reclone, px, node, host, clients, apiServer, central, certifiedUsb, spokeCfg,
      });
    });
  });

  renderHubVmServerSubpanel(spokeId, hubVmServerActiveSubtab, {
    simVms, otherVms, containerVms, templateVms, usb, usbState, presentUsb, unknownUsb, vhDevices,
    reclone, px, node, host, clients, apiServer, central, certifiedUsb, spokeCfg,
  });
}

function renderHubVmServerSubpanel(spokeId, subtab, { simVms, otherVms, containerVms, templateVms, usb, usbState = [], presentUsb = [], unknownUsb = [], vhDevices = {}, reclone, px, node = {}, host = {}, clients = [], apiServer = {}, central = {}, certifiedUsb = [], spokeCfg = {} }) {
  const panel = document.getElementById("hub-vmserver-subpanel");
  if (!panel) return;

  if (subtab === "vms") {
    panel.innerHTML = renderHubVmServerVmsPanel(spokeId, { simVms, otherVms, containerVms, templateVms, usbState, reclone, px });
    wireHubVmActions(panel, spokeId);
  } else if (subtab === "console") {
    panel.innerHTML = renderHubVmServerConsolePanel(spokeId, host, node, spokeCfg);
    wireHubVmServerConsolePanel(panel, spokeId, host, node, spokeCfg);
  } else if (subtab === "terminal") {
    panel.innerHTML = renderHubVmServerTerminalPanel(spokeId, host);
    wireHubVmServerTerminalPanel(panel, spokeId, host);
  } else if (subtab === "usb") {
    panel.innerHTML = renderHubVmServerUsbPanel({ usb, usbState, presentUsb, unknownUsb, certifiedUsb, vms: Array.isArray(host.proxmox_vms) ? host.proxmox_vms : [] });
    wireHubVmActions(panel, spokeId);
  } else if (subtab === "t3") {
    panel.innerHTML = renderHubVmServerT3Panel(otherVms, usb);
  } else if (subtab === "virtualhere") {
    panel.innerHTML = renderHubVmServerVirtualHerePanel(vhDevices, presentUsb, px);
  } else if (subtab === "commands") {
    panel.innerHTML = renderHubVmServerCommandQueuePanel();
    loadHubNodeCommandQueue(panel, spokeId);
    wireHubNodeCommandQueue(panel, spokeId);
  } else if (subtab === "details") {
    panel.innerHTML = renderHubVmServerDetailsPanel(node, px, host, spokeCfg);
    wireHubVmServerDetailsPanel(panel, spokeId);
  } else if (subtab === "clients") {
    panel.innerHTML = renderHubVmServerClientsPanel(clients);
  } else if (subtab === "central") {
    panel.innerHTML = renderHubVmServerCentralPanel(central);
  } else if (subtab === "api-server") {
    panel.innerHTML = renderHubVmServerApiServerPanel(apiServer);
  }
}

function getHubVmServerHostBySpokeId(spokeId) {
  return (aggregateProxmoxHosts || []).find(host => host.spoke_id === spokeId) || null;
}

function getHubVmConsoleFallbackHost(host = {}, node = {}) {
  return String(host.proxmox_host || node.hostname || host.hostname || "").trim();
}

async function getNoVNCRfbClass() {
  if (window.__noVNCRFB) return window.__noVNCRFB;
  if (!hubNoVNCRfbPromise) {
    hubNoVNCRfbPromise = import('https://cdn.jsdelivr.net/gh/novnc/noVNC@v1.4.0/core/rfb.js')
      .then(mod => mod.default || mod)
      .then(RFB => {
        window.__noVNCRFB = RFB;
        return RFB;
      });
  }
  return hubNoVNCRfbPromise;
}

function ensureHubAssetStylesheet(href) {
  if (!href || document.querySelector(`link[data-hub-asset="${href}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  link.dataset.hubAsset = href;
  document.head.appendChild(link);
}

function loadHubAssetScript(src, readyCheck) {
  if (typeof readyCheck === 'function' && readyCheck()) return Promise.resolve();
  const existing = document.querySelector(`script[data-hub-asset="${src}"]`);
  if (existing?._hubPromise) return existing._hubPromise;
  const script = existing || document.createElement('script');
  const promise = new Promise((resolve, reject) => {
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Failed to load ${src}`));
  });
  script.src = src;
  script.async = true;
  script.dataset.hubAsset = src;
  script._hubPromise = promise;
  if (!existing) document.head.appendChild(script);
  return promise;
}

async function ensureHubSpokeShellDeps() {
  if (window.Terminal && window.FitAddon?.FitAddon) return;
  if (!hubSpokeShellDepsPromise) {
    ensureHubAssetStylesheet(HUB_SPOKE_SHELL_XTERM_CSS);
    hubSpokeShellDepsPromise = loadHubAssetScript(HUB_SPOKE_SHELL_XTERM_JS, () => window.Terminal)
      .then(() => loadHubAssetScript(HUB_SPOKE_SHELL_FIT_JS, () => window.FitAddon?.FitAddon));
  }
  await hubSpokeShellDepsPromise;
}

function hubVmConsoleStatusText() {
  const { status, error, vmid, vmname } = hubVmConsoleState;
  const label = vmname ? `${vmname} (VM ${vmid})` : (vmid ? `VM ${vmid}` : 'No VM selected');
  if (status === 'connected') return `${label} · Connected`;
  if (status === 'connecting') return `${label} · Connecting…`;
  if (status === 'disconnected') return `${label} · Disconnected`;
  if (status === 'error') return `${label} · ${error || 'Connection failed'}`;
  return label;
}

function syncHubVmConsolePanel(panel) {
  if (!panel) return;
  const statusEl = panel.querySelector('#hub-vm-console-status');
  const emptyEl = panel.querySelector('#hub-vm-console-empty');
  const reconnectBtn = panel.querySelector('#hub-vm-console-reconnect-btn');
  const disconnectBtn = panel.querySelector('#hub-vm-console-disconnect-btn');
  const screen = panel.querySelector('#hub-vm-console-screen');
  if (statusEl) statusEl.textContent = hubVmConsoleStatusText();
  if (disconnectBtn) disconnectBtn.disabled = !hubVmConsoleState.rfb;
  if (reconnectBtn) reconnectBtn.disabled = hubVmConsoleState.status === 'connecting' || !hubVmConsoleState.spokeId || !hubVmConsoleState.vmid;
  if (!emptyEl || !screen) return;
  let message = 'Select a VM and click Console to start a session.';
  if (hubVmConsoleState.missingCredentials) {
    message = hubVmConsoleState.error || 'Configure Proxmox credentials first.';
  } else if (hubVmConsoleState.status === 'connecting') {
    message = 'Connecting to the VM console…';
  } else if (hubVmConsoleState.status === 'connected') {
    message = '';
  } else if (hubVmConsoleState.status === 'disconnected') {
    message = hubVmConsoleState.error || 'Console disconnected. Use Reconnect to start a new session.';
  } else if (hubVmConsoleState.status === 'error') {
    message = hubVmConsoleState.error || 'Unable to open the console.';
  }
  emptyEl.innerHTML = message ? `<div>${escHtml(message)}</div>` : '';
  emptyEl.classList.toggle('hidden', !message);
  screen.style.display = hubVmConsoleState.status === 'connected' || hubVmConsoleState.status === 'connecting' ? '' : 'none';
}

function disconnectHubVmConsole({ rerender = true, preserveSelection = true } = {}) {
  const spokeId = hubVmConsoleState.spokeId;
  const currentRfb = hubVmConsoleState.rfb;
  hubVmConsoleState.rfb = null;
  hubVmConsoleState.sessionId = '';
  hubVmConsoleState.connectToken += 1;
  if (!preserveSelection) {
    hubVmConsoleState.spokeId = null;
    hubVmConsoleState.vmid = null;
    hubVmConsoleState.vmtype = 'qemu';
    hubVmConsoleState.vmname = '';
    hubVmConsoleState.missingCredentials = false;
    hubVmConsoleState.error = '';
    hubVmConsoleState.status = 'idle';
  } else {
    hubVmConsoleState.status = hubVmConsoleState.vmid ? 'disconnected' : 'idle';
  }
  if (currentRfb) {
    try { currentRfb.disconnect(); } catch (_) {}
  }
  if (rerender && activeTab === 'vm-server' && hubVmServerSelectedSpoke === spokeId && hubVmServerActiveSubtab === 'console') {
    const host = getHubVmServerHostBySpokeId(spokeId) || { spoke_id: spokeId };
    const panel = document.getElementById('hub-vmserver-subpanel');
    if (panel) {
      panel.innerHTML = renderHubVmServerConsolePanel(spokeId, host, host.node || {}, host.spoke_config || {});
      wireHubVmServerConsolePanel(panel, spokeId, host, host.node || {}, host.spoke_config || {});
    }
  }
}

function hubSpokeShellStatusText() {
  if (!hubSpokeShellState.spokeId) return 'Opening terminal…';
  if (hubSpokeShellState.status === 'connected') return 'Connected to spoke host shell';
  if (hubSpokeShellState.status === 'connecting') return 'Connecting to spoke host shell…';
  if (hubSpokeShellState.status === 'disconnected') return hubSpokeShellState.error || 'Terminal disconnected. Use Reconnect to start a new shell.';
  if (hubSpokeShellState.status === 'error') return hubSpokeShellState.error || 'Unable to open the terminal.';
  return 'Opening terminal…';
}

function syncHubSpokeShellPanel(panel) {
  if (!panel) return;
  const statusEl = panel.querySelector('#hub-spoke-shell-status');
  const emptyEl = panel.querySelector('#hub-spoke-shell-empty');
  const reconnectBtn = panel.querySelector('#hub-spoke-shell-reconnect-btn');
  const disconnectBtn = panel.querySelector('#hub-spoke-shell-disconnect-btn');
  if (statusEl) statusEl.textContent = hubSpokeShellStatusText();
  if (disconnectBtn) disconnectBtn.disabled = !hubSpokeShellState.socket;
  if (reconnectBtn) reconnectBtn.disabled = hubSpokeShellState.status === 'connecting' || !hubSpokeShellState.spokeId;
  if (!emptyEl) return;
  let message = '';
  if (hubSpokeShellState.status === 'connecting') message = 'Connecting to the spoke host shell…';
  else if (hubSpokeShellState.status === 'disconnected' || hubSpokeShellState.status === 'error') message = hubSpokeShellState.error || 'Terminal disconnected.';
  emptyEl.innerHTML = message ? `<div>${escHtml(message)}</div>` : '';
  emptyEl.classList.toggle('hidden', !message);
}

function disconnectHubSpokeShell({ rerender = true, preserveSelection = true } = {}) {
  const spokeId = hubSpokeShellState.spokeId;
  const socket = hubSpokeShellState.socket;
  const resizeObserver = hubSpokeShellState.resizeObserver;
  const terminal = hubSpokeShellState.terminal;
  hubSpokeShellState.connectToken += 1;
  hubSpokeShellState.socket = null;
  hubSpokeShellState.resizeObserver = null;
  hubSpokeShellState.terminal = null;
  hubSpokeShellState.fitAddon = null;
  hubSpokeShellState.error = '';
  hubSpokeShellState.status = preserveSelection && spokeId ? 'disconnected' : 'idle';
  if (!preserveSelection) hubSpokeShellState.spokeId = null;
  if (resizeObserver) {
    try { resizeObserver.disconnect(); } catch (_) {}
  }
  if (socket && socket.readyState <= WebSocket.OPEN) {
    try { socket.close(1000, 'Terminal closed'); } catch (_) {}
  }
  if (terminal) {
    try { terminal.dispose(); } catch (_) {}
  }
  if (rerender && activeTab === 'vm-server' && hubVmServerSelectedSpoke === spokeId && hubVmServerActiveSubtab === 'terminal') {
    const host = getHubVmServerHostBySpokeId(spokeId) || { spoke_id: spokeId };
    const panel = document.getElementById('hub-vmserver-subpanel');
    if (panel) {
      panel.innerHTML = renderHubVmServerTerminalPanel(spokeId, host);
      wireHubVmServerTerminalPanel(panel, spokeId, host);
    }
  }
}

function renderHubVmServerTerminalPanel(spokeId, host = {}) {
  return `
    <div class="setup-card setup-section-gap">
      <div class="setup-card-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
        <div>
          <h2 style="margin:0;">Terminal</h2>
          <p id="hub-spoke-shell-status" class="muted" style="margin:6px 0 0;">Opening terminal…</p>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn btn-primary btn-small" id="hub-spoke-shell-reconnect-btn" type="button">Reconnect</button>
          <button class="btn btn-secondary btn-small" id="hub-spoke-shell-disconnect-btn" type="button">Disconnect</button>
        </div>
      </div>
      <div style="position:relative;margin-top:12px;">
        <div id="hub-spoke-shell-screen" style="min-height:560px;background:#111;border:1px solid var(--border-color,#24303f);border-radius:10px;overflow:hidden;padding:8px;"></div>
        <div id="hub-spoke-shell-empty" class="empty-state hidden" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;background:rgba(9,14,20,0.78);border-radius:10px;"></div>
      </div>
      <div class="muted" style="margin-top:10px;font-size:0.82rem;">Interactive bash on the spoke host, relayed through the hub websocket.</div>
    </div>`;
}

async function openSpokeShell(tenantId, spokeId, panel = document.getElementById('hub-vmserver-subpanel')) {
  if (!tenantId || !spokeId || !panel) return;
  const screen = panel.querySelector('#hub-spoke-shell-screen');
  if (!screen) return;
  if (hubSpokeShellState.spokeId === spokeId && hubSpokeShellState.socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(hubSpokeShellState.socket.readyState)) {
    syncHubSpokeShellPanel(panel);
    return;
  }

  disconnectHubSpokeShell({ rerender: false, preserveSelection: false });
  hubSpokeShellState.spokeId = spokeId;
  hubSpokeShellState.status = 'connecting';
  hubSpokeShellState.error = '';
  const connectToken = ++hubSpokeShellState.connectToken;
  syncHubSpokeShellPanel(panel);

  try {
    const token = sessionStorage.getItem('hub_token') || localStorage.getItem('hub_token') || authToken;
    if (!token) throw new Error('Authentication required.');
    await ensureHubSpokeShellDeps();
    if (connectToken !== hubSpokeShellState.connectToken) return;

    screen.innerHTML = '';
    const term = new window.Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 13,
      scrollback: 5000,
      theme: { background: '#111111' },
    });
    const fitAddon = new window.FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(screen);
    fitAddon.fit();

    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${scheme}://${location.host}/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spokeId)}/shell?token=${encodeURIComponent(token)}`);
    hubSpokeShellState.socket = socket;
    hubSpokeShellState.terminal = term;
    hubSpokeShellState.fitAddon = fitAddon;

    const sendResize = () => {
      if (!hubSpokeShellState.socket || hubSpokeShellState.socket !== socket || socket.readyState !== WebSocket.OPEN) return;
      fitAddon.fit();
      socket.send(JSON.stringify({ type: 'shell_resize', cols: term.cols || 80, rows: term.rows || 24 }));
    };

    term.onData(data => {
      if (hubSpokeShellState.socket !== socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: 'shell_input', data }));
    });

    if (typeof ResizeObserver === 'function') {
      const resizeObserver = new ResizeObserver(() => sendResize());
      resizeObserver.observe(screen);
      hubSpokeShellState.resizeObserver = resizeObserver;
    }

    socket.addEventListener('message', event => {
      if (hubSpokeShellState.socket !== socket) return;
      let message = null;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message?.type === 'shell_started') {
        hubSpokeShellState.status = 'connected';
        hubSpokeShellState.error = '';
        syncHubSpokeShellPanel(panel);
        sendResize();
      } else if (message?.type === 'shell_data') {
        term.write(String(message.data || ''));
      } else if (message?.type === 'shell_exit') {
        hubSpokeShellState.status = 'disconnected';
        hubSpokeShellState.error = message.error || (message.exit_code != null ? `Shell exited (${message.exit_code}).` : 'Shell disconnected.');
        syncHubSpokeShellPanel(panel);
        if (socket.readyState <= WebSocket.OPEN) socket.close();
      }
    });

    socket.addEventListener('close', event => {
      if (hubSpokeShellState.socket !== socket) return;
      hubSpokeShellState.socket = null;
      if (hubSpokeShellState.status === 'connected') {
        hubSpokeShellState.status = 'disconnected';
        hubSpokeShellState.error = hubSpokeShellState.error || (event.reason || 'Terminal disconnected.');
      } else if (hubSpokeShellState.status === 'connecting') {
        hubSpokeShellState.status = 'error';
        hubSpokeShellState.error = event.reason || 'Unable to open the terminal.';
      }
      syncHubSpokeShellPanel(panel);
    });

    socket.addEventListener('error', () => {
      if (hubSpokeShellState.socket !== socket) return;
      hubSpokeShellState.error = hubSpokeShellState.error || 'Terminal connection failed.';
    });
  } catch (err) {
    if (connectToken !== hubSpokeShellState.connectToken) return;
    hubSpokeShellState.status = 'error';
    hubSpokeShellState.error = err?.message || 'Unable to open the terminal.';
    syncHubSpokeShellPanel(panel);
  }
}

function wireHubVmServerTerminalPanel(panel, spokeId, host = {}) {
  panel.querySelector('#hub-spoke-shell-disconnect-btn')?.addEventListener('click', () => disconnectHubSpokeShell({ rerender: true, preserveSelection: true }));
  panel.querySelector('#hub-spoke-shell-reconnect-btn')?.addEventListener('click', () => {
    disconnectHubSpokeShell({ rerender: false, preserveSelection: false });
    openSpokeShell(getActiveTenantId(), spokeId, panel);
  });
  syncHubSpokeShellPanel(panel);
  openSpokeShell(getActiveTenantId(), spokeId, panel).catch(err => {
    hubSpokeShellState.status = 'error';
    hubSpokeShellState.error = err?.message || 'Unable to open the terminal.';
    syncHubSpokeShellPanel(panel);
  });
}

function renderHubVmServerConsolePanel(spokeId, host = {}, node = {}, spokeCfg = {}) {
  const selected = hubVmConsoleState.spokeId === spokeId && hubVmConsoleState.vmid;
  const title = selected
    ? `${hubVmConsoleState.vmname || 'VM'} · ${hubVmConsoleState.vmid}`
    : 'Select a VM from the VMs tab to launch a console.';
  return `
    <div class="setup-card setup-section-gap">
      <div class="setup-card-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
        <div>
          <h2 style="margin:0;">Console</h2>
          <p id="hub-vm-console-status" class="muted" style="margin:6px 0 0;">${escHtml(title)}</p>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn btn-secondary btn-small" id="hub-vm-console-configure-btn" type="button">Configure Credentials</button>
          <button class="btn btn-primary btn-small" id="hub-vm-console-reconnect-btn" type="button">Reconnect</button>
          <button class="btn btn-secondary btn-small" id="hub-vm-console-disconnect-btn" type="button">Disconnect</button>
        </div>
      </div>
      <div style="position:relative;margin-top:12px;">
        <div id="hub-vm-console-screen" style="min-height:560px;background:#111;border:1px solid var(--border-color,#24303f);border-radius:10px;overflow:hidden;"></div>
        <div id="hub-vm-console-empty" class="empty-state" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;background:rgba(9,14,20,0.78);border-radius:10px;"></div>
      </div>
      <div class="muted" style="margin-top:10px;font-size:0.82rem;">If the relay drops, use Reconnect to request a fresh Proxmox VNC ticket.</div>
    </div>`;
}

async function connectHubVmConsole(panel, spokeId) {
  if (!panel || hubVmConsoleState.spokeId !== spokeId || !hubVmConsoleState.sessionId) {
    syncHubVmConsolePanel(panel);
    return;
  }
  const screen = panel.querySelector('#hub-vm-console-screen');
  if (!screen) return;
  screen.innerHTML = '';
  hubVmConsoleState.status = 'connecting';
  hubVmConsoleState.error = '';
  const connectToken = ++hubVmConsoleState.connectToken;
  syncHubVmConsolePanel(panel);
  try {
    const RFB = await getNoVNCRfbClass();
    if (connectToken !== hubVmConsoleState.connectToken) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${scheme}://${location.host}/ws/console/${encodeURIComponent(hubVmConsoleState.sessionId)}`;
    const rfb = new RFB(screen, url);
    hubVmConsoleState.rfb = rfb;
    hubVmConsoleState.sessionId = '';
    rfb.scaleViewport = true;
    rfb.clipViewport = true;
    rfb.focusOnClick = true;
    rfb.background = '#111';
    rfb.addEventListener('connect', () => {
      if (hubVmConsoleState.rfb !== rfb) return;
      hubVmConsoleState.status = 'connected';
      hubVmConsoleState.error = '';
      hubVmConsoleState.missingCredentials = false;
      syncHubVmConsolePanel(panel);
    });
    rfb.addEventListener('disconnect', event => {
      if (hubVmConsoleState.rfb !== rfb) return;
      hubVmConsoleState.rfb = null;
      hubVmConsoleState.sessionId = '';
      hubVmConsoleState.status = 'disconnected';
      hubVmConsoleState.error = event?.detail?.clean ? '' : (event?.detail?.reason || 'Console disconnected.');
      syncHubVmConsolePanel(panel);
    });
  } catch (err) {
    if (connectToken !== hubVmConsoleState.connectToken) return;
    hubVmConsoleState.rfb = null;
    hubVmConsoleState.sessionId = '';
    hubVmConsoleState.status = 'error';
    hubVmConsoleState.error = err?.message || 'Unable to start the console.';
    syncHubVmConsolePanel(panel);
  }
}

function showHubConsolePanel(spokeId, vmid, vmtype, vmname, sessionId = '', error = '') {
  hubVmConsoleState.spokeId = spokeId;
  hubVmConsoleState.vmid = vmid;
  hubVmConsoleState.vmtype = vmtype || 'qemu';
  hubVmConsoleState.vmname = vmname || `VM ${vmid}`;
  hubVmConsoleState.sessionId = sessionId || '';
  hubVmConsoleState.status = sessionId ? 'connecting' : (error ? 'error' : 'idle');
  hubVmConsoleState.error = error || '';
  hubVmConsoleState.missingCredentials = /configure proxmox credentials/i.test(error || '');
  hubVmServerActiveSubtab = 'console';
  renderHubVmServer();
}

async function openHubVmConsole(spokeId, vmid, vmtype = 'qemu', vmname = '') {
  disconnectHubVmConsole({ rerender: false, preserveSelection: false });
  const tenantId = getActiveTenantId();
  const safeVmtype = vmtype || 'qemu';
  try {
    const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spokeId)}/console/${encodeURIComponent(vmid)}?vmtype=${encodeURIComponent(safeVmtype)}`, { method: 'POST' });
    const data = await readJson(res);
    if (!res || !res.ok || !data?.session_id) throw new Error(data?.detail || 'Console session failed.');
    showHubConsolePanel(spokeId, vmid, safeVmtype, vmname, data.session_id);
  } catch (err) {
    const message = err?.message || 'Console session failed.';
    showHubConsolePanel(spokeId, vmid, safeVmtype, vmname, '', message);
    showToast(message, 'error');
  }
}

async function loadHubProxmoxCredentialSummary(panel, spokeId, fallbackHost = '') {
  if (!panel || !spokeId) return;
  const hostEl = panel.querySelector('#hub-proxmox-credentials-host');
  const statusEl = panel.querySelector('#hub-proxmox-credentials-status');
  try {
    const res = await apiFetch(`/api/${encodeURIComponent(getActiveTenantId())}/spokes/${encodeURIComponent(spokeId)}/proxmox-credentials`);
    const data = await readJson(res);
    if (!res || !res.ok) throw new Error(data?.detail || 'Unable to load Proxmox credentials.');
    if (hostEl) hostEl.textContent = data?.proxmox_host || fallbackHost || '—';
    if (statusEl) {
      statusEl.textContent = data?.proxmox_token_configured ? '✓ Token configured' : 'Token not configured';
      statusEl.classList.toggle('ok', Boolean(data?.proxmox_token_configured));
    }
  } catch (err) {
    if (hostEl) hostEl.textContent = fallbackHost || '—';
    if (statusEl) statusEl.textContent = err?.message || 'Unable to load credentials';
  }
}

async function openHubProxmoxCredentialsModal(spokeId, fallbackHost = '') {
  const modal = $('#hub-proxmox-credentials-modal');
  const hostInput = $('#hub-proxmox-host-input');
  const tokenInput = $('#hub-proxmox-token-input');
  const statusEl = $('#hub-proxmox-token-status');
  const msgEl = $('#hub-proxmox-credentials-msg');
  if (!modal || !hostInput || !tokenInput) return;
  hubProxmoxCredentialsModalState = { spokeId, fallbackHost };
  modal.classList.remove('hidden');
  hostInput.value = fallbackHost || '';
  setSecretInputConfigured(tokenInput, false);
  if (statusEl) statusEl.textContent = 'Loading…';
  if (msgEl) { msgEl.textContent = ''; msgEl.className = 'form-msg'; }
  try {
    const res = await apiFetch(`/api/${encodeURIComponent(getActiveTenantId())}/spokes/${encodeURIComponent(spokeId)}/proxmox-credentials`);
    const data = await readJson(res);
    if (!res || !res.ok) throw new Error(data?.detail || 'Unable to load Proxmox credentials.');
    hostInput.value = data?.proxmox_host || fallbackHost || '';
    setSecretInputConfigured(tokenInput, Boolean(data?.proxmox_token_configured));
    if (statusEl) statusEl.textContent = data?.proxmox_token_configured ? '✓ Token configured' : 'Token not configured';
  } catch (err) {
    if (statusEl) statusEl.textContent = err?.message || 'Unable to load Proxmox credentials.';
  }
}

function closeHubProxmoxCredentialsModal() {
  $('#hub-proxmox-credentials-modal')?.classList.add('hidden');
  hubProxmoxCredentialsModalState = { spokeId: null, fallbackHost: '' };
}

async function saveHubProxmoxCredentials() {
  const { spokeId, fallbackHost } = hubProxmoxCredentialsModalState;
  const hostInput = $('#hub-proxmox-host-input');
  const tokenInput = $('#hub-proxmox-token-input');
  const statusEl = $('#hub-proxmox-token-status');
  const msgEl = $('#hub-proxmox-credentials-msg');
  const saveBtn = $('#hub-proxmox-credentials-save');
  if (!spokeId || !hostInput || !tokenInput || !saveBtn) return;
  const payload = { proxmox_host: hostInput.value.trim() };
  const tokenPayload = getSecretInputPayload(tokenInput);
  if (tokenPayload.include) payload.proxmox_token = tokenPayload.value.trim();
  saveBtn.disabled = true;
  try {
    const res = await apiFetch(`/api/${encodeURIComponent(getActiveTenantId())}/spokes/${encodeURIComponent(spokeId)}/proxmox-credentials`, {
      method: 'PUT',
      body: payload,
    });
    const data = await readJson(res);
    if (!res || !res.ok) throw new Error(data?.detail || 'Unable to save Proxmox credentials.');
    setSecretInputConfigured(tokenInput, Boolean(data?.proxmox_token_configured));
    if (statusEl) statusEl.textContent = data?.proxmox_token_configured ? '✓ Token configured' : 'Token not configured';
    if (msgEl) { msgEl.textContent = 'Credentials saved.'; msgEl.className = 'form-msg ok'; }
    const panel = document.getElementById('hub-vmserver-subpanel');
    if (panel && hubVmServerActiveSubtab === 'details' && hubVmServerSelectedSpoke === spokeId) {
      await loadHubProxmoxCredentialSummary(panel, spokeId, data?.proxmox_host || fallbackHost || '');
    }
    showToast('Proxmox credentials saved.', 'ok');
  } catch (err) {
    if (msgEl) { msgEl.textContent = err?.message || 'Unable to save Proxmox credentials.'; msgEl.className = 'form-msg error'; }
  } finally {
    saveBtn.disabled = false;
  }
}

function wireHubVmServerConsolePanel(panel, spokeId, host = {}, node = {}, spokeCfg = {}) {
  const fallbackHost = getHubVmConsoleFallbackHost(host, node);
  panel.querySelector('#hub-vm-console-configure-btn')?.addEventListener('click', () => openHubProxmoxCredentialsModal(spokeId, fallbackHost));
  panel.querySelector('#hub-vm-console-disconnect-btn')?.addEventListener('click', () => disconnectHubVmConsole({ rerender: true, preserveSelection: true }));
  panel.querySelector('#hub-vm-console-reconnect-btn')?.addEventListener('click', () => {
    if (!hubVmConsoleState.spokeId || !hubVmConsoleState.vmid) return;
    openHubVmConsole(hubVmConsoleState.spokeId, hubVmConsoleState.vmid, hubVmConsoleState.vmtype, hubVmConsoleState.vmname);
  });
  syncHubVmConsolePanel(panel);
  if (hubVmConsoleState.spokeId === spokeId && hubVmConsoleState.sessionId) connectHubVmConsole(panel, spokeId);
}

function _hubVmStatusDot(vm = {}) {
  if (vm.prov_status === "provisioning" || vm.pending_checkin === true) return "🔵";
  if (vm.status === "running") return "🟢";
  if (vm.status === "paused") return "🟡";
  return "⚫";
}

function _hubVmSort(a, b) {
  const aRunning = a.status === "running" ? 1 : 0;
  const bRunning = b.status === "running" ? 1 : 0;
  if (aRunning !== bRunning) return aRunning - bRunning;
  return Number(a.vmid || 0) - Number(b.vmid || 0);
}

function _hubVmCpuText(vm = {}) {
  return (vm.status === "running") && vm.cpu != null && !Number.isNaN(Number(vm.cpu))
    ? `${Number(vm.cpu).toFixed(1)}%`
    : "—";
}

function _hubVmRamText(vm = {}) {
  const memUsed = vm.mem ? fmtSize(Number(vm.mem) * 1024 * 1024) : "—";
  const memTotal = vm.maxmem ? fmtSize(Number(vm.maxmem) * 1024 * 1024) : "—";
  return `${memUsed} / ${memTotal}`;
}

function _hubRelativeTime(value) {
  if (!value) return "—";
  if (typeof value === "number") return formatRelativeTime(value);
  if (/^\d+(?:\.\d+)?$/.test(String(value))) return formatRelativeTime(Number(value));
  return relativeTime(value);
}

function _hubFormatFaults(faults = []) {
  if (!faults.length) return '<span class="muted">None</span>';
  return faults.map(fault => {
    const label = fault.type || fault.check || "fault";
    const detail = fault.message || fault.detail || JSON.stringify(fault);
    return `<div>⚠️ ${escHtml(label)} — ${escHtml(detail)}</div>`;
  }).join("");
}

function renderHubVmServerVmsPanel(spokeId, { simVms, otherVms, containerVms, templateVms, usbState = [], reclone = {}, px = {} }) {
  const VM_ACTIONS = [
    { action: "console", label: "🖥", title: "Console" },
    { action: "start_vm", label: "▶", title: "Start" },
    { action: "stop_vm", label: "■", title: "Stop" },
    { action: "reboot_vm", label: "↺", title: "Reboot" },
    { action: "snapshot_vm", label: "📷", title: "Snapshot" },
    { action: "reclone_vm", label: "⎘", title: "Reclone" },
    { action: "delete_vm", label: "✕", title: "Delete" },
  ];

  const recloneStatus = String(reclone.status || "idle").toLowerCase();
  const recloneRunning = recloneStatus === "running";
  const recloneTotal = Number(reclone.total || 0);
  const recloneCompleted = Number(reclone.completed || 0);
  const reclonePct = recloneTotal > 0 ? Math.max(0, Math.min(100, Math.round((recloneCompleted / recloneTotal) * 100))) : 0;
  const recloneLog = Array.isArray(reclone.log) ? reclone.log.slice(-12).reverse() : [];

  const provisioningUsb = usbState.filter(item => item && item.prov_status === "provisioning");
  const provisioningVmids = new Set(provisioningUsb.map(item => String(item?.vmid || "")).filter(Boolean));
  const pendingCheckin = simVms.filter(vm => vm && vm.pending_checkin === true && !provisioningVmids.has(String(vm.vmid)));
  const activeProvisioning = provisioningUsb.length + pendingCheckin.length;
  const provisionable = usbState.filter(item => item && item.prov_status);
  const completedProvisioning = provisionable.filter(item => item.prov_status !== "provisioning" && item.vmid != null).length;
  const provisioningTotal = Math.max(provisionable.length, activeProvisioning);
  const provisioningPct = provisioningTotal > 0 ? Math.max(0, Math.min(100, Math.round((completedProvisioning / provisioningTotal) * 100))) : 0;
  const slotItems = [
    ...provisioningUsb.map(item => ({
      name: item.name || item.hostname || `VM ${item.vmid || item.bus_path || "—"}`,
      phase: "Cloning",
      cls: "autoprov-phase-cloning",
    })),
    ...pendingCheckin.map(vm => ({
      name: vm.name || `VM ${vm.vmid}`,
      phase: "Waiting for check-in",
      cls: "autoprov-phase-configuring",
    })),
  ];

  const renderRecloneItem = (entry) => {
    if (typeof entry === "string") {
      return `<div class="autoprov-live-item reclone-live-item"><div class="autoprov-live-item-main"><div class="autoprov-live-item-name">${escHtml(entry)}</div></div></div>`;
    }
    const status = String(entry?.status || "queued").replace(/_/g, " ");
    const label = entry?.name || (entry?.vmid != null ? `VM ${entry.vmid}` : "Task");
    return `
      <div class="autoprov-live-item reclone-live-item">
        <div class="autoprov-live-item-main">
          <div class="autoprov-live-item-name">${escHtml(label)}</div>
        </div>
        <span class="autoprov-phase autoprov-phase-${escHtml(String(entry?.status || "queued"))}">${escHtml(status)}</span>
      </div>`;
  };

  const renderVmRows = (vmList, isTemplate = false) => {
    const sorted = [...vmList].sort(_hubVmSort);
    if (!sorted.length) return "";
    return sorted.map(vm => {
      const isProvisioning = vm.prov_status === "provisioning" || vm.pending_checkin === true;
      const statusText = isProvisioning ? "provisioning" : (vm.status || "unknown");
      const recloneSupported = vm.reclone_supported !== false;
      const recloneReason = vm.reclone_reason || "This guest cannot be recloned from the current configuration";
      const vmType = vm.type || "qemu";
      const sourceVmid = vm.reclone_source_vmid != null ? String(vm.reclone_source_vmid) : "";
      if (isTemplate) {
        return `<tr>
          <td>${escHtml(String(vm.vmid ?? "—"))}</td>
          <td>${escHtml(vm.name || "—")}</td>
          <td>${_hubVmCpuText(vm)}</td>
          <td>${_hubVmRamText(vm)}</td>
          <td>${_hubVmStatusDot(vm)} ${escHtml(statusText)}</td>
        </tr>`;
      }
      return `<tr data-vm-type="${escHtml(vmType)}" data-reclone-source-vmid="${escHtml(sourceVmid)}" data-vm-name="${escHtml(vm.name || '')}">
        <td><input type="checkbox" class="hub-vm-check" data-vmid="${escHtml(String(vm.vmid))}"${vm.status === "deleting" ? " disabled" : ""}></td>
        <td>${_hubVmStatusDot(vm)} ${escHtml(statusText)}</td>
        <td>${escHtml(String(vm.vmid ?? "—"))}</td>
        <td>${escHtml(vm.name || "—")}</td>
        <td>${_hubVmCpuText(vm)}</td>
        <td>${_hubVmRamText(vm)}</td>
        <td style="white-space:nowrap;">
          ${VM_ACTIONS.map(action => {
            const disabled = vm.status === "deleting" || (action.action === "reclone_vm" && !recloneSupported);
            const title = action.action === "reclone_vm" && !recloneSupported ? recloneReason : action.title;
            return `<button class="btn-icon hub-vm-action-btn" data-action="${action.action}" data-vmid="${escHtml(String(vm.vmid))}" title="${escHtml(title)}"${disabled ? " disabled" : ""}>${action.label}</button>`;
          }).join(" ")}
        </td>
      </tr>`;
    }).join("");
  };

  const vmPanel = (cat, vms, emptyText) => {
    const isTemplate = cat === "templates";
    const header = isTemplate
      ? `<tr><th>VM ID</th><th>Name</th><th>CPU %</th><th>RAM</th><th>Status</th></tr>`
      : `<tr><th style="width:36px;"><input type="checkbox" id="hub-server-th-check-${cat}" ${vms.length ? "" : "disabled"}></th><th>Status</th><th>${cat === "containers" ? "CT ID" : "VM ID"}</th><th>Name</th><th>CPU %</th><th>RAM</th><th>Actions</th></tr>`;
    const rows = renderVmRows(vms, isTemplate);
    return `
      <div id="hub-vm-cat-panel-${cat}" class="setup-card${cat !== "sim" ? " hidden" : ""}">
        <table class="data-table">
          <thead>${header}</thead>
          <tbody id="hub-vm-tbody-${cat}">${rows}</tbody>
        </table>
        <div id="hub-server-empty-${cat}" style="text-align:center;color:var(--muted);padding:32px;${rows ? "display:none;" : ""}">${escHtml(emptyText)}</div>
      </div>`;
  };

  return `
    <div>
      <div class="reclone-autoprov-grid setup-section-gap">
        <div id="hub-reclone-status-panel" class="setup-card vm-server-status-tile">
          <div class="autoprov-two-col">
            <div class="autoprov-left">
              <div class="setup-card-header" style="margin-bottom:8px;">
                <h2 style="font-size:1rem;margin:0;">Fleet Reclone</h2>
              </div>
              <div class="reclone-action-stack">
                <button id="hub-reclone-all-btn" class="btn btn-primary btn-small" type="button">⟳ Reclone All Now</button>
                <div class="badge ${recloneRunning ? "badge-blue" : "badge-grey"}">${recloneRunning ? "Running" : escHtml(reclone.status || "Idle")}</div>
              </div>
              <div${recloneRunning ? "" : ' class="hidden"'}>
                <div class="reclone-meta">
                  <span class="muted">${escHtml(String(reclone.completed || 0))} / ${escHtml(String(reclone.total || 0))}</span>
                  ${reclone.current_vm != null ? `<span class="muted">Current VM ${escHtml(String(reclone.current_vm))}</span>` : ""}
                </div>
                <div class="progress-bar-wrap"><div class="progress-bar" style="width:${reclonePct}%"></div></div>
                <div>${reclonePct}% complete</div>
              </div>
              <div class="reclone-last-run">Last run: ${escHtml(_hubRelativeTime(reclone.last_run))}</div>
            </div>
            <div class="autoprov-right">
              <div class="autoprov-live-list">
                ${recloneLog.length ? recloneLog.map(renderRecloneItem).join("") : '<div class="muted" style="padding:8px 0;">No reclone activity recorded.</div>'}
              </div>
            </div>
          </div>
        </div>

        <div id="hub-autoprov-live-panel" class="setup-card vm-server-status-tile">
          <div class="autoprov-two-col">
            <div class="autoprov-left">
              <div class="setup-card-header" style="margin-bottom:8px;">
                <h2 style="font-size:1rem;margin:0;">VM Auto-Provisioning</h2>
              </div>
              ${activeProvisioning ? `
                <div class="autoprov-live-summary">
                  <div class="autoprov-live-summary-main">
                    <strong>${escHtml(String(provisioningUsb.length))} cloning · ${escHtml(String(pendingCheckin.length))} waiting</strong>
                    <span class="muted">${provisioningPct}%</span>
                  </div>
                  <div class="progress-bar-wrap autoprov-progress-wrap"><div class="progress-bar" style="width:${provisioningPct}%"></div></div>
                </div>` : `
                <div class="autoprov-live-summary">
                  <div class="muted" style="padding:6px 0;">No provisioning in progress. Dongles inserted will trigger auto-provisioning.</div>
                </div>`}
            </div>
            <div class="autoprov-right">
              <div class="autoprov-live-list">
                ${slotItems.length ? slotItems.map(item => `
                  <div class="autoprov-live-item">
                    <span class="autoprov-live-item-name">${escHtml(item.name)}</span>
                    <span class="autoprov-phase ${item.cls}">${escHtml(item.phase)}</span>
                  </div>`).join("") : '<div class="muted" style="padding:8px 0;">No active provisioning items.</div>'}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="vm-cat-tab-nav setup-section-gap">
        <button class="vm-cat-tab hub-vm-cat-tab setup-subtab active" data-cat="sim" type="button">Simulation Clients <span class="badge-count">${simVms.length}</span></button>
        <button class="vm-cat-tab hub-vm-cat-tab setup-subtab" data-cat="other" type="button">Other Clients <span class="badge-count">${otherVms.length}</span></button>
        <button class="vm-cat-tab hub-vm-cat-tab setup-subtab" data-cat="containers" type="button">Containers <span class="badge-count">${containerVms.length}</span></button>
        <button class="vm-cat-tab hub-vm-cat-tab setup-subtab" data-cat="templates" type="button">Templates <span class="badge-count">${templateVms.length}</span></button>
        ${activeProvisioning ? `
          <div class="autoprov-status-bar is-active" style="margin-left:auto;">
            <span class="autoprov-status-icon"><span class="autoprov-spinner"></span></span>
            <span>Provisioning… ${escHtml(String(activeProvisioning))} active</span>
          </div>` : ""}
      </div>

      <div class="server-bulk-bar" id="hub-server-bulk-bar">
        <label><input type="checkbox" id="hub-vm-select-all"> Select All</label>
        <button class="btn btn-primary hub-vm-bulk-action" data-action="start_vm" type="button">▶ Start</button>
        <button class="btn btn-primary hub-vm-bulk-action" data-action="stop_vm" type="button">■ Stop</button>
        <button class="btn btn-warning hub-vm-bulk-action" data-action="reclone_vm" type="button">↺ Reclone</button>
        <button class="btn btn-danger hub-vm-bulk-action" data-action="delete_vm" type="button">✕ Delete</button>
      </div>

      ${vmPanel("sim", simVms, "Waiting for Proxmox agent to check in...")}
      ${vmPanel("other", otherVms, "No other VMs found.")}
      ${vmPanel("containers", containerVms, "No containers found.")}
      ${vmPanel("templates", templateVms, "No templates detected.")}
    </div>`;
}

function renderHubVmServerUsbPanel({ usbState = [], presentUsb = [], unknownUsb = [], certifiedUsb = [], vms = [] }) {
  const certified = Array.isArray(certifiedUsb) ? certifiedUsb : [];
  const vmMap = new Map(vms.map(vm => [Number(vm.vmid), vm]));
  const presentBusSet = new Set(presentUsb.map(item => String(item?.bus_path || "").trim()).filter(Boolean));
  const certifiedSet = new Set(certified.map(device => String(device?.vidpid || "").toLowerCase()).filter(Boolean));
  const missingVmids = new Set(
    usbState
      .filter(item => item?.missing_since && !presentBusSet.has(String(item?.bus_path || "").trim()))
      .map(item => Number(item.vmid))
      .filter(Boolean)
  );
  const runningVms = vms.filter(vm => vm.status === "running" && !vm.is_template && !missingVmids.has(Number(vm.vmid)));
  const simRunning = runningVms.filter(vm => String(vm.name || "").startsWith("client-sim-")).length;
  const filteredUnknownUsb = unknownUsb.filter(device => !certifiedSet.has(String(device?.vidpid || "").toLowerCase().trim()));

  const summaryRows = certified.map(device => {
    const vidpid = String(device?.vidpid || "").toLowerCase();
    const entries = usbState.filter(item => String(item?.vidpid || "").toLowerCase() === vidpid);
    const missingEntries = entries.filter(item => item?.missing_since && !presentBusSet.has(String(item?.bus_path || "").trim()));
    const activeEntries = entries.filter(item => !missingEntries.includes(item));
    const total = presentUsb.filter(item => String(item?.vidpid || "").toLowerCase() === vidpid).length;
    const available = Math.max(0, total - activeEntries.length);
    const hwName = entries.find(item => item?.name)?.name || presentUsb.find(item => String(item?.vidpid || "").toLowerCase() === vidpid)?.name || "";
    const activeVmHtml = activeEntries.length
      ? activeEntries.map(entry => {
          const vm = vmMap.get(Number(entry.vmid));
          const name = vm?.name || `VM ${entry.vmid}`;
          const dot = vm?.status === "running" ? "🟢" : "⚫";
          return `<div style="white-space:nowrap">${dot} ${escHtml(name)}</div>`;
        }).join("")
      : "—";
    const missingHtml = missingEntries.length
      ? `<div class="usb-missing-list">${missingEntries.map(entry => {
          const vm = vmMap.get(Number(entry.vmid));
          const name = vm?.name || `VM ${entry.vmid}`;
          return `<div class="usb-missing-item">🔴 ${escHtml(name)}</div>`;
        }).join("")}</div>`
      : "—";
    const vidpidHtml = hwName
      ? `${escHtml(device?.vidpid || "—")}<div class="muted" style="font-size:0.78rem;margin-top:2px;">${escHtml(hwName)}</div>`
      : escHtml(device?.vidpid || "—");
    const usbType = escHtml(device?.type || "wireless");
    return `<tr>
      <td>${escHtml(device?.label || device?.vidpid || "—")}</td>
      <td>${vidpidHtml}</td>
      <td class="usb-type-${usbType}">${usbType}</td>
      <td>${activeVmHtml}</td>
      <td>${missingHtml}</td>
      <td>${available > 0 ? `<span class="badge badge-green">${available}</span>` : '<span class="muted">—</span>'}</td>
      <td>${total}</td>
    </tr>`;
  }).join("");

  const unknownRows = filteredUnknownUsb.map(device => {
    const vidpid = escHtml(device?.vidpid || "");
    const name = escHtml(device?.name || device?.bus_path || "Unknown device");
    return `<tr>
      <td>${name}</td>
      <td>${vidpid || "—"}</td>
      <td class="usb-actions">
        <button type="button" class="btn btn-primary btn-small" data-action="certify" data-vidpid="${vidpid}" data-name="${name}">Add to certified</button>
        <button type="button" class="btn btn-primary btn-small" data-action="ignore" data-vidpid="${vidpid}">Ignore</button>
      </td>
    </tr>`;
  }).join("");

  return `
    <div id="hub-usb-summary-panel" class="setup-card setup-section-gap">
      <div class="setup-card-header">
        <h2>USB Devices</h2>
        <div id="hub-usb-vm-stat-pills" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <span class="server-stat-pill" title="Running VMs with dongles present">🟢 ${runningVms.length} running VM${runningVms.length !== 1 ? "s" : ""}</span>
          ${simRunning > 0 ? `<span class="server-stat-pill" title="client-sim-* VMs running">${simRunning} sim client${simRunning !== 1 ? "s" : ""}</span>` : ""}
        </div>
      </div>
      <table class="data-table" id="hub-usb-summary-table">
        <colgroup>
          <col>
          <col style="width:105px">
          <col style="width:90px">
          <col style="width:200px">
          <col style="width:200px">
          <col style="width:100px">
          <col style="width:75px">
        </colgroup>
        <thead>
          <tr><th>Device</th><th>VID:PID</th><th>Type</th><th title="VMs with this USB device actively assigned via USB provisioning">Active VMs</th><th>Missing Dongle</th><th>Available</th><th>Count</th></tr>
        </thead>
        <tbody id="hub-usb-summary-tbody">${summaryRows || '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:32px;">No certified USB devices configured.</td></tr>'}</tbody>
      </table>
      <div id="hub-unknown-usb-section"${unknownRows ? '' : ' class="hidden"'} style="margin-top:16px;">
        <div class="setup-card-header" style="padding:0 0 8px;">
          <h3>⚠️ Uncertified Devices Detected</h3>
          <p>These USB devices are connected but not in the certified list. Add or ignore them below.</p>
        </div>
        <table class="data-table" id="hub-unknown-usb-table">
          <colgroup>
            <col>
            <col style="width:105px">
            <col style="width:300px">
          </colgroup>
          <thead><tr><th>Device</th><th>VID:PID</th><th>Actions</th></tr></thead>
          <tbody id="hub-unknown-usb-tbody">${unknownRows}</tbody>
        </table>
      </div>
    </div>`;
}

function renderHubVmServerT3Panel(otherVms = [], usb = []) {
  const usbVmIds = new Set(usb.map(item => Number(item?.vmid)).filter(Boolean));
  const rows = otherVms.filter(vm => !vm.is_template).sort((a, b) => Number(a.vmid || 0) - Number(b.vmid || 0)).map(vm => `
    <tr>
      <td>${escHtml(String(vm.vmid ?? "—"))}</td>
      <td>${escHtml(vm.name || "—")}</td>
      <td>${escHtml(vm.type || "qemu")}</td>
      <td>${_hubVmStatusDot(vm)} ${escHtml(vm.status || "unknown")}</td>
      <td>${usbVmIds.has(Number(vm.vmid)) ? "🔌 USB" : "—"}</td>
    </tr>`).join("");

  return `
    <div class="setup-card setup-section-gap">
      <div class="setup-card-header">
        <h2>IoT (T3) Clients</h2>
        <p>IoT client classification coming soon.</p>
      </div>
      <table class="data-table">
        <thead><tr><th>VMID</th><th>Name</th><th>Type</th><th>Status</th><th>USB</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:32px;">No IoT clients found.</td></tr>'}</tbody>
      </table>
    </div>`;
}

function renderHubVmServerVirtualHerePanel(vhDevices = {}, presentUsb = [], px = {}) {
  const devices = Array.isArray(vhDevices.devices) ? vhDevices.devices : [];
  const svcActive = vhDevices.vh_service_active;
  const connected = vhDevices.vh_connected;
  const autoUseAll = vhDevices.auto_use_all;
  const count = vhDevices.count ?? devices.length;
  const inUse = devices.filter(d => d.auto_use).length;
  const available = devices.filter(d => !d.auto_use).length;

  const pills = [
    `<span class="server-stat-pill">${svcActive ? "🟢 Service running" : "🔴 Service stopped"}</span>`,
    `<span class="server-stat-pill">${autoUseAll ? "⚡ Auto-Use All: ON" : "⚫ Auto-Use All: OFF"}</span>`,
    `<span class="server-stat-pill">${connected ? (count > 0 ? `🔌 ${count} device${count !== 1 ? "s" : ""} — ${inUse} in use, ${available} available` : "⚫ No VH devices detected") : "⚫ Not connected to VH server"}</span>`
  ].join("");

  const groups = new Map();
  devices.forEach(device => {
    const server = device.server || "Unknown Server";
    if (!groups.has(server)) groups.set(server, []);
    groups.get(server).push(device);
  });

  const listHtml = devices.length
    ? [...groups.entries()].map(([server, devs]) => `
        <div style="margin-bottom:8px;">
          <div style="font-size:0.8rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:6px;">Server: ${escHtml(server)}</div>
          <table class="data-table">
            <thead><tr><th>Adapter</th><th>Address</th><th>Vendor</th><th>VID:PID</th><th>Serial</th><th>Status</th></tr></thead>
            <tbody>${devs.map(device => `
              <tr>
                <td><strong>${escHtml(device.name || "Unknown")}</strong></td>
                <td><code>${escHtml(device.address || "—")}</code></td>
                <td>${escHtml(device.vendor || "—")}</td>
                <td>${device.vendor_id && device.product_id ? `<code>${escHtml(device.vendor_id)}:${escHtml(device.product_id)}</code>` : "—"}</td>
                <td><code>${escHtml(device.serial || "—")}</code></td>
                <td>${device.auto_use ? `<span class="badge badge-green">In Use${device.in_use_by ? ` by ${escHtml(device.in_use_by)}` : ""}</span>` : '<span class="badge badge-grey">Available</span>'}</td>
              </tr>`).join("")}</tbody>
          </table>
        </div>`).join("")
    : `<p class="muted" style="padding:8px 0;">${px.connected === false ? "Proxmox telemetry is currently disconnected." : "No VirtualHere adapters found. Ensure the VH client service is running and connected to a server."}</p>`;

  return `
    <div class="setup-card setup-section-gap">
      <div class="setup-card-header" style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
        <div>
          <h2>🔌 VirtualHere Devices</h2>
          <div id="hub-vh-stat-pills" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;">${pills}</div>
        </div>
      </div>
      <div id="hub-vh-device-list" style="margin-top:12px;">${listHtml}</div>
    </div>`;
}

function renderHubVmServerCommandQueuePanel() {
  return `
    <div class="setup-card setup-section-gap" id="hub-node-cmd-wrapper">
      <div class="setup-card-header" style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
        <h2 style="font-size:1rem;margin:0;">Command Queue</h2>
        <button class="btn btn-secondary btn-small" id="hub-node-cmd-refresh" type="button">↻ Refresh</button>
      </div>
      <div id="hub-node-cmd-list"><div class="empty-state" style="padding:16px;">Loading…</div></div>
      <hr style="margin:16px 0;border-color:var(--border-color,#e2e8f0);">
      <div class="setup-card-header" style="margin-bottom:8px;"><h3 style="font-size:0.9rem;margin:0;">Send Proxmox Command</h3></div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;">
        <div class="form-group" style="min-width:160px;flex:1;">
          <label class="form-label">Action</label>
          <input id="hub-node-cmd-action" class="form-input" placeholder="e.g. start_vm" type="text">
        </div>
        <div class="form-group" style="min-width:80px;">
          <label class="form-label">VMID (optional)</label>
          <input id="hub-node-cmd-vmid" class="form-input" placeholder="e.g. 90001" type="number">
        </div>
        <div style="display:flex;align-items:flex-end;gap:8px;padding-bottom:1px;">
          <button id="hub-node-cmd-send" class="btn btn-primary" type="button">Send</button>
          <span id="hub-node-cmd-msg" class="form-msg" style="font-size:0.85rem;"></span>
        </div>
      </div>
    </div>`;
}

async function loadHubNodeCommandQueue(panel, spokeId) {
  const token = sessionStorage.getItem("hub_token");
  const tenantId = getActiveTenantId();
  const list = panel.querySelector("#hub-node-cmd-list");
  if (!list) return;
  try {
    const resp = await fetch(`/api/${encodeURIComponent(tenantId)}/commands?spoke_id=${encodeURIComponent(spokeId)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const cmds = await resp.json();
    if (!cmds.length) {
      list.innerHTML = '<div class="empty-state" style="padding:12px;">No commands in queue.</div>';
      return;
    }
    list.innerHTML = `
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>ID</th><th>Action</th><th>Target</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>
            ${cmds.map(c => {
              const statusColor = c.status === "done" ? "success" : c.status === "error" ? "danger" : "info";
              return `<tr>
                <td><code style="font-size:0.75rem;">${escHtml(String(c.id || "—"))}</code></td>
                <td>${escHtml(c.action || c.cmd_type || "—")}</td>
                <td>${escHtml(c.target || (c.args?.vmid != null ? `VM ${c.args.vmid}` : "node"))}</td>
                <td><span class="badge" style="background:var(--${statusColor}-color,#3182ce);color:#fff;font-size:0.75rem;border-radius:4px;padding:1px 6px;">${escHtml(c.status || "queued")}</span></td>
                <td class="muted" style="font-size:0.8rem;">${c.created_at ? new Date(c.created_at).toLocaleString() : "—"}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>`;
  } catch (err) {
    list.innerHTML = `<div class="empty-state" style="padding:12px;color:var(--danger-color,#e74c3c);">Failed to load: ${escHtml(err.message)}</div>`;
  }
}

function wireHubNodeCommandQueue(panel, spokeId) {
  const refreshBtn = panel.querySelector("#hub-node-cmd-refresh");
  const sendBtn = panel.querySelector("#hub-node-cmd-send");
  const msgEl = panel.querySelector("#hub-node-cmd-msg");
  if (refreshBtn) refreshBtn.addEventListener("click", () => loadHubNodeCommandQueue(panel, spokeId));
  if (!sendBtn) return;
  sendBtn.addEventListener("click", async () => {
    const action = (panel.querySelector("#hub-node-cmd-action")?.value || "").trim();
    const vmidVal = panel.querySelector("#hub-node-cmd-vmid")?.value || "";
    if (!action) {
      if (msgEl) { msgEl.textContent = "Action is required."; msgEl.className = "form-msg error"; }
      return;
    }
    const args = vmidVal ? { vmid: parseInt(vmidVal, 10) } : {};
    sendBtn.disabled = true;
    if (msgEl) { msgEl.textContent = "Sending…"; msgEl.className = "form-msg"; }
    try {
      await sendHubProxmoxCommand(getActiveTenantId(), spokeId, action, args);
      if (msgEl) { msgEl.textContent = "✓ Sent"; msgEl.className = "form-msg ok"; }
      setTimeout(() => loadHubNodeCommandQueue(panel, spokeId), 1200);
    } catch (err) {
      if (msgEl) { msgEl.textContent = `Error: ${err.message}`; msgEl.className = "form-msg error"; }
    } finally {
      sendBtn.disabled = false;
    }
  });
}

function renderHubVmServerDetailsPanel(node = {}, px = {}, host = {}, spokeCfg = {}) {
  const networkTypes = new Set(["nfs", "cifs", "glusterfs", "cephfs", "rbd", "iscsi", "pbs"]);
  const hostname = node.hostname || spokeCfg.hostname || host.spoke_name || "—";
  const cpu = node.cpu_percent != null && !Number.isNaN(Number(node.cpu_percent)) ? Number(node.cpu_percent).toFixed(1) : "—";
  const ramUsed = node.mem_used_kb ? fmtSizeKB(node.mem_used_kb) : "—";
  const ramTotal = node.mem_total_kb ? fmtSizeKB(node.mem_total_kb) : "—";
  const storagePills = Array.isArray(node.storage) ? node.storage.map(storage => {
    const icon = networkTypes.has(storage.type) ? "🌐" : "🗄️";
    const storageName = escHtml(storage.name || "—");
    const storageType = escHtml(storage.type || "dir");
    return `<span class="server-stat-pill" title="${storageName} (${storageType})">${icon} ${storageName}: ${fmtSizeKB(storage.used)} / ${fmtSizeKB(storage.total)}</span>`;
  }).join("") : "";
  const hwFaults = Array.isArray(px.hw_faults?.faults) ? px.hw_faults.faults : [];

  const proxmoxHost = getHubVmConsoleFallbackHost(host, node) || hostname;
  return `
    <div class="setup-card setup-section-gap">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
        <div style="display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;">
          <h2 id="hub-server-node-name" style="margin:0;">${escHtml(hostname)}</h2>
          <span class="server-stat-pill">⚡ CPU: ${escHtml(cpu)}%</span>
          <span class="server-stat-pill">🧠 RAM: ${escHtml(ramUsed)} / ${escHtml(ramTotal)}</span>
        </div>
        <div>
          <button class="btn btn-warning" id="hub-node-update-agent-btn">⬆️ Update Agent</button>
        </div>
      </div>
      <div id="hub-server-storage-pills" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">${storagePills}</div>
      <table class="data-table" style="margin-top:16px;">
        <tbody>
          <tr><th>Agent Version</th><td>${escHtml(px.agent_version || "—")}</td></tr>
          <tr><th>PVE Version</th><td>${escHtml(px.pve_version || "—")}</td></tr>
          <tr><th>Connected</th><td>${px.connected ? "Yes" : "No"}</td></tr>
          <tr><th>VMs</th><td>${escHtml(String(px.vm_count ?? host.vm_count ?? 0))} total, ${escHtml(String(px.running_count ?? 0))} running</td></tr>
          <tr><th>Last Check-in</th><td>${escHtml(_hubRelativeTime(px.last_seen || host.last_seen))}</td></tr>
          <tr><th>HW Faults</th><td>${_hubFormatFaults(hwFaults)}</td></tr>
        </tbody>
      </table>
    </div>
    <div class="setup-card setup-section-gap">
      <div class="setup-card-header" style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
        <div>
          <h2 style="margin:0;">Proxmox Console Access</h2>
          <p style="margin:6px 0 0;">Store a Proxmox API token so the hub can mint short-lived noVNC console sessions.</p>
        </div>
        <button class="btn btn-primary btn-small" id="hub-proxmox-credentials-btn" type="button">Configure Credentials</button>
      </div>
      <table class="data-table" style="margin-top:12px;">
        <tbody>
          <tr><th>Proxmox Host</th><td id="hub-proxmox-credentials-host">${escHtml(proxmoxHost || "—")}</td></tr>
          <tr><th>API Token</th><td id="hub-proxmox-credentials-status">Loading…</td></tr>
        </tbody>
      </table>
      <div class="muted" style="margin-top:10px;font-size:0.82rem;">Token format: <code>root@pam!tokenid=secret</code></div>
    </div>`;
}

function wireHubVmServerDetailsPanel(panel, spokeId) {
  const btn = panel.querySelector('#hub-node-update-agent-btn');
  const configureBtn = panel.querySelector('#hub-proxmox-credentials-btn');
  const host = getHubVmServerHostBySpokeId(spokeId) || {};
  const fallbackHost = getHubVmConsoleFallbackHost(host, host.node || {});
  loadHubProxmoxCredentialSummary(panel, spokeId, fallbackHost);
  configureBtn?.addEventListener('click', () => openHubProxmoxCredentialsModal(spokeId, fallbackHost));
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Updating…';
    try {
      await sendHubProxmoxCommand(getActiveTenantId(), spokeId, 'update_agent', {});
      showToast('Agent update queued for this node.', 'ok');
    } catch (err) {
      showToast(`Error: ${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
}

function renderHubVmServerReclonePanel(spokeId, reclone, actionVms) {
  const status = reclone.status || "idle";
  const statusColor = status === "running" ? "badge-blue" : status === "interrupted" ? "badge-warn" : "badge-grey";
  const pct = reclone.total > 0 ? Math.round((reclone.completed / reclone.total) * 100) : 0;
  const log = Array.isArray(reclone.log) ? reclone.log : [];
  return `
    <div class="setup-card setup-section-gap">
      <div class="setup-card-header" style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <h2 style="font-size:1rem;margin:0;">Fleet Reclone</h2>
        <span class="badge ${statusColor}">${escHtml(status)}</span>
        <button class="btn btn-primary btn-small" id="hub-reclone-all-btn" type="button">⟳ Reclone All</button>
      </div>
      ${status === "running" ? `
        <div class="reclone-meta" style="margin-bottom:8px;">
          <span class="muted">VM: ${escHtml(String(reclone.current_vm || "—"))}</span>
          &nbsp;·&nbsp;
          <span class="muted">${reclone.completed || 0} / ${reclone.total || 0}</span>
        </div>
        <div class="progress-bar-wrap" style="margin-bottom:8px;">
          <div class="progress-bar" style="width:${pct}%"></div>
        </div>` : ""}
      ${reclone.last_run ? `<div class="muted" style="font-size:0.82rem;margin-bottom:8px;">Last run: ${escHtml(String(reclone.last_run))}</div>` : ""}
      ${log.length ? `
        <div class="setup-card-header"><h3 style="font-size:0.9rem;margin:0;">Recent Log</h3></div>
        <div class="autoprov-live-list" style="max-height:200px;overflow-y:auto;font-size:0.82rem;">
          ${log.slice(-30).reverse().map(e => `<div class="autoprov-log-item">${escHtml(typeof e === "string" ? e : JSON.stringify(e))}</div>`).join("")}
        </div>` : ""}
      ${actionVms.length ? `
        <div class="setup-card-header" style="margin-top:10px;"><h3 style="font-size:0.9rem;margin:0;">Per-VM Reclone</h3></div>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>VMID</th><th>Name</th><th>Status</th><th>Action</th></tr></thead>
            <tbody>
              ${actionVms.map(vm => `<tr>
                <td>${escHtml(String(vm.vmid ?? "—"))}</td>
                <td>${escHtml(vm.name || "—")}</td>
                <td>${_hubVmStatusDot(vm)} ${escHtml(vm.status || "—")}</td>
                <td><button class="btn btn-warning btn-small hub-vm-action" data-action="reclone_vm"
                            data-vmid="${escHtml(String(vm.vmid))}" type="button">↺ Reclone</button></td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>` : ""}
    </div>`;
}

function renderHubVmServerClientsPanel(clients) {
  if (!clients.length) {
    return `<div class="setup-card setup-section-gap"><div class="empty-state">No clients reported by this spoke.</div></div>`;
  }
  const rows = clients.map(c => {
    const status = c.status || c.state || "unknown";
    const dot = status === "online" || status === "up" ? "🟢" : status === "offline" || status === "down" ? "⚫" : "🟡";
    return `<tr>
      <td>${dot} ${escHtml(status)}</td>
      <td>${escHtml(c.hostname || c.name || "—")}</td>
      <td>${escHtml(String(c.vmid ?? c.proxmox_vmid ?? "—"))}</td>
      <td>${escHtml(c.ip || c.ip_address || "—")}</td>
      <td>${escHtml(c.site || c.site_name || "—")}</td>
      <td>${escHtml(c.type || c.client_type || "—")}</td>
    </tr>`;
  }).join("");
  return `
    <div class="setup-card setup-section-gap">
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Status</th><th>Hostname</th><th>VMID</th><th>IP</th><th>Site</th><th>Type</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

function renderHubVmServerApiServerPanel(apiServer) {
  const health = apiServer.health || {};
  const services = apiServer.services || {};
  const serviceRows = Object.entries(services).map(([name, svc]) => {
    const ok = svc.consecutive_errors === 0;
    const dot = ok ? "🟢" : "🔴";
    const lastRun = svc.last_run ? new Date(svc.last_run).toLocaleTimeString() : "—";
    return `<tr>
      <td>${dot}</td>
      <td>${escHtml(name)}</td>
      <td>${escHtml(String(svc.run_count ?? "—"))}</td>
      <td>${escHtml(String(svc.consecutive_errors ?? "—"))}</td>
      <td>${escHtml(lastRun)}</td>
    </tr>`;
  }).join("");

  const healthDot = health.status === "ok" ? "🟢" : "🔴";
  return `
    <div class="setup-card setup-section-gap">
      <table class="data-table" style="margin-bottom:16px;">
        <tbody>
          <tr><th>Status</th><td>${healthDot} ${escHtml(health.status || "—")}</td></tr>
          <tr><th>Version</th><td>${escHtml(health.version || "—")}</td></tr>
          <tr><th>Clients</th><td>${escHtml(String(health.clients ?? "—"))}</td></tr>
          <tr><th>Repo Synced</th><td>${health.repo_synced ? "✅ Yes" : "⚠️ No"}</td></tr>
          ${health.repo_error ? `<tr><th>Repo Error</th><td style="color:var(--danger-color)">${escHtml(health.repo_error)}</td></tr>` : ""}
        </tbody>
      </table>
      ${serviceRows ? `
      <h3 style="margin:0 0 8px;font-size:0.95rem;">Background Services</h3>
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th></th><th>Service</th><th>Runs</th><th>Errors</th><th>Last Run</th></tr></thead>
          <tbody>${serviceRows}</tbody>
        </table>
      </div>` : ""}
    </div>`;
}

function renderHubVmServerCentralPanel(central) {
  const tokenState = central.token_state || {};
  const alerts = Array.isArray(central.hardware_alerts) ? central.hardware_alerts : [];
  const tokenDot = central.token_valid ? "🟢" : "⚫";

  const alertRows = alerts.map(a => `<tr>
    <td>${escHtml(a.severity || "—")}</td>
    <td>${escHtml(a.device || a.name || "—")}</td>
    <td>${escHtml(a.description || a.message || "—")}</td>
  </tr>`).join("");

  return `
    <div class="setup-card setup-section-gap">
      <table class="data-table" style="margin-bottom:16px;">
        <tbody>
          <tr><th>Token</th><td>${tokenDot} ${escHtml(tokenState.state || (central.token_valid ? "valid" : "invalid"))}</td></tr>
          ${tokenState.detail ? `<tr><th>Detail</th><td class="muted">${escHtml(tokenState.detail)}</td></tr>` : ""}
        </tbody>
      </table>
      ${alerts.length ? `
      <h3 style="margin:0 0 8px;font-size:0.95rem;">Hardware Alerts (${alerts.length})</h3>
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Severity</th><th>Device</th><th>Description</th></tr></thead>
          <tbody>${alertRows}</tbody>
        </table>
      </div>` : `<div class="empty-state">No hardware alerts.</div>`}
    </div>`;
}

function renderHubVmServerConfigPanel(host) {
  const node = (host.proxmox || {}).node || {};
  const px = host.proxmox || {};
  const cfg = host.spoke_config || {};
  const maxSlots = parseInt(cfg.usb_max_slots || "24", 10);
  const vmidStart = parseInt(cfg.vmid_start || "0", 10);
  const needsManual = maxSlots > 25;
  const autoStart = 90000 + ((host._host_id_num || 0) - 1) * 50 + 1;
  return `
    <div class="setup-card setup-section-gap">
      <table class="data-table" style="margin-bottom:16px;">
        <tbody>
          <tr><th>Proxmox Connected</th><td>${px.connected ? "🟢 Yes" : "⚫ No"}</td></tr>
          <tr><th>Agent Version</th><td>${escHtml(px.agent_version || "—")}</td></tr>
          <tr><th>PVE Version</th><td>${escHtml(px.pve_version || "—")}</td></tr>
          <tr><th>Node</th><td>${escHtml(node.node || "—")}</td></tr>
          <tr><th>CPU</th><td>${node.cpu != null ? Number(node.cpu).toFixed(1) + "%" : "—"}</td></tr>
          <tr><th>Memory</th><td>${node.mem && node.maxmem ? `${fmtSize(node.mem * 1024 * 1024)} / ${fmtSize(node.maxmem * 1024 * 1024)}` : "—"}</td></tr>
          <tr><th>VMs</th><td>${escHtml(String(px.vm_count ?? "—"))} total, ${escHtml(String(px.running_count ?? "—"))} running</td></tr>
          <tr><th>Last Agent Check-in</th><td>${escHtml(px.last_seen ? new Date(typeof px.last_seen === "number" ? px.last_seen * 1000 : px.last_seen).toLocaleString() : "—")}</td></tr>
        </tbody>
      </table>
      <div class="setup-card-header"><h3 style="margin:0 0 10px;">VM Slot Config</h3></div>
      <div class="setup-form">
        <div class="form-group">
          <label class="form-label" for="hub-spoke-max-slots">Max USB Slots</label>
          <input id="hub-spoke-max-slots" class="form-input" type="number" min="1" max="256" value="${maxSlots}" style="max-width:100px;">
          <div class="muted" style="font-size:0.8rem;margin-top:4px;">Standard range: 1–25 (auto VMID). Above 25 requires a manual VMID start.</div>
        </div>
        <div class="form-group" id="hub-spoke-vmid-start-group" style="${needsManual ? "" : "opacity:0.4;pointer-events:none;"}">
          <label class="form-label" for="hub-spoke-vmid-start">
            Manual VMID Start
            ${needsManual ? '<span style="color:var(--danger-color,#e74c3c);margin-left:4px;">⚠ Required</span>' : ""}
          </label>
          <input id="hub-spoke-vmid-start" class="form-input" type="number" min="0" value="${vmidStart}" style="max-width:120px;" ${needsManual ? "" : "disabled"}>
          <div class="muted" style="font-size:0.8rem;margin-top:4px;">
            ${needsManual
              ? `Set the first VMID for this host's slot range (e.g. <strong>${autoStart}</strong> for auto).`
              : "Auto-calculated from hostname (≤25 slots). Set slots &gt;25 to enable."}
          </div>
        </div>
        <div class="form-actions">
          <button id="hub-spoke-config-save-btn" class="btn btn-primary" type="button">Save</button>
          <span id="hub-spoke-config-msg" class="form-msg"></span>
        </div>
      </div>
    </div>`;
}

async function sendHubProxmoxCommand(tenantId, spokeId, action, args = {}) {
  const token = sessionStorage.getItem("hub_token");
  const resp = await fetch(`/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spokeId)}/proxmox-command`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ action, args }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return await resp.json().catch(() => ({}));
}

function wireHubVmActions(panel, spokeId) {
  const tenantId = getActiveTenantId();

  panel.querySelectorAll(".hub-vm-cat-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      panel.querySelectorAll(".hub-vm-cat-tab").forEach(b => b.classList.toggle("active", b === btn));
      const cat = btn.dataset.cat;
      panel.querySelectorAll("[id^=hub-vm-cat-panel-]").forEach(p => p.classList.add("hidden"));
      panel.querySelector(`#hub-vm-cat-panel-${cat}`)?.classList.remove("hidden");
      const bulkBar = panel.querySelector("#hub-server-bulk-bar");
      if (bulkBar) bulkBar.classList.toggle("hidden", cat === "templates");
    });
  });

  panel.querySelector("#hub-vm-select-all")?.addEventListener("change", (e) => {
    panel.querySelectorAll(".hub-vm-check:not([disabled])").forEach(cb => {
      cb.checked = e.target.checked;
    });
  });

  panel.querySelectorAll("[id^=hub-server-th-check-]").forEach(thChk => {
    thChk.addEventListener("change", (e) => {
      const cat = thChk.id.replace("hub-server-th-check-", "");
      const tbody = panel.querySelector(`#hub-vm-tbody-${cat}`);
      tbody?.querySelectorAll(".hub-vm-check:not([disabled])").forEach(cb => {
        cb.checked = e.target.checked;
      });
    });
  });

  panel.querySelectorAll('.hub-vm-action-btn:not([disabled])').forEach(btn => {
    btn.addEventListener('click', async () => {
      const vmid = parseInt(btn.dataset.vmid, 10);
      const action = btn.dataset.action;
      const tr = btn.closest('tr');
      const vmType = tr?.dataset.vmType || 'qemu';
      const vmName = tr?.dataset.vmName || `VM ${vmid}`;
      const srcVmid = tr?.dataset.recloneSourceVmid;
      if (action === 'console') {
        await openHubVmConsole(spokeId, vmid, vmType, vmName);
        return;
      }
      const args = { vmid, type: vmType };
      if (srcVmid) args.source_vmid = parseInt(srcVmid, 10);
      try {
        await sendHubProxmoxCommand(tenantId, spokeId, action, args);
      } catch (err) {
        showToast(`Error: ${err.message}`, 'error');
      }
    });
  });

  panel.querySelectorAll(".hub-vm-bulk-action").forEach(btn => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      const checked = [...panel.querySelectorAll(".hub-vm-check:checked")];
      if (!checked.length) {
        showToast("No VMs selected", "warn");
        return;
      }
      for (const cb of checked) {
        const vmid = parseInt(cb.dataset.vmid, 10);
        const tr = cb.closest("tr");
        const vmType = tr?.dataset.vmType || "qemu";
        const srcVmid = tr?.dataset.recloneSourceVmid;
        const args = { vmid, type: vmType };
        if (srcVmid) args.source_vmid = parseInt(srcVmid, 10);
        try {
          await sendHubProxmoxCommand(tenantId, spokeId, action, args);
        } catch (err) {
          showToast(`Error on VM ${vmid}: ${err.message}`, "error");
        }
      }
      showToast(`${action} sent to ${checked.length} VM(s)`, "ok");
    });
  });

  panel.querySelector("#hub-reclone-all-btn")?.addEventListener("click", async () => {
    try {
      await sendHubProxmoxCommand(tenantId, spokeId, "reclone_all", {});
    } catch (err) {
      showToast(`Error: ${err.message}`, "error");
    }
  });

  panel.querySelectorAll("[data-action='certify']").forEach(btn => {
    btn.addEventListener("click", async () => {
      const vidpid = btn.dataset.vidpid;
      const name = btn.dataset.name;
      try {
        await sendHubProxmoxCommand(tenantId, spokeId, "certify_usb", { vidpid, name });
        showToast(`${vidpid} queued for certification`, "ok");
      } catch (err) {
        showToast(`Error: ${err.message}`, "error");
      }
    });
  });

  panel.querySelectorAll("[data-action='ignore']").forEach(btn => {
    btn.addEventListener("click", async () => {
      const vidpid = btn.dataset.vidpid;
      try {
        await sendHubProxmoxCommand(tenantId, spokeId, "ignore_usb", { vidpid });
        showToast(`${vidpid} queued for ignore`, "ok");
      } catch (err) {
        showToast(`Error: ${err.message}`, "error");
      }
    });
  });
}

function wireHubRecloneActions(panel, spokeId, actionVms) {
  panel.querySelector("#hub-reclone-all-btn")?.addEventListener("click", () => {
    if (!confirm("Reclone all VMs on this spoke via hub?")) return;
    sendHubProxmoxCommand(currentTenantId, spokeId, "reclone_all", {});
  });
  panel.querySelectorAll(".hub-vm-action").forEach(btn => {
    btn.addEventListener("click", () => {
      const vmid = parseInt(btn.dataset.vmid, 10);
      sendHubProxmoxCommand(currentTenantId, spokeId, "reclone_vm", { vmid });
    });
  });
}

function wireHubVmServerConfigPanel(panel, spokeId, host) {
  const maxSlotsInput = panel.querySelector("#hub-spoke-max-slots");
  const vmidStartInput = panel.querySelector("#hub-spoke-vmid-start");
  const vmidStartGroup = panel.querySelector("#hub-spoke-vmid-start-group");
  const saveBtn = panel.querySelector("#hub-spoke-config-save-btn");
  const msg = panel.querySelector("#hub-spoke-config-msg");

  const updateVmidVisibility = () => {
    const slots = parseInt(maxSlotsInput?.value || "24", 10);
    const needsManual = slots > 25;
    if (vmidStartGroup) {
      vmidStartGroup.style.opacity = needsManual ? "1" : "0.4";
      vmidStartGroup.style.pointerEvents = needsManual ? "" : "none";
    }
    if (vmidStartInput) vmidStartInput.disabled = !needsManual;
  };

  maxSlotsInput?.addEventListener("input", updateVmidVisibility);

  saveBtn?.addEventListener("click", async () => {
    const slots = parseInt(maxSlotsInput?.value || "24", 10);
    const vmidStart = parseInt(vmidStartInput?.value || "0", 10);
    if (slots > 25 && (!vmidStart || vmidStart < 1)) {
      if (msg) { msg.textContent = "⚠ A Manual VMID Start is required when slots > 25."; msg.style.color = "var(--danger-color,#e74c3c)"; }
      return;
    }
    saveBtn.disabled = true;
    try {
      const res = await apiFetch(`/api/${encodeURIComponent(currentTenantId)}/spokes/${encodeURIComponent(spokeId)}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ usb_max_slots: String(slots), vmid_start: vmidStart }),
      });
      if (!res.ok) throw new Error(await res.text());
      if (msg) { msg.textContent = "✓ Saved"; msg.style.color = ""; setTimeout(() => { if (msg) msg.textContent = ""; }, 3000); }
      loadVmServer(true);
    } catch (err) {
      if (msg) { msg.textContent = err.message || "Save failed."; msg.style.color = "var(--danger-color,#e74c3c)"; }
    } finally {
      saveBtn.disabled = false;
    }
  });
}

function renderHubApiServer() {
  const container = $("#hub-api-server-content");
  if (!container) return;
  const rows = aggregateApiServerRows || [];
  const healthyCount = rows.filter(row => String(row.api_server?.health?.status || row.api_server?.status || "").toLowerCase() === "ok").length;
  const versions = uniqueValues(rows.map(row => row.api_server?.health?.version || row.api_server?.version).filter(Boolean));
  $("#hub-api-spokes-pill") && ($("#hub-api-spokes-pill").textContent = `${rows.length} spokes`);
  $("#hub-api-online-pill") && ($("#hub-api-online-pill").textContent = `${healthyCount} healthy`);
  $("#hub-api-version-pill") && ($("#hub-api-version-pill").textContent = versions.length ? versions.join(" • ") : "— versions");
  if (!rows.length) {
    container.innerHTML = '<div class="empty-state">No API server telemetry reported for this tenant.</div>';
    return;
  }
  container.innerHTML = `
    <div class="tenant-detail-grid">
      ${rows.map(row => {
        const health = row.api_server?.health || row.api_server || {};
        const services = row.api_server?.services || {};
        const serviceCount = Object.keys(services).length;
        const state = String(health.status || row.spoke_online && "ok" || "offline").toLowerCase();
        const pillClass = state === "ok" ? "online" : state === "offline" ? "offline" : "pending";
        return `
          <details class="setup-card"${row.spoke_online ? " open" : ""}>
            <summary class="panel-header">
              <span class="server-node-name">${escHtml(spokeDisplayName(row, "Spoke"))}</span>
              <span class="site-status-pill ${pillClass}">${escHtml(state || "unknown")}</span>
              <span class="stat-pill">${escHtml(health.version || "—")}</span>
              <span class="stat-pill">${serviceCount} services</span>
            </summary>
            <div class="setup-status-grid setup-section-gap">
              <div class="setup-status-item"><span class="setup-status-label">Spoke Status</span><span class="setup-status-value">${row.spoke_online ? "Online" : "Offline"}</span></div>
              <div class="setup-status-item"><span class="setup-status-label">API Version</span><span class="setup-status-value">${escHtml(health.version || "—")}</span></div>
              <div class="setup-status-item"><span class="setup-status-label">Clients</span><span class="setup-status-value">${escHtml(String(health.clients ?? "—"))}</span></div>
              <div class="setup-status-item"><span class="setup-status-label">Repo Sync</span><span class="setup-status-value">${escHtml(health.repo_synced ? "Synced" : health.repo_error || "Unknown")}</span></div>
            </div>
            <pre class="setup-section-gap" style="margin:0;max-height:280px;overflow:auto;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:10px;font-size:12px;line-height:1.35;">${escHtml(JSON.stringify(row.api_server || {}, null, 2))}</pre>
          </details>
        `;
      }).join("")}
    </div>
  `;
}

function hubNormalizeCentralSitesConfig(config = {}) {
  return {
    site_mappings: config?.site_mappings && typeof config.site_mappings === "object" && !Array.isArray(config.site_mappings)
      ? { ...config.site_mappings }
      : {},
    monitored_checks: Array.isArray(config?.monitored_checks) ? config.monitored_checks.filter(item => item && typeof item === "object") : [],
    hardware_checks: Array.isArray(config?.hardware_checks) ? config.hardware_checks.filter(item => item && typeof item === "object") : [],
  };
}

function hubCloneCentralSitesConfig(config = {}) {
  return hubNormalizeCentralSitesConfig(JSON.parse(JSON.stringify(config || {})));
}

function hubSerializeCentralSitesConfig(config = {}) {
  const normalized = hubNormalizeCentralSitesConfig(config);
  const sortedMappings = Object.fromEntries(
    Object.entries(normalized.site_mappings || {}).sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" }))
  );
  const sortChecks = (checks = [], keys = []) => [...checks].map(check => {
    const normalizedCheck = {};
    keys.forEach(key => {
      if (check?.[key] !== undefined && check[key] !== "") normalizedCheck[key] = check[key];
    });
    return normalizedCheck;
  }).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right), undefined, { numeric: true, sensitivity: "base" }));
  return JSON.stringify({
    site_mappings: sortedMappings,
    monitored_checks: sortChecks(normalized.monitored_checks, ["type", "id", "name"]),
    hardware_checks: sortChecks(normalized.hardware_checks, ["id", "name", "device_type"]),
  });
}

function hubPrimeCentralSitesConfigDraft(config = {}) {
  const normalized = hubNormalizeCentralSitesConfig(config);
  const savedKey = hubSerializeCentralSitesConfig(normalized);
  if (!hubCentralSitesConfigDraft || hubCentralSitesConfigSavedKey !== savedKey) {
    hubCentralSitesConfigDraft = hubCloneCentralSitesConfig(normalized);
    hubCentralSitesConfigSavedKey = savedKey;
  }
  return {
    draft: hubCloneCentralSitesConfig(hubCentralSitesConfigDraft),
    dirty: hubSerializeCentralSitesConfig(hubCentralSitesConfigDraft) !== savedKey,
  };
}

function hubCentralStatusSpokes(statusData = hubCentralData) {
  const source = statusData?.hub_central_data && typeof statusData.hub_central_data === "object"
    ? statusData.hub_central_data
    : statusData;
  const spokes = source?.spokes;
  if (Array.isArray(spokes)) return spokes;
  if (spokes && typeof spokes === "object") {
    return Object.entries(spokes).map(([spokeId, spokeData]) => ({ spoke_id: spokeId, ...(spokeData || {}) }));
  }
  return [];
}

function hubCentralClientStatusSummary(info = {}) {
  const current = info?.current ?? "—";
  const hourlyAvg = typeof info?.hourly_avg === "number" ? Math.round(info.hourly_avg) : (info?.hourly_avg ?? "—");
  const drop = typeof info?.drop_pct === "number" ? formatClientCountDelta(info.drop_pct) : "—";
  return `Current ${current} • Avg ${hourlyAvg} • Drop ${drop}`;
}

function hubCentralClientStatusDetailHtml(info = {}) {
  return `${hubCentralStatusPill(info.status || "NO_DATA")}<div class="form-hint">${escHtml(hubCentralClientStatusSummary(info))}</div>`;
}

function hubCentralSiteMappingRowHtml(wsite = "", centralSite = "", disabled = !canManageTenant()) {
  const attr = disabled ? " disabled" : "";
  return `<tr>
    <td><input class="form-input hub-central-site-wsite" type="text" value="${escHtml(wsite)}" placeholder="Workspace site"${attr}></td>
    <td><input class="form-input hub-central-site-central" type="text" value="${escHtml(centralSite)}" placeholder="Central site"${attr}></td>
    <td><button class="btn btn-danger btn-small" type="button" data-hub-remove-row${attr}>Remove</button></td>
  </tr>`;
}

function hubCentralMonitoredCheckRowHtml(check = {}, disabled = !canManageTenant()) {
  const attr = disabled ? " disabled" : "";
  const type = String(check?.type || "alert").toLowerCase() === "insight" ? "insight" : "alert";
  return `<tr>
    <td>
      <select class="form-input hub-central-check-type"${attr}>
        <option value="alert"${type === "alert" ? " selected" : ""}>Alert</option>
        <option value="insight"${type === "insight" ? " selected" : ""}>Insight</option>
      </select>
    </td>
    <td><input class="form-input hub-central-check-id" type="text" value="${escHtml(check?.id || "")}" placeholder="Check ID"${attr}></td>
    <td><input class="form-input hub-central-check-name" type="text" value="${escHtml(check?.name || "")}" placeholder="Display name"${attr}></td>
    <td><button class="btn btn-danger btn-small" type="button" data-hub-remove-row${attr}>Remove</button></td>
  </tr>`;
}

function hubCentralHardwareCheckRowHtml(check = {}, disabled = !canManageTenant()) {
  const attr = disabled ? " disabled" : "";
  return `<tr>
    <td><input class="form-input hub-central-hw-id" type="text" value="${escHtml(check?.id || "")}" placeholder="Alert ID"${attr}></td>
    <td><input class="form-input hub-central-hw-name" type="text" value="${escHtml(check?.name || "")}" placeholder="Display name"${attr}></td>
    <td><input class="form-input hub-central-hw-device-type" type="text" value="${escHtml(check?.device_type || "")}" placeholder="ap / switch / gateway"${attr}></td>
    <td><button class="btn btn-danger btn-small" type="button" data-hub-remove-row${attr}>Remove</button></td>
  </tr>`;
}

function hubRenderCentralAvailableCheckGroups(container, selectedChecks = []) {
  if (!container) return;
  const selected = new Set((selectedChecks || []).map(check => `${check.type}:${check.id}`));
  const groups = [
    { key: "alerts", title: "Alerts", type: "alert" },
    { key: "insights", title: "Insights", type: "insight" },
  ];
  if (!hubCentralAvailableChecks.alerts.length && !hubCentralAvailableChecks.insights.length) {
    container.innerHTML = '<div class="form-hint">Load available checks to populate Aruba alerts and insights.</div>';
    return;
  }
  container.innerHTML = groups.map(group => {
    const items = hubCentralAvailableChecks[group.key] || [];
    if (!items.length) return "";
    return `
      <div class="checks-group">
        <h3 class="checks-group-title">${escHtml(group.title)}</h3>
        <div class="check-checkbox-list">
          ${items.map(item => `
            <label class="check-checkbox-item">
              <input type="checkbox" class="hub-central-check-toggle" data-type="${group.type}" data-id="${escHtml(item.id || "")}" data-name="${escHtml(item.name || item.id || "")}"${selected.has(`${group.type}:${item.id}`) ? " checked" : ""}${canManageTenant() ? "" : " disabled"}>
              <span>${escHtml(item.name || item.id || "")}</span>
            </label>
          `).join("")}
        </div>
      </div>`;
  }).join("");
}

function hubRenderCentralAvailableHardware(container, selectedChecks = []) {
  if (!container) return;
  const selected = new Set((selectedChecks || []).map(check => check.id));
  const items = hubCentralAvailableChecks.hardware || [];
  if (!items.length) {
    container.innerHTML = '<div class="form-hint">Load available checks to populate Aruba hardware alert types.</div>';
    return;
  }
  container.innerHTML = `
    <div class="check-checkbox-list">
      ${items.map(item => `
        <label class="check-checkbox-item">
          <input type="checkbox" class="hub-central-hw-toggle" data-id="${escHtml(item.id || "")}" data-name="${escHtml(item.name || item.id || "")}" data-device-type="${escHtml(item.device_type || "")}"${selected.has(item.id) ? " checked" : ""}${canManageTenant() ? "" : " disabled"}>
          <span>${escHtml(item.name || item.id || "")} ${item.device_type ? `(${escHtml(item.device_type)})` : ""}</span>
        </label>
      `).join("")}
    </div>`;
}

function hubSyncCentralLiveState(statusData = hubCentralData) {
  const source = statusData?.hub_central_data && typeof statusData.hub_central_data === "object"
    ? { ...statusData.hub_central_data, ...statusData }
    : statusData;
  const status = source?.status && typeof source.status === "object" ? { ...source.status } : {};
  const wireless = source?.wireless_clients && typeof source.wireless_clients === "object" ? { ...source.wireless_clients } : {};
  let hardwareAlerts = Array.isArray(source?.hardware_alerts) ? [...source.hardware_alerts] : [];
  const clientCountStatus = source?.client_count_status && typeof source.client_count_status === "object"
    ? { ...source.client_count_status }
    : {};
  for (const spoke of hubCentralStatusSpokes(statusData)) {
    if (!hardwareAlerts.length && Array.isArray(spoke?.hardware_alerts) && spoke.hardware_alerts.length) {
      hardwareAlerts = [...spoke.hardware_alerts];
    }
    if (spoke?.status && typeof spoke.status === "object") {
      Object.entries(spoke.status).forEach(([wsite, siteStatus]) => {
        if (wsite && !(wsite in status) && siteStatus && typeof siteStatus === "object") status[wsite] = siteStatus;
      });
    }
    if (spoke?.wireless_clients && typeof spoke.wireless_clients === "object") {
      Object.entries(spoke.wireless_clients).forEach(([wsite, count]) => {
        if (wsite && !(wsite in wireless)) wireless[wsite] = count;
      });
    }
    if (spoke?.client_count_status && typeof spoke.client_count_status === "object") {
      Object.entries(spoke.client_count_status).forEach(([wsite, info]) => {
        if (wsite && !(wsite in clientCountStatus) && info && typeof info === "object") clientCountStatus[wsite] = info;
      });
    }
    for (const site of (spoke?.sites || [])) {
      if (!site?.wsite) continue;
      if (!(site.wsite in status) && site.status_map && typeof site.status_map === "object") status[site.wsite] = site.status_map;
      if (!(site.wsite in wireless) && typeof site.wireless_clients === "number") wireless[site.wsite] = site.wireless_clients;
    }
  }
  hubCentralSiteStatus = status;
  hubCentralWirelessClients = wireless;
  hubCentralHardwareAlerts = hardwareAlerts;
  hubClientCountStatus = clientCountStatus;
  return { status, wireless, hardwareAlerts, clientCountStatus };
}

function hubCentralCollectSiteMappings() {
  const mappings = {};
  document.querySelectorAll("#hub-central-sites-tbody tr").forEach(row => {
    const wsite = row.querySelector(".hub-central-site-wsite")?.value?.trim() || "";
    const centralSite = row.querySelector(".hub-central-site-central")?.value?.trim() || "";
    if (wsite && centralSite) mappings[wsite] = centralSite;
  });
  return mappings;
}

function hubCentralCollectMonitoredChecks() {
  const checks = new Map();
  document.querySelectorAll("#hub-central-monitored-tbody tr").forEach(row => {
    const type = row.querySelector(".hub-central-check-type")?.value?.trim() || "alert";
    const id = row.querySelector(".hub-central-check-id")?.value?.trim() || "";
    const name = row.querySelector(".hub-central-check-name")?.value?.trim() || id;
    if (!id) return;
    checks.set(`${type}:${id}`, { type, id, name });
  });
  document.querySelectorAll("#hub-central-available-checks .hub-central-check-toggle:checked").forEach(input => {
    const type = input.dataset.type || "alert";
    const id = input.dataset.id || "";
    if (!id) return;
    checks.set(`${type}:${id}`, { type, id, name: input.dataset.name || id });
  });
  return [...checks.values()];
}

function hubCentralCollectHardwareChecks() {
  const checks = new Map();
  document.querySelectorAll("#hub-central-hardware-tbody tr").forEach(row => {
    const id = row.querySelector(".hub-central-hw-id")?.value?.trim() || "";
    const name = row.querySelector(".hub-central-hw-name")?.value?.trim() || id;
    const device_type = row.querySelector(".hub-central-hw-device-type")?.value?.trim() || "";
    if (!id) return;
    checks.set(id, { id, name, device_type });
  });
  document.querySelectorAll("#hub-central-available-hardware .hub-central-hw-toggle:checked").forEach(input => {
    const id = input.dataset.id || "";
    if (!id) return;
    checks.set(id, { id, name: input.dataset.name || id, device_type: input.dataset.deviceType || "" });
  });
  return [...checks.values()];
}

function hubBuildCentralSitesConfigDraftFromDom() {
  return hubNormalizeCentralSitesConfig({
    site_mappings: hubCentralCollectSiteMappings(),
    monitored_checks: hubCentralCollectMonitoredChecks(),
    hardware_checks: hubCentralCollectHardwareChecks(),
  });
}

function hubRefreshCentralSitesDraftState() {
  if (!document.getElementById("save-hub-central-sites-btn")) return false;
  hubCentralSitesConfigDraft = hubBuildCentralSitesConfigDraftFromDom();
  const dirty = hubSerializeCentralSitesConfig(hubCentralSitesConfigDraft) !== hubCentralSitesConfigSavedKey;
  const dirtyPill = document.getElementById("hub-central-sites-dirty");
  if (dirtyPill) dirtyPill.classList.toggle("hidden", !dirty);
  return dirty;
}

async function loadHubCentralAvailableChecksCatalog() {
  const msgId = "hub-central-sites-msg";
  const tenantId = aggregateCentralData?.tenant_id || getActiveTenantId();
  if (!tenantId) return;
  try {
    const data = await requestJson(`/api/central/available?tenant_id=${encodeURIComponent(tenantId)}`);
    hubCentralAvailableChecks = {
      alerts: data.alerts || [],
      insights: data.insights || [],
      hardware: data.hardware || [],
    };
    hubRenderCentralAvailableCheckGroups(document.getElementById("hub-central-available-checks"), hubCentralCollectMonitoredChecks());
    hubRenderCentralAvailableHardware(document.getElementById("hub-central-available-hardware"), hubCentralCollectHardwareChecks());
    const total = hubCentralAvailableChecks.alerts.length + hubCentralAvailableChecks.insights.length;
    setFormMessage(msgId, `${total} checks loaded${data.warning ? ` — ${data.warning}` : ""}.`, !data.warning);
  } catch (error) {
    setFormMessage(msgId, error.message || "Unable to load Aruba Central checks.", false);
  }
}

async function saveHubCentralSitesConfig() {
  if (!canManageTenant()) {
    setFormMessage("hub-central-sites-msg", "Tenant Viewer access is read-only.", false);
    return;
  }
  const tenantId = aggregateCentralData?.tenant_id || getActiveTenantId();
  if (!tenantId) return;
  const payload = hubBuildCentralSitesConfigDraftFromDom();
  try {
    const savedConfig = hubNormalizeCentralSitesConfig(await requestJson(`/api/${encodeURIComponent(tenantId)}/aggregate/central-sites-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    aggregateCentralData = {
      ...(aggregateCentralData || {}),
      tenant_id: tenantId,
      central_sites_config: savedConfig,
    };
    hubCentralSitesConfigDraft = hubCloneCentralSitesConfig(savedConfig);
    hubCentralSitesConfigSavedKey = hubSerializeCentralSitesConfig(savedConfig);
    await loadCentral(true);
    setFormMessage("hub-central-sites-msg", "Central site config saved.", true);
    hubRefreshCentralSitesDraftState();
    if (activeTab === "central") await loadHubCentralData(true);
  } catch (error) {
    setFormMessage("hub-central-sites-msg", error.message || "Unable to save Central site config.", false);
  }
}

function hydrateHubCentralConfigForm(config) {
  const mappingBody = document.getElementById("hub-central-sites-tbody");
  const monitoredBody = document.getElementById("hub-central-monitored-tbody");
  const hardwareBody = document.getElementById("hub-central-hardware-tbody");
  const availableChecksContainer = document.getElementById("hub-central-available-checks");
  const availableHardwareContainer = document.getElementById("hub-central-available-hardware");
  const addMappingBtn = document.getElementById("hub-central-add-mapping-btn");
  const addCheckBtn = document.getElementById("hub-central-add-check-btn");
  const addHwBtn = document.getElementById("hub-central-add-hw-btn");
  const loadBtn = document.getElementById("hub-central-load-available-btn");
  const saveBtn = document.getElementById("save-hub-central-sites-btn");
  const syncDraft = () => {
    setFormMessage("hub-central-sites-msg", "", true);
    hubRefreshCentralSitesDraftState();
  };

  hubRenderCentralAvailableCheckGroups(availableChecksContainer, config.monitored_checks || []);
  hubRenderCentralAvailableHardware(availableHardwareContainer, config.hardware_checks || []);

  [mappingBody, monitoredBody, hardwareBody].forEach(body => {
    if (!body) return;
    body.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-hub-remove-row]");
      if (btn) {
        btn.closest("tr")?.remove();
        syncDraft();
      }
    });
    body.addEventListener("input", syncDraft);
    body.addEventListener("change", syncDraft);
  });
  availableChecksContainer?.addEventListener("change", syncDraft);
  availableHardwareContainer?.addEventListener("change", syncDraft);
  if (addMappingBtn) addMappingBtn.onclick = () => {
    mappingBody?.insertAdjacentHTML("beforeend", hubCentralSiteMappingRowHtml());
    syncDraft();
  };
  if (addCheckBtn) addCheckBtn.onclick = () => {
    monitoredBody?.insertAdjacentHTML("beforeend", hubCentralMonitoredCheckRowHtml());
    syncDraft();
  };
  if (addHwBtn) addHwBtn.onclick = () => {
    hardwareBody?.insertAdjacentHTML("beforeend", hubCentralHardwareCheckRowHtml());
    syncDraft();
  };
  if (loadBtn) loadBtn.onclick = () => { loadHubCentralAvailableChecksCatalog().catch(() => {}); };
  if (saveBtn) saveBtn.onclick = () => { saveHubCentralSitesConfig().catch(() => {}); };
  hubRefreshCentralSitesDraftState();
}

function hubCentralStatusPill(status) {
  const label = String(status || "UNKNOWN").toUpperCase();
  const pillClass = label === "OK" ? "online" : label === "DEGRADED" ? "pending" : "offline";
  return `<span class="site-status-pill ${pillClass}">${escHtml(label)}</span>`;
}

function renderHubCentral() {
  const container = $("#hub-central-content");
  if (!container) return;
  const data = aggregateCentralData || { spokes: [], hub_central_config: {}, central_sites_config: {}, mode: "distributed" };
  const spokes = data.spokes || [];
  const config = data.hub_central_config || {};
  const sitesConfig = hubNormalizeCentralSitesConfig(data.central_sites_config || {});
  const liveStatusData = data.hub_central_data && typeof data.hub_central_data === "object" ? data.hub_central_data : hubCentralData;
  if (liveStatusData) hubSyncCentralLiveState(liveStatusData);
  const connectedCount = data.mode === "centralized"
    ? ((liveStatusData?.token_valid || data.hub_central_data?.token_valid) ? 1 : 0)
    : spokes.filter(item => item.central_status?.token_valid).length;
  $("#hub-central-mode-pill") && ($("#hub-central-mode-pill").textContent = `${data.mode || "distributed"} mode`);
  $("#hub-central-spokes-pill") && ($("#hub-central-spokes-pill").textContent = `${spokes.length} spokes`);
  $("#hub-central-connected-pill") && ($("#hub-central-connected-pill").textContent = `${connectedCount} connected`);
  const disabled = canManageTenant() ? "" : " disabled";
  const note = canManageTenant() ? "" : '<div class="tenant-detail-note">Tenant Viewer access: Central settings are read-only.</div>';

  const centralControlCard = `
    <section class="setup-card">
      <div class="setup-card-header"><h2>Central Control</h2><p>Hub-managed Aruba Central settings and processing mode for this tenant.</p></div>
      <div class="setup-form">
        <div class="form-group">
          <label class="form-label" for="hub-central-mode">Mode</label>
          <select id="hub-central-mode" class="form-input"${disabled}>
            <option value="centralized"${data.mode === "centralized" ? " selected" : ""}>Centralized</option>
            <option value="distributed"${data.mode === "distributed" ? " selected" : ""}>Distributed</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label" for="hub-central-api-version">API Version</label>
          <select id="hub-central-api-version" class="form-input"${disabled}>
            <option value="classic"${config.api_version === "classic" ? " selected" : ""}>Classic</option>
            <option value="new_central"${config.api_version === "new_central" ? " selected" : ""}>Central / HPE GreenLake</option>
          </select>
        </div>
        <div class="form-group"><label class="form-label" for="hub-central-cluster-url">Cluster URL</label><input id="hub-central-cluster-url" type="url" class="form-input" value="${escHtml(config.cluster_url || "")}"${disabled}></div>
        <div class="form-group"><label class="form-label" for="hub-central-client-id">Client ID</label><input id="hub-central-client-id" type="text" class="form-input" value="${escHtml(config.client_id || "")}"${disabled}></div>
        <div class="form-group"><label class="form-label" for="hub-central-client-secret">Client Secret</label><input id="hub-central-client-secret" type="password" class="form-input" placeholder="Leave blank to keep existing"${disabled}></div>
        <div class="form-group"><label class="form-label" for="hub-central-customer-id">Customer ID</label><input id="hub-central-customer-id" type="text" class="form-input" value="${escHtml(config.customer_id || "")}"${disabled}></div>
        <div class="form-actions">
          <button id="save-central-btn" class="btn btn-primary" type="button"${disabled}>Save Central Settings</button>
          <span id="hub-central-msg" class="form-msg"></span>
        </div>
      </div>
    </section>`;

  if (data.mode !== "centralized") {
    const spokeRows = spokes.map(item => {
      const central = item.central_status || {};
      const state = central.token_state?.state || (central.token_valid ? "connected" : (item.spoke_online ? "unknown" : "offline"));
      const siteCount = Object.keys(central.status || {}).length;
      const pillClass = state === "connected" ? "online" : state === "offline" ? "offline" : "pending";
      return `
        <tr>
          <td><strong>${escHtml(spokeDisplayName(item, "Spoke"))}</strong></td>
          <td><span class="site-status-pill ${pillClass}">${escHtml(state)}</span></td>
          <td>${siteCount}</td>
          <td>${escHtml(item.spoke_online ? "Online" : "Offline")}</td>
          <td>${escHtml(item.last_seen ? relativeTime(item.last_seen) : "—")}</td>
        </tr>
      `;
    }).join("");
    container.innerHTML = `
      ${note}
      <div class="tenant-detail-grid">
        ${centralControlCard}
        <section class="setup-card">
          <div class="setup-card-header"><h2>Spoke Central Status</h2><p>Last known Central API status reported by each spoke.</p></div>
          <div class="table-scroll">
            <table class="data-table">
              <thead><tr><th>Spoke</th><th>Central Status</th><th>Mapped Sites</th><th>Spoke</th><th>Last Seen</th></tr></thead>
              <tbody>${spokeRows || '<tr><td colspan="5" class="empty-state">No spoke Central telemetry reported.</td></tr>'}</tbody>
            </table>
          </div>
        </section>
      </div>`;
    return;
  }

  const { draft: sitesConfigDraft, dirty: sitesConfigDirty } = hubPrimeCentralSitesConfigDraft(sitesConfig);
  const siteMappings = sitesConfig.site_mappings || {};
  const monitoredChecks = sitesConfig.monitored_checks || [];
  const draftEntries = Object.entries(sitesConfigDraft.site_mappings || {});
  const entries = Object.entries(siteMappings);
  const hasLiveCentralStatus = Boolean(
    Object.keys(hubCentralSiteStatus).length ||
    Object.keys(hubCentralWirelessClients).length ||
    Object.keys(hubClientCountStatus).length ||
    hubCentralStatusSpokes(liveStatusData).length
  );
  const overviewRows = entries.map(([wsite, centralSite]) => {
    const siteChecks = hubCentralSiteStatus[wsite] || {};
    const okCount = monitoredChecks.filter(check => siteChecks[check.id]?.status === "OK").length;
    const errorCount = monitoredChecks.filter(check => siteChecks[check.id]?.status === "ERROR").length;
    const wirelessCount = hubCentralWirelessClients[wsite] ?? hubClientCountStatus[wsite]?.current ?? "—";
    return `
      <tr>
        <td><strong>${escHtml(wsite)}</strong></td>
        <td>${escHtml(centralSite || "—")}</td>
        <td style="color:var(--hpe-green-dark);">${monitoredChecks.length ? okCount : "—"}</td>
        <td style="color:${errorCount ? '#c0392b' : 'inherit'};">${monitoredChecks.length ? errorCount : "—"}</td>
        <td>${escHtml(String(wirelessCount))}</td>
        <td>${hubCentralClientStatusDetailHtml(hubClientCountStatus[wsite] || {})}</td>
      </tr>`;
  }).join("");

  const clientRows = entries.map(([wsite, centralSite]) => {
    const info = hubClientCountStatus[wsite] || {};
    return `
      <tr>
        <td><strong>${escHtml(info.site_name || wsite)}</strong></td>
        <td>${escHtml(centralSite || wsite)}</td>
        <td>${escHtml(String(info.current ?? "—"))}</td>
        <td>${escHtml(String(typeof info.hourly_avg === "number" ? Math.round(info.hourly_avg) : (info.hourly_avg ?? "—")))}</td>
        <td>${escHtml(typeof info.drop_pct === "number" ? formatClientCountDelta(info.drop_pct) : "—")}</td>
        <td>${hubCentralStatusPill(info.status || "NO_DATA")}${info.baseline_stale ? ' <span class="form-hint">saved baseline</span>' : ''}</td>
      </tr>`;
  }).join("");

  container.innerHTML = `
    ${note}
    <div class="tenant-detail-grid">
      ${centralControlCard}
      <section class="setup-card">
        <div class="setup-card-header"><h2>Site Mappings Config</h2><p>Map workspace site names to Aruba Central site names for hub-side polling.</p></div>
        <div class="setup-form">
          <div class="form-group">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
              <label class="form-label">Site Mappings</label>
              <button id="hub-central-add-mapping-btn" class="btn btn-secondary btn-small" type="button"${disabled}>Add Row</button>
            </div>
            <div class="table-scroll">
              <table class="data-table">
                <thead><tr><th>Workspace Site</th><th>Central Site</th><th></th></tr></thead>
                <tbody id="hub-central-sites-tbody">${draftEntries.length ? draftEntries.map(([wsite, centralSite]) => hubCentralSiteMappingRowHtml(wsite, centralSite)).join("") : hubCentralSiteMappingRowHtml()}</tbody>
              </table>
            </div>
          </div>
          <div class="form-actions">
            <button id="save-hub-central-sites-btn" class="btn btn-primary" type="button"${disabled}>Save Site Config</button>
            <span id="hub-central-sites-dirty" class="site-status-pill pending${sitesConfigDirty ? '' : ' hidden'}">Unsaved</span>
            <span id="hub-central-sites-msg" class="form-msg"></span>
          </div>
        </div>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Monitored Checks Config</h2><p>Configure Aruba alerts and insights the hub should track for mapped sites.</p></div>
        <div class="setup-form">
          <div class="form-group">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
              <label class="form-label">Monitored Checks</label>
              <button id="hub-central-add-check-btn" class="btn btn-secondary btn-small" type="button"${disabled}>Add Check</button>
            </div>
            <div class="table-scroll">
              <table class="data-table">
                <thead><tr><th>Type</th><th>ID</th><th>Name</th><th></th></tr></thead>
                <tbody id="hub-central-monitored-tbody">${(sitesConfigDraft.monitored_checks || []).length ? sitesConfigDraft.monitored_checks.map(check => hubCentralMonitoredCheckRowHtml(check)).join("") : ""}</tbody>
              </table>
            </div>
            <div class="form-actions" style="justify-content:flex-start;">
              <button id="hub-central-load-available-btn" class="btn btn-secondary btn-small" type="button"${disabled}>Load Available Checks</button>
            </div>
            <div id="hub-central-available-checks"></div>
          </div>
        </div>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Hardware Checks Config</h2><p>Optional hardware alert types saved with the tenant Central site configuration.</p></div>
        <div class="setup-form">
          <div class="form-group">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
              <label class="form-label">Hardware Checks</label>
              <button id="hub-central-add-hw-btn" class="btn btn-secondary btn-small" type="button"${disabled}>Add Hardware Check</button>
            </div>
            <div class="table-scroll">
              <table class="data-table">
                <thead><tr><th>ID</th><th>Name</th><th>Device Type</th><th></th></tr></thead>
                <tbody id="hub-central-hardware-tbody">${(sitesConfigDraft.hardware_checks || []).length ? sitesConfigDraft.hardware_checks.map(check => hubCentralHardwareCheckRowHtml(check)).join("") : ""}</tbody>
              </table>
            </div>
            <div id="hub-central-available-hardware"></div>
          </div>
        </div>
      </section>
    </div>
    <div class="tenant-detail-grid setup-section-gap">
      <section class="setup-card">
        <div class="setup-card-header"><h2>Sites Overview</h2><p>Read-only Central health for each saved site mapping in centralized mode.</p></div>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>Site</th><th>Central Site</th><th>OK Checks</th><th>Error Checks</th><th>Wireless Clients</th><th>Client Status</th></tr></thead>
            <tbody>${entries.length ? (hasLiveCentralStatus ? overviewRows : '<tr><td colspan="6" class="empty-state">Waiting for centralized Aruba Central status data.</td></tr>') : '<tr><td colspan="6" class="empty-state">No Central site mappings configured yet.</td></tr>'}</tbody>
          </table>
        </div>
      </section>
      <section class="setup-card">
        <div class="setup-card-header"><h2>Client Count Status</h2><p>Per-site client baseline status from the hub Central poller.</p></div>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th>Site</th><th>Central Site</th><th>Current</th><th>Hourly Avg</th><th>Drop %</th><th>Status</th></tr></thead>
            <tbody>${entries.length ? (clientRows || '<tr><td colspan="6" class="empty-state">No client count samples collected yet.</td></tr>') : '<tr><td colspan="6" class="empty-state">No Central site mappings configured yet.</td></tr>'}</tbody>
          </table>
        </div>
      </section>
    </div>`;
  hydrateHubCentralConfigForm(sitesConfigDraft);
}

function renderHubConfigPage(data) {
  const approved = (data.spokes || []).filter(spoke => spoke.status === "approved");
  if (!hubConfigDraft) {
    const seed = approved.find(spoke => Object.keys(spoke.config || {}).length > 0)?.config || {};
    hubConfigDraft = JSON.stringify(seed, null, 2);
  }
  const readonly = canManageTenant() ? "" : " readonly";
  const disabled = canManageTenant() ? "" : " disabled";
  const note = canManageTenant() ? "" : '<div class="tenant-detail-note">Tenant Viewer access is read-only.</div>';
  const stateRows = approved.map(spoke => {
    const summary = summarizeHubConfigState(spoke);
    return `
      <tr>
        <td><strong>${escHtml(spokePrimaryLabel(spoke))}</strong></td>
        <td><span class="site-status-pill ${summary.className}">${escHtml(summary.label)}</span></td>
        <td>${escHtml(String(spoke.config_version || 0))}</td>
        <td>${escHtml(String(spoke.applied_config_version || 0))}</td>
        <td>${escHtml(spoke.last_config_applied_at ? fmtDate(spoke.last_config_applied_at) : "—")}</td>
        <td>${escHtml(Object.keys(spoke.config || {}).join(", ") || "—")}</td>
      </tr>
    `;
  }).join("");
  return `
    ${note}
    <nav class="setup-subnav setup-section-gap" role="tablist">
      <button class="setup-subtab hub-config-subtab ${hubConfigActiveSubtab === "api" ? "active" : ""}" data-hub-config-subtab="api" type="button">API</button>
      <button class="setup-subtab hub-config-subtab ${hubConfigActiveSubtab === "simulation" ? "active" : ""}" data-hub-config-subtab="simulation" type="button">Simulation Config</button>
    </nav>
    <div id="hub-config-api-panel" class="${hubConfigActiveSubtab === "api" ? "" : "hidden"}">
      <div class="setup-section-gap">${renderTenantConfigPanel(data)}</div>
      <div class="tenant-detail-grid setup-section-gap">
        <section class="setup-card">
          <div class="setup-card-header"><h2>Push Config to Spokes</h2><p>Save tenant config on the hub and deliver it to each spoke on its next inbox check.</p></div>
          <div class="setup-form">
            <div class="form-group">
              <label class="form-label" for="hub-config-payload">Config JSON</label>
              <textarea id="hub-config-payload" class="form-input" rows="14" spellcheck="false"${readonly}>${escHtml(hubConfigDraft || "{}")}</textarea>
            </div>
            <div class="form-actions">
              <button id="save-config-push-btn" class="btn btn-primary" type="button"${disabled}>Store + Push Config</button>
              <span id="hub-config-msg" class="form-msg"></span>
            </div>
          </div>
        </section>
        <section class="setup-card">
          <div class="setup-card-header"><h2>Per-Spoke Config State</h2><p>Desired hub config version versus last applied version on each spoke.</p></div>
          <div class="table-scroll">
            <table class="data-table">
              <thead><tr><th>Spoke</th><th>Status</th><th>Desired</th><th>Applied</th><th>Last Applied</th><th>Fields</th></tr></thead>
              <tbody>${stateRows || '<tr><td colspan="6" class="empty-state">No approved spokes in this tenant.</td></tr>'}</tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
    <div id="hub-sim-config-panel" class="${hubConfigActiveSubtab === "simulation" ? "" : "hidden"}"></div>
  `;
}

async function loadVmServer(force = false) {
  if (loadVmServer._inFlight) return;
  loadVmServer._inFlight = true;
  try {
  const container = $("#hub-vm-server-content");
  if (!container) return;
  const tenantId = getActiveTenantId();
  if (!tenantId || !currentUser) {
    aggregateProxmoxHosts = [];
    aggregateFleetRecloneStatus = defaultFleetRecloneStatus();
    aggregateUsbProvisioningStatus = defaultUsbProvisioningStatus();
    renderHubVmServer();
    return;
  }

  const cacheKey = `hub_vmserver_${tenantId}`;
  const saveCache = (hosts) => { try { localStorage.setItem(cacheKey, JSON.stringify(hosts)); } catch (_) {} };
  const loadCache = () => { try { const s = localStorage.getItem(cacheKey); return s ? JSON.parse(s) : null; } catch (_) { return null; } };

  const revalidate = async () => {
    const [fresh] = await Promise.all([
      loadAggregateData("proxmox"),
      loadHubVmServerAggregateStatus(),
    ]);
    const hosts = fresh?.hosts || [];
    aggregateProxmoxHosts = hosts;
    saveCache(hosts);
    renderHubVmServer();
  };

  if (!force && aggregateProxmoxHosts.length) {
    await loadHubVmServerAggregateStatus();
    renderHubVmServer();
    revalidate();
    return;
  }

  const cached = loadCache();
  if (!force && cached && cached.length) {
    aggregateProxmoxHosts = cached;
    await loadHubVmServerAggregateStatus();
    renderHubVmServer();
    revalidate();
    return;
  }

  container.innerHTML = '<div class="empty-state">Loading…</div>';
  await revalidate();
  } finally {
    loadVmServer._inFlight = false;
  }
}

async function loadApiServer(force = false) {
  const container = $("#hub-api-server-content");
  if (!container) return;
  if (!currentTenantId || !currentUser) {
    aggregateApiServerRows = [];
    renderHubApiServer();
    return;
  }
  container.innerHTML = '<div class="empty-state">Loading…</div>';
  const data = force || !aggregateApiServerRows.length ? await loadAggregateData("api-server") : { spokes: aggregateApiServerRows };
  aggregateApiServerRows = data?.spokes || [];
  renderHubApiServer();
}

async function loadCentral(force = false) {
  const container = $("#hub-central-content");
  if (!container) return;
  if (!currentTenantId || !currentUser) {
    aggregateCentralData = { mode: "distributed", hub_central_config: {}, central_sites_config: {}, spokes: [] };
    hubCentralSiteStatus = {};
    hubCentralWirelessClients = {};
    hubCentralHardwareAlerts = [];
    hubClientCountStatus = {};
    hubCentralSitesConfigDraft = null;
    hubCentralSitesConfigSavedKey = "";
    renderHubCentral();
    return;
  }
  container.innerHTML = '<div class="empty-state">Loading…</div>';
  const [data, statusData] = await Promise.all([
    force || !aggregateCentralData ? loadAggregateData("central") : Promise.resolve(aggregateCentralData),
    loadAggregateData("central-status"),
  ]);
  aggregateCentralData = data || { mode: "distributed", hub_central_config: {}, central_sites_config: {}, spokes: [] };
  if (statusData) {
    hubCentralData = statusData;
    hubSyncCentralLiveState(statusData);
  } else if (aggregateCentralData.mode !== "centralized") {
    hubCentralSiteStatus = {};
    hubCentralWirelessClients = {};
    hubCentralHardwareAlerts = [];
    hubClientCountStatus = {};
  }
  renderHubCentral();
}

// ── Hub Sites localStorage cache helpers ───────────────────────────────────
function hubSitesCacheKey() { return `hub_sites_${currentTenantId}`; }

function saveHubSitesCache(data) {
  try { localStorage.setItem(hubSitesCacheKey(), JSON.stringify(data)); } catch (_) {}
}

function loadHubSitesCache() {
  try { const s = localStorage.getItem(hubSitesCacheKey()); return s ? JSON.parse(s) : null; }
  catch (_) { return null; }
}

// ── Hub Central localStorage cache helpers ─────────────────────────────────
function hubCentralCacheKey() { return `hub_central_${currentTenantId}`; }

function saveHubCentralCache(data) {
  try { localStorage.setItem(hubCentralCacheKey(), JSON.stringify(data)); } catch (_) {}
}

function loadHubCentralCache() {
  try { const s = localStorage.getItem(hubCentralCacheKey()); return s ? JSON.parse(s) : null; }
  catch (_) { return null; }
}

function applyHubCentralData(data) {
  hubCentralData = data;
  hubSyncCentralLiveState(data);
  const spokes = data.spokes || [];
  const siteCount = hubCentralAggregateSites(data).length;
  $("#hcs-spokes-pill") && ($("#hcs-spokes-pill").textContent = `${spokes.length} spokes`);
  $("#hcs-sites-pill") && ($("#hcs-sites-pill").textContent = `${siteCount} sites`);
  renderHubCentralStatus();
}

async function loadHubCentralData(force = false) {
  const container = $("#hcs-overview");
  if (!container) return;
  if (!currentTenantId || !currentUser) {
    container.innerHTML = '<div class="empty-state">Sign in and select a tenant.</div>';
    return;
  }

  const saveCaches = (data) => {
    saveHubSitesCache(data);
    saveHubCentralCache(data);
  };
  const revalidate = () => {
    loadAggregateData("central-status").then(data => {
      if (data) {
        applyHubCentralData(data);
        saveCaches(data);
      }
    }).catch(() => {});
  };

  // In-memory cache still valid (tab switch within session) — render immediately.
  if (!force && hubCentralData) {
    renderHubCentralStatus();
    revalidate();
    return;
  }

  // Show localStorage cache immediately while fetching fresh data.
  const cached = loadHubSitesCache() || loadHubCentralCache();
  if (cached) {
    $("#hcs-site-detail")?.classList.add("hidden");
    $("#hcs-overview")?.classList.remove("hidden");
    applyHubCentralData(cached);
    revalidate();
    return;
  }

  // No cache at all — show loading spinner and wait for the first response.
  container.innerHTML = '<div class="empty-state">Loading…</div>';
  const data = await loadAggregateData("central-status");
  if (!data) {
    container.innerHTML = '<div class="empty-state">Unable to load Central data.</div>';
    return;
  }
  hubCentralSiteOpen = null;
  $("#hcs-site-detail")?.classList.add("hidden");
  $("#hcs-overview")?.classList.remove("hidden");
  applyHubCentralData(data);
  saveCaches(data);
}

function renderHubCentralStatus() {
  if (hubCentralActiveSubtab === "hcs-sites") renderHubCentralSites();
  else if (hubCentralActiveSubtab === "hcs-alerts") renderHubCentralAlerts();
  else if (hubCentralActiveSubtab === "hcs-clients") renderHubCentralClients();
}

function hubCentralAggregateSites(statusData = hubCentralData) {
  const source = statusData || {};
  const spokes = Array.isArray(source.spokes) ? source.spokes : [];
  const centralized = (source.mode || "distributed") === "centralized";
  const siteMap = new Map();

  for (const spoke of spokes) {
    for (const site of (spoke.sites || [])) {
      const wsite = String(site?.wsite || "").trim();
      if (!wsite) continue;
      if (!siteMap.has(wsite)) {
        siteMap.set(wsite, {
          wsite,
          central_site: site.central_site || "",
          check_ok: 0,
          check_fail: 0,
          check_unknown: 0,
          wireless_clients: 0,
          status_map: {},
          token_valid: centralized ? Boolean(source.token_valid) : false,
          spokes: [],
        });
      }
      const agg = siteMap.get(wsite);
      if (!agg.central_site && site.central_site) agg.central_site = site.central_site;
      if (centralized) {
        agg.check_ok = Math.max(agg.check_ok, site.check_ok || 0);
        agg.check_fail = Math.max(agg.check_fail, site.check_fail || 0);
        agg.check_unknown = Math.max(agg.check_unknown, site.check_unknown || 0);
        if (typeof site.wireless_clients === "number" && !agg.wireless_clients) agg.wireless_clients = site.wireless_clients;
      } else {
        agg.check_ok += site.check_ok || 0;
        agg.check_fail += site.check_fail || 0;
        agg.check_unknown += site.check_unknown || 0;
        if (typeof site.wireless_clients === "number") agg.wireless_clients += site.wireless_clients;
        if (spoke.token_valid) agg.token_valid = true;
      }
      for (const [cid, cv] of Object.entries(site.status_map || {})) {
        if (!agg.status_map[cid] || (cv.status === "ERROR" && agg.status_map[cid].status !== "ERROR")) {
          agg.status_map[cid] = { ...cv };
        }
      }
      if (!agg.spokes.some(existing => existing.spoke_id === spoke.spoke_id)) {
        agg.spokes.push({ spoke_id: spoke.spoke_id, spoke_name: spoke.spoke_name, spoke_online: spoke.spoke_online });
      }
    }
  }

  return [...siteMap.values()].sort((a, b) => a.wsite.localeCompare(b.wsite));
}

function hubCentralAggregateHardwareAlerts(statusData = hubCentralData) {
  const source = statusData || {};
  const alertMap = new Map();

  for (const spoke of (source.spokes || [])) {
    for (const alert of (spoke.hardware_alerts || [])) {
      const key = `${alert?.id || alert?.name || "unknown"}::${alert?.device_type || ""}`;
      if (!alertMap.has(key)) {
        alertMap.set(key, {
          id: alert?.id || "",
          name: alert?.name || alert?.id || "—",
          device_type: alert?.device_type || "",
          sites: {},
        });
      }
      const aggregate = alertMap.get(key);
      for (const [wsite, info] of Object.entries(alert?.sites || {})) {
        const existing = aggregate.sites[wsite] || { site_name: info?.site_name || wsite, devices: [] };
        const mergedDevices = new Set([...(existing.devices || []), ...((info?.devices || []).filter(Boolean))]);
        aggregate.sites[wsite] = {
          site_name: info?.site_name || existing.site_name || wsite,
          devices: [...mergedDevices].sort(),
        };
      }
    }
  }

  return [...alertMap.values()]
    .map(alert => ({
      ...alert,
      total: Object.values(alert.sites || {}).reduce((sum, site) => sum + ((site?.devices || []).length), 0),
    }))
    .sort((a, b) => String(a.name || a.id || "").localeCompare(String(b.name || b.id || "")));
}

function renderHubCentralSites() {
  const container = $("#hcs-overview");
  if (!container || !hubCentralData) return;
  const sites = hubCentralAggregateSites(hubCentralData);

  if (!sites.length) {
    container.innerHTML = '<div class="empty-state">No Central sites configured on any spoke.</div>';
    return;
  }

  const rows = sites.map(site => {
    const hasChecks = Object.keys(site.status_map).length > 0;
    return `<tr style="cursor:pointer;" title="View ${escHtml(site.wsite)} detail">
      <td><strong>${escHtml(site.wsite)}</strong></td>
      <td>${escHtml(site.central_site || '—')}</td>
      <td style="color:var(--hpe-green-dark);">${hasChecks ? site.check_ok : '—'}</td>
      <td style="color:${site.check_fail ? '#c0392b' : 'inherit'};">${hasChecks ? site.check_fail : '—'}</td>
      <td style="color:var(--muted);">${hasChecks ? site.check_unknown : '—'}</td>
      <td>${typeof site.wireless_clients === "number" ? site.wireless_clients : '—'}</td>
      <td><span class="status-dot ${site.token_valid ? 'online' : 'offline'}"></span></td>
      <td><button class="btn btn-small btn-secondary hcs-view-btn" data-wsite="${escHtml(site.wsite)}" type="button">View →</button></td>
    </tr>`;
  }).join('');

  container.innerHTML = `
    <div class="setup-card">
      <table class="data-table">
        <thead><tr><th>Site</th><th>Central Site</th><th>✓ OK</th><th>✗ Err</th><th>?</th><th>Wireless</th><th>Token</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  container.querySelectorAll("tr[title]").forEach(tr => {
    tr.addEventListener("click", () => openHubSiteDetail(tr.querySelector(".hcs-view-btn")?.dataset.wsite));
  });
  container.querySelectorAll(".hcs-view-btn").forEach(btn => {
    btn.addEventListener("click", event => { event.stopPropagation(); openHubSiteDetail(btn.dataset.wsite); });
  });
}

function openHubSiteDetail(wsite) {
  if (!wsite || !hubCentralData) return;
  hubCentralSiteOpen = wsite;
  const site = hubCentralAggregateSites(hubCentralData).find(entry => entry.wsite === wsite);
  if (!site) return;

  $("#hcs-overview")?.classList.add("hidden");
  $("#hcs-site-detail")?.classList.remove("hidden");
  const titleEl = $("#hcs-detail-title");
  const subEl = $("#hcs-detail-sub");
  if (titleEl) titleEl.textContent = wsite;
  if (subEl) subEl.textContent = site.central_site ? `Central site: ${site.central_site}` : "No Central site mapping";

  const checks = Object.entries(site.status_map || {});
  let checksHtml = "";
  if (checks.length) {
    const checkRows = checks.map(([cid, cv]) => {
      const ok = cv.status === "OK";
      const statusLabel = ok
        ? `<span style="color:var(--hpe-green-dark);font-weight:600;">✓ OK</span>`
        : `<span style="color:#c0392b;font-weight:600;">✗ ERROR</span>`;
      return `<tr>
        <td>${escHtml(cv.check_name || cid)}</td>
        <td style="color:var(--muted);text-transform:capitalize;">${escHtml(cv.check_type || '—')}</td>
        <td>${statusLabel}</td>
        <td>${cv.count ?? '—'}</td>
        <td style="color:var(--muted);">${cv.ts ? new Date(cv.ts * 1000).toLocaleTimeString() : '—'}</td>
      </tr>`;
    }).join('');
    checksHtml = `
      <div class="setup-card" style="margin-bottom:8px;">
        <div class="setup-card-header"><h2>Check Status</h2></div>
        <table class="data-table">
          <thead><tr><th>Check</th><th>Type</th><th>Status</th><th>Count</th><th>Last Seen</th></tr></thead>
          <tbody>${checkRows}</tbody>
        </table>
      </div>`;
  } else {
    checksHtml = `<div class="setup-card" style="margin-bottom:8px;"><div class="empty-state">No check data available for this site.</div></div>`;
  }

  let spokesHtml = "";
  if ((hubCentralData.mode || "distributed") === "distributed" && site.spokes.length > 1) {
    const spokeRows = site.spokes.map(spoke => `<tr>
      <td>${escHtml(spoke.spoke_name || spoke.spoke_id)}</td>
      <td><span class="status-dot ${spoke.spoke_online ? 'online' : 'offline'}"></span> ${spoke.spoke_online ? 'Online' : 'Offline'}</td>
      <td>${site.token_valid ? '✓ Valid' : '—'}</td>
    </tr>`).join('');
    spokesHtml = `
      <div class="setup-card" style="margin-bottom:8px;">
        <div class="setup-card-header"><h2>Reporting Spokes</h2></div>
        <table class="data-table">
          <thead><tr><th>Spoke</th><th>Status</th><th>Token</th></tr></thead>
          <tbody>${spokeRows}</tbody>
        </table>
      </div>`;
  }

  const summaryHtml = `
    <div class="setup-card" style="margin-bottom:8px;">
      <div class="setup-status-grid">
        <div class="setup-status-item"><span class="setup-status-label">Wireless Clients</span><span class="setup-status-value">${site.wireless_clients}</span></div>
        <div class="setup-status-item"><span class="setup-status-label">Checks Passing</span><span class="setup-status-value" style="color:var(--hpe-green-dark);">${checks.filter(([, value]) => value.status === "OK").length} / ${checks.length}</span></div>
        <div class="setup-status-item"><span class="setup-status-label">Mode</span><span class="setup-status-value" style="text-transform:capitalize;">${escHtml(hubCentralData.mode || '—')}</span></div>
      </div>
    </div>`;

  const detailContent = $("#hcs-detail-content");
  if (detailContent) detailContent.innerHTML = summaryHtml + checksHtml + spokesHtml;

  const backBtn = $("#hcs-detail-back");
  if (backBtn) backBtn.onclick = closeHubSiteDetail;
}

function closeHubSiteDetail() {
  hubCentralSiteOpen = null;
  $("#hcs-site-detail")?.classList.add("hidden");
  $("#hcs-overview")?.classList.remove("hidden");
}

function renderHubCentralAlerts() {
  const container = $("#hcs-alerts-content");
  if (!container || !hubCentralData) return;
  const alerts = hubCentralAggregateHardwareAlerts(hubCentralData);
  if (!alerts.length) {
    container.innerHTML = '<div class="empty-state">No active hardware alerts.</div>';
    return;
  }
  const rows = alerts.map(alert => `
    <tr>
      <td>${escHtml(alert.name || alert.id || '—')}</td>
      <td>${alert.total ?? 0}</td>
      <td>${escHtml(Object.keys(alert.sites || {}).join(', ') || '—')}</td>
    </tr>`).join('');
  container.innerHTML = `
    <div class="setup-card">
      <table class="data-table">
        <thead><tr><th>Alert</th><th>Affected</th><th>Sites</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderHubCentralClients() {
  const container = $("#hcs-clients-content");
  if (!container || !hubCentralData) return;
  const rows = hubCentralAggregateSites(hubCentralData).map(site => `
    <tr>
      <td>${escHtml(site.wsite)}</td>
      <td>${escHtml(site.central_site || '—')}</td>
      <td>${site.wireless_clients ?? '—'}</td>
    </tr>`).join('');
  if (!rows) {
    container.innerHTML = '<div class="empty-state">No client data available.</div>';
    return;
  }
  container.innerHTML = `
    <div class="setup-card">
      <table class="data-table">
        <thead><tr><th>Site</th><th>Central Site</th><th>Wireless Clients</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

async function populateSpokeSelect(selectEl, tenantId, preferredSpokeId = "", includeAll = false) {
  if (!selectEl || !tenantId) return "";
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes`);
  const spokes = (res?.ok ? await res.json() : null) || [];
  selectEl.innerHTML = "";
  if (!spokes.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No spokes available";
    selectEl.appendChild(opt);
    return "";
  }
  if (includeAll) {
    const allOpt = document.createElement("option");
    allOpt.value = "all";
    allOpt.textContent = "All";
    selectEl.appendChild(allOpt);
  }
  spokes.forEach((spoke) => {
    const opt = document.createElement("option");
    opt.value = spoke.id;
    opt.textContent = spoke.spoke_name || spoke.hostname || spoke.id;
    selectEl.appendChild(opt);
  });
  const validValues = new Set(spokes.map((s) => s.id));
  if (includeAll) validValues.add("all");
  const nextValue = validValues.has(preferredSpokeId)
    ? preferredSpokeId
    : (includeAll ? "all" : (spokes[0]?.id || ""));
  selectEl.value = nextValue;
  // Store spoke list on element for bulk push
  selectEl._spokeList = spokes;
  return selectEl.value;
}

async function loadSpokeConfig(tenantId, spokeId) {
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spokeId)}/config`);
  const data = await readJson(res);
  if (!res || !res.ok) throw new Error(data?.detail || "Unable to load spoke config.");
  return data || {};
}

async function pushSpokeConfig(tenantId, spokeId, config) {
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spokeId)}/config`, {
    method: "POST",
    body: config,
  });
  const data = await readJson(res);
  if (!res || !res.ok) throw new Error(data?.detail || "Unable to push spoke config.");
  return data || {};
}

function setHubTenantSetupPanels(subtab = hubTenantSetupActiveSubtab) {
  $$(".hub-ts-subtab").forEach((button) => button.classList.toggle("active", button.dataset.subtab === subtab));
  [
    "ts-setup-panel",
    "ts-central-api-panel",
    "ts-proxmox-panel",
    "ts-github-panel",
    "ts-security-panel",
    "ts-notifications-panel",
    "ts-troubleshoot-panel",
  ].forEach((panelId) => {
    document.getElementById(panelId)?.classList.toggle("hidden", panelId !== `${subtab}-panel`);
  });
}

function ensureSelectHasOption(selectEl, value, label = value) {
  if (!selectEl || !value) return;
  if (![...selectEl.options].some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    selectEl.appendChild(option);
  }
}

function formatTsBool(value, positive = "Yes", negative = "No") {
  if (value === null || value === undefined || value === "") return "—";
  return value ? positive : negative;
}

function setTroubleshootField(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value ?? "—";
}

async function initTsProxmoxTab(tenantId) {
  const saveBtn = $("#ts-proxmox-save-btn");
  const msg = $("#ts-proxmox-msg");
  if (!saveBtn || !tenantId) return;
  saveBtn.disabled = !canManageTenant(tenantId);

  const setFields = (cfg) => {
    const autoProvision = cfg.usb_auto_provision === true || String(cfg.usb_auto_provision || "").toLowerCase() === "on";
    $("#ts-usb-auto-provision") && ($("#ts-usb-auto-provision").checked = autoProvision);
    $("#ts-usb-missing-timeout") && ($("#ts-usb-missing-timeout").value = cfg.usb_missing_timeout ?? 60);
    $("#ts-usb-max-slots") && ($("#ts-usb-max-slots").value = cfg.usb_max_slots ?? 24);
    $("#ts-vm-image-1-template-id") && ($("#ts-vm-image-1-template-id").value = cfg.vm_image_1_template_id ?? 100);
    $("#ts-vm-image-2-template-id") && ($("#ts-vm-image-2-template-id").value = cfg.vm_image_2_template_id ?? 200);
    $("#ts-vm-image-1-pct") && ($("#ts-vm-image-1-pct").value = cfg.vm_image_1_pct ?? 50);
    $("#ts-reclone-concurrency") && ($("#ts-reclone-concurrency").value = cfg.reclone_concurrency ?? 1);
  };

  // Load from hub_config (global settings persisted on the hub)
  try {
    const res = await apiFetch(`/api/tenant/${encodeURIComponent(tenantId)}/hub-config`);
    if (res.ok) {
      const data = await res.json();
      setFields(data.hub_config || {});
    }
  } catch (_) { /* silent — fields stay at HTML defaults */ }

  saveBtn.onclick = async () => {
    const config = {
      usb_auto_provision: $("#ts-usb-auto-provision")?.checked ? "on" : "off",
      usb_missing_timeout: parseInt($("#ts-usb-missing-timeout")?.value || "60", 10) || 60,
      usb_max_slots: parseInt($("#ts-usb-max-slots")?.value || "24", 10) || 24,
      vm_image_1_template_id: parseInt($("#ts-vm-image-1-template-id")?.value || "100", 10) || 100,
      vm_image_2_template_id: parseInt($("#ts-vm-image-2-template-id")?.value || "200", 10) || 200,
      vm_image_1_pct: parseInt($("#ts-vm-image-1-pct")?.value || "50", 10) || 50,
      reclone_concurrency: parseInt($("#ts-reclone-concurrency")?.value || "1", 10) || 1,
    };
    saveBtn.disabled = true;
    try {
      // Merge into existing hub_config so other fields (repo_branch, etc.) are preserved
      const existing = await apiFetch(`/api/tenant/${encodeURIComponent(tenantId)}/hub-config`);
      const existingData = existing.ok ? await existing.json() : {};
      const merged = { ...(existingData.hub_config || {}), ...config };
      const res = await apiFetch(`/api/tenant/${encodeURIComponent(tenantId)}/hub-config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hub_config_enabled: true, hub_config: merged }),
      });
      const data = await res.json();
      if (res.ok) {
        showInlineMessage(msg, `✓ Saved globally. Pushed to ${data.pushed_to_spokes ?? 0} spoke(s).`, false);
      } else {
        showInlineMessage(msg, data.detail || "Save failed.", true);
      }
    } catch (error) {
      showInlineMessage(msg, error.message || "Failed to save Proxmox settings.", true);
    } finally {
      saveBtn.disabled = false;
    }
  };
}

async function initTsGithubTab(tenantId) {
  const select = $("#ts-github-spoke-select");
  const saveBtn = $("#ts-github-save-btn");
  const msg = $("#ts-github-msg");
  const tokenInput = $("#ts-github-token-input");
  if (!select || !saveBtn || !tenantId) return;
  saveBtn.disabled = !canManageTenant(tenantId);
  await populateSpokeSelect(select, tenantId, select.value || "all", true);

  const loadGithub = async () => {
    const spokeId = select.value;
    if (!spokeId) { showInlineMessage(msg, "No spokes available for this tenant.", true); return; }
    if (spokeId === "all") {
      $("#ts-branch-input") && ($("#ts-branch-input").value = "");
      if (tokenInput) { tokenInput.value = ""; resetSecretInput(tokenInput); }
      showInlineMessage(msg, "", false, 0);
      return;
    }
    const data = await loadSpokeConfig(tenantId, spokeId);
    const cfg = data.config || {};
    $("#ts-branch-input") && ($("#ts-branch-input").value = cfg.repo_branch ?? "");
    if (tokenInput) tokenInput.value = "";
    resetSecretInput(tokenInput);
    showInlineMessage(msg, "", false, 0);
  };

  select.onchange = () => { void loadGithub().catch((error) => showInlineMessage(msg, error.message || "Failed to load GitHub settings.", true)); };
  saveBtn.onclick = async () => {
    const spokeId = select.value;
    if (!spokeId) return;
    const payload = { repo_branch: $("#ts-branch-input")?.value.trim() || "main" };
    const tokenSecret = getSecretInputPayload(tokenInput);
    if (tokenSecret.include) payload.github_token = tokenSecret.value.trim();
    try {
      if (spokeId === "all") {
        const spokes = select._spokeList || [];
        if (!spokes.length) { showInlineMessage(msg, "No spokes to push to.", true); return; }
        await Promise.all(spokes.map((s) => pushSpokeConfig(tenantId, s.id, payload)));
        resetSecretInput(tokenInput);
        showInlineMessage(msg, `GitHub settings pushed to ${spokes.length} spoke${spokes.length !== 1 ? "s" : ""} ✓`, false);
      } else {
        await pushSpokeConfig(tenantId, spokeId, payload);
        resetSecretInput(tokenInput);
        showInlineMessage(msg, "GitHub settings pushed ✓", false);
      }
    } catch (error) {
      showInlineMessage(msg, error.message || "Failed to push GitHub settings.", true);
    }
  };

  await loadGithub();
}

async function initTsSecurityTab(tenantId) {
  const select = $("#ts-security-spoke-select");
  const saveBtn = $("#ts-security-save-btn");
  const msg = $("#ts-security-msg");
  const providerSelect = $("#ts-auth-provider");
  if (!select || !saveBtn || !tenantId) return;
  saveBtn.disabled = !canManageTenant(tenantId);
  await populateSpokeSelect(select, tenantId, select.value || "all", true);

  const loadSecurity = async () => {
    const spokeId = select.value;
    if (!spokeId) { showInlineMessage(msg, "No spokes available for this tenant.", true); return; }
    if (spokeId === "all") {
      $("#ts-session-timeout") && ($("#ts-session-timeout").value = 30);
      if (providerSelect) providerSelect.value = "local";
      showInlineMessage(msg, "", false, 0);
      return;
    }
    const data = await loadSpokeConfig(tenantId, spokeId);
    const cfg = data.config || {};
    $("#ts-session-timeout") && ($("#ts-session-timeout").value = cfg.session_timeout_minutes ?? 30);
    const provider = String(cfg.auth_provider || "local").toLowerCase();
    ensureSelectHasOption(providerSelect, provider, provider.toUpperCase());
    if (providerSelect) providerSelect.value = provider;
    showInlineMessage(msg, "", false, 0);
  };

  select.onchange = () => { void loadSecurity().catch((error) => showInlineMessage(msg, error.message || "Failed to load security settings.", true)); };
  saveBtn.onclick = async () => {
    const spokeId = select.value;
    if (!spokeId) return;
    const payload = {
      session_timeout_minutes: parseInt($("#ts-session-timeout")?.value || "30", 10) || 30,
      auth_provider: providerSelect?.value || "local",
    };
    try {
      if (spokeId === "all") {
        const spokes = select._spokeList || [];
        if (!spokes.length) { showInlineMessage(msg, "No spokes to push to.", true); return; }
        await Promise.all(spokes.map((s) => pushSpokeConfig(tenantId, s.id, payload)));
        showInlineMessage(msg, `Security settings pushed to ${spokes.length} spoke${spokes.length !== 1 ? "s" : ""} ✓`, false);
      } else {
        await pushSpokeConfig(tenantId, spokeId, payload);
        showInlineMessage(msg, "Security settings pushed ✓", false);
      }
    } catch (error) {
      showInlineMessage(msg, error.message || "Failed to push security settings.", true);
    }
  };

  await loadSecurity();
}

async function initTsNotificationsTab(tenantId) {
  const select = $("#ts-notifications-spoke-select");
  const saveBtn = $("#ts-notifications-save-btn");
  const msg = $("#ts-notifications-msg");
  const smtpPasswordInput = $("#ts-notif-smtp-password");
  const teamsWebhookInput = $("#ts-notif-teams-webhook");
  if (!select || !saveBtn || !tenantId) return;
  saveBtn.disabled = !canManageTenant(tenantId);
  await populateSpokeSelect(select, tenantId, select.value || "all", true);

  const loadNotifications = async () => {
    const spokeId = select.value;
    if (!spokeId) { showInlineMessage(msg, "No spokes available for this tenant.", true); return; }
    if (spokeId === "all") {
      $("#ts-notif-email-enabled") && ($("#ts-notif-email-enabled").checked = false);
      $("#ts-notif-teams-enabled") && ($("#ts-notif-teams-enabled").checked = false);
      $("#ts-notif-smtp-host") && ($("#ts-notif-smtp-host").value = "");
      $("#ts-notif-smtp-port") && ($("#ts-notif-smtp-port").value = 587);
      $("#ts-notif-smtp-user") && ($("#ts-notif-smtp-user").value = "");
      $("#ts-notif-smtp-from") && ($("#ts-notif-smtp-from").value = "");
      $("#ts-notif-smtp-to") && ($("#ts-notif-smtp-to").value = "");
      if (smtpPasswordInput) { smtpPasswordInput.value = ""; resetSecretInput(smtpPasswordInput); }
      if (teamsWebhookInput) { teamsWebhookInput.value = ""; resetSecretInput(teamsWebhookInput); }
      showInlineMessage(msg, "", false, 0);
      return;
    }
    const data = await loadSpokeConfig(tenantId, spokeId);
    const notif = data.config?.notifications || {};
    $("#ts-notif-email-enabled") && ($("#ts-notif-email-enabled").checked = !!notif.email_enabled);
    $("#ts-notif-teams-enabled") && ($("#ts-notif-teams-enabled").checked = !!notif.teams_enabled);
    $("#ts-notif-smtp-host") && ($("#ts-notif-smtp-host").value = notif.smtp_host || "");
    $("#ts-notif-smtp-port") && ($("#ts-notif-smtp-port").value = notif.smtp_port ?? 587);
    $("#ts-notif-smtp-user") && ($("#ts-notif-smtp-user").value = notif.smtp_user || "");
    $("#ts-notif-smtp-from") && ($("#ts-notif-smtp-from").value = notif.smtp_from || "");
    $("#ts-notif-smtp-to") && ($("#ts-notif-smtp-to").value = Array.isArray(notif.smtp_to) ? notif.smtp_to.join(", ") : (notif.smtp_to || ""));
    if (smtpPasswordInput) {
      smtpPasswordInput.value = "";
      setSecretInputConfigured(smtpPasswordInput, Boolean(notif.smtp_password || notif.smtp_password_configured));
    }
    if (teamsWebhookInput) {
      teamsWebhookInput.value = "";
      setSecretInputConfigured(teamsWebhookInput, Boolean(notif.teams_webhook_url || notif.teams_webhook_url_configured));
    }
    showInlineMessage(msg, "", false, 0);
  };

  const buildNotifPayload = () => {
    const notifications = {
      email_enabled: $("#ts-notif-email-enabled")?.checked ?? false,
      teams_enabled: $("#ts-notif-teams-enabled")?.checked ?? false,
      smtp_host: $("#ts-notif-smtp-host")?.value.trim() || "",
      smtp_port: parseInt($("#ts-notif-smtp-port")?.value || "587", 10) || 587,
      smtp_user: $("#ts-notif-smtp-user")?.value.trim() || "",
      smtp_from: $("#ts-notif-smtp-from")?.value.trim() || "",
      smtp_to: ($("#ts-notif-smtp-to")?.value || "").split(",").map((item) => item.trim()).filter(Boolean),
    };
    const smtpSecret = getSecretInputPayload(smtpPasswordInput);
    if (smtpSecret.include) notifications.smtp_password = smtpSecret.value;
    const teamsSecret = getSecretInputPayload(teamsWebhookInput);
    if (teamsSecret.include) notifications.teams_webhook_url = teamsSecret.value.trim();
    return { notifications };
  };

  select.onchange = () => { void loadNotifications().catch((error) => showInlineMessage(msg, error.message || "Failed to load notification settings.", true)); };
  saveBtn.onclick = async () => {
    const spokeId = select.value;
    if (!spokeId) return;
    try {
      if (spokeId === "all") {
        const spokes = select._spokeList || [];
        if (!spokes.length) { showInlineMessage(msg, "No spokes to push to.", true); return; }
        const payload = buildNotifPayload();
        await Promise.all(spokes.map((s) => pushSpokeConfig(tenantId, s.id, payload)));
        resetSecretInput(smtpPasswordInput);
        resetSecretInput(teamsWebhookInput);
        showInlineMessage(msg, `Notification settings pushed to ${spokes.length} spoke${spokes.length !== 1 ? "s" : ""} ✓`, false);
      } else {
        const current = await loadSpokeConfig(tenantId, spokeId);
        const existing = current.config?.notifications || {};
        const payload = buildNotifPayload();
        payload.notifications = { ...existing, ...payload.notifications };
        await pushSpokeConfig(tenantId, spokeId, payload);
        resetSecretInput(smtpPasswordInput);
        resetSecretInput(teamsWebhookInput);
        showInlineMessage(msg, "Notification settings pushed ✓", false);
      }
    } catch (error) {
      showInlineMessage(msg, error.message || "Failed to push notification settings.", true);
    }
  };

  await loadNotifications();
}

async function initTsTroubleshootTab(tenantId) {
  const select = $("#ts-troubleshoot-spoke-select");
  const updateBtn = $("#ts-troubleshoot-update-btn");
  const clearQueueBtn = $("#ts-troubleshoot-clear-queue-btn");
  const msg = $("#ts-troubleshoot-msg");
  if (!select || !updateBtn || !tenantId) return;
  updateBtn.disabled = !canManageTenant(tenantId);
  if (clearQueueBtn) clearQueueBtn.disabled = !canManageTenant(tenantId);
  await populateSpokeSelect(select, tenantId, select.value || "all", true);

  const clearTroubleshootFields = () => {
    setTroubleshootField("ts-trbl-version", "—");
    setTroubleshootField("ts-trbl-repo-synced", "—");
    setTroubleshootField("ts-trbl-repo-error", "—");
    setTroubleshootField("ts-trbl-installer-version", "—");
  };

  const loadTroubleshoot = async () => {
    const spokeId = select.value;
    if (!spokeId) {
      clearTroubleshootFields();
      showInlineMessage(msg, "No spokes available for this tenant.", true);
      return;
    }
    if (spokeId === "all") {
      clearTroubleshootFields();
      showInlineMessage(msg, "", false, 0);
      return;
    }
    const data = await loadSpokeConfig(tenantId, spokeId);
    const health = data.telemetry?.api_server?.health || {};
    setTroubleshootField("ts-trbl-version", health.version || "—");
    setTroubleshootField("ts-trbl-repo-synced", health.repo_synced != null ? formatTsBool(Boolean(health.repo_synced), "Yes", "No") : "—");
    setTroubleshootField("ts-trbl-repo-error", health.repo_error || "None");
    setTroubleshootField("ts-trbl-installer-version", health.installer_version || "—");
    showInlineMessage(msg, "", false, 0);
  };

  select.onchange = () => { void loadTroubleshoot().catch((error) => showInlineMessage(msg, error.message || "Failed to load troubleshooting data.", true)); };

  updateBtn.onclick = async () => {
    const spokeId = select.value;
    if (!spokeId) return;
    if (spokeId === "all") {
      const spokes = select._spokeList || [];
      if (!spokes.length) { showInlineMessage(msg, "No spokes to update.", true); return; }
      const results = await Promise.all(spokes.map((s) => sendCommandToSpoke(tenantId, s.id, "update_now")));
      const ok = results.every(Boolean);
      showInlineMessage(msg, ok ? `Update queued for ${spokes.length} spoke${spokes.length !== 1 ? "s" : ""} ✓` : "Some spokes failed to queue update.", !ok);
    } else {
      const ok = await sendCommandToSpoke(tenantId, spokeId, "update_now");
      showInlineMessage(msg, ok ? "Update queued for spoke ✓" : "Failed to queue update.", !ok);
    }
  };

  if (clearQueueBtn) {
    clearQueueBtn.onclick = async () => {
      const spokeId = select.value;
      const label = spokeId === "all" ? "all spokes" : (select.options[select.selectedIndex]?.text || "this spoke");
      if (!confirm(`Clear all pending commands for ${label}? This cannot be undone.`)) return;
      try {
        const url = spokeId === "all"
          ? `/api/${tenantId}/commands`
          : `/api/${tenantId}/commands?spoke_id=${encodeURIComponent(spokeId)}`;
        const res = await fetch(url, { method: "DELETE", headers: { Authorization: `Bearer ${getToken()}` } });
        const data = await res.json();
        if (res.ok) {
          showInlineMessage(msg, `Cleared ${data.cleared} command(s) from queue ✓`, false);
        } else {
          showInlineMessage(msg, data.detail || "Failed to clear queue.", true);
        }
      } catch (e) {
        showInlineMessage(msg, e.message || "Failed to clear queue.", true);
      }
    };
  }

  await loadTroubleshoot();
}

async function initHubTenantSetupSubtab(subtab = hubTenantSetupActiveSubtab, force = false) {
  const tenantId = getActiveTenantId();
  if (!tenantId || !currentUser) return;
  if (subtab === "ts-central-api") {
    await loadCentral(force);
    return;
  }
  if (subtab === "ts-proxmox") {
    await initTsProxmoxTab(tenantId);
    return;
  }
  if (subtab === "ts-github") {
    await initTsGithubTab(tenantId);
    return;
  }
  if (subtab === "ts-security") {
    await initTsSecurityTab(tenantId);
    return;
  }
  if (subtab === "ts-notifications") {
    await initTsNotificationsTab(tenantId);
    return;
  }
  if (subtab === "ts-troubleshoot") {
    await initTsTroubleshootTab(tenantId);
  }
}

async function activateHubTenantSetupSubtab(subtab = "ts-setup", force = false) {
  hubTenantSetupActiveSubtab = subtab || "ts-setup";
  setHubTenantSetupPanels(hubTenantSetupActiveSubtab);
  await initHubTenantSetupSubtab(hubTenantSetupActiveSubtab, force);
}

async function loadSetup() {
  await loadHubSettings();
}

async function loadTenantSetup(force = false) {
  const container = $("#hub-tenant-setup-content");
  if (!container) return;
  if (!currentTenantId || !currentUser) {
    container.innerHTML = '<div class="empty-state">Sign in and select a tenant to view setup.</div>';
    return;
  }
  container.innerHTML = '<div class="empty-state">Loading…</div>';
  const data = await loadTenantDetailData(force);
  container.innerHTML = data ? renderTenantSetupPanel(data) : '<div class="empty-state">Unable to load tenant setup.</div>';
  if (data) hydrateTenantSetupPanel(data, container);
  await activateHubTenantSetupSubtab(hubTenantSetupActiveSubtab, force);
}

async function loadConfig(force = false) {
  const container = $("#hub-config-content");
  if (!container) return;
  if (!currentTenantId || !currentUser) {
    container.innerHTML = '<div class="empty-state">Sign in and select a tenant to view config.</div>';
    return;
  }
  container.innerHTML = '<div class="empty-state">Loading…</div>';
  const data = await loadTenantDetailData(force);
  container.innerHTML = data ? renderHubConfigPage(data) : '<div class="empty-state">Unable to load tenant config.</div>';
  renderHubSimulationConfigPanel();
  if (hubConfigActiveSubtab === "simulation") await loadHubSimulationConf(force);
}

async function saveCentralSettings() {
  if (!canManageTenant()) {
    setFormMessage("hub-central-msg", "Tenant Viewer access is read-only.", false);
    return;
  }
  const payload = {
    mode: $("#hub-central-mode")?.value || "distributed",
    hub_central_config: {
      api_version: $("#hub-central-api-version")?.value || "classic",
      cluster_url: $("#hub-central-cluster-url")?.value.trim() || "",
      client_id: $("#hub-central-client-id")?.value.trim() || "",
      client_secret: $("#hub-central-client-secret")?.value || "",
      customer_id: $("#hub-central-customer-id")?.value.trim() || "",
    },
  };
  const res = await apiFetch(aggregateEndpoint("central"), { method: "POST", body: payload });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("hub-central-msg", err?.detail || "Unable to save Central settings.", false);
    return;
  }
  aggregateCentralData = await res.json();
  setFormMessage("hub-central-msg", "Central settings saved.", true);
  await loadCentral(true);
  await loadSetup();
}

async function saveConfigPush() {
  if (!canManageTenant()) {
    setFormMessage("hub-config-msg", "Tenant Viewer access is read-only.", false);
    return;
  }
  const raw = $("#hub-config-payload")?.value || "{}";
  hubConfigDraft = raw;
  let config;
  try {
    config = JSON.parse(raw || "{}");
  } catch (error) {
    setFormMessage("hub-config-msg", `Invalid JSON: ${error.message}`, false);
    return;
  }
  const res = await apiFetch(aggregateEndpoint("config-push"), { method: "POST", body: { config } });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("hub-config-msg", err?.detail || "Unable to push config.", false);
    return;
  }
  setFormMessage("hub-config-msg", "Config stored and queued for spokes.", true);
  tenantDetailState.data[currentTenantId] = null;
  await ensureSpokes(true);
  await loadConfig(true);
}

async function ensureSpokes(force = false) {
  if (!currentTenantId) return [];
  return ensureTenantSpokesFor(currentTenantId, force);
}

function renderClientRows(clients = []) {
  if (!clients.length) {
    return '<tr><td colspan="5" class="empty-state">No client telemetry reported.</td></tr>';
  }
  return clients.map(client => {
    const online = isOnline(client.last_seen);
    const clientId = client.client_id || client.id || client.hostname || "—";
    return `
      <tr>
        <td>${escHtml(clientId)}</td>
        <td>${escHtml(client.hostname || client.client_id || client.id || "—")}</td>
        <td><span class="site-status-pill ${online ? "online" : "offline"}">${online ? "Online" : "Offline"}</span></td>
        <td>${escHtml(relativeTime(client.last_seen))}</td>
        <td>${escHtml(client.ip_address || client.ip || "—")}</td>
      </tr>
    `;
  }).join("");
}

function renderSpokeBody(section, spoke) {
  const body = $(".spoke-section-body", section);
  if (!body) return;
  const clients = spoke.telemetry?.clients || [];
  body.innerHTML = `
    <div class="spoke-section-summary">
      <span class="stat-pill">${clients.length} clients</span>
      <span class="stat-pill">Seen ${escHtml(relativeTime(spoke.last_seen))}</span>
    </div>
    <div class="spoke-action-bar">
      <button class="btn btn-primary btn-small" data-action="detail" type="button">Open Detail</button>
      <button class="btn btn-primary btn-small" data-action="audit" type="button">View Audit Log</button>
      <button class="btn btn-primary btn-small" data-action="mode" type="button">Processing Mode</button>
      <select class="form-input form-input-sm quick-command-select">
        <option value="kill_switch">Kill Switch</option>
        <option value="restart_sim">Restart Simulation</option>
        <option value="reclone">Reclone</option>
        <option value="reboot">Reboot</option>
        <option value="repo_sync">Repo Sync</option>
        <option value="update_now">Update Now</option>
      </select>
      <button class="btn btn-primary btn-small" data-action="send" type="button">Send Command</button>
    </div>
    <table class="data-table spoke-client-table">
      <thead><tr><th>Client ID</th><th>Hostname</th><th>Status</th><th>Last Seen</th><th>IP</th></tr></thead>
      <tbody>${renderClientRows(clients)}</tbody>
    </table>
  `;
  const quickSelect = $(".quick-command-select", body);
  if (!canManageTenant(spoke.tenant_id)) {
    const modeButton = $('[data-action="mode"]', body);
    const sendButton = $('[data-action="send"]', body);
    if (modeButton) modeButton.disabled = true;
    if (quickSelect) quickSelect.disabled = true;
    if (sendButton) sendButton.disabled = true;
  }
  body.addEventListener("click", async event => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (!action) return;
    if (action === "detail") openSpokeModal(spoke, spoke.tenant_id, "spoke-clients");
    if (action === "audit") openSpokeModal(spoke, spoke.tenant_id, "spoke-audit");
    if (action === "mode") openSpokeModal(spoke, spoke.tenant_id, "spoke-mode");
    if (action === "send") await sendCommandToSpoke(spoke.tenant_id, spoke.id, quickSelect?.value || "kill_switch");
  }, { once: true });
}

function createSpokeSection(spoke) {
  const expanded = getExpandedSet().has(spoke.id);
  const online = isOnline(spoke.last_seen);
  const clients = spoke.telemetry?.clients || [];
  const section = document.createElement("section");
  section.className = "spoke-section";
  section.dataset.spokeId = spoke.id;
  section.dataset.tenantId = spoke.tenant_id;
  section.innerHTML = `
    <div class="spoke-section-header">
      <span class="spoke-toggle ${expanded ? "open" : ""}">▶</span>
      ${statusDot(online)}
      <span class="spoke-hostname">${escHtml(spokePrimaryLabel(spoke))}</span>
      ${(() => { const s = spokeSecondaryLabel(spoke, ""); return s ? `<span class="spoke-label-inline">${escHtml(s)}</span>` : ""; })()}
      <span class="spoke-meta">${clients.length} clients · ${escHtml(relativeTime(spoke.last_seen))}</span>
      <button class="btn btn-danger btn-small spoke-delete-btn" data-delete-spoke="${escHtml(spoke.id)}" title="Delete this spoke" type="button">✕ Delete</button>
    </div>
    <div class="spoke-section-body ${expanded ? "expanded" : ""}"></div>
  `;
  $(".spoke-section-header", section)?.addEventListener("click", event => {
    if (event.target.closest("button,select,input,a")) return;
    toggleSpokeSection(section, spoke);
  });
  if (expanded) {
    renderSpokeBody(section, spoke);
    $(".spoke-section-body", section).dataset.rendered = "1";
  }
  return section;
}

function toggleSpokeSection(section, spoke) {
  const expandedSet = getExpandedSet();
  const body = $(".spoke-section-body", section);
  const toggle = $(".spoke-toggle", section);
  const opening = !body.classList.contains("expanded");
  body.classList.toggle("expanded", opening);
  toggle?.classList.toggle("open", opening);
  if (opening) {
    expandedSet.add(spoke.id);
    if (!body.dataset.rendered) {
      renderSpokeBody(section, spoke);
      body.dataset.rendered = "1";
    }
  } else {
    expandedSet.delete(spoke.id);
  }
}

async function loadSpokes(force = false) {
  const spokes = await ensureSpokes(force);
  updateSpokeStatPills(spokes);
  const search = spokeUiState.search.trim().toLowerCase();
  const filtered = spokes.filter(spoke => spoke.status === "approved" && (!search || spokeSearchText(spoke).includes(search)));
  const list = $("#spokes-list");
  const empty = $("#spokes-empty");
  empty?.classList.toggle("hidden", filtered.length > 0);
  if (!list) return;
  if (!filtered.length) {
    list.innerHTML = "";
    return;
  }
  const group = document.createElement("section");
  group.className = "workspace-group setup-card";
  group.innerHTML = `<div class="workspace-header"><h2>${escHtml(tenantName(currentTenantId))}</h2><p>Tenant ID: ${escHtml(currentTenantId)}</p></div><div class="workspace-body"></div>`;
  list.innerHTML = "";
  list.appendChild(group);
  renderInBatches("spokes", $(".workspace-body", group), filtered, spoke => createSpokeSection(spoke), 30);
}

function populateCommandSpokeSelect() {
  const select = $("#cmd-spoke");
  if (!select) return;
  const spokes = getTenantSpokes().filter(spoke => spoke.status === "approved");
  select.innerHTML = spokes.map(spoke => `<option value="${escHtml(spoke.id)}">${escHtml(spokeCommandLabel(spoke))}</option>`).join("");
}

async function sendCommandToSpoke(tenantId, spokeId, type) {
  const response = await apiFetch("/api/commands", {
    method: "POST",
    body: { tenant_id: tenantId, spoke_id: spokeId, type, target: "spoke", payload: {} },
  });
  if (!response || !response.ok) {
    const err = await readJson(response);
    showToast(err?.detail || `Failed to send ${type}.`, "err");
    return false;
  }
  showToast(`${type} queued for ${spokeId}.`, "ok");
  if (activeTab === "commands") loadCommands();
  if (activeSpokeModal?.spoke?.id === spokeId) loadSpokeCommands();
  return true;
}

async function loadCommands() {
  if (!currentTenantId) return;
  await ensureSpokes();
  populateCommandSpokeSelect();
  const res = await apiFetch(`/api/${encodeURIComponent(currentTenantId)}/commands`);
  if (!res || !res.ok) return;
  const commands = await res.json();
  const queued = commands.filter(command => command.status === "queued").length;
  $("#commands-count-pill") && ($("#commands-count-pill").textContent = `${queued} queued`);
  const tbody = $("#commands-tbody");
  if (!tbody) return;
  if (!commands.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No commands queued.</td></tr>';
    return;
  }
  tbody.innerHTML = commands.map(command => {
    const spoke = getTenantSpokes().find(item => item.id === command.spoke_id);
    return `
      <tr>
        <td>${escHtml(spoke ? spokeCommandLabel(spoke) : command.spoke_id)}</td>
        <td>${escHtml(command.type)}</td>
        <td><span class="badge cmd-status-${escHtml(command.status)}">${escHtml(command.status)}</span></td>
        <td>${escHtml(fmtDate(command.created_at))}</td>
        <td>${escHtml(fmtDate(command.expires_at))}</td>
      </tr>
    `;
  }).join("");
}

async function sendCommandFromForm() {
  const spokeId = $("#cmd-spoke")?.value;
  const type = $("#cmd-type")?.value || "kill_switch";
  if (!currentTenantId || !spokeId) {
    setFormMessage("cmd-msg", "Select a spoke first.", false);
    return;
  }
  const ok = await sendCommandToSpoke(currentTenantId, spokeId, type);
  setFormMessage("cmd-msg", ok ? "Command queued." : "Failed to queue command.", ok);
  if (ok) loadCommands();
}

function getSpokeFromCache(tenantId, spokeId) {
  return (spokeCache[tenantId] || []).find(spoke => spoke.id === spokeId) || null;
}

function getActiveSpokeConfigState() {
  if (!activeSpokeModal) return null;
  if (!activeSpokeModal.configState) {
    activeSpokeModal.configState = {
      loaded: false,
      loading: false,
      error: "",
      config: activeSpokeModal.spoke?.config || {},
      telemetry: activeSpokeModal.spoke?.telemetry || {},
    };
  }
  return activeSpokeModal.configState;
}

function findSpokeConfigField(fieldId) {
  return SPOKE_CONFIG_FIELDS.find(field => field.id === fieldId) || null;
}

function normalizeSpokeConfigToggle(value) {
  return ["on", "true", "1", "enabled", "yes"].includes(String(value ?? "").trim().toLowerCase()) ? "on" : "off";
}

function parseSpokeConfigList(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return [];
    try {
      const parsed = JSON.parse(trimmed);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      return [];
    }
  }
  return [];
}

function formatSpokeVidPidList(value) {
  return parseSpokeConfigList(value).map(item => {
    if (item && typeof item === "object") {
      const vidpid = String(item.vidpid || item.id || "").trim();
      const type = String(item.type || "").trim();
      const label = String(item.label || "").trim();
      return [vidpid, type, label].filter(Boolean).join(" ").trim();
    }
    return String(item || "").trim();
  }).filter(Boolean).join("\n");
}

function parseSpokeVidPidListInput(rawValue) {
  return String(rawValue || "")
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
    .map(line => {
      const parts = line.split(/\s+/);
      const vidpid = String(parts.shift() || "").trim();
      if (!vidpid) return null;
      const type = String(parts[0] || "").trim();
      const label = parts.slice(type ? 1 : 0).join(" ").trim();
      const item = { vidpid };
      if (type) item.type = type;
      if (label) item.label = label;
      return item;
    })
    .filter(Boolean);
}

function getSpokeConfigFieldValue(field, sourceConfig = {}, spoke = {}) {
  const keys = field.keys || [field.id];
  for (const key of keys) {
    if (sourceConfig && Object.prototype.hasOwnProperty.call(sourceConfig, key)) {
      return sourceConfig[key];
    }
  }
  if (typeof field.fallback === "function") return field.fallback(spoke);
  return "";
}

function getSpokeConfigDisplayValue(field, sourceConfig = {}, spoke = {}) {
  const raw = getSpokeConfigFieldValue(field, sourceConfig, spoke);
  if (field.type === "toggle") return normalizeSpokeConfigToggle(raw);
  if (field.type === "vidpid-list") return formatSpokeVidPidList(raw);
  if (raw == null) return "";
  return String(raw).trim();
}

function getSpokeConfigSaveKey(field, sourceConfig = {}) {
  for (const key of field.keys || []) {
    if (Object.prototype.hasOwnProperty.call(sourceConfig || {}, key)) return key;
  }
  return field.saveKey || (field.keys && field.keys[0]) || field.id;
}

function ensureSpokeConfigSubtab() {
  const subnav = document.querySelector("#spoke-modal .setup-subnav");
  if (subnav && !subnav.querySelector('[data-subtab="spoke-config"]')) {
    const button = document.createElement("button");
    button.className = "setup-subtab spoke-subtab";
    button.dataset.subtab = "spoke-config";
    button.type = "button";
    button.textContent = "Config";
    const serverTab = subnav.querySelector('[data-subtab="spoke-server"]');
    if (serverTab && serverTab.nextSibling) {
      subnav.insertBefore(button, serverTab.nextSibling);
    } else {
      subnav.appendChild(button);
    }
  }
  const commandsPanel = document.getElementById("spoke-commands");
  if (commandsPanel && !document.getElementById("spoke-config")) {
    const panel = document.createElement("div");
    panel.id = "spoke-config";
    panel.className = "setup-subpanel hidden";
    panel.style.marginTop = "12px";
    commandsPanel.parentNode.insertBefore(panel, commandsPanel);
  }
}

function updateSpokeModalTitle() {
  if (!activeSpokeModal) return;
  const title = document.getElementById("spoke-modal-title");
  if (title) title.textContent = `${spokePrimaryLabel(activeSpokeModal.spoke)} — ${tenantName(activeSpokeModal.tenant_id)}`;
}

function syncActiveSpokeModalFromCache() {
  if (!activeSpokeModal) return null;
  const cached = getSpokeFromCache(activeSpokeModal.tenant_id, activeSpokeModal.spoke?.id);
  if (!cached) return activeSpokeModal.spoke;
  const state = getActiveSpokeConfigState();
  if (state?.loaded) {
    cached.config = state.config || {};
    cached.telemetry = state.telemetry || {};
    if (Object.prototype.hasOwnProperty.call(state.config || {}, "label")) {
      cached.label = state.config.label;
    }
  }
  activeSpokeModal.spoke = cached;
  return cached;
}

async function loadSpokeConfig(force = false) {
  if (!activeSpokeModal) return null;
  ensureSpokeConfigSubtab();
  const state = getActiveSpokeConfigState();
  if (!state) return null;
  if (state.loading && !force) return state;
  if (state.loaded && !force) {
    renderSpokeConfigTab();
    return state;
  }
  state.loading = true;
  state.error = "";
  renderSpokeConfigTab();
  const requestId = ++activeSpokeConfigRequestId;
  activeSpokeModal.configRequestId = requestId;
  const res = await apiFetch(`/api/${encodeURIComponent(activeSpokeModal.tenant_id)}/spokes/${encodeURIComponent(activeSpokeModal.spoke.id)}/config`);
  const data = await readJson(res);
  if (!activeSpokeModal || activeSpokeModal.configRequestId != requestId) return null;
  state.loading = false;
  if (!res || !res.ok) {
    state.loaded = false;
    state.error = data?.detail || "Unable to load spoke config.";
    renderSpokeConfigTab();
    return null;
  }
  state.loaded = true;
  state.error = "";
  state.config = data?.config || {};
  state.telemetry = data?.telemetry || {};
  const cached = getSpokeFromCache(activeSpokeModal.tenant_id, activeSpokeModal.spoke.id);
  if (cached) {
    cached.config = state.config;
    cached.telemetry = state.telemetry;
    if (Object.prototype.hasOwnProperty.call(state.config, "label")) cached.label = state.config.label;
    activeSpokeModal.spoke = cached;
  } else {
    activeSpokeModal.spoke = {
      ...activeSpokeModal.spoke,
      config: state.config,
      telemetry: state.telemetry,
      ...(Object.prototype.hasOwnProperty.call(state.config, "label") ? { label: state.config.label } : {}),
    };
  }
  updateSpokeModalTitle();
  renderSpokeClientsTab();
  renderSpokeServerTab();
  renderSpokeCentralTab();
  renderSpokeStatusTab();
  renderSpokeConfigTab();
  return state;
}

function renderSpokeConfigTab() {
  if (!activeSpokeModal) return;
  ensureSpokeConfigSubtab();
  const panel = document.getElementById("spoke-config");
  if (!panel) return;
  const state = getActiveSpokeConfigState();
  const spoke = syncActiveSpokeModalFromCache() || activeSpokeModal.spoke || {};
  const config = state?.loaded ? (state.config || {}) : (spoke.config || {});
  const telemetry = state?.loaded ? (state.telemetry || {}) : (spoke.telemetry || {});
  const proxmox = telemetry?.proxmox || activeSpokeModal.vmHost?.proxmox || {};
  const online = spoke.last_seen ? isOnline(spoke.last_seen) : Boolean(activeSpokeModal.vmHost?.spoke_online);
  const hostUsbCount = Number(activeSpokeModal.vmHost?.usb_count);
  const telemetryUsbCount = Number(proxmox.usb_count);
  const usbCount = Number.isFinite(hostUsbCount)
    ? hostUsbCount
    : Array.isArray(proxmox.present_usb)
      ? proxmox.present_usb.length
      : Number.isFinite(telemetryUsbCount)
        ? telemetryUsbCount
        : Array.isArray(telemetry?.usb_devices)
          ? telemetry.usb_devices.length
          : Array.isArray(proxmox.usb_state)
            ? proxmox.usb_state.length
            : 0;
  const lastSeen = spoke.last_seen || telemetry?.last_seen || activeSpokeModal.vmHost?.last_seen || null;
  const proxmoxConnected = proxmox.connected == null ? "—" : (proxmox.connected ? "Yes" : "No");
  const agentVersion = proxmox.agent_version || telemetry?.api_server?.health?.version || activeSpokeModal.vmHost?.proxmox?.agent_version || "—";
  const canManage = canManageTenant(activeSpokeModal.tenant_id);

  if (state?.loading && !state.loaded) {
    panel.innerHTML = '<div class="setup-card"><div class="empty-state">Loading current spoke config…</div></div>';
    return;
  }

  panel.innerHTML = `
    ${state?.error ? `<div class="settings-message error" style="margin-bottom:8px;">${escHtml(state.error)}</div>` : ""}
    <div class="setup-card" style="margin-bottom:8px;">
      <div class="setup-card-header"><h3>Telemetry Summary</h3><p>Latest telemetry reported by this spoke.</p></div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;">
        <div class="setup-status-item"><span class="setup-status-label">Online status</span><span class="setup-status-value">${online ? "Online" : "Offline"}</span></div>
        <div class="setup-status-item"><span class="setup-status-label">Last seen</span><span class="setup-status-value">${escHtml(lastSeen ? `${relativeTime(lastSeen)} (${fmtDate(lastSeen)})` : "—")}</span></div>
        <div class="setup-status-item"><span class="setup-status-label">USB count</span><span class="setup-status-value">${escHtml(String(usbCount || 0))}</span></div>
        <div class="setup-status-item"><span class="setup-status-label">Proxmox connected</span><span class="setup-status-value">${escHtml(proxmoxConnected)}</span></div>
        <div class="setup-status-item"><span class="setup-status-label">Agent version</span><span class="setup-status-value">${escHtml(String(agentVersion || "—"))}</span></div>
      </div>
    </div>
    ${SPOKE_CONFIG_FIELD_GROUPS.map(group => `
      <div class="setup-card" style="margin-bottom:8px;">
        <div class="setup-card-header"><h3>${escHtml(group.title)}</h3></div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;align-items:start;">
          ${group.fields.map(field => {
            const inputId = `spoke-config-${field.id}`;
            const value = getSpokeConfigDisplayValue(field, config, spoke);
            if (field.type === "vidpid-list") {
              return `
                <label class="form-group" style="grid-column:1 / -1;margin:0;">
                  <span class="form-label">${escHtml(field.label)}</span>
                  <textarea id="${inputId}" class="form-input" rows="4"${canManage ? "" : " disabled"}>${escHtml(value)}</textarea>
                  <span class="muted" style="font-size:0.82rem;">${escHtml(field.help || "")}</span>
                </label>`;
            }
            if (field.type === "toggle") {
              return `
                <label class="form-group" style="display:flex;align-items:center;gap:8px;margin:0;padding-top:18px;">
                  <input id="${inputId}" type="checkbox"${value === "on" ? " checked" : ""}${canManage ? "" : " disabled"}>
                  <span class="form-label" style="margin:0;">${escHtml(field.label)}</span>
                </label>`;
            }
            return `
              <label class="form-group" style="margin:0;">
                <span class="form-label">${escHtml(field.label)}</span>
                <input id="${inputId}" class="form-input" type="${field.type === "secret" ? "password" : field.type === "number" ? "number" : "text"}" value="${field.type === "secret" ? "" : escHtml(value)}"${canManage ? "" : " disabled"}>
              </label>`;
          }).join("")}
        </div>
      </div>`).join("")}
    <div class="form-actions" style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <button id="spoke-config-save-btn" class="btn btn-primary" type="button"${canManage ? "" : " disabled"}>Save Changes</button>
      <button id="spoke-config-reload-btn" class="btn btn-secondary" type="button">Reload</button>
      <button id="spoke-config-cancel-btn" class="btn btn-secondary" type="button">Cancel</button>
      <span id="spoke-config-msg" class="form-msg">${canManage ? "" : "Read-only"}</span>
    </div>
  `;

  const proxmoxPasswordInput = document.getElementById("spoke-config-proxmox_password");
  bindSecretInput(proxmoxPasswordInput);
  setSecretInputConfigured(proxmoxPasswordInput, Boolean(getSpokeConfigDisplayValue(findSpokeConfigField("proxmox_password"), config, spoke)));

  document.getElementById("spoke-config-save-btn")?.addEventListener("click", () => {
    saveSpokeConfigChanges().catch(err => {
      setFormMessage("spoke-config-msg", err?.message || "Unable to save spoke config.", false);
      showToast(err?.message || "Unable to save spoke config.", "error");
    });
  });
  document.getElementById("spoke-config-reload-btn")?.addEventListener("click", () => {
    loadSpokeConfig(true).catch(err => {
      setFormMessage("spoke-config-msg", err?.message || "Unable to reload spoke config.", false);
    });
  });
  document.getElementById("spoke-config-cancel-btn")?.addEventListener("click", closeSpokeModal);
}

async function saveSpokeConfigChanges() {
  if (!activeSpokeModal || !canManageTenant(activeSpokeModal.tenant_id)) return;
  const state = getActiveSpokeConfigState();
  const sourceConfig = state?.config || activeSpokeModal.spoke?.config || {};
  const payload = {};

  for (const field of SPOKE_CONFIG_FIELDS) {
    const input = document.getElementById(`spoke-config-${field.id}`);
    if (!input) continue;
    const saveKey = getSpokeConfigSaveKey(field, sourceConfig);
    if (field.type === "secret") {
      const secret = getSecretInputPayload(input);
      if (!secret.include) continue;
      const currentValue = String(secret.value ?? "");
      const baseline = String(getSpokeConfigFieldValue(field, sourceConfig, activeSpokeModal.spoke) ?? "");
      if (currentValue === baseline) continue;
      payload[saveKey] = currentValue;
      continue;
    }
    if (field.type === "vidpid-list") {
      const currentList = parseSpokeVidPidListInput(input.value);
      const baselineText = formatSpokeVidPidList(getSpokeConfigFieldValue(field, sourceConfig, activeSpokeModal.spoke));
      const currentText = formatSpokeVidPidList(currentList);
      if (currentText === baselineText) continue;
      payload[saveKey] = currentList;
      continue;
    }
    if (field.type === "toggle") {
      const currentValue = input.checked ? "on" : "off";
      const baseline = normalizeSpokeConfigToggle(getSpokeConfigFieldValue(field, sourceConfig, activeSpokeModal.spoke));
      if (currentValue === baseline) continue;
      payload[saveKey] = currentValue;
      continue;
    }
    const currentValue = String(input.value ?? "").trim();
    const baseline = String(getSpokeConfigFieldValue(field, sourceConfig, activeSpokeModal.spoke) ?? "").trim();
    if (currentValue === baseline) continue;
    payload[saveKey] = currentValue;
  }

  if (!Object.keys(payload).length) {
    setFormMessage("spoke-config-msg", "No changes to save.", true);
    showToast("No spoke config changes to save.", "ok");
    return;
  }

  const saveBtn = document.getElementById("spoke-config-save-btn");
  if (saveBtn) saveBtn.disabled = true;
  setFormMessage("spoke-config-msg", "Saving…", true);
  const res = await apiFetch(`/api/${encodeURIComponent(activeSpokeModal.tenant_id)}/spokes/${encodeURIComponent(activeSpokeModal.spoke.id)}/config`, {
    method: "POST",
    body: payload,
  });
  const data = await readJson(res);
  if (saveBtn) saveBtn.disabled = false;
  if (!res || !res.ok) {
    const message = data?.detail || "Failed to save spoke config.";
    setFormMessage("spoke-config-msg", message, false);
    showToast(message, "error");
    return;
  }

  if (state) {
    state.loaded = true;
    state.error = "";
    state.config = { ...sourceConfig, ...payload };
  }
  const cached = getSpokeFromCache(activeSpokeModal.tenant_id, activeSpokeModal.spoke.id);
  if (cached) {
    cached.config = { ...(cached.config || {}), ...payload };
    if (Object.prototype.hasOwnProperty.call(payload, "label")) cached.label = payload.label;
  }
  activeSpokeModal.spoke = {
    ...activeSpokeModal.spoke,
    config: { ...(activeSpokeModal.spoke?.config || {}), ...payload },
    ...(Object.prototype.hasOwnProperty.call(payload, "label") ? { label: payload.label } : {}),
  };
  updateSpokeModalTitle();
  renderSpokeConfigTab();
  setFormMessage("spoke-config-msg", `Changes queued (config v${data?.config_version || "?"}).`, true);
  showToast("Spoke config update queued.", "ok");
  await loadSpokes(true);
}

function showUpdateProgressModal(tenantId, jobId, spokeCount) {
  const existingOverlay = document.getElementById("update-progress-overlay");
  if (existingOverlay) existingOverlay.remove();

  const overlay = document.createElement("div");
  overlay.id = "update-progress-overlay";
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-box" style="max-width:600px;width:100%;padding:24px 28px;">
      <div class="modal-header" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;">
        <h2 style="margin:0;">⬆️ Update Progress</h2>
        <button id="update-progress-close" class="btn btn-secondary" style="padding:4px 10px;">✕</button>
      </div>
      <div class="muted" style="font-size:0.82rem;margin:6px 0 12px;">
        Proxmox agents updating now &nbsp;·&nbsp; Each spoke restarts automatically once its agent confirms updated &nbsp;·&nbsp; Polling every 15s
      </div>
      <div id="update-progress-body">
        <div class="muted">Waiting for first status check…</div>
      </div>
      <div id="update-progress-footer" style="margin-top:12px;font-size:0.82rem;color:var(--muted-color,#888);"></div>
    </div>`;
  document.body.appendChild(overlay);

  const statusIcon = s => s === "updated" ? "✅" : s === "timeout" ? "❌" : s === "pending" ? "⏳" : "—";

  function renderJob(job) {
    const body = document.getElementById("update-progress-body");
    const footer = document.getElementById("update-progress-footer");
    if (!body) return;
    const spokes = Object.entries(job.spokes || {});
    body.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:0.88rem;">
        <thead>
          <tr style="text-align:left;border-bottom:1px solid var(--border-color,#ddd);">
            <th style="padding:4px 8px;">Spoke</th>
            <th style="padding:4px 8px;">Agent</th>
            <th style="padding:4px 8px;">Spoke</th>
          </tr>
        </thead>
        <tbody>
          ${spokes.map(([id, sd]) => `
            <tr style="border-bottom:1px solid var(--border-color,#eee);">
              <td style="padding:5px 8px;">${escHtml(sd.spoke_name || id)}</td>
              <td style="padding:5px 8px;">
                ${statusIcon(sd.agent_status)}
                ${sd.agent_status === "updated"
                  ? `<span class="muted">${escHtml(sd.agent_version_before||"?")} → <strong>${escHtml(sd.agent_version_after||"?")}</strong></span>`
                  : `<span class="muted">${escHtml(sd.agent_version_before||"unknown")}</span>`}
              </td>
              <td style="padding:5px 8px;">
                ${statusIcon(sd.spoke_status)}
                ${sd.spoke_status === "updated"
                  ? `<span class="muted">${escHtml(sd.spoke_version_before||"?")} → <strong>${escHtml(sd.spoke_version_after||"?")}</strong></span>`
                  : `<span class="muted">${escHtml(sd.spoke_version_before||"unknown")} — waiting…</span>`}
              </td>
            </tr>`).join("")}
        </tbody>
      </table>`;
    if (footer) {
      footer.textContent = job.completed
        ? `Completed at ${job.completed_at || ""}`
        : "Polling every 15s…";
    }
  }

  let pollTimer = null;
  async function poll() {
    try {
      const res = await apiFetch(`/api/${tenantId}/update-status/${jobId}`);
      if (!res.ok) {
        // 404 = hub restarted and lost in-memory job — show a clear message
        if (res.status === 404) {
          const footer = document.getElementById("update-progress-footer");
          if (footer) footer.textContent = "⚠ Hub was restarted — job status unavailable. You can close this.";
        } else {
          pollTimer = setTimeout(poll, 15000);
        }
        return;
      }
      const job = await res.json();
      renderJob(job);
      if (!job.completed) {
        pollTimer = setTimeout(poll, 15000);
      }
    } catch (_) {
      // transient error — retry
      pollTimer = setTimeout(poll, 15000);
    }
  }

  // WS push: use a document-level listener so we always get messages even after WS reconnects
  const wsHandler = evt => {
    try {
      const msg = typeof evt.data === "string" ? JSON.parse(evt.data) : evt.data;
      if (msg.type === "update_job_status" && msg.job_id === jobId) {
        renderJob(msg.job);
        if (msg.job.completed) clearTimeout(pollTimer);
      }
    } catch (_) {}
  };
  // Attach to the live ws; re-attach on reconnect via a delegated document event
  const attachWsHandler = () => {
    if (ws) ws.addEventListener("message", wsHandler);
  };
  attachWsHandler();
  // Track ws reconnects: re-attach if ws changes (checked on each poll cycle via MutationObserver hack-free approach)
  const wsReattachTimer = setInterval(() => {
    if (ws && !ws._updateProgressHandlerAttached) {
      ws._updateProgressHandlerAttached = true;
      ws.addEventListener("message", wsHandler);
    }
  }, 5000);

  function cleanup() {
    clearTimeout(pollTimer);
    clearInterval(wsReattachTimer);
    if (ws) ws.removeEventListener("message", wsHandler);
  }

  document.getElementById("update-progress-close")?.addEventListener("click", () => {
    cleanup();
    overlay.remove();
  });

  poll();
}

async function openSpokeConfigModal(spoke, tenantId, options = {}) {
  if (!spoke || !tenantId) return;
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spoke.id)}/config`);
  const data = await readJson(res);
  if (!res || !res.ok) {
    const message = data?.detail || "Unable to load spoke config.";
    showToast(message, "error");
    return;
  }
  openSpokeModal(spoke, tenantId, "spoke-config", { ...options, configData: data });
}

function renderSpokeClientsTab() {
  const spoke = getSpokeFromCache(activeSpokeModal?.tenant_id, activeSpokeModal?.spoke?.id) || activeSpokeModal?.spoke;
  if (!spoke) return;
  activeSpokeModal.spoke = spoke;
  const tbody = $("#spoke-clients-tbody");
  if (!tbody) return;
  tbody.innerHTML = renderClientRows(spoke.telemetry?.clients || []);
}

async function loadSpokeCommands() {
  if (!activeSpokeModal) return;
  const { tenant_id: tenantId, spoke } = activeSpokeModal;
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/commands?spoke_id=${encodeURIComponent(spoke.id)}`);
  if (!res || !res.ok) return;
  const commands = await res.json();
  const tbody = $("#spoke-cmds-tbody");
  if (!tbody) return;
  const items = commands.slice(0, 20);
  tbody.innerHTML = items.length ? items.map(command => `
    <tr>
      <td>${escHtml(command.type)}</td>
      <td><span class="badge cmd-status-${escHtml(command.status)}">${escHtml(command.status)}</span></td>
      <td>${escHtml(fmtDate(command.created_at))}</td>
      <td>${escHtml(fmtDate(command.expires_at))}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="empty-state">No commands for this spoke.</td></tr>';
}

async function loadSpokeAudit() {
  if (!activeSpokeModal) return;
  const { tenant_id: tenantId, spoke } = activeSpokeModal;
  const res = await apiFetch(`/api/${encodeURIComponent(tenantId)}/spokes/${encodeURIComponent(spoke.id)}/audit`);
  if (!res || !res.ok) return;
  const audit = (await res.json()).slice(-20).reverse();
  const tbody = $("#spoke-audit-tbody");
  if (!tbody) return;
  tbody.innerHTML = audit.length ? audit.map(entry => `
    <tr>
      <td>${escHtml(fmtDate(entry.timestamp))}</td>
      <td>${escHtml(entry.task_type)}</td>
      <td>${escHtml(entry.execution_mode)}</td>
      <td>${escHtml(entry.status)}</td>
      <td>${escHtml(entry.detail || "—")}</td>
    </tr>`).join("") : '<tr><td colspan="5" class="empty-state">No audit entries.</td></tr>';
}

async function loadSpokeProcessingMode() {
  if (!activeSpokeModal) return;
  const saveBtn = $("#mode-save-btn");
  if (saveBtn) saveBtn.disabled = !canManageTenant(activeSpokeModal.tenant_id);
  const res = await apiFetch(`/api/${encodeURIComponent(activeSpokeModal.tenant_id)}/processing-summary`);
  if (!res || !res.ok) return;
  const summary = await res.json();
  const spokeSummary = summary.islands.find(item => item.spoke_id === activeSpokeModal.spoke.id);
  if (!spokeSummary) return;
  $("#mode-global") && ($("#mode-global").value = spokeSummary.global_mode || "centralized");
  const grid = $("#mode-features-grid");
  if (!grid) return;
  grid.innerHTML = PROCESSING_FEATURES.map(feature => `
    <div class="mode-feature-item">
      <label class="mode-feature-label" for="mode-${feature}">${escHtml(feature.replace(/_/g, " "))}</label>
      <select id="mode-${feature}" class="form-input mode-feature-select">
        <option value="inherit">Inherit</option>
        <option value="centralized">Centralized</option>
        <option value="distributed">Distributed</option>
      </select>
    </div>
  `).join("");
  PROCESSING_FEATURES.forEach(feature => {
    const value = spokeSummary.feature_overrides?.[feature];
    const select = $(`#mode-${feature}`);
    if (select) select.value = value || "inherit";
    if (select && !canManageTenant(activeSpokeModal.tenant_id)) select.disabled = true;
  });
  setFormMessage("mode-msg", "", true);
}

async function saveSpokeProcessingMode() {
  if (!activeSpokeModal || !canManageTenant(activeSpokeModal.tenant_id)) return;
  const payload = { global_mode: $("#mode-global")?.value || "centralized" };
  PROCESSING_FEATURES.forEach(feature => {
    const value = $(`#mode-${feature}`)?.value || "inherit";
    payload[feature] = value === "inherit" ? null : value;
  });
  const res = await apiFetch(`/api/${encodeURIComponent(activeSpokeModal.tenant_id)}/spokes/${encodeURIComponent(activeSpokeModal.spoke.id)}/processing-mode`, {
    method: "PATCH",
    body: payload,
  });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("mode-msg", err?.detail || "Failed to save mode.", false);
    return;
  }
  setFormMessage("mode-msg", "Processing mode saved.", true);
  showToast("Processing mode updated.", "ok");
  await loadSpokes(true);
}

function renderSpokeServerTab() {
  if (!activeSpokeModal) return;
  const proxmox = activeSpokeModal.spoke?.telemetry?.proxmox || {};
  const vms = proxmox.vms || [];
  const usbState = proxmox.usb_state || [];

  // Node bar
  $("#spoke-server-hostname") && ($("#spoke-server-hostname").textContent = proxmox.hostname || activeSpokeModal.spoke?.hostname || "—");
  $("#spoke-server-vm-count") && ($("#spoke-server-vm-count").textContent = `${proxmox.vm_count ?? vms.length} VMs`);
  $("#spoke-server-running-count") && ($("#spoke-server-running-count").textContent = `${proxmox.running_count ?? vms.filter(v => v.status === "running").length} running`);
  $("#spoke-server-pve-version") && ($("#spoke-server-pve-version").textContent = proxmox.pve_version ? `PVE ${proxmox.pve_version}` : "");
  $("#spoke-server-agent-version") && ($("#spoke-server-agent-version").textContent = proxmox.agent_version ? `Agent ${proxmox.agent_version}` : "");

  // VMs table
  const vmsTbody = $("#spoke-server-vms-tbody");
  if (vmsTbody) {
    if (!vms.length) {
      vmsTbody.innerHTML = `<tr><td colspan="5" class="empty-msg">No VMs</td></tr>`;
    } else {
      const usbByVmid = {};
      usbState.forEach(u => { if (u.vmid != null) usbByVmid[String(u.vmid)] = u; });
      vmsTbody.innerHTML = vms.map(vm => {
        const statusClass = vm.status === "running" ? "status-online" : "status-offline";
        const usbEntry = usbByVmid[String(vm.vmid)];
        const usbBadge = usbEntry
          ? `<span class="status-badge ${usbEntry.prov_status === "active" ? "status-online" : "status-unknown"}">${usbEntry.prov_status || "—"}</span>`
          : `<span class="status-badge status-offline">none</span>`;
        return `<tr>
          <td>${escapeHtml(String(vm.vmid ?? ""))}</td>
          <td>${escapeHtml(vm.name || "")}</td>
          <td>${escapeHtml(vm.type || "")}</td>
          <td><span class="status-badge ${statusClass}">${escapeHtml(vm.status || "—")}</span></td>
          <td>${usbBadge}</td>
        </tr>`;
      }).join("");
    }
  }

  // USB table
  const usbTbody = $("#spoke-server-usb-tbody");
  if (usbTbody) {
    if (!usbState.length) {
      usbTbody.innerHTML = `<tr><td colspan="4" class="empty-msg">No USB devices</td></tr>`;
    } else {
      usbTbody.innerHTML = usbState.map(u => {
        const ps = String(u.prov_status || "—");
        const psClass = ps === "active" ? "status-online" : ps === "provisioning" ? "status-pending" : "status-offline";
        return `<tr>
          <td>${escapeHtml(String(u.bus || u.bus_path || "—"))}</td>
          <td>${escapeHtml(String(u.vidpid || "—"))}</td>
          <td>${escapeHtml(String(u.vmid ?? "—"))}</td>
          <td><span class="status-badge ${psClass}">${escapeHtml(ps)}</span></td>
        </tr>`;
      }).join("");
    }
  }
}

function renderSpokeCentralTab() {
  if (!activeSpokeModal) return;
  const central = activeSpokeModal.spoke?.telemetry?.central || {};
  const checks = central.status || {};
  const alerts = central.hardware_alerts || [];
  const tokenState = central.token_state || (central.token_valid ? "valid" : "unknown");

  // Token bar
  const tokenBadge = $("#spoke-central-token-state");
  if (tokenBadge) {
    const cls = tokenState === "valid" ? "status-online" : tokenState === "expired" ? "status-offline" : "status-unknown";
    tokenBadge.className = `server-stat-pill ${cls}`;
    tokenBadge.textContent = `Token: ${escapeHtml(tokenState)}`;
  }

  // Checks table
  const checksTbody = $("#spoke-central-checks-tbody");
  if (checksTbody) {
    const checkEntries = Object.entries(checks);
    if (!checkEntries.length) {
      checksTbody.innerHTML = `<tr><td colspan="4" class="empty-msg">No checks</td></tr>`;
    } else {
      checksTbody.innerHTML = checkEntries.map(([name, c]) => {
        const state = String(c.state || "—");
        const stateClass = state === "ok" ? "status-online" : state === "warn" ? "status-pending" : state === "error" ? "status-offline" : "status-unknown";
        const lastCheck = c.last_check ? new Date(c.last_check * 1000).toLocaleTimeString() : "—";
        return `<tr>
          <td>${escapeHtml(name)}</td>
          <td><span class="status-badge ${stateClass}">${escapeHtml(state)}</span></td>
          <td>${escapeHtml(String(c.value ?? "—"))}</td>
          <td>${escapeHtml(lastCheck)}</td>
        </tr>`;
      }).join("");
    }
  }

  // Alerts table
  const alertsTbody = $("#spoke-central-alerts-tbody");
  if (alertsTbody) {
    if (!alerts.length) {
      alertsTbody.innerHTML = `<tr><td colspan="3" class="empty-msg">No alerts</td></tr>`;
    } else {
      alertsTbody.innerHTML = alerts.map(a => {
        const sev = String(a.severity || "info");
        const sevClass = sev === "critical" ? "status-offline" : sev === "warning" ? "status-pending" : "status-unknown";
        return `<tr>
          <td><span class="status-badge ${sevClass}">${escapeHtml(sev)}</span></td>
          <td>${escapeHtml(a.check_type || "—")}</td>
          <td>${escapeHtml(a.message || "—")}</td>
        </tr>`;
      }).join("");
    }
  }
}

function renderSpokeStatusTab() {
  if (!activeSpokeModal) return;
  const api = activeSpokeModal.spoke?.telemetry?.api_server || {};
  const health = api.health || {};
  const services = api.services || {};

  // Info table
  const infoTbody = $("#spoke-status-info-tbody");
  if (infoTbody) {
    const rows = [
      ["Hostname", activeSpokeModal.spoke?.hostname || health.hostname || "—"],
      ["Version", health.version || "—"],
      ["Installer Version", health.installer_version || "—"],
      ["Clients", String(health.clients ?? "—")],
      ["Repo Synced", health.repo_synced != null ? (health.repo_synced ? "Yes" : "No") : "—"],
      ["Repo Error", health.repo_error || "None"],
    ];
    infoTbody.innerHTML = rows.map(([k, v]) =>
      `<tr><td><strong>${escapeHtml(k)}</strong></td><td>${escapeHtml(String(v))}</td></tr>`
    ).join("");
  }

  // Services table
  const servicesTbody = $("#spoke-status-services-tbody");
  if (servicesTbody) {
    const entries = Object.entries(services);
    if (!entries.length) {
      servicesTbody.innerHTML = `<tr><td colspan="4" class="empty-msg">No services</td></tr>`;
    } else {
      servicesTbody.innerHTML = entries.map(([name, svc]) => {
        const status = String(svc.status || "—");
        const statusClass = status === "running" ? "status-online" : status === "stopped" ? "status-offline" : "status-unknown";
        const lastRun = svc.last_run ? new Date(svc.last_run * 1000).toLocaleString() : "—";
        return `<tr>
          <td>${escapeHtml(name)}</td>
          <td><span class="status-badge ${statusClass}">${escapeHtml(status)}</span></td>
          <td>${escapeHtml(String(svc.error_count ?? 0))}</td>
          <td>${escapeHtml(lastRun)}</td>
        </tr>`;
      }).join("");
    }
  }
}

function openSpokeModal(spoke, tenantId, subtab = "spoke-clients", options = {}) {
  ensureSpokeConfigSubtab();
  const configData = options.configData || null;
  const cached = getSpokeFromCache(tenantId, spoke?.id);
  const mergedSpoke = {
    ...(cached || spoke || {}),
    ...(configData?.config ? { config: configData.config } : {}),
    ...(configData?.telemetry ? { telemetry: configData.telemetry } : {}),
    ...(Object.prototype.hasOwnProperty.call(configData?.config || {}, "label") ? { label: configData.config.label } : {}),
  };
  if (cached && configData) {
    cached.config = configData.config || {};
    cached.telemetry = configData.telemetry || {};
    if (Object.prototype.hasOwnProperty.call(configData.config || {}, "label")) cached.label = configData.config.label;
  }
  activeSpokeModal = {
    spoke: mergedSpoke,
    tenant_id: tenantId,
    vmHost: options.vmHost || null,
    configState: {
      loaded: Boolean(configData),
      loading: false,
      error: "",
      config: configData?.config || mergedSpoke.config || {},
      telemetry: configData?.telemetry || mergedSpoke.telemetry || {},
    },
  };
  updateSpokeModalTitle();
  $("#spoke-modal")?.classList.remove("hidden");
  activateSpokeSubtab(subtab);
  renderSpokeClientsTab();
  renderSpokeServerTab();
  renderSpokeCentralTab();
  renderSpokeStatusTab();
  if (subtab === "spoke-config") {
    renderSpokeConfigTab();
    if (!configData) loadSpokeConfig().catch(() => {});
  }
  loadSpokeCommands();
  loadSpokeProcessingMode();
  loadSpokeAudit();
  const canManage = canManageTenant(tenantId);
  [
    '#spoke-clients .spoke-action-bar button',
    '#spoke-clients .spoke-action-bar select',
    '.spoke-subtab[data-subtab="spoke-mode"]',
    '#mode-save-btn',
  ].forEach(selector => {
    $$(selector).forEach(el => {
      el.disabled = !canManage;
      el.classList.toggle('hidden', !canManage && selector.includes('spoke-mode'));
    });
  });
}

function closeSpokeModal() {
  $("#spoke-modal")?.classList.add("hidden");
  activeSpokeConfigRequestId += 1;
  activeSpokeModal = null;
}

function activateSpokeSubtab(subtabId) {
  ensureSpokeConfigSubtab();
  $$(".spoke-subtab").forEach(button => button.classList.toggle("active", button.dataset.subtab === subtabId));
  ["spoke-clients", "spoke-config", "spoke-commands", "spoke-mode", "spoke-audit", "spoke-server", "spoke-central", "spoke-status"].forEach(panelId => {
    document.getElementById(panelId)?.classList.toggle("hidden", panelId !== subtabId);
  });
  if (subtabId === "spoke-config") {
    renderSpokeConfigTab();
    loadSpokeConfig().catch(() => {});
  }
  if (subtabId === "spoke-commands") loadSpokeCommands();
  if (subtabId === "spoke-mode") loadSpokeProcessingMode();
  if (subtabId === "spoke-audit") loadSpokeAudit();
  if (subtabId === "spoke-server") renderSpokeServerTab();
  if (subtabId === "spoke-central") renderSpokeCentralTab();
  if (subtabId === "spoke-status") renderSpokeStatusTab();
}

async function sendSpokeCommand(type) {
  if (!activeSpokeModal || !canManageTenant(activeSpokeModal.tenant_id)) return;
  const ok = await sendCommandToSpoke(activeSpokeModal.tenant_id, activeSpokeModal.spoke.id, type);
  if (ok) {
    loadSpokeCommands();
    loadSpokeAudit();
  }
}
window.sendSpokeCommand = sendSpokeCommand;

async function loadHubSettings() {
  if (!currentTenantId) return;
  syncHubSpokeConfigSetupUI();
  const disabled = !canManageTenant();
  ["notif-save-btn", "acme-request-btn"].forEach(id => { const btn = document.getElementById(id); if (btn) btn.disabled = disabled; });
  // Load tenant admin pending spokes whenever settings tab opens
  if (canManageTenant() && !currentUser?.is_superadmin) loadTenantPendingSpokes();
  if (canManageTenant() && !currentUser?.is_superadmin) loadHubConfig();
  const res = await apiFetch(`/api/${encodeURIComponent(currentTenantId)}/settings`);
  if (!res || !res.ok) return;
  const data = await res.json();
  const notifications = data.notifications || {};
  $("#notif-enabled") && ($("#notif-enabled").checked = Boolean(notifications.enabled));
  setSecretInputConfigured($("#notif-teams"), isConfiguredSecretValue(notifications.teams_webhook_url_configured ?? notifications.teams_webhook_url_set ?? notifications.teams_webhook_url));
  $("#notif-smtp-host") && ($("#notif-smtp-host").value = notifications.smtp_host || "");
  $("#notif-smtp-port") && ($("#notif-smtp-port").value = notifications.smtp_port || 587);
  $("#notif-smtp-user") && ($("#notif-smtp-user").value = notifications.smtp_user || "");
  setSecretInputConfigured($("#notif-smtp-pass"), isConfiguredSecretValue(notifications.smtp_password_configured ?? notifications.smtp_pass_configured ?? notifications.smtp_pass_set ?? notifications.smtp_pass));
  $("#notif-to-emails") && ($("#notif-to-emails").value = (notifications.to_emails || []).join(", "));

  await loadAcmeSettings();
}

async function savePassword() {
  const current_password = $("#pw-current")?.value || "";
  const new_password = $("#pw-new")?.value || "";
  const confirm = $("#pw-confirm")?.value || "";
  if (!current_password || !new_password) {
    setFormMessage("pw-msg", "Enter current and new password.", false);
    return;
  }
  if (new_password !== confirm) {
    setFormMessage("pw-msg", "Passwords do not match.", false);
    return;
  }
  const res = await apiFetch("/api/auth/change-password", { method: "POST", body: { current_password, new_password } });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("pw-msg", err?.detail || "Unable to change password.", false);
    return;
  }
  setFormMessage("pw-msg", "Password updated.", true);
  ["pw-current", "pw-new", "pw-confirm"].forEach(id => { const input = document.getElementById(id); if (input) input.value = ""; });
}

async function saveNotificationSettings() {
  if (!currentTenantId) return;
  const payload = {
    enabled: Boolean($("#notif-enabled")?.checked),
    smtp_host: $("#notif-smtp-host")?.value.trim() || "",
    smtp_port: Number($("#notif-smtp-port")?.value || 587),
    smtp_user: $("#notif-smtp-user")?.value.trim() || "",
    to_emails: $("#notif-to-emails")?.value || "",
  };
  const teamsSecret = getSecretInputPayload($("#notif-teams"));
  if (teamsSecret.include) payload.teams_webhook_url = teamsSecret.value.trim();
  const smtpSecret = getSecretInputPayload($("#notif-smtp-pass"));
  if (smtpSecret.include) payload.smtp_pass = smtpSecret.value;

  const res = await apiFetch(`/api/${encodeURIComponent(currentTenantId)}/settings/notifications`, { method: "POST", body: payload });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("notif-msg", err?.detail || "Unable to save notifications.", false);
    return;
  }
  setFormMessage("notif-msg", "Notifications saved.", true);
  await loadHubSettings();
}

function showKeyBanner(apiKey, spokeId) {
  const banner = $("#sa-key-banner");
  if (!banner) return;
  banner.innerHTML = `
    <strong>⚠ Save this API key — it will not be shown again.</strong>
    <div class="api-key-display">${escHtml(apiKey)}</div>
    <div class="row">
      <span>Spoke ${escHtml(spokeId)} approved.</span>
      <button class="btn btn-secondary btn-small" id="sa-key-dismiss" type="button">Dismiss</button>
    </div>
  `;
  banner.classList.remove("hidden");
  $("#sa-key-dismiss")?.addEventListener("click", () => banner.classList.add("hidden"), { once: true });
}



function acmeBadgeClass(daysRemaining) {
  if (typeof daysRemaining !== "number" || Number.isNaN(daysRemaining)) return "badge-grey";
  if (daysRemaining > 30) return "badge-green";
  if (daysRemaining >= 10) return "badge-yellow";
  return "badge-red";
}

function toggleAcmeDnsSection() {
  const provider = $("#acme-dns-provider")?.value || "cloudflare";
  const allFields = ["cloudflare", "he", "godaddy", "do", "porkbun", "gcloud", "dnsimple", "azure", "route53", "namecheap"];
  allFields.forEach((p) => $("#acme-" + p + "-fields")?.classList.add("hidden"));
  const map = {
    cloudflare: "acme-cloudflare-fields",
    hurricane_electric: "acme-he-fields",
    godaddy: "acme-godaddy-fields",
    digitalocean: "acme-do-fields",
    porkbun: "acme-porkbun-fields",
    gcloud: "acme-gcloud-fields",
    dnsimple: "acme-dnsimple-fields",
    azure_dns: "acme-azure-fields",
    route53: "acme-route53-fields",
    namecheap: "acme-namecheap-fields",
  };
  const target = map[provider];
  if (target) $("#" + target)?.classList.remove("hidden");
}

let hubAcmeSettings = null;
let hubAcmeStatus = {};
let hubAcmePoller = null;
let hubAcmeLogExpanded = false;

function getHubAcmeRequestStatus(status = {}, cfg = {}) {
  if (status?.running) return "running";
  if (status?.status) return String(status.status).toLowerCase();
  if (status?.last_result?.success === true) return "success";
  if (status?.last_result?.success === false) return "failed";
  if (status?.last_error || cfg?.last_error) return "error";
  return "idle";
}

function hubAcmeStatusLabel(statusValue) {
  switch (statusValue) {
    case "running": return "Running";
    case "success": return "Success";
    case "failed": return "Failed";
    case "error": return "Error";
    default: return "Idle";
  }
}

function getHubAcmeLogText(cfg = {}, status = {}) {
  return String(status?.last_log || cfg?.last_log || status?.last_error || cfg?.last_error || "");
}

function updateHubAcmeDisplay() {
  renderAcmeStatus(hubAcmeSettings?.cert_info || {}, hubAcmeSettings || {}, hubAcmeStatus || {});
  renderAcmeLogPanel(hubAcmeSettings || {}, hubAcmeStatus || {});
}

function renderAcmeStatus(certInfo = {}, cfg = {}, status = {}) {
  const container = $("#acme-cert-status");
  if (!container) return;
  const requestStatus = getHubAcmeRequestStatus(status, cfg);
  const requestStatusLabel = hubAcmeStatusLabel(requestStatus);
  const lastLogAt = status?.last_log_at || cfg?.last_log_at || "";
  const lastLogValue = lastLogAt ? escHtml(fmtDate(lastLogAt)) : '<span class="muted">—</span>';
  const lastError = status?.last_error || cfg?.last_error || "";
  const lastErrorValue = lastError ? escHtml(lastError) : '<span class="muted">—</span>';
  if (!certInfo || certInfo.source === "none") {
    container.innerHTML = `
      <div class="setup-status-item"><span class="setup-status-label">Certificate</span><span class="setup-status-value">Not configured</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Challenge</span><span class="setup-status-value">DNS-01</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Authority</span><span class="setup-status-value">${escHtml(cfg.ca || "letsencrypt")}</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Request Status</span><span class="setup-status-value">${escHtml(requestStatusLabel)}</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Log Updated</span><span class="setup-status-value">${lastLogValue}</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Last Error</span><span class="setup-status-value">${lastErrorValue}</span></div>
    `;
    return;
  }
  const days = Number(certInfo.days_remaining ?? 0);
  container.innerHTML = `
    <div class="setup-status-item"><span class="setup-status-label">Domain</span><span class="setup-status-value">${escHtml(certInfo.domain || cfg.domain || "—")}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">Expires</span><span class="setup-status-value">${escHtml(certInfo.expires || "—")} <span class="badge ${acmeBadgeClass(days)}">${Number.isFinite(days) ? `${days} days` : "unknown"}</span></span></div>
    <div class="setup-status-item"><span class="setup-status-label">Issuer</span><span class="setup-status-value">${escHtml(certInfo.issuer || "—")}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">Source</span><span class="setup-status-value">${escHtml(certInfo.source || "—")}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">Request Status</span><span class="setup-status-value">${escHtml(requestStatusLabel)}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">Log Updated</span><span class="setup-status-value">${lastLogValue}</span></div>
    <div class="setup-status-item"><span class="setup-status-label">Last Error</span><span class="setup-status-value">${lastErrorValue}</span></div>
  `;
}

function renderAcmeLogPanel(cfg = {}, status = {}) {
  const panel = $("#acme-log-panel");
  const meta = $("#acme-log-meta");
  const output = $("#acme-log-output");
  const toggleBtn = $("#acme-log-toggle-btn");
  const copyBtn = $("#acme-log-copy-btn");
  if (!panel || !meta || !output || !toggleBtn || !copyBtn) return;
  const requestStatus = getHubAcmeRequestStatus(status, cfg);
  const logText = getHubAcmeLogText(cfg, status);
  const logAt = status?.last_log_at || cfg?.last_log_at || "";
  const hasLog = Boolean(logText);
  const forceExpanded = requestStatus === "failed" || requestStatus === "error";
  if (forceExpanded) hubAcmeLogExpanded = true;
  const expanded = forceExpanded || hubAcmeLogExpanded;
  panel.classList.toggle("hidden", !hasLog && requestStatus === "idle");
  meta.textContent = `${hubAcmeStatusLabel(requestStatus)}${logAt ? ` · Updated ${fmtDate(logAt)}` : ""}`;
  toggleBtn.classList.toggle("hidden", forceExpanded || !hasLog);
  toggleBtn.textContent = expanded ? "Hide Log" : "Show Log";
  toggleBtn.onclick = () => {
    hubAcmeLogExpanded = !expanded;
    renderAcmeLogPanel(cfg, status);
  };
  copyBtn.disabled = !hasLog;
  copyBtn.onclick = async () => {
    if (!hasLog) return;
    try {
      await navigator.clipboard.writeText(logText);
      showToast("ACME log copied to clipboard", "ok");
    } catch (error) {
      console.warn("ACME log copy failed", error);
      showToast("Unable to copy ACME log", "warn");
    }
  };
  output.textContent = hasLog ? logText : "No ACME debug log captured yet.";
  output.classList.toggle("hidden", !expanded && !forceExpanded);
  if ((expanded || forceExpanded) && requestStatus === "running") {
    output.scrollTop = output.scrollHeight;
  }
}

function stopHubAcmeStatusPolling() {
  if (!hubAcmePoller) return;
  clearInterval(hubAcmePoller);
  hubAcmePoller = null;
}

async function pollHubAcmeStatus() {
  try {
    const res = await apiFetch("/api/acme/status");
    if (!res || !res.ok) return;
    hubAcmeStatus = await res.json();
    updateHubAcmeDisplay();
    if (!hubAcmeStatus?.running) stopHubAcmeStatusPolling();
  } catch (error) {
    console.warn("Hub ACME status poll failed", error);
  }
}

function startHubAcmeStatusPolling() {
  stopHubAcmeStatusPolling();
  pollHubAcmeStatus().catch(() => {});
  hubAcmePoller = window.setInterval(() => {
    pollHubAcmeStatus().catch(() => {});
  }, 2000);
}

async function loadAcmeSettings() {
  const [settingsRes, statusRes] = await Promise.all([
    apiFetch("/api/settings/acme"),
    apiFetch("/api/acme/status"),
  ]);
  if (!settingsRes || !settingsRes.ok) return;
  const data = await settingsRes.json();
  const creds = data.dns_credentials || {};
  const configured = data.dns_credentials_configured || {};
  const setValue = (id, value) => {
    const el = $(id);
    if (el) el.value = value || "";
  };
  hubAcmeSettings = data;
  hubAcmeStatus = statusRes?.ok ? await statusRes.json() : {};
  setValue("#acme-domain", data.domain || "");
  setValue("#acme-email", data.email || "");
  setValue("#acme-ca", data.ca || "letsencrypt");
  setValue("#acme-challenge", "dns-01");
  setValue("#acme-dns-provider", data.dns_provider || "cloudflare");
  setValue("#acme-gcloud-sa-json", configured.gcloud_service_account_json || creds.gcloud_service_account_json || "");
  setValue("#acme-gcloud-zone", creds.gcloud_zone_name || "");
  setValue("#acme-dnsimple-account-id", creds.dnsimple_account_id || "");
  setValue("#acme-azure-tenant", creds.azure_tenant_id || "");
  setValue("#acme-azure-client-id", creds.azure_client_id || "");
  setValue("#acme-azure-sub", creds.azure_subscription_id || "");
  setValue("#acme-azure-rg", creds.azure_resource_group || "");
  setValue("#acme-azure-zone", creds.azure_zone_name || "");
  setValue("#acme-r53-key", creds.route53_access_key || "");
  setValue("#acme-r53-zone-id", creds.route53_zone_id || "");
  setValue("#acme-nc-user", creds.namecheap_username || "");
  setValue("#acme-nc-ip", creds.namecheap_client_ip || "");
  $("#acme-enabled") && ($("#acme-enabled").checked = Boolean(data.enabled));
  setSecretInputConfigured($("#acme-cf-token"), isConfiguredSecretValue(data.dns_credentials_configured?.cf_api_token ?? creds.cf_api_token));
  setSecretInputConfigured($("#acme-he-ddns-key"), isConfiguredSecretValue(data.dns_credentials_configured?.he_ddns_key ?? creds.he_ddns_key));
  setSecretInputConfigured($("#acme-godaddy-key"), isConfiguredSecretValue(configured.godaddy_api_key ?? creds.godaddy_api_key));
  setSecretInputConfigured($("#acme-godaddy-secret"), isConfiguredSecretValue(configured.godaddy_api_secret ?? creds.godaddy_api_secret));
  setSecretInputConfigured($("#acme-do-token"), isConfiguredSecretValue(configured.do_token ?? creds.do_token));
  setSecretInputConfigured($("#acme-porkbun-key"), isConfiguredSecretValue(configured.porkbun_api_key ?? creds.porkbun_api_key));
  setSecretInputConfigured($("#acme-porkbun-secret"), isConfiguredSecretValue(configured.porkbun_secret_key ?? creds.porkbun_secret_key));
  setSecretInputConfigured($("#acme-dnsimple-token"), isConfiguredSecretValue(configured.dnsimple_token ?? creds.dnsimple_token));
  setSecretInputConfigured($("#acme-azure-client-secret"), isConfiguredSecretValue(configured.azure_client_secret ?? creds.azure_client_secret));
  setSecretInputConfigured($("#acme-r53-secret"), isConfiguredSecretValue(configured.route53_secret_key ?? creds.route53_secret_key));
  setSecretInputConfigured($("#acme-nc-key"), isConfiguredSecretValue(configured.namecheap_api_key ?? creds.namecheap_api_key));
  toggleAcmeDnsSection();
  updateHubAcmeDisplay();
  if (hubAcmeStatus?.running) startHubAcmeStatusPolling();
  else stopHubAcmeStatusPolling();
}

async function saveAcmeConfig() {
  const payload = {
    enabled: Boolean($("#acme-enabled")?.checked),
    domain: $("#acme-domain")?.value.trim() || "",
    email: $("#acme-email")?.value.trim() || "",
    ca: $("#acme-ca")?.value || "letsencrypt",
    challenge: "dns-01",
    dns_provider: $("#acme-dns-provider")?.value || "",
    dns_credentials: {},
  };
  const addSecret = (key, id) => {
    const secret = getSecretInputPayload($(id));
    if (secret.include) payload.dns_credentials[key] = secret.value;
  };
  const addValue = (key, id) => {
    const value = $(id)?.value.trim();
    if (value) payload.dns_credentials[key] = value;
  };
  addSecret("cf_api_token", "#acme-cf-token");
  addSecret("he_ddns_key", "#acme-he-ddns-key");
  addSecret("godaddy_api_key", "#acme-godaddy-key");
  addSecret("godaddy_api_secret", "#acme-godaddy-secret");
  addSecret("do_token", "#acme-do-token");
  addSecret("porkbun_api_key", "#acme-porkbun-key");
  addSecret("porkbun_secret_key", "#acme-porkbun-secret");
  addValue("gcloud_service_account_json", "#acme-gcloud-sa-json");
  addValue("gcloud_zone_name", "#acme-gcloud-zone");
  addSecret("dnsimple_token", "#acme-dnsimple-token");
  addValue("dnsimple_account_id", "#acme-dnsimple-account-id");
  addValue("azure_tenant_id", "#acme-azure-tenant");
  addValue("azure_client_id", "#acme-azure-client-id");
  addSecret("azure_client_secret", "#acme-azure-client-secret");
  addValue("azure_subscription_id", "#acme-azure-sub");
  addValue("azure_resource_group", "#acme-azure-rg");
  addValue("azure_zone_name", "#acme-azure-zone");
  addValue("route53_access_key", "#acme-r53-key");
  addSecret("route53_secret_key", "#acme-r53-secret");
  addValue("route53_zone_id", "#acme-r53-zone-id");
  addSecret("namecheap_api_key", "#acme-nc-key");
  addValue("namecheap_username", "#acme-nc-user");
  addValue("namecheap_client_ip", "#acme-nc-ip");

  const res = await apiFetch("/api/settings/acme", { method: "POST", body: payload });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("acme-msg", err?.detail || "Unable to save ACME settings.", false);
    return;
  }
  const data = await res.json();
  const creds = data.dns_credentials || {};
  const configured = data.dns_credentials_configured || {};
  hubAcmeSettings = data;
  setFormMessage("acme-msg", "TLS certificate settings saved.", true);
  updateHubAcmeDisplay();
  setSecretInputConfigured($("#acme-cf-token"), isConfiguredSecretValue(data.dns_credentials_configured?.cf_api_token ?? creds.cf_api_token));
  setSecretInputConfigured($("#acme-he-ddns-key"), isConfiguredSecretValue(data.dns_credentials_configured?.he_ddns_key ?? creds.he_ddns_key));
  setSecretInputConfigured($("#acme-godaddy-key"), isConfiguredSecretValue(configured.godaddy_api_key ?? creds.godaddy_api_key));
  setSecretInputConfigured($("#acme-godaddy-secret"), isConfiguredSecretValue(configured.godaddy_api_secret ?? creds.godaddy_api_secret));
  setSecretInputConfigured($("#acme-do-token"), isConfiguredSecretValue(configured.do_token ?? creds.do_token));
  setSecretInputConfigured($("#acme-porkbun-key"), isConfiguredSecretValue(configured.porkbun_api_key ?? creds.porkbun_api_key));
  setSecretInputConfigured($("#acme-porkbun-secret"), isConfiguredSecretValue(configured.porkbun_secret_key ?? creds.porkbun_secret_key));
  setSecretInputConfigured($("#acme-dnsimple-token"), isConfiguredSecretValue(configured.dnsimple_token ?? creds.dnsimple_token));
  setSecretInputConfigured($("#acme-azure-client-secret"), isConfiguredSecretValue(configured.azure_client_secret ?? creds.azure_client_secret));
  setSecretInputConfigured($("#acme-r53-secret"), isConfiguredSecretValue(configured.route53_secret_key ?? creds.route53_secret_key));
  setSecretInputConfigured($("#acme-nc-key"), isConfiguredSecretValue(configured.namecheap_api_key ?? creds.namecheap_api_key));
}

async function requestAcmeCert() {
  const button = $("#acme-request-btn");
  if (button) {
    button.disabled = true;
    button.textContent = "Requesting certificate…";
  }
  hubAcmeLogExpanded = true;
  hubAcmeStatus = {
    ...(hubAcmeStatus || {}),
    running: true,
    status: "running",
    last_result: null,
    last_error: null,
    last_log: "",
    last_log_at: "",
  };
  updateHubAcmeDisplay();
  startHubAcmeStatusPolling();
  setFormMessage("acme-msg", "Requesting certificate… (this may take 60-90 seconds)", true);
  try {
    const res = await apiFetch("/api/settings/acme/request", { method: "POST" });
    const data = await readJson(res);
    await loadAcmeSettings();
    if (!res || !res.ok || !data?.success) {
      setFormMessage("acme-msg", data?.error || data?.detail || "Certificate request failed.", false);
      return;
    }
    setFormMessage("acme-msg", `Certificate issued for ${data.domain} — expires ${data.expires || "unknown"}.`, true);
  } catch (error) {
    await loadAcmeSettings().catch(() => {});
    setFormMessage("acme-msg", error.message || "Certificate request failed.", false);
  } finally {
    stopHubAcmeStatusPolling();
    if (button) {
      button.disabled = !canManageTenant();
      button.textContent = "Request Certificate Now";
    }
  }
}

window.saveAcmeConfig = saveAcmeConfig;
window.requestAcmeCert = requestAcmeCert;

function normalizeHubAuthProvider(provider) {
  return ["local", "ldap", "radius", "tacacs"].includes(provider) ? provider : "local";
}

function normalizeHubAuthDefaultRole(role) {
  return role === "tenant_admin" ? "tenant_admin" : "superadmin";
}

function normalizeHubAuthConfig(data = {}) {
  return {
    ...HUB_AUTH_DEFAULTS,
    ...data,
    auth_provider: normalizeHubAuthProvider(String(data.auth_provider || HUB_AUTH_DEFAULTS.auth_provider).trim().toLowerCase()),
    auth_radius_port: Number.parseInt(data.auth_radius_port ?? HUB_AUTH_DEFAULTS.auth_radius_port, 10) || HUB_AUTH_DEFAULTS.auth_radius_port,
    auth_tacacs_port: Number.parseInt(data.auth_tacacs_port ?? HUB_AUTH_DEFAULTS.auth_tacacs_port, 10) || HUB_AUTH_DEFAULTS.auth_tacacs_port,
    auth_tacacs_superadmin_priv: Number.parseInt(data.auth_tacacs_superadmin_priv ?? HUB_AUTH_DEFAULTS.auth_tacacs_superadmin_priv, 10) || HUB_AUTH_DEFAULTS.auth_tacacs_superadmin_priv,
    auth_default_role: normalizeHubAuthDefaultRole(String(data.auth_default_role || HUB_AUTH_DEFAULTS.auth_default_role).trim().toLowerCase()),
  };
}

function renderHubAuthTenantOptions(selectedTenantId = "") {
  const select = hubAuthEl("sa-auth-tenant-id");
  if (!select) return;
  const options = ['<option value="">Select tenant</option>'];
  tenants.forEach((tenant) => {
    const selected = tenant.id === selectedTenantId ? ' selected' : '';
    options.push(`<option value="${escHtml(tenant.id)}"${selected}>${escHtml(tenant.name || tenant.id)}</option>`);
  });
  select.innerHTML = options.join("");
}

function updateHubAuthProviderVisibility(provider = hubAuthConfig.auth_provider || "local") {
  const nextProvider = normalizeHubAuthProvider(String(provider || "local").trim().toLowerCase());
  const providerSelect = hubAuthEl("sa-auth-provider");
  if (providerSelect && !providerSelect.matches(":focus")) providerSelect.value = nextProvider;
  hubAuthEl("sa-auth-ldap-fields")?.classList.toggle("hidden", nextProvider !== "ldap");
  hubAuthEl("sa-auth-radius-fields")?.classList.toggle("hidden", nextProvider !== "radius");
  hubAuthEl("sa-auth-tacacs-fields")?.classList.toggle("hidden", nextProvider !== "tacacs");
}

function applyHubAuthConfigToUi(config = hubAuthConfig) {
  const next = normalizeHubAuthConfig(config);
  hubAuthConfig = next;
  renderHubAuthTenantOptions(next.auth_ldap_tenant_id || "");
  setInputValueIfIdle(hubAuthEl("sa-auth-ldap-url"), next.auth_ldap_url || "");
  setInputValueIfIdle(hubAuthEl("sa-auth-ldap-bind-dn"), next.auth_ldap_bind_dn || "");
  setSecretInputConfigured(hubAuthEl("sa-auth-ldap-bind-password"), next.auth_ldap_bind_password_configured);
  setInputValueIfIdle(hubAuthEl("sa-auth-ldap-user-base"), next.auth_ldap_user_base || "");
  setInputValueIfIdle(hubAuthEl("sa-auth-ldap-user-filter"), next.auth_ldap_user_filter || HUB_AUTH_DEFAULTS.auth_ldap_user_filter);
  setInputValueIfIdle(hubAuthEl("sa-auth-ldap-group-superadmin"), next.auth_ldap_group_superadmin || "");
  setInputValueIfIdle(hubAuthEl("sa-auth-ldap-group-tenant-admin"), next.auth_ldap_group_tenant_admin || "");
  setInputValueIfIdle(hubAuthEl("sa-auth-radius-host"), next.auth_radius_host || "");
  setInputValueIfIdle(hubAuthEl("sa-auth-radius-role-attr"), next.auth_radius_role_attr || HUB_AUTH_DEFAULTS.auth_radius_role_attr);
  setInputValueIfIdle(hubAuthEl("sa-auth-radius-superadmin-val"), next.auth_radius_superadmin_val || HUB_AUTH_DEFAULTS.auth_radius_superadmin_val);
  setSecretInputConfigured(hubAuthEl("sa-auth-radius-secret"), next.auth_radius_secret_configured);
  setInputValueIfIdle(hubAuthEl("sa-auth-tacacs-host"), next.auth_tacacs_host || "");
  setSecretInputConfigured(hubAuthEl("sa-auth-tacacs-secret"), next.auth_tacacs_secret_configured);
  const radiusPort = hubAuthEl("sa-auth-radius-port");
  if (radiusPort && !radiusPort.matches(":focus")) radiusPort.value = next.auth_radius_port ?? HUB_AUTH_DEFAULTS.auth_radius_port;
  const tacacsPort = hubAuthEl("sa-auth-tacacs-port");
  if (tacacsPort && !tacacsPort.matches(":focus")) tacacsPort.value = next.auth_tacacs_port ?? HUB_AUTH_DEFAULTS.auth_tacacs_port;
  const tacacsPriv = hubAuthEl("sa-auth-tacacs-superadmin-priv");
  if (tacacsPriv && !tacacsPriv.matches(":focus")) tacacsPriv.value = next.auth_tacacs_superadmin_priv ?? HUB_AUTH_DEFAULTS.auth_tacacs_superadmin_priv;
  const defaultSuperadmin = hubAuthEl("sa-auth-default-role-superadmin");
  const defaultTenantAdmin = hubAuthEl("sa-auth-default-role-tenant-admin");
  if (defaultSuperadmin) defaultSuperadmin.checked = next.auth_default_role !== "tenant_admin";
  if (defaultTenantAdmin) defaultTenantAdmin.checked = next.auth_default_role === "tenant_admin";
  updateHubAuthProviderVisibility(next.auth_provider);
}

function getHubAuthDefaultRole() {
  return hubAuthEl("sa-auth-default-role-tenant-admin")?.checked ? "tenant_admin" : "superadmin";
}

async function loadHubAuthConfig(force = false) {
  if (!currentUser?.is_superadmin) return null;
  if (hubAuthConfigLoaded && !force) {
    applyHubAuthConfigToUi(hubAuthConfig);
    return hubAuthConfig;
  }
  const response = await apiFetch("/api/superadmin/auth-config");
  const data = await readJson(response);
  if (!response || !response.ok) {
    showInlineMessage(hubAuthEl("sa-auth-msg"), data?.detail || "Failed to load auth configuration.", true, 7000);
    return null;
  }
  hubAuthConfig = normalizeHubAuthConfig(data || {});
  hubAuthConfigLoaded = true;
  applyHubAuthConfigToUi(hubAuthConfig);
  return hubAuthConfig;
}

async function saveHubAuthConfig() {
  if (!currentUser?.is_superadmin) return;
  const provider = normalizeHubAuthProvider(String(hubAuthEl("sa-auth-provider")?.value || hubAuthConfig.auth_provider || "local").trim().toLowerCase());
  const payload = {
    auth_provider: provider,
    auth_ldap_url: hubAuthEl("sa-auth-ldap-url")?.value?.trim() || "",
    auth_ldap_bind_dn: hubAuthEl("sa-auth-ldap-bind-dn")?.value?.trim() || "",
    auth_ldap_user_base: hubAuthEl("sa-auth-ldap-user-base")?.value?.trim() || "",
    auth_ldap_user_filter: hubAuthEl("sa-auth-ldap-user-filter")?.value?.trim() || HUB_AUTH_DEFAULTS.auth_ldap_user_filter,
    auth_ldap_group_superadmin: hubAuthEl("sa-auth-ldap-group-superadmin")?.value?.trim() || "",
    auth_ldap_group_tenant_admin: hubAuthEl("sa-auth-ldap-group-tenant-admin")?.value?.trim() || "",
    auth_ldap_tenant_id: hubAuthEl("sa-auth-tenant-id")?.value || "",
    auth_radius_host: hubAuthEl("sa-auth-radius-host")?.value?.trim() || "",
    auth_radius_port: Number.parseInt(hubAuthEl("sa-auth-radius-port")?.value || HUB_AUTH_DEFAULTS.auth_radius_port, 10) || HUB_AUTH_DEFAULTS.auth_radius_port,
    auth_radius_role_attr: hubAuthEl("sa-auth-radius-role-attr")?.value?.trim() || HUB_AUTH_DEFAULTS.auth_radius_role_attr,
    auth_radius_superadmin_val: hubAuthEl("sa-auth-radius-superadmin-val")?.value?.trim() || HUB_AUTH_DEFAULTS.auth_radius_superadmin_val,
    auth_tacacs_host: hubAuthEl("sa-auth-tacacs-host")?.value?.trim() || "",
    auth_tacacs_port: Number.parseInt(hubAuthEl("sa-auth-tacacs-port")?.value || HUB_AUTH_DEFAULTS.auth_tacacs_port, 10) || HUB_AUTH_DEFAULTS.auth_tacacs_port,
    auth_tacacs_superadmin_priv: Number.parseInt(hubAuthEl("sa-auth-tacacs-superadmin-priv")?.value || HUB_AUTH_DEFAULTS.auth_tacacs_superadmin_priv, 10) || HUB_AUTH_DEFAULTS.auth_tacacs_superadmin_priv,
    auth_default_role: getHubAuthDefaultRole(),
  };
  const ldapSecret = getSecretInputPayload(hubAuthEl("sa-auth-ldap-bind-password"));
  if (ldapSecret.include) payload.auth_ldap_bind_password = ldapSecret.value;
  const radiusSecret = getSecretInputPayload(hubAuthEl("sa-auth-radius-secret"));
  if (radiusSecret.include) payload.auth_radius_secret = radiusSecret.value;
  const tacacsSecret = getSecretInputPayload(hubAuthEl("sa-auth-tacacs-secret"));
  if (tacacsSecret.include) payload.auth_tacacs_secret = tacacsSecret.value;

  showInlineMessage(hubAuthEl("sa-auth-msg"), "Saving authentication settings…", false, 0);
  const response = await apiFetch("/api/superadmin/auth-config", { method: "POST", body: payload });
  const data = await readJson(response);
  if (!response || !response.ok) {
    showInlineMessage(hubAuthEl("sa-auth-msg"), data?.detail || "Failed to save auth configuration.", true, 7000);
    return;
  }
  resetSecretInput(hubAuthEl("sa-auth-ldap-bind-password"));
  resetSecretInput(hubAuthEl("sa-auth-radius-secret"));
  resetSecretInput(hubAuthEl("sa-auth-tacacs-secret"));
  hubAuthConfigLoaded = false;
  await loadHubAuthConfig(true).catch(() => {});
  showInlineMessage(hubAuthEl("sa-auth-msg"), "Authentication settings saved.", false, 5000);
}

async function testHubAuthConnection() {
  if (!currentUser?.is_superadmin) return;
  const provider = normalizeHubAuthProvider(String(hubAuthEl("sa-auth-provider")?.value || hubAuthConfig.auth_provider || "local").trim().toLowerCase());
  const response = await apiFetch("/api/superadmin/auth-test", {
    method: "POST",
    body: {
      provider,
      username: hubAuthEl("sa-auth-test-username")?.value?.trim() || "",
      password: hubAuthEl("sa-auth-test-password")?.value || "",
    },
  });
  const data = await readJson(response);
  if (!response || !response.ok) {
    showInlineMessage(hubAuthEl("sa-auth-msg"), data?.detail || "Failed to test auth provider.", true, 7000);
    return;
  }
  showInlineMessage(hubAuthEl("sa-auth-msg"), data?.message || (data?.ok ? "Connection OK." : "Connection failed."), !data?.ok, 7000);
}

async function loadSuperadmin() {
  if (!currentUser?.is_superadmin) return;
  const [tenantsRes, pendingRes, usersRes, authRes] = await Promise.all([
    apiFetch("/api/superadmin/tenants"),
    apiFetch("/api/superadmin/pending-spokes"),
    apiFetch("/api/superadmin/users"),
    apiFetch("/api/superadmin/auth-config"),
  ]);
  let tenantData = [];
  if (tenantsRes?.ok) {
    tenantData = await tenantsRes.json();
    tenants = tenantData.map(item => ({ id: item.id, name: item.name || item.id, raw: item }));
    await Promise.all(tenantData.map(item => ensureTenantSpokesFor(item.id, true)));
    buildTenantSelector();
    buildSuperadminTenantTabs();
  }
  if (usersRes?.ok) {
    const users = await usersRes.json();
    tenantUserCounts = buildTenantUserCounts(users);
    renderSuperadminUsers(users);
  }
  if (tenantsRes?.ok) renderSuperadminTenants(tenantData);
  if (pendingRes?.ok) renderPendingSpokes(await pendingRes.json());
  if (authRes?.ok) {
    hubAuthConfig = normalizeHubAuthConfig(await authRes.json());
    hubAuthConfigLoaded = true;
    applyHubAuthConfigToUi(hubAuthConfig);
  } else {
    hubAuthConfigLoaded = false;
  }
  loadGkillState();
}

function renderPendingSpokes(items) {
  $("#sa-pending-count") && ($("#sa-pending-count").textContent = String(items.length));
  const tbody = $("#sa-pending-tbody");
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No pending spokes.</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(item => `
    <tr>
      <td><strong>${escHtml(item.spoke_name || item.hostname)}</strong></td>
      <td><code>${escHtml(item.hostname)}</code></td>
      <td>${item.tenant_hint
        ? `<span class="role-badge" style="background:var(--hpe-green,#01a982)">${escHtml(tenantName(item.tenant_hint))}</span>`
        : '<span class="muted">—</span>'}</td>
      <td>${escHtml(fmtDate(item.registered_at))}</td>
      <td>
        <select class="form-input form-input-sm sa-tenant-assign" data-pending-id="${escHtml(item.id)}">
          ${tenants.map(tenant => `<option value="${escHtml(tenant.id)}"${item.tenant_hint === tenant.id ? ' selected' : ''}>${escHtml(tenant.name)}</option>`).join("")}
        </select>
      </td>
      <td>
        <button class="btn btn-primary btn-small" data-approve-id="${escHtml(item.id)}" type="button">Approve</button>
        <button class="btn btn-danger btn-small" data-reject-id="${escHtml(item.id)}" type="button">Reject</button>
      </td>
    </tr>
  `).join("");
}

// ── Hub as Source of Truth config ────────────────────────────────────────────
const HUB_CONFIG_FIELDS = [
  "repo_branch","reclone_schedule_enabled","reclone_schedule_cron","reclone_concurrency",
  "vm_image_1_template_id","vm_image_2_template_id","vm_image_1_pct",
  "usb_auto_provision","usb_missing_timeout","usb_max_slots","vm_silent_timeout",
  "l1_vlan_start","l1_vlan_end","usb_vidpids","ignored_hostnames",
];

function syncHubSpokeConfigSetupUI() {
  const shouldHide = !currentTenantId || !canManageTenant() || Boolean(currentUser?.is_superadmin);
  const tabBtn = document.getElementById("settings-spoke-config-tab-btn");
  const panel = document.getElementById("settings-spoke-config");
  if (tabBtn) tabBtn.style.display = shouldHide ? "none" : "";
  if (!shouldHide) return true;
  const spokeConfigSelected = Boolean(tabBtn?.classList.contains("active") || (panel && !panel.classList.contains("hidden")));
  tabBtn?.classList.remove("active");
  panel?.classList.add("hidden");
  if (spokeConfigSelected) {
    $$(".settings-subtab").forEach(button => button.classList.toggle("active", button.dataset.subtab === "settings-account"));
    ["settings-account", "settings-notifications", "settings-tls", "settings-pending-spokes", "settings-spoke-config"].forEach(panelId => {
      document.getElementById(panelId)?.classList.toggle("hidden", panelId !== "settings-account");
    });
  }
  return false;
}

async function loadHubConfig() {
  if (!syncHubSpokeConfigSetupUI()) return;
  // Pre-seed toggle from cached tenant data (instant, no flicker)
  const cached = tenants.find(t => t.id === currentTenantId);
  if (cached?.raw?.hub_config_enabled !== undefined) {
    const toggle = document.getElementById("hub-config-enabled-toggle");
    if (toggle) toggle.checked = Boolean(cached.raw.hub_config_enabled);
    document.getElementById("hub-config-fields")?.classList.toggle("hidden", !cached.raw.hub_config_enabled);
  }
  try {
    const res = await fetch(`/api/tenant/${currentTenantId}/hub-config`,
      { headers: { Authorization: `Bearer ${authToken}` } });
    if (!res.ok) return;
    const data = await res.json();
    const toggle = document.getElementById("hub-config-enabled-toggle");
    if (toggle) toggle.checked = Boolean(data.hub_config_enabled);
    const fields = document.getElementById("hub-config-fields");
    if (fields) fields.classList.toggle("hidden", !data.hub_config_enabled);
    const cfg = data.hub_config || {};
    HUB_CONFIG_FIELDS.forEach(key => {
      const el = document.getElementById(`hc-${key}`);
      if (el && cfg[key] !== undefined) el.value = typeof cfg[key] === "object" ? JSON.stringify(cfg[key]) : cfg[key];
    });
  } catch (_) { /* silent */ }
}

async function saveHubConfig() {
  if (!syncHubSpokeConfigSetupUI()) return;
  const toggle = document.getElementById("hub-config-enabled-toggle");
  const enabled = toggle?.checked ?? false;
  const config = {};
  HUB_CONFIG_FIELDS.forEach(key => {
    const el = document.getElementById(`hc-${key}`);
    if (!el) return;
    const v = el.value.trim();
    if (!v) return;
    if (key === "usb_vidpids" || key === "ignored_hostnames") {
      try { config[key] = JSON.parse(v); } catch { config[key] = v; }
    } else {
      config[key] = v;
    }
  });
  const statusEl = document.getElementById("hub-config-save-status");
  if (statusEl) statusEl.textContent = "Saving…";
  try {
    const res = await fetch(`/api/tenant/${currentTenantId}/hub-config`, {
      method: "PUT",
      headers: { Authorization: `Bearer ${authToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ hub_config_enabled: enabled, hub_config: config }),
    });
    const data = await res.json();
    if (res.ok) {
      if (statusEl) statusEl.textContent = `✅ Saved. Pushed to ${data.pushed_to_spokes} spoke(s).`;
      setTimeout(() => { if (statusEl) statusEl.textContent = ""; }, 4000);
    } else {
      if (statusEl) statusEl.textContent = `❌ ${data.detail || "Save failed"}`;
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `❌ ${e.message}`;
  }
}

async function saveProcessingMode(tenantId, feature, value) {
  const msg = document.getElementById('processing-modes-msg');
  if (!tenantId || !feature) return;
  if (msg) msg.textContent = 'Saving…';
  try {
    const response = await apiFetch(`/api/hub/tenants/${encodeURIComponent(tenantId)}/processing-modes`, {
      method: 'PATCH',
      body: { [feature]: value },
    });
    const data = await readJson(response);
    if (!response?.ok) throw new Error(data?.detail || 'Save failed');
    if (tenantDetailState.data[tenantId]?.settings) {
      tenantDetailState.data[tenantId].settings.processing_modes = data?.processing_modes || tenantDetailState.data[tenantId].settings.processing_modes;
    }
    if (msg) msg.textContent = `Saved. Pushed to ${data?.pushed_to_spokes ?? 0} spoke(s).`;
    setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
  } catch (error) {
    if (msg) msg.textContent = `Error: ${error.message}`;
  }
}
window.saveProcessingMode = saveProcessingMode;

// ── Tenant admin pending spokes ──────────────────────────────────────────────
async function loadTenantPendingSpokes() {
  if (!currentTenantId || !canManageTenant()) return;
  try {
    const res = await fetch(`/api/tenant/${currentTenantId}/pending-spokes`,
      { headers: { Authorization: `Bearer ${authToken}` } });
    if (!res.ok) return;
    const items = await res.json();
    renderTenantPendingSpokes(items);
  } catch (_) { /* silent */ }
}

function renderTenantPendingSpokes(items) {
  const btn = document.getElementById("settings-pending-tab-btn");
  const countEl = document.getElementById("settings-pending-count");
  if (btn) btn.style.display = items.length > 0 ? "" : "none";
  if (countEl) countEl.textContent = String(items.length);
  const tbody = document.getElementById("settings-pending-tbody");
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No pending spokes for this tenant.</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(item => `
    <tr>
      <td><strong>${escHtml(item.spoke_name || item.hostname)}</strong></td>
      <td><code>${escHtml(item.hostname)}</code></td>
      <td>${escHtml(fmtDate(item.registered_at))}</td>
      <td>
        <button class="btn btn-primary btn-small" data-tenant-approve-id="${escHtml(item.id)}" type="button">Approve</button>
        <button class="btn btn-danger btn-small" data-tenant-reject-id="${escHtml(item.id)}" type="button">Reject</button>
      </td>
    </tr>
  `).join("");
  // wire approve/reject
  tbody.querySelectorAll("[data-tenant-approve-id]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.tenantApproveId;
      const res = await fetch(`/api/tenant/${currentTenantId}/pending-spokes/${id}/approve`,
        { method: "POST", headers: { Authorization: `Bearer ${authToken}` } });
      if (!res.ok) { alert("Approval failed: " + (await res.text())); return; }
      const data = await res.json();
      const banner = document.getElementById("settings-pending-key-banner");
      if (banner && data.api_key) {
        banner.textContent = `✅ Spoke approved. API Key (shown once): ${data.api_key}`;
        banner.classList.remove("hidden");
        setTimeout(() => banner.classList.add("hidden"), 30000);
      }
      showToast("Spoke approved.", "ok");
      await refreshAfterSpokeApproval(currentTenantId);
    });
  });
  tbody.querySelectorAll("[data-tenant-reject-id]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.tenantRejectId;
      await fetch(`/api/tenant/${currentTenantId}/pending-spokes/${id}`,
        { method: "DELETE", headers: { Authorization: `Bearer ${authToken}` } });
      loadTenantPendingSpokes();
    });
  });
}



function renderSuperadminTenants(items) {
  $("#sa-tenants-count") && ($("#sa-tenants-count").textContent = String(items.length));
  const tbody = $("#sa-tenants-tbody");
  if (!tbody) return;
  tbody.innerHTML = items.length ? [...items].sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id), undefined, { numeric: true, sensitivity: "base" })).map(item => {
    const summary = summarizeTenantSpokes(spokeCache[item.id] || []);
    const userCount = getTenantUserCount(item.id);
    return `
      <tr>
        <td>${escHtml(item.name || item.id)}</td>
        <td><code>${escHtml(item.id)}</code></td>
        <td>${summary.approvedCount}</td>
        <td>${userCount ?? '<span class="muted">—</span>'}</td>
        <td>${item.created_at ? escHtml(fmtDate(item.created_at)) : '<span class="muted">—</span>'}</td>
        <td>
          <div class="tenant-table-actions">
            <button class="btn btn-primary btn-small" data-open-tenant="${escHtml(item.id)}" type="button">Manage</button>
            <button class="btn btn-danger btn-small" data-delete-tenant="${escHtml(item.id)}" type="button">Delete</button>
          </div>
        </td>
      </tr>
    `;
  }).join("") : '<tr><td colspan="6" class="empty-state">No tenants found.</td></tr>';
}

function renderUserRoles(user) {
  if (user.is_superadmin) return '<span class="role-badge">SUPERADMIN</span>';
  return user.tenant_roles?.length ? user.tenant_roles.map(role => `
    <span class="tenant-role-chip">${escHtml(role.tenant_id)} · ${escHtml(normalizeTenantRole(role.role))} <button data-remove-role="${escHtml(user.id)}:${escHtml(role.tenant_id)}" type="button">×</button></span>
  `).join("") : "—";
}

function renderUserRoleAssignForm(user, tenants) {
  if (user.is_superadmin) return '—';
  const options = tenants.map(t => `<option value="${escHtml(t.id)}">${escHtml(t.name)}</option>`).join('');
  return `<div class="inline-form-row">` +
    `<select class="form-input form-input-sm user-tenant-select" data-user-id="${escHtml(user.id)}">${options}</select>` +
    `<select class="form-input form-input-sm user-role-select" data-user-id="${escHtml(user.id)}">` +
    `<option value="admin">Tenant Admin</option><option value="viewer">Tenant Viewer</option></select>` +
    `<button class="btn btn-primary btn-small" data-assign-role="${escHtml(user.id)}" type="button">Assign</button>` +
    `<button class="btn btn-danger btn-small" data-delete-user="${escHtml(user.id)}" type="button">Delete</button>` +
    `</div>`;
}

function renderSuperadminUsers(users) {
  $("#sa-users-count") && ($("#sa-users-count").textContent = String(users.length));
  const tbody = $("#sa-users-tbody");
  if (!tbody) return;
  tbody.innerHTML = users.length ? users.map(user => `
    <tr>
      <td>${escHtml(user.username)}</td>
      <td>${user.is_superadmin ? "superadmin" : "tenant-scoped"}</td>
      <td><div class="tenant-role-list">${renderUserRoles(user)}</div></td>
      <td>${renderUserRoleAssignForm(user, tenants)}</td>
    </tr>
  `).join("") : '<tr><td colspan="4" class="empty-state">No users found.</td></tr>';
}

async function loadGkillState() {
  const res = await apiFetch("/api/superadmin/gkill-state");
  if (!res || !res.ok) return;
  const data = await res.json();
  $("#sa-gkill-value") && ($("#sa-gkill-value").textContent = String(data.value || "—"));
  $("#sa-gkill-fetched") && ($("#sa-gkill-fetched").textContent = data.last_fetched ? fmtDate(new Date(data.last_fetched * 1000).toISOString()) : "—");
  $("#sa-gkill-error") && ($("#sa-gkill-error").textContent = data.error || "—");
  updateGkillBadge(data.value);
}

async function approvePendingSpoke(id) {
  const select = $(`.sa-tenant-assign[data-pending-id="${CSS.escape(id)}"]`);
  const tenantId = select?.value;
  if (!tenantId) return;
  const res = await apiFetch(`/api/superadmin/pending-spokes/${encodeURIComponent(id)}/approve`, {
    method: "POST",
    body: { tenant_id: tenantId },
  });
  if (!res || !res.ok) {
    const err = await readJson(res);
    showToast(err?.detail || "Failed to approve spoke.", "err");
    return;
  }
  const data = await res.json();
  showKeyBanner(data.api_key, data.spoke_id);
  showToast("Spoke approved.", "ok");
  await refreshAfterSpokeApproval(tenantId);
}

async function rejectPendingSpoke(id) {
  const res = await apiFetch(`/api/superadmin/pending-spokes/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res || !res.ok) {
    showToast("Failed to reject spoke.", "err");
    return;
  }
  showToast("Pending spoke rejected.", "ok");
  loadSuperadmin();
}

function openSuperadminTenantForm() {
  if (!currentUser?.is_superadmin) {
    showToast("Only superadmins can add tenants.", "warn");
    return;
  }
  showTab("hub-superadmin", { source: "admin" });
  const tenantsButton = $('.sa-subtab[data-subtab="sa-tenants"]');
  if (tenantsButton) tenantsButton.click();
  $("#sa-tenant-form")?.classList.remove("hidden");
  $("#sa-tenant-name")?.focus();
}

async function createTenant() {
  const name = $("#sa-tenant-name")?.value.trim();
  const aruba_cid = $("#sa-tenant-cid")?.value.trim() || null;
  if (!name) {
    setFormMessage("sa-tenant-msg", "Tenant name is required.", false);
    return;
  }
  const res = await apiFetch("/api/superadmin/tenants", { method: "POST", body: { name, aruba_cid } });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("sa-tenant-msg", err?.detail || "Unable to create tenant.", false);
    return;
  }
  setFormMessage("sa-tenant-msg", "Tenant created.", true);
  $("#sa-tenant-name") && ($("#sa-tenant-name").value = "");
  $("#sa-tenant-cid") && ($("#sa-tenant-cid").value = "");
  $("#sa-tenant-form")?.classList.add("hidden");
  await loadSuperadmin();
}

async function deleteSpoke(spokeId) {
  if (!window.confirm(`Delete spoke "${spokeId}"? This will remove it from the hub. The spoke itself is not affected.`)) return;
  const res = await apiFetch(`/api/spokes/${encodeURIComponent(spokeId)}`, { method: "DELETE" });
  if (!res || !res.ok) {
    showToast("Failed to delete spoke.", "err");
    return;
  }
  showToast("Spoke deleted.", "ok");
  await loadSpokes(true);
}

async function deleteTenant(id) {
  if (!window.confirm(`Delete tenant ${id}?`)) return;
  const res = await apiFetch(`/api/superadmin/tenants/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res || !res.ok) {
    showToast("Failed to delete tenant.", "err");
    return;
  }
  if (currentTenantId === id) {
    currentTenantId = tenants.find(tenant => tenant.id !== id)?.id || null;
    tenantContextActive = false;
  }
  showToast("Tenant deleted.", "ok");
  await loadSuperadmin();
  await loadDashboard(true);
}

async function createUser() {
  const username = $("#sa-new-username")?.value.trim();
  const password = $("#sa-new-password")?.value || "";
  if (!username || !password) {
    setFormMessage("sa-user-msg", "Username and password are required.", false);
    return;
  }
  const res = await apiFetch("/api/superadmin/users", { method: "POST", body: { username, password } });
  if (!res || !res.ok) {
    const err = await readJson(res);
    setFormMessage("sa-user-msg", err?.detail || "Unable to create user.", false);
    return;
  }
  setFormMessage("sa-user-msg", "User created.", true);
  $("#sa-new-username") && ($("#sa-new-username").value = "");
  $("#sa-new-password") && ($("#sa-new-password").value = "");
  loadSuperadmin();
}

async function deleteUser(userId) {
  if (!window.confirm("Delete this user?")) return;
  const res = await apiFetch(`/api/superadmin/users/${encodeURIComponent(userId)}`, { method: "DELETE" });
  if (!res || !res.ok) {
    showToast("Failed to delete user.", "err");
    return;
  }
  showToast("User deleted.", "ok");
  loadSuperadmin();
}

async function assignRole(userId) {
  const tenantId = $(`.user-tenant-select[data-user-id="${CSS.escape(userId)}"]`)?.value;
  const role = $(`.user-role-select[data-user-id="${CSS.escape(userId)}"]`)?.value || "viewer";
  if (!tenantId) return;
  const res = await apiFetch(`/api/superadmin/users/${encodeURIComponent(userId)}/roles`, { method: "POST", body: { tenant_id: tenantId, role } });
  if (!res || !res.ok) {
    showToast("Failed to assign role.", "err");
    return;
  }
  showToast("Role assigned.", "ok");
  loadSuperadmin();
}

async function removeRole(userId, tenantId) {
  const res = await apiFetch(`/api/superadmin/users/${encodeURIComponent(userId)}/roles/${encodeURIComponent(tenantId)}`, { method: "DELETE" });
  if (!res || !res.ok) {
    showToast("Failed to remove role.", "err");
    return;
  }
  showToast("Role removed.", "ok");
  loadSuperadmin();
}

function applyOnlineState(root, online) {
  root.querySelectorAll("[data-online-state] .status-dot, .spoke-section-header > .status-dot").forEach(dot => {
    dot.className = `status-dot ${online ? "online" : "offline"}`;
  });
}

function updateOnlineBadges(spokeOnline) {
  if (!spokeOnline) return;
  document.querySelectorAll("[data-spoke-id]").forEach(node => {
    const tenantId = node.dataset.tenantId;
    const spokeId = node.dataset.spokeId;
    const online = spokeOnline?.[tenantId]?.[spokeId];
    if (typeof online === "boolean") applyOnlineState(node, online);
  });
}

function updateAutoRefreshCountdownDisplay(text, paused = false) {
  const countdown = $("#auto-refresh-countdown");
  if (!countdown) return;
  countdown.textContent = text;
  countdown.classList.toggle("paused", paused);
}

function computeHubRefreshPaused() {
  if (document.hidden || !currentUser) return true;
  if (activeTab !== "dashboard") {
    return !autoRefreshActiveTabs.has(activeTab);
  }
  if (tenantDetailState.open) {
    return !autoRefreshActiveTabs.has(tenantDetailState.activeTab);
  }
  return false;
}

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
  if (autoRefreshCountdownTimer) {
    clearInterval(autoRefreshCountdownTimer);
    autoRefreshCountdownTimer = null;
  }
}

function syncAutoRefreshState() {
  stopAutoRefresh();
  const toggle = $("#auto-refresh-toggle");
  const intervalSelect = $("#auto-refresh-interval");
  if (!toggle?.checked) {
    updateAutoRefreshCountdownDisplay("Off");
    return;
  }
  const seconds = parseInt(intervalSelect?.value || "10", 10);
  if (!(seconds > 0)) {
    updateAutoRefreshCountdownDisplay("Off");
    return;
  }
  if (refreshPaused) {
    updateAutoRefreshCountdownDisplay("Paused", true);
    return;
  }
  autoRefreshSecondsLeft = seconds;
  updateAutoRefreshCountdownDisplay(String(autoRefreshSecondsLeft) + 's');
  autoRefreshCountdownTimer = setInterval(() => {
    autoRefreshSecondsLeft = Math.max(0, autoRefreshSecondsLeft - 1);
    updateAutoRefreshCountdownDisplay(String(autoRefreshSecondsLeft) + 's');
  }, 1000);
  autoRefreshTimer = setInterval(async () => {
    autoRefreshSecondsLeft = seconds;
    updateAutoRefreshCountdownDisplay(String(autoRefreshSecondsLeft) + 's');
    await refreshCurrentView(false);
  }, seconds * 1000);
}

function updateHubRefreshPausedState() {
  const wasPaused = refreshPaused;
  refreshPaused = computeHubRefreshPaused();
  syncAutoRefreshState();
  if (wasPaused && !refreshPaused) {
    refreshCurrentView(true).catch(() => {});
  }
}

function startAutoRefresh() {
  updateHubRefreshPausedState();
}

document.addEventListener('visibilitychange', () => {
  updateHubRefreshPausedState();
});

function connectHubWebSocket() {
  if (!authToken) return;
  if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) return;
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${window.location.host}/ws`);
  ws.onopen = () => updateApiStatus(true, "Connected");
  ws.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === "telemetry") {
      if (activeTab === "dashboard") scheduleReload("ws-dashboard", () => loadDashboard(true));
      if (activeTab === "simulations") scheduleReload("ws-simulations", () => loadHubSimulations(true));
      if (activeTab === "clients") scheduleReload("ws-clients", () => loadClients(true));
      if (activeTab === "vm-server") scheduleReload("ws-vm-server", () => loadVmServer(true));
      if (activeTab === "api-server") scheduleReload("ws-api-server", () => loadApiServer(true));
      if (activeTab === "central") scheduleReload("ws-hub-central", () => loadHubCentralData(true));
      if (activeTab === "spokes") scheduleReload("ws-spokes", () => loadSpokes(true));
      if (activeTab === "reseed") scheduleReload("ws-reseed", () => ensureSpokes(true).then(() => renderHubReseedPanel()));
      if (activeTab === "tenant-setup") scheduleReload("ws-tenant-setup", () => loadTenantSetup(true));
      if (activeTab === "config") scheduleReload("ws-config", () => loadConfig(true));
      if (activeSpokeModal && data.tenant_id === activeSpokeModal.tenant_id && data.spoke_id === activeSpokeModal.spoke.id) {
        scheduleReload("ws-modal", () => loadSpokes(true).then(() => {
          renderSpokeClientsTab();
          renderSpokeServerTab();
          renderSpokeCentralTab();
          renderSpokeStatusTab();
          const configPanel = document.getElementById("spoke-config");
          if (configPanel && !configPanel.classList.contains("hidden")) {
            return loadSpokeConfig(true);
          }
          renderSpokeConfigTab();
          return null;
        }));
      }
    } else if (data.type === "aruba_update") {
      const activeTenantId = getActiveTenantId();
      if (!data.tenant_id || !activeTenantId || data.tenant_id === activeTenantId) {
        if (data.status && typeof data.status === "object") hubCentralSiteStatus = data.status;
        if (data.wireless_clients && typeof data.wireless_clients === "object") hubCentralWirelessClients = data.wireless_clients;
        if (Array.isArray(data.hardware_alerts)) hubCentralHardwareAlerts = data.hardware_alerts;
        if (data.client_count_status && typeof data.client_count_status === "object") hubClientCountStatus = data.client_count_status;
        if (data.central_sites_config && aggregateCentralData) aggregateCentralData.central_sites_config = data.central_sites_config;
        if (hubCentralData && hubCentralData.mode === "centralized") {
          hubCentralData.client_count_status = hubClientCountStatus;
          if (data.token_state?.state) {
            hubCentralData.token_state = data.token_state.state;
            hubCentralData.token_valid = data.token_state.state === "connected";
          }
        }
        const centralizedModeActive = aggregateCentralData?.mode === "centralized" || hubCentralData?.mode === "centralized";
        if (activeTab === "central") scheduleReload("ws-hub-central-aruba", () => loadHubCentralData(true));
        if (activeTab === "tenant-setup" && hubTenantSetupActiveSubtab === "ts-central-api") {
          if (centralizedModeActive) scheduleReload("ws-hub-central-setup-aruba", () => loadCentral(true));
          else renderHubCentral();
        }
      }
    } else if (data.type === "heartbeat_update") {
      updateOnlineBadges(data.island_online);
    } else if (data.type === "gkill_switch_update") {
      updateGkillBadge(data.value);
    } else if (data.type === "notification") {
      showToast(data.message, data.level === "warning" ? "warn" : "ok");
    } else if (data.type === "cert_renewed") {
      showToast(`TLS certificate renewed — expires ${data.expires || "unknown"}`, "ok");
      if (activeTab === "setup") loadAcmeSettings();
    } else if (data.type === "backup_progress") {
      updateSuperadminBackupProgress(data);
    } else if (data.type === "reseed_progress") {
      updateHubReseedProgress(data);
    } else if (data.type === "pending_spoke_registered") {
      if (currentUser?.is_superadmin && activeTab === "superadmin") loadSuperadmin();
      if (canManageTenant() && !currentUser?.is_superadmin && data.tenant_hint === currentTenantId) {
        loadTenantPendingSpokes();
        showToast(`New spoke '${data.spoke_name || data.hostname}' is pending approval.`, "ok");
      }
    } else if (data.type === "spoke_approved") {
      scheduleReload(`ws-approved-${data.tenant_id || "all"}`, () => {
        refreshAfterSpokeApproval(data.tenant_id || currentTenantId).catch(() => {});
      });
    } else if (data.type === "task_result") {
      showToast(`Spoke ${data.spoke_id}: ${data.task_type} ${data.status}`, data.status === "success" ? "ok" : "err");
      if (activeSpokeModal && data.spoke_id === activeSpokeModal.spoke.id) {
        loadSpokeCommands();
        loadSpokeAudit();
      }
    }
  };
  ws.onclose = () => {
    updateApiStatus(false, "Disconnected");
    ws = null;
    if (!authToken) return;
    wsReconnectTimer = window.setTimeout(connectHubWebSocket, 3000);
  };
  ws.onerror = () => {
    if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
  };
}

function bindEvents() {
  document.addEventListener("click", event => {
    const adminShortcut = event.target.closest("[data-admin-tab]");
    if (adminShortcut) {
      showTab(adminShortcut.dataset.adminTab, { source: "admin" });
      return;
    }

    if (event.target.closest("#tenant-context-change-btn")) {
      showTab("hub-dashboard", { source: "admin" });
      return;
    }

    if (event.target.closest("#hub-superadmin-btn")) {
      openSuperadminBackupModal().catch(() => {
        superadminBackupState.backupError = "Failed to open backup panel.";
        superadminBackupState.open = true;
        renderSuperadminBackupModal();
      });
      return;
    }

    if (event.target.id === "sa-backup-modal") {
      closeSuperadminBackupModal(false);
      return;
    }

    const backupTabButton = event.target.closest(".sa-backup-tab");
    if (backupTabButton) {
      superadminBackupState.activeTab = backupTabButton.dataset.saBackupTab || "backup";
      renderSuperadminBackupModal();
      return;
    }

    if (event.target.closest("#sa-backup-modal-x") || event.target.closest("#sa-backup-cancel-btn") || event.target.closest("#sa-backup-close-btn")) {
      closeSuperadminBackupModal(false);
      return;
    }

    if (event.target.closest("#sa-backup-proceed-btn")) {
      superadminBackupState.backupError = "";
      superadminBackupState.step = "key";
      renderSuperadminBackupModal();
      return;
    }

    if (event.target.closest("#sa-backup-back-btn")) {
      superadminBackupState.backupError = "";
      superadminBackupState.step = "confirm";
      renderSuperadminBackupModal();
      return;
    }

    if (event.target.closest("#sa-backup-start-btn")) {
      startSuperadminBackup();
      return;
    }

    if (event.target.closest("#sa-backup-config-save-btn")) {
      saveSuperadminBackupConfig();
      return;
    }

    if (event.target.closest("#reseed-refresh-templates-btn")) {
      loadHubReseedTemplates(true).catch(() => {});
      return;
    }

    if (event.target.closest("#reseed-select-all-btn")) {
      setHubReseedSelectedSpokeIds(listTenantReseedSpokes().map(spoke => spoke.id));
      hubReseedState.error = "";
      renderHubReseedPanel();
      return;
    }

    if (event.target.closest("#reseed-clear-btn") || event.target.closest("#reseed-cancel-btn")) {
      setHubReseedSelectedSpokeIds([]);
      hubReseedState.error = "";
      renderHubReseedPanel();
      return;
    }

    if (event.target.closest("#reseed-start-btn")) {
      startHubReseed();
      return;
    }

    if (event.target.closest("#reseed-close-btn")) {
      if (isHubReseedComplete()) {
        resetHubReseedState({ preserveTemplates: true });
        renderHubReseedPanel();
      }
      return;
    }
 
    const tabButton = event.target.closest("#tab-nav .tab");
    if (tabButton) {
      if (tabButton.dataset.tenantId) setCurrentTenant(tabButton.dataset.tenantId, false);
      // Row-1 back-nav buttons in tenant context exit tenant context first
      if (tabButton.classList.contains("tab-back") && tenantContextActive) {
        exitTenantContext();
        showTab(tabButton.dataset.tab, { button: tabButton, source: "admin" });
        return;
      }
      showTab(tabButton.dataset.tab, { button: tabButton, source: tabButton.closest("#tenant-context-nav .tenant-context-nav-row2") ? "tenant" : "admin" });
      return;
    }

    const enterTenantButton = event.target.closest("[data-enter-tenant]");
    if (enterTenantButton) {
      enterTenantContext(enterTenantButton.dataset.enterTenant, "simulations", true);
      return;
    }

    if (event.target.closest("[data-add-tenant]")) {
      openSuperadminTenantForm();
      return;
    }

    const tenantSortButton = event.target.closest("[data-dashboard-tenant-sort]");
    if (tenantSortButton) {
      const key = tenantSortButton.dataset.dashboardTenantSort;
      if (key) {
        tenantDashboardSort.direction = tenantDashboardSort.key === key && tenantDashboardSort.direction === "asc" ? "desc" : "asc";
        tenantDashboardSort.key = key;
        $("#dashboard-grid") && ($("#dashboard-grid").innerHTML = renderDashboardTenantTable(dashboardTenantRows));
      }
      return;
    }

    const openTenantButton = event.target.closest("[data-open-tenant]");
    if (openTenantButton) {
      enterTenantContext(openTenantButton.dataset.openTenant, "simulations", true);
      return;
    }

    const tenantDetailTab = event.target.closest(".tenant-detail-tab");
    if (tenantDetailTab) {
      tenantDetailState.activeTab = tenantDetailTab.dataset.tenantDetailTab;
      renderTenantDetail();
      return;
    }

    const hubConfigSubtab = event.target.closest(".hub-config-subtab");
    if (hubConfigSubtab) {
      hubConfigActiveSubtab = hubConfigSubtab.dataset.hubConfigSubtab || "api";
      if (tenantDetailState.open) {
        renderTenantDetail();
      } else {
        loadConfig(false).catch(() => {});
      }
      if (hubConfigActiveSubtab === "simulation") loadHubSimulationConf().catch(() => {});
      return;
    }

    if (event.target.closest("[data-open-tenant-setup]")) {
      if (tenantDetailState.open) {
        tenantDetailState.activeTab = "setup";
        renderTenantDetail();
      } else {
        showTab("hub-tenant-setup", { source: "tenant" });
      }
      return;
    }

    if (event.target.closest("#tenant-detail-back-btn")) {
      showTab("hub-dashboard", { source: "admin" });
      return;
    }

    const detailSpokeButton = event.target.closest("[data-open-spoke-modal]");
    if (detailSpokeButton) {
      const spoke = getSpokeFromCache(tenantDetailState.tenantId, detailSpokeButton.dataset.openSpokeModal);
      if (spoke) openSpokeModal(spoke, tenantDetailState.tenantId, "spoke-clients");
      return;
    }

    const hTsButton = event.target.closest(".hub-ts-subtab");
    if (hTsButton) {
      const subtab = hTsButton.dataset.subtab;
      activateHubTenantSetupSubtab(subtab).catch(() => {});
      return;
    }

    const hCentralBtn = event.target.closest(".hub-central-subtab");
    if (hCentralBtn) {
      hubCentralActiveSubtab = hCentralBtn.dataset.subtab;
      $$(".hub-central-subtab").forEach(b => b.classList.toggle("active", b.dataset.subtab === hubCentralActiveSubtab));
      ["hcs-sites-panel", "hcs-alerts-panel", "hcs-clients-panel"].forEach(id => {
        document.getElementById(id)?.classList.toggle("hidden", id !== `${hubCentralActiveSubtab}-panel`);
      });
      if (!hubCentralData && currentTenantId && currentUser) {
        loadHubCentralData().catch(() => {});
      } else {
        renderHubCentralStatus();
      }
      return;
    }

    const setupButton = event.target.closest(".settings-subtab");
    if (setupButton) {
      const requestedSubtab = setupButton.dataset.subtab;
      const subtab = requestedSubtab === "settings-spoke-config" && !syncHubSpokeConfigSetupUI()
        ? "settings-account"
        : requestedSubtab;
      $$(".settings-subtab").forEach(button => button.classList.toggle("active", button.dataset.subtab === subtab));
      ["settings-account", "settings-notifications", "settings-tls", "settings-pending-spokes", "settings-spoke-config"].forEach(panelId => {
        document.getElementById(panelId)?.classList.toggle("hidden", panelId !== subtab);
      });
      if (subtab === "settings-tls") loadAcmeSettings().catch(() => {});
      if (subtab === "settings-spoke-config") loadHubConfig().catch(() => {});
      return;
    }

    const saButton = event.target.closest(".sa-subtab");
    if (saButton) {
      const subtab = saButton.dataset.subtab;
      $$(".sa-subtab").forEach(button => button.classList.toggle("active", button.dataset.subtab === subtab));
      ["sa-pending", "sa-tenants", "sa-users", "sa-security", "sa-gkill"].forEach(panelId => {
        document.getElementById(panelId)?.classList.toggle("hidden", panelId !== subtab);
      });
      if (subtab === "sa-security") loadHubAuthConfig().catch(() => {});
      return;
    }

    const spokeSubtab = event.target.closest(".spoke-subtab");
    if (spokeSubtab) {
      activateSpokeSubtab(spokeSubtab.dataset.subtab);
      return;
    }

    if (event.target.matches("[data-approve-id]")) approvePendingSpoke(event.target.dataset.approveId);
    if (event.target.matches("[data-reject-id]")) rejectPendingSpoke(event.target.dataset.rejectId);
    if (event.target.matches("[data-delete-tenant]")) deleteTenant(event.target.dataset.deleteTenant);
    if (event.target.matches("[data-delete-user]")) deleteUser(event.target.dataset.deleteUser);
    if (event.target.matches("[data-delete-spoke]")) { deleteSpoke(event.target.dataset.deleteSpoke); return; }
    if (event.target.matches("[data-assign-role]")) assignRole(event.target.dataset.assignRole);
    if (event.target.matches("[data-remove-role]")) {
      const [userId, tenantId] = event.target.dataset.removeRole.split(":");
      removeRole(userId, tenantId);
      return;
    }
    if (event.target.closest("#save-central-btn")) {
      saveCentralSettings();
      return;
    }
    if (event.target.closest("#save-config-push-btn")) {
      saveConfigPush();
      return;
    }
    if (event.target.closest("#save-tenant-github-btn")) {
      saveTenantGithubSettings();
      return;
    }
    if (event.target.closest("#hub-sim-config-refresh-btn")) {
      loadHubSimulationConf(true).catch(() => {});
      return;
    }
    if (event.target.closest("#hub-sim-config-save-btn")) {
      saveHubSimulationConf();
    }
  });

  document.addEventListener("change", event => {
    const spokeSelect = event.target.closest("#sa-spoke-select");
    if (spokeSelect) {
      superadminBackupState.selectedSpokeId = spokeSelect.value || "";
      superadminBackupState.backupError = "";
      renderSuperadminBackupModal();
      return;
    }
    const templateSelect = event.target.closest("#reseed-template-select");
    if (templateSelect) {
      hubReseedState.selectedTemplateKey = templateSelect.value || "";
      hubReseedState.error = "";
      renderHubReseedPanel();
      return;
    }
    const spokeToggle = event.target.closest("[data-reseed-spoke-id]");
    if (spokeToggle) {
      const next = new Set(hubReseedState.selectedSpokeIds);
      if (spokeToggle.checked) next.add(spokeToggle.dataset.reseedSpokeId);
      else next.delete(spokeToggle.dataset.reseedSpokeId);
      setHubReseedSelectedSpokeIds(Array.from(next));
      hubReseedState.error = "";
      renderHubReseedPanel();
    }
  });
 
  $("#hub-logout-btn")?.addEventListener("click", () => logout(true));
  $("#login-submit-btn")?.addEventListener("click", submitLogin);
  $("#login-username")?.addEventListener("keydown", event => { if (event.key === "Enter") submitLogin(); });
  $("#login-password")?.addEventListener("keydown", event => { if (event.key === "Enter") submitLogin(); });
  $("#refresh-dashboard-btn")?.addEventListener("click", () => loadDashboard(true));
  $("#dashboard-add-tenant-btn")?.addEventListener("click", openSuperadminTenantForm);
  document.querySelectorAll(".hub-simtop-subtab").forEach((button) => {
    button.addEventListener("click", () => activateHubSimTopTab(button.dataset.hubsimtop || "hub-simtop-checks"));
  });
  document.querySelectorAll(".hub-sim-subtab").forEach((button) => {
    button.addEventListener("click", () => {
      hubSimChecksFilter = button.dataset.hubsimtab || "failing";
      document.querySelectorAll(".hub-sim-subtab").forEach((item) => item.classList.toggle("active", item === button));
      renderHubSimChecksList();
    });
  });
  $("#hub-checks-filter")?.addEventListener("input", (event) => {
    hubSimChecksSearch = event.target.value || "";
    renderHubSimChecksList();
  });
  $("#hub-sim-detail-back")?.addEventListener("click", () => {
    $("#hub-sim-detail")?.classList.add("hidden");
    $("#hub-sim-overview")?.classList.remove("hidden");
    hubSimOpenCheckId = null;
  });
  $("#hub-hw-detail-back")?.addEventListener("click", () => {
    $("#hub-hw-detail")?.classList.add("hidden");
    $("#hub-hw-overview")?.classList.remove("hidden");
    hubHwOpenCheckId = null;
  });
  $("#hub-cc-detail-back")?.addEventListener("click", () => {
    $("#hub-cc-detail")?.classList.add("hidden");
    $("#hub-cc-overview")?.classList.remove("hidden");
    hubCcOpenWsite = null;
  });
  $("#hub-sim-refresh-btn")?.addEventListener("click", () => loadHubSimulations(true));
  $("#refresh-clients-btn")?.addEventListener("click", () => loadClients(true));
  $("#refresh-hub-central-btn")?.addEventListener("click", () => loadHubCentralData(true));
  $("#refresh-vm-server-btn")?.addEventListener("click", () => loadVmServer(true));
  $("#refresh-api-server-btn")?.addEventListener("click", () => loadApiServer(true));
  $("#refresh-spokes-btn")?.addEventListener("click", () => loadSpokes(true));
  $("#refresh-commands-btn")?.addEventListener("click", loadCommands);
  $("#refresh-config-btn")?.addEventListener("click", () => loadConfig(true));
  $("#refresh-tenant-setup-btn")?.addEventListener("click", () => loadTenantSetup(true));
  $("#auto-refresh-toggle")?.addEventListener("change", startAutoRefresh);
  $("#auto-refresh-interval")?.addEventListener("change", startAutoRefresh);
  $("#send-command-btn")?.addEventListener("click", sendCommandFromForm);
  $("#collapse-all-btn")?.addEventListener("click", () => { getExpandedSet().clear(); loadSpokes(); });
  $("#expand-all-btn")?.addEventListener("click", async () => {
    const spokes = await ensureSpokes();
    spokeUiState.expandedByTenant[currentTenantId] = new Set(spokes.filter(spoke => spoke.status === "approved").map(spoke => spoke.id));
    loadSpokes();
  });
  $("#hub-clients-search")?.addEventListener("input", event => {
    hubClientUiState.search = event.target.value || "";
    renderClientRowsForHub();
  });
  $("#hub-clients-status-filter")?.addEventListener("change", event => {
    hubClientUiState.status = event.target.value || "all";
    renderClientRowsForHub();
  });
  document.querySelectorAll("[data-clienttype]").forEach(button => {
    button.addEventListener("click", () => setClientTypeFilter(button.dataset.clienttype || "all"));
  });
  document.querySelectorAll("[data-hubclienttype]").forEach(button => {
    button.addEventListener("click", () => setHubClientTypeFilter(button.dataset.hubclienttype || "all"));
  });
  $("#spoke-search")?.addEventListener("input", event => {
    spokeUiState.search = event.target.value || "";
    scheduleReload("spoke-search", () => loadSpokes(), 120);
  });
  $('#spoke-modal-close')?.addEventListener('click', closeSpokeModal);
  $('#spoke-modal')?.addEventListener('click', event => { if (event.target === event.currentTarget) closeSpokeModal(); });
  $('#hub-proxmox-credentials-close')?.addEventListener('click', closeHubProxmoxCredentialsModal);
  $('#hub-proxmox-credentials-modal')?.addEventListener('click', event => { if (event.target === event.currentTarget) closeHubProxmoxCredentialsModal(); });
  $('#hub-proxmox-credentials-save')?.addEventListener('click', saveHubProxmoxCredentials);
  $('#mode-save-btn')?.addEventListener('click', saveSpokeProcessingMode);
  $("#pw-save-btn")?.addEventListener("click", savePassword);
  $("#notif-save-btn")?.addEventListener("click", saveNotificationSettings);
  $("#hub-config-save-btn")?.addEventListener("click", saveHubConfig);
  $("#hub-config-enabled-toggle")?.addEventListener("change", function () {
    document.getElementById("hub-config-fields")?.classList.toggle("hidden", !this.checked);
  });
  $("#sa-gkill-refresh-btn")?.addEventListener("click", loadGkillState);
  hubAuthEl("sa-auth-provider")?.addEventListener("change", () => updateHubAuthProviderVisibility(hubAuthEl("sa-auth-provider")?.value));
  $("#sa-auth-test-btn")?.addEventListener("click", testHubAuthConnection);
  $("#sa-auth-save-btn")?.addEventListener("click", saveHubAuthConfig);
  $("#sa-add-tenant-btn")?.addEventListener("click", () => $("#sa-tenant-form")?.classList.toggle("hidden"));
  $("#sa-cancel-tenant-btn")?.addEventListener("click", () => $("#sa-tenant-form")?.classList.add("hidden"));
  $("#sa-save-tenant-btn")?.addEventListener("click", createTenant);
  $("#sa-create-user-btn")?.addEventListener("click", createUser);
}

(async function init() {
  bindEvents();
  await pingApi();
  await loadUserContext();
  if (currentUser) {
    connectHubWebSocket();
    const _urlParams = new URLSearchParams(location.search);
    const _urlSpoke = _urlParams.get("spoke");
    const _urlTenant = _urlParams.get("tenant");
    if (_urlTenant && _urlTenant !== currentTenantId) {
      // Switch to the tenant from the URL before loading
      currentTenantId = _urlTenant;
    }
    if (currentTenantId) await ensureSpokes(true);
    syncTenantContextChrome();
    syncHubPermissionUI();
    if (_urlSpoke) {
      // Navigate directly to this spoke's detail in vm-server tab
      hubVmServerSelectedSpoke = _urlSpoke;
      showTab("vm-server");
    } else {
      await loadDashboard();
    }
  }
  startAutoRefresh();
})();

document.getElementById("acme-dns-provider")?.addEventListener("change", toggleAcmeDnsSection);


  })();
}

async function setFooterVersions() {
  try {
    const init = window.__CS_WEBUI_INIT__ || await fetchInitPayload();
    if (!init) return;
    const fWebui = document.getElementById('footer-cswebui-version');
    const fRepo  = document.getElementById('footer-repo-version');
    if (fWebui) {
      const ver = init.app_version || init.installer_version || '—';
      fWebui.textContent = `CS-WebUI v${ver}`;
      fWebui.title = `cs-webui frontend version: v${ver}`;
    }
    if (fRepo) {
      const rver = init.installer_version || '—';
      fRepo.textContent = `GitHub Repo v${rver}`;
    }
  } catch (_) {}
}

(async function initUnifiedWebUi() {
  const mode = await detectWebuiMode();
  void setFooterVersions();
  if (mode === 'hub') startHubApp();
  else startSpokeApp();
})();
