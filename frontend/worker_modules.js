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
     * second and raises an inline "past 8.1h paid" note at the same line the server
     * uses - the same figure, the same basis and the same operator, so the card, the
     * administrator's alert and the clock-out gate light up on the same second rather
     * than a second or a break apart.
     *
     * The device clock drives the display only; the hours that count still come from the
     * server's clock-in record. The timer tears itself down as soon as the card is
     * replaced (tab switch, re-render, logout), so no interval outlives the panel.
     *
     * The timer counts time *on site*. The thresholds are about *paid* hours
     * (``shift_hours.OVERTIME_BASIS`` on the server, and the threshold it sends is a paid
     * figure), so the unpaid break is taken out of the count before either is compared - a
     * worker on a normal day sees the timer pass 8:00:00 without being told they are on
     * overtime, because at that point they have worked 7.5 h and been on site for 8. The
     * comparison is on seconds, like the server's: the break is a whole number of minutes
     * and the elapsed time is read to the second.
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

        // Started here rather than after the card is drawn so the two requests overlap: a
        // worker waiting with their thumb over the button pays for the slower of them, not
        // for both. It is awaited before the markup because the banner belongs in the first
        // paint - a warning that appears a beat after the button it is about is a warning
        // that arrives after somebody has already tapped it.
        //
        // Swallowed failure, deliberately: a notice that cannot be read is not a reason for
        // the one screen that has to work with no signal to show an error instead of a punch
        // card. What goes missing is the badge.
        const alerts = this.refreshAlerts().catch(() => null);

        let status;
        try {
            status = await this.fetchStatus();
        } catch (err) {
            container.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        await alerts;

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

            ${/* The inbox's own band, above the button and below the review alert: same
                 place, same reason. Both say "something about this shift needs you before
                 you tap", and a notice the system wrote about a shift that was closed under
                 the worker is exactly the thing they are about to discover by tapping. Filled
                 from ``State`` by ``paintAlertBadges`` right after this markup lands, so the
                 count on it and the count on the tab are one number. */ ''}
            <div id="workerAlertsBanner"></div>

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

        // The banner host is in the document now, so the notice line can be drawn into it.
        this.paintAlertBadges();

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
    //: The last self-report fetched, so the download is built from the rows on screen.
    _myHoursReport: null,

    //: The month the worker asked for (``YYYY-MM``), or "" for the server's own default -
    //: the month in progress, which is what "how much have I worked" means while it runs.
    _myHoursMonth: '',

    //: Columns ticked but not yet saved, or ``null`` while the account's own choice stands.
    //: Held in memory for the reason the console's credential editor holds an edit: a
    //: repaint (a month change, a tab switch) must not throw away a tick somebody made.
    _myHoursColumnDraft: null,

    /**
     * What the period added up to, and where it was worked.
     *
     * The reader's own timesheet, from the same server function the console's Shifts tab
     * reads - so the figure here and the figure an administrator sees for the same shifts
     * are one number, including the rule that matters most: hours nobody has signed off
     * are named as awaiting approval and kept out of the approved total. ``by_site`` is the
     * half that answers "how much did I work *where*", which is the question somebody sent
     * to two sites in a month actually asks.
     *
     * Silently absent when the report could not be read: the punch list below is the
     * record, and a summary that failed is not a reason to hide it.
     */
    myHoursSummaryHtml(report) {
        if (!report || !report.totals) return '';
        const totals = report.totals;
        const period = report.period || {};
        const hours = (value) => `${Number(value || 0).toFixed(2)} h`;
        const fact = (label, value) => `
            <div class="ui-fact">
                <span class="ui-fact-sub">${this.escapeHtml(label)}</span>
                <span class="ui-fact-value">${this.escapeHtml(value)}</span>
            </div>`;
        const sites = (report.by_site || []).map((site) =>
            fact(site.site_name, hours(site.hours))).join('');
        return `
            <section class="hand-card" data-my-hours="true">
                <div class="hand-section-head">
                    <div class="ui-stack is-tight">
                        <h3 class="hand-section-title">${this.escapeHtml(I18n.__('myHours'))}</h3>
                        <p class="hand-section-note">${this.escapeHtml(`${period.start || ''} \u2192 ${period.end || ''}`)}</p>
                    </div>
                    <div class="ui-row is-tight">
                        <!-- Which month this report is about. A month, not a free date range:
                             a timesheet is filed and paid by the month, and two fields that
                             must be kept in order is a way to ask for the wrong thing. -->
                        <label class="ui-label" for="myHoursMonth">${this.escapeHtml(I18n.__('myHoursMonth'))}</label>
                        <select id="myHoursMonth" class="ui-field is-flex">
                            ${this.myHoursMonthOptions(String(period.start || '').slice(0, 7))
                                .map((month) => `<option value="${this.escapeHtml(month.value)}"${month.chosen ? ' selected' : ''}>${this.escapeHtml(month.label)}</option>`)
                                .join('')}
                        </select>
                        <!-- Two files, one tap each, rather than a format picker: this is a
                             phone in daylight, and a control whose value decides what the
                             next button does is a control nobody notices. -->
                        <button type="button" class="ui-btn ui-btn-sm" data-download-hours="csv">${HAND_ICONS.history}${this.escapeHtml(I18n.__('myHoursDownload'))}</button>
                        <button type="button" class="ui-btn ui-btn-sm" data-download-hours="pdf">${HAND_ICONS.history}${this.escapeHtml(I18n.__('myHoursExportPdf'))}</button>
                    </div>
                </div>
                <div class="ui-facts">
                    ${fact(I18n.__('myHoursWorked'), hours(totals.hours))}
                    ${fact(I18n.__('myHoursApproved'), hours(totals.approved_hours))}
                    ${Number(totals.awaiting_approval_hours || 0) > 0
                        ? fact(I18n.__('myHoursAwaiting'), hours(totals.awaiting_approval_hours)) : ''}
                    ${Number(totals.overtime_hours || 0) > 0
                        ? fact(I18n.__('myHoursOvertime'), hours(totals.overtime_hours)) : ''}
                    ${fact(I18n.__('myHoursSites'), String(Number(totals.sites || 0)))}
                </div>
                ${sites
                    ? `<p class="hand-section-note" style="margin-top:var(--ui-3)">${this.escapeHtml(I18n.__('myHoursWhere'))}</p>
                       <div class="ui-facts">${sites}</div>`
                    : ''}
                ${this.myHoursColumnsHtml(report)}
            </section>`;
    },

    /**
     * Downloads the rows the summary was built from.
     *
     * Built here from the report in hand rather than streamed from the server, for the
     * reason the console's own download gives: a file re-derived on the server can
     * disagree with the figures on the screen it was taken from, and this one cannot.
     *
     * The columns are the worker's own - fixed English headers, so two exports of the same
     * month line up column for column whatever language the reader is in, and in the
     * vocabulary's order whatever order the boxes were ticked in.
     */
    downloadMyHoursCsv() {
        const report = this._myHoursReport;
        if (!report || !Array.isArray(report.rows)) {
            Toast.error(I18n.__('myHoursNothingToExport'));
            return;
        }
        const columns = this.myHoursColumns(report);
        const header = this.myHoursColumnDefs()
            .filter((def) => columns.indexOf(def.id) >= 0)
            .map((def) => def.csv);
        const lines = [header.join(',')];
        report.rows.forEach((row) => {
            lines.push(columns.map((id) => this.myHoursCsvCell(row, id)).join(','));
        });
        API.saveFile(`${this.myHoursExportName(report)}.csv`, lines.join('\r\n'));
    },

    /**
     * The months the report can be asked for: the chosen one, and the eleven before it.
     *
     * Twelve rather than an open range because that is the span a monthly timesheet is
     * argued about - "this month's pay", "last month's" - and a list is a control a worker
     * cannot get wrong: no dates to type, no two fields to keep in order, and nothing to
     * validate before it reaches the server. The names come from the platform's own
     * calendar (``Intl``) in the reader's current language, so "September 2026" is
     * September in Urdu on a phone that has Urdu, without a table of twelve names in four
     * languages that would go stale the moment the window moved.
     */
    myHoursMonthOptions(selected) {
        const latest = /^\d{4}-\d{2}$/.test(selected) ? selected : this.myHoursThisMonth();
        const [year, month] = latest.split('-').map(Number);
        const options = [];
        for (let back = 0; back < 12; back += 1) {
            const at = new Date(year, month - 1 - back, 1);
            const value = `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, '0')}`;
            options.push({ value, label: this.myHoursMonthLabel(at, value), chosen: back === 0 });
        }
        return options;
    },

    /** ``YYYY-MM`` for today, read from the device the way every other clock here is. */
    myHoursThisMonth() {
        const now = new Date();
        return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    },

    /** "September 2026" in the reader's language, or the value itself if that is refused. */
    myHoursMonthLabel(at, fallback) {
        try {
            return new Intl.DateTimeFormat(I18n.lang || 'en', { month: 'long', year: 'numeric' }).format(at);
        } catch (err) {
            return fallback;
        }
    },

    /**
     * The query that asks for one month, or nothing at all for the server's own default.
     *
     * ``end`` is the last day of the month, computed by asking for day 0 of the next one -
     * which is the one date arithmetic ``Date`` gets right for February in a leap year.
     */
    myHoursRangeQuery() {
        const month = String(this._myHoursMonth || '');
        if (!/^\d{4}-\d{2}$/.test(month)) return '';
        const [year, index] = month.split('-').map(Number);
        const lastDay = new Date(year, index, 0).getDate();
        return `?start=${month}-01&end=${month}-${String(lastDay).padStart(2, '0')}`;
    },

    /**
     * The columns a worker may keep in their own files: the vocabulary, the header each one
     * is written under, and the sheet's label for it.
     *
     * Mirrors ``reports.REPORT_COLUMNS`` in the backend, and a test compares the two lists -
     * in order, both ways - because a choice saved against this table has to still be a
     * column when it is read back: a column this file does not know how to fill is a column
     * of empty cells in somebody's payslip evidence.
     *
     * Every id is a field on the row the server sends, and every ``csv`` label is fixed
     * English - the console's own export convention, so two months of downloads line up
     * column for column whatever language the reader is in. A spreadsheet is data; the sheet
     * on paper is the translated one, which is why each column carries a ``key`` too.
     */
    myHoursColumnDefs() {
        return [
            { id: 'date', csv: 'Date', key: 'date' },
            { id: 'site', csv: 'Site', key: 'site' },
            { id: 'arrival', csv: 'Arrival', key: 'shiftsArrival' },
            { id: 'break', csv: 'Break hours', key: 'myHoursColBreak' },
            { id: 'hours', csv: 'Hours', key: 'hours' },
            { id: 'recorded', csv: 'Recorded hours', key: 'myHoursColRecorded' },
            { id: 'approved', csv: 'Approved hours', key: 'myHoursApproved' },
            { id: 'status', csv: 'Status', key: 'status' }
        ];
    },

    /** What an account that has never chosen gets: the file this screen has always written. */
    myHoursDefaultColumns() {
        return ['date', 'site', 'arrival', 'break', 'hours', 'approved', 'status'];
    },

    /**
     * The columns the two files carry right now: ticks not yet saved, else the account's own
     * choice, else the default.
     *
     * The draft wins, so a tick changes the next file immediately - which is the whole
     * promise of this screen, that the file and the figures beside it cannot disagree. Save
     * is what makes the choice outlive the page, not what makes it take effect. Anything the
     * vocabulary does not know is dropped and the order is always the vocabulary's, so what
     * the server sends back and what is ticked on screen describe the same file.
     */
    myHoursColumns(report) {
        const asked = (this._myHoursColumnDraft
            || (report && Array.isArray(report.columns) ? report.columns : []))
            .map((id) => String(id).toLowerCase());
        const chosen = this.myHoursColumnDefs()
            .map((def) => def.id)
            .filter((id) => asked.indexOf(id) >= 0);
        return chosen.length ? chosen : this.myHoursDefaultColumns();
    },

    /**
     * Tick or untick one column, refusing the one tick that cannot be taken.
     *
     * A file with no columns is not a report, so the *last* tick cannot come off: the change
     * is refused, the box springs back and the toast says why rather than leaving a control
     * that undid itself for no visible reason. The returned list is what the files will now
     * carry, which is how the caller knows whether to put the box back.
     */
    toggleMyHoursColumn(id, on) {
        const current = this.myHoursColumns(this._myHoursReport);
        const next = on
            ? this.myHoursColumnDefs()
                .map((def) => def.id)
                .filter((key) => key === id || current.indexOf(key) >= 0)
            : current.filter((key) => key !== id);
        if (!next.length) {
            Toast.error(I18n.__('myHoursColumnsEmpty'));
            return current;
        }
        this._myHoursColumnDraft = next;
        return next;
    },

    /**
     * Remember the choice on the account, so next month and the next device agree with it.
     *
     * The server's own list is what is kept, not the one that was sent: ids this build does
     * not know are dropped there, and a screen still offering a column the server cannot fill
     * would promise cells that come back empty. No repaint: the boxes on screen already say
     * exactly what the answer says, and a repaint would close the panel under the finger that
     * just pressed Save.
     */
    async saveMyHoursColumns() {
        const columns = this.myHoursColumns(this._myHoursReport);
        try {
            const saved = await API.request('/worker/me/report/columns', {
                method: 'POST', body: { columns }
            });
            this._myHoursColumnDraft = null;
            if (this._myHoursReport && saved && Array.isArray(saved.columns)) {
                this._myHoursReport.columns = saved.columns;
            }
            Toast.success(I18n.__('myHoursColumnsSaved'));
            return saved;
        } catch (err) {
            Toast.error(err.message);
            return null;
        }
    },

    /**
     * The chooser: one checkbox per column, and the one button that keeps the choice.
     *
     * Closed by default, because the card it sits in is the report and this is the shape of
     * two documents - a worker who never opens it still gets the file they always got. The
     * labels are the sheet's own, so the box that says "Break" is the column the paper will
     * head "Break": a chooser naming its columns differently from the file would make the
     * worker read two vocabularies for one thing.
     */
    myHoursColumnsHtml(report) {
        const chosen = this.myHoursColumns(report);
        const box = (def) => `
                <label class="ui-check">
                    <input type="checkbox" data-report-column="${def.id}"${chosen.indexOf(def.id) >= 0 ? ' checked' : ''}>
                    <span>${this.escapeHtml(I18n.__(def.key))}</span>
                </label>`;
        return `
            <details class="hand-columns" data-report-columns="true">
                <summary>${this.escapeHtml(I18n.__('myHoursColumns'))}</summary>
                <p class="hand-section-note">${this.escapeHtml(I18n.__('myHoursColumnsHint'))}</p>
                <div class="hand-columns-list">${this.myHoursColumnDefs().map(box).join('')}</div>
                <button type="button" class="ui-btn ui-btn-sm" data-save-columns="true">${this.escapeHtml(I18n.__('myHoursColumnsSave'))}</button>
            </details>`;
    },

    /** What one shift puts in one CSV column: the raw datum, unquoted. */
    myHoursCsvCell(row, id) {
        const text = (value) => {
            const raw = value === null || value === undefined ? '' : String(value);
            return /[",\r\n]/.test(raw) ? `"${raw.replace(/"/g, '""')}"` : raw;
        };
        const approve = row.approved_hours === null || row.approved_hours === undefined
            ? '' : Number(row.approved_hours).toFixed(2);
        switch (id) {
            case 'date': return text(row.date);
            case 'site': return text(row.site_name);
            // The clock-in time and the verdict together, the way the console's own
            // timesheet shows them: "04:12 late 12 min" answers "was I late, and by how
            // much", and neither half of it is worth a column on its own.
            case 'arrival': return text(
                [row.arrival_time || '', this.myHoursArrivalWords(row.arrival_verdict, row.arrival_minutes)]
                    .filter(Boolean).join(' '));
            case 'break': return text(Number(row.break_hours || 0).toFixed(2));
            case 'hours': return text(Number(row.hours || 0).toFixed(2));
            // Door to door: what the site saw, beside what the day counts for.
            case 'recorded': return text(
                row.recorded_hours === null || row.recorded_hours === undefined
                    ? '' : Number(row.recorded_hours).toFixed(2));
            case 'approved': return text(approve);
            case 'status': return text(
                row.awaiting_approval ? I18n.__('myHoursAwaiting') : (row.status || ''));
            default: return '';
        }
    },

    /** The plain-words arrival verdict, for a file whose other cells are data too. */
    myHoursArrivalWords(verdict, minutes) {
        const away = Number(minutes || 0);
        if (verdict === 'late') return `late ${away} min`;
        if (verdict === 'early') return `early ${away} min`;
        if (verdict === 'on_time') return 'on time';
        return '';
    },

    /**
     * The name both files are saved under, period included - without an extension, which
     * the print dialog appends itself.
     */
    myHoursExportName(report) {
        const period = (report && report.period) || {};
        return `my_hours_${period.start || 'from'}_${period.end || 'to'}`;
    },

    /**
     * The timesheet on paper - the PDF half of the download.
     *
     * Printed by the browser, through the same ``PrintReport`` the console's Shifts tab
     * prints through, for the reason stated there: no PDF library is pinned, and a sheet
     * the browser draws has the reader's own fonts and direction already right - which is
     * this page's whole problem, since a worker's report is read in Urdu as often as in
     * English and Arabic.
     *
     * Built from the report in hand, like the CSV, so the paper cannot disagree with the
     * figures on the screen it was printed from.
     */
    printMyHours() {
        const report = this._myHoursReport;
        if (!report || !Array.isArray(report.rows) || report.rows.length === 0) {
            // Nothing on screen for this period, so there is no honest sheet to print.
            Toast.error(I18n.__('myHoursNothingToExport'));
            return;
        }
        PrintReport.sheet(this.myHoursPrintHtml(report), this.myHoursExportName(report));
    },

    /**
     * The sheet: whose it is, what period, the days, and what they add up to.
     *
     * The frame - the title, the period line, the table, the note about approved hours - is
     * ``PrintReport.sheetHtml``'s, the same one the console's Shifts tab prints through:
     * the note at the foot is the console's own sentence (``shiftsApprovedOnly``) rather
     * than a second wording of the same rule, because the figure on paper and the figure on
     * the screen have to mean the same thing to the person holding both. This method
     * supplies what is this screen's own: whose month it is, and which figures.
     *
     * The columns are the worker's own, under the sheet's translated labels rather than the
     * CSV's fixed English ones - the same choice, read in the reader's language, which is
     * what makes the paper worth printing for somebody who chose Urdu.
     */
    myHoursPrintHtml(report) {
        const totals = report.totals || {};
        const period = report.period || {};
        const hours = (value) => `${Number(value || 0).toFixed(2)} h`;
        const who = [report.worker_name, report.worker_id ? `id ${report.worker_id}` : ''].filter(Boolean);

        const chosen = this.myHoursColumns(report);
        const defs = this.myHoursColumnDefs().filter((def) => chosen.indexOf(def.id) >= 0);
        // A worker's cell is a figure or a date, so it is escaped here and handed on as
        // text: the sheet's other cells - the console's - are markup of their own.
        const cells = report.rows.map(
            (row) => chosen.map((id) => this.escapeHtml(this.myHoursPrintCell(row, id)))
        );

        // The figures, named the way the card names them, so the two can be read together.
        const facts = [
            [I18n.__('myHoursWorked'), hours(totals.hours)],
            [I18n.__('myHoursApproved'), hours(totals.approved_hours)]
        ];
        if (Number(totals.awaiting_approval_hours || 0) > 0) {
            facts.push([I18n.__('myHoursAwaiting'), hours(totals.awaiting_approval_hours)]);
        }
        if (Number(totals.overtime_hours || 0) > 0) {
            facts.push([I18n.__('myHoursOvertime'), hours(totals.overtime_hours)]);
        }
        facts.push([I18n.__('myHoursSites'), String(Number(totals.sites || 0))]);
        const totalsLine = facts.map(([label, value]) => `${label} ${value}`).join(' · ');

        return PrintReport.sheetHtml({
            title: I18n.__('myHours'),
            meta: [who.join(' · '), PrintReport.periodLine(period)],
            columns: defs.map((def) => I18n.__(def.key)),
            rows: cells,
            totals: this.escapeHtml(totalsLine)
        });
    },

    /** What one shift puts in one of the sheet's columns, in the reader's own words. */
    myHoursPrintCell(row, id) {
        const hours = (value) => `${Number(value || 0).toFixed(2)} h`;
        switch (id) {
            case 'date': return row.date;
            case 'site': return row.site_name;
            case 'arrival': return [row.arrival_time || '', this.myHoursArrivalText(row)]
                .filter(Boolean).join(' · ');
            case 'break': return hours(row.break_hours);
            case 'hours': return hours(row.hours);
            case 'recorded': return row.recorded_hours === null || row.recorded_hours === undefined
                ? '' : hours(row.recorded_hours);
            case 'approved': return row.approved_hours === null || row.approved_hours === undefined
                ? '' : hours(row.approved_hours);
            case 'status': return row.awaiting_approval ? I18n.__('myHoursAwaiting') : (row.status || '');
            default: return '';
        }
    },

    /** "Late 12 min" in the reader's language, for the sheet's arrival cell. */
    myHoursArrivalText(row) {
        const minutes = Number(row.arrival_minutes || 0);
        if (row.arrival_verdict === 'late') {
            return I18n.__('shiftsArrivalLate').replace('{minutes}', minutes);
        }
        if (row.arrival_verdict === 'early') {
            return I18n.__('shiftsArrivalEarly').replace('{minutes}', minutes);
        }
        if (row.arrival_verdict === 'on_time') return I18n.__('shiftsArrivalOnTime');
        return I18n.__('shiftsArrivalUnknown');
    },

    /**
     * One listener per export button on the summary that was just rendered.
     *
     * Bound on the buttons themselves, so a repaint cannot leave a second listener behind -
     * the same idiom as the handset's tab bar. Deliberate rather than an ``onclick``: this
     * file's inline-handler count is pinned by ``test_frontend_xss.py`` and may fall but
     * never rise, and that allowance is what an injected ``<img onerror>`` needs.
     *
     * Which file a button writes is read off the button rather than remembered anywhere:
     * the two are always both on screen, so there is no state to go stale. The column
     * chooser's own controls are bound here too, and for the same reason.
     */
    bindHistoryControls(root) {
        const scope = root || document;
        if (typeof scope.querySelectorAll !== 'function') return;
        const buttons = scope.querySelectorAll('[data-download-hours]');
        for (let i = 0; i < buttons.length; i += 1) {
            const button = buttons[i];
            if (typeof button.addEventListener !== 'function') continue;
            button.addEventListener('click', (event) => {
                event.preventDefault();
                if (String(button.getAttribute('data-download-hours')) === 'pdf') {
                    this.printMyHours();
                    return;
                }
                // Anything that is not a deliberate "pdf" is the spreadsheet, so a stale
                // page or a value this build does not know cannot turn a download into a
                // print dialog nobody asked for.
                this.downloadMyHoursCsv();
            });
        }
        // The month picker is bound per render like the buttons, and the repaint is the
        // fetch: the report, the summary and the file all come from the period the server
        // answered for, rather than from the value of a field.
        const month = typeof scope.querySelector === 'function' ? scope.querySelector('#myHoursMonth') : null;
        if (month && typeof month.addEventListener === 'function') {
            month.addEventListener('change', () => {
                this._myHoursMonth = String(month.value || '');
                this.renderHistory(scope);
            });
        }
        // One listener per checkbox on the chooser that was just rendered, for the same
        // reason as the buttons above - and a refused change is written back onto the box,
        // because that state lived only in the DOM.
        const boxes = scope.querySelectorAll('[data-report-column]');
        for (let i = 0; i < boxes.length; i += 1) {
            const box = boxes[i];
            if (typeof box.addEventListener !== 'function') continue;
            box.addEventListener('change', () => {
                const id = String(box.getAttribute('data-report-column') || '');
                box.checked = this.toggleMyHoursColumn(id, box.checked === true).indexOf(id) >= 0;
            });
        }
        const save = typeof scope.querySelector === 'function' ? scope.querySelector('[data-save-columns]') : null;
        if (save && typeof save.addEventListener === 'function') {
            save.addEventListener('click', (event) => {
                event.preventDefault();
                this.saveMyHoursColumns();
            });
        }
    },

    async renderHistory(container) {
        if (!container) return;
        container.innerHTML = UI.loadingHtml();

        let workerLogs;
        let report = null;
        try {
            // Own rows only, and scoped by the token on the server rather than by a
            // filter here: /admin/logs answers 403 for a worker reading their own.
            workerLogs = (await API.request('/worker/me/logs?limit=20')).slice(0, 20);
        } catch (err) {
            container.innerHTML = `<p class="ui-error">${this.escapeHtml(I18n.__('error'))}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        try {
            // The month to date, which is what "how much have I worked" means while the
            // month is still running - or the month the worker picked. With nothing picked
            // the period is the server's own default, so the reader's clock and the site's
            // cannot disagree about it.
            report = await API.request(`/worker/me/report${this.myHoursRangeQuery()}`);
        } catch (err) {
            report = null;
        }
        this._myHoursReport = report;

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

        const summary = this.myHoursSummaryHtml(report);

        if (Device.isMobile) {
            container.innerHTML = `${summary}
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
            this.bindHistoryControls(container);
            return;
        }

        container.innerHTML = `${summary}
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
        this.bindHistoryControls(container);
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
    },

    // ==========================================================================
    // The worker's own inbox
    // ==========================================================================

    //: The list the server last answered with. Held in memory so marking one read repaints
    //: from what the reader just did, rather than from a second read of the same rows - the
    //: rows are the evidence, and a repaint that re-asks can only disagree with them.
    _alerts: null,

    //: Where the inbox was drawn, so a repaint has somewhere to go.
    _alertsHost: null,

    /**
     * How many notices are unread, and the newest one among them.
     *
     * One number in one place, because it is painted in two: the badge on the handset's
     * alerts tab and the banner on the clock panel. Two requests would be two chances for
     * the tab to say "2" while the banner says "1".
     */
    alertCount: 0,
    _newestUnread: null,

    /**
     * Read the waiting count without opening the inbox.
     *
     * Called from the clock panel, which is where a worker is standing when the notice
     * matters: the auto-close writes its notice precisely because the *next* clock-out
     * refuses them, so the sentence has to be in front of them before they tap that
     * button, not behind a tab they have not opened.
     *
     * ``unread_only`` and ``limit=1``: the badge is about what is unread, and a list of
     * read notices nobody is looking at is payload a phone on site cellular should not pay
     * for twice.
     *
     * Throwing is deliberate - the caller decides. On the clock panel this failure is
     * silence, because a badge that cannot be read is not a reason for the one screen that
     * has to work with no signal to show an error instead of a punch card.
     */
    async refreshAlerts() {
        const data = await API.request('/worker/me/notifications?limit=1&unread_only=true');
        this.setAlertState({
            unread: data && data.unread,
            newest: (data && data.notifications && data.notifications[0]) || null
        });
        return data;
    },

    /** One place that writes the count, so the two badges can never disagree. */
    setAlertState({ unread, newest }) {
        this.alertCount = Math.max(0, Number(unread) || 0);
        this._newestUnread = newest || null;
        this.paintAlertBadges();
    },

    /**
     * Repaint the two places the count shows.
     *
     * Nothing here knows which layout is up: the desktop has no tab bar and no banner (its
     * inbox is a card on the same screen), so both calls do nothing there rather than the
     * caller branching.
     */
    paintAlertBadges() {
        if (typeof UI !== 'undefined' && UI.repaintWorkerTabBar) UI.repaintWorkerTabBar();
        const banner = document.getElementById('workerAlertsBanner');
        if (!banner) return;
        banner.innerHTML = this.alertsBannerHtml();
        // Bound where the host is filled, and once per host: the band is a control, and a
        // delegated listener is what keeps ``onclick`` out of the markup - the inline
        // handlers in this file are counted by ``test_frontend_xss`` and the count only
        // falls. The host itself is new on every render, so the mark is per element and not
        // a flag on this module.
        if (!banner.__alertsBound && banner.addEventListener) {
            banner.addEventListener('click', (event) => {
                if (event.target.closest('[data-alerts-banner]')) UI.setWorkerTab('alerts');
            });
            banner.__alertsBound = true;
        }
    },

    /**
     * The clock panel's one line about it: how many, what the newest one says, and the way
     * in.
     *
     * A ``<button>`` rather than a band with a link in it: the whole width is the target, and
     * the "See all" at its end is what says that tapping it goes somewhere. Absent on a
     * desktop, where the inbox is a card of its own a few centimetres away - a banner
     * pointing at a tab that does not exist there would be a button to nowhere.
     */
    alertsBannerHtml() {
        const count = Math.max(0, Number(this.alertCount) || 0);
        if (!count || !Device.isMobile) return '';
        const line = count === 1
            ? I18n.__('alertsUnreadOne')
            : I18n.__('alertsUnreadMany').replace('{count}', String(count));
        const newest = this._newestUnread;
        return `
            <button type="button" class="hand-alert is-tappable" data-alerts-banner>
                ${HAND_ICONS.alert}
                <span class="hand-alert-body">
                    <span class="hand-alert-headline">${this.escapeHtml(line)}</span>
                    ${newest ? `<span class="hand-alert-line">${this.escapeHtml(newest.title)}</span>` : ''}
                </span>
                <span class="hand-alert-more">${this.escapeHtml(I18n.__('alertsOpen'))}</span>
            </button>`;
    },

    /**
     * The inbox: what this application has told *this* worker, newest first.
     *
     * The other half of the push channel, and the reason it is worth having even on a
     * deployment with no VAPID keys at all: a phone that was off, a permission that was
     * never granted or a push that failed all leave the event here. The server scopes the
     * list by the token rather than by anything this screen sends, so there is no id here
     * to get wrong.
     */
    async renderAlerts(container) {
        if (!container) return;
        this._alertsHost = container;
        container.innerHTML = UI.loadingHtml();
        let data;
        try {
            data = await API.request('/worker/me/notifications');
        } catch (err) {
            container.innerHTML = `<p class="ui-note is-body is-danger">${this.escapeHtml(I18n.__('error'))}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        this._alerts = data;
        container.innerHTML = this.alertsListHtml(data);
        this.bindAlerts(container);
        this.setAlertState({
            unread: data.unread,
            newest: (data.notifications || []).filter((notice) => !notice.read)[0] || null
        });
    },

    /**
     * One listener for the whole inbox, bound to the host rather than to each card.
     *
     * The list is replaced wholesale on every read, so a listener per card would have to be
     * re-attached on each repaint or go quietly dead; the host is the one element that
     * outlives the markup. It also keeps ``onclick`` out of the string, which is the CSP
     * budget ``test_frontend_xss`` pins - and only a ``button[data-alert]`` is a read, so a
     * notice that is already read has nothing to trigger.
     */
    bindAlerts(root) {
        const host = root || this._alertsHost;
        if (!host || !host.addEventListener) return;
        host.addEventListener('click', (event) => {
            if (event.target.closest('[data-mark-all-alerts]')) {
                this.markAllAlertsRead();
                return;
            }
            const row = event.target.closest('button[data-alert]');
            if (row) this.markAlertRead(row.getAttribute('data-alert'));
        });
    },

    alertsListHtml(data) {
        const notices = (data && data.notifications) || [];
        const unread = Math.max(0, Number(data && data.unread) || 0);
        return `
            <div class="hand-section-head">
                <div class="ui-stack is-tight">
                    <h3 class="hand-section-title">${this.escapeHtml(I18n.__('alertsMine'))}</h3>
                    <p class="hand-section-note">${this.escapeHtml(I18n.__('alertsIntro'))}</p>
                </div>
                ${unread > 0
                    ? `<button type="button" data-mark-all-alerts
                               class="ui-btn">${this.escapeHtml(I18n.__('alertsMarkAll'))}</button>`
                    : ''}
            </div>
            ${notices.length === 0
                ? `<div class="ui-empty" role="status">
                       <span class="ui-empty-icon">${HAND_ICONS.alert}</span>
                       <p class="ui-empty-title">${this.escapeHtml(I18n.__('alertsEmpty'))}</p>
                       <p class="ui-empty-body">${this.escapeHtml(I18n.__('alertsEmptyHint'))}</p>
                   </div>`
                : `<div class="hand-stack">${notices.map((notice) => this.alertCardHtml(notice)).join('')}</div>`}`;
    },

    // ``codeLabel`` is the shared one in frontendjavascript.js, for the same reason the note
    // category uses it: a kind this build does not know yet reads as opened-up words, and
    // the chip is dropped rather than drawn empty when there is nothing to say.
    alertKindLabel(kind) { return codeLabel('alertKind', kind); },

    /**
     * One notice: the server's two sentences, the kind, when it was sent.
     *
     * A ``<button>`` when there is something to do with it and a ``<div>`` when there is
     * not. Reading a notice is the only action it has, so a read one is not a control at
     * all - a card that looks tappable and does nothing is worse than a card that plainly
     * does not.
     *
     * The title and the body are values off the wire, escaped like every other one, and the
     * kind is a chip because it is what a reader scans for: a crossing wants a different
     * reaction from a day the system closed for them.
     */
    alertCardHtml(notice) {
        const unread = !notice.read;
        const kind = this.alertKindLabel(notice.kind);
        const inner = `
            <div class="ui-spread">
                <p class="hand-note-subject">${this.escapeHtml(notice.title)}</p>
                ${unread ? `<span class="ui-badge is-danger">${this.escapeHtml(I18n.__('alertsNew'))}</span>` : ''}
            </div>
            ${kind ? `<p class="hand-note-tags"><span class="ui-badge is-quiet">${this.escapeHtml(kind)}</span></p>` : ''}
            <p class="hand-note-preview">${this.escapeHtml(notice.body)}</p>
            <p class="hand-note-stamp">${this.escapeHtml(I18n.__('alertsSent'))}: ${this.escapeHtml(notice.created_at || '')}</p>`;
        // Not ``is-note``: that class carries the pointer cursor and the hover state of a
        // control, and this one has nothing to do when it is tapped.
        if (!unread) return `<div class="hand-card" data-alert="${notice.id}">${inner}</div>`;
        return `
            <button type="button" data-alert="${notice.id}" data-alert-unread="1" class="hand-card is-note">
                ${inner}
                <span class="hand-alert-more">${this.escapeHtml(I18n.__('alertsMarkRead'))}</span>
            </button>`;
    },

    /**
     * Mark one notice read.
     *
     * The id goes in the **query string**, not a JSON body: the route takes it as a plain
     * parameter, so a body it never reads would mark the whole inbox read - the one mistake
     * here that destroys information instead of showing it. ``backend/tests`` and this
     * module's own suite both pin the URL for exactly that reason.
     */
    async markAlertRead(alertId) {
        try {
            await API.request(`/worker/me/notifications/read?notification_id=${encodeURIComponent(alertId)}`, {
                method: 'POST'
            });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('alertsMarkedRead'));
        this.markReadLocally([Number(alertId)]);
        await this.repaintAlerts();
    },

    /** The whole inbox at once - the button that is only offered while something is unread. */
    async markAllAlertsRead() {
        try {
            await API.request('/worker/me/notifications/read', { method: 'POST' });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('alertsMarkedAll'));
        this.markReadLocally(null);
        await this.repaintAlerts();
    },

    /**
     * Apply a read to the copy in memory.
     *
     * ``ids === null`` means the whole inbox. The count is worked out from the rows *this*
     * page holds rather than from the server's ``marked`` total, because a worker with more
     * notices than fit on one screen has unread rows this list has never seen, and the badge
     * must keep counting them.
     */
    markReadLocally(ids) {
        if (!this._alerts) return;
        const notices = this._alerts.notifications || [];
        let cleared = 0;
        notices.forEach((notice) => {
            if (notice.read) return;
            if (ids && ids.indexOf(Number(notice.id)) < 0) return;
            notice.read = true;
            cleared += 1;
        });
        this._alerts.unread = Math.max(0, Number(this._alerts.unread || 0) - cleared);
        this.setAlertState({
            unread: this._alerts.unread,
            newest: notices.filter((notice) => !notice.read)[0] || null
        });
    },

    /** Repaint the inbox from the rows on screen - no request, nothing can be lost. */
    repaintAlerts() {
        if (!this._alertsHost || !this._alerts) return this.renderAlerts(this._alertsHost);
        this._alertsHost.innerHTML = this.alertsListHtml(this._alerts);
        return Promise.resolve();
    },

    // ---------------------------------------------------------------------
    // The push half of the channel.
    //
    // The inbox above is the *record*: it is here whenever the app is open. Push is
    // the *delivery*: it is what tells the worker there is something to read when the
    // app is closed. The backend has had both ends of the wire for a while -
    // ``GET /worker/me/push`` reports whether this deployment has VAPID keys, and
    // ``POST /worker/me/push/subscribe`` stores what the browser hands over - but
    // nothing in the frontend ever asked. A notice arrived only if the worker happened
    // to be looking at the app, which is precisely when they did not need a push.
    //
    // The flow, in the order the browser allows it:
    //
    //   1. register ``push-service-worker.js`` (a web push cannot exist without one);
    //   2. ask the backend whether pushing is *configured here* - a deployment with no
    //      VAPID keys must never prompt for a permission nothing can use;
    //   3. ask the browser for the ``PushManager`` subscription, showing the permission
    //      prompt only when the worker has actually chosen to enable it;
    //   4. send the subscription to the backend, which validates the endpoint against
    //      its push-service allowlist and stores it against the signed-in account.
    //
    // Every step is skippable and the app does not care: push is a convenience layered
    // on the inbox, never a dependency of it.
    // ---------------------------------------------------------------------

    /** Where the subscription state lives between renders (not persisted: the browser owns it). */
    _pushConfig: null,
    _pushRegistration: null,
    _pushSubscription: null,

    /**
     * Register the service worker, quietly.
     *
     * Called once per session from ``UI.init``. Not awaited, and every failure is
     * swallowed into a state the settings card can name: a browser without service
     * workers (old WebViews on site phones are the common case) still gets the whole
     * app - what it does not get is a promise about notifications, and the card is
     * where that is said instead of a toast nobody can read later.
     */
    async initPush() {
        if (!('serviceWorker' in navigator)) {
            this._pushConfig = { supported: false, reason: 'pushSwUnsupported' };
            return;
        }
        try {
            this._pushRegistration = await navigator.serviceWorker.register('push-service-worker.js');
        } catch (err) {
            this._pushConfig = { supported: false, reason: 'pushSwFailed' };
            return;
        }
        // The config read and the subscription read are independent: a deployment
        // that cannot push still wants to show "off" rather than a spinner, and a
        // configured one wants to show the truth about this browser.
        const [config, subscription] = await Promise.all([
            this.fetchPushConfig().catch(() => null),
            this.readPushSubscription().catch(() => null),
        ]);
        this._pushConfig = config;
        this._pushSubscription = subscription;
    },

    /** The deployment's answer: can this server push, and with what public key. */
    async fetchPushConfig() {
        const config = await API.request('/worker/me/push');
        return { supported: true, ...config };
    },

    /** What this browser is currently subscribed to, if anything. */
    async readPushSubscription() {
        if (!this._pushRegistration) return null;
        return this._pushRegistration.pushManager.getSubscription();
    },

    /**
     * One turn of the switch.
     *
     * Enabling is the only path that can prompt: ``subscribeUser`` shows the browser's
     * permission dialog, which is why it is behind a control the worker pressed rather
     * than fired on sign-in. Disabling is silent - ``unsubscribe`` releases the browser
     * subscription, and the backend row is retired by endpoint so nothing can address it.
     */
    async togglePush(enabled) {
        if (enabled) {
            await this.enablePush();
        } else {
            await this.disablePush();
        }
    },

    async enablePush() {
        if (!this._pushRegistration || !this._pushConfig || !this._pushConfig.available) {
            throw new Error(I18n.__('pushUnavailable'));
        }
        if (!this._pushConfig.public_key) {
            throw new Error(I18n.__('pushUnavailable'));
        }
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') {
            // Denied or dismissed are the same answer here: no subscription was
            // created, and the card keeps saying "off". Not an error - a worker who
            // said no later gets exactly the same prompt by tapping again.
            throw new Error(I18n.__('pushPermissionDenied'));
        }
        const subscription = await this._pushRegistration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: this.urlBase64ToUint8Array(this._pushConfig.public_key),
        });
        const json = subscription.toJSON();
        const keys = (json && json.keys) || {};
        try {
            await API.request('/worker/me/push/subscribe', {
                method: 'POST',
                body: { endpoint: json.endpoint, p256dh: keys.p256dh, auth: keys.auth },
            });
        } catch (err) {
            // The backend refused the subscription (endpoint not on the push-service
            // allowlist, deployment lost its keys between the config read and now).
            // Release the browser-side subscription too, or the card would say "on"
            // for a subscription no server will ever send to.
            await subscription.unsubscribe().catch(() => {});
            throw err;
        }
        this._pushSubscription = subscription;
    },

    async disablePush() {
        const subscription = this._pushSubscription || await this.readPushSubscription();
        if (subscription) {
            const endpoint = subscription.endpoint;
            try {
                await API.request('/worker/me/push/unsubscribe', {
                    method: 'POST',
                    body: { endpoint },
                });
            } catch (err) {
                // The server could not be told (offline, 5xx). The worker asked for
                // off and the browser side is being released regardless, so this is
                // not a failure to surface as a thrown error - the toggle would look
                // broken for having done exactly what was asked. A stale row the
                // server keeps is harmless: the next failed delivery retires it.
                console.warn('push unsubscribe not acknowledged', err);
            }
            await subscription.unsubscribe().catch(() => {});
        }
        this._pushSubscription = null;
    },

    /**
     * The VAPID public key arrives URL-safe base64; ``applicationServerKey`` wants bytes.
     * The padding is re-added because atob throws on a length that is not a multiple of
     * four, and browser keys omit it.
     */
    urlBase64ToUint8Array(base64String) {
        const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
        const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        const raw = atob(base64);
        const output = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
        return output;
    },

    /**
     * The settings card, on the profile tab.
     *
     * It has three honest states and says which one it is in: unavailable (this browser
     * has no service worker, or this deployment has no VAPID keys - with the reason),
     * off (a switch the worker can turn on), and on (a switch they can turn off, plus
     * how many devices are already subscribed server-side). A toggle is bound here in
     * code rather than inline for the same reason as every other control in this file:
     * the CSP budget is pinned and only falls.
     */
    pushSettingsHtml() {
        const config = this._pushConfig;
        if (!config) {
            return this.pushCardHtml(I18n.__('pushChecking'), null, null);
        }
        if (config.supported === false) {
            return this.pushCardHtml(I18n.__(config.reason || 'pushSwUnsupported'), null, null);
        }
        if (!config.available) {
            // The reason from the server names the missing piece ("no VAPID key pair is
            // configured..."); it is an operator-facing sentence but it is honest, and
            // a worker told "the server cannot send notifications here" can bring the
            // right problem to the right person.
            return this.pushCardHtml(I18n.__('pushServerOff'), null, config.reason || '');
        }
        const on = !!this._pushSubscription;
        const count = Math.max(0, Number(config.subscriptions) || 0);
        const note = on
            ? I18n.__('pushOnNote')
            : I18n.__('pushOffNote');
        return this.pushCardHtml(note, on, null, count);
    },

    pushCardHtml(note, enabled, errorText, deviceCount) {
        const count = Math.max(0, Number(deviceCount) || 0);
        return `
            <section class="hand-card" data-push-card>
                <div class="hand-section-head">
                    <h3 class="hand-section-title">${HAND_ICONS.alert}${this.escapeHtml(I18n.__('pushTitle'))}</h3>
                </div>
                <p class="hand-section-note">${this.escapeHtml(note)}</p>
                ${errorText ? `<p class="ui-note is-warn">${this.escapeHtml(errorText)}</p>` : ''}
                ${enabled !== null
                    ? `<button type="button" data-push-toggle="${enabled ? 'off' : 'on'}" class="ui-btn ${enabled ? '' : 'ui-btn-primary'}">
                        ${this.escapeHtml(enabled ? I18n.__('pushDisable') : I18n.__('pushEnable'))}
                    </button>`
                    : ''}
                ${count > 0 ? `<p class="hand-note-stamp">${this.escapeHtml(I18n.__('pushDevices').replace('{count}', String(count)))}</p>` : ''}
            </section>`;
    },

    /** Bind the card's one control. The host is the profile container; re-binding is safe. */
    bindPushSettings(host) {
        if (!host || !host.addEventListener) return;
        host.addEventListener('click', async (event) => {
            const button = event.target.closest('[data-push-toggle]');
            if (!button) return;
            const enabling = button.getAttribute('data-push-toggle') === 'on';
            button.disabled = true;
            try {
                await this.togglePush(enabling);
                if (enabling) {
                    Toast.success(I18n.__('pushEnabled'));
                } else {
                    Toast.info(I18n.__('pushDisabled'));
                }
            } catch (err) {
                Toast.error(err.message || I18n.__('pushUnavailable'));
            } finally {
                // Re-read the deployment config so the device count is the server's
                // truth, then repaint the card from state rather than optimism.
                this._pushConfig = await this.fetchPushConfig().catch(() => this._pushConfig);
                this.refreshPushCard();
            }
        });
    },

    /** Repaint just the push card in place - the profile around it is untouched. */
    refreshPushCard() {
        const host = document.getElementById('pushSettings');
        if (!host) return;
        host.innerHTML = this.pushSettingsHtml();
    },

    // -----------------------------------------------------------------------
    // Calibration capture: the worker's own opt-in
    // -----------------------------------------------------------------------
    /**
     * The face-matching photo card, on the profile tab.
     *
     * The corpus it feeds is a biometric store, and the consent that permits it is the
     * worker's own. The backend has accepted that decision since migration 23 - recorded in
     * an append-only table and in ``audit_log`` - but the only way to make it was an HTTP
     * call, which for the person whose face is kept is the same as having no way at all.
     *
     * What this card must get right, and why each part is here rather than left implicit:
     *
     * * **The wording says what is kept.** A toggle labelled "help improve the service" is
     *   not consent to keep somebody's photograph; the note names the photo, says who
     *   cannot see the answer, and says it can be stopped.
     * * **The current state is the server's, not the page's.** ``_corpusConsent`` holds what
     *   ``GET /worker/me/corpus/consent`` answered, and the card is repainted from a fresh
     *   read after every change - a switch that shows "on" from optimism is a switch that
     *   lies about a consent record.
     * * **One endpoint, both directions.** Withdrawal goes through the same call with
     *   ``granted: false``, because a worker who wants out must not have to find another
     *   screen for it.
     */
    _corpusConsent: null,

    /**
     * Read the worker's own capture consent once per session, quietly.
     *
     * Called from ``UI.init`` beside ``initPush``. A failure leaves ``_corpusConsent`` null,
     * which the card renders as "checking…" and no control at all - deliberately: offering an
     * "I agree" button whose write the page has not proven it can make is asking somebody to
     * consent to something that may not be recorded, and a consent that might not be recorded
     * is not a consent.
     */
    async initCorpusConsent() {
        this._corpusConsent = await this.fetchCorpusConsent().catch(() => null);
        return this._corpusConsent;
    },

    async fetchCorpusConsent() {
        return API.request('/worker/me/corpus/consent');
    },

    corpusConsentHtml() {
        const state = this._corpusConsent;
        if (!state) {
            return this.corpusCardHtml(I18n.__('corpusChecking'), null);
        }
        const on = !!state.granted;
        return this.corpusCardHtml(
            on ? I18n.__('corpusOnNote') : I18n.__('corpusOffNote'),
            on
        );
    },

    corpusCardHtml(note, granted) {
        return `
            <section class="hand-card" data-corpus-card>
                <div class="hand-section-head">
                    <h3 class="hand-section-title">${HAND_ICONS.camera}${this.escapeHtml(I18n.__('corpusTitle'))}</h3>
                </div>
                <p class="hand-section-note">${this.escapeHtml(I18n.__('corpusNote'))}</p>
                <p class="hand-section-note">${this.escapeHtml(note)}</p>
                ${granted !== null
                    ? `<button type="button" data-corpus-consent="${granted ? 'withdraw' : 'grant'}" class="ui-btn ${granted ? '' : 'ui-btn-primary'}">
                        ${this.escapeHtml(granted ? I18n.__('corpusWithdraw') : I18n.__('corpusEnable'))}
                    </button>`
                    : ''}
            </section>`;
    },

    /** Bind the card's one control. The host is the profile container; re-binding is safe. */
    bindCorpusConsent(host) {
        if (!host || !host.addEventListener) return;
        host.addEventListener('click', async (event) => {
            const button = event.target.closest('[data-corpus-consent]');
            if (!button) return;
            const granting = button.getAttribute('data-corpus-consent') === 'grant';
            button.disabled = true;
            try {
                await API.request('/worker/me/corpus/consent', {
                    method: 'POST',
                    body: { granted: granting }
                });
                if (granting) {
                    Toast.success(I18n.__('corpusGranted'));
                } else {
                    Toast.info(I18n.__('corpusWithdrawn'));
                }
            } catch (err) {
                Toast.error(err.message || I18n.__('corpusUnavailable'));
            } finally {
                // The server's answer, not the button's - a failed write must not leave the
                // card saying "on" for a decision nobody recorded.
                this._corpusConsent = await this.fetchCorpusConsent().catch(
                    () => this._corpusConsent
                );
                this.refreshCorpusConsentCard();
            }
        });
    },

    /** Repaint just the consent card in place - the profile around it is untouched. */
    refreshCorpusConsentCard() {
        const host = document.getElementById('corpusConsent');
        if (!host) return;
        host.innerHTML = this.corpusConsentHtml();
    }
};
