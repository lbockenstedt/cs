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
function _fmtConfigKey(k) { return k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
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
  central_api: {
    mode: 'classic',
    classic: { url: '', username: '', password_configured: false },
    central: { url: '', client_id: '', customer_id: '', client_secret_configured: false }
  },
  site_mappings: {},
  monitored_checks: [],
  hardware_checks: [],
  relay_enabled: 'off',
  relay_server_url: '',
  relay_spoke_name: '',
  relay_spoke_id: '',
  relay_poll_interval: 60,
  hub_isolation_timeout: 3600, // Keep the server-side timeout in seconds so relay status rendering can reuse the authoritative safeguard value.
  hub_isolation_timeout_min: 60, // Keep the setup default in minutes so the Hub form shows the required 60-minute timeout before settings load.
  relay_api_key_configured: false,
  repo_sync_interval: 300,
  usb_vidpids: '[]',
  usb_missing_timeout: '60',
  vm_image_1_template_id: '100',
  vm_image_1_template_spec: '100',
  vm_image_2_template_id: '200',
  vm_image_2_template_spec: '200',
  vm_image_1_pct: '50',
  usb_auto_provision: 'off',
  use_all_dongles: false,
  sim_phy: 'wireless',
  usb_ignored_vidpids: '[]',
  ignored_hostnames: '["sim-rpi-0000"]',
  vm_silent_timeout: '24',
  reclone_schedule_enabled: 'off',
  reclone_schedule_cron: 'sunday 02:00',
  reclone_concurrency: '1',
  l1_vlan_start: '100',
  l1_vlan_end: '199'
};
let spokeSimConfState = { loaded: false, loading: false, rawContent: '', fetchedAt: '', sections: {}, sectionOrder: [], keyOrder: {}, error: null };
let centralTokenValid = null;
let centralLastSyncedTs = null;
let centralStatusInitialized = false;
let latestProxmoxData = { vms: [], usb_state: [], unknown_usb: [], reclone_state: null };
let latestRecloneState = null;
let usbCountdownTimer = null;
let activeVmCat = 'sim';   // 'sim' | 'other' | 'containers' | 'templates'
let webuiVmid = null;      // VMID of the LXC container running this service (protected from delete)

// ── Tab navigation ────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.remove('active');
      t.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.tab-content').forEach((c) => c.classList.add('hidden'));

    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    document.getElementById(`tab-${tab.dataset.tab}`).classList.remove('hidden');
    if (tab.dataset.tab === 'setup') activateSetupSubtab('setup-github');
    if (tab.dataset.tab === 'server') { activateServerSubtab('server-vms'); loadProxmoxApproved().catch(() => {}); }
    if (tab.dataset.tab === 'central') { activateCentralSubtab('central-sites-panel'); }
    if (tab.dataset.tab === 'simulations') { activateSimTopTab('simtop-checks'); }
    resetTabDrilldowns(tab.dataset.tab);
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

function activateSetupSubtab(subtabId = 'setup-github') {
  setupSubtabButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.subtab === subtabId);
  });
  setupSubpanels.forEach((panel) => {
    const isActive = panel.id === subtabId;
    panel.classList.toggle('active', isActive);
    panel.classList.toggle('hidden', !isActive);
  });
  if (subtabId === 'setup-account') loadSpokeLocalUsers().catch(() => {});
  if (subtabId === 'setup-simulation') loadSpokeSimConf().catch(() => {});
}

function activateConfigSubtab(subtabId = 'config-simulation-panel') {
  document.querySelectorAll('.config-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.subtab === subtabId);
  });
  document.querySelectorAll('.config-subpanel').forEach((panel) => {
    const isActive = panel.id === subtabId;
    panel.classList.toggle('active', isActive);
    panel.classList.toggle('hidden', !isActive);
  });
  if (subtabId === 'config-simulation-panel') {
    loadSpokeSimConf().catch(() => {});
  }
  if (subtabId === 'config-user-overrides') {
    loadSpokeUserOverridesConf().catch(() => {});
  }
}

function activateServerSubtab(subtabId = 'server-vms') {
  document.querySelectorAll('.server-subtab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.subtab === subtabId);
  });
  ['server-node', 'server-vms', 'server-usb', 'server-t3', 'server-other', 'server-vh', 'server-commands'].forEach((id) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    const isActive = id === subtabId;
    panel.classList.toggle('active', isActive);
    panel.classList.toggle('hidden', !isActive);
  });
  if (subtabId === 'server-vh') renderVhDevices(latestProxmoxData);
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
      // Check agent version to give a useful hint
      const agentVer = document.getElementById('server-agent-version');
      const ver = agentVer ? agentVer.textContent.trim() : '';
      const hint = ver && parseFloat(ver) < 0.99
        ? `Agent v${ver} detected — update to v0.99+ to enable log streaming (click ⬆ Update Agent)`
        : 'No logs yet — logs arrive on the next agent telemetry poll (≤60s after activity).';
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

function updateHubIsolationBanner(isolated, lastSyncTs, timeoutSecs) { // Toggle the header isolation banner so operators immediately know hub config pushes are paused.
  const banner = document.getElementById('hub-isolation-banner'); // Look up the dedicated banner element so relay updates can reuse one rendering path.
  if (!banner) return; // Exit safely when the header element is missing so status updates never throw.
  if (isolated) { // Render the warning only while isolated because recovery should remove the alert automatically.
    const effectiveTimeoutSecs = Number.isFinite(Number(timeoutSecs)) ? Number(timeoutSecs) : 3600; // Fall back to the default timeout so the warning stays meaningful even if the server omits the field.
    const minutesAgo = lastSyncTs ? Math.max(1, Math.floor((Date.now() / 1000 - Number(lastSyncTs)) / 60)) : Math.max(1, Math.floor(effectiveTimeoutSecs / 60)); // Convert the last check-in timestamp into elapsed minutes so the banner matches the requested copy.
    banner.textContent = `⚠ Hub isolated — last contact ${minutesAgo}m ago. Config pushes paused.`; // Show the required warning text so operators know the spoke froze hub-driven config changes intentionally.
    banner.title = `Hub config pushes pause after ${Math.max(5, Math.round(effectiveTimeoutSecs / 60))} minutes without successful hub contact.`; // Add hover context so operators can see which timeout triggered the safeguard.
    banner.style.display = ''; // Reveal the banner while isolated so the warning is visible from every tab.
    return; // Stop after rendering the warning because the recovery hide path below should not run.
  } // End the isolated branch so the recovery cleanup below is explicit.
  banner.textContent = ''; // Clear stale warning text on recovery so old outage data does not linger in the header.
  banner.title = ''; // Clear the tooltip on recovery so the header reflects the healthy state.
  banner.style.display = 'none'; // Hide the banner after recovery so the header returns to its normal layout.
} // Finish the banner helper so every relay status update can reuse the same isolation rendering logic.

function setRelayStatus(data = {}) {
  const stateText = document.getElementById('relay-state-text');
  const lastTime = document.getElementById('relay-last-time');
  const lastError = document.getElementById('relay-last-error');
  const dot = document.getElementById('relay-indicator');
  const spokeIdDisplay = document.getElementById('relay-spoke-id-display');
  const apikeyStatus = document.getElementById('relay-apikey-status');
  const isolationStatus = document.getElementById('relay-isolation-status'); // Cache the setup-grid isolation field so relay status updates can show the safeguard state alongside other hub health details.
  const lastCheckin = data.hub_last_checkin ?? data.last_sync; // Prefer the explicit hub check-in timestamp so the setup grid and banner share the same isolation source of truth.

  const isNameConflict = data.registration_status === 'name_conflict' || (data.error || '').startsWith('name_conflict:');
  if (stateText) stateText.textContent = !data.enabled ? 'Disabled' : data.connected ? '✓ Connected' : isNameConflict ? '✗ Name conflict' : data.error ? '✗ Error' : data.registration_status === 'pending' ? 'Pending approval' : 'Enabled';
  if (lastTime) lastTime.textContent = lastCheckin ? new Date(lastCheckin * 1000).toLocaleTimeString() : '—'; // Show the last successful hub check-in time so operators can see when isolation started counting from.
  if (lastError) lastError.textContent = data.error || '—';
  if (spokeIdDisplay) spokeIdDisplay.textContent = data.spoke_id || currentSettings.relay_spoke_id || '—'; // Preserve the known spoke ID during live updates so relay broadcasts never blank the setup status grid.
  if (apikeyStatus) apikeyStatus.textContent = data.api_key_configured ? '✓ Received' : 'Pending approval';
  if (isolationStatus) isolationStatus.textContent = data.hub_isolated ? '⚠ Isolated' : 'Normal'; // Show whether hub pushes are paused so the setup grid reflects the safeguard state immediately.
  updateHubIsolationBanner(data.hub_isolated, lastCheckin, data.hub_isolation_timeout); // Sync the header banner with the latest relay isolation data so every UI surface updates together.

  if (dot) {
    dot.className = data.connected ? 'ind-dot green' : 'ind-dot red';
    dot.title = data.connected
      ? `Hub connected — last sync: ${new Date((lastCheckin || 0) * 1000).toLocaleTimeString()}` // Reuse the last successful check-in timestamp in the tooltip so the header reflects the same time shown in setup.
      : `Hub disconnected: ${data.error || 'unknown'}`;
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
}

// ── Setup tab — settings form ─────────────────────────────────────
const branchInput = document.getElementById('branch-input');
const githubTokenInput = document.getElementById('github-token-input');
const githubTokenStatus = document.getElementById('github-token-status');
const syncNowBtn = document.getElementById('sync-now-btn');
const syncNowMsg = document.getElementById('sync-now-message');
const settingsMsg = document.getElementById('settings-message');
const checkUpdateBtn = document.getElementById('check-update-btn');
const updateMsg = document.getElementById('update-message');
const versionCurrent = document.getElementById('version-current');
const versionAvailable = document.getElementById('version-available');
const versionLastChecked = document.getElementById('version-last-checked');
const setupActiveBranch = document.getElementById('setup-active-branch');
const repoUrlInput = document.getElementById('repo-url-input');
const centralTabButton = document.querySelector('.tab[data-tab="central"]');
const configTabButton = document.querySelector('.tab[data-tab="config"]');
const simTabButton = document.querySelector('.tab[data-tab="simulations"]');
const setupTabButton = document.querySelector('.tab[data-tab="setup"]');
const setupSubtabButtons = document.querySelectorAll('.setup-subtab:not(.server-subtab):not(.sim-subtab):not(.central-subtab):not(.simtop-subtab)');
const setupSubpanels = document.querySelectorAll('.setup-subpanel:not(#server-vms):not(#server-usb):not(#server-t3):not(#server-other):not(#server-vh):not(#server-node):not(#server-commands)');
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
const relayTenantHintInput = document.getElementById('relay-tenant-hint-input');
const relayMsg = document.getElementById('relay-message');
const hubIsolationTimeoutInput = document.getElementById('hub-isolation-timeout-input'); // Cache the isolation timeout input so the Hub setup card can load and save the safeguard threshold.

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
const emailNotifMsg      = document.getElementById('email-notif-msg');
const teamsEnabledToggle = document.getElementById('teams-enabled-toggle');
const teamsWebhookUrl    = document.getElementById('teams-webhook-url');
const testTeamsBtn       = document.getElementById('test-teams-btn');
const teamsNotifMsg      = document.getElementById('teams-notif-msg');
const usbAutoProvisionInput = document.getElementById('usb-auto-provision');
const useAllDonglesInput = document.getElementById('use-all-dongles');
const simPhyInput = document.getElementById('usb-sim-phy');
const usbMissingTimeoutInput = document.getElementById('usb-missing-timeout');
const vmImage1TemplateIdInput = document.getElementById('vm-image-1-template-id');
const vmImage2TemplateIdInput = document.getElementById('vm-image-2-template-id');
const templateVmidSpecError = document.getElementById('template-vmid-spec-error');
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
const configSimulationPanel = document.getElementById('config-simulation-panel');
const setupSimulationPanel = document.getElementById('setup-simulation-panel');

function mergeSettings(next = {}) {
  const mergedCentralApi = normalizeCentralApiSettings(next, currentSettings.central_api);
  const merged = {
    repo_url: next.repo_url ?? currentSettings.repo_url ?? repoUrlInput?.value ?? '',
    repo_branch: next.repo_branch ?? currentSettings.repo_branch ?? '',
    github_token_configured: next.github_token_configured ?? currentSettings.github_token_configured ?? false,
    central_api: mergedCentralApi,
    site_mappings: next.site_mappings ?? currentSettings.site_mappings ?? {},
    monitored_checks: Array.isArray(next.monitored_checks)
      ? next.monitored_checks
      : (currentSettings.monitored_checks || []),
    hardware_checks: Array.isArray(next.hardware_checks)
      ? next.hardware_checks
      : (currentSettings.hardware_checks || []),
    relay_enabled: next.relay_enabled ?? currentSettings.relay_enabled ?? 'off',
    relay_server_url: next.relay_server_url ?? currentSettings.relay_server_url ?? '',
    relay_spoke_name: next.relay_spoke_name ?? currentSettings.relay_spoke_name ?? '',
    relay_spoke_id: next.relay_spoke_id ?? currentSettings.relay_spoke_id ?? '',
    relay_poll_interval: next.relay_poll_interval ?? currentSettings.relay_poll_interval ?? 60,
    hub_isolation_timeout: next.hub_isolation_timeout ?? currentSettings.hub_isolation_timeout ?? ((next.hub_isolation_timeout_min ?? currentSettings.hub_isolation_timeout_min ?? 60) * 60), // Keep the server timeout in seconds so relay payloads and saves share one authoritative value.
    hub_isolation_timeout_min: next.hub_isolation_timeout_min ?? (next.hub_isolation_timeout != null ? Math.max(5, Math.round(Number(next.hub_isolation_timeout) / 60)) : currentSettings.hub_isolation_timeout_min ?? 60), // Maintain a minutes copy so the setup form can render and save the timeout without repeated conversion boilerplate.
    relay_api_key_configured: next.relay_api_key_configured ?? currentSettings.relay_api_key_configured ?? false,
    // hub_managed: true when this spoke is under hub control (set server-side on every config_update).
    // Used to lock locally-editable fields that the hub now owns (e.g. USB allowlist).
    hub_managed: next.hub_managed ?? currentSettings.hub_managed ?? false,
    repo_sync_interval: next.repo_sync_interval ?? currentSettings.repo_sync_interval ?? 300,
    usb_vidpids: next.usb_vidpids ?? currentSettings.usb_vidpids ?? '[]',
    usb_missing_timeout: next.usb_missing_timeout ?? currentSettings.usb_missing_timeout ?? '60',
    vm_image_1_template_id: next.vm_image_1_template_id ?? currentSettings.vm_image_1_template_id ?? '100',
    vm_image_1_template_spec: next.vm_image_1_template_spec ?? currentSettings.vm_image_1_template_spec ?? String(next.vm_image_1_template_id ?? currentSettings.vm_image_1_template_id ?? '100'),
    vm_image_2_template_id: next.vm_image_2_template_id ?? currentSettings.vm_image_2_template_id ?? '200',
    vm_image_2_template_spec: next.vm_image_2_template_spec ?? currentSettings.vm_image_2_template_spec ?? String(next.vm_image_2_template_id ?? currentSettings.vm_image_2_template_id ?? '200'),
    vm_image_1_pct: next.vm_image_1_pct ?? currentSettings.vm_image_1_pct ?? '50',
    usb_auto_provision: next.usb_auto_provision ?? currentSettings.usb_auto_provision ?? 'off',
    use_all_dongles: next.use_all_dongles ?? currentSettings.use_all_dongles ?? false,
    sim_phy: next.sim_phy ?? currentSettings.sim_phy ?? 'wireless',
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

function showToast(message, level = 'success') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position:fixed;top:72px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  const cls = level === 'error' ? 'error' : level === 'warn' ? 'warn' : level === 'info' ? 'info' : 'success';
  toast.className = `settings-message ${cls}`;
  toast.textContent = message;
  toast.style.cssText = 'min-width:240px;max-width:420px;box-shadow:0 4px 16px rgba(0,0,0,0.15);cursor:pointer;';
  toast.addEventListener('click', () => toast.remove());
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
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

function sendProxmoxCommand(action, vmid) {
  const args = vmid ? { vmid: parseInt(vmid, 10) } : {};
  return fetch('/api/commands', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: 'proxmox', action, args }),
  }).then((r) => r.json().then((data) => {
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  }));
}

