/**
 * シフト自動作成くん - メインJavaScript
 * カレンダー描画・シフト生成・休み希望管理
 */

/* ============================================
   グローバル変数
   ============================================ */
let currentGenerationId = null;
let isGenerating = false;
let currentYear = null;
let currentMonth = null;
let currentStaffList = [];

/* ============================================
   CSRF トークン管理
   ============================================ */
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

/**
 * 更新系APIの応答を検証する。失敗時はサーバーのメッセージをそのまま投げる。
 * CSRFトークン切れ（ページを開きっぱなしにすると起きる）は原因が分かるよう
 * 案内したうえでページを再読み込みし、次の操作が通る状態に戻す。
 */
function ensureUpdateOk(response) {
    if (response.ok) return null;
    return response.json().catch(() => ({})).then(j => {
        if (j.csrf_error) {
            alert(j.error || 'セキュリティトークンの有効期限が切れました。ページを再読み込みします。');
            location.reload();
            return null;
        }
        throw new Error(j.error || '更新失敗');
    });
}

function fetchWithCsrf(url, options = {}) {
    const headers = options.headers || {};
    if (!headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }
    headers['X-CSRFToken'] = getCsrfToken();
    return fetch(url, { ...options, headers });
}

/* ============================================
   配置タイプの定義
   ============================================ */
const ASSIGNMENT_MAP = {
    day_pattern1:    { label: 'デイ8:30-17:30',  badgeClass: 'badge-day-full'  },
    day_pattern2:    { label: 'デイ9:00-16:00',  badgeClass: 'badge-day-p2'   },
    day_pattern3:    { label: 'デイ午前のみ',     badgeClass: 'badge-day-am'   },
    day_pattern4:    { label: 'デイ午後のみ',     badgeClass: 'badge-day-pm'   },
    early:           { label: '早番7:30-16:30',  badgeClass: 'badge-day-full' },
    late:            { label: '遅番9:30-18:30',  badgeClass: 'badge-day-p2'   },
    nurse_short:     { label: '看護9:30-13:30',  badgeClass: 'badge-visit-am' },
    visit_am:        { label: '訪問午前のみ',     badgeClass: 'badge-visit-am'  },
    visit_pm:        { label: '訪問午後のみ',     badgeClass: 'badge-visit-pm'  },
    day_p3_visit_pm: { label: '兼務(デイ→訪問)',  badgeClass: 'badge-dual-a'    },
    visit_am_day_p4: { label: '兼務(訪問→デイ)',  badgeClass: 'badge-dual-b'    },
    cooking_1:      { label: '①6-8',            badgeClass: 'badge-cook-1'    },
    cooking_2:    { label: '②8-13',           badgeClass: 'badge-cook-2'    },
    cooking_3:       { label: '③12-19',          badgeClass: 'badge-cook-3'    },
    cooking_4:       { label: '④6-13',           badgeClass: 'badge-cook-4'    },
    cooking_5:        { label: '⑤9-15',           badgeClass: 'badge-cook-2'    },
    // 旧名の後方互換
    day_am:          { label: 'デイ午前のみ',     badgeClass: 'badge-day-am'   },
    day_pm:          { label: 'デイ午後のみ',     badgeClass: 'badge-day-pm'   },
    day_am_visit_pm: { label: '兼務(デイ→訪問)',  badgeClass: 'badge-dual-a'   },
    visit_am_day_pm: { label: '兼務(訪問→デイ)',  badgeClass: 'badge-dual-b'   },
};

// デイ午前に寄与するアサインメント
const DAY_AM_SET = new Set([
    'day_pattern1', 'day_pattern2', 'day_pattern3', 'day_p3_visit_pm',
    'day_am', 'day_am_visit_pm', 'late',
]);
// デイ午後に寄与するアサインメント
const DAY_PM_SET = new Set([
    'day_pattern1', 'day_pattern2', 'day_pattern4', 'visit_am_day_p4',
    'day_pm', 'visit_am_day_pm', 'early', 'late',
]);
// 訪問午前
const VISIT_AM_SET = new Set(['visit_am', 'visit_am_day_p4', 'visit_am_day_pm']);
// 訪問午後
const VISIT_PM_SET = new Set(['visit_pm', 'day_p3_visit_pm', 'day_am_visit_pm']);
// 兼務
const DUAL_SET = new Set(['day_p3_visit_pm', 'visit_am_day_p4', 'day_am_visit_pm', 'visit_am_day_pm']);

// ③ 相談員事務スロットラベル
const DESK_SLOT_LABELS = ['9-11時', '11-13時', '13-15時', '15-17時'];

const DAY_NAMES = ['日', '月', '火', '水', '木', '金', '土'];

/* ============================================
   カレンダーページ初期化
   ============================================ */
function initCalendarPage() {
    const yearSelect = document.getElementById('year-select');
    const monthSelect = document.getElementById('month-select');

    if (!yearSelect || !monthSelect) return;

    const now = new Date();
    const currentYear = now.getFullYear();
    const currentMonth = now.getMonth() + 1;

    for (let y = currentYear - 1; y <= currentYear + 1; y++) {
        const opt = document.createElement('option');
        opt.value = y;
        opt.textContent = y + '年';
        if (y === currentYear) opt.selected = true;
        yearSelect.appendChild(opt);
    }

    monthSelect.value = currentMonth;

    yearSelect.addEventListener('change', () => loadShifts());
    monthSelect.addEventListener('change', () => loadShifts());

    loadShifts();
}

/* ============================================
   シフトデータ読み込み
   ============================================ */
function loadShifts(year, month) {
    const yearSelect = document.getElementById('year-select');
    const monthSelect = document.getElementById('month-select');

    if (!yearSelect || !monthSelect) return;

    year = year || parseInt(yearSelect.value);
    month = month || parseInt(monthSelect.value);

    showLoading('シフトデータを読み込み中...');

    fetch(`/api/shifts/${year}/${month}`)
        .then(response => {
            if (!response.ok) {
                throw new Error('シフトデータの取得に失敗しました。');
            }
            return response.json();
        })
        .then(data => {
            hideLoading();

            const hasShifts = data.shifts && data.shifts.length > 0;
            const hasWarnings = data.warnings && data.warnings.length > 0;

            currentYear = year;
            currentMonth = month;

            if (hasShifts) {
                currentGenerationId = data.generation_id;
                currentStaffList = data.staff_list || [];
                populateIndividualStaffSelect();
                renderCalendar(data, year, month);
                renderWarnings(data.warnings || []);
                renderConfirmation(data.confirmation);
                showElement('calendar-container');
                showElement('export-buttons');
                showElement('edit-bar');
                renderPalette(data);
                hideElement('no-data-message');
            } else if (hasWarnings) {
                // W-13: シフト0件でも警告があれば表示
                currentGenerationId = data.generation_id;
                renderWarnings(data.warnings);
                renderConfirmation(null);
                hideElement('calendar-container');
                hideElement('export-buttons');
                hideElement('edit-bar');
                hideElement('no-data-message');
            } else {
                currentGenerationId = null;
                renderConfirmation(null);
                hideElement('calendar-container');
                hideElement('export-buttons');
                hideElement('edit-bar');
                hideElement('warnings-container');
                showElement('no-data-message');
            }
        })
        .catch(error => {
            hideLoading();
            hideElement('calendar-container');
            hideElement('export-buttons');
            showElement('no-data-message');
            console.error('Error loading shifts:', error);
        });
}

/* ============================================
   シフト生成
   ============================================ */
