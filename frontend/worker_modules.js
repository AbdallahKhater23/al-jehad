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
            // One class rather than four palette utilities: the figure is the primary
            // blue while the paid day is still running, and switches to the same warning
            // colour as the note beneath it when it passes the line. The colour is never
            // the only signal - the note that appears at the same moment carries the
            // words, so a worker who cannot tell the two hues apart still sees the change.
            label.classList.toggle('is-over', past);

            const noteEl = document.getElementById('shiftOvertimeNote');
            if (!noteEl) return;
            if (dayReached && autoCloses && !announcedDay) {
                // The shift is at its paid limit: the server closes it at 8.5 h on site.
                noteEl.innerHTML = `
                    <p class="hand-note-title">${HAND_ICONS.alert}<span>${this.escapeHtml(I18n.__('shiftEndsNow'))}</span></p>
                    <p class="hand-note-sub">${this.escapeHtml(dayDone)}</p>`;
                announcedDay = true;
                Toast.info(I18n.__('shiftEndsNow'));
            } else if (dayReached && autoCloses) {
                noteEl.innerHTML = `
                    <p class="hand-note-title">${HAND_ICONS.alert}<span>${this.escapeHtml(I18n.__('shiftEndsNow'))}</span></p>
                    <p class="hand-note-sub">${this.escapeHtml(dayDone)}</p>`;
            } else if (past && !raised) {
                noteEl.innerHTML = `
                    <p class="hand-note-title">${HAND_ICONS.alert}<span>${this.escapeHtml(note)}</span></p>
                    <p class="hand-note-sub">${this.escapeHtml(openShiftHint)}</p>`;
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
        container.innerHTML = UI.loadingHtml();

        let status;
        try {
            status = await this.fetchStatus();
        } catch (err) {
            container.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
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
            <div class="hand-hero ${active ? 'is-live' : ''}">
                <div class="hand-hero-top">
                    <p class="hand-hero-status">
                        <span class="hand-dot ${active ? '' : 'is-off'}" aria-hidden="true"></span>${this.escapeHtml(I18n.__('shiftStatus'))}
                    </p>
                    ${/* The site, in the corner: the one fact on this card the worker did not
                         choose and cannot change, so it is a chip rather than a second
                         headline. */ ''}
                    ${active && active.site_name
                        ? `<p class="hand-hero-site">${this.escapeHtml(I18n.__('site'))}<strong>${this.escapeHtml(active.site_name)}</strong></p>`
                        : ''}
                </div>
                <p class="hand-hero-word">${this.escapeHtml(active ? I18n.__('onShift') : I18n.__('currentlyClockedOut'))}</p>
                ${active ? `
                    ${clockInTime ? `
                        <span id="shiftElapsed" class="hand-timer">0:00:00</span>
                        <span class="hand-timer-label">${this.escapeHtml(I18n.__('handTimeOnShift'))}</span>` : ''}
                    ${/* The two numbers a worker is paid by, said out loud: the paid day, the
                         unpaid break, and the shift length they add up to. Without them the
                         timer above reads as the thing that gets paid, and 8:30:00 on a
                         30-minute break day looks like an hour of missing money - and the
                         clock-in the server actually recorded, so the timer can be checked
                         against something a worker can point at. */ ''}
                    ${clockInTime ? `<div class="hand-policy" data-day-policy="${status.paidDayHours || 'default'}">
                        <span class="hand-policy-cell">
                            <span class="hand-policy-label">${this.escapeHtml(I18n.__('clockedInAt'))}</span>
                            <span class="hand-policy-value is-mono">${this.escapeHtml(active.clock_in_time)}</span>
                        </span>
                        <span class="hand-policy-cell">
                            <span class="hand-policy-label">${this.escapeHtml(I18n.__('shiftPaidDay'))}</span>
                            <span class="hand-policy-value"><b>${status.paidDayHours ? this.hoursLabel(status.paidDayHours) : '8'} h</b></span>
                        </span>
                        <span class="hand-policy-cell">
                            <span class="hand-policy-label">${this.escapeHtml(I18n.__('shiftUnpaidBreak'))}</span>
                            <span class="hand-policy-value"><b>${this.minutesLabel(status.breakMinutes)}</b></span>
                        </span>
                    </div>` : ''}
                    ${clockInTime ? `<div id="shiftOvertimeNote" class="hand-note hidden"></div>` : ''}
                ` : ''}
                ${status.stale ? `<p class="hand-hero-meta is-warn">${this.escapeHtml(I18n.__('lastKnownStatus'))}</p>` : ''}
            </div>

            ${status.flagged ? `
                <div class="hand-alert" data-flagged-for-review>
                    ${HAND_ICONS.alert}
                    <p>${this.escapeHtml(I18n.__('flaggedForReview'))}</p>
                </div>` : ''}

            <button type="button" class="clock-button hand-clock ${active ? 'out' : 'in'} ${compact ? 'compact' : ''}"
                    onclick="WORKER_MODULES.handleClock('${action}')">
                ${action === 'Clock Out' ? HAND_ICONS.clockOut : HAND_ICONS.clockIn}
                <span>${this.escapeHtml(I18n.__(actionKey))}</span>
            </button>

            ${this.offlinePanelHtml(status)}

            <div class="hand-facts">
                <div class="hand-fact">
                    <p class="hand-fact-label">${this.escapeHtml(I18n.__('hoursThisMonth'))}</p>
                    <p class="hand-fact-value is-mono">${this.escapeHtml(status.monthHours)} h</p>
                </div>
                <div class="hand-fact">
                    <p class="hand-fact-label">${this.escapeHtml(I18n.__('location'))}</p>
                    <p class="hand-fact-value ${Location.isSecure ? 'is-ok' : 'is-warn'}">
                        ${this.escapeHtml(Location.isSecure ? I18n.__('handReady') : I18n.__('handNotReady'))}
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

    /**
     * The worker's own record: a timeline on a phone, a table on a desk.
     *
     * Two layouts from one data set, and the phone one is not a shrunk table. It is a
     * rail with a stop per punch - a filled dot for a clock-in, a hollow one for a
     * clock-out - because the question this screen answers is "what did my week look
     * like", and a sequence of stops answers that at a glance in a way four aligned
     * columns never did on a 360px screen.
     *
     * The site name is a value the worker never chose, so it is escaped like every other
     * value off the wire, and it appears exactly once per row - a second copy anywhere in
     * this markup would be a second chance to miss the escaping.
     */
    async renderHistory(container) {
        if (!container) return;
        container.innerHTML = UI.loadingHtml();

        let workerLogs;
        try {
            // Own rows only, and scoped by the token on the server rather than by a
            // filter here: /admin/logs answers 403 for a worker reading their own.
            workerLogs = (await API.request('/worker/me/logs?limit=20')).slice(0, 20);
        } catch (err) {
            container.innerHTML = `<p class="ui-error">${this.escapeHtml(I18n.__('error'))}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }

        if (workerLogs.length === 0) {
            container.innerHTML = `
                <div class="ui-empty" role="status">
                    <span class="ui-empty-icon">${HAND_ICONS.history}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('noHistory'))}</p>
                    <p class="ui-empty-body">${this.escapeHtml(I18n.__('handNoHistoryHint'))}</p>
                </div>`;
            return;
        }

        const actionLabel = (log) => log.action === 'Clock In' ? I18n.__('clockIn') : I18n.__('clockOut');

        if (Device.isMobile) {
            container.innerHTML = `
                <ul class="hand-rail">
                    ${workerLogs.map(log => `
                        <li class="hand-stop ${log.action === 'Clock In' ? 'is-in' : 'is-out'}">
                            <div class="hand-stop-what">
                                <span class="hand-stop-action">${this.escapeHtml(actionLabel(log))}</span>
                                <span class="hand-stop-when">${this.escapeHtml(log.timestamp)}</span>
                                <span class="hand-stop-where">${this.escapeHtml(log.site)}</span>
                            </div>
                            <div class="hand-stop-tally">
                                <p class="hand-stop-hours">${log.hours ? Number(log.hours).toFixed(2) + ' h' : '-'}</p>
                                <p class="hand-stop-status">${this.escapeHtml(log.status || '')}</p>
                            </div>
                        </li>`).join('')}
                </ul>`;
            return;
        }

        container.innerHTML = `
            <div class="ui-table-wrap">
                <table class="ui-table">
                    <thead>
                        <tr>
                            <th scope="col">${this.escapeHtml(I18n.__('action'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('time'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('site'))}</th>
                            <th scope="col" class="is-end">${this.escapeHtml(I18n.__('hours'))}</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${workerLogs.map(log => `<tr>
                            <td class="${log.action === 'Clock In' ? 'is-in' : 'is-out'}"><strong>${this.escapeHtml(actionLabel(log))}</strong></td>
                            <td class="is-numeric">${this.escapeHtml(log.timestamp)}</td>
                            <td>${this.escapeHtml(log.site)}</td>
                            <td class="is-numeric is-end">${log.hours ? Number(log.hours).toFixed(2) : '-'}</td>
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
            // A queue is a count the worker is carrying, so it gets the one figure in the
            // panel plus the two things they can do about it: wait, or send it now.
            blocks.push(`
                <div class="hand-alert">
                    ${HAND_ICONS.cloudOff}
                    <div>
                        <p><strong>${this.escapeHtml(I18n.__('queuedPunches'))}: ${Number(status.queued) || 0}</strong></p>
                        <p>${this.escapeHtml(I18n.__('queuedPunchesHint'))}</p>
                        <button type="button" onclick="WORKER_MODULES.syncNow(this)" class="ui-btn ui-btn-sm">${HAND_ICONS.clock}${this.escapeHtml(I18n.__('syncNow'))}</button>
                    </div>
                </div>`);
        }
        if (!OFFLINE.online()) {
            blocks.push(`<p class="hand-hero-meta">${this.escapeHtml(I18n.__('offlineModeHint'))}</p>`);
        }
        if (status.deviceState === 'key_lost' || status.deviceState === 'revoked') {
            blocks.push(`
                <div class="hand-alert is-danger">
                    ${HAND_ICONS.alert}
                    <div>
                        <p>${this.escapeHtml(status.deviceState === 'revoked' ? I18n.__('deviceRevoked') : I18n.__('deviceKeyLost'))}</p>
                        ${status.deviceState === 'key_lost' ? `<button type="button" onclick="WORKER_MODULES.reRegisterDevice(this)" class="ui-btn ui-btn-sm ui-btn-danger">${this.escapeHtml(I18n.__('reRegisterDevice'))}</button>` : ''}
                    </div>
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

    // ``codeLabel`` is the shared one in frontendjavascript.js: an unknown code reads as
    // words instead of as a key, and an absent one reads as nothing.
    noteCategoryLabel(category) { return codeLabel('noteCat', category); },
    noteStatusLabel(status) { return codeLabel('noteStatus', status); },

    /** One state, one badge weight - the same four the console's tables use. */
    noteStatusClass(status) {
        if (status === 'resolved') return 'is-live';
        if (status === 'in_progress') return 'is-ok';
        if (status === 'closed') return 'is-quiet';
        return 'is-warn';
    },

    noteStatusChip(status) {
        return `<span class="ui-badge ${this.noteStatusClass(status)}">${this.escapeHtml(this.noteStatusLabel(status))}</span>`;
    },

    /** No category, no chip - an empty badge is a rendering artefact, not a fact. */
    noteCategoryChip(category) {
        const label = this.noteCategoryLabel(category);
        return label ? `<span class="ui-badge is-quiet">${this.escapeHtml(label)}</span>` : '';
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
            container.innerHTML = `<p class="ui-error">${this.escapeHtml(I18n.__('error'))}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        this._notes = data;
        container.innerHTML = this.notesListHtml(data);
    },

    notesListHtml(data) {
        const notes = (data && data.notes) || [];
        const open = Number(data && data.open) || 0;
        return `
            <div class="hand-section-head">
                <div class="ui-stack is-tight">
                    <h3 class="hand-section-title">${this.escapeHtml(I18n.__('notesMine'))}</h3>
                    <p class="hand-section-note">${this.escapeHtml(I18n.__('notesIntro'))}</p>
                </div>
                <button type="button" data-new-note onclick="WORKER_MODULES.startNote()"
                        class="ui-btn ui-btn-primary">${this.escapeHtml(I18n.__('notesNew'))}</button>
            </div>
            ${this._composingNote ? this.noteComposerHtml() : ''}
            ${open > 0 && !this._composingNote
                ? `<p class="hand-note-line">${this.escapeHtml(I18n.__('notesOpenHint'))}</p>`
                : ''}
            ${notes.length === 0
                ? `<div class="ui-empty" role="status">
                       <span class="ui-empty-icon">${HAND_ICONS.notes}</span>
                       <p class="ui-empty-title">${this.escapeHtml(I18n.__('notesEmpty'))}</p>
                       <p class="ui-empty-body">${this.escapeHtml(I18n.__('notesEmptyHint'))}</p>
                   </div>`
                : `<div class="hand-stack">${notes.map((note) => this.noteCardHtml(note)).join('')}</div>`}`;
    },

    noteCardHtml(note) {
        const unread = Number(note.worker_unread) || 0;
        const preview = note.last_message ? note.last_message.body : '';
        return `
            <button type="button" data-note="${note.id}" onclick="WORKER_MODULES.openNote(${note.id})"
                    class="hand-card is-note">
                <div class="ui-spread">
                    <p class="hand-note-subject">${this.escapeHtml(note.subject)}</p>
                    ${unread > 0
                        ? `<span data-unread="${unread}" class="ui-badge is-ok">${this.escapeHtml(I18n.__('noteWaitingReply'))}</span>`
                        : ''}
                </div>
                <p class="hand-note-tags">
                    ${this.noteCategoryChip(note.category)}
                    ${this.noteStatusChip(note.status)}
                    ${note.priority === 'high' ? `<span data-urgent="1" class="ui-badge is-danger">${this.escapeHtml(I18n.__('notePriorityHigh'))}</span>` : ''}
                </p>
                ${preview ? `<p class="hand-note-preview">${this.escapeHtml(preview)}</p>` : ''}
                <p class="hand-note-stamp">${this.escapeHtml(I18n.__('noteLastActivity'))}: ${this.escapeHtml(note.last_reply_at || note.created_at || '')}</p>
            </button>`;
    },

    /**
     * The new-note form. Category first, because it decides who has to read it.
     *
     * Every field carries a real `<label>` rather than a placeholder: a placeholder is
     * gone the moment somebody types, and this is a form filled in standing up, in the
     * sun, with a reason to be quick. The ids and the two handlers are load-bearing -
     * ``submitNote`` reads them back and the tests drive them by id - so only the shell
     * around them changed.
     */
    noteComposerHtml() {
        return `
            <div class="hand-composer" data-note-composer>
                <p class="hand-section-title">${this.escapeHtml(I18n.__('notesNew'))}</p>
                <label class="hand-field-group" style="margin-top:12px">
                    <span class="hand-field-label">${this.escapeHtml(I18n.__('noteCategory'))}</span>
                    <select id="noteCategory" class="ui-field">
                        ${this.NOTE_CATEGORIES.map((value) =>
                            `<option value="${value}">${this.escapeHtml(this.noteCategoryLabel(value))}</option>`).join('')}
                    </select>
                </label>
                <label class="hand-field-group">
                    <span class="hand-field-label">${this.escapeHtml(I18n.__('noteSubject'))}</span>
                    <input type="text" id="noteSubject" maxlength="120"
                           placeholder="${this.escapeHtml(I18n.__('noteSubjectPlaceholder'))}" class="ui-field">
                </label>
                <label class="hand-field-group">
                    <span class="hand-field-label">${this.escapeHtml(I18n.__('noteMessage'))}</span>
                    <textarea id="noteBody" rows="4" maxlength="2000"
                              placeholder="${this.escapeHtml(I18n.__('noteMessagePlaceholder'))}" class="ui-field"></textarea>
                </label>
                <label class="ui-check" style="margin-bottom:12px">
                    <input type="checkbox" id="noteUrgent">
                    <span>${this.escapeHtml(I18n.__('noteUrgent'))}</span>
                </label>
                <div class="ui-row">
                    <button type="button" data-send-note onclick="WORKER_MODULES.submitNote(this)"
                            class="ui-btn ui-btn-primary">${this.escapeHtml(I18n.__('noteSend'))}</button>
                    <button type="button" onclick="WORKER_MODULES.cancelNote()"
                            class="ui-btn ui-btn-quiet">${this.escapeHtml(I18n.__('noteCancel'))}</button>
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
            this._notesHost.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
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
        return `
            <button type="button" onclick="WORKER_MODULES.backToNotes()"
                    class="ui-btn ui-btn-quiet ui-btn-sm" data-notes-back>
                ${HAND_ICONS.back}${this.escapeHtml(I18n.__('noteBack'))}
            </button>
            <div class="hand-section-head" style="margin-top:12px">
                <div class="ui-stack is-tight">
                    <h3 class="hand-section-title">${this.escapeHtml(note.subject)}</h3>
                    <p class="hand-section-note">
                        ${this.escapeHtml(I18n.__('noteOpened'))}: ${this.escapeHtml(note.created_at || '')}
                    </p>
                </div>
                <span class="ui-row" style="gap:6px">
                    ${this.noteCategoryChip(note.category)}
                    ${this.noteStatusChip(note.status)}
                </span>
            </div>
            ${hint ? `<p class="hand-alert" data-thread-hint>${HAND_ICONS.info}<span>${this.escapeHtml(hint)}</span></p>` : ''}
            <div class="hand-stack" style="margin:12px 0 16px">
                ${messages.map((message) => this.noteMessageHtml(message)).join('')}
            </div>
            <div class="hand-card" style="background:var(--ops-surface-2);box-shadow:none">
                <textarea id="noteReplyBody" rows="3" maxlength="2000"
                          placeholder="${this.escapeHtml(I18n.__('noteReplyPlaceholder'))}" class="ui-field"></textarea>
                <div class="ui-row" style="margin-top:8px">
                    <button type="button" data-send-reply onclick="WORKER_MODULES.sendNoteReply(${note.id}, this)"
                            class="ui-btn ui-btn-primary">${this.escapeHtml(I18n.__('noteReply'))}</button>
                    ${note.status === 'closed' ? '' : `<button type="button" data-close-note onclick="WORKER_MODULES.closeNote(${note.id})"
                            class="ui-btn ui-btn-quiet">${this.escapeHtml(I18n.__('noteClose'))}</button>`}
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
        return `
            <div class="hand-bubble-row ${mine ? 'is-mine' : ''}" data-message="${message.id}">
                <div class="hand-bubble ${mine ? 'is-mine' : ''}">
                    <p class="hand-bubble-who">${this.escapeHtml(who)} · ${this.escapeHtml(message.created_at || '')}</p>
                    <p class="hand-bubble-body">${this.escapeHtml(message.body)}</p>
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
