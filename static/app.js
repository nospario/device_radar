/* Bluetooth Radar — Dashboard JS */

const REFRESH_INTERVAL = 15000;

// -- Helpers --

function timeAgo(timestamp) {
    if (!timestamp) return 'never';
    const now = Date.now() / 1000;
    const diff = now - timestamp;
    if (diff < 60) return `${Math.round(diff)}s ago`;
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
}

function formatTime(timestamp) {
    if (!timestamp) return '';
    const d = new Date(timestamp * 1000);
    return d.toLocaleString();
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

async function api(url, options) {
    const resp = await fetch(url, options);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

// -- Dashboard --

let dashboardTimer = null;
let cachedDevices = [];

async function loadStats() {
    try {
        const stats = await api('/api/stats');
        document.getElementById('stat-total').textContent = stats.total_devices;
        document.getElementById('stat-detected').textContent = stats.home_devices;
        document.getElementById('stat-lost').textContent = stats.away_devices;
        document.getElementById('stat-watchlisted').textContent = stats.watchlisted_devices;
        document.getElementById('stat-events').textContent = stats.events_today;
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}

function scanTypeBadge(scanType) {
    if (!scanType) return '';
    let cls = 'scan-type-ble';
    if (scanType === 'WiFi') cls = 'scan-type-wifi';
    else if (scanType === 'Classic') cls = 'scan-type-classic';
    else if (scanType === 'BLE+Classic') cls = 'scan-type-classic';
    return `<span class="scan-type-badge ${cls}">${escapeHtml(scanType)}</span>`;
}

async function loadDevices() {
    const showHidden = document.getElementById('filter-hidden').checked;

    const params = new URLSearchParams();
    if (showHidden) params.set('hidden', '1');

    try {
        cachedDevices = await api(`/api/devices?${params}`);
        renderDevices();
    } catch (e) {
        console.error('Failed to load devices:', e);
    }
}

function getColumnFilters() {
    const el = (id) => { const e = document.getElementById(id); return e ? e.value : ''; };
    return {
        state: el('col-filter-state'),
        name: el('col-filter-name').toLowerCase(),
        mac: el('col-filter-mac').toLowerCase(),
        type: el('col-filter-type').toLowerCase(),
        scan: el('col-filter-scan'),
        paired: el('col-filter-paired'),
        notify: el('col-filter-notify'),
        watchlist: el('col-filter-watchlist'),
    };
}

function applyColumnFilters(devices) {
    const f = getColumnFilters();
    return devices.filter(d => {
        const name = (d.friendly_name || d.advertised_name || '(unknown)').toLowerCase();
        const mac = (d.mac_address || '').toLowerCase();
        const type = (d.device_type || '').toLowerCase();
        const scan = d.scan_type || '';
        if (f.state && d.state !== f.state) return false;
        if (f.name && !name.includes(f.name)) return false;
        if (f.mac && !mac.includes(f.mac)) return false;
        if (f.type && !type.includes(f.type)) return false;
        if (f.scan && !scan.includes(f.scan)) return false;
        if (f.paired === 'yes' && !d.is_paired) return false;
        if (f.paired === 'no' && d.is_paired) return false;
        if (f.notify === 'on' && !d.is_notify) return false;
        if (f.notify === 'off' && d.is_notify) return false;
        if (f.watchlist === 'yes' && !d.is_watchlisted) return false;
        if (f.watchlist === 'no' && d.is_watchlisted) return false;
        return true;
    });
}

function renderDevices() {
    const devices = applyColumnFilters(cachedDevices);
    const tbody = document.getElementById('device-tbody');

    if (devices.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" class="empty">No devices found</td></tr>';
        return;
    }

    tbody.innerHTML = devices.map(d => {
        const name = escapeHtml(d.friendly_name || d.advertised_name || '(unknown)');
        const stateClass = d.state === 'DETECTED' ? 'state-detected' : 'state-lost';
        const watchClass = d.is_watchlisted ? 'active' : '';
        const rssi = d.last_rssi !== null ? d.last_rssi : 'n/a';
        const lastSeen = timeAgo(d.last_seen);
        const linked = d.linked_devices && d.linked_devices.length > 0;
        const linkedBadge = linked
            ? `<span class="linked-badge" title="Linked with ${d.linked_devices.length} device(s)">${d.linked_devices.length} linked</span>`
            : '';

        return `<tr>
            <td><span class="state-badge ${stateClass}">${d.state}</span></td>
            <td><a href="/device/${encodeURIComponent(d.mac_address)}">${name}</a> ${linkedBadge}</td>
            <td><code>${escapeHtml(d.mac_address)}</code></td>
            <td>${escapeHtml(d.device_type)}</td>
            <td>${scanTypeBadge(d.scan_type)}</td>
            <td>${escapeHtml(d.manufacturer || '')}</td>
            <td>${rssi}</td>
            <td title="${formatTime(d.last_seen)}">${lastSeen}</td>
            <td>${d.is_paired ? '<span class="state-badge state-detected">Yes</span>' : '<span class="state-badge state-lost">No</span>'}</td>
            <td>
                <button class="notify-toggle ${d.is_notify ? 'active' : ''}"
                        onclick="toggleNotify('${d.mac_address}', ${!d.is_notify})">
                    ${d.is_notify ? 'On' : 'Off'}
                </button>
            </td>
            <td>
                <button class="watchlist-toggle ${watchClass}"
                        onclick="toggleWatchlist('${d.mac_address}', ${!d.is_watchlisted})">
                    ${d.is_watchlisted ? 'Watching' : 'Watch'}
                </button>
            </td>
        </tr>`;
    }).join('');
}

async function toggleWatchlist(mac, enable) {
    try {
        await api(`/api/devices/${encodeURIComponent(mac)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_watchlisted: enable }),
        });
        loadDevices();
        loadStats();
    } catch (e) {
        console.error('Failed to toggle watchlist:', e);
    }
}

async function toggleNotify(mac, enable) {
    try {
        await api(`/api/devices/${encodeURIComponent(mac)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_notify: enable }),
        });
        loadDevices();
    } catch (e) {
        console.error('Failed to toggle notify:', e);
    }
}

function saveFilters() {
    const el = (id) => { const e = document.getElementById(id); return e ? e.value : ''; };
    const filters = {
        hidden: document.getElementById('filter-hidden').checked,
        state: el('col-filter-state'),
        name: el('col-filter-name'),
        mac: el('col-filter-mac'),
        type: el('col-filter-type'),
        scan: el('col-filter-scan'),
        paired: el('col-filter-paired'),
        notify: el('col-filter-notify'),
        watchlist: el('col-filter-watchlist'),
    };
    localStorage.setItem('dashboard-filters', JSON.stringify(filters));
}

function restoreFilters() {
    try {
        const raw = localStorage.getItem('dashboard-filters');
        if (!raw) return;
        const f = JSON.parse(raw);
        document.getElementById('filter-hidden').checked = !!f.hidden;
        // Migrate old keys
        if (f.state) { const e = document.getElementById('col-filter-state'); if (e) e.value = f.state; }
        if (f.scanType) { const e = document.getElementById('col-filter-scan'); if (e) e.value = f.scanType; }
        if (f.watchlisted) { const e = document.getElementById('col-filter-watchlist'); if (e) e.value = 'yes'; }
        // New keys
        if (f.name) { const e = document.getElementById('col-filter-name'); if (e) e.value = f.name; }
        if (f.mac) { const e = document.getElementById('col-filter-mac'); if (e) e.value = f.mac; }
        if (f.type) { const e = document.getElementById('col-filter-type'); if (e) e.value = f.type; }
        if (f.scan) { const e = document.getElementById('col-filter-scan'); if (e) e.value = f.scan; }
        if (f.paired) { const e = document.getElementById('col-filter-paired'); if (e) e.value = f.paired; }
        if (f.notify) { const e = document.getElementById('col-filter-notify'); if (e) e.value = f.notify; }
        if (f.watchlist) { const e = document.getElementById('col-filter-watchlist'); if (e) e.value = f.watchlist; }
    } catch (e) { /* ignore corrupt data */ }
}

function onFilterChange() {
    saveFilters();
    renderDevices();
}

function resetFilters() {
    document.getElementById('filter-hidden').checked = false;
    document.querySelectorAll('.col-filter').forEach(el => {
        if (el.tagName === 'SELECT') el.value = '';
        else el.value = '';
    });
    saveFilters();
    loadDevices();
}

function onHiddenChange() {
    saveFilters();
    loadDevices();
}

// -- DNS Resolver Toggle --

async function loadDnsStatus() {
    const indicator = document.getElementById('dns-indicator');
    const label = document.getElementById('dns-mode-label');
    const btn = document.getElementById('dns-toggle-btn');
    if (!indicator || !label || !btn) return;

    try {
        const status = await api('/api/dns/status');
        const isPihole = status.mode === 'pihole';
        indicator.className = 'dns-indicator ' + (isPihole ? 'dns-pihole' : 'dns-router');
        label.textContent = isPihole ? 'Pi-hole' : 'Router';
        btn.textContent = isPihole ? 'Use Router' : 'Use Pi-hole';
        btn.disabled = false;
        btn.dataset.currentMode = status.mode;

        // Show FTL status as tooltip
        const card = document.getElementById('dns-toggle-card');
        if (card) {
            const servers = (status.dns_servers || []).join(', ') || 'auto';
            const ftl = status.pihole_ftl_active ? 'running' : 'stopped';
            card.title = `DNS: ${servers}\nPi-hole FTL: ${ftl}`;
        }
    } catch (e) {
        label.textContent = '?';
        btn.disabled = true;
        console.error('Failed to load DNS status:', e);
    }
}

async function toggleDns() {
    const btn = document.getElementById('dns-toggle-btn');
    if (!btn) return;
    const currentMode = btn.dataset.currentMode;
    const newMode = currentMode === 'pihole' ? 'router' : 'pihole';

    btn.disabled = true;
    btn.textContent = 'Switching...';

    try {
        await api('/api/dns/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: newMode }),
        });
        // Brief delay for network to settle after reconnection
        setTimeout(loadDnsStatus, 2000);
    } catch (e) {
        console.error('Failed to toggle DNS:', e);
        btn.textContent = 'Error';
        setTimeout(loadDnsStatus, 1000);
    }
}