function generateShift() {
    const yearSelect = document.getElementById('year-select');
    const monthSelect = document.getElementById('month-select');
    const generateBtn = document.getElementById('generate-btn');

    if (!yearSelect || !monthSelect) return;

    const year = parseInt(yearSelect.value);
    const month = parseInt(monthSelect.value);

    let msg = `${year}年${month}月のシフトを自動生成します。\nよろしいですか？`;
    if (currentGenerationId) {
        msg = `${year}年${month}月のシフトは既に生成済みです。\n再生成すると現在のシフトは上書きされ、元に戻せません。\n\n本当に再生成しますか？`;
    }
    if (!confirm(msg)) {
        return;
    }

    isGenerating = true;
    // W-11: ボタン無効化
    if (generateBtn) {
        generateBtn.disabled = true;
        generateBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
    showLoading('シフトを生成中...');
    hideElement('calendar-container');
    hideElement('export-buttons');
    hideElement('no-data-message');
    hideElement('warnings-container');

    fetchWithCsrf('/api/generate', {
        method: 'POST',
        body: JSON.stringify({ year: year, month: month }),
    })
        .then(response => {
            // 依頼文30: サーバがHTML(タイムアウト502等)を返した場合に
            // 「Unexpected token '<'」ではなく分かりやすいメッセージにする。
            const ct = response.headers.get('content-type') || '';
            if (!ct.includes('application/json')) {
                // 依頼文34: 400/403はCSRFトークン期限切れ等（時間超過ではない）。
                // 実態に合うメッセージを出し、リロードを促す。
                if (response.status === 400 || response.status === 403) {
                    throw new Error(`セキュリティトークンの有効期限が切れた可能性があります（${response.status}）。ページを再読み込み（リロード）してから、もう一度生成してください。`);
                }
                throw new Error(`サーバーエラー（${response.status}）が発生しました。生成に時間がかかり過ぎた可能性があります。少し待ってから再度お試しください。`);
            }
            if (!response.ok) {
                return response.json().then(err => {
                    throw new Error(err.error || err.message || 'シフト生成に失敗しました。');
                });
            }
            return response.json();
        })
        .then(data => {
            isGenerating = false;
            hideLoading();
            // W-11: ボタン再有効化
            if (generateBtn) {
                generateBtn.disabled = false;
                generateBtn.classList.remove('opacity-50', 'cursor-not-allowed');
            }

            if (data.status === 'success') {
                let msg = `シフトを生成しました。（${data.shift_count || 0}件）`;
                if (data.warning_count > 0) {
                    msg += `\n${data.warning_count}件の警告があります。`;
                }
                alert(msg);
                loadShifts(year, month);
            } else {
                alert('シフト生成に失敗しました: ' + (data.error || data.message || '不明なエラー'));
                showElement('no-data-message');
            }
        })
        .catch(error => {
            isGenerating = false;
            hideLoading();
            // W-11: ボタン再有効化
            if (generateBtn) {
                generateBtn.disabled = false;
                generateBtn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
            showElement('no-data-message');
            alert('エラーが発生しました: ' + error.message);
            console.error('Error generating shift:', error);
        });
}

/* ============================================
   カレンダー描画
   ============================================ */
/* ============================================
   依頼文28: 職員ごとのシフト固定 ON/OFF
   ============================================ */
function toggleShiftFix(staffId, makeFixed) {
    const y = currentYear, m = currentMonth;
    if (!y || !m) return;
    fetchWithCsrf('/api/shift-fix', {
        method: 'POST',
        body: JSON.stringify({ staff_id: staffId, year: y, month: m, fixed: makeFixed }),
    })
        .then(r => r.json())
        .then(res => {
            if (res && res.error) { alert(res.error); loadShifts(y, m); return; }
            // 固定状態は次回の再生成から反映される。表示を更新してチェック状態を確定。
            loadShifts(y, m);
        })
        .catch(() => { alert('固定の切り替えに失敗しました。'); loadShifts(y, m); });
}

/* ============================================
   シフトの手直し（画面で直接編集）
   ============================================ */
let pendingEdits = {};        // "date|staff_id" -> {assignment?, visit_slot?}
let currentShiftData = null;  // 直近に読み込んだ /api/shifts のデータ
let editUndoStack = [];       // 「1つ前に戻す」用（1操作 = 1グループ）
let editBaseline = {};        // 保存済みの内容 "date|staff_id" -> {assignment, visit_slot}
let undoCollector = null;     // 1操作分の変更前の状態を集める

function editKey(dateStr, staffId) { return `${dateStr}|${staffId}`; }

function rebuildEditBaseline(data) {
    editBaseline = {};
    (data.shifts || []).forEach(x => {
        editBaseline[editKey(x.date, x.staff_id)] = {
            assignment: x.assignment || '',
            visit_slot: x.visit_slot || null,
        };
    });
    editUndoStack = [];
    updatePendingBadge();
}

function cellStateOf(dateStr, staffId) {
    const row = (currentShiftData.shifts || []).find(
        x => x.date === dateStr && x.staff_id === staffId);
    return {
        assignment: row ? (row.assignment || '') : '',
        visit_slot: row ? (row.visit_slot || null) : null,
    };
}

// 変更前の状態を控える（1操作分をまとめて戻せるように）
function recordUndo(dateStr, staffId) {
    const entry = Object.assign({ key: editKey(dateStr, staffId), date: dateStr, staffId: staffId },
                                cellStateOf(dateStr, staffId));
    if (undoCollector) undoCollector.push(entry);
    else editUndoStack.push([entry]);
}

function beginEditGroup(fn) {
    undoCollector = [];
    try { fn(); } finally {
        if (undoCollector.length) editUndoStack.push(undoCollector);
        undoCollector = null;
        updatePendingBadge();
    }
}

// 直前の1操作だけ元に戻す
function undoLastEdit() {
    const group = editUndoStack.pop();
    if (!group || !group.length) { setEditStatus('戻せる操作がありません'); return; }
    group.slice().reverse().forEach(e => {
        const shifts = currentShiftData.shifts || [];
        const idx = shifts.findIndex(x => x.date === e.date && x.staff_id === e.staffId);
        if (!e.assignment) {
            if (idx >= 0) shifts.splice(idx, 1);
        } else if (idx >= 0) {
            shifts[idx] = Object.assign({}, shifts[idx],
                { assignment: e.assignment, visit_slot: e.visit_slot });
        } else {
            shifts.push({ date: e.date, staff_id: e.staffId,
                          assignment: e.assignment, visit_slot: e.visit_slot });
        }
        syncPendingWithBaseline(e.key, e);
    });
    renderCalendar(currentShiftData, currentYear, currentMonth);
    checkPublicHolidayLocally();
    updatePendingBadge();
    setEditStatus('1つ前の状態に戻しました', 'ok');
}

// 保存済みの内容と同じに戻ったら「未保存の変更」から外す
function syncPendingWithBaseline(key, state) {
    const base = editBaseline[key] || { assignment: '', visit_slot: null };
    if ((state.assignment || '') === (base.assignment || '')
        && (state.visit_slot || null) === (base.visit_slot || null)) {
        delete pendingEdits[key];
    } else {
        pendingEdits[key] = { assignment: state.assignment || '', visit_slot: state.visit_slot || null };
    }
}

function renderPalette(data) {
    currentShiftData = data;
    rebuildEditBaseline(data);
    const pal = data.palette || {};
    const build = (id, items, title) => {
        const box = document.getElementById(id);
        if (!box) return;
        if (!items || !items.length) { box.innerHTML = ''; return; }
        box.innerHTML = `<span class="text-xs font-bold text-gray-500 mr-1">${title}</span>`
            + items.map(it =>
                `<span class="palette-chip badge ${(ASSIGNMENT_MAP[it.code] || {}).badgeClass || 'badge-off'}"`
                + ` draggable="true" data-code="${it.code}" style="cursor:grab">${escapeHtml(it.label)}</span>`
            ).join('')
            + `<span class="palette-chip badge badge-off" draggable="true" data-code="off"`
            + ` style="cursor:grab;border:1px dashed #9ca3af">休み</span>`;
    };
    build('palette-care', pal.care, '介護・看護:');
    build('palette-cook', pal.cooking, '調理:');
    updatePendingBadge();
}

function setEditStatus(msg, kind) {
    const el = document.getElementById('edit-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'text-sm font-bold ' + (
        kind === 'error' ? 'text-red-600' : kind === 'ok' ? 'text-emerald-700' : 'text-gray-600'
    );
    if (msg && kind === 'ok') {
        clearTimeout(window.__editStatusTimer);
        window.__editStatusTimer = setTimeout(() => { el.textContent = ''; }, 6000);
    }
}

function updatePendingBadge() {
    const n = Object.keys(pendingEdits).length;
    const badge = document.getElementById('pending-count');
    const btn = document.getElementById('save-edits-btn');
    const undoBtn = document.getElementById('undo-edit-btn');
    if (badge) badge.textContent = n ? `未保存の変更 ${n}件` : '';
    if (btn) btn.disabled = n === 0;
    if (undoBtn) undoBtn.disabled = editUndoStack.length === 0;
}

function applyLocalEdit(dateStr, staffId, code) {
    if (!currentShiftData) return;
    recordUndo(dateStr, staffId);
    const shifts = currentShiftData.shifts || [];
    const idx = shifts.findIndex(x => x.date === dateStr && x.staff_id === staffId);
    if (code === '' || code === 'off' || code === 'cook_off') {
        if (idx >= 0) shifts.splice(idx, 1);
    } else if (idx >= 0) {
        shifts[idx] = Object.assign({}, shifts[idx], {
            assignment: code, bath_role: null, break_start: null, counselor_desk_slots: null,
        });
    } else {
        shifts.push({ date: dateStr, staff_id: staffId, assignment: code });
    }
    syncPendingWithBaseline(editKey(dateStr, staffId), cellStateOf(dateStr, staffId));
    afterLocalEdit();
}

// 訪問へ出る時間帯だけを変える（シフト本体はそのまま）
function applyLocalVisitSlot(dateStr, staffId, slot) {
    if (!currentShiftData) return;
    recordUndo(dateStr, staffId);
    const shifts = currentShiftData.shifts || [];
    const idx = shifts.findIndex(x => x.date === dateStr && x.staff_id === staffId);
    if (idx >= 0) {
        shifts[idx] = Object.assign({}, shifts[idx], { visit_slot: slot });
    } else if (slot) {
        // 休みの人に訪問だけを割り当てる
        shifts.push({
            date: dateStr, staff_id: staffId,
            assignment: slot === 'am' ? 'visit_am' : 'visit_pm', visit_slot: slot,
        });
    }
    syncPendingWithBaseline(editKey(dateStr, staffId), cellStateOf(dateStr, staffId));
    afterLocalEdit();
}

function afterLocalEdit() {
    renderCalendar(currentShiftData, currentYear, currentMonth);
    checkPublicHolidayLocally();
    updatePendingBadge();
}

// 公休（休みの日数）が目標とずれていたら、その場で警告として出す
function checkPublicHolidayLocally() {
    if (!currentShiftData) return;
    const days = new Date(currentYear, currentMonth, 0).getDate();
    const work = {};
    (currentShiftData.shifts || []).forEach(x => {
        if (x.assignment && x.assignment !== 'off' && x.assignment !== 'cook_off') {
            work[x.staff_id] = (work[x.staff_id] || 0) + 1;
        }
    });
    const extra = [];
    (currentShiftData.staff_list || []).forEach(s => {
        const target = s.public_holiday_target || 0;
        if (!target) return;
        const off = days - (work[s.id] || 0);
        if (off !== target) {
            const diff = off - target;
            extra.push({
                date: `${currentYear}-${String(currentMonth).padStart(2, '0')}-01`,
                warning_type: 'public_holiday_unmet',
                message: `公休日数: ${s.name} 目標${target}日 / 実際${off}日（差${diff > 0 ? '+' : ''}${diff}日）`,
            });
        }
    });
    const base = (currentShiftData.warnings || []).filter(w => w.warning_type !== 'public_holiday_unmet');
    renderWarnings(base.concat(extra));
}

function discardShiftEdits() {
    if (!Object.keys(pendingEdits).length) { setEditStatus('取り消す変更はありません'); return; }
    pendingEdits = {};
    editUndoStack = [];
    setEditStatus('変更をすべて取り消しました（保存済みの内容に戻しました）', 'ok');
    loadShifts(currentYear, currentMonth);
}

function saveShiftEdits() {
    const changes = Object.entries(pendingEdits).map(([k, v]) => {
        const parts = k.split('|');
        const ch = { date: parts[0], staff_id: Number(parts[1]) };
        if (v.assignment !== undefined) ch.assignment = v.assignment;
        if (v.visit_slot !== undefined) ch.visit_slot = v.visit_slot;
        return ch;
    });
    if (!changes.length) return;
    const btn = document.getElementById('save-edits-btn');
    if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }
    fetchWithCsrf('/api/shift/cells', {
        method: 'POST',
        body: JSON.stringify({ year: currentYear, month: currentMonth, changes: changes }),
    })
        .then(r => r.json().then(j => ({ ok: r.ok, j: j })))
        .then(res => {
            if (!res.ok) throw new Error(res.j.error || '保存に失敗しました');
            pendingEdits = {};
            editUndoStack = [];
            setEditStatus(`保存しました（${res.j.applied}件）`, 'ok');
            loadShifts(currentYear, currentMonth);
        })
        .catch(e => setEditStatus('保存に失敗しました: ' + e.message, 'error'))
        .finally(() => {
            if (btn) { btn.textContent = '変更を保存'; }
            updatePendingBadge();
        });
}

// ドラッグ&ドロップ（表とパレットにイベント委譲で1度だけ登録）
function initShiftDragAndDrop() {
    const table = document.getElementById('calendar-table');
    const bar = document.getElementById('edit-bar');
    if (!table || table.dataset.dndReady === '1') return;
    table.dataset.dndReady = '1';

    if (bar && bar.dataset.dndReady !== '1') {
        bar.dataset.dndReady = '1';
        bar.addEventListener('dragstart', e => {
            const chip = e.target.closest('.palette-chip');
            if (!chip) return;
            window.__dragInfo = { type: 'palette', code: chip.dataset.code };
            e.dataTransfer.effectAllowed = 'copy';
            e.dataTransfer.setData('text/plain', chip.dataset.code);
        });
    }

    table.addEventListener('dragstart', e => {
        const chip = e.target.closest('.visit-chip');
        if (chip) {
            // 「訪問（午前/午後）」の札だけを切り離して別の職員へ渡す
            const cellTd = chip.closest('td.shift-cell');
            window.__dragInfo = {
                type: 'visit', code: 'visit_am', slot: chip.dataset.slot || 'am',
                date: chip.dataset.date, staffId: Number(chip.dataset.staff), group: 'care',
                explicit: !!(currentShiftData && (currentShiftData.shifts || []).some(
                    x => x.date === chip.dataset.date
                      && x.staff_id === Number(chip.dataset.staff) && x.visit_slot)),
            };
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', 'visit_am');
            e.stopPropagation();
            return;
        }
        const td = e.target.closest('td.shift-cell');
        if (!td || !td.dataset.assignment) return;
        window.__dragInfo = {
            type: 'cell', code: td.dataset.assignment, date: td.dataset.date,
            staffId: Number(td.dataset.staff), group: td.dataset.group,
        };
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', td.dataset.assignment);
    });
    table.addEventListener('dragover', e => {
        const td = e.target.closest('td.shift-cell');
        if (!td || !window.__dragInfo) return;
        e.preventDefault();
        td.style.outline = '2px dashed #10b981';
    });
    table.addEventListener('dragleave', e => {
        const td = e.target.closest('td.shift-cell');
        if (td) td.style.outline = '';
    });
    table.addEventListener('drop', e => {
        const td = e.target.closest('td.shift-cell');
        const info = window.__dragInfo;
        if (!td || !info) return;
        e.preventDefault();
        td.style.outline = '';
        const toDate = td.dataset.date;
        const toStaff = Number(td.dataset.staff);
        const toGroup = td.dataset.group;
        const code = info.code;
        const isCookCode = code.indexOf('cooking_') === 0 || code === 'cook_off';
        if (code !== 'off' && isCookCode !== (toGroup === 'cooking')) {
            setEditStatus('介護・看護と調理のシフトは入れ替えられません', 'error');
            window.__dragInfo = null;
            return;
        }
        if (info.type === 'visit') {
            if (toGroup === 'cooking') {
                setEditStatus('訪問は介護・看護の職員にだけ割り当てられます', 'error');
                window.__dragInfo = null;
                return;
            }
            if (info.date !== toDate) {
                setEditStatus('別の日へは移動できません。同じ日の別の職員へドラッグしてください', 'error');
                window.__dragInfo = null;
                return;
            }
            if (info.staffId === toStaff) { window.__dragInfo = null; return; }
            const slot = info.slot || 'am';
            beginEditGroup(() => {
                // 元の人が明示の訪問担当だったら外す（早番の暗黙分は表示だけ）
                if (info.explicit) applyLocalVisitSlot(info.date, info.staffId, null);
                // 相手はいまのシフトのまま「訪問（午前/午後）」を付ける
                applyLocalVisitSlot(toDate, toStaff, slot);
            });
            setEditStatus(
                slot === 'am' ? '午前訪問を移しました（シフトはそのままで訪問（午前）が付きます）'
                              : '午後訪問を移しました', 'ok');
            window.__dragInfo = null;
            return;
        }
        if (info.type === 'cell') {
            if (info.date !== toDate) {
                setEditStatus('別の日へは移動できません。同じ日の別の職員へドラッグしてください', 'error');
                window.__dragInfo = null;
                return;
            }
            if (info.staffId === toStaff) { window.__dragInfo = null; return; }
            beginEditGroup(() => {
                applyLocalEdit(info.date, info.staffId, '');   // 移動元は休みにする
                applyLocalEdit(toDate, toStaff, code === 'off' ? '' : code);
            });
            window.__dragInfo = null;
            return;
        }
        applyLocalEdit(toDate, toStaff, code === 'off' ? '' : code);
        window.__dragInfo = null;
    });

    // オンコール担当の入れ替え（プルダウン）
    table.addEventListener('change', e => {
        const sel = e.target.closest('.oncall-select');
        if (!sel) return;
        const value = sel.value ? Number(sel.value) : null;
        fetchWithCsrf('/api/oncall', {
            method: 'POST',
            body: JSON.stringify({ date: sel.dataset.date, staff_id: value }),
        })
            .then(r => r.json().then(j => ({ ok: r.ok, j: j })))
            .then(res => {
                if (!res.ok) throw new Error(res.j.error || '変更に失敗しました');
                if (currentShiftData) {
                    currentShiftData.oncall = currentShiftData.oncall || {};
                    currentShiftData.oncall[res.j.date] = res.j.name || '';
                }
                setEditStatus(`オンコールを変更しました（${res.j.date}: ${res.j.name || '未割当'}）`, 'ok');
            })
            .catch(err => {
                setEditStatus('オンコールの変更に失敗しました: ' + err.message, 'error');
                loadShifts(currentYear, currentMonth);
            });
    });
}

function renderCalendar(data, year, month) {
    const table = document.getElementById('calendar-table');
    if (!table) return;
    currentShiftData = data;

    // 調理シフト種類マスタのラベルを取り込む（新種類の表示用。既存①〜⑤は上書きしない）
    if (data.cook_labels) {
        const cookBadges = ['badge-cook-1', 'badge-cook-2', 'badge-cook-3', 'badge-cook-4'];
        let bi = 0;
        for (const code of Object.keys(data.cook_labels)) {
            if (data.cook_labels[code] && !ASSIGNMENT_MAP[code]) {
                ASSIGNMENT_MAP[code] = { label: data.cook_labels[code], badgeClass: cookBadges[bi % 4] };
                bi++;
            }
        }
    }

    const shifts = data.shifts || [];
    const staffList = data.staff_list || [];
    const fixedSet = new Set(data.fixed_staff_ids || []);  // 依頼文28: 固定中の職員ID
    const holidays = data.holidays || {};
    const oncallMap = data.oncall || {};  // {date: 氏名} オンコール担当
    const dayOffMap = data.day_off_requests || {};  // {date: [staff_id,...]} 休み希望
    const parkingMap = data.parking || {};  // 駐車場 {date: {staff_id: "4"/"7"/"8"/"コイン"}}

    // 駐車場バッジ（車通勤・出勤者のみ map に存在）
    function parkingBadge(dateStr, staffId) {
        const byStaff = parkingMap[dateStr];
        const label = byStaff && byStaff[staffId];
        if (!label) return '';
        if (label === 'コイン') {
            return ' <span class="badge" style="background:#9ca3af;color:#fff">コイン</span>';
        }
        return ` <span class="badge" style="background:#7c3aed;color:#fff">P${escapeHtml(label)}</span>`;
    }

    const careStaff = staffList.filter(s => s.department !== 'cooking');
    const cookingStaff = staffList.filter(s => s.department === 'cooking');
    const hasCooking = cookingStaff.length > 0;

    // ② 看護師/PTのIDセット（デイ人数カウントから除外）
    const nursePtIds = new Set();
    careStaff.forEach(s => {
        if (isNurseOrPtStaff(s)) {
            nursePtIds.add(s.id);
        }
    });

    const shiftMap = {};
    const phoneDutyMap = {};
    const deskSlotMap = {};  // ③ {date: {staff_id: [slot_idx, ...]}}
    const bathMap = {};      // お風呂当番 {date: {staff_id: "中"/"外"}}
    const mealMap = {};      // 食事介助 {date: {staff_id: "12:00-13:00"}}
    const visitMap = {};     // 訪問へ出る時間帯 {date: {staff_id: "am"/"pm"}}
    shifts.forEach(s => {
        if (!shiftMap[s.date]) shiftMap[s.date] = {};
        shiftMap[s.date][s.staff_id] = s.assignment;
        if (s.visit_slot) {
            if (!visitMap[s.date]) visitMap[s.date] = {};
            visitMap[s.date][s.staff_id] = s.visit_slot;
        }
        if (s.is_phone_duty) {
            if (!phoneDutyMap[s.date]) phoneDutyMap[s.date] = {};
            phoneDutyMap[s.date][s.staff_id] = true;
        }
        if (s.counselor_desk_slots && s.counselor_desk_slots.length > 0) {
            if (!deskSlotMap[s.date]) deskSlotMap[s.date] = {};
            deskSlotMap[s.date][s.staff_id] = s.counselor_desk_slots;
        }
        if (s.bath_role) {
            if (!bathMap[s.date]) bathMap[s.date] = {};
            bathMap[s.date][s.staff_id] = s.bath_role;
        }
        if (s.meal_assist) {
            if (!mealMap[s.date]) mealMap[s.date] = {};
            mealMap[s.date][s.staff_id] = s.meal_assist;
        }
    });

    const daysInMonth = new Date(year, month, 0).getDate();

    // --- 階別の デイ利用日／訪問日（曜日ルール）---
    //   2階・3階・外部デイそれぞれの曜日は設定画面が正。API の operating_days から受け取る。
    //   API が未対応の場合は従来どおりの曜日にフォールバックする。
    //   設定側は 0=月〜6=日、JS の getDay() は 0=日〜6=土 なので添字を揃えて保持する。
    const _opDays = data.operating_days || {};
    function toGetDaySet(list, fallback) {
        // 未設定(undefined)なら既定値。空配列は「その曜日は無し」の明示指定として尊重する。
        const src = Array.isArray(list) ? list : fallback;
        // 0=月..6=日 → getDay() の 0=日..6=土 へ変換
        return new Set(src.map(d => (d + 1) % 7));
    }
    const F3_DAY = toGetDaySet(_opDays.floor3_day_service, [1, 4, 6]); // 既定 火金日
    const F3_VISIT = toGetDaySet(_opDays.floor3_visit, [0, 3]);        // 既定 月木
    const F2_DAY = toGetDaySet(_opDays.floor2_day_service, [0, 3, 5]); // 既定 月木土
    const F2_VISIT = toGetDaySet(_opDays.floor2_visit, [1, 4]);        // 既定 火金
    const EXT_DAY = toGetDaySet(_opDays.external_day_service, [2]);    // 既定 水
    // 3階=黄・2階=橙・外部デイ=灰、訪問は青文字
    function badge(text, bg) {
        return `<span style="display:inline-block;background:${bg};color:#333;`
             + `font-size:8px;line-height:1.3;padding:0 2px;border-radius:2px">${text}</span>`;
    }
    function visitBadge(text) {
        return `<span style="display:inline-block;color:#1d4ed8;font-weight:700;`
             + `font-size:8px;line-height:1.3;margin-left:1px">${text}</span>`;
    }
    function floorAnnotHtml(dow) {
        let s = '';
        if (F3_DAY.has(dow)) s += badge('デイ3階', '#FDE047');
        if (F2_DAY.has(dow)) s += badge('デイ2階', '#FDBA74');
        if (EXT_DAY.has(dow)) s += badge('外部デイ', '#CBD5E1');
        if (F3_VISIT.has(dow)) s += visitBadge('訪3階');
        if (F2_VISIT.has(dow)) s += visitBadge('訪2階');
        return s ? `<br>${s}` : '';
    }

    // 各日付（列）のメタ情報を事前計算（縦＝職員名・横＝日付）
    const dayMeta = [];
    for (let day = 1; day <= daysInMonth; day++) {
        const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const dayOfWeek = new Date(year, month - 1, day).getDay();
        let colClass = '';
        if (holidays[dateStr]) {
            colClass = 'row-holiday';
        } else if (dayOfWeek === 6) {
            colClass = 'row-saturday';
        } else if (dayOfWeek === 0) {
            colClass = 'row-sunday';
        }
        const holidayName = holidays[dateStr]
            ? `<br><span style="font-size:9px;font-weight:normal;color:#b45309">${escapeHtml(holidays[dateStr])}</span>` : '';
        dayMeta.push({
            dateStr,
            colClass,
            isVisitDay: F3_VISIT.has(dayOfWeek) || F2_VISIT.has(dayOfWeek),
            label: `${month}/${day}<br>${DAY_NAMES[dayOfWeek]}${holidayName}${floorAnnotHtml(dayOfWeek)}`,
        });
    }

    const totalCols = daysInMonth + 2;   // 職員名 + 日付 + 出勤日数

    // ヘッダー行（日付が横並び）
    function headerRowHtml() {
        let h = '<tr>';
        h += '<th class="date-cell" style="text-align:left;min-width:120px;left:0;position:sticky;z-index:11">職員名</th>';
        dayMeta.forEach(m => {
            h += `<th class="date-cell ${m.colClass}" style="min-width:62px">${m.label}</th>`;
        });
        h += '<th class="date-cell">出勤<br>日数</th>';
        h += '</tr>';
        return h;
    }

    // セクション見出し行
    function sectionTitleRow(title) {
        return `<tr><td colspan="${totalCols}" style="background:#1f2937;color:#fff;font-weight:700;text-align:left;padding:6px 10px">${title}</td></tr>`;
    }

    // その日に「午前訪問」を明示的に割り当てられた職員がいるか
    function hasExplicitAmVisit(dateStr) {
        const day = shiftMap[dateStr] || {};
        if (Object.values(day).some(a => VISIT_AM_SET.has(a))) return true;
        const vs = visitMap[dateStr] || {};
        return Object.values(vs).some(v => v === 'am');
    }

    // その日付が訪問の営業日か（2階・3階いずれかが訪問の曜日）
    function isVisitDate(dateStr) {
        const parts = String(dateStr).split('-').map(Number);
        if (parts.length !== 3) return false;
        const dow = new Date(parts[0], parts[1] - 1, parts[2]).getDay();
        return F3_VISIT.has(dow) || F2_VISIT.has(dow);
    }

    // 休みのセル。本人が休み希望を出していた日は「希望休」と分かるようにする。
    function offCellHtml(dateStr, s) {
        const ids = dayOffMap[dateStr] || [];
        if (ids.indexOf(s.id) !== -1) {
            return '<span class="badge" style="background:#fde68a;color:#92400e">希望休</span>';
        }
        return '<span class="badge badge-off">休</span>';
    }

    // ケアスタッフ 1 セルの中身
    function careCellHtml(dateStr, s) {
        const assignment = shiftMap[dateStr] ? shiftMap[dateStr][s.id] : null;
        if (!assignment || assignment === 'off') {
            return offCellHtml(dateStr, s);
        }
        const info = ASSIGNMENT_MAP[assignment];
        const isPhone = phoneDutyMap[dateStr] && phoneDutyMap[dateStr][s.id];
        const phoneBadge = isPhone ? ' <span class="badge badge-phone">TEL</span>' : '';
        const deskSlots = deskSlotMap[dateStr] && deskSlotMap[dateStr][s.id];
        let deskLabel = '';
        if (deskSlots && deskSlots.length > 0) {
            const isFullDay = [0, 1, 2, 3].every(i => deskSlots.includes(i));
            if (isFullDay) {
                deskLabel = `<br><span style="font-size:9px;color:#6b7280">相談（終日）</span>`;
            } else {
                const slotTexts = deskSlots.map(si => DESK_SLOT_LABELS[si] || '').filter(Boolean);
                if (slotTexts.length > 0) {
                    deskLabel = `<br><span style="font-size:9px;color:#6b7280">相談 ${slotTexts.join(',')}</span>`;
                }
            }
        }
        const bathRole = bathMap[dateStr] && bathMap[dateStr][s.id];
        const bathDisplay = bathRole ? ` <span class="badge" style="background:#0ea5e9;color:#fff">${bathRole}介助</span>` : '';
        const badge = info
            ? `<span class="badge ${info.badgeClass}">${info.label}</span>`
            : `<span class="badge badge-off">${escapeHtml(assignment)}</span>`;
        // 訪問営業日の早番(7:30-16:30)は既定で午前が訪問。
        //   ただし、その日に別の職員へ午前訪問を割り当てたら早番からは外す
        //   （早番と訪問（午前）を分けて動かせるようにするため）。
        // 訪問へ出る時間帯（明示指定）。元のシフト表示の下に付ける。
        const slot = visitMap[dateStr] && visitMap[dateStr][s.id];
        const slotNote = slot
            ? `<br><span class="badge visit-chip" draggable="true" data-date="${dateStr}"`
              + ` data-staff="${s.id}" data-slot="${slot}"`
              + ` title="この札を別の職員のマスへドラッグすると、訪問だけを移せます"`
              + ` style="background:#dbeafe;color:#1d4ed8;cursor:grab">訪問（${slot === 'am' ? '午前' : '午後'}）</span>`
            : '';
        // 訪問営業日の早番は既定で午前が訪問（明示の担当がいない日だけ表示）
        const visitNote = slotNote || ((assignment === 'early' && isVisitDate(dateStr)
                           && !hasExplicitAmVisit(dateStr))
            ? `<br><span class="badge visit-chip" draggable="true" data-date="${dateStr}"`
              + ` data-staff="${s.id}" data-slot="am"`
              + ` title="この札を別の職員のマスへドラッグすると、午前訪問だけを移せます"`
              + ` style="background:#dbeafe;color:#1d4ed8;cursor:grab">訪問（午前）</span>`
            : '');
        return `${badge}${bathDisplay}${phoneBadge}${parkingBadge(dateStr, s.id)}${visitNote}${deskLabel}`;
    }

    // 職員名セル（資格併記＋シフト固定トグル）
    function nameCellHtml(s, showQual) {
        const quals = (s.qualifications || []).join('/');
        const qualLabel = (showQual && quals)
            ? `<br><span style="font-weight:normal;font-size:9px;color:#9ca3af">${escapeHtml(quals)}</span>` : '';
        // 依頼文28: この月のシフトを固定（再生成の対象外）にするトグル
        const isFixed = fixedSet.has(s.id);
        const fixToggle =
            `<br><label class="fix-toggle" style="font-weight:normal;font-size:10px;cursor:pointer;white-space:nowrap;color:${isFixed ? '#dc2626' : '#9ca3af'}"`
            + ` title="ONにすると ${year}年${month}月 のこの職員のシフトを固定し、再生成しても変更しません">`
            + `<input type="checkbox" ${isFixed ? 'checked' : ''} onchange="toggleShiftFix(${s.id}, this.checked)" style="vertical-align:middle"> ${isFixed ? '固定中' : '固定'}</label>`;
        return `<td class="staff-name-cell" style="text-align:left;font-weight:600;white-space:nowrap;background:#f9fafb;position:sticky;left:0;z-index:5">${escapeHtml(s.name)}${qualLabel}${fixToggle}</td>`;
    }

    let html = '<thead>' + sectionTitleRow('介護スタッフ') + headerRowHtml() + '</thead><tbody>';

    // --- 介護スタッフ: 職員行 ---
    careStaff.forEach(s => {
        let work = 0;
        let r = '<tr>' + nameCellHtml(s, true);
        dayMeta.forEach(m => {
            const assignment = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
            if (assignment && assignment !== 'off') work++;
            const _a = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
            const _drag = (_a && _a !== 'off') ? ' draggable="true"' : '';
            r += `<td class="staff-cell shift-cell ${m.colClass}" data-date="${m.dateStr}" data-staff="${s.id}"`
               + ` data-group="care" data-assignment="${_a || ''}"${_drag}>${careCellHtml(m.dateStr, s)}</td>`;
        });
        r += `<td class="date-cell" style="font-weight:bold">${work}</td></tr>`;
        html += r;
    });

    // --- 介護スタッフ: サマリー行（日付ごとの配置数）---
    const careSummaryRows = [
        ['デイ午前', DAY_AM_SET, 'understaffed_day_am', true],
        ['デイ午後', DAY_PM_SET, 'understaffed_day_pm', true],
        // 訪問営業日の早番(7:30-16:30)は午前訪問＋午後デイ。自動作成側と同じく訪問午前に数える
        ['訪問午前', VISIT_AM_SET, 'understaffed_visit_am', false, true],
        ['訪問午後', VISIT_PM_SET, 'understaffed_visit_pm', false],
        ['兼務者数', DUAL_SET, 'dual_shortage', false],
    ];
    careSummaryRows.forEach(([label, set, warnType, excludeNursePt, earlyCountsOnVisitDay]) => {
        let r = `<tr><td class="staff-name-cell" style="text-align:left;font-weight:700;background:#eef2ff;position:sticky;left:0;z-index:5">${label}</td>`;
        dayMeta.forEach(m => {
            let cnt = 0;
            careStaff.forEach(s => {
                const a = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
                const slotV = visitMap[m.dateStr] && visitMap[m.dateStr][s.id];
                if (label === '訪問午前' && slotV === 'am') cnt++;
                else if (label === '訪問午後' && slotV === 'pm') cnt++;
                else if (a && set.has(a) && !(excludeNursePt && nursePtIds.has(s.id))) cnt++;
                else if (a === 'early' && earlyCountsOnVisitDay && m.isVisitDay
                         && !hasExplicitAmVisit(m.dateStr)) cnt++;
            });
            const warn = (data.warnings || []).some(w => w.date === m.dateStr && w.warning_type === warnType);
            r += `<td class="staff-cell ${warn ? 'summary-warning' : m.colClass}" style="font-weight:600">${cnt}</td>`;
        });
        r += '<td class="date-cell"></td></tr>';
        html += r;
    });
    // オンコール行
    {
        let r = `<tr><td class="staff-name-cell" style="text-align:left;font-weight:700;background:#eef2ff;position:sticky;left:0;z-index:5">オンコール</td>`;
        const oncallCandidates = (data.staff_list || []).filter(s => s.department !== 'cooking');
        dayMeta.forEach(m => {
            const n = oncallMap[m.dateStr] || '';
            const opts = ['<option value="">-</option>'].concat(
                oncallCandidates.map(s =>
                    `<option value="${s.id}" ${s.name === n ? 'selected' : ''}>${escapeHtml(s.name)}</option>`)
            ).join('');
            r += `<td class="staff-cell ${m.colClass}" style="font-size:11px">`
               + `<select class="oncall-select" data-date="${m.dateStr}"`
               + ` style="max-width:92px;font-size:10px;border:1px solid #e5e7eb;border-radius:4px;padding:1px 2px">${opts}</select></td>`;
        });
        r += '<td class="date-cell"></td></tr>';
        html += r;
    }

    // --- 調理スタッフセクション ---
    if (hasCooking) {
        html += `<tr><td colspan="${totalCols}" style="height:16px;border:none;background:#fff"></td></tr>`;
        html += sectionTitleRow('調理スタッフ');
        html += headerRowHtml();

        cookingStaff.forEach(s => {
            let work = 0;
            let r = '<tr>' + nameCellHtml(s, false);
            dayMeta.forEach(m => {
                const a = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
                let cell;
                if (a && a !== 'cook_off') {
                    work++;
                    const info = ASSIGNMENT_MAP[a];
                    const baseBadge = info
                        ? `<span class="badge ${info.badgeClass}">${info.label}</span>`
                        : `<span class="badge badge-off">${escapeHtml(a)}</span>`;
                    cell = `${baseBadge}${parkingBadge(m.dateStr, s.id)}`;
                } else {
                    cell = offCellHtml(m.dateStr, s);
                }
                const _drag = (a && a !== 'cook_off') ? ' draggable="true"' : '';
                r += `<td class="staff-cell shift-cell ${m.colClass}" data-date="${m.dateStr}" data-staff="${s.id}"`
                   + ` data-group="cooking" data-assignment="${a || ''}"${_drag}>${cell}</td>`;
            });
            r += `<td class="date-cell" style="font-weight:bold">${work}</td></tr>`;
            html += r;
        });

        // 調理配置数 行
        let r = `<tr><td class="staff-name-cell" style="text-align:left;font-weight:700;background:#fff7ed;position:sticky;left:0;z-index:5">調理配置数</td>`;
        dayMeta.forEach(m => {
            let cnt = 0;
            cookingStaff.forEach(s => {
                const a = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
                if (a && a !== 'cook_off') cnt++;
            });
            const warn = (data.warnings || []).some(w => w.date === m.dateStr && (w.warning_type || '').startsWith('understaffed_cook'));
            r += `<td class="staff-cell ${warn ? 'summary-warning' : m.colClass}" style="font-weight:600">${cnt}</td>`;
        });
        r += '<td class="date-cell"></td></tr>';
        html += r;
    }

    html += '</tbody>';

    table.innerHTML = html;
    initShiftDragAndDrop();
    table.className = 'calendar-table';
}

/* ============================================
   警告バナー描画
   ============================================ */
function renderWarnings(warnings) {
    const container = document.getElementById('warnings-container');
    if (!container) return;

    if (!warnings || warnings.length === 0) {
        hideElement('warnings-container');
        return;
    }

    let html = '<div class="bg-red-50 border-l-4 border-red-500 rounded-lg p-4">';
    html += '<div class="flex items-start">';
    html += '<div class="flex-shrink-0">';
    html += '<svg class="w-6 h-6 text-red-500 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">';
    html += '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>';
    html += '</svg>';
    html += '</div>';
    html += '<div class="ml-3">';
    html += '<h3 class="text-lg font-bold text-red-800 mb-2">条件未達の警告</h3>';
    html += '<ul class="space-y-1">';

    warnings.forEach(w => {
        const dateParts = w.date.split('-');
        const displayDate = `${parseInt(dateParts[1])}月${parseInt(dateParts[2])}日`;
        html += `<li class="text-red-700 text-base">&#9888; ${displayDate}: ${escapeHtml(w.message)}</li>`;
    });

    html += '</ul>';
    html += '</div>';
    html += '</div>';
    html += '</div>';

    container.innerHTML = html;
    showElement('warnings-container');
}

/* ============================================
   エクスポート
   ============================================ */
function exportShift(format) {
    if (!currentGenerationId) {
        alert('エクスポートするシフトデータがありません。先にシフトを生成してください。');
        return;
    }
    window.location.href = `/api/export/${currentGenerationId}/${format}`;
}

// PDF出力（4種: group=care|cooking, half=first|second）
function exportPdf(group, half) {
    if (!currentGenerationId) {
        alert('エクスポートするシフトデータがありません。先にシフトを生成してください。');
        return;
    }
    window.location.href = `/api/export/${currentGenerationId}/pdf?group=${group}&half=${half}`;
}

// 個別PDF: 職員選択プルダウンを在籍職員で埋める
function populateIndividualStaffSelect() {
    const sel = document.getElementById('individual-staff-select');
    if (!sel) return;
    sel.innerHTML = '';
    if (!currentStaffList.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '職員なし';
        sel.appendChild(opt);
        return;
    }
    currentStaffList.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.name;
        sel.appendChild(opt);
    });
}