async function triggerAgentUpdate() {
  const btn = document.getElementById('agent-update-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Updating…'; }
  try {
    await sendProxmoxCommand('update_agent');
    if (btn) { btn.textContent = '✓ Queued'; }
  } catch (e) {
    showToast('Failed to queue agent update: ' + e.message, 'error');
    if (btn) { btn.textContent = '⬆ Update Agent'; btn.disabled = false; }
    return;
  }
  setTimeout(() => {
    if (btn) { btn.textContent = '⬆ Update Agent'; btn.disabled = false; }
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
  renderProxmoxApproveState(
    Array.isArray(latestProxmoxData.pending_proxmox) ? latestProxmoxData.pending_proxmox : [],
    Array.isArray(latestProxmoxData.approved_proxmox) ? latestProxmoxData.approved_proxmox : []
  );

  const tabBtn = document.getElementById('tab-server-btn');
  const tabPanel = document.getElementById('tab-server');
  if (tabBtn) tabBtn.style.display = '';
  if (tabPanel) tabPanel.style.display = '';

  const updateBtn = document.getElementById('agent-update-btn');
  if (updateBtn && !updateBtn._bound) {
    updateBtn.addEventListener('click', triggerAgentUpdate);
    updateBtn._bound = true;
  }

  const node = latestProxmoxData.node || {};
  const setEl = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  setEl('server-node-name', node.hostname || 'Proxmox');
  setEl('server-cpu', node.cpu_percent != null && !Number.isNaN(Number(node.cpu_percent)) ? Number(node.cpu_percent).toFixed(1) : '—');
  const ramUsed  = node.mem_used_kb  ? fmtSizeKB(node.mem_used_kb)  : '—';
  const ramTotal = node.mem_total_kb ? fmtSizeKB(node.mem_total_kb) : '—';
  setEl('server-ram', `${ramUsed} / ${ramTotal}`);
  setEl('server-last-seen', formatRelativeTime(latestProxmoxData.last_seen));

  const agentVerPill = document.getElementById('server-agent-version-pill');
  const agentVer = latestProxmoxData.agent_version;
  if (agentVerPill) {
    agentVerPill.style.display = agentVer ? '' : 'none';
    agentVerPill.title = latestProxmoxData.pve_version
      ? `Proxmox agent version reported by the host. Cached locally so the badge survives a spoke restart. Host PVE version: ${latestProxmoxData.pve_version}.`
      : 'Proxmox agent version reported by the host. Cached locally so the badge survives a spoke restart.';
    setEl('server-agent-version', agentVer || '—');
  }

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
  const autoRecoveryPending = new Set(
    Array.isArray(latestProxmoxData.auto_recovery_pending) ? latestProxmoxData.auto_recovery_pending : []
  );

  const configuredTemplateIds = getConfiguredTemplateIds(currentSettings);

  // Categorise VMs: templates → sim clients (vmid > 90000, qemu) → containers (lxc) → other clients
  const templateVms = vms.filter((v) =>
    v.is_template === true || v.is_template === 'true' ||
    configuredTemplateIds.has(String(v.vmid))
  );
  const nonTemplateVms = vms.filter((v) => !templateVms.includes(v));
  const containerVms = nonTemplateVms.filter((v) => v.type === 'lxc');
  const qemuVms      = nonTemplateVms.filter((v) => v.type !== 'lxc');
  const simVms       = qemuVms.filter((v) => Number(v.vmid) > 90000);
  const nonSimQemu   = qemuVms.filter((v) => !simVms.includes(v));
  // T3: qemu VMs whose PCI passthrough addresses overlap with known T3 device addresses on this node
  const t3AddrSet = new Set((data?.t3_pci_devices || []).map(d => String(d.id || '').toLowerCase()));
  const iotVms    = t3AddrSet.size
    ? nonSimQemu.filter(v => (v.pci_passthrough_addrs || []).some(a => t3AddrSet.has(String(a).toLowerCase())))
    : [];
  const otherVms  = nonSimQemu.filter(v => !iotVms.includes(v));

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
      const statusDot = vm.status === 'running' ? '🟢' : vm.status === 'paused' ? '🟡' : '⚫';
      const memUsed  = vm.mem    ? fmtSize(Number(vm.mem)    * 1024 * 1024) : '—';
      const memTotal = vm.maxmem ? fmtSize(Number(vm.maxmem) * 1024 * 1024) : '—';
      const cpu = vm.cpu != null && !Number.isNaN(Number(vm.cpu)) ? Number(vm.cpu).toFixed(1) + '%' : '—';
      return `<tr class="vm-row-template">
        <td>${vm.vmid}</td>
        <td>${escHtml(vm.name || '—')}</td>
        <td>${cpu}</td>
        <td>${memUsed} / ${memTotal}</td>
        <td>${statusDot} ${vm.status || 'unknown'}</td>
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
    { action: 'reclone_vm',  label: '⎘',  title: 'Reclone'  },
    { action: 'delete_vm',   label: '✕',  title: 'Delete'   },
  ];

  // Build per-VM reclone status map: vmid → 'queued' | 'in_progress'
  const vmRecloneStatus = new Map();
  if (latestRecloneState?.status === 'running') {
    (latestRecloneState.log || []).forEach((e) => {
      if (e.status === 'queued' || e.status === 'in_progress') {
        vmRecloneStatus.set(Number(e.vmid), e.status);
      }
    });
    // current_vm always shown as in_progress if not already in the log
    if (latestRecloneState.current_vm != null) {
      const cid = Number(latestRecloneState.current_vm);
      if (!vmRecloneStatus.has(cid)) vmRecloneStatus.set(cid, 'in_progress');
    }
  }

  function _renderVmGroup(catKey, vmList) {
    const tbody  = document.getElementById(`server-vm-tbody-${catKey}`);
    const empty  = document.getElementById(`server-empty-${catKey}`);
    const thChk  = document.getElementById(`server-th-check-${catKey}`);
    if (!tbody) return;

    // Sort: in-flight states first (recloning/provisioning/deleting/queued), then stopped, then running by VMID
    const statusPriority = (vm) => {
      const rLog = vmRecloneStatus.get(Number(vm.vmid));
      if (rLog === 'in_progress') return 0;
      if (rLog === 'queued') return 1;
      if (['deleting', 'provisioning', 'cloning', 'configuring'].includes(vm.status)) return 2;
      if (vm.status !== 'running') return 3;
      return 4;
    };
    const sorted = [...vmList].sort((a, b) => {
      const pa = statusPriority(a), pb = statusPriority(b);
      if (pa !== pb) return pa - pb;
      return Number(a.vmid) - Number(b.vmid);
    });

    tbody.innerHTML = '';
    if (thChk) { thChk.disabled = sorted.length === 0; thChk.checked = false; }
    if (empty) empty.style.display = sorted.length ? 'none' : '';
    if (empty && !sorted.length && catKey === 'sim') {
      empty.textContent = latestProxmoxData.last_seen
        ? 'No Deployed VMs'
        : 'Waiting for Proxmox agent to check in…';
    }
    if (!sorted.length) return;

    sorted.forEach((vm) => {
      const recloneLog   = vmRecloneStatus.get(Number(vm.vmid));
      const isWebui      = webuiVmid != null && Number(vm.vmid) === webuiVmid;
      const baseStatusText = `${vm.status === 'running' ? '🟢' : vm.status === 'paused' ? '🟡' : '⚫'} ${vm.status || 'unknown'}`;
      let statusLabel;
      let statusTitle;
      if (recloneLog === 'in_progress') {
        statusLabel = '🔄 recloning…';
        statusTitle = 'Reclone is currently running for this VM.';
      } else if (recloneLog === 'queued') {
        statusLabel = '⏳ queued';
        statusTitle = 'VM is queued for the next reclone action.';
      } else if (vm.status === 'deleting') {
        statusLabel = '🔴 deleting…';
        statusTitle = 'VM delete is in progress.';
      } else if (vm.status === 'provisioning') {
        statusLabel = '🟡 provisioning…';
        statusTitle = 'VM is being provisioned and is not ready yet.';
      } else if (vm.status === 'cloning') {
        statusLabel = '🟡 cloning…';
        statusTitle = 'VM clone is in progress.';
      } else if (vm.status === 'configuring') {
        statusLabel = '🟡 configuring…';
        statusTitle = 'VM clone finished and guest configuration is still running.';
      } else {
        statusLabel = baseStatusText;
        statusTitle = `VM status: ${vm.status || 'unknown'}`;
      }
      const memUsed  = vm.mem    ? fmtSize(Number(vm.mem)    * 1024 * 1024) : '—';
      const memTotal = vm.maxmem ? fmtSize(Number(vm.maxmem) * 1024 * 1024) : '—';
      // Show CPU only for running VMs — stopped VMs always report 0 which is misleading
      const cpuVal = (vm.status === 'running') && vm.cpu != null && !Number.isNaN(Number(vm.cpu))
        ? Number(vm.cpu).toFixed(1) + '%' : '—';
      const recoveryBadge = autoRecoveryPending.has(Number(vm.vmid))
        ? ' <span class="badge badge-yellow" title="Guest-agent watchdog queued an auto-recovery reclone for this VM">↺ auto-recovery</span>'
        : '';
      const webuiBadge = isWebui
        ? ' <span class="badge badge-grey" title="This is the container running the dashboard — cannot be deleted">🔒 webui</span>'
        : '';

      const actionBtns = VM_ACTIONS.map((a) => {
        const disabled = (a.action === 'delete_vm' && isWebui) ? ' disabled title="Cannot delete the container running this service"' : ` title="${a.title}"`;
        return `<button class="btn-icon vm-action-btn" data-action="${a.action}" data-vmid="${vm.vmid}"${disabled}>${a.label}</button>`;
      }).join(' ');

      const tr = document.createElement('tr');
      tr.dataset.vmid = vm.vmid;
      tr.dataset.status = baseStatusText;
      tr.innerHTML = `
        <td><input type="checkbox" class="vm-check" data-vmid="${vm.vmid}"${isWebui ? ' disabled' : ''}></td>
        <td class="vm-status-cell" title="${escHtml(statusTitle)}">${statusLabel}</td>
        <td>${vm.vmid}</td>
        <td>${escHtml(vm.name || '—')}${recoveryBadge}${webuiBadge}</td>
        <td>${cpuVal}</td>
        <td>${memUsed} / ${memTotal}</td>
        <td>${actionBtns}</td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('.vm-action-btn:not([disabled])').forEach((btn) => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.action;
        const vmid = btn.dataset.vmid;
        if (action === 'delete_vm') {
          const vmName = btn.closest('tr')?.querySelector('td:nth-child(4)')?.textContent?.trim() || `VM ${vmid}`;
          if (!confirm(`Delete ${vmName} (VMID ${vmid})?\n\nThis will stop and permanently destroy the VM. This cannot be undone.`)) return;
        }
        sendProxmoxCommand(action, vmid)
          .then(() => showToast(`${btn.title} command sent for VM ${vmid}`, 'success'))
          .catch((err) => showToast(`Error: ${err.message}`, 'error'));
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

  _renderVmGroup('sim', simVms);
  _renderVmGroup('other', otherVms);
  _renderVmGroup('containers', containerVms);

  // T3 subtab: render IoT VMs
  const _vmStatusDot = (s) => `<span class="status-dot ${s === 'running' ? 'online' : 'offline'}" title="${escHtml(s)}"></span> ${escHtml(s)}`;
  const _t3Tbody = document.getElementById('server-t3-vm-tbody');
  if (_t3Tbody) {
    _t3Tbody.innerHTML = iotVms.length
      ? iotVms.map(v => `<tr>
          <td>${escHtml(String(v.vmid))}</td>
          <td>${escHtml(v.name || '—')}</td>
          <td>${escHtml(v.type || 'qemu')}</td>
          <td>${_vmStatusDot(v.status || 'unknown')}</td>
          <td>${escHtml((v.pci_passthrough_addrs || []).join(', ') || '—')}</td>
        </tr>`).join('')
      : `<tr><td colspan="5" class="empty-state">No IoT (T3) devices detected on this node.</td></tr>`;
  }
  document.querySelectorAll('.server-subtab[data-subtab="server-t3"]').forEach(btn => {
    btn.innerHTML = `IoT (T3) <span class="badge-count">${iotVms.length}</span>`;
  });

  // Other subtab: non-sim, non-IoT VMs + containers
  const _otherAll = [...otherVms, ...containerVms];
  const _otherTbody = document.getElementById('server-other-vm-tbody');
  if (_otherTbody) {
    _otherTbody.innerHTML = _otherAll.length
      ? _otherAll.map(v => `<tr>
          <td style="white-space:nowrap">${escHtml(String(v.vmid))}</td>
          <td>${escHtml(v.name || '—')}</td>
          <td style="white-space:nowrap">${escHtml(v.type || 'qemu')}</td>
          <td style="white-space:nowrap">${_vmStatusDot(v.status || 'unknown')}</td>
          <td></td>
        </tr>`).join('')
      : `<tr><td colspan="5" class="empty-state">No other VMs or containers.</td></tr>`;
  }
  document.querySelectorAll('.server-subtab[data-subtab="server-other"]').forEach(btn => {
    btn.innerHTML = `Other <span class="badge-count">${_otherAll.length}</span>`;
  });

  // 1-hour average pills
  const cpuAvgPill = document.getElementById('server-cpu-avg-pill');
  const memAvgPill = document.getElementById('server-mem-avg-pill');
  // Rolling average — updated with every sample (no need to wait for a full hour).
  // _resource_1h_average on the backend returns the mean of whatever samples exist in
  // the last 60-minute window, so data is available from the very first telemetry tick.
  // Show sample count in the tooltip so the user knows how much history backs the number.
  const _sampleCount = data?.resource_sample_count ?? 0;
  const _sampleAge = data?.resource_samples_started
    ? Math.round((Date.now() / 1000 - Number(data.resource_samples_started)) / 60)
    : 0;
  const _sampleContext = _sampleCount > 0
    ? `Rolling 1-hour average — ${_sampleCount} sample${_sampleCount !== 1 ? 's' : ''} over the last ${Math.min(_sampleAge, 60)} min.`
    : 'Collecting samples — average will appear with the first telemetry tick.';
  if (cpuAvgPill) {
    const _cpuVal = data?.cpu_1h_avg ?? data?.cpu_est_avg;
    if (_cpuVal != null) {
      cpuAvgPill.title = _sampleContext;
      cpuAvgPill.innerHTML = `📊 CPU avg: <span id="server-cpu-avg">${Number(_cpuVal).toFixed(1)}</span>%`;
    } else {
      cpuAvgPill.title = _sampleContext;
      cpuAvgPill.innerHTML = `📊 CPU avg: –`;
    }
  }
  if (memAvgPill) {
    const _memVal = data?.mem_1h_avg ?? data?.mem_est_avg;
    if (_memVal != null) {
      memAvgPill.title = _sampleContext;
      memAvgPill.innerHTML = `📊 Mem avg: <span id="server-mem-avg">${Number(_memVal).toFixed(1)}</span>%`;
    } else {
      memAvgPill.title = _sampleContext;
      memAvgPill.innerHTML = `📊 Mem avg: –`;
    }
  }

  // Reset select-all
  const selectAll = document.getElementById('server-select-all');
  if (selectAll) selectAll.checked = false;
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

function renderProxmoxApproveState(pending, approved) {
  const btn = document.getElementById('agent-approve-btn');
  const extraCard = document.getElementById('proxmox-extra-pending');
  const extraList = document.getElementById('proxmox-extra-pending-list');
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
    btn.textContent = `✓ Approve ${escHtml(first.hostname)}`;
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
  if (extraCard && extraList) {
    if (otherPending.length) {
      extraCard.classList.remove('hidden');
      extraList.innerHTML = otherPending.map((a) => {
        const enc = encodeURIComponent(String(a.hostname || ''));
        return `<strong>${escHtml(a.hostname)}</strong> `
          + `<button class="btn btn-secondary" style="font-size:11px;padding:2px 8px;" onclick="approveProxmoxAgent(decodeURIComponent('${enc}'))">Approve</button> `;
      }).join(' &nbsp; ');
    } else {
      extraCard.classList.add('hidden');
    }
  }
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
  // Local kill switch is driven by /api/init local_kill_switch (from simulation.conf),
  // NOT from WebUI settings — the settings object never contains kill_switch.
  if (repoUrlInput) repoUrlInput.value = settings.repo_url || repoUrlInput.value;
  if (branchInput && !branchInput.matches(':focus')) branchInput.value = settings.repo_branch || '';
  if (setupActiveBranch) setupActiveBranch.textContent = settings.repo_branch || '—';
  if (githubTokenStatus) githubTokenStatus.textContent = settings.github_token_configured ? '✓ Token configured' : 'Not configured';
  const centralApi = settings.central_api || defaultCentralApiSettings();
  setInputValueIfIdle(centralClassicUrlInput, centralApi.classic.url || '');
  setInputValueIfIdle(centralClassicUsernameInput, centralApi.classic.username || '');
  setInputValueIfIdle(centralCentralUrlInput, centralApi.central.url || '');
  setInputValueIfIdle(centralClientIdInput, centralApi.central.client_id || '');
  setInputValueIfIdle(centralCustomerIdInput, centralApi.central.customer_id || '');
  applyCentralModeUI(centralApi.mode || 'classic');

  const csStatus = document.getElementById('central-client-secret-status');
  if (centralClassicPasswordStatus) centralClassicPasswordStatus.textContent = centralApi.classic.password_configured ? '✓ Password configured — leave blank to keep current.' : '';
  if (csStatus) csStatus.textContent = centralApi.central.client_secret_configured ? '✓ Secret configured — leave blank to keep current.' : '';
  if (relayEnabledSelect && !relayEnabledSelect.matches(':focus')) relayEnabledSelect.value = settings.relay_enabled || 'off';
  setInputValueIfIdle(relayServerUrlInput, settings.relay_server_url || '');
  setInputValueIfIdle(relaySpokeName, settings.relay_spoke_name || '');
  setInputValueIfIdle(relayTenantHintInput, settings.relay_tenant_hint || '');
  setInputValueIfIdle(hubIsolationTimeoutInput, String(Math.max(5, Math.round(Number(settings.hub_isolation_timeout ?? ((settings.hub_isolation_timeout_min ?? 60) * 60)) / 60)))); // Convert the stored seconds timeout into minutes so the Hub setup input shows the editable safeguard value.
  const spokeIdDisplay = document.getElementById('relay-spoke-id-display');
  if (spokeIdDisplay) spokeIdDisplay.textContent = settings.relay_spoke_id || '—';
  const apikeyStatus = document.getElementById('relay-apikey-status');
  if (apikeyStatus) apikeyStatus.textContent = settings.relay_api_key_configured ? '✓ Received' : 'Pending approval';
  const relayIndicator = document.getElementById('relay-indicator');
  if (relayIndicator) {
    const relayOn = settings.relay_enabled === 'on' && settings.relay_server_url;
    relayIndicator.style.display = relayOn ? '' : 'none';
  }
  if (usbAutoProvisionInput) usbAutoProvisionInput.checked = settings.usb_auto_provision === 'on';
  if (useAllDonglesInput) useAllDonglesInput.checked = Boolean(settings.use_all_dongles);
  if (simPhyInput && !simPhyInput.matches(':focus')) simPhyInput.value = settings.sim_phy ?? 'wireless';
  if (usbMissingTimeoutInput && !usbMissingTimeoutInput.matches(':focus')) usbMissingTimeoutInput.value = settings.usb_missing_timeout ?? '60';
  if (vmImage1TemplateIdInput && !vmImage1TemplateIdInput.matches(':focus')) vmImage1TemplateIdInput.value = getTemplateSpecValue(settings, 1);
  if (vmImage2TemplateIdInput && !vmImage2TemplateIdInput.matches(':focus')) vmImage2TemplateIdInput.value = getTemplateSpecValue(settings, 2);
  updateTemplateSpecValidation();
  if (vmImage1PctInput && !vmImage1PctInput.matches(':focus')) vmImage1PctInput.value = settings.vm_image_1_pct ?? '50';
  if (vmSilentTimeoutInput && !vmSilentTimeoutInput.matches(':focus')) vmSilentTimeoutInput.value = settings.vm_silent_timeout ?? '24';
  const schedule = parseScheduleCron(settings.reclone_schedule_cron);
  if (recloneScheduleEnabledInput) recloneScheduleEnabledInput.checked = settings.reclone_schedule_enabled === 'on';
  if (recloneConcurrencyInput) recloneConcurrencyInput.value = settings.reclone_concurrency ?? '1';
  if (l1VlanStartInput && !l1VlanStartInput.matches(':focus')) l1VlanStartInput.value = settings.l1_vlan_start ?? '100';
  if (l1VlanEndInput && !l1VlanEndInput.matches(':focus')) l1VlanEndInput.value = settings.l1_vlan_end ?? '199';
  if (recloneScheduleDayInput && !recloneScheduleDayInput.matches(':focus')) recloneScheduleDayInput.value = schedule.day;
  if (recloneScheduleTimeInput && !recloneScheduleTimeInput.matches(':focus')) recloneScheduleTimeInput.value = schedule.time;
  renderUsbVidPidTable();
  renderIgnoredUsbList();
  renderIgnoredHostnamesList();
  renderSiteMappingsTable();
  renderSelectedChecksPreview();
  renderHwChecksPreview();
  if ((availableChecks.alerts.length || availableChecks.insights.length) && availableChecksContainer) {
    renderAvailableChecks();
  }
  renderCentralOverview();
  renderChecksList(); // Refresh sim tab whenever settings change (monitored checks may have changed)
  renderSpokeMonitoredItems();
  renderUsbSummary(latestProxmoxData);
  renderRecloneStatus(latestRecloneState || latestProxmoxData.reclone_state || {});
  renderAutoProvisionStatus();
  if (centralSiteDetailOpen) {
    renderSiteClients(centralSiteDetailOpen);
    renderSiteChecks(centralSiteDetailOpen, centralStatusData[centralSiteDetailOpen] || {});
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

  // Teams
  if (teamsEnabledToggle) teamsEnabledToggle.checked = !!notif.teams_enabled;
  setInputValueIfIdle(teamsWebhookUrl, notif.teams_webhook_url || '');
}

function showSettingsMessage(text, isError) {
  settingsMsg.textContent = text;
  settingsMsg.className = `settings-message ${isError ? 'error' : 'success'}`;
  clearTimeout(settingsMsg._timer);
  settingsMsg._timer = setTimeout(() => {
    settingsMsg.className = 'settings-message hidden';
  }, 5000);
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
    const token = githubTokenInput.value.trim();
    if (!token) return;
    try {
      await requestJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ github_token: token })
      });
      showSettingsMessage('GitHub token saved.', false);
      githubTokenInput.value = '';
    } catch (err) {
      showSettingsMessage(`Error: ${err.message}`, true);
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
  if (versionCurrent) versionCurrent.textContent = data.current_version ?? '—';
  if (versionAvailable) versionAvailable.textContent = data.available_version ?? '—';
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
    // If update started, applyVersionStatus via WS will drive state from here
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

function updateClientCount() {
  clientCount.textContent = `${clients.size} client${clients.size === 1 ? '' : 's'}`;
  if (emptyRow) emptyRow.style.display = clients.size > 0 ? 'none' : '';
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
  detailCell.colSpan = 10;
  detailRow.appendChild(detailCell);

  const statusCell = createCell('status-cell');
  const statusDot = document.createElement('span');
  statusDot.className = 'status-dot offline';
  statusCell.appendChild(statusDot);

  const hostnameCell = createCell('hostname-cell');
  const platformCell = createCell();
  const simIdCell = createCell();
  const ssidCell = createCell();
  const activeCell = createCell('badge-cell');
  const impactCell = createCell('impact-cell');
  const lastSeenCell = createCell();
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
    simIdCell,
    ssidCell,
    activeCell,
    impactCell,
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
    simIdCell,
    ssidCell,
    activeCell,
    impactCell,
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
  if (!activeSimulations || !activeSimulations.length) {
    container.textContent = '—';
    return;
  }

  activeSimulations.forEach((simulation) => {
    const badge = document.createElement('span');
    badge.className = badgeClass(simulation);
    badge.textContent = simulation;
    container.appendChild(badge);
  });
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
  refs.hostnameCell.textContent = merged.hostname || '—';
  refs.platformCell.textContent = merged.platform || '—';
  refs.simIdCell.textContent = merged.simulation_id || '—';
  refs.ssidCell.textContent = merged.connected_ssid || '—';
  renderBadges(refs.activeCell, merged.active_simulations || []);
  renderImpactCell(refs.impactCell, merged.active_simulations || []);
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

  updateClientCount();
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

async function requestText(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  if (!response.ok) {
    throw new Error(text || `HTTP ${response.status}`);
  }
  return text;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\x22/g, '&quot;')
    .replace(/\x27/g, '&#39;');
}

async function spokeChangePassword() {
  const current = document.getElementById('sp-pw-current')?.value || '';
  const newPw = document.getElementById('sp-pw-new')?.value || '';
  const confirm = document.getElementById('sp-pw-confirm')?.value || '';
  const msg = document.getElementById('sp-pw-msg');
  if (!newPw) {
    if (msg) { msg.textContent = 'Enter a new password.'; msg.className = 'form-msg error'; }
    return;
  }
  if (newPw !== confirm) {
    if (msg) { msg.textContent = 'Passwords do not match.'; msg.className = 'form-msg error'; }
    return;
  }
  try {
    await requestJson('/api/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current, new_password: newPw }),
    });
    if (msg) { msg.textContent = 'Password updated.'; msg.className = 'form-msg success'; }
    ['sp-pw-current', 'sp-pw-new', 'sp-pw-confirm'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
  } catch (error) {
    if (msg) { msg.textContent = error.message || 'Failed to change password.'; msg.className = 'form-msg error'; }
  }
}

async function loadSpokeLocalUsers() {
  const tbody = document.getElementById('sp-local-users-body');
  if (!tbody) return;
  try {
    const users = await requestJson('/api/auth/local-users');
    if (!Array.isArray(users) || !users.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="muted">No users configured.</td></tr>';
      return;
    }
    tbody.innerHTML = users.map((u) => `
      <tr>
        <td><strong>${escHtml(u.username)}</strong>${u.username === 'admin' ? ' <span class="badge badge-blue">primary</span>' : ''}</td>
        <td>${escHtml(u.role || 'admin')}</td>
        <td>${u.username !== 'admin' ? `<button class="btn btn-sm btn-danger sp-remove-user-btn" data-username="${escHtml(u.username)}" type="button">Remove</button>` : ''}</td>
      </tr>`).join('');
    tbody.querySelectorAll('.sp-remove-user-btn').forEach((button) => {
      button.addEventListener('click', () => deleteSpokeUser(button.dataset.username || ''));
    });
  } catch (_) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">Unable to load users.</td></tr>';
  }
}

async function addSpokeUser() {
  const username = document.getElementById('sp-new-username')?.value.trim() || '';
  const password = document.getElementById('sp-new-password')?.value || '';
  const role = document.getElementById('sp-new-role')?.value || 'admin';
  const msg = document.getElementById('sp-user-msg');
  if (!username || !password) {
    if (msg) { msg.textContent = 'Username and password required.'; msg.className = 'form-msg error'; }
    return;
  }
  try {
    await requestJson('/api/auth/local-users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, role }),
    });
    if (msg) { msg.textContent = `User "${username}" added.`; msg.className = 'form-msg success'; }
    const usernameInput = document.getElementById('sp-new-username');
    const passwordInput = document.getElementById('sp-new-password');
    if (usernameInput) usernameInput.value = '';
    if (passwordInput) passwordInput.value = '';
    loadSpokeLocalUsers();
  } catch (error) {
    if (msg) { msg.textContent = error.message || 'Failed to add user.'; msg.className = 'form-msg error'; }
  }
}

async function deleteSpokeUser(username) {
  if (!username) return;
  try {
    await requestJson(`/api/auth/local-users/${encodeURIComponent(username)}`, { method: 'DELETE' });
    showNotification(`User "${username}" removed.`, 'success');
    loadSpokeLocalUsers();
  } catch (error) {
    showNotification(error.message || 'Failed to remove user.', 'error');
  }
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

function getTemplateSpecValue(settings = currentSettings, slot = 1) {
  const specKey = slot === 1 ? 'vm_image_1_template_spec' : 'vm_image_2_template_spec';
  const idKey = slot === 1 ? 'vm_image_1_template_id' : 'vm_image_2_template_id';
  if (settings && Object.prototype.hasOwnProperty.call(settings, specKey)) {
    return String(settings[specKey] ?? '').trim();
  }
  return String(settings?.[idKey] ?? (slot === 1 ? '100' : '200')).trim();
}

function parseVmidSpec(spec, label = 'Template VMIDs') {
  const normalized = String(spec ?? '').trim();
  if (!normalized) return [];
  const vmids = new Set();
  for (const rawPart of normalized.split(',')) {
    const part = rawPart.trim();
    if (!part) continue;
    const rangeMatch = part.match(/^(\d+)-(\d+)$/);
    if (rangeMatch) {
      const start = Number.parseInt(rangeMatch[1], 10);
      const end = Number.parseInt(rangeMatch[2], 10);
      if (start > end) throw new Error(`${label}: range start must be less than or equal to end (${part})`);
      if ((end - start) > 1000) throw new Error(`${label}: range too large (${part}); max span is 1001 VMIDs`);
      for (let vmid = start; vmid <= end; vmid += 1) vmids.add(vmid);
      continue;
    }
    if (/^\d+$/.test(part)) {
      vmids.add(Number.parseInt(part, 10));
      continue;
    }
    throw new Error(`${label}: invalid token "${part}"`);
  }
  return [...vmids].sort((a, b) => a - b);
}

function getConfiguredTemplateIds(settings = currentSettings) {
  const ids = new Set();
  [1, 2].forEach((slot) => {
    try {
      parseVmidSpec(getTemplateSpecValue(settings, slot), slot === 1 ? 'VM Image 1 Template VMIDs' : 'VM Image 2 Template VMIDs')
        .forEach((vmid) => ids.add(String(vmid)));
    } catch {
      const fallbackKey = slot === 1 ? 'vm_image_1_template_id' : 'vm_image_2_template_id';
      const fallback = String(settings?.[fallbackKey] ?? '').trim();
      if (fallback) ids.add(fallback);
    }
  });
  return ids;
}

function getTemplateSpecValidationError(spec1 = vmImage1TemplateIdInput?.value ?? getTemplateSpecValue(currentSettings, 1), spec2 = vmImage2TemplateIdInput?.value ?? getTemplateSpecValue(currentSettings, 2)) {
  let parsed1 = [];
  let parsed2 = [];
  try {
    parsed1 = parseVmidSpec(spec1, 'VM Image 1 Template VMIDs');
    parsed2 = parseVmidSpec(spec2, 'VM Image 2 Template VMIDs');
  } catch (error) {
    return error.message;
  }
  if (!parsed1.length || !parsed2.length) return '';
  const parsed2Set = new Set(parsed2);
  const overlap = parsed1.filter((vmid) => parsed2Set.has(vmid));
  if (!overlap.length) return '';
  return `VM Image 1 and VM Image 2 overlap at VMID(s): ${overlap.slice(0, 5).join(', ')}${overlap.length > 5 ? '…' : ''}`;
}

function updateTemplateSpecValidation() {
  const message = getTemplateSpecValidationError();
  [vmImage1TemplateIdInput, vmImage2TemplateIdInput].forEach((input) => {
    if (input) input.setCustomValidity(message);
  });
  if (templateVmidSpecError) showInlineMessage(templateVmidSpecError, message, Boolean(message), 0);
  return message;
}

function formatUiDate(value) {
  if (!value) return '—';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function renderUsbVidPidTable() {
  if (!usbVidPidTbody) return;
  usbVidPidTbody.innerHTML = '';
  // When hub-managed, the hub owns this list — disable all local edits so users know to change it from the hub.
  const hubOwned = Boolean(currentSettings.hub_managed);
  const devices = parseJsonList(currentSettings.usb_vidpids);
  devices.forEach((device) => {
    const tr = document.createElement('tr');
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'btn-icon';
    removeBtn.textContent = '✕';
    // Disable removal when hub-managed — only the hub may modify the allowlist.
    removeBtn.disabled = hubOwned;
    if (hubOwned) removeBtn.title = 'Managed by Hub — edit from the Hub UI';
    if (!hubOwned) removeBtn.addEventListener('click', () => removeVidPid(device.vidpid));
    tr.innerHTML = `<td>${escHtml(device.vidpid || '—')}</td><td>${escHtml(device.type || 'wireless')}</td><td>${escHtml(device.label || '—')}</td>`;
    const actionTd = document.createElement('td');
    actionTd.appendChild(removeBtn);
    tr.appendChild(actionTd);
    usbVidPidTbody.appendChild(tr);
  });
  // Show or hide the "Managed by Hub" notice banner above the table.
  const hubNotice = document.getElementById('usb-hub-managed-notice');
  if (hubNotice) hubNotice.style.display = hubOwned ? '' : 'none';
  // Lock or unlock the Add Device form based on hub management state.
  const addForm = document.getElementById('usb-add-device-form');
  if (addForm) addForm.style.display = hubOwned ? 'none' : '';
}

function renderIgnoredUsbList() {
  if (!usbIgnoredList) return;
  usbIgnoredList.innerHTML = '';
  // When hub-managed, the hub owns the ignored list too — local removes are disabled.
  const hubOwned = Boolean(currentSettings.hub_managed);
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
    // When hub-managed, disable the remove button so the hub remains the authority.
    button.disabled = hubOwned;
    if (hubOwned) button.title = 'Managed by Hub — edit from the Hub UI';
    if (!hubOwned) {
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
    }
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
  currentSettings.vm_image_1_template_id = String(data.image1_template_id ?? currentSettings.vm_image_1_template_id ?? '100');
  currentSettings.vm_image_1_template_spec = String(data.image1_template_spec ?? currentSettings.vm_image_1_template_spec ?? currentSettings.vm_image_1_template_id ?? '100');
  currentSettings.vm_image_2_template_id = String(data.image2_template_id ?? currentSettings.vm_image_2_template_id ?? '200');
  currentSettings.vm_image_2_template_spec = String(data.image2_template_spec ?? currentSettings.vm_image_2_template_spec ?? currentSettings.vm_image_2_template_id ?? '200');
  currentSettings.vm_image_1_pct = String(data.image1_pct ?? currentSettings.vm_image_1_pct ?? '50');
  currentSettings.usb_auto_provision = data.auto_provision || 'off';
  currentSettings.use_all_dongles = Boolean(data.use_all_dongles);
  currentSettings.sim_phy = ['wireless', 'ethernet', 'any'].includes(data.sim_phy) ? data.sim_phy : (currentSettings.sim_phy || 'wireless');
  if (usbAutoProvisionInput) usbAutoProvisionInput.checked = currentSettings.usb_auto_provision === 'on';
  if (useAllDonglesInput) useAllDonglesInput.checked = currentSettings.use_all_dongles;
  if (simPhyInput && !simPhyInput.matches(':focus')) simPhyInput.value = currentSettings.sim_phy;
  if (usbMissingTimeoutInput && !usbMissingTimeoutInput.matches(':focus')) usbMissingTimeoutInput.value = currentSettings.usb_missing_timeout;
  if (vmImage1TemplateIdInput && !vmImage1TemplateIdInput.matches(':focus')) vmImage1TemplateIdInput.value = getTemplateSpecValue(currentSettings, 1);
  if (vmImage2TemplateIdInput && !vmImage2TemplateIdInput.matches(':focus')) vmImage2TemplateIdInput.value = getTemplateSpecValue(currentSettings, 2);
  updateTemplateSpecValidation();
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
  const vmImage1TemplateSpec = String(vmImage1TemplateIdInput?.value ?? getTemplateSpecValue(currentSettings, 1)).trim();
  const vmImage2TemplateSpec = String(vmImage2TemplateIdInput?.value ?? getTemplateSpecValue(currentSettings, 2)).trim();
  const parsedImage1 = parseVmidSpec(vmImage1TemplateSpec, 'VM Image 1 Template VMIDs');
  const parsedImage2 = parseVmidSpec(vmImage2TemplateSpec, 'VM Image 2 Template VMIDs');
  return {
    usb_vidpids: currentSettings.usb_vidpids,
    usb_missing_timeout: String(usbMissingTimeoutInput?.value || currentSettings.usb_missing_timeout || '60'),
    vm_image_1_template_id: String(parsedImage1[0] ?? currentSettings.vm_image_1_template_id ?? '100'),
    vm_image_1_template_spec: vmImage1TemplateSpec,
    vm_image_2_template_id: String(parsedImage2[0] ?? currentSettings.vm_image_2_template_id ?? '200'),
    vm_image_2_template_spec: vmImage2TemplateSpec,
    vm_image_1_pct: String(vmImage1PctInput?.value ?? currentSettings.vm_image_1_pct ?? '50'),
    usb_auto_provision: usbAutoProvisionInput?.checked ? 'on' : 'off',
    use_all_dongles: Boolean(useAllDonglesInput?.checked),
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

function renderTableRowsIncremental(tbody, items, keyFn, rowHtmlFn) {
  if (!tbody) return;
  const existingRows = new Map();
  tbody.querySelectorAll('tr[data-key]').forEach((row) => {
    existingRows.set(row.dataset.key || '', row);
  });

  items.forEach((item, index) => {
    const key = String(keyFn(item, index) ?? index);
    const rowHtml = rowHtmlFn(item, index);
    let row = existingRows.get(key);
    if (!row) {
      row = document.createElement('tr');
      row.dataset.key = key;
    }
    if (row.innerHTML !== rowHtml) row.innerHTML = rowHtml;
    tbody.appendChild(row);
    existingRows.delete(key);
  });

  existingRows.forEach((row) => row.remove());
}

// ── IoT / T3 Device panel ────────────────────────────────────────────────────
// Renders the "IoT (T3)" subtab in the VM Server tab.
// Reads proxmox_state.t3_pci_devices — a list of PCI devices on this Proxmox
// node that match the T3 target VID:PIDs (currently 168c:0034).
// Shows a table row per device and a count pill; an empty state when none found.
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

  const svcLabel = svcActive != null ? (svcActive ? '🟢 Service running' : '🔴 Service stopped') : null;
  const autoLabel = autoUseAll != null ? (autoUseAll ? '⚡ Auto-Use All: ON' : '⚫ Auto-Use All: OFF') : null;
  const countLabel = connected
    ? (count > 0 ? `🔌 ${count} device${count !== 1 ? 's' : ''} — ${inUse} in use, ${available} available` : '⚫ No VH devices detected')
    : '⚫ Not connected to VH server';
  pills.innerHTML = [svcLabel, autoLabel, countLabel].filter(Boolean)
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
      html += `<div style="margin-bottom:16px;">
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

  // Running VM stats pill
  const allVms = Array.isArray(latestProxmoxData.vms) ? latestProxmoxData.vms : [];
  const runningVms = allVms.filter((v) => v.status === 'running' && !v.is_template);
  const usbStatPills = document.getElementById('usb-vm-stat-pills');
  if (usbStatPills) {
    const simRunning = runningVms.filter((v) => v.name && v.name.startsWith('client-sim-')).length;
    const totalRunning = runningVms.length;
    usbStatPills.innerHTML = `<span class="server-stat-pill" title="Total non-template VMs currently running">🟢 ${totalRunning} running VM${totalRunning !== 1 ? 's' : ''}</span>`
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

  const presentUsb = Array.isArray(latestProxmoxData.present_usb) ? latestProxmoxData.present_usb : [];
  const presentBusSet = new Set(presentUsb.map((item) => String(item?.bus_path || '').trim()).filter(Boolean));
  const vmMap = new Map(allVms.map((v) => [Number(v.vmid), v]));

  // Small tables, but incremental updates avoid visible flicker during polling.
  renderTableRowsIncremental(
    usbSummaryTbody,
    certified,
    (device, index) => String(device?.vidpid || device?.label || index),
    (device) => {
      const entries = usbState.filter((item) => (item.vidpid || '').toLowerCase() === String(device.vidpid || '').toLowerCase());
      const missingEntries = entries.filter((item) => item.missing_since && !presentBusSet.has(String(item?.bus_path || '').trim()));
      const activeEntries = entries.filter((item) => !missingEntries.includes(item));
      const missing = missingEntries.length;
      const total = presentUsb.filter((item) => (item.vidpid || '').toLowerCase() === String(device.vidpid || '').toLowerCase()).length;
      const activeVmHtml = activeEntries.length === 0 ? '—' : activeEntries.map((e) => {
        const vm = vmMap.get(Number(e.vmid));
        const name = escHtml(vm?.name || `VM ${e.vmid}`);
        const dot = vm?.status === 'running' ? '🟢' : '⚫';
        return `<div style="white-space:nowrap">${dot} ${name}</div>`;
      }).join('');
      const missingHtml = missing
        ? `<div class="usb-missing-list">${missingEntries.map((item) => `<div class="usb-missing-item">VM ${item.vmid} · <span data-missing-until="${Number(item.missing_since) + missingTimeoutSeconds}"></span></div>`).join('')}</div>`
        : '—';
      const deviceLabel = escHtml(device.label || device.vidpid || '—');
      const deviceVidPid = escHtml(device.vidpid || '—');
      const usbType = escHtml(device.type || 'wireless');
      return `
        <td>${deviceLabel}</td>
        <td>${deviceVidPid}</td>
        <td class="usb-type-${usbType}">${usbType}</td>
        <td>${activeVmHtml}</td>
        <td>${missingHtml}</td>
        <td>${total}</td>
      `;
    }
  );

  renderTableRowsIncremental(
    unknownUsbTbody,
    unknownUsb,
    (device, index) => String(device?.bus_path || device?.vidpid || index),
    (device) => {
      const vid = escHtml(device.vidpid || '');
      const nameLabel = escHtml(device.name || device.bus_path || 'Unknown device');
      return `
        <td>${nameLabel}</td>
        <td>${vid || '—'}</td>
        <td class="usb-actions">
          <button type="button" class="btn btn-secondary btn-small" data-action="certify" data-vidpid="${vid}" data-name="${nameLabel}">Add to certified</button>
          <button type="button" class="btn btn-secondary btn-small" data-action="ignore" data-vidpid="${vid}">Ignore</button>
        </td>
      `;
    }
  );
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
  // The progress panel is only meaningful while a run is actively in progress.
  // Once the run ends (completed / failed / interrupted), the state resets to
  // idle and the tile should disappear rather than lingering at the last
  // progress value (e.g. "3 / 9 VMs (33%)").
  const isActive = status === 'running';
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

  const iconMap = { completed: '✅', failed: '❌', in_progress: '⏳', queued: '🕐' };
  const logEntries = (state.log || []).slice().reverse();
  if (logEntries.length === 0 && status !== 'idle') {
    recloneVmLog.innerHTML = `<div class="muted" style="padding:8px 0;font-size:13px;">No VMs processed yet.</div>`;
  } else {
    recloneVmLog.innerHTML = logEntries.map((entry) => {
      const entryName = escHtml(entry.name || `VM ${entry.vmid}`);
      const entryStatus = escHtml(entry.status || 'unknown');
      return `
      <div class="log-entry">
        <span>${iconMap[entry.status] || '•'}</span>
        <span>${entryName}</span>
        <span class="muted">${entryStatus}</span>
        <span class="muted">${formatUiDate(entry.timestamp)}</span>
      </div>
    `;
    }).join('');
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
    arLog.innerHTML = autoLog.slice().reverse().map((entry) => {
      const entryName = escHtml(entry.name || `VM ${entry.vmid}`);
      return `
      <div class="log-entry">
        <span>${iconMap[entry.status] || '↺'}</span>
        <span>${entryName}</span>
        <span class="muted">auto-recovery</span>
        <span class="muted">${formatUiDate(entry.timestamp)}</span>
      </div>
    `;
    }).join('');
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

function renderAutoProvisionStatus() {
  // ── VM page status bar (right side of tab nav) ─────────────────────────────
  const bar = document.getElementById('autoprov-status-bar');
  if (bar) {
    const usbState = Array.isArray(latestProxmoxData.usb_state) ? latestProxmoxData.usb_state : [];
    const autoProv = currentSettings.usb_auto_provision === 'on';
    const iconEl = document.getElementById('autoprov-status-icon');
    const textEl = document.getElementById('autoprov-status-text');
    bar.classList.remove('hidden', 'is-active', 'is-idle');

    if (!autoProv) {
      bar.classList.add('is-idle');
      if (iconEl) iconEl.textContent = '⏹';
      if (textEl) textEl.textContent = 'Auto-Provisioning: Not Running';
    } else {
      const total = usbState.length;
      const provisioning = usbState.filter((e) => e.prov_status === 'provisioning');
      const activeUsb    = usbState.filter((e) => e.prov_status === 'active');

      // Cross-reference: "active" USB entries whose VM is not yet running in Proxmox
      const vms = Array.isArray(latestProxmoxData.vms) ? latestProxmoxData.vms : [];
      const runningVmids = new Set(vms.filter((v) => v.status === 'running').map((v) => Number(v.vmid)));
      const startingUp = activeUsb.filter((e) => e.vmid != null && !runningVmids.has(Number(e.vmid)));
      const fullyActive = activeUsb.length - startingUp.length;

      if (total === 0) {
        bar.classList.add('is-idle');
        if (iconEl) iconEl.textContent = '📋';
        if (textEl) textEl.textContent = 'Auto-Provisioning: No USB devices tracked';
      } else if (provisioning.length > 0 || startingUp.length > 0) {
        bar.classList.add('is-active');
        if (iconEl) iconEl.textContent = '⏳';
        const parts = [];
        if (provisioning.length > 0) parts.push(`${provisioning.length} cloning`);
        if (startingUp.length > 0) parts.push(`${startingUp.length} starting up`);
        parts.push(`${fullyActive} / ${total} active`);
        if (textEl) textEl.textContent = `Auto-Provisioning: ${parts.join(' · ')}`;
      } else {
        bar.classList.add('is-idle');
        if (iconEl) iconEl.textContent = '✅';
        if (textEl) textEl.textContent = `Auto-Provisioning: All ${total} VMs active`;
      }
    }
  }

  // ── Right-side live panel ───────────────────────────────────────────────────
  const liveBadge   = document.getElementById('autoprov-live-badge');
  const liveSummary = document.getElementById('autoprov-live-summary');
  const logEl       = document.getElementById('auto-prov-log');
  if (!liveSummary || !logEl) return;

  const usbState = Array.isArray(latestProxmoxData.usb_state) ? latestProxmoxData.usb_state : [];
  const autoProv = currentSettings.usb_auto_provision === 'on';
  const missingTimeoutMins = parseInt(latestProxmoxData.missing_timeout_mins, 10) || 60;
  const vms = Array.isArray(latestProxmoxData.vms) ? latestProxmoxData.vms : [];
  const runningVmids = new Set(vms.filter((v) => v.status === 'running').map((v) => Number(v.vmid)));

  if (!autoProv) {
    if (liveBadge) { liveBadge.textContent = 'Off'; liveBadge.className = 'badge badge-grey'; }
    liveSummary.innerHTML = `<div class="muted" style="font-size:13px;">Auto-Provisioning is disabled. Enable it in Setup → USB.</div>`;
    logEl.innerHTML = '';
    return;
  }

  // In-flight entries only: provisioning, starting-up, missing, tearing_down
  const inFlight = usbState.filter((e) => {
    if (e.prov_status === 'provisioning' || e.prov_status === 'tearing_down' || e.prov_status === 'missing') return true;
    if (e.prov_status === 'active' && e.vmid != null && !runningVmids.has(Number(e.vmid))) return true;
    return false;
  });

  // Badge
  if (liveBadge) {
    const total = usbState.length;
    if (inFlight.length > 0) {
      liveBadge.textContent = `${inFlight.length} in progress`; liveBadge.className = 'badge badge-blue';
    } else if (total === 0) {
      liveBadge.textContent = 'Idle'; liveBadge.className = 'badge badge-grey';
    } else {
      liveBadge.textContent = 'All settled'; liveBadge.className = 'badge badge-green';
    }
  }

  // Summary line from last completed provisioning/teardown event
  const summary = latestProxmoxData.prov_summary;
  if (summary && summary.at) {
    const when = new Date(summary.at * 1000).toLocaleString();
    const verb = summary.action === 'provisioned' ? 'provisioned' : 'deleted';
    const colour = summary.action === 'provisioned' ? '#16a34a' : '#dc2626';
    liveSummary.innerHTML = `<div style="font-size:12px;color:${colour};margin-bottom:6px;">
      ${summary.count} VM${summary.count !== 1 ? 's' : ''} ${verb} · ${when}</div>`;
  } else {
    liveSummary.innerHTML = '';
  }

  // In-flight VM rows
  if (inFlight.length === 0) {
    logEl.innerHTML = `<div class="muted" style="padding:6px 0;font-size:13px;">No active provisioning work.</div>`;
    return;
  }

  const now = Date.now() / 1000;
  logEl.innerHTML = inFlight.map((e) => {
    const isStarting = e.prov_status === 'active';
    const icon  = isStarting ? '🔄' : e.prov_status === 'provisioning' ? '⏳' : e.prov_status === 'tearing_down' ? '🗑️' : '⚠️';
    const label = isStarting ? 'Starting Up' : e.prov_status === 'provisioning' ? 'Cloning' : e.prov_status === 'tearing_down' ? 'Tearing Down' : 'USB Missing';
    const name  = escHtml(e.name || `USB ${e.bus_path || ''}`);
    let detail = '';
    if (e.prov_status === 'missing' && e.missing_since) {
      const elapsedMins = Math.round((now - e.missing_since) / 60);
      const remainMins  = Math.max(0, missingTimeoutMins - elapsedMins);
      detail = `<span class="muted">${elapsedMins}m elapsed · tears down in ~${remainMins}m</span>`;
    } else if (e.prov_status === 'tearing_down') {
      detail = `<span class="muted">Destroying VM…</span>`;
    } else if (e.prov_status === 'provisioning') {
      detail = `<span class="muted">Cloning &amp; configuring…</span>`;
    } else if (isStarting) {
      detail = `<span class="muted">VM booting…</span>`;
    }
    return `<div class="log-entry">
        <span>${icon}</span><span>VM ${e.vmid ?? '—'}</span>
        <span class="muted">${name}</span><span>${label}</span>${detail}
      </div>`;
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
  const classicPassword = centralClassicPasswordInput?.value ?? '';
  if (classicPassword) payload.classic.password = classicPassword;
  const clientSecret = centralClientSecretInput?.value ?? '';
  if (clientSecret) payload.central.client_secret = clientSecret;
  return payload;
}

function resetCentralSecretInputs() {
  if (centralClassicPasswordInput) centralClassicPasswordInput.value = '';
  if (centralClientSecretInput) centralClientSecretInput.value = '';
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
  const challenge = document.getElementById('spoke-acme-challenge')?.value || 'http-01';
  const isDns = challenge === 'dns-01';
  document.getElementById('spoke-acme-dns-section')?.classList.toggle('hidden', !isDns);
  if (isDns) {
    const provider = document.getElementById('spoke-acme-dns-provider')?.value || 'cloudflare';
    document.getElementById('spoke-acme-cloudflare-fields')?.classList.toggle('hidden', provider !== 'cloudflare');
    document.getElementById('spoke-acme-he-fields')?.classList.toggle('hidden', provider !== 'hurricane_electric');
  }
}

function renderSpokeAcmeStatus(certInfo = {}, cfg = {}) {
  const container = document.getElementById('spoke-acme-cert-status');
  if (!container) return;
  if (!certInfo || certInfo.source === 'none') {
    container.innerHTML = `
      <div class="setup-status-item"><span class="setup-status-label">Certificate</span><span class="setup-status-value">Not configured</span></div>
      <div class="setup-status-item"><span class="setup-status-label">Challenge</span><span class="setup-status-value">${escapeHtml(cfg.challenge || 'http-01')}</span></div>
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
  const setValue = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.value = value || '';
  };
  setValue('spoke-acme-domain', data.domain || '');
  setValue('spoke-acme-email', data.email || '');
  setValue('spoke-acme-ca', data.ca || 'letsencrypt');
  setValue('spoke-acme-challenge', data.challenge || 'http-01');
  setValue('spoke-acme-dns-provider', data.dns_provider || 'cloudflare');
  const enabled = document.getElementById('spoke-acme-enabled');
  if (enabled) enabled.checked = !!data.enabled;
  const tlsEnabled = document.getElementById('spoke-tls-enabled');
  if (tlsEnabled) tlsEnabled.checked = data.spoke_tls === 'on';
  const token = document.getElementById('spoke-acme-cf-token');
  if (token) token.value = '';
  toggleSpokeAcmeDnsSection();
  renderSpokeAcmeStatus(data.cert_info || {}, data);
}

async function saveSpokeAcmeConfig() {
  const payload = {
    enabled: !!document.getElementById('spoke-acme-enabled')?.checked,
    domain: document.getElementById('spoke-acme-domain')?.value.trim() || '',
    email: document.getElementById('spoke-acme-email')?.value.trim() || '',
    ca: document.getElementById('spoke-acme-ca')?.value || 'letsencrypt',
    challenge: document.getElementById('spoke-acme-challenge')?.value || 'http-01',
    dns_provider: document.getElementById('spoke-acme-dns-provider')?.value || '',
    dns_credentials: {
      cf_api_token: document.getElementById('spoke-acme-cf-token')?.value || '',
      he_ddns_key: document.getElementById('spoke-acme-he-ddns-key')?.value || '',
    },
    spoke_tls: document.getElementById('spoke-tls-enabled')?.checked ? 'on' : 'off'
  };
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
    const token = document.getElementById('spoke-acme-cf-token');
    if (token) token.value = '';
    const heKey = document.getElementById('spoke-acme-he-ddns-key');
    if (heKey) heKey.value = '';
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
  try {
    const data = await requestJson('/api/central/status');
    mergeSettings({
      site_mappings: data.site_mappings || {},
      monitored_checks: data.monitored_checks || []
    });
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
    if (centralEmpty) {
      centralEmpty.textContent = `Could not load Central status: ${error.message}`;
      centralEmpty.classList.remove('hidden');
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
  saveOverridesButton.className = 'btn btn-secondary';
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
  actions.appendChild(saveOverridesButton);

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
  root.querySelectorAll('[data-config-section][data-config-key], [data-section][data-key]').forEach((input) => {
    const section = input.dataset.configSection || input.dataset.section;
    const key = input.dataset.configKey || input.dataset.key;
    if (!section || !key) return;
    if (!payloads[section]) payloads[section] = {};
    payloads[section][key] = input.type === 'checkbox'
      ? (input.checked ? (input.dataset.on || 'on') : (input.dataset.off || 'off'))
      : input.value;
  });
  return payloads;
}

const SIM_FIXED_SECTION_ORDER = ['simulation', 'server', 'address', ...Array.from({ length: 10 }, (_, i) => `s${i}`)];
const SIM_SLOT_KEYS = ['wsite', 'ssid', 'ssidpw', 'dhcp_fail', 'dns_fail', 'assoc_fail', 'port_flap', 'ping_test', 'download', 'www_traffic', 'iperf', 'sim_phy', 'l1'];
const SIM_SELECT_FIELDS = { sim_phy: ['wireless', 'ethernet'], sim_load: ['100', '75', '50', '25', '0'] };

function simIsBoolValue(value) {
  return BOOL_VALUE_SET.has(String(value ?? '').trim().toLowerCase());
}

function simBoolPair(value) {
  const normalized = String(value ?? '').trim().toLowerCase();
  if (normalized === 'yes' || normalized === 'no') return ['yes', 'no'];
  if (normalized === 'true' || normalized === 'false') return ['true', 'false'];
  return ['on', 'off'];
}

function simFieldLabel(key) {
  return _fmtConfigKey(String(key || ''));
}

function simSectionKeys(section, values = {}, keyOrder = {}) {
  const ordered = keyOrder?.[section] || [];
  if (String(section).match(/^s\d+$/)) {
    const seen = new Set();
    const keys = [];
    SIM_SLOT_KEYS.forEach((key) => {
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

function renderSimField(section, key, rawValue = '') {
  const value = String(rawValue ?? '');
  const label = simFieldLabel(key);
  if (simIsBoolValue(value)) {
    const [onValue, offValue] = simBoolPair(value);
    return `
      <label class="toggle-label" style="justify-content:space-between;align-items:center;padding:10px 12px;border:1px solid var(--border);border-radius:10px;gap:12px;">
        <span>${escHtml(label)}</span>
        <input type="checkbox" data-section="${escHtml(section)}" data-key="${escHtml(key)}" data-on="${escHtml(onValue)}" data-off="${escHtml(offValue)}"${value.toLowerCase() === onValue ? ' checked' : ''}>
      </label>
    `;
  }
  if (SIM_SELECT_FIELDS[key]) {
    return `
      <label class="form-group">
        <span class="form-label">${escHtml(label)}</span>
        <select class="form-input" data-section="${escHtml(section)}" data-key="${escHtml(key)}">
          ${SIM_SELECT_FIELDS[key].map((option) => `<option value="${escHtml(option)}"${option === value ? ' selected' : ''}>${escHtml(option)}</option>`).join('')}
        </select>
      </label>
    `;
  }
  const inputType = PW_KEY_RE.test(key) ? 'password' : 'text';
  return `
    <label class="form-group">
      <span class="form-label">${escHtml(label)}</span>
      <input class="form-input" type="${inputType}" value="${escHtml(value)}" data-section="${escHtml(section)}" data-key="${escHtml(key)}">
    </label>
  `;
}

function renderSimSection(section, values = {}, { open = false } = {}) {
  const keys = simSectionKeys(section, values, spokeSimConfState.keyOrder);
  const isSlot = Boolean(String(section).match(/^s\d+$/));
  const title = isSlot ? `Simulation S${section.slice(1)}` : `[${section}]`;
  const textKeys = keys.filter((key) => !simIsBoolValue(String(values[key] ?? '')) && !SIM_SELECT_FIELDS[key]);
  const selectKeys = keys.filter((key) => SIM_SELECT_FIELDS[key]);
  const boolKeys = keys.filter((key) => simIsBoolValue(String(values[key] ?? '')));
  const minColWidth = isSlot ? '160px' : '220px';
  const inputKeys = [...textKeys, ...selectKeys];
  const inputFields = inputKeys.map((key) => renderSimField(section, key, values[key] ?? '')).join('');
  const boolFields = boolKeys.map((key) => {
    const value = String(values[key] ?? '');
    const [onValue, offValue] = simBoolPair(value);
    const checked = value.toLowerCase() === onValue ? ' checked' : '';
    const label = simFieldLabel(key);
    return `<label style="display:flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap;font-size:0.875rem;">
      <input type="checkbox" data-section="${escHtml(section)}" data-key="${escHtml(key)}" data-on="${escHtml(onValue)}" data-off="${escHtml(offValue)}"${checked}>
      <span>${escHtml(label)}</span>
    </label>`;
  }).join('');
  const body = keys.length ? `
      <div class="setup-form setup-section-gap" data-sim-section-form="${escHtml(section)}">
        ${inputKeys.length ? `<div class="form-grid" style="grid-template-columns:repeat(auto-fit,minmax(${minColWidth},1fr));gap:8px;">${inputFields}</div>` : ''}
        ${boolKeys.length ? `<div style="display:flex;flex-wrap:wrap;gap:8px 20px;padding:6px 0;">${boolFields}</div>` : ''}
        <div class="form-actions" style="display:flex;justify-content:flex-end;">
          <button type="button" class="btn btn-primary btn-small" data-save-sim-section="${escHtml(section)}">Save [${escHtml(section)}] to GitHub</button>
        </div>
        <div class="settings-message hidden" data-sim-section-message="${escHtml(section)}" role="alert"></div>
      </div>`
    : `
      <div class="setup-form setup-section-gap" data-sim-section-form="${escHtml(section)}">
        <div class="muted">No fields found in this section.</div>
        <div class="form-actions" style="display:flex;justify-content:flex-end;">
          <button type="button" class="btn btn-primary btn-small" data-save-sim-section="${escHtml(section)}">Save [${escHtml(section)}] to GitHub</button>
        </div>
        <div class="settings-message hidden" data-sim-section-message="${escHtml(section)}" role="alert"></div>
      </div>`;
  return `
    <details class="setup-card setup-section-gap"${open ? ' open' : ''}>
      <summary style="cursor:pointer;font-weight:600;">${escHtml(title)}</summary>
      ${body}
    </details>
  `;
}

function spokeSimOrderedSections() {
  const sections = spokeSimConfState.sections || {};
  const orderedSections = [];
  const seen = new Set();
  SIM_FIXED_SECTION_ORDER.forEach((section) => {
    if (section === 'simulation' || section === 'server' || section === 'address') {
      if (Object.prototype.hasOwnProperty.call(sections, section)) {
        orderedSections.push(section);
        seen.add(section);
      }
      return;
    }
    orderedSections.push(section);
    seen.add(section);
  });
  (spokeSimConfState.sectionOrder || []).forEach((section) => {
    if (!seen.has(section)) {
      orderedSections.push(section);
      seen.add(section);
    }
  });
  Object.keys(sections).forEach((section) => {
    if (!seen.has(section)) orderedSections.push(section);
  });
  return orderedSections;
}

function bindSpokeSimConfigPanel(container) {
  if (!container) return;
  container.querySelectorAll('[data-refresh-spoke-sim]').forEach((button) => {
    button.addEventListener('click', () => loadSpokeSimConf(true).catch(() => {}));
  });
  container.querySelectorAll('[data-save-sim-section]').forEach((button) => {
    button.addEventListener('click', async () => {
      const section = button.dataset.saveSimSection;
      const form = container.querySelector(`[data-sim-section-form="${CSS.escape(section)}"]`);
      const msg = container.querySelector(`[data-sim-section-message="${CSS.escape(section)}"]`);
      if (!section || !form || !msg) return;
      button.disabled = true;
      const originalText = button.textContent;
      button.textContent = 'Saving…';
      try {
        const updates = collectSectionedConfigState(form)[section] || {};
        const result = await requestJson('/api/config/simulation', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ section, updates }),
        });
        showInlineMessage(msg, result?.pushed ? `[${section}] saved and pushed to GitHub.` : `[${section}] saved. GitHub push skipped.`, false, 7000);
        await loadSpokeSimConf(true);
      } catch (error) {
        showInlineMessage(msg, `Error: ${error.message}`, true, 7000);
      } finally {
        button.disabled = false;
        button.textContent = originalText;
      }
    });
  });
}

function renderSpokeSimConfigPanel() {
  const containers = [configSimulationPanel, setupSimulationPanel].filter(Boolean);
  if (!containers.length) return;
  const fetched = spokeSimConfState.fetchedAt ? new Date(spokeSimConfState.fetchedAt).toLocaleString() : '—';
  const infoBar = `
    <section class="setup-card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
        <div>
          <div style="font-weight:600;">configs/simulation.conf</div>
          <div class="muted" style="font-size:0.85rem;">Last loaded: ${escHtml(fetched)}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <button type="button" class="btn btn-secondary btn-small" data-refresh-spoke-sim="true">Refresh</button>
        </div>
      </div>
    </section>
  `;

  containers.forEach((container) => {
    if (spokeSimConfState.loading) {
      container.innerHTML = `${infoBar}<div class="empty-state">Loading simulation.conf…</div>`;
      bindSpokeSimConfigPanel(container);
      return;
    }
    if (spokeSimConfState.error) {
      container.innerHTML = `${infoBar}<section class="setup-card"><div class="empty-state">${escHtml(spokeSimConfState.error)}</div></section>`;
      bindSpokeSimConfigPanel(container);
      return;
    }
    const sections = spokeSimConfState.sections || {};
    const orderedSections = spokeSimOrderedSections();
    container.innerHTML = `${infoBar}<div data-spoke-sim-config-form>
      ${orderedSections.map((section, index) => renderSimSection(section, sections[section] || {}, { open: index === 0 })).join('')}
    </div>`;
    bindSpokeSimConfigPanel(container);
  });
}

async function loadSpokeSimConf(force = false) {
  if (!force && spokeSimConfState.loaded) {
    renderSpokeSimConfigPanel();
    return spokeSimConfState;
  }
  if (spokeSimConfState.loading) return spokeSimConfState;
  spokeSimConfState = { ...spokeSimConfState, loading: true, error: null };
  renderSpokeSimConfigPanel();
  try {
    const content = await requestText('/api/config');
    const parsed = parseSpokeIni(content || '');
    spokeSimConfState = {
      ...parsed,
      loaded: true,
      loading: false,
      rawContent: content || '',
      fetchedAt: new Date().toISOString(),
      error: null,
    };
  } catch (error) {
    spokeSimConfState = { ...spokeSimConfState, loaded: false, loading: false, error: error.message };
  }
  renderSpokeSimConfigPanel();
  return spokeSimConfState;
}

async function loadConfigEditor(force = false) {
  return loadSpokeSimConf(force);
}

// ── User-overrides INI parser/serializer ─────────────────────────────────────
function parseSpokeIni(text) {
  const sections = {};
  const sectionOrder = [];
  const keyOrder = {};
  let current = null;
  for (const rawLine of (text || '').split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith(';') || line.startsWith('#')) continue;
    const secMatch = line.match(/^\[(.+)\]$/);
    if (secMatch) {
      current = secMatch[1];
      if (!sections[current]) {
        sections[current] = {};
        sectionOrder.push(current);
        keyOrder[current] = [];
      }
      continue;
    }
    if (!current) continue;
    const eqIdx = line.indexOf('=');
    if (eqIdx <= 0) continue;
    const key = line.slice(0, eqIdx).trim();
    const value = line.slice(eqIdx + 1).trim();
    if (!Object.prototype.hasOwnProperty.call(sections[current], key)) keyOrder[current].push(key);
    sections[current][key] = value;
  }
  return { sections, sectionOrder, keyOrder };
}

function serializeSpokeIni(sections, sectionOrder, keyOrder) {
  return sectionOrder.map((section) => {
    const keys = keyOrder[section] || Object.keys(sections[section] || {});
    const body = keys.map((key) => `${key} = ${sections[section]?.[key] ?? ''}`).join('\n');
    return `[${section}]\n${body}`;
  }).join('\n\n') + (sectionOrder.length ? '\n' : '');
}

// ── Spoke user-overrides state ────────────────────────────────────────────────
let spokeUserOverridesState = { sections: {}, sectionOrder: [], keyOrder: {}, loaded: false, loading: false, error: null };
let spokeUserOverridesSearch = '';
let spokeUomState = null;

function getSpokeUserOverrideTemplate() {
  const sections = spokeUserOverridesState.sections || {};
  const sectionOrder = spokeUserOverridesState.sectionOrder || [];
  const keyOrder = spokeUserOverridesState.keyOrder || {};
  const templateOrder = [];
  const seen = new Set();
  const sampleValues = {};

  sectionOrder.forEach((section) => {
    (keyOrder[section] || Object.keys(sections[section] || {})).forEach((key) => {
      if (seen.has(key)) return;
      seen.add(key);
      templateOrder.push(key);
      sampleValues[key] = sections[section]?.[key];
    });
  });

  if (!templateOrder.length) {
    templateOrder.push('wsite', 'ssid', 'ssidpw', 'dhcp_fail', 'kill_switch', 'sim_load');
    sampleValues.dhcp_fail = 'off';
    sampleValues.kill_switch = 'off';
    sampleValues.sim_load = '100';
  }

  const values = {};
  templateOrder.forEach((key) => {
    const sample = sampleValues[key];
    if (_isBoolVal(sample) || key === 'dhcp_fail' || key === 'kill_switch') {
      values[key] = 'off';
    } else if (key === 'sim_load') {
      values[key] = sample ? String(sample) : '100';
    } else {
      values[key] = '';
    }
  });

  return { values, order: templateOrder };
}

async function loadSpokeUserOverridesConf(force = false) {
  if (!force && spokeUserOverridesState.loaded) return spokeUserOverridesState;
  if (spokeUserOverridesState.loading) return spokeUserOverridesState;
  spokeUserOverridesState.loading = true;
  spokeUserOverridesState.error = null;
  renderSpokeUserOverridesEditor();
  try {
    const data = await requestJson('/api/config/user-overrides-conf');
    const parsed = parseSpokeIni(data.content || '');
    spokeUserOverridesState = { ...parsed, loaded: true, loading: false, error: null };
  } catch (error) {
    spokeUserOverridesState = { ...spokeUserOverridesState, loading: false, loaded: false, error: error.message };
  }
  renderSpokeUserOverridesEditor();
  return spokeUserOverridesState;
}

async function saveSpokeUserOverrides() {
  const { sections, sectionOrder, keyOrder } = spokeUserOverridesState;
  const content = serializeSpokeIni(sections, sectionOrder, keyOrder);
  return requestJson('/api/config/user-overrides-conf', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

function applySpokeUserOverrideSearch() {
  const term = spokeUserOverridesSearch;
  const panel = document.getElementById('config-user-overrides');
  if (!panel) return;
  const cards = panel.querySelectorAll('[data-override-username]');
  let shown = 0;
  cards.forEach((card) => {
    const match = !term || card.dataset.overrideUsername.toLowerCase().includes(term);
    card.style.display = match ? '' : 'none';
    if (match) shown += 1;
  });
  const countEl = document.getElementById('spoke-uo-search-count');
  if (countEl) countEl.textContent = term ? `${shown} of ${cards.length} shown` : '';
}

function renderSpokeUserOverrideCard(username, values) {
  const card = document.createElement('div');
  card.className = 'setup-card setup-section-gap';
  card.dataset.overrideUsername = username;

  const cardHdr = document.createElement('div');
  cardHdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;';
  const title = document.createElement('h3');
  title.style.margin = '0';
  title.textContent = username;
  const delBtn = document.createElement('button');
  delBtn.className = 'btn btn-danger';
  delBtn.style.cssText = 'padding:4px 10px;font-size:0.8rem;';
  delBtn.textContent = '✕ Remove';
  cardHdr.appendChild(title);
  cardHdr.appendChild(delBtn);
  card.appendChild(cardHdr);

  const form = document.createElement('div');
  form.className = 'setup-form';
  const tracked = { ...values };
  const orderedKeys = spokeUserOverridesState.keyOrder[username] || Object.keys(values);
  const textKeys = orderedKeys.filter((key) => !_isBoolVal(values[key]));
  const boolKeys = orderedKeys.filter((key) => _isBoolVal(values[key]));

  if (textKeys.length) {
    const fieldGrid = document.createElement('div');
    fieldGrid.className = 'config-field-grid';
    textKeys.forEach((key) => {
      const inputType = key === 'sim_load' ? 'number' : (PW_KEY_RE.test(key) ? 'password' : 'text');
      const { group, input } = buildConfigInput(
        { section: username, key, type: inputType },
        values[key],
      );
      if (key === 'sim_load') {
        input.min = '0';
        input.max = '100';
        input.step = '1';
      }
      const lbl = group.querySelector('label');
      if (lbl) lbl.textContent = _fmtConfigKey(key);
      input.addEventListener('input', () => { tracked[key] = input.value.trim(); });
      fieldGrid.appendChild(group);
    });
    form.appendChild(fieldGrid);
  }

  if (boolKeys.length) {
    const h3 = document.createElement('h3');
    h3.textContent = 'Flags';
    form.appendChild(h3);
    const grid = document.createElement('div');
    grid.className = 'toggle-grid';
    boolKeys.forEach((key) => {
      const toggle = buildConfigToggle({ section: username, key }, values[key]);
      const text = toggle.querySelector('.toggle-label');
      if (text) text.textContent = _fmtConfigKey(key);
      const input = toggle.querySelector('input');
      input?.addEventListener('change', () => { tracked[key] = input.checked ? 'on' : 'off'; });
      grid.appendChild(toggle);
    });
    form.appendChild(grid);
  }

  const actions = document.createElement('div');
  actions.className = 'form-actions';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = 'Save';
  actions.appendChild(saveBtn);
  form.appendChild(actions);

  const msg = document.createElement('div');
  msg.className = 'settings-message hidden';
  form.appendChild(msg);

  delBtn.addEventListener('click', async () => {
    if (!window.confirm(`Remove overrides for "${username}"?`)) return;
    const snapshot = {
      sections: { ...spokeUserOverridesState.sections },
      sectionOrder: [...spokeUserOverridesState.sectionOrder],
      keyOrder: { ...spokeUserOverridesState.keyOrder },
    };
    const idx = spokeUserOverridesState.sectionOrder.indexOf(username);
    if (idx >= 0) spokeUserOverridesState.sectionOrder.splice(idx, 1);
    delete spokeUserOverridesState.sections[username];
    delete spokeUserOverridesState.keyOrder[username];
    renderSpokeUserOverridesEditor();
    try {
      await saveSpokeUserOverrides();
      showNotification(`Removed overrides for ${username}.`);
    } catch (error) {
      spokeUserOverridesState.sections = snapshot.sections;
      spokeUserOverridesState.sectionOrder = snapshot.sectionOrder;
      spokeUserOverridesState.keyOrder = snapshot.keyOrder;
      renderSpokeUserOverridesEditor();
      showNotification(`Failed to remove ${username}: ${error.message}`, 'error');
    }
  });

  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      spokeUserOverridesState.sections[username] = { ...tracked };
      const result = await saveSpokeUserOverrides();
      showInlineMessage(msg, result?.pushed ? 'Saved and pushed to GitHub.' : 'Saved locally.', false, 5000);
    } catch (error) {
      showInlineMessage(msg, `Error: ${error.message}`, true, 7000);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });

  card.appendChild(form);
  return card;
}

function renderSpokeUserOverridesEditor() {
  const panel = document.getElementById('config-user-overrides');
  if (!panel) return;
  panel.textContent = '';

  const hdr = document.createElement('div');
  hdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;';
  const hdrLeft = document.createElement('div');
  const h2 = document.createElement('h2');
  h2.style.margin = '0';
  h2.textContent = 'User Overrides';
  const p = document.createElement('p');
  p.style.cssText = 'margin:4px 0 0;color:var(--text-secondary);font-size:0.85rem;';
  p.textContent = 'Per-user simulation profile overrides stored in user-overrides.conf';
  hdrLeft.appendChild(h2);
  hdrLeft.appendChild(p);
  const addBtn = document.createElement('button');
  addBtn.className = 'btn btn-primary';
  addBtn.textContent = '+ Add User';
  addBtn.addEventListener('click', () => openSpokeUserOverrideModal(null));
  hdr.appendChild(hdrLeft);
  hdr.appendChild(addBtn);
  panel.appendChild(hdr);

  if (spokeUserOverridesState.loading) {
    const loading = document.createElement('p');
    loading.className = 'muted';
    loading.textContent = 'Loading…';
    panel.appendChild(loading);
    return;
  }
  if (spokeUserOverridesState.error) {
    const error = document.createElement('p');
    error.style.color = 'var(--error)';
    error.textContent = `Error: ${spokeUserOverridesState.error}`;
    panel.appendChild(error);
    return;
  }

  const { sections, sectionOrder } = spokeUserOverridesState;
  if (sectionOrder.length > 5) {
    const searchRow = document.createElement('div');
    searchRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:12px;';
    const input = document.createElement('input');
    input.type = 'search';
    input.placeholder = 'Search users…';
    input.className = 'form-input';
    input.style.maxWidth = '280px';
    input.value = spokeUserOverridesSearch;
    input.addEventListener('input', () => {
      spokeUserOverridesSearch = input.value.trim().toLowerCase();
      applySpokeUserOverrideSearch();
    });
    const count = document.createElement('span');
    count.id = 'spoke-uo-search-count';
    count.style.cssText = 'font-size:0.82rem;color:var(--text-secondary);';
    searchRow.appendChild(input);
    searchRow.appendChild(count);
    panel.appendChild(searchRow);
  }

  if (!sectionOrder.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No user overrides configured. Click "+ Add User" to create one.';
    panel.appendChild(empty);
    return;
  }

  sectionOrder.forEach((username) => {
    panel.appendChild(renderSpokeUserOverrideCard(username, sections[username] || {}));
  });
  if (spokeUserOverridesSearch) applySpokeUserOverrideSearch();
}

function openSpokeUserOverrideModal(prefill) {
  ensureSpokeUserOverrideModal();
  spokeUomState = { prefill };
  const body = document.getElementById('spoke-uom-body');
  if (!body) return;
  body.textContent = '';

  const fg = document.createElement('div');
  fg.className = 'form-group';
  const lbl = document.createElement('label');
  lbl.textContent = 'Username';
  lbl.className = 'form-label';
  const inp = document.createElement('input');
  inp.id = 'spoke-uom-username';
  inp.className = 'form-input';
  inp.placeholder = 'e.g. jsmith';
  inp.type = 'text';
  fg.appendChild(lbl);
  fg.appendChild(inp);
  body.appendChild(fg);

  const hint = document.createElement('p');
  hint.className = 'form-hint';
  hint.style.margin = '0';
  hint.textContent = 'A new card will be created with the standard override fields.';
  body.appendChild(hint);

  const overlay = document.getElementById('spoke-user-override-modal-overlay');
  if (!overlay) return;
  overlay.style.display = '';
  overlay.classList.remove('hidden');
  inp.focus();
}

function closeSpokeUserOverrideModal() {
  const overlay = document.getElementById('spoke-user-override-modal-overlay');
  if (overlay) {
    overlay.style.display = 'none';
    overlay.classList.add('hidden');
  }
  spokeUomState = null;
}

function ensureSpokeUserOverrideModal() {
  if (document.getElementById('spoke-uom-save')?.dataset.wired) return;
  const saveBtn = document.getElementById('spoke-uom-save');
  const cancelBtn = document.getElementById('spoke-uom-cancel');
  const overlay = document.getElementById('spoke-user-override-modal-overlay');
  if (saveBtn) {
    saveBtn.dataset.wired = '1';
    saveBtn.addEventListener('click', async () => {
      const inp = document.getElementById('spoke-uom-username');
      const username = (inp?.value || '').trim();
      if (!username) {
        inp?.focus();
        return;
      }
      if (spokeUserOverridesState.sections[username]) {
        window.alert(`User "${username}" already exists.`);
        return;
      }
      const template = getSpokeUserOverrideTemplate();
      spokeUserOverridesState.sections[username] = { ...template.values };
      spokeUserOverridesState.sectionOrder.push(username);
      spokeUserOverridesState.keyOrder[username] = [...template.order];
      closeSpokeUserOverrideModal();
      renderSpokeUserOverridesEditor();
    });
  }
  cancelBtn?.addEventListener('click', closeSpokeUserOverrideModal);
  overlay?.addEventListener('click', (event) => {
    if (event.target === overlay) closeSpokeUserOverrideModal();
  });
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

function updateCmdTargetDropdown(clientList = [...clients.values()]) {
  if (!cmdTarget) return;
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
  config_update:        'Hub config update',
  config_clear:         'Hub config clear',
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
  if (target === 'proxmox') return 'Proxmox Host';
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
    descTd.style.wordBreak = 'break-word';
    const descSpan = document.createElement('span');
    descSpan.textContent = cmdDescription(cmd);
    descTd.appendChild(descSpan);
    if (cmd.message) {
      const msgSpan = document.createElement('div');
      msgSpan.style.cssText = 'margin-top:4px;font-size:0.82em;color:var(--muted);word-break:break-word;white-space:normal;';
      msgSpan.textContent = cmd.message;
      descTd.appendChild(msgSpan);
    }
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

requestJson('/api/commands').then(renderCommandTable).catch(() => {});

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
  if (document.getElementById('server-services')?.classList.contains('active')) {
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
    renderRecloneStatus(message);
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
    updateClientCount();
    updateCmdTargetDropdown([]);
  }
}

function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl = new URL(`${protocol}://${window.location.host}/ws`);
  if (window.__SPOKE_WS_TOKEN__) wsUrl.searchParams.set('token', window.__SPOKE_WS_TOKEN__);
  socket = new WebSocket(wsUrl.toString());
  setWsStatus(false, 'Connecting');

  socket.addEventListener('open', () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    setWsStatus(true, 'Connected');
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
      if (checkUpdateBtn) {
        checkUpdateBtn.disabled = false;
        checkUpdateBtn.textContent = '🔄 Check & Update Now';
      }
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
const simTopPanels = ['simtop-checks', 'simtop-hardware', 'simtop-clients', 'simtop-insights'];

function renderSpokeMonitoredItems() {
  const checks   = (currentSettings.monitored_checks || []);
  const DEST = {
    site:    'spoke-monitored-checks-content',
    alert:   'spoke-monitored-hardware-content',
    client:  'spoke-monitored-clients-content',
    insight: 'spoke-monitored-insights-content',
  };
  const LABELS = { site: 'Monitored Sites', alert: 'Monitored Alerts', client: 'Monitored Clients', insight: 'Monitored Insights' };
  const byType = { site: [], alert: [], client: [], insight: [] };
  checks.forEach((c) => { if (byType[c.type]) byType[c.type].push(c); });

  const makeTable = (items, type) => {
    if (!items.length) return '';
    const rows = items.map((item) => {
      const isOk = !item.consecutive_failures;
      const dot  = isOk ? 'check-dot dot-pass' : 'check-dot dot-fail';
      const lastSeen = item.last_seen ? new Date(item.last_seen * 1000).toLocaleString() : '—';
      const badge = isOk
        ? `<span class="badge badge-success">Reporting</span>`
        : `<span class="badge badge-failure">Missing (${item.consecutive_failures || 0})</span>`;
      return `<tr>
        <td><span class="${dot}"></span></td>
        <td><strong>${escHtml(item.name || item.identifier || '—')}</strong></td>
        <td>${escHtml(item.identifier || '—')}</td>
        <td>${badge}</td>
        <td style="color:var(--muted);font-size:0.8rem;">${escHtml(lastSeen)}</td>
      </tr>`;
    }).join('');
    return `<div class="setup-card" style="margin-bottom:1rem;">
      <h4 style="margin:0 0 0.5rem;color:var(--muted);font-size:0.85rem;text-transform:uppercase;letter-spacing:0.05em;">${escHtml(LABELS[type])}</h4>
      <div style="overflow-x:auto;"><table class="data-table">
        <thead><tr><th></th><th>Name</th><th>Identifier</th><th>Status</th><th>Last Seen</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div></div>`;
  };

  Object.entries(DEST).forEach(([type, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = makeTable(byType[type], type);
  });

  const insightsEl = document.getElementById('spoke-monitored-insights-content');
  if (insightsEl && !byType.insight.length) {
    insightsEl.innerHTML = `<div class="empty-state">No monitored insights. Add insights via <strong>Central API → Insights</strong> using the Monitor button.</div>`;
  }
}

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
  renderSpokeMonitoredItems();
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
];
const SIM_TEST_LABELS = {
  dns_fail:   'DNS Fail',
  assoc_fail: 'Association Fail',
  dhcp_fail:  'DHCP Fail',
  port_flap:  'Port Flap',
  iperf:      'iPerf',
  www_traffic:'Web Traffic',
  download:   'Download',
  ping_test:  'Ping Test',
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
    if (simClientsList) simClientsList.innerHTML = `<div class="sim-client-card" style="color:#e74c3c">Error loading clients: ${err.message}</div>`;
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
      showToast('Client history cleared.', 'success');
    } catch (err) {
      showToast(`Purge failed: ${err.message}`, 'error');
    } finally {
      purgeHistoryBtn.disabled = false;
      purgeHistoryBtn.textContent = '🗑 Purge History';
    }
  });
}

if (simTabButton) {
  simTabButton.addEventListener('click', () => {
    loadSimulations();
  });
}

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

if (centralTabButton) {
  centralTabButton.addEventListener('click', () => {
    if (!centralStatusInitialized || !Object.keys(centralStatusData).length) {
      loadCentralStatus();
    } else {
      renderCentralOverview();
    }
  });
}

if (configTabButton) {
  configTabButton.addEventListener('click', () => {
    activateConfigSubtab('config-simulation-panel');
    loadSpokeSimConf().catch(() => {});
  });
}

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
      if (btn.dataset.subtab === 'setup-tls') loadSpokeAcmeSettings().catch(() => {});
    });
  });
}