function initDashboard() {
    restoreFilters();
    loadStats();
    loadDevices();
    loadDnsStatus();

    // Show hidden triggers a re-fetch (server-side)
    document.getElementById('filter-hidden').addEventListener('change', onHiddenChange);

    // Column filters (client-side, just re-render + save)
    document.querySelectorAll('.col-filter').forEach(el => {
        el.addEventListener('input', onFilterChange);
        el.addEventListener('change', onFilterChange);
    });

    // Auto-refresh
    dashboardTimer = setInterval(() => {
        loadStats();
        loadDevices();
        loadDnsStatus();
    }, REFRESH_INTERVAL);
}

// -- Device Linking --

async function linkDevice(primaryMac) {
    const select = document.getElementById('link-target');
    const targetMac = select.value;
    if (!targetMac) return;

    const status = document.getElementById('link-status');
    try {
        await api(`/api/devices/${encodeURIComponent(primaryMac)}/link`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_mac: targetMac }),
        });
        status.textContent = 'Device linked successfully';
        status.className = 'link-status link-success';
        status.style.display = 'block';
        setTimeout(() => location.reload(), 800);
    } catch (e) {
        status.textContent = 'Failed to link device';
        status.className = 'link-status link-error';
        status.style.display = 'block';
        console.error('Link error:', e);
    }
}