// 個別PDF: 在籍職員全員を「1人1ファイルのPDF」にしてZIPで出力
function exportPdfIndividualAll() {
    if (!currentGenerationId) {
        alert('エクスポートするシフトデータがありません。先にシフトを生成してください。');
        return;
    }
    window.location.href = `/api/export/${currentGenerationId}/pdf-individual`;
}

// 個別PDF: 選択した職員1名だけを出力
function exportPdfIndividualOne() {
    if (!currentGenerationId) {
        alert('エクスポートするシフトデータがありません。先にシフトを生成してください。');
        return;
    }
    const sel = document.getElementById('individual-staff-select');
    const sid = sel ? sel.value : '';
    if (!sid) {
        alert('職員を選択してください。');
        return;
    }
    window.location.href = `/api/export/${currentGenerationId}/pdf-individual?staff_id=${sid}`;
}

// Excel出力（4分割: group=care|cooking, half=first|second）
function exportExcelSplit(group, half) {
    if (!currentGenerationId) {
        alert('エクスポートするシフトデータがありません。先にシフトを生成してください。');
        return;
    }
    window.location.href = `/api/export/${currentGenerationId}/excel?group=${group}&half=${half}`;
}

// 依頼文23-B: 手直しExcelをアップロードしてPDF化（保存データは変更しない）
function excelToPdf() {
    const fileInput = document.getElementById('excel-to-pdf-file');
    const group = document.getElementById('excel-to-pdf-group').value;
    const half = document.getElementById('excel-to-pdf-half').value;
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert('Excelファイルを選択してください。');
        return;
    }
    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('group', group);
    formData.append('half', half);

    // FormData送信ではContent-Typeをブラウザに任せる（境界付与のため）。CSRFのみ手動付与。
    fetch('/api/export/excel-to-pdf', {
        method: 'POST',
        headers: { 'X-CSRFToken': getCsrfToken() },
        body: formData,
    })
        .then(async (response) => {
            if (!response.ok) {
                let msg = 'PDFの作成に失敗しました。';
                try {
                    const err = await response.json();
                    if (err && err.error) msg = err.error;
                } catch (e) { /* ignore */ }
                throw new Error(msg);
            }
            const disposition = response.headers.get('Content-Disposition') || '';
            let filename = 'シフト表.pdf';
            // filename*=UTF-8''<percent-encoded> を優先（日本語名）、無ければ filename= にフォールバック
            const mStar = disposition.match(/filename\*=UTF-8''([^;]+)/i);
            const mPlain = disposition.match(/filename="?([^";]+)"?/i);
            if (mStar && mStar[1]) {
                filename = decodeURIComponent(mStar[1]);
            } else if (mPlain && mPlain[1]) {
                filename = mPlain[1];
            }
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.URL.revokeObjectURL(url);
        })
        .catch((error) => {
            console.error('Error creating PDF from Excel:', error);
            alert(error.message || 'PDFの作成に失敗しました。');
        });
}