document.getElementById('sp-pw-save-btn')?.addEventListener('click', spokeChangePassword);
document.getElementById('sp-add-user-btn')?.addEventListener('click', addSpokeUser);

if (setupTabButton) {
  setupTabButton.addEventListener('click', () => {
    activateSetupSubtab('setup-github');
    if (!currentSettings.repo_url && !currentSettings.repo_branch) {
      loadSettings();
    }
  });
}

async function _autoSaveRelay() {
  const pskInput = document.getElementById('relay-psk-input');
  const payload = {
    relay_enabled: relayEnabledSelect?.value || 'off',
    relay_server_url: relayServerUrlInput?.value?.trim() || '',
    relay_spoke_name: relaySpokeName?.value?.trim() || '',
    relay_tenant_hint: relayTenantHintInput?.value?.trim() || '',
    hub_isolation_timeout: (parseInt(hubIsolationTimeoutInput?.value, 10) || 60) * 60, // Convert the minutes input back to seconds so the API stores the safeguard in the server's source-of-truth unit.
  };
  const pskVal = pskInput?.value?.trim();
  if (pskVal) payload.relay_onboarding_psk = pskVal;

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
[relayServerUrlInput, relaySpokeName, relayTenantHintInput, hubIsolationTimeoutInput].forEach((el) => { // Include the timeout input so leaving that field saves the safeguard alongside the other hub settings.
  if (el) el.addEventListener('blur', _autoSaveRelay); // Auto-save the timeout on blur so the setup form persists the safeguard with the same behavior as the other hub fields.
});

// Registration diagnostics button
const relayDiagBtn = document.getElementById('relay-diag-btn');
const relayDiagPanel = document.getElementById('relay-diag-panel');
if (relayDiagBtn) {
  relayDiagBtn.addEventListener('click', async () => {
    relayDiagBtn.disabled = true;
    relayDiagBtn.textContent = '⏳ Running…';
    try {
      const d = await requestJson('/api/relay/diag');
      if (!relayDiagPanel) return;
      relayDiagPanel.classList.remove('hidden');

      // Config check
      const cfg = d.config || {};
      const cfgLines = [
        `relay_enabled : ${cfg.relay_enabled}`,
        `server_url    : ${cfg.server_url}`,
        `spoke_name    : ${cfg.spoke_name}`,
        `hostname      : ${cfg.hostname}`,
        `spoke_id     : ${cfg.spoke_id}`,
        `api_key       : ${cfg.api_key_configured ? '✅ set' : '❌ not set'}`,
        `tenant_id     : ${cfg.tenant_id}`,
      ].join('\n');
      const cfgEl = document.getElementById('relay-diag-config');
      if (cfgEl) cfgEl.textContent = cfgLines;

      // Reachability
      const reach = d.reachability || {};
      const reachEl = document.getElementById('relay-diag-reach');
      if (reachEl) {
        const icon = reach.ok ? '✅' : '❌';
        reachEl.textContent = `${icon} ${reach.tested_url}\n${reach.detail || ''}`;
        reachEl.style.color = reach.ok ? 'var(--success,#22c55e)' : 'var(--error,#ef4444)';
      }

      // Log
      const log = d.log || [];
      const logCountEl = document.getElementById('relay-diag-log-count');
      if (logCountEl) logCountEl.textContent = String(log.length);
      const logEl = document.getElementById('relay-diag-log');
      if (logEl) {
        if (!log.length) {
          logEl.textContent = '(no registration attempts recorded yet — relay may not have been enabled or synced)';
        } else {
          logEl.textContent = log.map(e => {
            const rest = Object.entries(e).filter(([k]) => k !== 'ts' && k !== 'event')
              .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ');
            return `[${e.ts}] ${e.event}  ${rest}`;
          }).join('\n');
        }
      }
    } catch (err) {
      const logEl = document.getElementById('relay-diag-log');
      if (logEl) logEl.textContent = `Error fetching diagnostics: ${err}`;
      if (relayDiagPanel) relayDiagPanel.classList.remove('hidden');
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
  const validationError = updateTemplateSpecValidation();
  if (validationError) {
    showInlineMessage(msgEl, validationError, true, 0);
    return;
  }
  try {
    currentSettings.use_all_dongles = Boolean(useAllDonglesInput?.checked);
    const payload = collectUsbSettingsPayload();
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    currentSettings.vm_image_1_template_id = payload.vm_image_1_template_id;
    currentSettings.vm_image_1_template_spec = payload.vm_image_1_template_spec;
    currentSettings.vm_image_2_template_id = payload.vm_image_2_template_id;
    currentSettings.vm_image_2_template_spec = payload.vm_image_2_template_spec;
    showInlineMessage(msgEl, 'Saved.', false);
  } catch (err) {
    showInlineMessage(msgEl, `Error: ${err.message}`, true);
  }
}

async function _autoSaveSimPhy(msgEl) {
  const simPhy = ['wireless', 'ethernet', 'any'].includes(simPhyInput?.value) ? simPhyInput.value : 'wireless';
  try {
    await requestJson('/api/config/simulation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: 'simulation', updates: { sim_phy: simPhy } }),
    });
    currentSettings.sim_phy = simPhy;
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
if (useAllDonglesInput) useAllDonglesInput.addEventListener('change', () => _autoSaveUsb(usbSettingsMsg));
if (simPhyInput) simPhyInput.addEventListener('change', () => _autoSaveSimPhy(usbSettingsMsg));
[usbMissingTimeoutInput, vmImage1PctInput].forEach((el) => {
  if (el) el.addEventListener('blur', () => _autoSaveUsb(usbSettingsMsg));
});
[vmImage1TemplateIdInput, vmImage2TemplateIdInput].forEach((el) => {
  if (!el) return;
  el.addEventListener('input', updateTemplateSpecValidation);
  el.addEventListener('blur', () => _autoSaveUsb(usbSettingsMsg));
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
  return {
    email_enabled: emailEnabledToggle?.checked ?? false,
    smtp_host:     smtpHost?.value.trim() || '',
    smtp_port:     parseInt(smtpPort?.value, 10) || 587,
    smtp_user:     smtpUser?.value.trim() || '',
    smtp_password: smtpPassword?.value || '',   // only sent if non-blank
    smtp_from:     smtpFrom?.value.trim() || '',
    smtp_to:       (smtpTo?.value || '').split(',').map(s => s.trim()).filter(Boolean),
  };
}

async function _autoSaveEmail() {
  const payload = collectEmailPayload();
  if (!payload.smtp_password) delete payload.smtp_password;
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notifications: payload })
    });
    showInlineMessage(emailNotifMsg, 'Saved.', false, 1500);
  } catch (err) {
    showInlineMessage(emailNotifMsg, `Error: ${err.message}`, true);
  }
}

if (emailEnabledToggle) emailEnabledToggle.addEventListener('change', _autoSaveEmail);
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
  const payload = {
    teams_enabled:     teamsEnabledToggle?.checked ?? false,
    teams_webhook_url: teamsWebhookUrl?.value.trim() || '',
  };
  try {
    await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notifications: payload })
    });
    showInlineMessage(teamsNotifMsg, 'Saved.', false, 1500);
  } catch (err) {
    showInlineMessage(teamsNotifMsg, `Error: ${err.message}`, true);
  }
}