async function unlinkDevice(mac) {
    const status = document.getElementById('link-status');
    try {
        await api(`/api/devices/${encodeURIComponent(mac)}/unlink`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        status.textContent = 'Device unlinked';
        status.className = 'link-status link-success';
        status.style.display = 'block';
        setTimeout(() => location.reload(), 800);
    } catch (e) {
        status.textContent = 'Failed to unlink device';
        status.className = 'link-status link-error';
        status.style.display = 'block';
        console.error('Unlink error:', e);
    }
}

// -- Link Search Dropdown --

function initLinkSearch(devices) {
    const input = document.getElementById('link-search');
    const hidden = document.getElementById('link-target');
    const dropdown = document.getElementById('link-dropdown');
    let activeIndex = -1;

    function render(filtered) {
        activeIndex = -1;
        if (filtered.length === 0) {
            dropdown.style.display = 'none';
            return;
        }
        dropdown.innerHTML = filtered.map((d, i) =>
            `<div class="link-dropdown-item" data-mac="${d.mac}" data-index="${i}">${escapeHtml(d.label)}</div>`
        ).join('');
        dropdown.style.display = 'block';

        dropdown.querySelectorAll('.link-dropdown-item').forEach(el => {
            el.addEventListener('mousedown', (e) => {
                e.preventDefault();
                pick(el.dataset.mac, el.textContent);
            });
        });
    }

    function pick(mac, label) {
        hidden.value = mac;
        input.value = label;
        dropdown.style.display = 'none';
    }

    function setActive(items) {
        items.forEach((el, i) => el.classList.toggle('active', i === activeIndex));
    }

    input.addEventListener('input', () => {
        hidden.value = '';
        const q = input.value.toLowerCase();
        const filtered = q
            ? devices.filter(d => d.label.toLowerCase().includes(q))
            : devices;
        render(filtered);
    });

    input.addEventListener('focus', () => {
        if (!hidden.value) {
            const q = input.value.toLowerCase();
            render(q ? devices.filter(d => d.label.toLowerCase().includes(q)) : devices);
        }
    });

    input.addEventListener('blur', () => {
        setTimeout(() => { dropdown.style.display = 'none'; }, 150);
    });

    input.addEventListener('keydown', (e) => {
        const items = dropdown.querySelectorAll('.link-dropdown-item');
        if (!items.length) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            activeIndex = Math.min(activeIndex + 1, items.length - 1);
            setActive(items);
            items[activeIndex].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            activeIndex = Math.max(activeIndex - 1, 0);
            setActive(items);
            items[activeIndex].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (activeIndex >= 0 && items[activeIndex]) {
                pick(items[activeIndex].dataset.mac, items[activeIndex].textContent);
            }
        } else if (e.key === 'Escape') {
            dropdown.style.display = 'none';
        }
    });
}