/* ============================================
   依頼文41: 手修正Excel → アプリに反映（確認→範囲限定上書き）
   ============================================ */
let _shiftReflectToken = null;

function shiftReflectPreview() {
    const fileInput = document.getElementById('shift-reflect-file');
    const group = document.getElementById('shift-reflect-group').value;
    const half = document.getElementById('shift-reflect-half').value;
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert('Excelファイルを選択してください。');
        return;
    }
    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('group', group);
    formData.append('half', half);
    fetch('/api/shift/upload-preview', {
        method: 'POST',
        headers: { 'X-CSRFToken': getCsrfToken() },
        body: formData,
    })
        .then(async (response) => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                let msg = data.error || '読み取りに失敗しました。';
                if (data.errors && data.errors.length) msg += '\n\n' + data.errors.join('\n');
                throw new Error(msg);
            }
            _shiftReflectToken = data.token;
            renderShiftReflectModal(data);
        })
        .catch((error) => {
            console.error('upload-preview error:', error);
            alert(error.message || '読み取りに失敗しました。');
        });
}

function renderShiftReflectModal(data) {
    const summary = document.getElementById('shift-reflect-summary');
    summary.textContent = `${data.year}年${data.month}月 ${data.group_label} ${data.range_label}：`
        + `変更されるセル ${data.diff_count}件` + (data.unparseable.length ? `／解釈できないセル ${data.unparseable.length}件` : '');

    const unp = document.getElementById('shift-reflect-unparseable');
    if (data.unparseable && data.unparseable.length) {
        let h = '<div class="font-bold mb-1">⚠ 解釈できないセル（反映時はこのセルだけ上書きせず元のまま保持します）:</div><ul class="list-disc ml-5">';
        data.unparseable.forEach(u => {
            h += `<li>${escapeHtml(u.name)} ${escapeHtml(u.date)}: 「${escapeHtml(u.text)}」</li>`;
        });
        h += '</ul>';
        unp.innerHTML = h;
        showElement('shift-reflect-unparseable');
    } else {
        hideElement('shift-reflect-unparseable');
    }

    const box = document.getElementById('shift-reflect-diffs');
    if (!data.diffs || data.diffs.length === 0) {
        box.innerHTML = '<p class="text-gray-500">変更されるセルはありません（現在の保存内容と同じです）。</p>';
    } else {
        let h = '<table class="w-full text-sm border-collapse"><thead><tr class="bg-gray-100">'
            + '<th class="border border-gray-300 px-2 py-1 text-left">職員</th>'
            + '<th class="border border-gray-300 px-2 py-1 text-left">日付</th>'
            + '<th class="border border-gray-300 px-2 py-1 text-left">現在</th>'
            + '<th class="border border-gray-300 px-2 py-1 text-left">変更後</th></tr></thead><tbody>';
        data.diffs.forEach(d => {
            h += '<tr>'
                + `<td class="border border-gray-300 px-2 py-1">${escapeHtml(d.name)}</td>`
                + `<td class="border border-gray-300 px-2 py-1">${escapeHtml(d.date)}</td>`
                + `<td class="border border-gray-300 px-2 py-1 text-gray-500">${escapeHtml(d.from)}</td>`
                + `<td class="border border-gray-300 px-2 py-1 font-semibold text-amber-700">${escapeHtml(d.to)}</td>`
                + '</tr>';
        });
        h += '</tbody></table>';
        box.innerHTML = h;
    }
    const applyBtn = document.getElementById('shift-reflect-apply-btn');
    applyBtn.disabled = (data.diff_count === 0);
    applyBtn.classList.toggle('opacity-50', data.diff_count === 0);
    showElement('shift-reflect-modal');
    document.getElementById('shift-reflect-modal').classList.add('flex');
}

