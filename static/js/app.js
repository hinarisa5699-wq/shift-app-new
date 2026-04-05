/**
 * シフト自動作成くん - メインJavaScript
 * カレンダー描画・シフト生成・休み希望管理
 */

/* ============================================
   グローバル変数
   ============================================ */
let currentGenerationId = null;
let isGenerating = false;

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
    day_pattern1:    { label: 'デイ8:30-17:30',  badgeClass: 'badge-day-full'  },
    day_pattern2:    { label: 'デイ9:00-16:00',  badgeClass: 'badge-day-p2'   },
    day_pattern3:    { label: 'デイ午前のみ',     badgeClass: 'badge-day-am'   },
    day_pattern4:    { label: 'デイ午後のみ',     badgeClass: 'badge-day-pm'   },
    visit_am:        { label: '訪問午前のみ',     badgeClass: 'badge-visit-am'  },
    visit_pm:        { label: '訪問午後のみ',     badgeClass: 'badge-visit-pm'  },
    day_p3_visit_pm: { label: '兼務(③→訪問)',    badgeClass: 'badge-dual-a'    },
    visit_am_day_p4: { label: '兼務(訪問→④)',    badgeClass: 'badge-dual-b'    },
    cook_early:      { label: '早番',             badgeClass: 'badge-cook-1'    },
    cook_morning:    { label: '日勤',             badgeClass: 'badge-cook-2'    },
    cook_late:       { label: '遅番',             badgeClass: 'badge-cook-3'    },
    cook_long:       { label: '通し',             badgeClass: 'badge-cook-4'    },
    // 旧名の後方互換
    day_am:          { label: 'デイ午前のみ',     badgeClass: 'badge-day-am'   },
    day_pm:          { label: 'デイ午後のみ',     badgeClass: 'badge-day-pm'   },
    day_am_visit_pm: { label: '兼務(③→訪問)',    badgeClass: 'badge-dual-a'   },
    visit_am_day_pm: { label: '兼務(訪問→④)',    badgeClass: 'badge-dual-b'   },
};

// デイ午前に寄与するアサインメント
const DAY_AM_SET = new Set([
    'day_pattern1', 'day_pattern2', 'day_pattern3', 'day_p3_visit_pm',
    'day_am', 'day_am_visit_pm',
]);
// デイ午後に寄与するアサインメント
const DAY_PM_SET = new Set([
    'day_pattern1', 'day_pattern2', 'day_pattern4', 'visit_am_day_p4',
    'day_pm', 'visit_am_day_pm',
]);
// 訪問午前
const VISIT_AM_SET = new Set(['visit_am', 'visit_am_day_p4', 'visit_am_day_pm']);
// 訪問午後
const VISIT_PM_SET = new Set(['visit_pm', 'day_p3_visit_pm', 'day_am_visit_pm']);
// 兼務
const DUAL_SET = new Set(['day_p3_visit_pm', 'visit_am_day_p4', 'day_am_visit_pm', 'visit_am_day_pm']);

// ③ 相談員事務スロットラベル
const DESK_SLOT_LABELS = ['9-11時', '11-13時', '13-15時', '15-17時'];