// -- Device Detail --

function getDeviceType() {
    const select = document.getElementById('device-type');
    if (select.value === '__custom__') {
        return document.getElementById('device-type-custom').value.trim() || 'Unknown';
    }
    return select.value;
}

function initDevicePage(mac) {
    // Format timestamps
    document.querySelectorAll('[data-timestamp]').forEach(el => {
        const ts = parseFloat(el.getAttribute('data-timestamp'));
        if (ts) el.textContent = formatTime(ts);
    });

    // Custom device type toggle
    const typeSelect = document.getElementById('device-type');
    const typeCustom = document.getElementById('device-type-custom');
    typeSelect.addEventListener('change', () => {
        if (typeSelect.value === '__custom__') {
            typeCustom.style.display = '';
            typeCustom.focus();
        } else {
            typeCustom.style.display = 'none';
            typeCustom.value = '';
        }
    });

    // Save form
    const form = document.getElementById('device-form');
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const status = document.getElementById('save-status');

        const deviceType = getDeviceType();
        try {
            await api(`/api/devices/${encodeURIComponent(mac)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    friendly_name: document.getElementById('friendly-name').value,
                    device_type: deviceType,
                    is_watchlisted: document.getElementById('is-watchlisted').checked,
                    is_notify: document.getElementById('is-notify').checked,
                    is_welcome: document.getElementById('is-welcome').checked,
                    is_hidden: document.getElementById('is-hidden').checked,
                    alexa_voice: document.getElementById('alexa-voice').value,
                    dns_tracking_enabled: document.getElementById('dns-tracking-enabled') ? document.getElementById('dns-tracking-enabled').checked : undefined,
                }),
            });

            // If a custom type was entered, add it to the dropdown as a proper option
            if (typeSelect.value === '__custom__' && deviceType !== 'Unknown') {
                const opt = document.createElement('option');
                opt.value = deviceType;
                opt.textContent = deviceType;
                typeSelect.insertBefore(opt, typeSelect.querySelector('option[value="__custom__"]'));
                typeSelect.value = deviceType;
                typeCustom.style.display = 'none';
                typeCustom.value = '';
            }

            status.textContent = 'Saved!';
            setTimeout(() => { status.textContent = ''; }, 2000);
        } catch (e) {
            status.textContent = 'Error saving';
            status.style.color = 'var(--red)';
            console.error('Failed to save:', e);
        }
    });

    // Proximity form
    const proxForm = document.getElementById('proximity-form');
    if (proxForm) {
        proxForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const pStatus = document.getElementById('proximity-save-status');

            try {
                await api(`/api/devices/${encodeURIComponent(mac)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        proximity_enabled: document.getElementById('proximity-enabled').checked,
                        proximity_rssi_threshold: parseInt(document.getElementById('proximity-level').value),
                        proximity_interval: parseInt(document.getElementById('proximity-interval').value) || 30,
                        proximity_alexa_device: document.getElementById('proximity-alexa-device').value,
                        proximity_prompt: document.getElementById('proximity-prompt').value,
                    }),
                });

                pStatus.textContent = 'Saved!';
                setTimeout(() => { pStatus.textContent = ''; }, 2000);
            } catch (err) {
                pStatus.textContent = 'Error saving';
                pStatus.style.color = 'var(--red)';
                console.error('Failed to save proximity:', err);
            }
        });
    }

    // Calendar form
    const calForm = document.getElementById('calendar-form');
    if (calForm) {
        calForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const cStatus = document.getElementById('calendar-save-status');

            try {
                const calBoxes = document.querySelectorAll('#calendar-checkboxes input[type="checkbox"]');
                const selectedCals = [...calBoxes].filter(cb => cb.checked).map(cb => cb.value);

                await api(`/api/devices/${encodeURIComponent(mac)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        calendar_calendars: JSON.stringify(selectedCals),
                    }),
                });

                cStatus.textContent = 'Saved!';
                setTimeout(() => { cStatus.textContent = ''; }, 2000);
            } catch (err) {
                cStatus.textContent = 'Error saving';
                cStatus.style.color = 'var(--red)';
                console.error('Failed to save calendars:', err);
            }
        });
    }

    // News feeds form
    const newsForm = document.getElementById('news-form');
    if (newsForm) {
        newsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const nStatus = document.getElementById('news-save-status');

            try {
                const newsBoxes = document.querySelectorAll('#news-checkboxes input[type="checkbox"]');
                const selectedFeeds = [...newsBoxes].filter(cb => cb.checked).map(cb => cb.value);

                await api(`/api/devices/${encodeURIComponent(mac)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        news_feeds: JSON.stringify(selectedFeeds),
                    }),
                });

                nStatus.textContent = 'Saved!';
                setTimeout(() => { nStatus.textContent = ''; }, 2000);
            } catch (err) {
                nStatus.textContent = 'Error saving';
                nStatus.style.color = 'var(--red)';
                console.error('Failed to save news feeds:', err);
            }
        });
    }
}