function shiftReflectCancel() {
    _shiftReflectToken = null;
    hideElement('shift-reflect-modal');
}

function shiftReflectApply() {
    if (!_shiftReflectToken) { shiftReflectCancel(); return; }
    const btn = document.getElementById('shift-reflect-apply-btn');
    btn.disabled = true; btn.textContent = '反映中...';
    fetch('/api/shift/upload-apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify({ token: _shiftReflectToken }),
    })
        .then(async (response) => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                let msg = data.error || '反映に失敗しました。';
                if (data.errors && data.errors.length) msg += '\n\n' + data.errors.join('\n');
                throw new Error(msg);
            }
            shiftReflectCancel();
            alert(`${data.message}\n\n条件未達の警告: ${data.warning_count}件`);
            // カレンダーを再読込して反映内容・再計算警告を表示
            if (typeof loadShifts === 'function') {
                loadShifts();
            } else {
                window.location.reload();
            }
        })
        .catch((error) => {
            console.error('upload-apply error:', error);
            alert(error.message || '反映に失敗しました。');
        })
        .finally(() => {
            btn.disabled = false; btn.textContent = '反映する';
        });
}

/* ============================================
   シフト確定
   ============================================ */
function renderConfirmation(conf) {
    const el = document.getElementById('confirmation-status');
    const confirmBtn = document.getElementById('confirm-btn');
    const unconfirmBtn = document.getElementById('unconfirm-btn');
    const confirmed = !!(conf && conf.confirmed_at);
    if (el) {
        if (confirmed) {
            const who = conf.confirmed_role || conf.confirmed_by || '';
            el.textContent = `確定済み｜確定者：${who}／確定日時：${conf.confirmed_at}（再生成するには確定解除が必要です）`;
            el.classList.remove('hidden');
        } else {
            el.textContent = '';
            el.classList.add('hidden');
        }
    }
    // 確定中は「確定」ボタンを隠し「確定解除」を表示
    if (confirmBtn) confirmBtn.classList.toggle('hidden', confirmed);
    if (unconfirmBtn) unconfirmBtn.classList.toggle('hidden', !confirmed);
}

