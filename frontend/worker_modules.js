// --- Worker Module Implementation ---

//: Mirrors ``DEFAULT_SHIFT_RULES`` in backend/main.py. Only a fallback: the real
//: threshold arrives with the worker's own stats, so an admin retuning
//: ``overtime_notify_hours`` does not leave the panel warning about 8.1 forever.
const DEFAULT_OVERTIME_NOTIFY_HOURS = 8.1;

//: The same for the break policy: a full day is this many *paid* hours plus an unpaid
//: break, so the shift itself runs longer than the paid day. Sent with the worker's own
//: stats for the same reason - the panel must not keep saying 8/30 after an operator has
//: changed it.
const DEFAULT_BREAK_MINUTES = 30;
const DEFAULT_BREAK_AFTER_HOURS = 4;

const WORKER_MODULES = {
    /** Live-timer handle; one at a time, torn down on every re-render. */
    _elapsedTimer: null,

    /**
     * Current shift + month total for the logged-in worker.
     *
     * This has to survive having no signal: the clock button is the one thing a
     * worker on a remote site must still be able to reach, so a failed request
     * degrades to the last state the server confirmed plus whatever this phone has
     * queued. The queued punches are folded on top of that state, otherwise the
     * panel would offer "Clock In" again straight after an offline clock-in.
     */
    async fetchStatus() {
        const workerId = State.user.id;
        const offline = typeof OFFLINE !== 'undefined' && OFFLINE.available();
        let active = null;
        let monthHours = 0;
        let stale = false;
        let overtimeNotifyHours = DEFAULT_OVERTIME_NOTIFY_HOURS;
        let breakMinutes = DEFAULT_BREAK_MINUTES;
        let breakAfterHours = DEFAULT_BREAK_AFTER_HOURS;
        let paidDayHours = null;
        let autoCloses = false;
        let flagged = false;

        try {
            // One self-scoped call. The two earlier ones were admin-only routes, so a
            // worker always got a 403, saw an empty list, and was offered "Clock In"
            // while already on shift - every tap then answered "Already clocked in!".
            const stats = await API.request('/worker/me/stats');
            active = stats.active_session || null;
            monthHours = Number(stats.total_hours) || 0;
            // Clock-out is refused while a shift is awaiting review, so say so on the
            // panel instead of letting the worker find out by selfie.
            flagged = !!stats.flagged_for_review;
            // An older server may not send this yet; keep the documented default.
            if (Number(stats.overtime_notify_hours) > 0) {
                overtimeNotifyHours = Number(stats.overtime_notify_hours);
            }
            if (Number(stats.break_minutes) >= 0) breakMinutes = Number(stats.break_minutes);
            if (Number(stats.break_after_hours) >= 0) breakAfterHours = Number(stats.break_after_hours);
            if (Number(stats.paid_day_hours) > 0) paidDayHours = Number(stats.paid_day_hours);
            autoCloses = String(stats.auto_close_at_regular || '0') === '1';
            if (offline) {
                await OFFLINE.cacheShiftState(workerId, {
                    active: !!active,
                    site_name: active ? active.site_name : null,
                    clock_in_time: active ? active.clock_in_time : null
                }).catch(() => {});
            }
        } catch (err) {
            stale = true;
            const cached = offline ? await OFFLINE.cachedShiftState(workerId).catch(() => null) : null;
            active = cached && cached.active ? cached : null;
        }

        const local = offline
            ? await OFFLINE.localState(workerId, !!active).catch(() => null)
            : null;
        const openShift = local ? local.active : !!active;
        return {
            active: openShift ? (active || { clock_in_time: null, site_name: null }) : null,
            monthHours,
            stale,
            queued: local ? local.queued : 0,
            nextAction: local ? local.nextAction : (openShift ? 'Clock Out' : 'Clock In'),
            deviceState: typeof OFFLINE !== 'undefined' ? OFFLINE.deviceState : 'unsupported',
            overtimeNotifyHours,
            breakMinutes,
            breakAfterHours,
            paidDayHours,
            autoCloses,
            flagged
        };
    },

    /** Seconds on shift for a server timestamp, or null if it is unreadable. */
    elapsedSeconds(clockInTime) {
        const start = new Date(String(clockInTime).replace(' ', 'T'));
        if (isNaN(start.getTime())) return null;
        return Math.max(0, (Date.now() - start.getTime()) / 1000);
    },

    /** "8:05:07" - hours are not zero-padded so a long shift never wraps the line. */
    formatElapsed(totalSeconds) {
        const seconds = Math.max(0, Math.floor(totalSeconds));
        const pad = (value) => String(value).padStart(2, '0');
        return `${Math.floor(seconds / 3600)}:${pad(Math.floor((seconds % 3600) / 60))}:${pad(seconds % 60)}`;
    },

    elapsedLabel(clockInTime) {
        const seconds = this.elapsedSeconds(clockInTime);
        if (seconds === null) return null;
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        return hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
    },

    /** "8.1" for 8.1, "8" for 8.0 -- mirrors Python's ``:g`` in the server's flags. */
    hoursLabel(value) {
        const number = Number(value);
        return Number.isFinite(number) ? String(Number(number.toFixed(2))) : String(value);
    },

    /** "30m" / "1h 15m" - a break is minutes, not a decimal of an hour, on a phone. */
    minutesLabel(value) {
        const minutes = Math.max(0, Math.round(Number(value) || 0));
        if (minutes < 60) return `${minutes}m`;
        return minutes % 60 === 0 ? `${minutes / 60}h` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
    },

    /**
     * Live elapsed timer for the open shift.
     *
     * The card used to print one frozen "Elapsed: 3h 12m" string at render time, so
     * a worker had no way to watch their own shift run long: the first they heard of
     * overtime was an admin-side approval queue after clock-out. This ticks once a
     * second and raises an inline "past 8.1h - needs overtime approval" note at the
     * same threshold the server uses, so the worker can decide to stop on their own
     * terms instead of discovering it on their payslip.
     *
     * The device clock drives the display only; the hours that count still come from the
     * server's clock-in record. The timer tears itself down as soon as the card is
     * replaced (tab switch, re-render, logout), so no interval outlives the panel.
     *
     * The timer counts time *on site*. The thresholds are about *paid* hours, so the
     * unpaid break is taken out of the count before either is compared - a worker on a
     * normal day sees the timer pass 8:00:00 without being told they are on overtime,
     * because at that point they have worked 7.5 h and been on site for 8.
     *
     * The sub-line no longer promises a close the server will not make. A
     * ``hard_cutoff_hours`` read here used to fall back to a hardcoded 11 and tell the
     * worker "The shift closes automatically at 11h" - a number the server never sent and
     * a close that never happened, on the one screen a worker trusts to know whether they
     * are still on the clock. What it says now is the policy as it actually is: a paid day
     * of N hours plus an unpaid break, and whether that is the end of the shift.
     */
    startElapsedTimer(clockInTime, settings) {
        this.stopElapsedTimer();
        if (!clockInTime) return;

        const options = settings || {};
        const threshold = Number(options.overtimeHours) > 0
            ? Number(options.overtimeHours) : DEFAULT_OVERTIME_NOTIFY_HOURS;
        const breakMinutes = Number(options.breakMinutes) >= 0
            ? Number(options.breakMinutes) : DEFAULT_BREAK_MINUTES;
        const breakAfter = Number(options.breakAfterHours) >= 0
            ? Number(options.breakAfterHours) : DEFAULT_BREAK_AFTER_HOURS;
        const paidDay = Number(options.paidDayHours) > 0 ? Number(options.paidDayHours) : null;
        const autoCloses = !!options.autoCloses;

        const note = I18n.__('overtimeNeedsApproval').replace('{hours}', this.hoursLabel(threshold));
        const openShiftHint = I18n.__('overtimeOpenShiftHint');
        const dayDone = paidDay === null ? '' : I18n.__('shiftDayComplete')
            .replace('{paid}', this.hoursLabel(paidDay))
            .replace('{onsite}', this.hoursLabel(paidDay + breakMinutes / 60));
        let raised = false;
        let announcedDay = false;

        const tick = () => {
            const label = document.getElementById('shiftElapsed');
            if (!label) { this.stopElapsedTimer(); return; }  // card is gone
            const seconds = this.elapsedSeconds(clockInTime);
            if (seconds === null) return;
            label.textContent = this.formatElapsed(seconds);

            // The break is only counted once the shift is long enough to have contained
            // one - the same rule the server deducts by, so the two agree on the number.
            const taken = seconds >= breakAfter * 3600 ? breakMinutes * 60 : 0;
            const paid = Math.max(0, seconds - taken);
            // ``>=``, not ``>``: ``overtime.scan_overtime`` alerts on ``paid >= threshold``,
            // so the worker's own card and the administrator's notification light up at the
            // same moment instead of a second apart on the same shift.
            const past = paid >= threshold * 3600;
            const dayReached = paidDay !== null && paid >= paidDay * 3600;
            label.classList.toggle('text-red-600', past);
            label.classList.toggle('dark:text-red-400', past);
            label.classList.toggle('text-blue-600', !past);
            label.classList.toggle('dark:text-blue-400', !past);

            const noteEl = document.getElementById('shiftOvertimeNote');
            if (!noteEl) return;
            if (dayReached && autoCloses && !announcedDay) {
                // The shift is at its paid limit: the server closes it at 8.5 h on site.
                noteEl.innerHTML = `
                    <p class="font-bold">${I18n.__('shiftEndsNow')}</p>
                    <p class="mt-1 text-xs font-normal">${dayDone}</p>`;
                announcedDay = true;
                Toast.info(I18n.__('shiftEndsNow'));
            } else if (dayReached && autoCloses) {
                noteEl.innerHTML = `
                    <p class="font-bold">${I18n.__('shiftEndsNow')}</p>
                    <p class="mt-1 text-xs font-normal">${dayDone}</p>`;
            } else if (past && !raised) {
                noteEl.innerHTML = `
                    <p class="font-bold">${note}</p>
                    <p class="mt-1 text-xs font-normal">${openShiftHint}</p>`;
                raised = true;
                // The moment it tips over, while the worker is looking at the card.
                Toast.info(note);
            }
            noteEl.classList.toggle('hidden', !(past || (dayReached && autoCloses)));
        };

        tick();
        this._elapsedTimer = setInterval(tick, 1000);
    },

    stopElapsedTimer() {
        if (this._elapsedTimer === null) return;
        clearInterval(this._elapsedTimer);
        this._elapsedTimer = null;
    },

    /**
     * The check-in card. One primary action is derived from the shift status so a
     * worker can never tap the wrong button by accident.
     */
    async renderClockPanel(container) {
        if (!container) return;
        // Whatever the previous render started (tab switch now, stale fetch later)
        // must not keep ticking against a card that no longer exists.
        this.stopElapsedTimer();
        container.innerHTML = `<div class="flex items-center gap-3 justify-center py-6 text-gray-500 dark:text-gray-400">
            <span class="spinner"></span><span>${I18n.__('loading')}</span></div>`;

        let status;
        try {
            status = await this.fetchStatus();
        } catch (err) {
            container.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${err.message}</p>`;
            return;
        }

        const compact = !Device.isMobile;
        const active = status.active;
        const action = status.nextAction || (active ? 'Clock Out' : 'Clock In');
        const actionKey = action === 'Clock Out' ? 'clockOut' : 'clockIn';
        // Only a known clock-in time can be counted from; an offline cache without
        // one still shows the shift, just without a timer and without a false alarm.
        const clockInTime = active && active.clock_in_time ? active.clock_in_time : null;

        container.innerHTML = `
            <div class="text-center ${compact ? 'mb-5' : 'mb-6'}">
                <p class="text-sm text-gray-500 dark:text-gray-400">${I18n.__('shiftStatus')}</p>
                <p class="text-2xl font-extrabold mt-1 ${active ? 'text-green-600 dark:text-green-400' : 'text-gray-400'}">
                    ${active ? I18n.__('onShift') : I18n.__('currentlyClockedOut')}
                </p>
                ${active ? `
                    ${active.clock_in_time ? `<p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
                        ${I18n.__('clockedInAt')} <strong>${active.clock_in_time}</strong>
                    </p>` : ''}
                    ${active.site_name ? `<p class="text-sm text-gray-600 dark:text-gray-300">${I18n.__('site')}: <strong>${active.site_name}</strong></p>` : ''}
                    ${clockInTime ? `<p class="mt-1 text-sm font-semibold text-blue-600 dark:text-blue-400">
                        ${I18n.__('elapsed')}: <span id="shiftElapsed" class="tabular-nums">0:00:00</span>
                    </p>` : ''}
                    ${/* The two numbers a worker is paid by, said out loud: the paid day, the
                         unpaid break, and the shift length they add up to. Without them the
                         timer above reads as the thing that gets paid, and 8:30:00 on a
                         30-minute break day looks like an hour of missing money. */ ''}
                    ${clockInTime ? `<p class="mt-1 text-xs text-gray-500 dark:text-gray-400" data-day-policy="${status.paidDayHours || 'default'}">
                        ${I18n.__('shiftPaidDay')}: <b>${status.paidDayHours ? this.hoursLabel(status.paidDayHours) : '8'} h</b>
                        · ${I18n.__('shiftUnpaidBreak')}: <b>${this.minutesLabel(status.breakMinutes)}</b>
                    </p>` : ''}
                    ${clockInTime ? `<div id="shiftOvertimeNote" class="hidden mt-3 p-3 rounded-xl bg-amber-50 dark:bg-amber-900/25 border border-amber-300 dark:border-amber-700 text-sm text-left text-amber-800 dark:text-amber-200"></div>` : ''}
                ` : ''}
                ${status.stale ? `<p class="mt-2 text-xs text-amber-600 dark:text-amber-400">${I18n.__('lastKnownStatus')}</p>` : ''}
            </div>

            ${status.flagged ? `
                <p class="mb-4 p-3 rounded-xl bg-amber-50 dark:bg-amber-900/25 border border-amber-300 dark:border-amber-700 text-sm text-amber-800 dark:text-amber-200" data-flagged-for-review>
                    ${I18n.__('flaggedForReview')}
                </p>` : ''}

            <button class="clock-button ${active ? 'out' : 'in'} ${compact ? 'compact' : ''}"
                    onclick="WORKER_MODULES.handleClock('${action}')">
                ${I18n.__(actionKey)}
            </button>

            ${this.offlinePanelHtml(status)}

            <div class="grid grid-cols-2 gap-3 mt-4">
                <div class="p-3 rounded-xl bg-gray-50 dark:bg-gray-700 text-center">
                    <p class="text-xs text-gray-500 dark:text-gray-400">${I18n.__('hoursThisMonth')}</p>
                    <p class="font-bold text-lg">${status.monthHours} h</p>
                </div>
                <div class="p-3 rounded-xl bg-gray-50 dark:bg-gray-700 text-center">
                    <p class="text-xs text-gray-500 dark:text-gray-400">${I18n.__('location')}</p>
                    <p class="font-bold text-sm ${Location.isSecure ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}">
                        ${Location.isSecure ? I18n.__('gpsReady') : I18n.__('gpsInsecure')}
                    </p>
                </div>
            </div>
        `;

        // Last: the card has to be in the document before the first tick looks for it.
        this.startElapsedTimer(clockInTime, {
            overtimeHours: status.overtimeNotifyHours,
            breakMinutes: status.breakMinutes,
            breakAfterHours: status.breakAfterHours,
            paidDayHours: status.paidDayHours,
            autoCloses: status.autoCloses
        });
    },

    /** Kept for backwards compatibility with older callers. */
    async renderWorkerDashboard(container) {
        return this.renderClockPanel(container);
    },

    /** Card list on mobile, table on desktop. */
    async renderHistory(container) {
        if (!container) return;
        container.innerHTML = `<div class="flex items-center gap-3 justify-center py-6 text-gray-500 dark:text-gray-400">
            <span class="spinner"></span><span>${I18n.__('loading')}</span></div>`;

        let workerLogs;
        try {
            // Own rows only, and scoped by the token on the server rather than by a
            // filter here: /admin/logs answers 403 for a worker reading their own.
            workerLogs = (await API.request('/worker/me/logs?limit=20')).slice(0, 20);
        } catch (err) {
            container.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${err.message}</p>`;
            return;
        }

        if (workerLogs.length === 0) {
            container.innerHTML = `<p class="text-center py-8 text-gray-500 dark:text-gray-400">${I18n.__('noHistory')}</p>`;
            return;
        }

        const actionLabel = (log) => log.action === 'Clock In' ? I18n.__('clockIn') : I18n.__('clockOut');

        if (Device.isMobile) {
            container.innerHTML = `
                <div class="space-y-2">
                    ${workerLogs.map(log => `
                        <div class="p-3 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex justify-between gap-3">
                            <div class="min-w-0">
                                <p class="font-semibold ${log.action === 'Clock In' ? 'text-green-600 dark:text-green-400' : 'text-red-500'}">${actionLabel(log)}</p>
                                <p class="text-xs text-gray-500 dark:text-gray-400">${log.timestamp}</p>
                                <p class="text-xs text-gray-500 dark:text-gray-400">${log.site}</p>
                            </div>
                            <div class="text-right flex-none">
                                <p class="font-bold">${log.hours ? Number(log.hours).toFixed(2) + ' h' : '-'}</p>
                                <p class="text-xs text-gray-500 dark:text-gray-400">${log.status || ''}</p>
                            </div>
                        </div>`).join('')}
                </div>`;
            return;
        }

        container.innerHTML = `
            <div class="overflow-x-auto">
                <table class="w-full text-sm">
                    <thead class="bg-gray-100 dark:bg-gray-700">
                        <tr>
                            <th class="p-3 text-left">${I18n.__('action')}</th>
                            <th class="p-3 text-left">${I18n.__('time')}</th>
                            <th class="p-3 text-left">${I18n.__('site')}</th>
                            <th class="p-3 text-right">${I18n.__('hours')}</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y dark:divide-gray-600">
                        ${workerLogs.map(log => `<tr>
                            <td class="p-3 ${log.action === 'Clock In' ? 'text-green-600 dark:text-green-400' : 'text-red-500'} font-semibold">${actionLabel(log)}</td>
                            <td class="p-3">${log.timestamp}</td>
                            <td class="p-3">${log.site}</td>
                            <td class="p-3 text-right">${log.hours ? Number(log.hours).toFixed(2) : '-'}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    },

    /**
     * What this phone is holding for the server - queued punches, a device that
     * needs re-registering, or simply "no connection". Rendered inside the clock
     * panel because that is where an offline punch becomes a visible obligation
     * rather than a silently missing record.
     */
    offlinePanelHtml(status) {
        if (typeof OFFLINE === 'undefined' || !OFFLINE.available()) return '';
        const blocks = [];
        if (status.queued > 0) {
            blocks.push(`
                <div class="mt-4 p-3 rounded-xl bg-amber-50 dark:bg-amber-900/25 text-sm text-amber-800 dark:text-amber-200">
                    <p class="font-semibold">${I18n.__('queuedPunches')}: ${status.queued}</p>
                    <p class="mt-1">${I18n.__('queuedPunchesHint')}</p>
                    <button onclick="WORKER_MODULES.syncNow(this)" class="mt-2 px-3 py-2 rounded-lg bg-amber-500 text-white font-semibold">${I18n.__('syncNow')}</button>
                </div>`);
        }
        if (!OFFLINE.online()) {
            blocks.push(`<p class="mt-3 text-xs text-gray-500 dark:text-gray-400">${I18n.__('offlineModeHint')}</p>`);
        }
        if (status.deviceState === 'key_lost' || status.deviceState === 'revoked') {
            blocks.push(`
                <div class="mt-4 p-3 rounded-xl bg-red-50 dark:bg-red-900/25 text-sm text-red-700 dark:text-red-300">
                    <p>${status.deviceState === 'revoked' ? I18n.__('deviceRevoked') : I18n.__('deviceKeyLost')}</p>
                    ${status.deviceState === 'key_lost' ? `<button onclick="WORKER_MODULES.reRegisterDevice(this)" class="mt-2 px-3 py-2 rounded-lg bg-red-500 text-white font-semibold">${I18n.__('reRegisterDevice')}</button>` : ''}
                </div>`);
        }
        return blocks.join('');
    },

    /** Manual replay of everything queued on this phone. */
    async syncNow(button) {
        if (typeof OFFLINE === 'undefined' || !OFFLINE.available()) return;
        if (button) { button.disabled = true; button.textContent = I18n.__('loading'); }
        const summary = await OFFLINE.syncNow();
        if (summary.offline) Toast.info(I18n.__('offlineNoConnection'));
        else if (summary.error === 'unauthorized') Toast.error(I18n.__('sessionExpired'));
        else if (summary.error) Toast.error(summary.message || I18n.__('syncFailed'));
        else if (summary.sent) Toast.success(`${I18n.__('syncDone')} (${summary.sent})`);
        else Toast.info(I18n.__('nothingToSync'));
        await this.renderClockPanel(document.getElementById('workerDashboard'));
    },

    /** Issue a fresh signing key for this phone (only safe with an empty queue). */
    async reRegisterDevice(button) {
        if (typeof OFFLINE === 'undefined') return;
        if (button) button.disabled = true;
        try {
            await OFFLINE.rotateDevice();
            Toast.success(I18n.__('deviceRegistered'));
        } catch (err) {
            Toast.error(err.message || I18n.__('syncFailed'));
        }
        await this.renderClockPanel(document.getElementById('workerDashboard'));
    },

    async handleClock(action) {
        UI.doAttendance(action);
    },

    // -----------------------------------------------------------------
    //  Notes - asking the administrator for something, in writing
    //
    //  A worker who needs a password change, is missing material, or finds a
    //  Wednesday gone from their timesheet previously had one route: catch somebody
    //  on the phone. This is the other one, and unlike a phone call it leaves a
    //  record - which is the whole point of the tab. The administrator answers in
    //  the same thread, and the worker can tell them the answer was wrong.
    // -----------------------------------------------------------------

    /** The element the notes view was last painted into; a re-render needs a target. */
    _notesHost: null,

    /** The last list the server sent, so the tab can be repainted without a request. */
    _notes: null,

    /** True while the new-note form is on screen. */
    _composingNote: false,

    /** The thread currently open, or null for the list. */
    _noteThread: null,

    /**
     * The categories the worker can pick, in the order a site actually needs them.
     *
     * Mirrors ``notes.CATEGORIES`` on the server, which validates the value: a client
     * list is a convenience, never the check.
     */
    NOTE_CATEGORIES: ['password_reset', 'missing_item', 'shift_hours', 'enrollment', 'working_conditions', 'other'],

    escapeHtml(value) {
        const escapes = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
        return String(value === null || value === undefined ? '' : value)
            .replace(/[&<>"']/g, (char) => escapes[char]);
    },

    noteCategoryLabel(category) { return I18n.__(`noteCat_${category}`); },
    noteStatusLabel(status) { return I18n.__(`noteStatus_${status}`); },

    noteStatusClass(status) {
        if (status === 'resolved') return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
        if (status === 'in_progress') return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
        if (status === 'closed') return 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300';
        return 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200';
    },

    noteStatusChip(status) {
        return `<span class="px-2 py-0.5 rounded-full text-xs font-semibold ${this.noteStatusClass(status)}">${this.noteStatusLabel(status)}</span>`;
    },

    async renderNotes(container) {
        if (!container) return;
        this._notesHost = container;
        this._noteThread = null;
        container.innerHTML = UI.loadingHtml();
        let data;
        try {
            data = await API.request('/worker/notes');
        } catch (err) {
            container.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        this._notes = data;
        container.innerHTML = this.notesListHtml(data);
    },

    notesListHtml(data) {
        const notes = (data && data.notes) || [];
        const open = Number(data && data.open) || 0;
        const button = 'px-4 py-2 rounded-xl font-semibold';
        return `
            <div class="flex items-start justify-between gap-3 mb-3">
                <div class="min-w-0">
                    <h3 class="text-xl font-bold">${I18n.__('notesMine')}</h3>
                    <p class="text-xs text-gray-500 dark:text-gray-400">${I18n.__('notesIntro')}</p>
                </div>
                <button type="button" data-new-note onclick="WORKER_MODULES.startNote()"
                        class="flex-none ${button} bg-blue-600 text-white">${I18n.__('notesNew')}</button>
            </div>
            ${this._composingNote ? this.noteComposerHtml() : ''}
            ${open > 0 && !this._composingNote
                ? `<p class="mb-3 text-xs text-amber-700 dark:text-amber-300">${I18n.__('notesOpenHint')}</p>`
                : ''}
            ${notes.length === 0
                ? `<p class="py-6 text-center text-gray-500 dark:text-gray-400">
                       <span class="block font-semibold">${I18n.__('notesEmpty')}</span>
                       <span class="text-xs">${I18n.__('notesEmptyHint')}</span>
                   </p>`
                : `<div class="space-y-2">${notes.map((note) => this.noteCardHtml(note)).join('')}</div>`}`;
    },

    noteCardHtml(note) {
        const unread = Number(note.worker_unread) || 0;
        const preview = note.last_message ? note.last_message.body : '';
        return `
            <button type="button" data-note="${note.id}" onclick="WORKER_MODULES.openNote(${note.id})"
                    class="w-full text-left p-3 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
                <div class="flex items-start justify-between gap-2">
                    <p class="font-semibold min-w-0 truncate">${this.escapeHtml(note.subject)}</p>
                    ${unread > 0
                        ? `<span data-unread="${unread}" class="flex-none px-2 py-0.5 rounded-full text-xs font-bold bg-blue-600 text-white">${I18n.__('noteWaitingReply')}</span>`
                        : ''}
                </div>
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    ${this.noteCategoryLabel(note.category)} · ${this.noteStatusChip(note.status)}
                    ${note.priority === 'high' ? ` · <span data-urgent="1" class="font-semibold text-red-600 dark:text-red-400">${I18n.__('notePriorityHigh')}</span>` : ''}
                </p>
                ${preview ? `<p class="mt-2 text-sm text-gray-600 dark:text-gray-300 truncate">${this.escapeHtml(preview)}</p>` : ''}
                <p class="mt-1 text-xs text-gray-400">${I18n.__('noteLastActivity')}: ${this.escapeHtml(note.last_reply_at || note.created_at || '')}</p>
            </button>`;
    },

    /** The new-note form. Category first, because it decides who has to read it. */
    noteComposerHtml() {
        const field = 'w-full mt-1 p-3 rounded-xl border border-gray-200 dark:border-gray-600 dark:bg-gray-700 dark:text-white';
        const label = 'text-xs font-semibold text-gray-500 dark:text-gray-400';
        return `
            <div class="mb-4 p-4 rounded-xl border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20" data-note-composer>
                <p class="font-semibold mb-3">${I18n.__('notesNew')}</p>
                <label class="block mb-2">
                    <span class="${label}">${I18n.__('noteCategory')}</span>
                    <select id="noteCategory" class="${field}">
                        ${this.NOTE_CATEGORIES.map((value) =>
                            `<option value="${value}">${this.noteCategoryLabel(value)}</option>`).join('')}
                    </select>
                </label>
                <label class="block mb-2">
                    <span class="${label}">${I18n.__('noteSubject')}</span>
                    <input type="text" id="noteSubject" maxlength="120"
                           placeholder="${I18n.__('noteSubjectPlaceholder')}" class="${field}">
                </label>
                <label class="block mb-3">
                    <span class="${label}">${I18n.__('noteMessage')}</span>
                    <textarea id="noteBody" rows="4" maxlength="2000"
                              placeholder="${I18n.__('noteMessagePlaceholder')}" class="${field}"></textarea>
                </label>
                <label class="flex items-center gap-2 mb-3 text-sm">
                    <input type="checkbox" id="noteUrgent">
                    <span>${I18n.__('noteUrgent')}</span>
                </label>
                <div class="flex flex-wrap gap-2">
                    <button type="button" data-send-note onclick="WORKER_MODULES.submitNote(this)"
                            class="px-5 py-2 rounded-xl bg-blue-600 text-white font-semibold">${I18n.__('noteSend')}</button>
                    <button type="button" onclick="WORKER_MODULES.cancelNote()"
                            class="px-4 py-2 rounded-xl font-semibold border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200">${I18n.__('noteCancel')}</button>
                </div>
            </div>`;
    },

    /** Repaints the list from the last response - no request, nothing can be lost. */
    repaintNotes() {
        if (!this._notesHost || !this._notes) return this.renderNotes(this._notesHost);
        this._notesHost.innerHTML = this.notesListHtml(this._notes);
        return Promise.resolve();
    },

    startNote() {
        this._composingNote = true;
        return this.repaintNotes();
    },

    cancelNote() {
        this._composingNote = false;
        return this.repaintNotes();
    },

    async submitNote(button) {
        const subject = (document.getElementById('noteSubject') || {}).value || '';
        const body = (document.getElementById('noteBody') || {}).value || '';
        const categoryField = document.getElementById('noteCategory');
        const urgentField = document.getElementById('noteUrgent');
        if (!subject.trim()) {
            Toast.error(I18n.__('noteSubjectRequired'));
            return;
        }
        if (!body.trim()) {
            Toast.error(I18n.__('noteBodyRequired'));
            return;
        }
        if (button) button.disabled = true;
        try {
            await API.request('/worker/notes', {
                method: 'POST',
                body: {
                    category: categoryField ? categoryField.value : 'other',
                    subject: subject.trim(),
                    body: body.trim(),
                    priority: urgentField && urgentField.checked ? 'high' : 'normal'
                }
            });
        } catch (err) {
            // The reason matters here more than usual: the server refuses a new note
            // once the worker has too many open, and "429" explains nothing.
            if (button) button.disabled = false;
            Toast.error(err.message);
            return;
        }
        this._composingNote = false;
        Toast.success(I18n.__('noteSent'));
        await this.renderNotes(this._notesHost);
    },

    /** Opens one note and its thread. Reading it is what clears "new reply". */
    async openNote(noteId) {
        if (!this._notesHost) return;
        this._notesHost.innerHTML = UI.loadingHtml();
        let note;
        try {
            note = await API.request(`/worker/notes/${noteId}`);
        } catch (err) {
            this._notesHost.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        this._noteThread = note;
        this._composingNote = false;
        this._notesHost.innerHTML = this.noteThreadHtml(note);
    },

    noteThreadHtml(note) {
        // The server already filters internal messages out of every worker-facing query,
        // so this drops nothing today. It is here because it costs one line and the
        // failure it prevents is an administrator's private working note appearing on a
        // worker's phone - which must not depend on the renderer trusting the payload.
        const messages = (note.messages || []).filter((message) => !message.internal);
        const hint = note.status === 'resolved' ? I18n.__('noteResolvedHint')
            : (note.status === 'closed' ? I18n.__('noteClosedHint') : '');
        const field = 'w-full p-3 rounded-xl border border-gray-200 dark:border-gray-600 dark:bg-gray-700 dark:text-white';
        return `
            <button type="button" onclick="WORKER_MODULES.backToNotes()"
                    class="mb-3 text-sm font-semibold text-blue-600 dark:text-blue-400" data-notes-back>
                ← ${I18n.__('noteBack')}
            </button>
            <div class="mb-3">
                <p class="font-bold text-lg">${this.escapeHtml(note.subject)}</p>
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    ${this.noteCategoryLabel(note.category)} · ${this.noteStatusChip(note.status)}
                    · ${I18n.__('noteOpened')}: ${this.escapeHtml(note.created_at || '')}
                </p>
            </div>
            ${hint ? `<p class="mb-3 p-3 rounded-xl text-xs bg-blue-50 dark:bg-blue-900/20 text-blue-800 dark:text-blue-200" data-thread-hint>${hint}</p>` : ''}
            <div class="space-y-2 mb-4">
                ${messages.map((message) => this.noteMessageHtml(message)).join('')}
            </div>
            <div class="p-3 rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800">
                <textarea id="noteReplyBody" rows="3" maxlength="2000"
                          placeholder="${I18n.__('noteReplyPlaceholder')}" class="${field}"></textarea>
                <div class="flex flex-wrap gap-2 mt-2">
                    <button type="button" data-send-reply onclick="WORKER_MODULES.sendNoteReply(${note.id}, this)"
                            class="px-5 py-2 rounded-xl bg-blue-600 text-white font-semibold">${I18n.__('noteReply')}</button>
                    ${note.status === 'closed' ? '' : `<button type="button" data-close-note onclick="WORKER_MODULES.closeNote(${note.id})"
                            class="px-4 py-2 rounded-xl font-semibold border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200">${I18n.__('noteClose')}</button>`}
                </div>
            </div>`;
    },

    /**
     * One message. Your own words sit on the right, the administrator's on the left -
     * so "who said what" is legible without reading the label above each bubble.
     */
    noteMessageHtml(message) {
        const mine = !message.from_admin;
        const who = mine ? I18n.__('noteFromYou') : I18n.__('noteFromAdmin');
        const bubble = mine
            ? 'bg-blue-600 text-white'
            : 'bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600';
        return `
            <div class="flex ${mine ? 'justify-end' : 'justify-start'}" data-message="${message.id}">
                <div class="max-w-[85%] p-3 rounded-xl ${bubble}">
                    <p class="text-[11px] opacity-70 mb-1">${who} · ${this.escapeHtml(message.created_at || '')}</p>
                    <p class="text-sm whitespace-pre-wrap break-words">${this.escapeHtml(message.body)}</p>
                </div>
            </div>`;
    },

    async sendNoteReply(noteId, button) {
        const field = document.getElementById('noteReplyBody');
        const body = field ? String(field.value || '').trim() : '';
        if (!body) {
            Toast.error(I18n.__('noteBodyRequired'));
            return;
        }
        if (button) button.disabled = true;
        let response;
        try {
            response = await API.request(`/worker/notes/${noteId}/replies`, {
                method: 'POST',
                body: { body: body }
            });
        } catch (err) {
            if (button) button.disabled = false;
            Toast.error(err.message);
            return;
        }
        // A reply on a finished note puts it back in the administrator's queue. Say so:
        // the worker just reopened something, and that is not a silent act.
        Toast.success(response && response.reopened ? I18n.__('noteReopened') : I18n.__('noteReplySent'));
        await this.openNote(noteId);
    },

    async closeNote(noteId) {
        if (!confirm(I18n.__('noteClose') + '?')) return;
        try {
            await API.request(`/worker/notes/${noteId}/close`, { method: 'POST' });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('noteClosed'));
        await this.renderNotes(this._notesHost);
    },

    backToNotes() {
        this._noteThread = null;
        return this.renderNotes(this._notesHost);
    }
};