// -- History --

let historyPage = 0;
const PAGE_SIZE = 50;

async function loadHistory() {
    const eventType = document.getElementById('filter-event-type').value;
    const mac = document.getElementById('filter-mac').value.trim();

    const params = new URLSearchParams();
    if (eventType) params.set('event_type', eventType);
    if (mac) params.set('mac', mac);
    params.set('limit', PAGE_SIZE);
    params.set('offset', historyPage * PAGE_SIZE);

    try {
        const data = await api(`/api/events?${params}`);
        const tbody = document.getElementById('history-tbody');

        if (data.events.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty">No events found</td></tr>';
        } else {
            tbody.innerHTML = data.events.map(e => {
                const name = escapeHtml(e.friendly_name || e.device_name || e.d_adv_name || '(unknown)');
                const evtClass = e.event_type === 'arrived' ? 'event-arrived' : 'event-departed';
                const rssi = e.rssi !== null ? e.rssi : 'n/a';

                return `<tr>
                    <td><span class="event-badge ${evtClass}">${e.event_type}</span></td>
                    <td><a href="/device/${encodeURIComponent(e.mac_address)}">${name}</a></td>
                    <td><code>${escapeHtml(e.mac_address)}</code></td>
                    <td>${escapeHtml(e.device_type || '')}</td>
                    <td>${rssi}</td>
                    <td title="${formatTime(e.timestamp)}">${formatTime(e.timestamp)}</td>
                </tr>`;
            }).join('');
        }

        // Pagination
        const totalPages = Math.ceil(data.total / PAGE_SIZE) || 1;
        document.getElementById('page-info').textContent = `Page ${historyPage + 1} of ${totalPages}`;
        document.getElementById('btn-prev').disabled = historyPage === 0;
        document.getElementById('btn-next').disabled = (historyPage + 1) >= totalPages;
    } catch (e) {
        console.error('Failed to load history:', e);
    }
}