function confirmShift() {
    if (!currentYear || !currentMonth) {
        alert('確定するシフトがありません。先にシフトを表示・生成してください。');
        return;
    }
    if (!confirm(`${currentYear}年${currentMonth}月のシフトを確定しますか？（同じ月で再度確定すると最新の確定者・日時に上書きされます）`)) {
        return;
    }
    fetchWithCsrf(`/api/shifts/${currentYear}/${currentMonth}/confirm`, { method: 'POST' })
        .then(response => {
            if (!response.ok) {
                return response.json().then(err => {
                    throw new Error(err.error || err.message || '確定に失敗しました');
                });
            }
            return response.json();
        })
        .then(conf => {
            renderConfirmation(conf);
            alert('シフトを確定しました。');
        })
        .catch(error => {
            alert('確定に失敗しました: ' + error.message);
            console.error('Error confirming shift:', error);
        });
}

function unconfirmShift() {
    if (!currentYear || !currentMonth) return;
    if (!confirm(`${currentYear}年${currentMonth}月の確定を解除しますか？（解除すると再生成できるようになります）`)) {
        return;
    }
    fetchWithCsrf(`/api/shifts/${currentYear}/${currentMonth}/confirm`, { method: 'DELETE' })
        .then(response => {
            if (!response.ok) throw new Error('確定解除に失敗しました');
            return response.json();
        })
        .then(() => {
            renderConfirmation(null);
            alert('確定を解除しました。');
        })
        .catch(error => {
            alert('確定解除に失敗しました: ' + error.message);
            console.error('Error unconfirming shift:', error);
        });
}

