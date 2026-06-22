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

/* ============================================
   CSRF トークン管理
   ============================================ */
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
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
    day_pattern1:    { label: '訪問8:30-17:30',  badgeClass: 'badge-day-full'  },
    day_pattern2:    { label: '訪問9:00-16:00',  badgeClass: 'badge-day-p2'   },
    day_pattern3:    { label: '訪問午前のみ',     badgeClass: 'badge-day-am'   },
    day_pattern4:    { label: '訪問午後のみ',     badgeClass: 'badge-day-pm'   },
    early:           { label: '早番7:30-16:30',  badgeClass: 'badge-day-full' },
    late:            { label: '遅番9:30-18:30',  badgeClass: 'badge-day-p2'   },
    nurse_short:     { label: '看護9:30-13:30',  badgeClass: 'badge-visit-am' },
    visit_am:        { label: 'デイ午前のみ',     badgeClass: 'badge-visit-am'  },
    visit_pm:        { label: 'デイ午後のみ',     badgeClass: 'badge-visit-pm'  },
    day_p3_visit_pm: { label: '兼務(訪問→デイ)',  badgeClass: 'badge-dual-a'    },
    visit_am_day_p4: { label: '兼務(デイ→訪問)',  badgeClass: 'badge-dual-b'    },
    cooking_1:      { label: '①6-8',            badgeClass: 'badge-cook-1'    },
    cooking_2:    { label: '②8-13',           badgeClass: 'badge-cook-2'    },
    cooking_3:       { label: '③12-19',          badgeClass: 'badge-cook-3'    },
    cooking_4:       { label: '④6-13',           badgeClass: 'badge-cook-4'    },
    cooking_5:        { label: '⑤9-15',           badgeClass: 'badge-cook-2'    },
    // 旧名の後方互換
    day_am:          { label: '訪問午前のみ',     badgeClass: 'badge-day-am'   },
    day_pm:          { label: '訪問午後のみ',     badgeClass: 'badge-day-pm'   },
    day_am_visit_pm: { label: '兼務(訪問→デイ)',  badgeClass: 'badge-dual-a'   },
    visit_am_day_pm: { label: '兼務(デイ→訪問)',  badgeClass: 'badge-dual-b'   },
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
                renderCalendar(data, year, month);
                renderWarnings(data.warnings || []);
                renderConfirmation(data.confirmation);
                showElement('calendar-container');
                showElement('export-buttons');
                showElement('excel-to-pdf-box');
                hideElement('no-data-message');
            } else if (hasWarnings) {
                // W-13: シフト0件でも警告があれば表示
                currentGenerationId = data.generation_id;
                renderWarnings(data.warnings);
                renderConfirmation(null);
                hideElement('calendar-container');
                hideElement('export-buttons');
                hideElement('no-data-message');
            } else {
                currentGenerationId = null;
                renderConfirmation(null);
                hideElement('calendar-container');
                hideElement('export-buttons');
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
function renderCalendar(data, year, month) {
    const table = document.getElementById('calendar-table');
    if (!table) return;

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
    const holidays = data.holidays || {};
    const oncallMap = data.oncall || {};  // {date: 氏名} オンコール担当

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
    shifts.forEach(s => {
        if (!shiftMap[s.date]) shiftMap[s.date] = {};
        shiftMap[s.date][s.staff_id] = s.assignment;
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
            label: `${month}/${day}<br>${DAY_NAMES[dayOfWeek]}${holidayName}`,
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

    // ケアスタッフ 1 セルの中身
    function careCellHtml(dateStr, s) {
        const assignment = shiftMap[dateStr] ? shiftMap[dateStr][s.id] : null;
        if (!assignment || assignment === 'off') {
            return '<span class="badge badge-off">休</span>';
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
        return `${badge}${bathDisplay}${phoneBadge}${deskLabel}`;
    }

    // 職員名セル（資格併記）
    function nameCellHtml(s, showQual) {
        const quals = (s.qualifications || []).join('/');
        const qualLabel = (showQual && quals)
            ? `<br><span style="font-weight:normal;font-size:9px;color:#9ca3af">${escapeHtml(quals)}</span>` : '';
        return `<td class="staff-name-cell" style="text-align:left;font-weight:600;white-space:nowrap;background:#f9fafb;position:sticky;left:0;z-index:5">${escapeHtml(s.name)}${qualLabel}</td>`;
    }

    let html = '<thead>' + sectionTitleRow('介護スタッフ') + headerRowHtml() + '</thead><tbody>';

    // --- 介護スタッフ: 職員行 ---
    careStaff.forEach(s => {
        let work = 0;
        let r = '<tr>' + nameCellHtml(s, true);
        dayMeta.forEach(m => {
            const assignment = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
            if (assignment && assignment !== 'off') work++;
            r += `<td class="staff-cell ${m.colClass}">${careCellHtml(m.dateStr, s)}</td>`;
        });
        r += `<td class="date-cell" style="font-weight:bold">${work}</td></tr>`;
        html += r;
    });

    // --- 介護スタッフ: サマリー行（日付ごとの配置数）---
    const careSummaryRows = [
        ['訪問午前', DAY_AM_SET, 'understaffed_day_am', true],
        ['訪問午後', DAY_PM_SET, 'understaffed_day_pm', true],
        ['デイ午前', VISIT_AM_SET, 'understaffed_visit_am', false],
        ['デイ午後', VISIT_PM_SET, 'understaffed_visit_pm', false],
        ['兼務者数', DUAL_SET, 'dual_shortage', false],
    ];
    careSummaryRows.forEach(([label, set, warnType, excludeNursePt]) => {
        let r = `<tr><td class="staff-name-cell" style="text-align:left;font-weight:700;background:#eef2ff;position:sticky;left:0;z-index:5">${label}</td>`;
        dayMeta.forEach(m => {
            let cnt = 0;
            careStaff.forEach(s => {
                const a = shiftMap[m.dateStr] ? shiftMap[m.dateStr][s.id] : null;
                if (a && set.has(a) && !(excludeNursePt && nursePtIds.has(s.id))) cnt++;
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
        dayMeta.forEach(m => {
            const n = oncallMap[m.dateStr];
            const cell = n ? `<span class="badge badge-phone">${escapeHtml(n)}</span>` : '<span class="text-gray-300">-</span>';
            r += `<td class="staff-cell ${m.colClass}" style="font-size:11px">${cell}</td>`;
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
                    cell = info
                        ? `<span class="badge ${info.badgeClass}">${info.label}</span>`
                        : `<span class="badge badge-off">${escapeHtml(a)}</span>`;
                } else {
                    cell = '<span class="badge badge-off">休</span>';
                }
                r += `<td class="staff-cell ${m.colClass}">${cell}</td>`;
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
    fetchWithCsrf('/api/cooking_types', {
        method: 'POST',
        body: JSON.stringify({ label: label, start_time: start, end_time: end }),
    })
        .then(r => { if (!r.ok) return r.json().then(j => { throw new Error(j.error || '追加失敗'); }); location.reload(); })
        .catch(e => alert('調理種類の追加に失敗しました: ' + e.message));
}

function updateCookingType(typeId, field, value) {
    const body = {}; body[field] = value;
    fetchWithCsrf(`/api/cooking_types/${typeId}`, { method: 'PUT', body: JSON.stringify(body) })
        .then(r => { if (!r.ok) throw new Error('更新失敗'); })
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
        .then(r => { if (!r.ok) throw new Error('更新失敗'); })
        .catch(e => alert('名前の更新に失敗しました: ' + e.message));
}

function updateCookingComboPatterns(ruleId) {
    const row = document.querySelector(`[data-combo-row="${ruleId}"]`);
    const patterns = Array.from(row.querySelectorAll('input[type="checkbox"]:checked')).map(el => el.value);
    fetchWithCsrf(`/api/cooking_combo_rules/${ruleId}`, { method: 'PUT', body: JSON.stringify({ allowed_patterns: patterns }) })
        .then(r => { if (!r.ok) throw new Error('更新失敗'); })
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