// ① 休憩開始時刻 → 表示ラベル（1時間固定）
function formatBreakLabel(breakStart) {
    if (!breakStart) return '';
    // "12:30" → "12:30-13:30", "10:00" → "10:00-11:00"
    const [h, m] = breakStart.split(':').map(Number);
    const endH = h + 1;
    const end = `${String(endH).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
    return `休${breakStart}-${end}`;
}

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

            if (hasShifts) {
                currentGenerationId = data.generation_id;
                renderCalendar(data, year, month);
                renderWarnings(data.warnings || []);
                showElement('calendar-container');
                showElement('export-buttons');
                hideElement('no-data-message');
            } else if (hasWarnings) {
                // W-13: シフト0件でも警告があれば表示
                currentGenerationId = data.generation_id;
                renderWarnings(data.warnings);
                hideElement('calendar-container');
                hideElement('export-buttons');
                hideElement('no-data-message');
            } else {
                currentGenerationId = null;
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

    const shifts = data.shifts || [];
    const staffList = data.staff_list || [];
    const holidays = data.holidays || {};

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
    const breakMap = {};     // ① {date: {staff_id: "12:00"}}
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
        if (s.break_start) {
            if (!breakMap[s.date]) breakMap[s.date] = {};
            breakMap[s.date][s.staff_id] = s.break_start;
        }
    });

    const daysInMonth = new Date(year, month, 0).getDate();
    let html = '';

    // Header row ④ 資格バッジ付き
    html += '<thead><tr>';
    html += '<th class="date-cell">日付</th>';
    careStaff.forEach(s => {
        const quals = (s.qualifications || []).join('/');
        const qualLabel = quals ? `<br><span class="text-xs text-gray-400" style="font-weight:normal;font-size:9px">${escapeHtml(quals)}</span>` : '';
        html += `<th class="staff-cell">${escapeHtml(s.name)}${qualLabel}</th>`;
    });
    html += '<th class="summary-cell">ケアサマリー</th>';
    if (hasCooking) {
        cookingStaff.forEach(s => {
            html += `<th class="staff-cell">${escapeHtml(s.name)}</th>`;
        });
        html += '<th class="summary-cell">調理サマリー</th>';
    }
    html += '</tr></thead>';

    // Body rows
    html += '<tbody>';
    for (let day = 1; day <= daysInMonth; day++) {
        const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const dateObj = new Date(year, month - 1, day);
        const dayOfWeek = dateObj.getDay();

        // ⑧ 祝日色分け
        let rowClass = '';
        if (holidays[dateStr]) {
            rowClass = 'row-holiday';
        } else if (dayOfWeek === 6) {
            rowClass = 'row-saturday';
        } else if (dayOfWeek === 0) {
            rowClass = 'row-sunday';
        }

        const holidayName = holidays[dateStr] ? ` [${escapeHtml(holidays[dateStr])}]` : '';
        const dayLabel = `${month}/${day}(${DAY_NAMES[dayOfWeek]})${holidayName}`;

        let dayAmCount = 0, dayPmCount = 0;
        let visitAmCount = 0, visitPmCount = 0;
        let dualCount = 0;

        html += `<tr class="${rowClass}">`;
        html += `<td class="date-cell">${dayLabel}</td>`;

        // Care staff cells ① 休憩時間 + ③ 事務時間帯
        careStaff.forEach(s => {
            const assignment = shiftMap[dateStr] ? shiftMap[dateStr][s.id] : null;
            if (assignment) {
                const info = ASSIGNMENT_MAP[assignment];
                const isPhone = phoneDutyMap[dateStr] && phoneDutyMap[dateStr][s.id];
                const phoneBadge = isPhone ? ' <span class="badge badge-phone">TEL</span>' : '';
                // ① 個人別休憩時間
                const breakStart = breakMap[dateStr] && breakMap[dateStr][s.id];
                const breakLabel = formatBreakLabel(breakStart);
                const breakDisplay = breakLabel ? `<br><span style="font-size:9px;color:#b45309">${breakLabel}</span>` : '';
                // ③ 相談員事務時間帯
                const deskSlots = deskSlotMap[dateStr] && deskSlotMap[dateStr][s.id];
                let deskLabel = '';
                if (deskSlots && deskSlots.length > 0) {
                    const slotTexts = deskSlots.map(si => DESK_SLOT_LABELS[si] || '').filter(Boolean);
                    if (slotTexts.length > 0) {
                        deskLabel = `<br><span style="font-size:9px;color:#6b7280">相談 ${slotTexts.join(',')}</span>`;
                    }
                }
                if (info) {
                    html += `<td class="staff-cell"><span class="badge ${info.badgeClass}">${info.label}</span>${phoneBadge}${breakDisplay}${deskLabel}</td>`;
                } else {
                    html += `<td class="staff-cell"><span class="badge badge-off">${escapeHtml(assignment)}</span>${phoneBadge}${breakDisplay}${deskLabel}</td>`;
                }
                // ② 看護師/PTはデイ人数カウントから除外
                if (!nursePtIds.has(s.id)) {
                    if (DAY_AM_SET.has(assignment)) dayAmCount++;
                    if (DAY_PM_SET.has(assignment)) dayPmCount++;
                }
                if (VISIT_AM_SET.has(assignment)) visitAmCount++;
                if (VISIT_PM_SET.has(assignment)) visitPmCount++;
                if (DUAL_SET.has(assignment)) dualCount++;
            } else {
                html += '<td class="staff-cell"><span class="badge badge-off">休</span></td>';
            }
        });

        // Care summary cell
        const dayWarnings = (data.warnings || []).filter(w => w.date === dateStr);
        const careSummaryClass = dayWarnings.some(w => w.warning_type && !w.warning_type.startsWith('understaffed_cook'))
            ? 'summary-cell summary-warning' : 'summary-cell';
        html += `<td class="${careSummaryClass}">`;
        html += `デイ午前:<strong>${dayAmCount}</strong> `;
        html += `デイ午後:<strong>${dayPmCount}</strong><br>`;
        html += `訪問午前:<strong>${visitAmCount}</strong> `;
        html += `訪問午後:<strong>${visitPmCount}</strong><br>`;
        html += `兼務:<strong>${dualCount}</strong>`;
        html += '</td>';

        // Cooking staff cells ① 休憩時間表示
        if (hasCooking) {
            let cookCount = 0;
            cookingStaff.forEach(s => {
                const assignment = shiftMap[dateStr] ? shiftMap[dateStr][s.id] : null;
                if (assignment && assignment !== 'cook_off') {
                    const info = ASSIGNMENT_MAP[assignment];
                    const breakStart = breakMap[dateStr] && breakMap[dateStr][s.id];
                    const breakLabel = formatBreakLabel(breakStart);
                    const breakDisplay = breakLabel ? `<br><span style="font-size:9px;color:#b45309">${breakLabel}</span>` : '';
                    if (info) {
                        html += `<td class="staff-cell"><span class="badge ${info.badgeClass}">${info.label}</span>${breakDisplay}</td>`;
                    } else {
                        html += `<td class="staff-cell"><span class="badge badge-off">${escapeHtml(assignment)}</span>${breakDisplay}</td>`;
                    }
                    cookCount++;
                } else {
                    html += '<td class="staff-cell"><span class="badge badge-off">休</span></td>';
                }
            });

            const cookSummaryClass = dayWarnings.some(w => w.warning_type && w.warning_type.startsWith('understaffed_cook'))
                ? 'summary-cell summary-warning' : 'summary-cell';
            html += `<td class="${cookSummaryClass}">`;
            html += `配置:<strong>${cookCount}</strong>人`;
            html += '</td>';
        }

        html += '</tr>';
    }

    // ⑦ フッター: 出勤日数行
    html += '<tr class="font-bold" style="background-color:#f3f4f6">';
    html += '<td class="date-cell" style="font-weight:bold">出勤日数</td>';
    careStaff.forEach(s => {
        let count = 0;
        for (let day = 1; day <= daysInMonth; day++) {
            const ds = `${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
            const asgn = shiftMap[ds] && shiftMap[ds][s.id];
            if (asgn && asgn !== 'off') count++;
        }
        html += `<td class="staff-cell" style="font-weight:bold">${count}</td>`;
    });
    html += '<td class="summary-cell"></td>';
    if (hasCooking) {
        cookingStaff.forEach(s => {
            let count = 0;
            for (let day = 1; day <= daysInMonth; day++) {
                const ds = `${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
                const asgn = shiftMap[ds] && shiftMap[ds][s.id];
                if (asgn && asgn !== 'cook_off') count++;
            }
            html += `<td class="staff-cell" style="font-weight:bold">${count}</td>`;
        });
        html += '<td class="summary-cell"></td>';
    }
    html += '</tr>';

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
    const period = document.getElementById('new-rule-period').value;
    const minCount = parseInt(document.getElementById('new-rule-min-count').value) || 1;
    const isHard = document.getElementById('new-rule-is-hard').checked;

    if (!name) {
        alert('ルール名を入力してください。');
        return;
    }

    const data = {
        name: name,
        rule_type: ruleType,
        period: period,
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