/* ============================================
   休み希望管理（職員フォーム用）
   ============================================ */

function loadDayoffs(staffId) {
    const container = document.getElementById('dayoff-list');
    if (!container) return;

    fetch(`/api/staff/${staffId}/dayoffs`)
        .then(response => {
            if (!response.ok) throw new Error('取得に失敗しました');
            return response.json();
        })
        .then(data => {
            const dayoffs = data.dayoffs || data || [];
            if (dayoffs.length === 0) {
                container.innerHTML = '<p class="text-gray-400 text-sm">休み希望はまだ登録されていません。</p>';
                return;
            }

            let html = '';
            dayoffs.sort((a, b) => a.date.localeCompare(b.date));

            dayoffs.forEach(d => {
                const dateParts = d.date.split('-');
                const displayDate = `${dateParts[0]}年${parseInt(dateParts[1])}月${parseInt(dateParts[2])}日`;
                const dateObj = new Date(parseInt(dateParts[0]), parseInt(dateParts[1]) - 1, parseInt(dateParts[2]));
                const dow = DAY_NAMES[dateObj.getDay()];

                html += `<div class="flex items-center justify-between bg-gray-50 rounded-lg px-4 py-3 border border-gray-200">`;
                html += `<span class="text-base font-medium text-gray-700">${displayDate}(${dow})</span>`;
                html += `<button onclick="deleteDayoff(${staffId}, ${d.id})" `;
                html += `class="bg-red-100 hover:bg-red-200 text-red-700 font-medium py-1 px-4 rounded-lg transition-colors text-sm">`;
                html += '削除</button>';
                html += '</div>';
            });

            container.innerHTML = html;
        })
        .catch(error => {
            container.innerHTML = '<p class="text-red-500 text-sm">休み希望の読み込みに失敗しました。</p>';
            console.error('Error loading dayoffs:', error);
        });
}

function addDayoff(staffId) {
    const dateInput = document.getElementById('dayoff-date');
    if (!dateInput || !dateInput.value) {
        alert('日付を選択してください。');
        return;
    }

    const date = dateInput.value;

    fetchWithCsrf(`/api/staff/${staffId}/dayoff`, {
        method: 'POST',
        body: JSON.stringify({ date: date }),
    })
        .then(response => {
            if (!response.ok) {
                return response.json().then(err => {
                    throw new Error(err.error || err.message || '追加に失敗しました');
                });
            }
            return response.json();
        })
        .then(() => {
            dateInput.value = '';
            loadDayoffs(staffId);
        })
        .catch(error => {
            alert('休み希望の追加に失敗しました: ' + error.message);
            console.error('Error adding dayoff:', error);
        });
}

function deleteDayoff(staffId, dayoffId) {
    if (!confirm('この休み希望を削除してもよろしいですか？')) {
        return;
    }

    fetchWithCsrf(`/api/staff/${staffId}/dayoff/${dayoffId}`, {
        method: 'DELETE',
    })
        .then(response => {
            if (!response.ok) throw new Error('削除に失敗しました');
            loadDayoffs(staffId);
        })
        .catch(error => {
            alert('休み希望の削除に失敗しました: ' + error.message);
            console.error('Error deleting dayoff:', error);
        });
}

/* ============================================
   出勤可能日管理（職員フォーム用）
   ============================================ */

function loadWorkableDates(staffId) {
    const container = document.getElementById('workable-date-list');
    if (!container) return;

    fetch(`/api/staff/${staffId}/workable-dates`)
        .then(response => {
            if (!response.ok) throw new Error('取得に失敗しました');
            return response.json();
        })
        .then(data => {
            const dates = data || [];
            if (dates.length === 0) {
                container.innerHTML = '<p class="text-gray-400 text-sm">出勤可能日は登録されていません（制約なし＝全日出勤可）。</p>';
                return;
            }

            let html = '';
            dates.sort((a, b) => a.date.localeCompare(b.date));

            dates.forEach(d => {
                const dateParts = d.date.split('-');
                const displayDate = `${dateParts[0]}年${parseInt(dateParts[1])}月${parseInt(dateParts[2])}日`;
                const dateObj = new Date(parseInt(dateParts[0]), parseInt(dateParts[1]) - 1, parseInt(dateParts[2]));
                const dow = DAY_NAMES[dateObj.getDay()];

                html += `<div class="flex items-center justify-between bg-gray-50 rounded-lg px-4 py-3 border border-gray-200">`;
                html += `<span class="text-base font-medium text-gray-700">${displayDate}(${dow})</span>`;
                html += `<button onclick="deleteWorkableDate(${staffId}, ${d.id})" `;
                html += `class="bg-red-100 hover:bg-red-200 text-red-700 font-medium py-1 px-4 rounded-lg transition-colors text-sm">`;
                html += '削除</button>';
                html += '</div>';
            });

            container.innerHTML = html;
        })
        .catch(error => {
            container.innerHTML = '<p class="text-red-500 text-sm">出勤可能日の読み込みに失敗しました。</p>';
            console.error('Error loading workable dates:', error);
        });
}

function setWorkableMode(staffId, mode) {
    fetchWithCsrf(`/api/staff/${staffId}/workable-mode`, {
        method: 'POST',
        body: JSON.stringify({ mode: mode }),
    })
        .then(response => {
            if (!response.ok) throw new Error('保存に失敗しました');
            return response.json();
        })
        .catch(error => {
            alert('出勤可能日の扱いの保存に失敗しました: ' + error.message);
            console.error('Error saving workable mode:', error);
        });
}