function initHistory() {
    loadHistory();

    document.getElementById('btn-apply-filter').addEventListener('click', () => {
        historyPage = 0;
        loadHistory();
    });

    document.getElementById('filter-event-type').addEventListener('change', () => {
        historyPage = 0;
        loadHistory();
    });

    document.getElementById('btn-prev').addEventListener('click', () => {
        if (historyPage > 0) { historyPage--; loadHistory(); }
    });

    document.getElementById('btn-next').addEventListener('click', () => {
        historyPage++;
        loadHistory();
    });
}

// -- Pairing --

async function loadPairingDevices() {
    try {
        const watchlistedOnly = document.getElementById('filter-watchlisted').checked;
        const params = new URLSearchParams({ hidden: '1' });
        if (watchlistedOnly) params.set('watchlisted', '1');
        const devices = await api(`/api/devices?${params}`);
        const tbody = document.getElementById('pairing-tbody');

        if (devices.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty">No devices found</td></tr>';
            return;
        }

        tbody.innerHTML = devices.map(d => {
            const name = escapeHtml(d.friendly_name || d.advertised_name || '(unknown)');
            const paired = d.is_paired;
            const pairedBadge = paired
                ? '<span class="state-badge state-detected">Paired</span>'
                : '<span class="state-badge state-lost">Not Paired</span>';
            const actionBtn = paired
                ? `<button class="btn btn-unpair" onclick="unpairDevice('${d.mac_address}')">Unpair</button>`
                : `<button class="btn btn-pair" onclick="pairDevice('${d.mac_address}')">Pair</button>`;

            return `<tr>
                <td><a href="/device/${encodeURIComponent(d.mac_address)}">${name}</a></td>
                <td><code>${escapeHtml(d.mac_address)}</code></td>
                <td>${escapeHtml(d.device_type)}</td>
                <td>${pairedBadge}</td>
                <td>${actionBtn}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        console.error('Failed to load pairing devices:', e);
    }
}

function showPairStatus(message, success) {
    const el = document.getElementById('pair-status');
    el.textContent = message;
    el.className = 'pair-status ' + (success ? 'pair-success' : 'pair-error');
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 5000);
}

async function pairDevice(mac) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Pairing...';
    showPairStatus('Pairing in progress — confirm on your device...', true);

    try {
        const result = await api(`/api/devices/${encodeURIComponent(mac)}/pair`, {
            method: 'POST',
        });
        showPairStatus(result.message, result.success);
        loadPairingDevices();
    } catch (e) {
        showPairStatus('Failed to pair device', false);
        console.error('Pair error:', e);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Pair';
    }
}

async function unpairDevice(mac) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Removing...';

    try {
        const result = await api(`/api/devices/${encodeURIComponent(mac)}/unpair`, {
            method: 'POST',
        });
        showPairStatus(result.message, result.success);
        loadPairingDevices();
    } catch (e) {
        showPairStatus('Failed to unpair device', false);
        console.error('Unpair error:', e);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Unpair';
    }
}

function initPairing() {
    loadPairingDevices();
    document.getElementById('filter-watchlisted').addEventListener('change', loadPairingDevices);
}

// -- DNS Traffic Page --

let trafficPage = 0;
const TRAFFIC_PAGE_SIZE = 50;
let topDomainsChart = null;

function getTrafficTimeRange() {
    const sel = document.getElementById('traffic-filter-time');
    if (!sel) return {};
    const val = sel.value;
    const now = Date.now() / 1000;
    const midnight = now - (now % 86400);
    switch (val) {
        case '1h': return { from: now - 3600 };
        case '6h': return { from: now - 21600 };
        case 'today': return { from: midnight };
        case 'yesterday': return { from: midnight - 86400, to: midnight };
        case '7d': return { from: now - 604800 };
        default: return { from: midnight };
    }
}

function getTrafficFilters() {
    const range = getTrafficTimeRange();
    const params = new URLSearchParams();
    if (range.from) params.set('from', range.from);
    if (range.to) params.set('to', range.to);
    const device = document.getElementById('traffic-filter-device');
    if (device && device.value) params.set('device_mac', device.value);
    const domain = document.getElementById('traffic-filter-domain');
    if (domain && domain.value.trim()) params.set('domain', domain.value.trim());
    const category = document.getElementById('traffic-filter-category');
    if (category && category.value) params.set('category', category.value);
    return params;
}

async function loadTrafficStats() {
    try {
        const params = getTrafficFilters();
        const stats = await api(`/api/traffic/stats?${params}`);
        document.getElementById('stat-total-queries').textContent = stats.total_queries.toLocaleString();
        document.getElementById('stat-unique-domains').textContent = stats.unique_domains.toLocaleString();
        document.getElementById('stat-top-domain').textContent = stats.top_domain || '--';
        document.getElementById('stat-top-domain').title = stats.top_domain ? `${stats.top_domain} (${stats.top_domain_count})` : '';
        document.getElementById('stat-blocked').textContent = stats.blocked_count.toLocaleString();
        document.getElementById('stat-active-alerts').textContent = stats.active_alerts;
    } catch (e) {
        console.error('Failed to load traffic stats:', e);
    }
}

const CATEGORY_COLORS = {
    'Social Media': '#5b8af5',
    'Entertainment': '#a855f7',
    'News': '#f59e0b',
    'Productivity': '#34c759',
    'Shopping': '#ffd60a',
    'Search': '#8b90a0',
    'Other': '#6b7280',
};

function categoryBadge(category) {
    if (!category) return '<span class="category-badge category-uncategorised">Uncategorised</span>';
    const cls = 'category-' + category.toLowerCase().replace(/\s+/g, '-');
    return `<span class="category-badge ${cls}">${escapeHtml(category)}</span>`;
}

function statusBadge(status) {
    if (!status) return '';
    const cls = status === 'blocked' ? 'state-lost' :
                status === 'cached' ? 'state-detected' : '';
    return `<span class="state-badge ${cls}">${escapeHtml(status)}</span>`;
}

async function loadTrafficQueries() {
    try {
        const params = getTrafficFilters();
        params.set('limit', TRAFFIC_PAGE_SIZE);
        params.set('offset', trafficPage * TRAFFIC_PAGE_SIZE);

        const data = await api(`/api/traffic/domains?${params}`);
        const tbody = document.getElementById('traffic-tbody');

        if (data.domains.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty">No domains found</td></tr>';
        } else {
            tbody.innerHTML = data.domains.map(d => {
                const devices = escapeHtml(d.devices || 'Unknown');
                return `<tr>
                    <td><strong>${escapeHtml(d.root_domain)}</strong></td>
                    <td>${d.query_count.toLocaleString()}</td>
                    <td>${categoryBadge(d.category)}</td>
                    <td>${devices}</td>
                    <td title="${formatTime(d.first_seen)}">${timeAgo(d.first_seen)}</td>
                    <td title="${formatTime(d.last_seen)}">${timeAgo(d.last_seen)}</td>
                </tr>`;
            }).join('');
        }

        const totalPages = Math.ceil(data.total / TRAFFIC_PAGE_SIZE) || 1;
        document.getElementById('traffic-page-info').textContent =
            `Page ${trafficPage + 1} of ${totalPages} (${data.total.toLocaleString()} domains)`;
        document.getElementById('traffic-btn-prev').disabled = trafficPage === 0;
        document.getElementById('traffic-btn-next').disabled = (trafficPage + 1) >= totalPages;
    } catch (e) {
        console.error('Failed to load traffic domains:', e);
    }
}

async function loadTopDomainsChart() {
    try {
        const params = getTrafficFilters();
        params.set('limit', '10');
        const data = await api(`/api/traffic/top-domains?${params}`);

        const ctx = document.getElementById('top-domains-chart');
        if (!ctx) return;

        const labels = data.domains.map(d => d.root_domain);
        const counts = data.domains.map(d => d.count);
        const colors = data.domains.map(d => CATEGORY_COLORS[d.category] || '#6b7280');

        if (topDomainsChart) {
            topDomainsChart.destroy();
        }

        topDomainsChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Queries',
                    data: counts,
                    backgroundColor: colors,
                    borderRadius: 4,
                }],
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                },
                scales: {
                    x: {
                        ticks: { color: '#8b90a0' },
                        grid: { color: '#2a2e3a' },
                    },
                    y: {
                        ticks: { color: '#e1e4eb', font: { size: 12 } },
                        grid: { display: false },
                    },
                },
            },
        });
    } catch (e) {
        console.error('Failed to load top domains chart:', e);
    }
}

function refreshTraffic() {
    trafficPage = 0;
    loadTrafficStats();
    loadTrafficQueries();
    loadTopDomainsChart();
}

function exportTrafficCsv() {
    const params = getTrafficFilters();
    window.location.href = `/api/traffic/export?${params}`;
}

function initTrafficPage() {
    loadTrafficStats();
    loadTrafficQueries();
    loadTopDomainsChart();

    // Filter change handlers
    ['traffic-filter-time', 'traffic-filter-device', 'traffic-filter-category', 'traffic-filter-status'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', refreshTraffic);
    });

    const domainInput = document.getElementById('traffic-filter-domain');
    if (domainInput) {
        let debounce = null;
        domainInput.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(refreshTraffic, 400);
        });
    }

    // Pagination
    document.getElementById('traffic-btn-prev').addEventListener('click', () => {
        if (trafficPage > 0) { trafficPage--; loadTrafficQueries(); }
    });
    document.getElementById('traffic-btn-next').addEventListener('click', () => {
        trafficPage++; loadTrafficQueries();
    });

    // Auto-refresh every 30 seconds
    setInterval(() => {
        loadTrafficStats();
        if (trafficPage === 0) loadTrafficQueries();
    }, 30000);
}

// -- Device DNS Alert Management --

async function toggleAlert(mac, alertId, active) {
    try {
        await api(`/api/devices/${encodeURIComponent(mac)}/alerts/${alertId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_active: !!active }),
        });
        location.reload();
    } catch (e) {
        console.error('Failed to toggle alert:', e);
    }
}