if (teamsEnabledToggle) teamsEnabledToggle.addEventListener('change', _autoSaveTeams);
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
  try {
    const r = await fetch('/api/system/health');
    if (!r.ok) return;
    const d = await r.json();

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
  } catch (_) {}
}

// Load health when Troubleshooting tab is activated
document.querySelectorAll('.setup-subtab').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.dataset.subtab === 'setup-troubleshoot') loadSystemHealth();
  });
});
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
  if (!confirm('Clear all server-side cache?\n\nThis resets Proxmox state, VM list, command history, and reclone logs. No restart is required.')) return;
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
    // Bulk bar hidden for templates (read-only)
    const bulkBar = document.getElementById('vm-bulk-bar');
    if (bulkBar) bulkBar.classList.toggle('hidden', activeVmCat === 'templates');
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
  document.getElementById(`server-bulk-${op}`)?.addEventListener('click', () => {
    if (activeVmCat === 'templates') return; // no bulk ops on templates
    const panel = document.getElementById(`vm-cat-panel-${activeVmCat}`);
    if (!panel) return;
    const vmids = [...panel.querySelectorAll('.vm-check:checked')].map((cb) => cb.dataset.vmid);
    if (!vmids.length) return;
    if (op === 'delete' && !confirm(`Delete ${vmids.length} VM(s)?\n\nThis will stop and permanently destroy them. This cannot be undone.`)) return;
    const action = op === 'reclone' ? 'reclone_vm' : op === 'delete' ? 'delete_vm' : `${op}_vm`;
    vmids.forEach((vmid) => sendProxmoxCommand(action, vmid));
    showToast(`${op.charAt(0).toUpperCase() + op.slice(1)} command sent for ${vmids.length} VM(s)`, 'success');
  });
});

