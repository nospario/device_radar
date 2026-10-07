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
    if (resp.status === 401) {
        // A password is set and this change needs a login: go there, and come back afterwards
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
        throw new Error('login required');
    }
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

// A locally administered ("private") MAC is what phones, tablets and laptops
// use for WiFi privacy; such addresses have no vendor.
function isPrivateMac(mac) {
    const first = parseInt((mac || '').split(':')[0], 16);
    return !isNaN(first) && (first & 2) !== 0;
}

// Text for the Manufacturer column: the vendor, or "Private address" for
// WiFi-only devices that hide theirs.
function manufacturerLabel(d) {
    if (d.manufacturer) return d.manufacturer;
    if (d.scan_type === 'WiFi' && isPrivateMac(d.mac_address)) return 'Private address';
    return '';
}

// Text the Name filter searches: the displayed name plus IP address and
// manufacturer, so a device can be found by "192.168.1.158" or "tp-link".
// Linked devices are merged into one row, so their IP addresses count too.
function nameSearchText(d) {
    const linkedIps = (d.linked_devices || []).map(l => l.ip_address);
    return [d.friendly_name || d.advertised_name || '(unknown)', d.ip_address, ...linkedIps, manufacturerLabel(d)]
        .filter(Boolean).join(' ').toLowerCase();
}