function addWorkableDate(staffId) {
    const dateInput = document.getElementById('workable-date');
    if (!dateInput || !dateInput.value) {
        alert('日付を選択してください。');
        return;
    }

    const date = dateInput.value;

    fetchWithCsrf(`/api/staff/${staffId}/workable-date`, {
        method: 'POST',
        body: JSON.stringify({ date: date }),
    })
        .then(response => {
            if (!response.ok) {
                return response.json().then(err => {
                    throw new Error(err.error || err.message || '追加に失敗しました');
                });
            }
            return response.json();
        })
        .then(() => {
            dateInput.value = '';
            loadWorkableDates(staffId);
        })
        .catch(error => {
            alert('出勤可能日の追加に失敗しました: ' + error.message);
            console.error('Error adding workable date:', error);
        });
}

function deleteWorkableDate(staffId, wdId) {
    if (!confirm('この出勤可能日を削除してもよろしいですか？')) {
        return;
    }

    fetchWithCsrf(`/api/staff/${staffId}/workable-date/${wdId}`, {
        method: 'DELETE',
    })
        .then(response => {
            if (!response.ok) throw new Error('削除に失敗しました');
            loadWorkableDates(staffId);
        })
        .catch(error => {
            alert('出勤可能日の削除に失敗しました: ' + error.message);
            console.error('Error deleting workable date:', error);
        });
}

/* ============================================
   配置ルール管理（設定ページ用）
   ============================================ */

function togglePlacementRuleActive(ruleId, isActive) {
    fetchWithCsrf(`/api/placement_rules/${ruleId}`, {
        method: 'PUT',
        body: JSON.stringify({ is_active: isActive }),
    })
        .then(response => {
            if (!response.ok) throw new Error('更新に失敗しました');
            return response.json();
        })
        .catch(error => {
            alert('配置ルールの更新に失敗しました: ' + error.message);
            console.error('Error updating placement rule:', error);
        });
}

function deletePlacementRule(ruleId) {
    if (!confirm('この配置ルールを削除してもよろしいですか？')) return;

    fetchWithCsrf(`/api/placement_rules/${ruleId}`, {
        method: 'DELETE',
    })
        .then(response => {
            if (!response.ok) throw new Error('削除に失敗しました');
            location.reload();
        })
        .catch(error => {
            alert('配置ルールの削除に失敗しました: ' + error.message);
        });
}

function addPlacementRule() {
    const name = document.getElementById('new-rule-name').value.trim();
    const ruleType = document.getElementById('new-rule-type').value;
    const timeStart = document.getElementById('new-rule-time-start').value;
    const timeEnd = document.getElementById('new-rule-time-end').value;
    const minCount = parseInt(document.getElementById('new-rule-min-count').value) || 1;
    const isHard = document.getElementById('new-rule-is-hard').checked;

    if (!name) {
        alert('ルール名を入力してください。');
        return;
    }

    const data = {
        name: name,
        rule_type: ruleType,
        time_start: timeStart,
        time_end: timeEnd,
        min_count: minCount,
        is_hard: isHard,
        target_qualification_ids: [],
        target_gender: '',
    };

    // 資格選択
    if (ruleType === 'qualification_min') {
        const qualSelect = document.querySelectorAll('#new-rule-quals input:checked');
        data.target_qualification_ids = Array.from(qualSelect).map(el => parseInt(el.value));
    }
    // 性別選択
    if (ruleType === 'gender_min') {
        data.target_gender = document.getElementById('new-rule-gender').value || 'male';
    }

    fetchWithCsrf('/api/placement_rules', {
        method: 'POST',
        body: JSON.stringify(data),
    })
        .then(response => {
            if (!response.ok) throw new Error('追加に失敗しました');
            location.reload();
        })
        .catch(error => {
            alert('配置ルールの追加に失敗しました: ' + error.message);
        });
}

// 配置ルールの適用時間帯（開始/終了）を変更して保存
function updatePlacementRuleTime(ruleId, field, value) {
    const body = {};
    body[field] = value;
    fetchWithCsrf(`/api/placement_rules/${ruleId}`, {
        method: 'PUT',
        body: JSON.stringify(body),
    })
        .then(response => {
            if (!response.ok) throw new Error('更新に失敗しました');
        })
        .catch(error => {
            alert('適用時間帯の更新に失敗しました: ' + error.message);
        });
}

function toggleCookingComboActive(ruleId, isActive) {
    fetchWithCsrf(`/api/cooking_combo_rules/${ruleId}`, {
        method: 'PUT',
        body: JSON.stringify({ is_active: isActive }),
    })
        .then(response => {
            if (!response.ok) throw new Error('更新に失敗しました');
        })
        .catch(error => {
            alert('更新に失敗しました: ' + error.message);
        });
}

/* ---- 調理シフト種類マスタ（依頼文21）---- */
function addCookingType() {
    const label = document.getElementById('new-cooking-type-label').value.trim();
    const start = document.getElementById('new-cooking-type-start').value;
    const end = document.getElementById('new-cooking-type-end').value;
    if (!label) { alert('種類の名前を入力してください。'); return; }
    const countsEl = document.getElementById('new-cooking-type-counts');
    const counts = countsEl ? countsEl.checked : true;
    fetchWithCsrf('/api/cooking_types', {
        method: 'POST',
        body: JSON.stringify({
            label: label, start_time: start, end_time: end, counts_as_cooking: counts,
        }),
    })
        .then(r => { if (!r.ok) return r.json().then(j => { throw new Error(j.error || '追加失敗'); }); location.reload(); })
        .catch(e => alert('調理種類の追加に失敗しました: ' + e.message));
}

function updateCookingType(typeId, field, value) {
    const body = {}; body[field] = value;
    fetchWithCsrf(`/api/cooking_types/${typeId}`, { method: 'PUT', body: JSON.stringify(body) })
        .then(r => ensureUpdateOk(r))
        .catch(e => alert('調理種類の更新に失敗しました: ' + e.message));
}

function deleteCookingType(typeId) {
    if (!confirm('この調理シフト種類を削除しますか？')) return;
    fetchWithCsrf(`/api/cooking_types/${typeId}`, { method: 'DELETE' })
        .then(r => { if (!r.ok) return r.json().then(j => { throw new Error(j.error || '削除失敗'); }); location.reload(); })
        .catch(e => alert('削除に失敗しました: ' + e.message));
}

/* ---- 調理組み合わせマスタ（依頼文21）---- */
function updateCookingComboName(ruleId, name) {
    fetchWithCsrf(`/api/cooking_combo_rules/${ruleId}`, { method: 'PUT', body: JSON.stringify({ name: name }) })
        .then(r => ensureUpdateOk(r))
        .catch(e => alert('名前の更新に失敗しました: ' + e.message));
}

function updateCookingComboPatterns(ruleId) {
    const row = document.querySelector(`[data-combo-row="${ruleId}"]`);
    const patterns = Array.from(row.querySelectorAll('input[type="checkbox"]:checked')).map(el => el.value);
    fetchWithCsrf(`/api/cooking_combo_rules/${ruleId}`, { method: 'PUT', body: JSON.stringify({ allowed_patterns: patterns }) })
        .then(r => ensureUpdateOk(r))
        .catch(e => alert('組み合わせの更新に失敗しました: ' + e.message));
}

function addCookingCombo() {
    const name = document.getElementById('new-combo-name').value.trim();
    const patterns = Array.from(document.querySelectorAll('#new-combo-patterns input[type="checkbox"]:checked')).map(el => el.value);
    if (patterns.length === 0) { alert('含める種類を1つ以上選択してください。'); return; }
    fetchWithCsrf('/api/cooking_combo_rules', {
        method: 'POST',
        body: JSON.stringify({ name: name, allowed_patterns: patterns }),
    })
        .then(r => { if (!r.ok) return r.json().then(j => { throw new Error(j.error || '追加失敗'); }); location.reload(); })
        .catch(e => alert('組み合わせの追加に失敗しました: ' + e.message));
}

function deleteCookingCombo(ruleId) {
    if (!confirm('この組み合わせを削除しますか？')) return;
    fetchWithCsrf(`/api/cooking_combo_rules/${ruleId}`, { method: 'DELETE' })
        .then(r => { if (!r.ok) throw new Error('削除失敗'); location.reload(); })
        .catch(e => alert('削除に失敗しました: ' + e.message));
}

/* ============================================
   ユーティリティ関数
   ============================================ */

function showLoading(text) {
    const el = document.getElementById('loading');
    const textEl = document.getElementById('loading-text');
    if (el) {
        el.classList.remove('hidden');
        if (textEl && text) textEl.textContent = text;
    }
}

function hideLoading() {
    const el = document.getElementById('loading');
    if (el) el.classList.add('hidden');
}

function showElement(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('hidden');
}

function hideElement(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
}

function isNurseOrPtStaff(staff) {
    const qualificationCodes = new Set(staff.qualification_codes || []);
    if (qualificationCodes.has('nurse') || qualificationCodes.has('pt')) {
        return true;
    }

    const qualificationNames = new Set(staff.qualifications || []);
    return qualificationNames.has('看護師')
        || qualificationNames.has('PT')
        || qualificationNames.has('理学療法士');
}