updateCentralToolbar();
activateSetupSubtab('setup-github');
const updateAllBtn = document.getElementById('update-all-btn');
if (updateAllBtn && !updateAllBtn._bound) {
  updateAllBtn.addEventListener('click', triggerUpdateAll);
  updateAllBtn._bound = true;
}
connectWebSocket();
loadSimulations();

// Single init call replaces 5 separate REST calls — UI renders immediately from cache
(async () => {
  try {
    const init = await requestJson('/api/init');
    // Proxmox
    if (init.proxmox) {
      if (init.proxmox.webui_vmid != null) webuiVmid = init.proxmox.webui_vmid;
      if (init.proxmox.connected || (init.proxmox.vms || []).length || (init.proxmox.usb_state || []).length || (init.proxmox.unknown_usb || []).length || (init.proxmox.pending_proxmox || []).length || (init.proxmox.approved_proxmox || []).length) {
        renderServerTab(init.proxmox);
      }
    }
    // Reclone
    if (init.reclone) renderRecloneStatus(init.reclone);
    // Update All
    if (init.update_all) handleUpdateAllProgress(init.update_all);
    // Central
    if (init.central) {
      centralTokenValid = Boolean(init.central.token_valid);
      setCentralApiStatus(centralTokenValid, init.central.token_state);
      handleCentralUpdate(init.central.status || {}, Date.now() / 1000, init.central.wireless_clients || {}, init.central.hardware_alerts || [], init.central.client_count_status || {});
    }
    // Relay
    if (init.relay) setRelayStatus(init.relay);
    // Kill switch (global from GitHub, local from simulation.conf)
    if (init.kill_switch !== undefined) applyGkillSwitch(init.kill_switch);
    if (init.local_kill_switch !== undefined) {
      simDisabledState.local = init.local_kill_switch === 'on';
      renderSimDisabledBanner();
    }
    // Version badge — prefer app_version (VERSION file), fall back to installer_version
    const badge = document.getElementById('installer-version');
    if (badge) badge.textContent = `v${init.app_version || init.installer_version || '—'}`;
  } catch (_) { /* silent — WS will provide live state */ }
})();