function applyColumnFilters(devices) {
    const f = getColumnFilters();
    return devices.filter(d => {
        const name = nameSearchText(d);
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
            <td>${escapeHtml(manufacturerLabel(d))}</td>
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

function initDashboard() {
    restoreFilters();
    loadStats();
    loadPeople();
    loadHealth();
    loadDevices();
    loadCleanup();

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
        loadPeople();
        loadHealth();
        loadDevices();
    }, REFRESH_INTERVAL);
    cleanupTimer = setInterval(loadCleanup, CLEANUP_REFRESH_INTERVAL);
}

// -- People: who is home, decided by each person's phone --

const PEOPLE_ICON = {home: '\u{1F7E2}', away: '\u{1F534}', no_phone: '\u26AA'};

// "expected ~18:40" for someone who is away and has a usable prediction ('' otherwise)
function etaText(p) {
    const pr = p.prediction;
    if (p.state !== 'away' || !pr || pr.kind !== 'return') return '';
    if (pr.status === 'ready') {
        return ' \u00b7 expected ~' + new Date(pr.median * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
    }
    return pr.status === 'overdue' ? ' \u00b7 later than usual' : '';
}

function peopleChipText(p) {
    if (p.state === 'home') return p.since ? `arrived ${timeAgo(p.since)}` : 'home';
    if (p.state === 'away') {
        const base = p.since ? `left ${timeAgo(p.since)}` : (p.last_seen ? `last seen ${timeAgo(p.last_seen)}` : 'away');
        return base + etaText(p);
    }
    return 'no phone tracked';
}

// escapeHtml() is for text content; inside an attribute value quotes must be escaped too.
function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderPeople(people) {
    const strip = document.getElementById('people-strip');
    if (!strip) return;
    if (!people.length) { strip.hidden = true; return; }
    strip.hidden = false;
    strip.innerHTML = people.map(p => {
        const tip = p.state === 'no_phone'
            ? 'Only phones count as presence. Name this person\'s phone "' + p.display + '\'s iPhone" or set its Person.'
            : (p.phones.length ? 'Phone: ' + p.phones.join(', ') : '');
        return `<span class="person-chip person-${p.state}" title="${escapeAttr(tip)}">` +
               `${PEOPLE_ICON[p.state] || ''} <strong>${escapeHtml(p.display)}</strong> ` +
               `<span class="person-detail">${escapeHtml(peopleChipText(p))}</span></span>`;
    }).join('');
}

async function loadPeople() {
    try {
        renderPeople(await api('/api/people'));
    } catch (e) {
        console.error('Failed to load people:', e);
    }
}

// -- Reports: trends and predictions from each person's phone --

function fmtHours(h) {
    const total = Math.round(h * 60) % 1440;
    return String(Math.floor(total / 60)).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
}

function timeSummaryHtml(s) {
    if (!s || !s.enough) return `<span class="text-dim">not enough data yet (${s ? s.n : 0} so far)</span>`;
    return `<strong>${fmtHours(s.median)}</strong> <span class="text-dim">middle half ${fmtHours(s.q1)}\u2013${fmtHours(s.q3)} \u00b7 ${s.n} outings</span>`;
}

function hoursOutHtml(s) {
    if (!s || !s.enough) return '<span class="text-dim">\u2014</span>';
    return `<strong>${s.median.toFixed(1)} h</strong> <span class="text-dim">typically</span>`;
}

// One row per day, 0-24h across: green = at home, hatched grey = scanner was off, dim = still to come
function timelineSvg(rows) {
    const left = 54, width = 720, rowH = 15, gap = 4, top = 16;
    const height = top + rows.length * (rowH + gap) + 2;
    const x = h => left + (Math.max(0, Math.min(24, h)) / 24) * width;
    let svg = `<svg class="timeline-svg" viewBox="0 0 ${left + width + 8} ${height}" role="img" aria-label="Time at home, last ${rows.length} days">`;
    [0, 6, 12, 18, 24].forEach(h => {
        svg += `<text class="tick" x="${x(h)}" y="10" text-anchor="middle">${String(h).padStart(2, '0')}</text>` +
               `<line class="gridline" x1="${x(h)}" x2="${x(h)}" y1="${top - 2}" y2="${height - 2}"/>`;
    });
    rows.forEach((r, i) => {
        const y = top + i * (rowH + gap);
        svg += `<text class="daylabel" x="${left - 6}" y="${y + rowH - 3}" text-anchor="end">${escapeHtml(r.weekday)} ${escapeHtml(String(r.date).slice(8))}</text>` +
               `<rect class="track" x="${left}" y="${y}" width="${width}" height="${rowH}" rx="2"/>`;
        (r.unknown || []).forEach(([a, b]) => { svg += `<rect class="unknown" x="${x(a)}" y="${y}" width="${Math.max(1, x(b) - x(a))}" height="${rowH}"/>`; });
        (r.home || []).forEach(([a, b]) => { svg += `<rect class="home" x="${x(a)}" y="${y}" width="${Math.max(1, x(b) - x(a))}" height="${rowH}" rx="2"/>`; });
        if (r.future_from !== null && r.future_from !== undefined) {
            svg += `<rect class="future" x="${x(r.future_from)}" y="${y}" width="${Math.max(0, x(24) - x(r.future_from))}" height="${rowH}"/>`;
        }
    });
    return svg + '</svg>';
}

// Hours at home per day; faded bars are days the scanner only watched part of
function dailyBarsSvg(rows) {
    const width = 720, height = 90, base = 72, barW = width / rows.length;
    let svg = `<svg class="bars-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Hours at home per day">`;
    [0, 12, 24].forEach(h => {
        const y = base - (h / 24) * (base - 6);
        svg += `<line class="gridline" x1="0" x2="${width}" y1="${y}" y2="${y}"/><text class="tick" x="2" y="${y - 2}">${h}h</text>`;
    });
    rows.forEach((r, i) => {
        const h = Math.max(0, Math.min(24, r.home_h));
        const barH = (h / 24) * (base - 6);
        const label = `${r.weekday} ${r.date}: ${r.home_h} h at home` + (r.partial ? ` (scanner watched ${r.observed_h} h)` : '');
        svg += `<rect class="bar${r.partial ? ' partial' : ''}" x="${(i * barW + 1).toFixed(1)}" y="${(base - barH).toFixed(1)}" ` +
               `width="${(barW - 2).toFixed(1)}" height="${barH.toFixed(1)}"><title>${escapeHtml(label)}</title></rect>`;
        if (i % 7 === 0 || i === rows.length - 1) {
            svg += `<text class="tick" x="${(i * barW + barW / 2).toFixed(1)}" y="${height - 4}" text-anchor="middle">${escapeHtml(String(r.date).slice(5))}</text>`;
        }
    });
    return svg + '</svg>';
}

function trendHtml(t) {
    if (!t || !t.enough) return `<span class="text-dim">Not enough recent data to say whether weekday return times are drifting (${t ? t.n_recent : 0} in the last 4 weeks, ${t ? t.n_before : 0} before that).</span>`;
    const dir = t.shift_min > 0 ? 'later' : 'earlier';
    if (!t.clear) return `Weekday return times are steady: about ${Math.abs(t.shift_min)} min ${dir} than the 4 weeks before, which is within normal week-to-week variation.`;
    return `<strong>Weekday return times have moved ${Math.abs(t.shift_min)} min ${dir}</strong> compared with the 4 weeks before (likely range ${t.low_min} to ${t.high_min} min).`;
}

function accuracyHtml(p) {
    const b = p.backtest || {};
    if (!b.tests) return '<span class="text-dim">Not enough history to test how accurate predictions would have been.</span>';
    const hit = Math.round(b.hit_rate * 100);
    let text = `Checked against ${b.tests} past outings: the 80% window contained the real return time ${hit}% of the time, ` +
               `and the middle guess was off by ${b.median_error_min} min typically`;
    if (b.naive_error_min !== null && b.naive_error_min !== undefined) text += ` (guessing the usual time would be off by ${b.naive_error_min} min)`;
    return text + '.';
}

function personReportHtml(p) {
    const q = p.quality || {};
    const reasons = (q.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('');
    const typical = p.typical || {};
    const row = (label, t) => `<tr><th>${label}</th><td>${timeSummaryHtml(t.leave)}</td><td>${timeSummaryHtml(t.return)}</td><td>${hoursOutHtml(t.hours_out)}</td></tr>`;
    const stateIcon = p.state === 'home' ? '\u{1F7E2}' : '\u{1F534}';
    const days = (p.daily || []).filter(d => !d.partial);
    const avg = kind => {
        const v = days.filter(d => (kind === 'weekend') === ['Sat', 'Sun'].includes(d.weekday)).map(d => d.home_h);
        return v.length ? (v.reduce((a, b) => a + b, 0) / v.length).toFixed(1) + ' h' : '\u2014';
    };
    return `<article class="report-card">
        <header class="report-head">
            <h3>${stateIcon} ${escapeHtml(p.display)}</h3>
            <span class="q-badge q-${escapeAttr(q.verdict || 'insufficient')}" title="How trustworthy this person's data is">${escapeHtml(q.verdict || 'insufficient')}</span>
        </header>
        <p class="report-line">${escapeHtml(p.prediction_text || '')}</p>
        ${p.typical_text ? `<p class="report-line text-dim">${escapeHtml(p.typical_text)}</p>` : ''}
        <h4>Typical times</h4>
        <div class="table-wrap"><table class="report-table"><thead><tr><th></th><th>Leaves</th><th>Gets home</th><th>Time out</th></tr></thead>
        <tbody>${row('Weekdays', typical.weekday || {})}${row('Weekends', typical.weekend || {})}</tbody></table></div>
        <h4>Last 14 days at home</h4>
        ${timelineSvg(p.timeline || [])}
        <p class="text-dim chart-note"><span class="swatch swatch-home"></span> at home <span class="swatch swatch-unknown"></span> scanner was off (unknown)</p>
        <h4>Hours at home per day (last 28 days)</h4>
        ${dailyBarsSvg(p.daily || [])}
        <p class="text-dim chart-note">Average on fully-watched days: weekdays ${avg('weekday')}, weekends ${avg('weekend')}. Faded bars are days the scanner was only on for part of the day.</p>
        <h4>Trend</h4>
        <p class="report-line">${trendHtml(p.trend)}</p>
        <h4>How reliable are the predictions?</h4>
        <p class="report-line">${accuracyHtml(p)}</p>
        <details class="quality-details"><summary>Data quality: ${escapeHtml(q.verdict || 'insufficient')}</summary>
            <p class="text-dim">${escapeHtml(q.signal || '')} signal \u00b7 ${q.observed_days || 0} fully-watched days \u00b7 ${q.outings || 0} usable outings \u00b7 ${q.home_periods || 0} home periods${q.home_periods_per_day ? ` (${q.home_periods_per_day} a day)` : ''}</p>
            ${reasons ? `<ul class="quality-reasons">${reasons}</ul>` : '<p class="text-dim">Nothing to flag.</p>'}
        </details>
    </article>`;
}

function householdHtml(h, untracked) {
    if (!h || !h.people || !h.people.length) return '';
    const names = h.people.map(n => escapeHtml(n.charAt(0).toUpperCase() + n.slice(1))).join(', ');
    const line = (label, w) => {
        const f = w && w.empty_from, u = w && w.empty_until;
        if (!f || !f.enough || !u || !u.enough) return `<li>${label}: <span class="text-dim">not enough data yet</span></li>`;
        return `<li>${label}: usually empty from <strong>${fmtHours(f.median)}</strong> until <strong>${fmtHours(u.median)}</strong> <span class="text-dim">(${f.n} empty periods)</span></li>`;
    };
    const hours = h.empty_h_per_day === null || h.empty_h_per_day === undefined ? '' :
        `<p class="report-line">Nobody home for about <strong>${h.empty_h_per_day} h a day</strong> on average (${h.observed_days} days watched).</p>`;
    const note = untracked && untracked.length
        ? `<p class="text-dim">Counts only tracked phones (${names}). ${escapeHtml(untracked.join(', '))} ${untracked.length === 1 ? 'has' : 'have'} no phone tracked, so ${untracked.length === 1 ? 'that person' : 'those people'} may be home when the house looks empty.</p>` : '';
    return `<article class="report-card"><header class="report-head"><h3>\u{1F3E0} The house</h3></header>${hours}
        <ul class="household-list">${line('Weekdays', h.weekday)}${line('Weekends', h.weekend)}</ul>${note}</article>`;
}

function renderReports(report) {
    const root = document.getElementById('reports-root');
    const meta = document.getElementById('reports-meta');
    if (!root) return;
    if (meta && report.generated_at) meta.textContent = 'Updated ' + new Date(report.generated_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
    if (!report.persons || !report.persons.length) {
        root.innerHTML = '<p class="text-dim">No phones are being tracked yet. Name a phone like "Sam\'s iPhone" (or set its Role to Phone) and reports will build up as it comes and goes.</p>' +
            ((report.untracked || []).length ? `<p class="text-dim">Known people without a phone: ${escapeHtml(report.untracked.join(', '))}.</p>` : '');
        return;
    }
    root.innerHTML = report.persons.map(personReportHtml).join('') + householdHtml(report.household, report.untracked);
}

async function loadReports() {
    const root = document.getElementById('reports-root');
    try {
        renderReports(await api('/api/reports'));
    } catch (e) {
        console.error('Failed to load reports:', e);
        if (root) root.innerHTML = '<p class="text-dim">Could not load the reports.</p>';
    }
}

function initReports() {
    loadReports();
    setInterval(loadReports, 5 * 60 * 1000);
}

// -- System health (watchdog results) --

const HEALTH_ICON = {ok: '\u{1F7E2}', warn: '\u{1F7E1}', fail: '\u{1F534}'};

function renderHealth(h) {
    const el = document.getElementById('health-strip');
    if (!el) return;
    const level = h.stale ? 'warn' : (h.worst >= 2 ? 'fail' : (h.worst === 1 ? 'warn' : 'ok'));
    const existing = el.querySelector('details');
    const open = existing ? existing.open : h.problems > 0;   // keep the user's choice across refreshes
    const rows = (h.checks || []).map(c =>
        `<li class="health-${escapeAttr(c.status)}">${HEALTH_ICON[c.status] || ''} ` +
        `<strong>${escapeHtml(c.label)}</strong> <span class="text-dim">${escapeHtml(c.message)}</span></li>`
    ).join('');
    el.hidden = false;
    el.className = 'health-strip health-strip-' + level;
    el.innerHTML = `<details${open ? ' open' : ''}><summary>${HEALTH_ICON[level]} Health: ` +
                   `${escapeHtml(h.summary)}</summary><ul class="health-list">${rows}</ul></details>`;
}

async function loadHealth() {
    try {
        renderHealth(await api('/api/health'));
    } catch (e) {
        console.error('Failed to load health:', e);
    }
}

// -- Housekeeping (stale device cleanup) --

let cleanupTimer = null;
const CLEANUP_REFRESH_INTERVAL = 60000;

function formatCount(n) {
    return Number(n).toLocaleString();
}

async function loadCleanup() {
    const summary = document.getElementById('cleanup-summary');
    const settingsEl = document.getElementById('cleanup-settings');
    const btn = document.getElementById('btn-cleanup');
    if (!summary) return;
    try {
        const p = await api('/api/cleanup/preview');
        const s = p.settings;
        summary.textContent =
            `${formatCount(p.total)} records stored · ${formatCount(p.protected)} protected (never removed) · ` +
            `${formatCount(p.to_delete)} due for deletion · ${formatCount(p.to_hide)} due to be hidden`;
        let mode = 'on';
        if (!s.enabled) mode = 'off';
        else if (s.dry_run) mode = 'in dry-run mode (logs only)';
        settingsEl.textContent =
            `Automatic cleanup is ${mode}: unnamed devices are hidden after ${s.hide_after_hours}h unseen, ` +
            `deleted after ${s.delete_short_lived_after_days}d (short-lived) or ${s.delete_other_after_days}d (others). ` +
            `Named, watchlisted, notify, paired and linked devices are never removed.`;
        btn.disabled = (p.to_delete + p.to_hide) === 0;
    } catch (e) {
        console.error('Failed to load cleanup status:', e);
        summary.textContent = 'Could not load cleanup status.';
    }
}

async function runCleanup() {
    const btn = document.getElementById('btn-cleanup');
    const result = document.getElementById('cleanup-result');
    if (!confirm('Permanently delete stale, unnamed device records now?\n\n' +
                 'Devices you have named, watchlisted, paired or linked are never touched. ' +
                 'A database backup is taken before the first cleanup.')) return;
    btn.disabled = true;
    result.textContent = 'Cleaning up...';
    try {
        const r = await api('/api/cleanup/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({dry_run: false}),
        });
        let msg = `Done: deleted ${formatCount(r.deleted)}, hid ${formatCount(r.hidden)}.`;
        if (r.backup) msg += ` Backup saved to ${r.backup}.`;
        if (r.vacuumed) msg += ' Database compacted.';
        result.textContent = msg;
    } catch (e) {
        console.error('Cleanup failed:', e);
        result.textContent = 'Cleanup failed - see the bt-web log.';
    }
    loadStats();
    loadDevices();
    loadCleanup();
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
                    always_on: document.getElementById('always-on').checked,
                    role: document.getElementById('device-role').value,
                    person: document.getElementById('device-person').value,
                    is_watchlisted: document.getElementById('is-watchlisted').checked,
                    is_notify: document.getElementById('is-notify').checked,
                    is_welcome: document.getElementById('is-welcome').checked,
                    is_hidden: document.getElementById('is-hidden').checked,
                    alexa_voice: document.getElementById('alexa-voice').value,
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