async function deleteAlert(mac, alertId) {
    if (!confirm('Delete this alert?')) return;
    try {
        await api(`/api/devices/${encodeURIComponent(mac)}/alerts/${alertId}`, {
            method: 'DELETE',
        });
        location.reload();
    } catch (e) {
        console.error('Failed to delete alert:', e);
    }
}

function initAlertForm(mac) {
    const ollamaToggle = document.getElementById('alert-use-ollama');
    const customGroup = document.getElementById('alert-custom-msg-group');
    if (ollamaToggle && customGroup) {
        ollamaToggle.addEventListener('change', () => {
            customGroup.style.display = ollamaToggle.checked ? 'none' : '';
        });
    }

    const form = document.getElementById('alert-form');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const status = document.getElementById('alert-save-status');

        try {
            await api(`/api/devices/${encodeURIComponent(mac)}/alerts`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    domain: document.getElementById('alert-domain').value.trim(),
                    threshold_minutes: parseInt(document.getElementById('alert-threshold').value) || 5,
                    cooldown_minutes: parseInt(document.getElementById('alert-cooldown').value) || 30,
                    alert_type: document.getElementById('alert-type').value,
                    use_ollama: document.getElementById('alert-use-ollama').checked,
                    custom_message: document.getElementById('alert-custom-message').value || null,
                }),
            });

            status.textContent = 'Alert created!';
            setTimeout(() => location.reload(), 800);
        } catch (err) {
            status.textContent = 'Error creating alert';
            status.style.color = 'var(--red)';
            console.error('Failed to create alert:', err);
        }
    });
}