// ── Auto-refresh ──────────────────────────────────────────────────────────────
let _refreshTimer = null;
let refreshIntervalSeconds = 0;

function syncRefreshTimer() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
  if (refreshIntervalSeconds > 0 && !document.hidden) {
    _refreshTimer = setInterval(refreshAll, refreshIntervalSeconds * 1000);
  }
}

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

function applyRefreshInterval(seconds) {
  refreshIntervalSeconds = Number.isFinite(seconds) ? seconds : 0;
  syncRefreshTimer();
  localStorage.setItem('refreshInterval', String(refreshIntervalSeconds));
}

document.addEventListener('visibilitychange', () => {
  const isHidden = document.hidden;
  syncRefreshTimer();
  if (!isHidden && refreshIntervalSeconds > 0) {
    refreshAll().catch(() => {});
  }
});

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

  function appendLine(text) {
    const filter = filterInput.value.trim();
    if (filter && !text.toLowerCase().includes(filter.toLowerCase())) return;

    const span = document.createElement('span');
    span.className = 'log-line ' + classify(text);
    span.innerHTML = highlight(text, filter) + '\n';
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

  function startTail() {
    if (evtSource) return;
    evtSource = new EventSource('/api/logs/stream');
    evtSource.onmessage = (e) => {
      const line = JSON.parse(e.data);
      if (line) appendLine(line);
    };
    evtSource.onerror = () => appendLine('[stream disconnected — click Start Tail to reconnect]');
    tailBtn.classList.add('hidden');
    stopBtn.classList.remove('hidden');
  }

  function stopTail() {
    if (evtSource) { evtSource.close(); evtSource = null; }
    tailBtn.classList.remove('hidden');
    stopBtn.classList.add('hidden');
  }

  tailBtn.addEventListener('click', startTail);
  stopBtn.addEventListener('click', stopTail);
  refreshBtn.addEventListener('click', loadHistory);
  clearBtn.addEventListener('click', clearOutput);
  if (sourceSelect) sourceSelect.addEventListener('change', loadHistory);

  // Re-apply filter live
  filterInput.addEventListener('input', () => {
    const lines = Array.from(output.querySelectorAll('.log-line')).map(s => s.textContent);
    clearOutput();
    lines.forEach(appendLine);
  });

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

(() => {
  const overlay = document.getElementById('spoke-login-overlay');
  const usernameGroup = document.getElementById('spoke-login-username-group');
  const usernameInput = document.getElementById('spoke-login-username');
  const passwordInput = document.getElementById('spoke-login-password');
  const errorBox = document.getElementById('spoke-login-error');
  const loginBtn = document.getElementById('spoke-login-btn');
  const logoutBtn = document.getElementById('spoke-logout-btn');
  const userPill = document.getElementById('spoke-current-user-pill');
  let authProvider = 'local';

  if (!overlay || !passwordInput || !loginBtn) return;

  function setOverlayVisible(isVisible) {
    overlay.classList.toggle('hidden', !isVisible);
    overlay.style.display = isVisible ? 'flex' : 'none';
    document.body.classList.toggle('spoke-login-active', isVisible);
  }

  function setError(message = '') {
    errorBox.textContent = message;
    errorBox.className = message ? 'form-msg msg-error' : 'form-msg';
  }

  function setFooterAuth(username = '', role = '') {
    if (username) {
      window.spokeCurrentUser = { username, role };
      if (userPill) {
        userPill.textContent = `👤 ${username}`;
        userPill.classList.remove('hidden');
        userPill.style.display = 'inline-flex';
      }
      if (logoutBtn) {
        logoutBtn.classList.remove('hidden');
        logoutBtn.style.display = 'inline-flex';
        logoutBtn.disabled = false;
      }
      return;
    }

    delete window.spokeCurrentUser;
    if (userPill) {
      userPill.classList.add('hidden');
      userPill.style.display = 'none';
    }
    if (logoutBtn) {
      logoutBtn.classList.add('hidden');
      logoutBtn.style.display = 'none';
      logoutBtn.disabled = false;
    }
  }

  function setAuthProvider(provider = 'local') {
    authProvider = String(provider || 'local').toLowerCase();
    const needsUsername = authProvider !== 'local';
    if (usernameGroup) usernameGroup.classList.toggle('hidden', !needsUsername);
    if (usernameInput) usernameInput.value = needsUsername ? (usernameInput.value || '') : 'admin';
  }

  async function submitLogin() {
    const username = authProvider === 'local' ? 'admin' : (usernameInput?.value || '').trim();
    const password = passwordInput.value;

    if (authProvider !== 'local' && !username) {
      setError('Enter your username.');
      usernameInput?.focus();
      return;
    }
    if (!password) {
      setError('Enter your password.');
      passwordInput.focus();
      return;
    }

    loginBtn.disabled = true;
    setError('');
    try {
      const result = await requestJson('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      setFooterAuth(result?.username || username, result?.role || '');
      passwordInput.value = '';
      setOverlayVisible(false);
    } catch (error) {
      setError(error.message || 'Sign in failed.');
      passwordInput.value = '';
      if (authProvider !== 'local' && !username) usernameInput?.focus();
      else passwordInput.focus();
    } finally {
      loginBtn.disabled = false;
    }
  }

  async function initializeAuth() {
    try {
      const data = await requestJson('/api/auth/check');
      const username = data?.username || '';
      const role = data?.role || '';
      setAuthProvider(data?.auth_provider || 'local');
      if (!data?.auth_required || data?.authenticated) {
        setFooterAuth(username, role);
        setOverlayVisible(false);
        return;
      }
      setFooterAuth();
      setError('');
      passwordInput.value = '';
      setOverlayVisible(true);
      if (authProvider === 'local') passwordInput.focus();
      else usernameInput?.focus();
    } catch (error) {
      console.warn('Auth check failed:', error);
      setOverlayVisible(false);
    }
  }

  loginBtn.addEventListener('click', submitLogin);
  passwordInput.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    submitLogin();
  });
  logoutBtn?.addEventListener('click', async () => {
    logoutBtn.disabled = true;
    try {
      await requestJson('/api/auth/logout', { method: 'POST' });
    } catch (error) {
      console.warn('Logout failed:', error);
    }
    window.location.reload();
  });

  initializeAuth();
})();

document.getElementById('spoke-acme-challenge')?.addEventListener('change', toggleSpokeAcmeDnsSection);
document.getElementById('spoke-acme-dns-provider')?.addEventListener('change', toggleSpokeAcmeDnsSection);
