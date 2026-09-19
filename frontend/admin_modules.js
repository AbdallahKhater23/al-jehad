// --- UI Controller - Expanded Modules ---
const UI_MODULES = {
    // =====================================================================
    //  Live Ops - who is on site right now, and the one action that changes it
    // =====================================================================
    //
    // The board answers three questions in the order an operator asks them: how
    // many people are on site, which shift is the one I have to do something
    // about, and which row is that. It used to answer none of them: a plain
    // four-column table, a frozen clock-in timestamp, and no way to find one
    // person in a list of forty.
    //
    // Three rules this screen is built on:
    //
    // * **The figures agree with the payslip.** Elapsed time is recomputed here
    //   every second, but which hours are *paid* follows ``shift_hours.py``
    //   exactly - the unpaid break is charged only once a shift is long enough
    //   to have contained one, and both thresholds are compared against paid
    //   hours. A board that flagged overtime 30 minutes early would be worse
    //   than no board.
    // * **Live without flicker.** A one-second local tick moves the numerals; a
    //   slow poll re-reads the session list and repaints *only* when the data
    //   actually changed. Nothing steals focus, nothing wipes the search box,
    //   and a background tab makes no requests at all.
    // * **Colour is never the only signal.** Every state carries an icon and a
    //   word, and the one live region on the page announces a sentence ("On
    //   site now: 4. Sites with people: 2.") rather than a bare number.

    //: The last good read of the board, and the handles that keep it live.
    _liveOps: null,
    _liveOpsTick: null,
    _liveOpsPoll: null,
    //: Guards against a slow render landing after a newer one (rapid tab clicks).
    _liveOpsRun: 0,
    _liveOpsQuery: '',
    _liveOpsSite: '',
    _liveOpsSort: 'longest',

    //: How often the session list is re-read. One request, and a repaint only
    //: when the answer differs - polling that repaints regardless is what makes
    //: a dashboard feel broken.
    LIVE_OPS_POLL_MS: 45000,

    /**
     * Inline SVG, never an emoji: an emoji is font-dependent, renders
     * differently on every phone, and cannot take a token colour.
     */
    OPS_ICONS: {
        search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>',
        refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"></path><path d="M21 3v6h-6"></path></svg>',
        person: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path></svg>',
        alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 20h20L12 3Z"></path><path d="M12 10v4"></path><path d="M12 17h.01"></path></svg>',
        clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>',
        // A sort indicator is an icon, so it is drawn like one: an arrow glyph
        // (↓) is a font character that every platform renders differently.
        chevronDown: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"></path></svg>',
        chevronUp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 15 6-6 6 6"></path></svg>',

        // The rest of the console's icons. Same rules as above - an inline SVG on a
        // 24-grid, drawn with currentColor so a badge, a button and a nav item can
        // each tint the same glyph with their own token.
        check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 12.5 5 5 10-11"></path></svg>',
        close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"></path></svg>',
        plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>',
        trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"></path><path d="M9 7V4h6v3"></path><path d="m6.5 7 1 13h9l1-13"></path></svg>',
        pin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s7-5.7 7-11a7 7 0 1 0-14 0c0 5.3 7 11 7 11Z"></path><circle cx="12" cy="10" r="2.5"></circle></svg>',
        key: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="15" r="4"></circle><path d="m11 12 8-8M17 6l2 2M15 8l2 2"></path></svg>',
        note: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-8 8H8l-5 3 1.4-4.3A8 8 0 1 1 21 12Z"></path></svg>',
        link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.5 13.5a4.5 4.5 0 0 0 6.4 0l2.6-2.6a4.5 4.5 0 0 0-6.4-6.4l-1 1"></path><path d="M13.5 10.5a4.5 4.5 0 0 0-6.4 0l-2.6 2.6a4.5 4.5 0 0 0 6.4 6.4l1-1"></path></svg>',
        sliders: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h8M16 6h4M4 12h4M12 12h8M4 18h8M16 18h4"></path><circle cx="14" cy="6" r="2"></circle><circle cx="10" cy="12" r="2"></circle><circle cx="14" cy="18" r="2"></circle></svg>',
        shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v6c0 5-3 8-7 9-4-1-7-4-7-9V6l7-3Z"></path><path d="m9 12 2 2 4-4"></path></svg>',
        download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11"></path><path d="m7.5 11 4.5 4.5L16.5 11"></path><path d="M5 20h14"></path></svg>',
        copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M15 5.5A2.5 2.5 0 0 0 12.5 3H6a3 3 0 0 0-3 3v6.5A2.5 2.5 0 0 0 5.5 15"></path></svg>',
        camera: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l1.5-2h7L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Z"></path><circle cx="12" cy="13.5" r="3.5"></circle></svg>',
        pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L20 8l-4-4L4 16v4Z"></path><path d="m14 6 4 4"></path></svg>',
        power: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 4v8"></path><path d="M7.5 7.5a6.5 6.5 0 1 0 9 0"></path></svg>',
        table: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="M3 10h18M9 10v9"></path></svg>'
    },

    /**
     * One sentence a screen can say when a read fails: what failed, and why.
     *
     * The reason is the point. "Database is locked." tells an admin to try again in a
     * minute; a generic "something went wrong" tells them to phone somebody. Every tab
     * paints this rather than leaving a red line with only the word "Error" in it.
     */
    uiErrorHtml(err, retry) {
        const message = (err && (err.message || err.detail)) || I18n.__('error');
        return `
            <div class="ui-error" role="alert">
                <span class="ui-stack is-tight">
                    <span>${this.escapeHtml(I18n.__('error'))}</span>
                    <span class="ops-sub">${this.escapeHtml(message)}</span>
                </span>
                ${retry ? `<button type="button" class="ui-btn" data-retry="true" onclick="${retry}">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('liveOpsRetry'))}</button>` : ''}
            </div>`;
    },

    /** A clock-in timestamp as a Date, or null when it cannot be read. */
    liveOpsStart(clockInTime) {
        if (!clockInTime) return null;
        // The server writes "YYYY-MM-DD HH:MM:SS"; Safari refuses that shape
        // without the T, which is why the worker's own card does the same swap.
        const start = new Date(String(clockInTime).replace(' ', 'T'));
        return isNaN(start.getTime()) ? null : start;
    },

    /**
     * "4h 12m" / "37m" for a count of seconds, in the reader's language.
     *
     * The units are translated, not just the sentence around them: an Arabic
     * board reading "9h 35m مدفوعة من 8 س" mixes two scripts in one clause, and
     * in a right-to-left line the Latin run can reorder against its own label.
     */
    liveOpsDuration(seconds) {
        const minutes = Math.max(0, Math.floor(Number(seconds) / 60));
        const hours = Math.floor(minutes / 60);
        if (hours <= 0) return I18n.__('liveOpsMinutesShort').replace('{minutes}', String(minutes));
        return I18n.__('liveOpsHoursShort')
            .replace('{hours}', String(hours))
            .replace('{minutes}', String(minutes % 60));
    },

    /** Just the HH:MM of a server timestamp, which is all a row needs to show. */
    liveOpsClockTime(timestamp) {
        const at = this.liveOpsStart(timestamp);
        if (!at) return '\u2014';
        const pad = (value) => String(value).padStart(2, '0');
        return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
    },

    /**
     * What one open shift means *right now*.
     *
     * ``state`` is the line the shift has crossed, and it is the whole point of
     * the board: ``on`` is a normal working day, ``over`` has passed the hour
     * the server alerts on at clock-out (overtime approval is coming), and
     * ``closing`` has reached the paid day the system will close it at.
     */
    liveOpsFacts(session, rules, now) {
        const policy = rules || {};
        const number = (key, fallback, { allowZero = false } = {}) => {
            const value = Number(policy[key]);
            if (!isFinite(value)) return fallback;
            return value > 0 || (allowZero && value >= 0) ? value : fallback;
        };
        const breakMinutes = number('break_minutes', 30, { allowZero: true });
        const breakAfter = number('break_after_hours', 4, { allowZero: true });
        const daySeconds = number('regular_hours', 8) * 3600;
        const overtimeSeconds = number('overtime_notify_hours', 8.1) * 3600;
        const autoCloses = String(policy.auto_close_at_regular === undefined ? 1 : policy.auto_close_at_regular) !== '0';

        const breakSeconds = breakMinutes * 60;
        // When the shift reaches the paid length of a day, in time on site - the
        // mirror of ``shift_hours.paid_limit_at``, break included only when the
        // limit is long enough to have contained one.
        const closingOffset = daySeconds / 3600 >= breakAfter ? daySeconds + breakSeconds : daySeconds;

        const late = !!(session && (session.late_flag === true || String(session.late_flag || '') === '1'));
        const start = this.liveOpsStart(session && session.clock_in_time);
        if (!start) {
            return {
                start: null, seconds: null, paidSeconds: null, percent: 0, closesAt: null,
                breakSeconds, daySeconds, overtimeSeconds, autoCloses, late, state: 'unknown'
            };
        }

        const seconds = Math.max(0, ((now === undefined ? Date.now() : now) - start.getTime()) / 1000);
        // The same rule the server deducts by, so the two never disagree.
        const taken = seconds >= breakAfter * 3600 ? Math.min(breakSeconds, seconds) : 0;
        const paidSeconds = Math.max(0, seconds - taken);
        const state = paidSeconds >= daySeconds && autoCloses
            ? 'closing'
            : paidSeconds >= overtimeSeconds ? 'over' : 'on';
        return {
            start, seconds, paidSeconds, daySeconds, overtimeSeconds, autoCloses, late, state,
            breakSeconds,
            closesAt: autoCloses ? new Date(start.getTime() + closingOffset * 1000) : null,
            percent: Math.min(100, Math.round((paidSeconds / daySeconds) * 100))
        };
    },

    liveOpsStateClass(state) {
        if (state === 'closing') return 'is-closing';
        if (state === 'over') return 'is-over';
        if (state === 'on') return 'is-on';
        return 'is-unknown';
    },

    liveOpsStateLabel(facts) {
        if (facts.state === 'closing') return I18n.__('liveOpsStateClosing');
        if (facts.state === 'over') return I18n.__('liveOpsStateOver');
        if (facts.state === 'on') return I18n.__('liveOpsStateOn');
        return I18n.__('liveOpsStateUnknown');
    },

    /**
     * The clock-in time and the late flag, on every element the ticker rewrites.
     *
     * The tick runs off the DOM, not off a captured closure: it reads the start
     * time from the element it is about to change. Anything carrying
     * ``data-fact`` therefore has to carry ``data-start`` too - an element that
     * ticks without one is recomputed from `undefined` and paints "clock-in
     * unreadable" over a perfectly healthy row a second after it renders.
     */
    liveOpsFactAttrs(facts) {
        const start = facts.start instanceof Date ? facts.start.toISOString() : '';
        return `data-start="${this.escapeHtml(start)}" data-late="${facts.late ? '1' : '0'}"`;
    },

    /** The verdict badges on a row: the hour state, then the arrival flag. */
    liveOpsBadgesHtml(facts) {
        const icon = facts.state === 'on' || facts.state === 'unknown' ? this.OPS_ICONS.clock : this.OPS_ICONS.alert;
        const badges = [
            `<span class="ops-badge ${this.liveOpsStateClass(facts.state)}" data-fact="state" data-state="${facts.state}" ${this.liveOpsFactAttrs(facts)}>${icon}${this.escapeHtml(this.liveOpsStateLabel(facts))}</span>`
        ];
        if (facts.late) {
            badges.push(`<span class="ops-badge is-late">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('liveOpsLate'))}</span>`);
        }
        return badges.join('');
    },

    liveOpsAvatarHtml(session) {
        const source = String((session && session.name) || (session && session.worker_id) || '?').trim();
        const initials = source.split(/\s+/).slice(0, 2).map((part) => part.slice(0, 1)).join('').toUpperCase() || '?';
        return `<span class="ops-avatar" aria-hidden="true">${this.escapeHtml(initials)}</span>`;
    },

    /**
     * Search + site + sort, applied to one read of the board.
     *
     * Filtering happens on the client because the list is already in hand and a
     * round trip per keystroke would be worse on the connection this console is
     * usually opened over.
     */
    liveOpsRows(data) {
        const query = String(this._liveOpsQuery || '').trim().toLowerCase();
        const site = this._liveOpsSite || '';
        const elapsed = (facts) => (facts.seconds === null ? -1 : facts.seconds);
        const rows = ((data && data.sessions) || []).map((session) => ({
            session, facts: this.liveOpsFacts(session, data && data.rules)
        })).filter(({ session }) => {
            if (site && String(session.site_name || '') !== site) return false;
            if (!query) return true;
            return [session.name, session.worker_id, session.site_name, session.role]
                .some((value) => String(value === null || value === undefined ? '' : value).toLowerCase().indexOf(query) >= 0);
        });
        const sort = this._liveOpsSort || 'longest';
        rows.sort((a, b) => {
            // Longest first is the default because the shift that needs a
            // decision is the one that has been open longest - a plain arrival
            // order buries exactly the row the operator came here to find.
            if (sort === 'newest') return elapsed(a.facts) - elapsed(b.facts);
            if (sort === 'name') return String(a.session.name || '').localeCompare(String(b.session.name || ''));
            return elapsed(b.facts) - elapsed(a.facts);
        });
        return rows;
    },

    liveOpsStats(data) {
        const rows = ((data && data.sessions) || []).map((session) => ({
            session, facts: this.liveOpsFacts(session, data && data.rules)
        }));
        const sites = new Set(rows.map(({ session }) => String(session.site_name || '')).filter(Boolean));
        let longest = null;
        for (const row of rows) {
            if (row.facts.seconds === null) continue;
            if (!longest || row.facts.seconds > longest.facts.seconds) longest = row;
        }
        return {
            onSite: rows.length,
            sites: sites.size,
            longest,
            late: rows.filter((row) => row.facts.late).length,
            over: rows.filter((row) => row.facts.state === 'over' || row.facts.state === 'closing').length
        };
    },

    liveOpsStatsHtml(data) {
        const stats = this.liveOpsStats(data);
        const longestValue = stats.longest ? this.liveOpsDuration(stats.longest.facts.seconds) : '\u2014';
        const longestHint = stats.longest
            ? `${stats.longest.session.name || stats.longest.session.worker_id} \u00b7 ${stats.longest.session.site_name || ''}`
            : I18n.__('liveOpsNobodyOnSite');
        return `
            <div class="ops-stat">
                <span class="ops-stat-label">${this.escapeHtml(I18n.__('liveOpsOnSiteNow'))}</span>
                <span class="ops-stat-value" data-stat="on-site">${stats.onSite}</span>
                <span class="ops-stat-hint">${this.escapeHtml(`${I18n.__('liveOpsSitesCovered')}: ${stats.sites}`)}</span>
            </div>
            <div class="ops-stat">
                <span class="ops-stat-label">${this.escapeHtml(I18n.__('liveOpsLongestShift'))}</span>
                <span class="ops-stat-value" id="liveOpsStatLongest" data-stat="longest">${this.escapeHtml(longestValue)}</span>
                <span class="ops-stat-hint">${this.escapeHtml(longestHint)}</span>
            </div>
            <div class="ops-stat${stats.late ? ' is-warn' : ''}">
                <span class="ops-stat-label">${this.escapeHtml(I18n.__('liveOpsLateArrivals'))}</span>
                <span class="ops-stat-value" data-stat="late">${stats.late}</span>
                <span class="ops-stat-hint">${this.escapeHtml(I18n.__('liveOpsLateHint'))}</span>
            </div>
            <div class="ops-stat${stats.over ? ' is-danger' : ''}">
                <span class="ops-stat-label">${this.escapeHtml(I18n.__('liveOpsOverDay'))}</span>
                <span class="ops-stat-value" id="liveOpsStatOver" data-stat="over">${stats.over}</span>
                <span class="ops-stat-hint">${this.escapeHtml(I18n.__('liveOpsOverDayHint'))}</span>
            </div>`;
    },

    /** The sentence the live region announces: counts, never a bare number. */
    liveOpsStatusSentence(data) {
        const stats = this.liveOpsStats(data);
        return I18n.__('liveOpsStatusLine')
            .replace('{workers}', String(stats.onSite))
            .replace('{sites}', String(stats.sites));
    },

    liveOpsStatusTime(data) {
        const at = new Date((data && data.at) || Date.now());
        const pad = (value) => String(value).padStart(2, '0');
        return I18n.__('liveOpsUpdated').replace('{time}', `${pad(at.getHours())}:${pad(at.getMinutes())}`);
    },

    liveOpsChipsHtml(data) {
        const sessions = (data && data.sessions) || [];
        const counts = new Map();
        for (const session of sessions) {
            const name = String(session.site_name || '');
            counts.set(name, (counts.get(name) || 0) + 1);
        }
        const chip = (value, label, count) => `
            <button type="button" class="ops-chip" data-site-chip="${this.escapeHtml(value)}"
                    aria-pressed="${this._liveOpsSite === value ? 'true' : 'false'}"
                    onclick="UI_MODULES.setLiveOpsSite('${this.liveOpsInlineString(value)}')">${this.escapeHtml(`${label} (${count})`)}</button>`;
        const all = chip('', I18n.__('liveOpsAllSites'), sessions.length);
        const sites = Array.from(counts.keys()).sort().map((name) => chip(name, name, counts.get(name)));
        return all + sites.join('');
    },

    liveOpsSortSelectHtml() {
        const sort = this._liveOpsSort || 'longest';
        const option = (value, key) => `<option value="${value}"${sort === value ? ' selected' : ''}>${this.escapeHtml(I18n.__(key))}</option>`;
        return `<label class="sr-only" for="liveOpsSort">${this.escapeHtml(I18n.__('liveOpsSort'))}</label>
            <select class="ops-sort" id="liveOpsSort" data-sort-select onchange="UI_MODULES.setLiveOpsSort(this.value)">
                ${option('longest', 'liveOpsSortLongest')}${option('newest', 'liveOpsSortNewest')}${option('name', 'liveOpsSortName')}
            </select>`;
    },

    liveOpsSortButtonHtml(label, key) {
        const sort = this._liveOpsSort || 'longest';
        const active = (key === 'name' && sort === 'name') || (key === 'elapsed' && sort !== 'name');
        const arrow = active ? (sort === 'newest' ? this.OPS_ICONS.chevronUp : this.OPS_ICONS.chevronDown) : '';
        return `<button type="button" class="ops-sort-btn" data-sort-btn="${key}" onclick="UI_MODULES.setLiveOpsSort('${key}')">${this.escapeHtml(label)}${arrow}</button>`;
    },

    /**
     * A value safe inside a single-quoted JS literal in a double-quoted attribute.
     *
     * JS-escaped first, then HTML-escaped: the entity is decoded by the parser
     * *after* the attribute boundary is settled, so a quote can neither end the
     * attribute nor the string. Names and site names are operator-entered text.
     */
    liveOpsInlineString(value) {
        return this.escapeHtml(String(value === null || value === undefined ? '' : value)
            .replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
    },

    liveOpsProgressHtml(facts) {
        const cls = facts.state === 'closing' ? ' is-closing' : facts.state === 'over' ? ' is-over' : '';
        const attrs = this.liveOpsFactAttrs(facts);
        return `<span class="ops-progress${cls}" aria-hidden="true"><span data-fact="bar" ${attrs} style="width:${facts.percent}%"></span></span>`;
    },

    liveOpsPaidLineHtml(facts) {
        if (facts.paidSeconds === null) return '';
        const paid = I18n.__('liveOpsPaidOf')
            .replace('{paid}', this.liveOpsDuration(facts.paidSeconds))
            .replace('{day}', this.hoursLabel(facts.daySeconds / 3600));
        if (facts.state !== 'closing' || !facts.closesAt) return this.escapeHtml(paid);
        // The moment the system will close this shift is the one number an
        // operator needs before deciding to leave it alone.
        const pad = (value) => String(value).padStart(2, '0');
        const at = `${pad(facts.closesAt.getHours())}:${pad(facts.closesAt.getMinutes())}`;
        return this.escapeHtml(`${paid} \u00b7 ${I18n.__('liveOpsClosingAt').replace('{time}', at)}`);
    },

    liveOpsRowHtml(session, facts) {
        const elapsed = facts.seconds === null ? '\u2014' : this.liveOpsDuration(facts.seconds);
        const start = this.escapeHtml(String(session.clock_in_time || ''));
        return `
            <tr class="ops-row ${this.liveOpsStateClass(facts.state)}"
                data-session="${this.escapeHtml(session.worker_id)}">
                <td>
                    <div class="ops-row-main">
                        ${this.liveOpsAvatarHtml(session)}
                        <div class="ops-who">
                            <span class="ops-name">${this.escapeHtml(session.name || session.worker_id)}</span>
                            <span class="ops-sub">${this.escapeHtml(`${session.role || ''} \u00b7 ${session.worker_id}`)}</span>
                        </div>
                    </div>
                </td>
                <td>${this.escapeHtml(session.site_name || '\u2014')}</td>
                <td><span class="ops-figure">${this.escapeHtml(this.liveOpsClockTime(session.clock_in_time))}</span></td>
                <td>
                    <span class="ops-elapsed" data-fact="elapsed" ${this.liveOpsFactAttrs(facts)}>${this.escapeHtml(elapsed)}</span>
                    <span class="ops-since">${this.liveOpsPaidLineHtml(facts)}</span>
                    ${this.liveOpsProgressHtml(facts)}
                </td>
                <td><div class="ops-badges">${this.liveOpsBadgesHtml(facts)}</div></td>
                <td class="is-end">
                    <button type="button" class="ops-btn ops-btn-danger" data-force-out="${this.escapeHtml(session.worker_id)}"
                            onclick="UI.forceAction('${this.liveOpsInlineString(session.worker_id)}', 'out', '${this.liveOpsInlineString(session.site_name)}')">${this.escapeHtml(I18n.__('forceOut'))}</button>
                </td>
            </tr>`;
    },

    liveOpsCardHtml(session, facts) {
        const elapsed = facts.seconds === null ? '\u2014' : this.liveOpsDuration(facts.seconds);
        return `
            <article class="ops-card ${this.liveOpsStateClass(facts.state)}" data-session="${this.escapeHtml(session.worker_id)}">
                <div class="ops-card-top">
                    ${this.liveOpsAvatarHtml(session)}
                    <div class="ops-who">
                        <span class="ops-name">${this.escapeHtml(session.name || session.worker_id)}</span>
                        <span class="ops-sub">${this.escapeHtml(`${session.site_name || ''} \u00b7 ${this.liveOpsClockTime(session.clock_in_time)}`)}</span>
                    </div>
                </div>
                <div class="ops-card-facts">
                    <div>
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('liveOpsOnSiteFor'))}</span>
                        <span class="ops-elapsed" data-fact="elapsed" ${this.liveOpsFactAttrs(facts)}>${this.escapeHtml(elapsed)}</span>
                    </div>
                    <div class="ops-badges">${this.liveOpsBadgesHtml(facts)}</div>
                </div>
                ${this.liveOpsProgressHtml(facts)}
                <p class="ops-note">${this.liveOpsPaidLineHtml(facts)}</p>
                <div class="ops-card-foot">
                    <button type="button" class="ops-btn ops-btn-danger" data-force-out="${this.escapeHtml(session.worker_id)}"
                            onclick="UI.forceAction('${this.liveOpsInlineString(session.worker_id)}', 'out', '${this.liveOpsInlineString(session.site_name)}')">${this.escapeHtml(I18n.__('forceOut'))}</button>
                </div>
            </article>`;
    },

    liveOpsTableHtml(rows) {
        const sort = this._liveOpsSort || 'longest';
        const ariaSort = sort === 'name' ? 'none' : sort === 'newest' ? 'ascending' : 'descending';
        return `
            <div class="ui-table-wrap">
                <table class="ops-table" data-live-ops-table>
                    <caption class="sr-only">${this.escapeHtml(I18n.__('activeShifts'))}</caption>
                    <thead>
                        <tr>
                            <th scope="col">${this.liveOpsSortButtonHtml(I18n.__('name'), 'name')}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('site'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('liveOpsClockIn'))}</th>
                            <th scope="col" aria-sort="${ariaSort}">${this.liveOpsSortButtonHtml(I18n.__('liveOpsOnSiteFor'), 'elapsed')}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('liveOpsState'))}</th>
                            <th scope="col"><span class="sr-only">${this.escapeHtml(I18n.__('liveOpsAction'))}</span></th>
                        </tr>
                    </thead>
                    <tbody>${rows.map(({ session, facts }) => this.liveOpsRowHtml(session, facts)).join('')}</tbody>
                </table>
            </div>`;
    },

    liveOpsCardsHtml(rows) {
        return `<div class="ops-cards">${rows.map(({ session, facts }) => this.liveOpsCardHtml(session, facts)).join('')}</div>`;
    },

    liveOpsEmptyHtml(total) {
        const filtered = total > 0;
        const action = filtered
            ? `<button type="button" class="ops-btn" data-clear-filters onclick="UI_MODULES.clearLiveOpsFilters()">${this.escapeHtml(I18n.__('liveOpsClearFilters'))}</button>`
            : `<button type="button" class="ops-btn ops-btn-primary" data-force-in-cta onclick="UI_MODULES.openForceIn()">${this.OPS_ICONS.person}${this.escapeHtml(I18n.__('liveOpsForceCta'))}</button>`;
        return `
            <div class="ops-empty" data-empty="${filtered ? 'filtered' : 'nobody'}">
                <span class="ops-empty-icon">${filtered ? this.OPS_ICONS.search : this.OPS_ICONS.person}</span>
                <p class="ops-empty-title">${this.escapeHtml(filtered ? I18n.__('liveOpsNoMatches') : I18n.__('noActiveShifts'))}</p>
                <p class="ops-empty-body">${this.escapeHtml(filtered ? I18n.__('liveOpsNoMatchesHint') : I18n.__('liveOpsNobodyHint'))}</p>
                ${action}
            </div>`;
    },

    liveOpsBoardHtml(data) {
        const rows = this.liveOpsRows(data);
        const total = ((data && data.sessions) || []).length;
        if (rows.length === 0) return this.liveOpsEmptyHtml(total);
        return Device.isMobile ? this.liveOpsCardsHtml(rows) : this.liveOpsTableHtml(rows);
    },

    liveOpsFilterNoteHtml(data) {
        const total = ((data && data.sessions) || []).length;
        const shown = this.liveOpsRows(data).length;
        return shown === total
            ? I18n.__('liveOpsOpenCount').replace('{total}', String(total))
            : I18n.__('liveOpsShowing').replace('{shown}', String(shown)).replace('{total}', String(total));
    },

    liveOpsHtml(data) {
        return `
            <!-- No heading here: the tab title and its one-line description are painted by
                 the console shell above this pane, so the board would be repeating the
                 words it is already framed by. What stays is the part the shell cannot
                 know - whether the live dot is breathing, and when the board last read
                 the server. The section keeps a name of its own for a screen reader
                 arriving at it out of order. -->
            <section class="ops-board" data-live-ops="board" aria-label="${this.escapeHtml(I18n.__('activeShifts'))}">
                <header class="ops-head">
                    <div>
                        <div class="ops-status">
                            <span class="ops-live-dot" aria-hidden="true"></span>
                            <span id="liveOpsStatus" role="status" aria-atomic="true">${this.escapeHtml(this.liveOpsStatusSentence(data))}</span>
                            <span class="ops-status-time" id="liveOpsStatusTime">${this.escapeHtml(this.liveOpsStatusTime(data))}</span>
                        </div>
                    </div>
                    <button type="button" class="ops-btn" data-refresh onclick="UI_MODULES.refreshLiveOps()">${this.OPS_ICONS.refresh}<span>${this.escapeHtml(I18n.__('liveOpsRefresh'))}</span></button>
                </header>
                <div class="ops-stats" id="liveOpsStats">${this.liveOpsStatsHtml(data)}</div>
                <div class="ops-toolbar">
                    <label class="ops-search">
                        <span class="sr-only">${this.escapeHtml(I18n.__('liveOpsSearchLabel'))}</span>
                        ${this.OPS_ICONS.search}
                        <input type="search" id="liveOpsQuery" data-search value="${this.escapeHtml(this._liveOpsQuery || '')}"
                               placeholder="${this.escapeHtml(I18n.__('liveOpsSearchPlaceholder'))}"
                               oninput="UI_MODULES.setLiveOpsQuery(this.value)" />
                    </label>
                    <div class="ops-chips" role="group" aria-label="${this.escapeHtml(I18n.__('liveOpsSiteFilter'))}">${this.liveOpsChipsHtml(data)}</div>
                    ${Device.isMobile ? this.liveOpsSortSelectHtml() : ''}
                </div>
                <details class="ops-panel" id="liveOpsForceIn"${this._forceInOpen ? ' open' : ''} ontoggle="UI_MODULES.liveOpsPanelToggled(this)">
                    <summary>${this.OPS_ICONS.person}<span>${this.escapeHtml(I18n.__('forceInTitle'))}</span></summary>
                    ${UI.forceInPanelHtml(data.sessions || [], data.users || [], data.sites || [])}
                </details>
                <p class="ops-note" id="liveOpsFilterNote">${this.escapeHtml(this.liveOpsFilterNoteHtml(data))}</p>
                <div id="liveOpsBoard">${this.liveOpsBoardHtml(data)}</div>
            </section>`;
    },

    liveOpsSkeletonHtml() {
        const bars = [90, 70, 55].map((width, index) => `
            <div class="ops-skeleton-row">
                <span class="ops-skeleton-bar" style="width:34px;height:34px;border-radius:999px"></span>
                <span class="ops-skeleton-bar" style="width:${width}%;animation-delay:${index * 0.12}s"></span>
            </div>`).join('');
        // The wait is announced once, and the layout does not jump when the real
        // board arrives because the skeleton occupies the same block.
        return `
            <section class="ops-board" aria-busy="true">
                <p class="ops-note" role="status">${this.escapeHtml(I18n.__('liveOpsSkeleton'))}</p>
                <div class="ops-skeleton">${bars}</div>
            </section>`;
    },

    liveOpsErrorHtml(err) {
        return `
            <div class="ops-error" role="alert">
                <span>${this.escapeHtml(I18n.__('liveOpsError'))}</span>
                <span class="ops-sub">${this.escapeHtml((err && err.message) || '')}</span>
                <button type="button" class="ops-btn" onclick="UI.renderAdminTab('Live Ops')">${this.OPS_ICONS.refresh}<span>${this.escapeHtml(I18n.__('liveOpsRetry'))}</span></button>
            </div>`;
    },

    /** The four reads the board needs, and the rules that say when a day ends. */
    async fetchLiveOps() {
        const [sessions, users, sites, rules] = await Promise.all([
            API.request('/admin/active_sessions'),
            API.request('/admin/users'),
            API.request('/admin/sites'),
            // A board without the rules still works (documented defaults), so a
            // failure here must not take the screen with it.
            API.request('/admin/shift_rules').catch(() => ({}))
        ]);
        const list = (value) => (Array.isArray(value) ? value : []);
        return {
            sessions: list(sessions),
            users: list(users),
            sites: list(sites),
            rules: rules && typeof rules === 'object' && !Array.isArray(rules) ? rules : {},
            at: Date.now()
        };
    },

    async renderLiveOps(content) {
        if (!content) return;
        this.stopLiveOps();
        const run = ++this._liveOpsRun;
        content.innerHTML = this.liveOpsSkeletonHtml();
        let data;
        try {
            data = await this.fetchLiveOps();
        } catch (err) {
            if (this.liveOpsRenderIsStale(run)) return;   // a newer render owns the screen
            content.innerHTML = this.liveOpsErrorHtml(err);
            return;
        }
        if (this.liveOpsRenderIsStale(run)) return;
        this._liveOps = data;
        content.innerHTML = this.liveOpsHtml(data);
        this.startLiveOps();
    },

    /** The 1 s tick (numerals only) and the slow poll (one request). */
    startLiveOps() {
        this.stopLiveOps();
        this._liveOpsTick = setInterval(() => this.tickLiveOps(), 1000);
        this._liveOpsPoll = setInterval(() => this.pollLiveOps(), this.LIVE_OPS_POLL_MS);
    },

    /**
     * Whether the board this call started is still the one that should be on screen.
     *
     * Two things end a board's turn, and only the first one was ever checked. A newer
     * render (``_liveOpsRun``) is the obvious one. The other is the reader leaving the
     * tab: ``stopLiveOps`` silences the timers, but the four reads were already in
     * flight, and a board painted onto the Shifts tab is not a stale number - it is the
     * wrong screen, with the report the reader asked for gone from under it.
     */
    liveOpsRenderIsStale(run) {
        return run !== this._liveOpsRun || State.adminTab !== 'Live Ops';
    },

    stopLiveOps() {
        if (this._liveOpsTick !== null) { clearInterval(this._liveOpsTick); this._liveOpsTick = null; }
        if (this._liveOpsPoll !== null) { clearInterval(this._liveOpsPoll); this._liveOpsPoll = null; }
    },

    /**
     * Recompute the numbers from the clock. No request, and no element is
     * replaced - only text and class names - so a focused control keeps focus
     * and a reading eye is never moved off the row it is on.
     */
    tickLiveOps() {
        const data = this._liveOps;
        if (!data) return;
        const rules = data.rules;
        const stateClasses = ['is-on', 'is-over', 'is-closing', 'is-unknown'];
        document.querySelectorAll('[data-fact]').forEach((el) => {
            // Read the start off the element itself: the tick must survive a
            // repaint of the pane it is looking at.
            const facts = this.liveOpsFacts({ clock_in_time: el.dataset.start, late_flag: el.dataset.late }, rules);
            if (el.dataset.fact === 'elapsed') {
                el.textContent = facts.seconds === null ? '\u2014' : this.liveOpsDuration(facts.seconds);
            } else if (el.dataset.fact === 'bar') {
                el.style.width = `${facts.percent}%`;
                if (el.parentElement) {
                    el.parentElement.classList.remove(...stateClasses);
                    el.parentElement.classList.add(this.liveOpsStateClass(facts.state));
                }
            } else if (el.dataset.fact === 'state' && el.dataset.state !== facts.state) {
                // The badge is repainted in place: its label, its icon and its tone.
                el.className = `ops-badge ${this.liveOpsStateClass(facts.state)}`;
                el.dataset.state = facts.state;
                el.innerHTML = `${facts.state === 'on' || facts.state === 'unknown' ? this.OPS_ICONS.clock : this.OPS_ICONS.alert}${this.escapeHtml(this.liveOpsStateLabel(facts))}`;
                // ...and so is the accent that repeats it on the row itself.
                const row = typeof el.closest === 'function' ? el.closest('[data-session]') : null;
                if (row) {
                    row.classList.remove(...stateClasses);
                    row.classList.add(this.liveOpsStateClass(facts.state));
                }
            }
        });

        // The headline figures move too - a shift crossing the line changes what
        // the longest row and the "past the paid day" count mean.
        const stats = this.liveOpsStats(data);
        const longest = document.getElementById('liveOpsStatLongest');
        if (longest) longest.textContent = stats.longest ? this.liveOpsDuration(stats.longest.facts.seconds) : '\u2014';
        const over = document.getElementById('liveOpsStatOver');
        if (over) over.textContent = String(stats.over);
    },

    /**
     * Re-read the session list and repaint only if it changed.
     *
     * A hidden tab makes no requests, and a change that does not touch the
     * layout (a new name on the same clock-in) is still worth repainting - the
     * signature covers identity, site, start time and the late flag.
     */
    async pollLiveOps() {
        if (typeof document.visibilityState === 'string' && document.visibilityState === 'hidden') return;
        if (!this._liveOps || State.adminTab !== 'Live Ops') { this.stopLiveOps(); return; }
        let sessions;
        try {
            sessions = await API.request('/admin/active_sessions');
        } catch (err) {
            return;   // a blip keeps the board up, with its last known-good rows
        }
        if (!this._liveOps || State.adminTab !== 'Live Ops') return;
        const fresh = { ...this._liveOps, sessions: Array.isArray(sessions) ? sessions : [], at: Date.now() };
        if (this.liveOpsSignature(fresh) === this.liveOpsSignature(this._liveOps)) return;
        this._liveOps = fresh;
        this.paintLiveOps(fresh);
    },

    liveOpsSignature(data) {
        return ((data && data.sessions) || [])
            .map((session) => [session.worker_id, session.name, session.site_name, session.clock_in_time, session.late_flag].join('\u0001'))
            .sort().join('\u0002');
    },

    /** Repaint the parts that a data change touches, and nothing else. */
    paintLiveOps(data) {
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(data);
        const stats = document.getElementById('liveOpsStats');
        if (stats) stats.innerHTML = this.liveOpsStatsHtml(data);
        const status = document.getElementById('liveOpsStatus');
        if (status) status.textContent = this.liveOpsStatusSentence(data);
        const stamp = document.getElementById('liveOpsStatusTime');
        if (stamp) stamp.textContent = this.liveOpsStatusTime(data);
        const note = document.getElementById('liveOpsFilterNote');
        if (note) note.textContent = this.liveOpsFilterNoteHtml(data);
    },

    /** The Refresh button: a full re-read, but the toolbar stays put. */
    async refreshLiveOps() {
        try {
            const fresh = await this.fetchLiveOps();
            this._liveOps = fresh;
            this.paintLiveOps(fresh);
            const panel = document.getElementById('liveOpsForceIn');
            // Never redraw a panel somebody is part-way through filling in.
            if (panel && !this._forceInOpen) panel.innerHTML = UI.forceInPanelHtml(fresh.sessions, fresh.users, fresh.sites);
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('liveOpsError'));
        }
    },

    setLiveOpsQuery(value) {
        this._liveOpsQuery = String(value || '');
        State.liveOpsQuery = this._liveOpsQuery;
        if (!this._liveOps) return;
        // Only the board is repainted, so the caret stays in the search box.
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(this._liveOps);
        const note = document.getElementById('liveOpsFilterNote');
        if (note) note.textContent = this.liveOpsFilterNoteHtml(this._liveOps);
    },

    setLiveOpsSite(site) {
        this._liveOpsSite = String(site || '');
        State.liveOpsSite = this._liveOpsSite;
        if (!this._liveOps) return;
        document.querySelectorAll('[data-site-chip]').forEach((chip) => {
            chip.setAttribute('aria-pressed', chip.dataset.siteChip === this._liveOpsSite ? 'true' : 'false');
        });
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(this._liveOps);
        const note = document.getElementById('liveOpsFilterNote');
        if (note) note.textContent = this.liveOpsFilterNoteHtml(this._liveOps);
    },

    setLiveOpsSort(key) {
        // The two headers and the phone select are the same control, so they all
        // move to the state this call settles on. Re-tapping the column that is
        // already sorted flips the direction; tapping it from another column
        // starts at that column's natural order (longest on site).
        const current = this._liveOpsSort || 'longest';
        const onElapsed = current === 'longest' || current === 'newest';
        this._liveOpsSort = key === 'elapsed'
            ? (onElapsed ? (current === 'longest' ? 'newest' : 'longest') : 'longest')
            : (key || 'longest');
        State.liveOpsSort = this._liveOpsSort;
        if (!this._liveOps) return;
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(this._liveOps);
        const select = document.getElementById('liveOpsSort');
        if (select) select.value = this._liveOpsSort;
    },

    clearLiveOpsFilters() {
        this._liveOpsQuery = '';
        this._liveOpsSite = '';
        State.liveOpsQuery = '';
        State.liveOpsSite = '';
        if (!this._liveOps) return;
        const search = document.getElementById('liveOpsQuery');
        if (search) search.value = '';
        document.querySelectorAll('[data-site-chip]').forEach((chip) => chip.setAttribute('aria-pressed', chip.dataset.siteChip === '' ? 'true' : 'false'));
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(this._liveOps);
        const note = document.getElementById('liveOpsFilterNote');
        if (note) note.textContent = this.liveOpsFilterNoteHtml(this._liveOps);
    },

    liveOpsPanelToggled(details) {
        this._forceInOpen = !!(details && details.open);
    },

    /** The empty board's one action: open the panel and put the caret in it. */
    openForceIn() {
        this._forceInOpen = true;
        const panel = document.getElementById('liveOpsForceIn');
        if (panel) panel.open = true;
        const worker = document.getElementById('forceInWorker');
        if (worker) worker.focus();
    },

    // -----------------------------------------------------------------
    //  Approvals - the clock-ins that need a person to decide
    // -----------------------------------------------------------------
    //  These are the punches the app could not confirm on its own: the fix was
    //  outside every site radius, or the face check did not match. The screen used
    //  to be a stack of unlabelled paragraphs - "Clock In: 2026-09-16 07:58:00" -
    //  with a blank box and two buttons under each one, which answered none of the
    //  questions an admin actually has: how long has this been waiting, which one
    //  is the urgent one, and what each button does to the worker's pay.
    //
    //  So a review is a card that answers them in order. Who and where at the top,
    //  how long it has been waiting as the one loud figure on it - because waiting
    //  is what the decision costs - and the two actions at the bottom, with the note
    //  labelled as the audit record it is. The list runs oldest first: a queue is
    //  worked from the front, and the oldest review is the one being phoned about.

    /** Seconds a review has been waiting, or null when its timestamp is unreadable. */
    approvalsWaitingSeconds(log) {
        const start = this.liveOpsStart(log && log.timestamp);
        return start ? Math.max(0, (Date.now() - start.getTime()) / 1000) : null;
    },

    approvalsCardHtml(log) {
        const id = this.escapeHtml(log.id);
        const seconds = this.approvalsWaitingSeconds(log);
        // An hour is where a review stops being paperwork and starts being somebody
        // who cannot be paid this month, so it changes colour at that line.
        const danger = seconds !== null && seconds >= 3600;
        const waiting = seconds === null ? '\u2014' : this.liveOpsDuration(seconds);
        const who = this.escapeHtml(log.name || log.worker_id || '');
        const sub = this.escapeHtml([log.role ? this.roleLabel(log.role) : '', log.worker_id]
            .filter(Boolean).join(' \u00b7 '));
        // A number stays a number and a string stays a quoted string: ``/admin/approve_review``
        // takes an int, and a coerced "7" is a 422 waiting to happen.
        const idArg = typeof log.id === 'number' ? String(log.id) : `'${this.liveOpsInlineString(log.id)}'`;
        return `
            <article class="ui-card${danger ? ' is-danger' : ' is-warn'}" data-review="${id}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        ${this.liveOpsAvatarHtml(log)}
                        <div class="ops-who">
                            <span class="ops-name">${who}</span>
                            <span class="ops-sub">${sub}</span>
                        </div>
                    </div>
                    <span class="ui-badge${danger ? ' is-danger' : ' is-warn'}">${this.OPS_ICONS.alert}${this.escapeHtml(`${I18n.__('approvalsWaiting')} ${waiting}`)}</span>
                </div>
                <div class="ui-facts" style="margin-top:14px">
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('approvalsSite'))}</span>
                        <span class="ui-fact-value">${this.escapeHtml(log.site_name || '\u2014')}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('approvalsDate'))}</span>
                        <span class="ui-fact-value">${this.escapeHtml(String(log.timestamp || '').slice(0, 10) || '\u2014')}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('approvalsClockIn'))}</span>
                        <span class="ui-fact-value">${this.escapeHtml(this.liveOpsClockTime(log.timestamp))}</span>
                    </div>
                </div>
                <label class="ui-label" for="note-${id}" style="margin-top:16px">${this.escapeHtml(I18n.__('approvalsNote'))}</label>
                <textarea id="note-${id}" class="ui-field" rows="2"
                          placeholder="${this.escapeHtml(I18n.__('approvalsNotePlaceholder'))}"></textarea>
                <p class="ui-section-note" style="margin-top:6px">${this.escapeHtml(I18n.__('approvalsNoteUsed'))}</p>
                <div class="ui-row" style="margin-top:12px">
                    <button type="button" class="ui-btn ui-btn-primary" data-approve="${id}"
                            onclick="UI_MODULES.handleApproval(${idArg}, 'approve')">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('approvalsApprove'))}</button>
                    <button type="button" class="ui-btn ui-btn-danger" data-reject="${id}"
                            onclick="UI_MODULES.handleApproval(${idArg}, 'reject')">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('approvalsReject'))}</button>
                </div>
            </article>`;
    },

    approvalsHtml(logs) {
        if (logs.length === 0) {
            // The empty state says what empty *means* here - nothing is broken, everything
            // confirmed itself - and offers the one action that fills the queue.
            return `
                <div class="ui-empty" data-approvals-empty="true">
                    <span class="ui-empty-icon">${this.OPS_ICONS.check}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('approvalsEmpty'))}</p>
                    <p class="ui-empty-body">${this.escapeHtml(I18n.__('approvalsEmptyHint'))}</p>
                    <button type="button" class="ui-btn" onclick="UI.renderAdminTab('Live Ops')">${this.OPS_ICONS.clock}${this.escapeHtml(I18n.__('activeShifts'))}</button>
                </div>`;
        }
        const ordered = logs.slice().sort((a, b) =>
            (this.approvalsWaitingSeconds(b) || 0) - (this.approvalsWaitingSeconds(a) || 0));
        return `
            <p class="ui-section-note" data-approvals-count>${this.escapeHtml(I18n.__('approvalsCount').replace('{count}', String(ordered.length)))}</p>
            <div class="ui-stack">${ordered.map((log) => this.approvalsCardHtml(log)).join('')}</div>`;
    },

    async renderApprovals(content) {
        if (!content) return;
        content.innerHTML = UI.consoleSkeletonHtml(I18n.__('approvalsTitle'));
        let logs;
        try {
            logs = await API.request('/admin/pending_reviews');
        } catch (err) {
            // A failed read used to leave the previous screen up, or throw. It says what
            // went wrong now, and offers the one control that can fix it.
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Approvals')");
            return;
        }
        content.innerHTML = `<div class="ui-page" data-approvals="true">${this.approvalsHtml(Array.isArray(logs) ? logs : [])}</div>`;
    },

    async handleApproval(logId, action) {
        // Read defensively: a second click - or a second admin on the same review - lands
        // after the list has re-rendered, when this row's note box is already detached.
        const field = document.getElementById(`note-${logId}`);
        const note = field ? field.value : '';
        const card = typeof document.querySelector === 'function' ? document.querySelector(`[data-review="${logId}"]`) : null;
        const buttons = card && card.querySelectorAll ? Array.from(card.querySelectorAll('button')) : [];
        // The decision is a round trip on a phone tether. Both buttons go down while it is
        // in flight, so the card cannot take a second answer to the same question.
        buttons.forEach((button) => { button.disabled = true; });
        try {
            await API.request('/admin/approve_review', { method: 'POST', body: { log_id: logId, admin_id: State.user.id, note } });
        } catch (err) {
            buttons.forEach((button) => { button.disabled = false; });
            Toast.error(err.message);
            return;
        }
        // Was an ``alert()``: a modal that stops the browser, cannot be read by the toast
        // queue, and announces nothing to a screen reader that was not already looking.
        Toast.success(I18n.__('approvalsDecided'));
        UI.renderAdminTab('Approvals');
    },

    // -----------------------------------------------------------------
    //  Sites - the places a clock-in is allowed from
    // -----------------------------------------------------------------
    //: The name of the site whose editor is open, or ``null``. The card renders its facts or
    //: its editor from this, so opening one is a repaint and not a second request.
    _siteEdit: null,
    //: The last list the API returned, for repaints that must not refetch (opening the editor).
    _sites: null,
    _sitesContent: null,

    //  A site is a point and a radius, and the tab showed one of them: a row reading
    //  "Radius: 65m" with a red Delete link, and an add form whose entire instruction
    //  was the placeholder "Lat,Lon". Neither survives contact with a real site - an
    //  admin who cannot see the coordinates cannot tell a correct site from a typo,
    //  and "Lat,Lon" is not a format most people can produce on a phone. They can
    //  long-press a spot in a maps app and copy two numbers, which is what the form
    //  now asks for and what the card now shows back.

    sitesCardHtml(site) {
        const name = String(site.site_name || '');
        const lat = Number(site.lat);
        const lon = Number(site.lon);
        const coords = Number.isFinite(lat) && Number.isFinite(lon)
            ? `${lat.toFixed(5)}, ${lon.toFixed(5)}`
            : '\u2014';
        const facts = this._siteEdit === name
            ? this.sitesEditHtml(site)
            : `
                <div class="ui-facts" style="margin-top:14px">
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('sitesRadius'))}</span>
                        <span class="ui-fact-value">${this.escapeHtml(`${site.radius} m`)}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('sitesWindow'))}</span>
                        <span class="ui-fact-value" data-site-window="${this.escapeHtml(name)}">${this.escapeHtml(this.windowLabel(site))}</span>
                        <span class="ops-sub">${this.escapeHtml(this.windowOriginLabel(site, ['clock_in_window_start', 'clock_in_window_end']))}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('sitesWindowTimezone'))}</span>
                        <span class="ui-fact-value">${this.escapeHtml(String((site.window || {}).site_timezone || ''))}</span>
                        <span class="ops-sub">${this.escapeHtml(this.windowOriginLabel(site, ['site_timezone']))}</span>
                    </div>
                </div>`;
        return `
            <li class="ui-card" data-site="${this.escapeHtml(name)}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        <span class="ops-avatar" aria-hidden="true">${this.OPS_ICONS.pin}</span>
                        <div class="ops-who">
                            <span class="ops-name">${this.escapeHtml(name)}</span>
                            <span class="ops-sub">${this.escapeHtml(coords)}</span>
                        </div>
                    </div>
                    <div class="ui-row">
                        <button type="button" class="ui-btn ui-btn-sm" data-edit-site="${this.escapeHtml(name)}">${this.OPS_ICONS.clock}${this.escapeHtml(I18n.__('sitesEdit'))}</button>
                        <button type="button" class="ui-btn ui-btn-danger ui-btn-sm" data-delete-site="${this.escapeHtml(name)}"
                                onclick="UI_MODULES.deleteSite('${this.liveOpsInlineString(name)}')">${this.OPS_ICONS.trash}${this.escapeHtml(I18n.__('sitesDelete'))}</button>
                    </div>
                </div>
                ${facts}
            </li>`;
    },

    /**
     * ``04:00-06:30``, with ``(overnight)`` spelled in the reader's language.
     *
     * Built here from the structured fields rather than using ``window.window`` from the API,
     * which is an English sentence ("22:00-06:00 (overnight)") - fine for a log, wrong for a
     * screen an Arabic-speaking administrator reads.
     */
    windowLabel(site) {
        const info = (site && site.window) || {};
        const start = String(info.clock_in_window_start || '');
        const end = String(info.clock_in_window_end || '');
        if (!start || !end) return '\u2014';
        return `${start}-${end}` + (info.crosses_midnight ? ` (${I18n.__('sitesWindowOvernight')})` : '');
    },

    /**
     * Where the hours (or the zone) in force came from: this site, or the company window.
     *
     * Per *field* on purpose. A site can set only its hours and keep the company timezone (or
     * the other way round), so one "has this site configured a window?" answer would be a lie
     * about one of the two rows on screen.
     */
    windowOriginLabel(site, keys) {
        const source = ((site && site.window) || {}).source || {};
        const fromSite = keys.some((key) => source[key] === 'site');
        return fromSite ? I18n.__('sitesWindowFromSite') : I18n.__('sitesWindowFromCompany');
    },

    /**
     * The window editor, opened in place of a site's facts.
     *
     * The time boxes load the site's *configured* columns, never the window that is in force -
     * blank on the two sites out of three that have none of their own. Pre-filling them with the
     * resolved company window would quietly convert an inheriting site into an overriding one:
     * from that save on, moving the company hours would no longer move this site, and nobody
     * would have asked for that.
     */
    sitesEditHtml(site) {
        const raw = (key) => {
            const value = site ? site[key] : null;
            return value === null || value === undefined ? '' : String(value);
        };
        const lat = Number(site.lat);
        const lon = Number(site.lon);
        const coords = Number.isFinite(lat) && Number.isFinite(lon) ? `${lat},${lon}` : '';
        const inherits = !raw('clock_in_window_start') && !raw('clock_in_window_end') && !raw('site_timezone');
        return `
            <form id="editSiteForm" class="ui-grid three" style="margin-top:14px">
                <div class="ui-grid three" style="grid-column:1/-1;gap:12px">
                    <div>
                        <label class="ui-label" for="editSiteLocation">${this.escapeHtml(I18n.__('sitesLocation'))}</label>
                        <input type="text" id="editSiteLocation" class="ui-field" inputmode="decimal" required
                               value="${this.escapeHtml(coords)}">
                    </div>
                    <div>
                        <label class="ui-label" for="editSiteRadius">${this.escapeHtml(I18n.__('sitesRadius'))}</label>
                        <input type="number" id="editSiteRadius" class="ui-field" min="1" step="1" required
                               value="${this.escapeHtml(String(site.radius))}">
                    </div>
                    <div>
                        <label class="ui-label" for="editSiteTimezone">${this.escapeHtml(I18n.__('sitesWindowTimezone'))}</label>
                        <input type="text" id="editSiteTimezone" class="ui-field" list="siteTimezoneOptions"
                               placeholder="${this.escapeHtml(I18n.__('sitesWindowTimezonePlaceholder'))}"
                               value="${this.escapeHtml(raw('site_timezone'))}">
                    </div>
                </div>
                <div style="grid-column:1/-1">
                    <p class="ui-section-note" id="editSiteWindowNotice">${this.escapeHtml(
                        inherits
                            ? I18n.__('sitesWindowNoticeCompany').replace('{window}', this.windowLabel(site))
                            : I18n.__('sitesWindowNoticeSite').replace('{window}', this.windowLabel(site))
                    )}</p>
                </div>
                <div>
                    <label class="ui-label" for="editSiteWindowStart">${this.escapeHtml(I18n.__('sitesWindowStart'))}</label>
                    <input type="time" id="editSiteWindowStart" class="ui-field" value="${this.escapeHtml(raw('clock_in_window_start'))}">
                </div>
                <div>
                    <label class="ui-label" for="editSiteWindowEnd">${this.escapeHtml(I18n.__('sitesWindowEnd'))}</label>
                    <input type="time" id="editSiteWindowEnd" class="ui-field" value="${this.escapeHtml(raw('clock_in_window_end'))}">
                </div>
                <div style="grid-column:1/-1" class="ui-row">
                    <button type="submit" class="ui-btn ui-btn-primary">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('save'))}</button>
                    <button type="button" class="ui-btn" data-company-window="1">${this.escapeHtml(I18n.__('sitesWindowUseCompany'))}</button>
                    <button type="button" class="ui-btn ui-btn-quiet" data-cancel-site-edit="1">${this.escapeHtml(I18n.__('cancel'))}</button>
                </div>
            </form>`;
    },

    /** The zones an administrator is most likely to want, as suggestions - not a closed list. */
    siteTimezoneOptionsHtml() {
        return `<datalist id="siteTimezoneOptions">
            ${['Africa/Cairo', 'Africa/Alexandria', 'Asia/Riyadh', 'Asia/Dubai', 'Asia/Kolkata', 'Europe/London', 'UTC']
                .map((zone) => `<option value="${this.escapeHtml(zone)}"></option>`).join('')}
        </datalist>`;
    },

    sitesAddHtml() {
        // The hint above the fields is the whole difference between a form an admin can
        // fill in from a phone and one they have to guess at - including the window, which is
        // the field that decides whether a worker arriving at 05:30 is on time or a review.
        return `
            <section class="ui-card is-flat" aria-labelledby="sitesAddTitle">
                <h3 class="ui-section-title" id="sitesAddTitle">${this.escapeHtml(I18n.__('sitesAdd'))}</h3>
                <p class="ui-section-note" style="margin-top:4px">${this.escapeHtml(I18n.__('sitesLocationHint'))}</p>
                <form id="addSiteForm" class="ui-grid three" style="margin-top:14px">
                    <div>
                        <label class="ui-label" for="siteName">${this.escapeHtml(I18n.__('sitesName'))}</label>
                        <input type="text" id="siteName" class="ui-field" required
                               placeholder="${this.escapeHtml(I18n.__('sitesNamePlaceholder'))}">
                    </div>
                    <div>
                        <label class="ui-label" for="location">${this.escapeHtml(I18n.__('sitesLocation'))}</label>
                        <input type="text" id="location" class="ui-field" inputmode="decimal" required
                               placeholder="${this.escapeHtml(I18n.__('sitesLocationPlaceholder'))}">
                    </div>
                    <div>
                        <label class="ui-label" for="radius">${this.escapeHtml(I18n.__('sitesRadius'))}</label>
                        <input type="number" id="radius" class="ui-field" min="1" step="1" placeholder="100" required>
                    </div>
                    <div style="grid-column:1/-1">
                        <p class="ui-section-note">${this.escapeHtml(I18n.__('sitesWindowHint'))}</p>
                    </div>
                    <div>
                        <label class="ui-label" for="siteWindowStart">${this.escapeHtml(I18n.__('sitesWindowStart'))}</label>
                        <input type="time" id="siteWindowStart" class="ui-field">
                    </div>
                    <div>
                        <label class="ui-label" for="siteWindowEnd">${this.escapeHtml(I18n.__('sitesWindowEnd'))}</label>
                        <input type="time" id="siteWindowEnd" class="ui-field">
                    </div>
                    <div>
                        <label class="ui-label" for="siteWindowTimezone">${this.escapeHtml(I18n.__('sitesWindowTimezone'))}</label>
                        <input type="text" id="siteWindowTimezone" class="ui-field" list="siteTimezoneOptions"
                               placeholder="${this.escapeHtml(I18n.__('sitesWindowTimezonePlaceholder'))}">
                    </div>
                    <div style="grid-column:1/-1">
                        <button type="submit" class="ui-btn ui-btn-primary">${this.OPS_ICONS.plus}${this.escapeHtml(I18n.__('sitesAdd'))}</button>
                    </div>
                </form>
                ${this.siteTimezoneOptionsHtml()}
            </section>`;
    },

    sitesHtml(sites) {
        const list = sites.length === 0
            ? `<div class="ui-empty" data-sites-empty="true">
                    <span class="ui-empty-icon">${this.OPS_ICONS.pin}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('sitesEmpty'))}</p>
                    <p class="ui-empty-body">${this.escapeHtml(I18n.__('sitesEmptyHint'))}</p>
               </div>`
            : `<ul id="sitesList" class="ui-stack" style="list-style:none;margin:0;padding:0" data-sites-list="true">
                    ${sites.map((site) => this.sitesCardHtml(site)).join('')}
               </ul>`;
        return `
            ${this.sitesAddHtml()}
            <section class="ui-section">
                <div class="ui-section-head">
                    <h3 class="ui-section-title">${this.escapeHtml(I18n.__('sitesTitle'))}</h3>
                    <p class="ui-section-note" data-sites-count>${this.escapeHtml(I18n.__('sitesCount').replace('{count}', String(sites.length)))}</p>
                </div>
                ${list}
            </section>`;
    },

    async renderSites(content) {
        if (!content) return;
        content.innerHTML = UI.consoleSkeletonHtml(I18n.__('sitesTitle'));
        let sites;
        try {
            sites = await API.request('/admin/sites');
        } catch (err) {
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Sites')");
            return;
        }
        this._sites = Array.isArray(sites) ? sites : [];
        this._sitesContent = content;
        this.paintSites(content);
    },

    /** Repaint from the list already in hand: opening the editor must not refetch the sites. */
    paintSites(content) {
        const target = content || this._sitesContent;
        if (!target) return;
        target.innerHTML = `<div class="ui-page" data-sites="true">${this.sitesHtml(this._sites || [])}</div>`;
        const form = document.getElementById('addSiteForm');
        if (form) form.onsubmit = (event) => this.addSite(event);
        const edit = document.getElementById('editSiteForm');
        if (edit) edit.onsubmit = (event) => this.saveSite(event);
        // One listener for every card, bound as a property rather than written as an
        // ``onclick=`` attribute in the markup: handler text built from data is the sink the
        // document CSP keeps 'unsafe-inline' for (see docs/FRONTEND_RENDERING.md).
        const list = document.getElementById('sitesList');
        if (list) list.onclick = (event) => this.onSitesClick(event);
    },

    /**
     * The Sites tab's buttons: edit, cancel, and "use the company window".
     *
     * Delegated from the list container, so no handler text is ever built out of a site name,
     * and guarded on ``closest`` because the event this receives is the browser's to shape.
     */
    onSitesClick(event) {
        const target = event && event.target;
        if (!target || typeof target.closest !== 'function') return;
        const edit = target.closest('[data-edit-site]');
        if (edit) { this.openSiteEdit(edit.dataset.editSite); return; }
        if (target.closest('[data-cancel-site-edit]')) { this.cancelSiteEdit(); return; }
        if (target.closest('[data-company-window]')) this.useCompanyWindow();
    },

    openSiteEdit(name) {
        this._siteEdit = String(name || '');
        this.paintSites();
    },

    cancelSiteEdit() {
        this._siteEdit = null;
        this.paintSites();
    },

    /**
     * Empty the three window boxes: the site goes back to the company window when saved.
     *
     * Emptied rather than filled with today's company values, because the save sends whatever is
     * in the boxes - a prefilled copy would pin this site to the company hours *as they are now*,
     * which is the opposite of what "use the company window" means.
     */
    useCompanyWindow() {
        ['editSiteWindowStart', 'editSiteWindowEnd', 'editSiteTimezone'].forEach((id) => {
            const field = document.getElementById(id);
            if (field) field.value = '';
        });
        const notice = document.getElementById('editSiteWindowNotice');
        if (notice) notice.textContent = I18n.__('sitesWindowWillInherit');
    },

    /** The add form's one action, in one place so its failure path is the same shape as every other form's. */
    async addSite(event) {
        if (event && event.preventDefault) event.preventDefault();
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value).trim() : '';
        };
        try {
            await API.request('/admin/sites/add', { method: 'POST', body: {
                site_name: value('siteName'),
                location_input: value('location'),
                radius: parseFloat(value('radius')),
                admin_id: State.user.id,
                // Blank means "inherit the company window", which is what an empty time box
                // says - so it is sent as null rather than as "", and the site keeps
                // following the company hours when those change.
                clock_in_window_start: value('siteWindowStart') || null,
                clock_in_window_end: value('siteWindowEnd') || null,
                site_timezone: value('siteWindowTimezone') || null
            }});
        } catch (err) {
            // The reason has to reach the admin: "Site name already exists." and "Invalid
            // location format." are both actionable, and without this the failure was an
            // unhandled rejection - the form simply did nothing at all.
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('sitesAdded'));
        UI.renderAdminTab('Sites');
    },

    /**
     * Save the open editor: where the site is, and the window its arrivals are judged by.
     *
     * All three window fields are sent, and a blank box is sent as ``null`` - the API keeps
     * "absent" (leave it alone) and "explicitly cleared" (put this site back on the company
     * window) apart on purpose, and this form is the second of those. Omitting them because
     * nothing looked changed would make "clear the window" unsaveable.
     */
    async saveSite(event) {
        if (event && event.preventDefault) event.preventDefault();
        const name = this._siteEdit;
        if (!name) return;
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value).trim() : '';
        };
        try {
            await API.request('/admin/sites/edit', { method: 'POST', body: {
                site_name: name,
                location_input: value('editSiteLocation'),
                radius: parseFloat(value('editSiteRadius')),
                admin_id: State.user.id,
                clock_in_window_start: value('editSiteWindowStart') || null,
                clock_in_window_end: value('editSiteWindowEnd') || null,
                site_timezone: value('editSiteTimezone') || null
            }});
        } catch (err) {
            Toast.error(err.message);
            return null;
        }
        Toast.success(I18n.__('sitesSaved'));
        this._siteEdit = null;
        UI.renderAdminTab('Sites');
        return true;
    },

    async deleteSite(name) {
        // The confirmation names the consequence rather than asking "are you sure": what an
        // admin needs to know before deleting a site is that the shifts already recorded
        // stay as they are, and that nobody can clock in there again.
        if (!confirm(I18n.__('sitesDeleteConfirm'))) return;
        const fd = new FormData(); fd.append('site_name', name); fd.append('admin_id', State.user.id);
        try {
            await API.request('/admin/sites/delete', { method: 'POST', body: fd });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        UI.renderAdminTab('Sites');
    },

    // -----------------------------------------------------------------
    //  Credentials - who can sign in, and with what
    //
    //  Replaces the enrollment dashboard. That screen listed accounts and could
    //  capture one photo, and the console's separate Enroll tab was never wired to
    //  anything - it rendered "module coming soon". Those are one job, not three:
    //  when a worker calls from site, the admin needs to see whether the account is
    //  usable and to set a password.
    //
    //  What this tab will not do is show an existing password, and it does not
    //  pretend otherwise. Passwords are stored as bcrypt hashes, which are one-way:
    //  there is nothing to reveal. The column is password *state* - set, never set,
    //  when it last changed, how many sessions a reset killed - and the one moment a
    //  password is readable is the moment it is created here, so it can be handed
    //  to the worker. Keeping a readable copy instead would put every worker's
    //  password in the hands of anyone holding the database file or a backup of it.
    // -----------------------------------------------------------------

    /** The roster on screen; null until the first load has answered. */
    _credentials: null,

    /** The account whose password is being set, by id. */
    _credentialsTarget: null,

    /** The generated password for that account, held in memory only. */
    _credentialsPassword: '',

    /** A password just saved: shown once, then forgotten when the panel is closed. */
    _credentialsRevealed: null,

    /** Which of the two create flows is open: ``''``, ``'create'`` or ``'invite'``. */
    _credentialsMode: '',

    /** The photo chosen for a new account, and the password generated beside it. */
    _credentialsNewPhoto: null,

    _credentialsNewPassword: '',

    /** Why the chosen file was refused, in the admin's words, or ``''``. */
    _credentialsPhotoError: '',

    /** The account just created: its password is readable here once, then never again. */
    _credentialsCreated: null,

    /** The registration link just issued - the plaintext token exists only in this object. */
    _credentialsIssued: null,

    /**
     * The upload policy, mirroring ``backend/uploads.py``.
     *
     * 5 MB, JPEG/PNG/WebP, nothing else. Checked here so the admin is told before a
     * 6 MB photo has been uploaded over a phone tether, and checked again on the server -
     * because ``accept=`` and a ``file.type`` are both chosen by whoever sends the
     * request, and only the bytes are the truth.
     */
    PHOTO_POLICY: {
        maxBytes: 5 * 1024 * 1024,
        maxMb: 5,
        accepted: ['image/jpeg', 'image/png', 'image/webp']
    },

    credentialsQuery() {
        return State.credentialsQuery || '';
    },

    /**
     * The accounts matching a search.
     *
     * Same rule as the shifts tab: every term has to match somewhere, so "tower 600"
     * narrows instead of widening. It matches identity (name, id, phone, email) and
     * role, because "who are the moallems" is a roster question too.
     */
    credentialsMatches(users, query) {
        const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
        if (terms.length === 0) return users;
        return users.filter((user) => {
            const haystack = [user.name, user.id, user.phone, user.email, user.role,
                this.roleLabel(user.role)].join(' ').toLowerCase();
            return terms.every((term) => haystack.indexOf(term) >= 0);
        });
    },

    // -----------------------------------------------------------------
    //  Links tab - quick clock links
    // -----------------------------------------------------------------
    //  A link is a credential with a person's name on it: one tap clocks that worker in,
    //  the next clocks them out, and no password is involved at any point. So this screen
    //  is where one is issued, where it is watched, and where it is killed.
    //
    //  The list of punches is the part that matters. A link cannot prove who was holding
    //  the phone, and the system does not pretend otherwise - it photographs the tap. A
    //  photograph can only be looked at, so this screen is built for looking: every tap is
    //  listed under its link with the selfie it was taken with, fetched with the session's
    //  token rather than by a URL that would work for anybody who has it.
    // -----------------------------------------------------------------
    async renderLinks(content) {
        // Entering the tab forgets the last issued link: it cannot be shown again, and a
        // stale URL left on screen is a URL somebody would copy tomorrow and expect to work.
        this._newLink = null;
        this.paintLinks(content, UI.loadingHtml());
        await this.loadLinks(content);
    },

    async loadLinks(content) {
        let links = null;
        try {
            links = await API.request('/admin/quick_links');
        } catch (err) {
            this._links = null;
            this.paintLinks(content, this.linkCreateHtml(null) +
                `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`);
            return;
        }
        let users = null;
        try {
            users = await API.request('/admin/users');
        } catch (_err) {
            // A roster that cannot be read is not a reason to lose the links. The form falls
            // back to typing an id, and the list - which is what this screen is for, and the
            // only place a punch made with a link can be reviewed - still renders.
            users = null;
        }
        this._links = links;
        this._linksRoster = users;
        this.paintLinks(content, this.linkCreateHtml(users) + this.linksHtml(links) +
            '<div id="linkUsesPanel"></div>');
    },

    paintLinks(content, html) {
        content.innerHTML = html;
        const form = document.getElementById('linkCreateForm');
        if (form) {
            form.onsubmit = (event) => {
                event.preventDefault();
                return this.createLink();
            };
        }
    },

    /** Accounts a link may be issued to: the ones whose hours a report pays out. */
    linkCandidates(users) {
        return (users || []).filter((user) =>
            user.role !== 'admin' && user.role !== 'head_admin' &&
            this.accountStatus(user) === 'active');
    },

    linkCreateHtml(users) {
        const field = 'ui-field';
        const candidates = this.linkCandidates(users);
        // The server refuses a link for a deactivated account or an administrator. Offering
        // either would be offering a request that always fails, so they are simply absent.
        const chooser = users === null
            ? `<input id="linkWorker" class="${field}" inputmode="numeric" placeholder="${I18n.__('userId')}" required>
                        <span class="ui-note is-warn">${I18n.__('linksRosterUnavailable')}</span>`
            : (candidates.length === 0
                ? `<p class="ui-note is-body is-warn" data-no-candidates>${I18n.__('linksNoCandidates')}</p>`
                : `<select id="linkWorker" class="${field}" required>
                        ${candidates.map((user) => `<option value="${this.escapeHtml(user.id)}">${this.escapeHtml(user.name || user.id)} (${this.escapeHtml(user.id)})</option>`).join('')}
                   </select>`);
        const ttl = [['24', 'linksTtlDay'], ['24 * 7', 'linksTtlWeek'], ['24 * 30', 'linksTtlMonth']]
            .map(([hours, key]) => `<option value="${hours}"${hours === '24 * 30' ? ' selected' : ''}>${I18n.__(key)}</option>`).join('');
        return `
            <div class="ui-card is-stacked">
                <h3 class="ui-card-title">${I18n.__('linksIssue')}</h3>
                <p class="ui-note">${I18n.__('linksHint')}</p>
                <form id="linkCreateForm" class="ui-grid four">
                    <label class="ui-label">${I18n.__('linksWorker')}
                        ${chooser}
                    </label>
                    <label class="ui-label">${I18n.__('linksTtl')}
                        <select id="linkTtl" class="${field}">${ttl}</select>
                    </label>
                    <label class="ui-label">${I18n.__('linksMaxUses')}
                        <input id="linkMaxUses" class="${field}" type="number" min="0" max="100" value="0" />
                    </label>
                    <label class="ui-label">${I18n.__('linksNoteField')}
                        <input id="linkNote" class="${field}" placeholder="${I18n.__('linksNotePlaceholder')}" />
                    </label>
                    <button type="submit" class="ui-btn ui-btn-primary ui-span-all">${I18n.__('linksCreate')}</button>
                </form>
                <p class="ui-note">${I18n.__('linksMaxUsesHint')}</p>
                <div id="linkResult" data-link-result>${this._newLink ? this.newLinkHtml(this._newLink) : ''}</div>
            </div>`;
    },

    async createLink() {
        const workerField = document.getElementById('linkWorker');
        const workerId = workerField ? String(workerField.value || '').trim() : '';
        if (!workerId) {
            Toast.error(I18n.__('linksWorkerRequired'));
            return;
        }
        const ttl = document.getElementById('linkTtl');
        const cap = document.getElementById('linkMaxUses');
        const note = document.getElementById('linkNote');
        const payload = {
            worker_id: workerId,
            ttl_hours: Number(ttl ? ttl.value : 24 * 30),
            max_uses: Number(cap && cap.value !== '' ? cap.value : 0),
            note: note && note.value ? String(note.value) : null
        };
        try {
            const res = await API.request('/admin/quick_links', { method: 'POST', body: payload });
            // Held on the module, not written into the DOM: the list is repainted straight
            // after this, and a panel painted first would be wiped by that repaint - taking
            // the one copy of the link with it.
            this._newLink = res;
            Toast.success(I18n.__('linksCreated'));
            await this.loadLinks(document.getElementById('adminContent'));
        } catch (err) {
            Toast.error(err.message);
        }
    },

    /**
     * The link itself, shown once and never again.
     *
     * Only the token's hash is stored, so a list of past links cannot show their URLs: the
     * panel that appears here is the single chance to copy this one, and it says so rather
     * than leaving an administrator to discover it later. It is built from the reply that
     * created it every time the tab repaints - the copy on screen is not a second chance,
     * it is the same one still on screen.
     */
    newLinkHtml(res) {
        const field = 'ui-field';
        const qr = res.qr_png_data_uri
            ? `<img src="${this.escapeHtml(res.qr_png_data_uri)}" alt="${I18n.__('linksQr')}" class="ui-qr" />`
            : '';
        return `
            <div class="ui-alert is-ok is-stacked" data-new-link="${this.escapeHtml(res.link_id)}">
                <p class="ui-card-title">${I18n.__('linksFor')} ${this.escapeHtml(res.worker_name || res.worker_id)} (${this.escapeHtml(res.worker_id)})</p>
                <input id="linkUrl" class="${field}" readonly value="${this.escapeHtml(res.url)}" />
                <div class="ui-row">
                    <button type="button" onclick="UI_MODULES.copyLinkUrl()" class="ui-btn ui-btn-primary">${I18n.__('linksCopy')}</button>
                </div>
                <p class="ui-note is-warn">${I18n.__('linksShownOnce')}</p>
                ${qr}
            </div>`;
    },

    async copyLinkUrl() {
        // The field first, then the reply that painted it: they hold the same URL, and the
        // fallback keeps the button working if the input has not been painted yet.
        const field = document.getElementById('linkUrl');
        const url = (field && String(field.value || '')) || String((this._newLink || {}).url || '');
        if (!url) return;
        const done = () => Toast.success(I18n.__('copied'));
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(done).catch(() => prompt(I18n.__('linksCopy'), url));
        } else {
            prompt(I18n.__('linksCopy'), url);
        }
    },

    linksHtml(links) {
        if (!links || links.length === 0) {
            return `<p class="ui-empty" data-no-links>${I18n.__('linksEmpty')}</p>`;
        }
        return Device.isMobile ? this.linkCardsHtml(links) : this.linkTableHtml(links);
    },

    linkTableHtml(links) {
        return `
            <div class="ui-table-wrap">
                <table class="ui-table">
                    <thead class="ui-table-head">
                        <tr>
                            <th>${I18n.__('linksWorker')}</th>
                            <th>${I18n.__('linksState')}</th>
                            <th>${I18n.__('linksTaps')}</th>
                            <th>${I18n.__('linksExpires')}</th>
                            <th>${I18n.__('linksLastUse')}</th>
                            <th>${I18n.__('activeShifts')}</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>
                        ${links.map((link) => `<tr data-link="${link.id}">
                            <td><span class="ui-strong">${this.escapeHtml(link.worker_name || link.worker_id)}</span>
                                <span class="ui-note">${this.escapeHtml(link.worker_id)}</span>
                                ${link.note ? `<span class="ui-note">${this.escapeHtml(link.note)}</span>` : ''}</td>
                            <td data-link-state="${this.linkState(link)}">${this.linkStateHtml(link)}</td>
                            <td data-link-uses="${link.uses}">${this.linkUsesLabel(link)}</td>
                            <td class="ui-nowrap">${this.escapeHtml(link.expires_at)}</td>
                            <td class="ui-nowrap">${this.linkLastUse(link)}</td>
                            <td class="ui-nowrap">${this.linkOpenShift(link)}</td>
                            <td class="is-end">${this.linkActionHtml(link)}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    },

    /** The phone layout: one card per link, the same facts as the table. */
    linkCardsHtml(links) {
        return `<div class="ui-stack">${links.map((link) => `
            <div class="ui-card is-stacked" data-link="${link.id}">
                <div class="ui-spread">
                    <p class="ui-strong ui-truncate">${this.escapeHtml(link.worker_name || link.worker_id)}</p>
                    <p class="ui-note ui-nowrap">${this.escapeHtml(link.worker_id)}</p>
                </div>
                <p class="ui-note" data-link-state="${this.linkState(link)}">${this.linkStateHtml(link)}</p>
                <p class="ui-note">${this.linkUsesLabel(link)} · ${I18n.__('linksExpires')} ${this.escapeHtml(link.expires_at)}</p>
                <p class="ui-note">${I18n.__('linksLastUse')}: ${this.linkLastUse(link)}</p>
                <p class="ui-note">${this.linkOpenShift(link)}</p>
                <div class="ui-row">${this.linkActionHtml(link)}</div>
            </div>`).join('')}</div>`;
    },

    /** One word for why the link is or is not usable, including the account it belongs to. */
    linkState(link) {
        if (link.state === 'revoked') return 'revoked';
        if (link.state === 'expired') return 'expired';
        if (link.state === 'used_up') return 'used_up';
        // The link is intact but the account behind it was switched off - a distinction that
        // matters, because revoking the account is not the same as revoking the credential.
        if (link.worker_active === false) return 'account_inactive';
        return 'active';
    },

    linkStateHtml(link) {
        const state = this.linkState(link);
        // Roles, not colours: the four tones are the ones the badges and the stat tiles
        // already use, and they carry the dark theme with them - which the sixteen utility
        // strings that used to be here did not, once they were written for light only.
        const tones = {
            active: 'is-ok',
            revoked: 'is-danger',
            expired: 'is-warn',
            used_up: 'is-warn',
            account_inactive: 'is-danger'
        };
        return `<span class="ui-state ${tones[state] || ''}">${codeLabel('linksState', state)}</span>`;
    },

    linkUsesLabel(link) {
        const cap = Number(link.max_uses) || 0;
        if (cap === 0) return `${link.uses} · ${I18n.__('linksUnlimited')}`;
        return `${link.uses} / ${cap}`;
    },

    linkLastUse(link) {
        if (!link.last_used_at) return `<span class="ui-tone-muted">${I18n.__('linksNever')}</span>`;
        return `${this.escapeHtml(link.last_used_at)}${link.last_used_ip ? ` <span class="ui-tone-muted">${this.escapeHtml(link.last_used_ip)}</span>` : ''}`;
    },

    linkOpenShift(link) {
        if (!link.clocked_in) return '';
        return `<span class="ui-tone-ok">${I18n.__('linksOnShift')}</span> ${this.escapeHtml(link.clock_in_time || '')} ${this.escapeHtml(link.open_shift_site || '')}`;
    },

    /**
     * Revoking is the one action a credential needs, and a dead one never gets the button.
     *
     * An expired or used-up link is already dead, so revoking it would change nothing while
     * looking like it did something. A link whose *account* was deactivated is the case that
     * is not obvious: the link itself is still live, so killing it is a real action.
     */
    linkActionHtml(link) {
        const id = this.escapeHtml(link.id);
        const state = this.linkState(link);
        const revoke = (state === 'active' || state === 'account_inactive')
            ? `<button type="button" data-revoke-link="${id}" onclick="UI_MODULES.revokeLink(${link.id})"
                    class="ui-btn ui-btn-danger ui-btn-sm">${I18n.__('linksRevoke')}</button>`
            : '';
        return `${revoke}
            <button type="button" data-link-uses="${id}" onclick="UI_MODULES.openLinkUses(${link.id})"
                    class="ui-btn ui-btn-sm">${I18n.__('linksUses')}</button>`;
    },

    async revokeLink(linkId) {
        if (!confirm(I18n.__('linksConfirmRevoke'))) return;
        try {
            const res = await API.request(`/admin/quick_links/${linkId}/revoke`, { method: 'POST' });
            Toast.success(res.message || I18n.__('linksRevoked'));
        } catch (err) {
            Toast.error(err.message);
        }
        await this.loadLinks(document.getElementById('adminContent'));
    },

    async openLinkUses(linkId) {
        const panel = document.getElementById('linkUsesPanel');
        if (panel) panel.innerHTML = UI.loadingHtml();
        try {
            const data = await API.request(`/admin/quick_links/${linkId}/uses`);
            if (panel) panel.innerHTML = this.linkUsesHtml(data);
        } catch (err) {
            if (panel) panel.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
        }
    },

    /** Every tap a link produced, newest first, each with the selfie it was taken with. */
    linkUsesHtml(data) {
        const uses = data.uses || [];
        const rows = uses.length === 0
            ? `<p class="ui-note is-center" data-no-uses>${I18n.__('linksNoUses')}</p>`
            : uses.map((use) => `
                <div class="ui-card is-tight is-stacked" data-use="${use.id}">
                    <div class="ui-spread">
                        <p class="ui-card-title">${this.escapeHtml(use.action)} · ${this.escapeHtml(use.site_name || '')}</p>
                        <p class="ui-note ui-nowrap">${this.escapeHtml(use.created_at)}</p>
                    </div>
                    <p class="ui-note">
                        ${use.hours !== null && use.hours !== undefined ? `${I18n.__('linksPaidHours')}: <b>${this.hoursLabel(use.hours)}</b> · ` : ''}
                        ${I18n.__('linksFaceCount')}: <b>${this.escapeHtml(use.face_count)}</b>
                        ${use.ip ? ` · ${this.escapeHtml(use.ip)}` : ''}
                        ${use.lat !== null && use.lat !== undefined ? ` · ${this.escapeHtml(use.lat)}, ${this.escapeHtml(use.lon)}` : ''}
                    </p>
                    ${use.flag_reason ? `<p class="ui-note is-warn">${this.escapeHtml(use.flag_reason)}</p>` : ''}
                    <div class="ui-row">
                        <button type="button" data-show-photo="${use.id}" onclick="UI_MODULES.showLinkPhoto(${use.id})"
                                class="ui-btn ui-btn-sm">${I18n.__('linksShowPhoto')}</button>
                        <img id="linkPhoto${use.id}" class="hidden ui-photo" alt="${I18n.__('linksPhotoAlt')}" />
                    </div>
                </div>`).join('');
        return `
            <div class="ui-card is-stacked" data-uses-for="${this.escapeHtml(data.link_id)}">
                <div class="ui-spread">
                    <h3 class="ui-card-title">${I18n.__('linksUsesTitle')} · ${this.escapeHtml(data.worker_name || data.worker_id)}</h3>
                    <button type="button" onclick="UI_MODULES.closeLinkUses()" class="ui-btn ui-btn-quiet ui-btn-sm is-icon">✕</button>
                </div>
                <p class="ui-note">${I18n.__('linksUsesHint')}</p>
                <div class="ui-stack">${rows}</div>
            </div>`;
    },

    closeLinkUses() {
        const panel = document.getElementById('linkUsesPanel');
        if (panel) panel.innerHTML = '';
    },

    /**
     * One selfie, fetched with this admin's token.
     *
     * Deliberately not an ``<img src>``: a photo of a worker's face must not be readable by
     * anybody who happens to know the URL, and an image tag cannot carry an Authorization
     * header. The bytes are fetched with the session's credential and turned into an object
     * URL that only this page can use.
     */
    async showLinkPhoto(useId) {
        const image = document.getElementById(`linkPhoto${useId}`);
        const headers = {};
        if (State.token) headers['Authorization'] = `Bearer ${State.token}`;
        try {
            const response = await fetch(`${API.baseURL}/admin/quick_link_photo/${useId}`, { headers });
            if (!response.ok) throw new Error(I18n.__('linksPhotoFailed'));
            const blob = await response.blob();
            if (image) {
                image.src = URL.createObjectURL(blob);
                image.classList.remove('hidden');
            }
        } catch (err) {
            Toast.error(err.message || I18n.__('linksPhotoFailed'));
        }
    },

    async renderCredentials(content) {
        // Paint the toolbar before the request: the roster can be slow from site, and
        // the admin should see the search they are about to use rather than a blank tab.
        this.paintCredentials(content, this.credentialsToolbarHtml() + UI.loadingHtml());
        await this.loadCredentials(content);
    },

    async loadCredentials(content) {
        try {
            const users = await API.request('/admin/users');
            this._credentials = users;
            this.paintCredentials(content, this.credentialsToolbarHtml() + this.credentialsHtml(users));
        } catch (err) {
            // No roster for this screen, so nothing may be set from what is left over.
            this._credentials = null;
            this.paintCredentials(content, this.credentialsToolbarHtml() +
                `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`);
        }
    },

    /**
     * Paints the tab and binds the search box.
     *
     * Repainted from a string on every path, like the shifts tab: the roster, the
     * search and the password panel then always come from one consistent view.
     */
    paintCredentials(content, html) {
        content.innerHTML = html;
        const form = document.getElementById('credentialsSearchForm');
        if (form) {
            form.onsubmit = (event) => {
                event.preventDefault();
                return this.applyCredentialsSearch();
            };
        }
    },

    /** Repaints the roster from the last response; only a search or a save needs this. */
    repaintCredentialsFromCache() {
        const content = document.getElementById('adminContent');
        if (!content || !this._credentials) return UI.renderAdminTab('Credentials');
        this.paintCredentials(content, this.credentialsToolbarHtml() + this.credentialsHtml(this._credentials));
        return Promise.resolve();
    },

    async applyCredentialsSearch() {
        const box = document.getElementById('credentialsQuery');
        State.credentialsQuery = box ? String(box.value || '').trim() : '';
        return this.repaintCredentialsFromCache();
    },

    async clearCredentialsSearch() {
        State.credentialsQuery = '';
        return this.repaintCredentialsFromCache();
    },

    /** Name, id, role, contact, and how each account's access actually stands. */
    credentialsToolbarHtml() {
        const query = this.credentialsQuery();
        // No ``oninput`` here on purpose: searching repaints the roster, and repainting on
        // every keystroke would replace the box the caret is in. The Search button is the
        // commit, which is also what makes the phone keyboard's Go key do the right thing.
        return `
            <div class="ui-section-head">
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('credentials'))}</h3>
                <p class="ui-section-note" data-hashed-note style="max-width:60ch">${this.escapeHtml(I18n.__('credentialsHashedNote'))}</p>
            </div>
            <form id="credentialsSearchForm" class="ui-row" style="margin-top:12px">
                <label class="sr-only" for="credentialsQuery">${this.escapeHtml(I18n.__('credentialsSearchPlaceholder'))}</label>
                <input type="search" id="credentialsQuery" class="ui-field is-flex" value="${this.escapeHtml(query)}"
                       placeholder="${this.escapeHtml(I18n.__('credentialsSearchPlaceholder'))}">
                <button type="submit" class="ui-btn">${this.OPS_ICONS.search}${this.escapeHtml(I18n.__('search'))}</button>
                ${query ? `<button type="button" onclick="UI_MODULES.clearCredentialsSearch()" class="ui-btn ui-btn-quiet">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('clear'))}</button>` : ''}
            </form>
            ${this.credentialsActionsHtml()}`;
    },

    /**
     * The two ways an account starts from this tab: made here, or made by its owner.
     *
     * Both are on the Credentials screen rather than in a tab of their own, because both
     * end in the same place - a row in the roster below - and because the question an
     * admin is answering ("this man needs access") is the question this tab already asks.
     */
    credentialsActionsHtml() {
        return `
            <div class="ui-row" style="margin-top:12px">
                <button type="button" id="credentialsOpenCreate" data-open-create="true"
                        onclick="UI_MODULES.openCredentialsMode('create')"
                        class="ui-btn ui-btn-primary">${this.OPS_ICONS.person}${this.escapeHtml(I18n.__('credentialsNewAccount'))}</button>
                <button type="button" id="credentialsOpenInvite" data-open-invite="true"
                        onclick="UI_MODULES.openCredentialsMode('invite')" class="ui-btn">${this.OPS_ICONS.link}${this.escapeHtml(I18n.__('credentialsLink'))}</button>
            </div>`;
    },

    credentialsHtml(users) {
        const query = this.credentialsQuery();
        const shown = this.credentialsMatches(users, query);
        // The panel sits above the list rather than inside a row: one account is being
        // edited at a time, and a password that is about to be handed over deserves to
        // be somewhere the eye lands, not squeezed between columns.
        const panel = this.credentialsCreatorHtml() + this.credentialsPanelHtml() + this.credentialsEditPanelHtml();
        if (users.length === 0) {
            return `${panel}
                <div class="ui-empty">
                    <span class="ui-empty-icon">${this.OPS_ICONS.person}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('credentialsEmpty'))}</p>
                </div>`;
        }
        if (shown.length === 0) {
            // No table at all in this state: an empty table with a header row reading
            // "Name / Role / Password" looks like a roster that failed to load, which is
            // the opposite of what it means.
            return `${panel}
                <div class="ui-empty" data-no-matches="true">
                    <span class="ui-empty-icon">${this.OPS_ICONS.search}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('credentialsNoMatches'))}</p>
                    <button type="button" class="ui-btn" onclick="UI_MODULES.clearCredentialsSearch()">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('clear'))}</button>
                </div>`;
        }
        const note = query ? `
            <p class="ui-section-note" data-filter-note style="margin-top:12px">
                ${this.escapeHtml(I18n.__('shiftsFiltered'))}: “${this.escapeHtml(query)}” · ${shown.length} / ${users.length}
            </p>` : '';
        return `${panel}${note}${Device.isMobile ? this.credentialsCardsHtml(shown) : this.credentialsTableHtml(shown)}`;
    },

    credentialsTableHtml(users) {
        return `
            <div class="ui-table-wrap" style="margin-top:12px">
                <table class="ui-table" data-credentials-table="true">
                    <caption class="sr-only">${this.escapeHtml(I18n.__('credentials'))}</caption>
                    <thead>
                        <tr>
                            <th scope="col">${this.escapeHtml(I18n.__('name'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('userId'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('role'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('credentialsContact'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('credentialsFace'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('password'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('credentialsSessions'))}</th>
                            <th scope="col">${this.escapeHtml(I18n.__('credentialsStatus'))}</th>
                            <th scope="col"><span class="sr-only">${this.escapeHtml(I18n.__('credentialsActions'))}</span></th>
                        </tr>
                    </thead>
                    <tbody>
                        ${users.map(user => `<tr data-user="${this.escapeHtml(user.id)}">
                            <!-- No avatar in this cell on purpose: it is the name column, and an
                                 avatar's initials are text in the cell - the roster is read by
                                 name, and "SW Seed Worker" is not a name. The phone cards
                                 have the room for one; a nine-column table does not. -->
                            <td><span class="ops-name">${this.escapeHtml(user.name || user.id)}</span></td>
                            <td class="is-numeric">${this.escapeHtml(user.id)}</td>
                            <td>${this.escapeHtml(this.roleLabel(user.role))}</td>
                            <td class="ops-sub">${this.credentialsContact(user)}</td>
                            <td data-face="${user.face_enrolled ? 'enrolled' : 'missing'}">${this.credentialsFace(user)}</td>
                            <td data-password="${user.password_set ? 'set' : 'never'}">${this.credentialsPasswordState(user)}</td>
                            <td class="is-numeric">${this.sessionsRevoked(user)}</td>
                            <td data-status="${this.accountStatus(user)}">${this.accountStatusHtml(user)}</td>
                            <td class="is-end">${this.credentialsActionHtml(user, true)}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    },

    /**
     * The phone layout: one card per account, the same fields as the table.
     */
    credentialsCardsHtml(users) {
        return `<div class="ui-stack">${users.map(user => `
            <div class="ui-card is-stacked" data-user="${this.escapeHtml(user.id)}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        ${this.liveOpsAvatarHtml(user)}
                        <span class="ops-name">${this.escapeHtml(user.name || user.id)}</span>
                    </div>
                    <span class="ops-sub is-numeric">${this.escapeHtml(user.id)}</span>
                </div>
                <p class="ops-sub" style="margin-top:4px">${this.escapeHtml(this.roleLabel(user.role))} \u00b7 ${this.credentialsContact(user)}</p>
                <div class="ui-facts" style="margin-top:12px">
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('credentialsFace'))}</span>
                        <span>${this.credentialsFace(user)}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('password'))}</span>
                        <span>${this.credentialsPasswordState(user)}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('credentialsSessions'))}</span>
                        <span class="ui-fact-value">${this.sessionsRevoked(user)}</span>
                    </div>
                    <div class="ui-fact" data-status="${this.accountStatus(user)}">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('credentialsStatus'))}</span>
                        <span>${this.accountStatusHtml(user)}</span>
                    </div>
                </div>
                <div class="ui-row" style="margin-top:12px">${this.credentialsActionHtml(user)}</div>
            </div>`).join('')}</div>`;
    },

    credentialsContact(user) {
        const parts = [user.phone, user.email].filter((value) => value);
        return parts.length > 0 ? parts.map((value) => this.escapeHtml(value)).join(' · ') : '\u2014';
    },

    credentialsFace(user) {
        // A pill with a word in it, never a bare colour: the two states here are the ones
        // a red/green-blind admin is most likely to meet.
        return user.face_enrolled
            ? `<span class="ui-badge is-ok">${this.OPS_ICONS.camera}${this.escapeHtml(I18n.__('credentialsFaceEnrolled'))}</span>`
            : `<span class="ui-badge is-warn">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('credentialsFaceMissing'))}</span>`;
    },

    /**
     * What is known about the password: whether one exists, and when it was last set.
     *
     * The value itself is not here because it is not anywhere - see the tab's comment.
     */
    credentialsPasswordState(user) {
        if (!user.password_set) {
            return `<span class="ui-badge is-warn">${this.OPS_ICONS.key}${this.escapeHtml(I18n.__('credentialsPasswordNever'))}</span>`;
        }
        // The date is the useful half of this cell: "set" alone does not tell an admin
        // whether the worker is holding a password from last week or from last year.
        const changed = user.password_changed_at
            ? `<span class="ui-fact-sub">${this.escapeHtml(I18n.__('credentialsChanged'))} ${this.escapeHtml(user.password_changed_at)}</span>`
            : '';
        return `<span class="ui-stack is-tight"><span class="ui-badge is-quiet">${this.OPS_ICONS.key}${this.escapeHtml(I18n.__('credentialsPasswordSet'))}</span>${changed}</span>`;
    },

    sessionsRevoked(user) {
        const count = Number(user.sessions_revoked) || 0;
        return count > 0 ? `${count}` : '0';
    },

    /**
     * Whether the signed-in admin may set this account's password.
     *
     * The server refuses a standard admin touching an administrator's password
     * (``/admin/users/edit_password``), and the button is hidden for the same reason:
     * an action that always answers 403 is not a button, it is a trap.
     */
    canSetCredentials(user) {
        const actor = State.user || {};
        const privilegedTarget = user.role === 'admin' || user.role === 'head_admin';
        return !(actor.role === 'admin' && privilegedTarget);
    },

    /**
     * Whether this admin may edit, deactivate or delete the account.
     *
     * The same rule as the password, because it is the same question. A standard admin
     * may not act on an administrator's account at all - ``/admin/users/edit``,
     * ``/admin/users/status`` and ``/admin/users/delete`` each answer 403 - so the
     * buttons are absent rather than present-and-refused, on the same reasoning: removing
     * an administrator is privilege escalation whichever verb it is wearing. The server
     * enforces it either way, because a hidden button has never stopped anybody.
     */
    canManageAccount(user) {
        return this.canSetCredentials(user);
    },

    /** ``active`` / ``inactive`` as the row publishes it, so a test reads no Tailwind. */
    accountStatus(user) {
        return String(user.status || 'active').trim().toLowerCase() === 'active'
            ? 'active' : 'inactive';
    },

    accountStatusHtml(user) {
        return this.accountStatus(user) === 'active'
            ? `<span class="ui-badge is-ok">${this.escapeHtml(I18n.__('credentialsActive'))}</span>`
            : `<span class="ui-badge is-danger">${this.escapeHtml(I18n.__('credentialsInactive'))}</span>`;
    },

    credentialsById(userId) {
        return (this._credentials || []).find(
            (user) => String(user.id) === String(userId)
        ) || null;
    },

    /**
     * The row's actions: set the password, edit the account, deactivate it, delete it.
     *
     * Four buttons is a lot for one cell, and each of them is a different answer to "this
     * account is wrong": a password nobody knows, a name or a rate typed wrong, a worker
     * who has left but whose hours are still owed, and an account that should never have
     * existed. The last two are not the same button on purpose - see ``deleteUser``.
     */
    credentialsActionHtml(user, compact) {
        if (!this.canManageAccount(user)) {
            return `<span class="ui-badge is-quiet" data-protected="true">${this.OPS_ICONS.shield}${this.escapeHtml(I18n.__('credentialsAdminProtected'))}</span>`;
        }
        if (String(user.id) === String(this._credentialsTarget)) return '';
        const id = this.escapeHtml(user.id);
        const inactive = this.accountStatus(user) !== 'active';
        // Your own row gets no way to switch yourself off: the server refuses it (the only
        // account that could be the last head admin is the one asking), so offering the
        // button would be offering a request that always fails. Editing yourself is fine -
        // a name or a rate is not a lockout.
        const isSelf = String(user.id) === String((State.user || {}).id);
        const statusKey = inactive ? 'credentialsReactivate' : 'credentialsDeactivate';
        // Two shapes of the same four actions. The roster is nine columns wide, and four
        // labelled buttons in the last one wrapped onto two lines and doubled the height of
        // every row on the screen - so in the table the three secondary actions are icons
        // with a tooltip and an accessible name, and on the phone card, where there is room,
        // they keep their words. The attribute values do not move either way: which action
        // a button carries is the contract, and how it is drawn is not.
        const action = (attr, value, label, handler, className, icon) => {
            const name = this.escapeHtml(label);
            const attributes = `${attr}="${value}" onclick="${handler}"`;
            return compact
                ? `<button type="button" ${attributes} class="${className} is-icon" title="${name}" aria-label="${name}">${icon}</button>`
                : `<button type="button" ${attributes} class="${className}">${icon}${name}</button>`;
        };
        const setPassword = action('data-set-password', id, I18n.__('credentialsSetPassword'),
            `UI_MODULES.openCredentialsPanel('${id}')`, 'ui-btn ui-btn-sm ui-btn-primary', this.OPS_ICONS.key);
        const edit = action('data-edit-user', id, I18n.__('credentialsEdit'),
            `UI_MODULES.openUserEdit('${id}')`, 'ui-btn ui-btn-sm', this.OPS_ICONS.pencil);
        const status = isSelf
            ? `<span class="ui-badge is-quiet" data-self="true">${this.escapeHtml(I18n.__('credentialsSelfAccount'))}</span>`
            : action('data-user-status', inactive ? 'activate' : 'deactivate', I18n.__(statusKey),
                `UI_MODULES.setUserStatus('${id}', ${inactive ? 'true' : 'false'})`,
                'ui-btn ui-btn-sm', this.OPS_ICONS.power);
        // Your own row also gets no Delete: it is the same lockout as switching yourself
        // off - the only account that could be the last head admin is the one asking - and
        // the server refuses it for the same reason. What your own row keeps is the hint
        // that says so, so the missing buttons are explained rather than merely absent.
        const remove = isSelf ? '' : action('data-delete-user', id, I18n.__('credentialsDelete'),
            `UI_MODULES.deleteUser('${id}')`, 'ui-btn ui-btn-sm ui-btn-danger', this.OPS_ICONS.trash);
        // ``nowrap`` belongs to the table, not to the actions: the roster is nine columns
        // wide and four labelled buttons in the last one wrapped onto two lines, doubling
        // every row's height. On the phone card there is no table to keep on one line, and
        // holding the row to 426px inside a 320px screen made the *whole page* 472px wide -
        // a horizontal scrollbar on the credentials screen and a tab bar that stopped short
        // of the content. The card lets it wrap, which is what a card is for.
        const rowStyle = compact
            ? 'flex-wrap:nowrap;justify-content:flex-end;gap:6px'
            : 'justify-content:flex-end;gap:6px';
        return `<span class="ui-row" style="${rowStyle}">${setPassword}${edit}${status}${remove}</span>`;
    },

    /** The account being edited, looked up in the roster on screen. */
    credentialsTarget() {
        if (!this._credentialsTarget) return null;
        return (this._credentials || []).find(
            (user) => String(user.id) === String(this._credentialsTarget)
        ) || null;
    },

    /**
     * The set-password panel, or a saved password.
     *
     * After a save the panel keeps the password on screen instead of hiding it: this
     * is the only moment it is readable, and the admin who set it is the one who has
     * to read it out to the worker.
     */
    credentialsPanelHtml() {
        // One panel, one accent stripe down the start edge, and the password itself in the
        // monospace stack so a character an admin is about to read out loud cannot be
        // mistaken for its neighbour.
        const mono = 'font-family:var(--ops-mono)';
        const revealed = this._credentialsRevealed;
        if (revealed) {
            return `
                <div class="ui-card is-accent" data-password-reveal="${this.escapeHtml(revealed.id)}" style="margin-bottom:16px">
                    <div class="ui-section-head">
                        <h3 class="ui-section-title">${this.escapeHtml(I18n.__('credentialsSaved'))}</h3>
                        <span class="ui-badge is-ok">${this.OPS_ICONS.key}${this.escapeHtml(I18n.__('credentialsRevealOnce'))}</span>
                    </div>
                    <p class="ui-section-note" style="margin-top:4px">${this.escapeHtml(I18n.__('credentialsRevealNote'))}</p>
                    <div class="ui-row" style="margin-top:12px">
                        <input id="credentialsRevealed" readonly value="${this.escapeHtml(revealed.password)}" class="ui-field is-flex" style="${mono}">
                        <button type="button" onclick="UI_MODULES.copyCredentialsPassword()" class="ui-btn">${this.OPS_ICONS.copy}${this.escapeHtml(I18n.__('credentialsCopyPassword'))}</button>
                        <button type="button" onclick="UI_MODULES.closeCredentialsPanel()" class="ui-btn ui-btn-quiet">${this.escapeHtml(I18n.__('close'))}</button>
                    </div>
                    <p class="ui-fact-sub" style="margin-top:8px">${this.escapeHtml(revealed.name)} (${this.escapeHtml(revealed.id)})</p>
                </div>`;
        }
        const target = this.credentialsTarget();
        if (!target) return '';
        const actor = State.user || {};
        const warning = String(target.id) === String(actor.id)
            ? I18n.__('credentialsSelfSignOut')
            : I18n.__('credentialsSignsOut');
        return `
            <div class="ui-card is-accent" data-password-panel="${this.escapeHtml(target.id)}" style="margin-bottom:16px">
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('credentialsSetFor'))} ${this.escapeHtml(target.name || target.id)} (${this.escapeHtml(target.id)})</h3>
                <div class="ui-row" style="margin-top:12px">
                    <input type="text" id="credentialsNewPassword" value="${this.escapeHtml(this._credentialsPassword)}"
                           oninput="UI_MODULES.setCredentialsPassword(this.value)" autocomplete="new-password"
                           placeholder="${this.escapeHtml(I18n.__('credentialsPasswordPlaceholder'))}" class="ui-field is-flex" style="${mono}">
                    <button type="button" onclick="UI_MODULES.regenerateCredentialsPassword()" class="ui-btn ui-btn-sm">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('credentialsRegenerate'))}</button>
                    <button type="button" onclick="UI_MODULES.copyCredentialsPassword()" class="ui-btn ui-btn-sm">${this.OPS_ICONS.copy}${this.escapeHtml(I18n.__('credentialsCopyPassword'))}</button>
                </div>
                <div class="ui-row" style="margin-top:12px">
                    <button type="button" onclick="UI_MODULES.saveCredentialsPassword()" class="ui-btn ui-btn-primary">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('save'))}</button>
                    <button type="button" onclick="UI_MODULES.closeCredentialsPanel()" class="ui-btn ui-btn-quiet">${this.escapeHtml(I18n.__('cancel'))}</button>
                </div>
                <p class="ui-section-note" style="margin-top:8px">${this.escapeHtml(I18n.__('credentialsPasswordManual'))}</p>
                <p class="ui-section-note" style="margin-top:4px">${this.escapeHtml(warning)}</p>
            </div>`;
    },

    /** Opens the panel for an account, with a password already generated. */
    openCredentialsPanel(userId) {
        this._credentialsRevealed = null;
        this._credentialsTarget = String(userId);
        this._credentialsPassword = this.generatePassword();
        return this.repaintCredentialsFromCache();
    },

    /**
     * Records the password the admin typed into the field.
     *
     * No repaint on purpose: this runs on every keystroke, and re-rendering the input the
     * caret is in would send the caret to the end after each character typed. What it
     * writes is what ``saveCredentialsPassword`` sends, so the box on screen - generated
     * or typed - is exactly the password that gets saved.
     */
    setCredentialsPassword(value) {
        this._credentialsPassword = String(value === null || value === undefined ? '' : value);
    },

    regenerateCredentialsPassword() {
        this._credentialsPassword = this.generatePassword();
        return this.repaintCredentialsFromCache();
    },

    closeCredentialsPanel() {
        this._credentialsTarget = null;
        this._credentialsPassword = '';
        // Closing is also how the password stops being readable: it was never stored
        // anywhere, so forgetting it here is the whole of the cleanup.
        this._credentialsRevealed = null;
        return this.repaintCredentialsFromCache();
    },

    /**
     * Copies the password on screen - the one just saved, the one just created, or the
     * one about to be set.
     */
    copyCredentialsPassword() {
        const value = this._credentialsCreated
            ? this._credentialsCreated.password
            : (this._credentialsRevealed ? this._credentialsRevealed.password : this._credentialsPassword);
        if (!value) {
            Toast.error(I18n.__('credentialsPasswordRequired'));
            return;
        }
        const done = () => Toast.success(I18n.__('copied'));
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(value).then(done).catch(() => prompt(I18n.__('credentialsCopyPassword'), value));
        } else {
            prompt(I18n.__('credentialsCopyPassword'), value);
        }
    },

    /**
     * Sets the shown password for the chosen account.
     *
     * ``/admin/users/edit_password`` also bumps the account's ``token_version``, so
     * every existing session - including one already on a phone - stops working. That
     * is the point of a reset, and it is why the panel says so before the button.
     */
    async saveCredentialsPassword() {
        const target = this.credentialsTarget();
        if (!target) return;
        const password = this._credentialsPassword;
        if (!password) {
            Toast.error(I18n.__('credentialsPasswordRequired'));
            return;
        }
        const content = document.getElementById('adminContent');
        try {
            await API.request('/admin/users/edit_password', {
                method: 'POST',
                body: { worker_id: target.id, new_password: password, admin_id: State.user.id }
            });
            this._credentialsRevealed = { id: target.id, name: target.name || target.id, password: password };
            this._credentialsTarget = null;
            this._credentialsPassword = '';
            Toast.success(I18n.__('credentialsSaved'));
            // Reload so the row's password state and revoked-session count describe the
            // save that just happened, not the situation before it.
            await this.loadCredentials(content);
        } catch (err) {
            Toast.error(err.message);
        }
    },

    // -----------------------------------------------------------------
    //  Credentials - changing an account that already exists
    //
    //  Three corrections the console could not make before: a name or a rate that was
    //  typed wrong, an account that must stop being usable, and an account that should
    //  never have existed. They live on the row, because the row is where the mistake is
    //  noticed - an admin reading the roster, not an admin on a settings screen.
    //
    //  What none of them can do is change an account's *id*. The id is the key every
    //  attendance row, punch, device key and audit entry is written against, so a "new"
    //  id would not be the same person with a corrected number: it would be a different
    //  person, and the first one's history would be left behind naming somebody who no
    //  longer exists. A typo'd id is deleted and re-created - which is the case Delete
    //  is for.
    // -----------------------------------------------------------------

    /** The account being edited, as read from the server, or ``null``. */
    _credentialsEdit: null,

    /** The edit form's fields, kept so a repaint never loses what was typed. */
    _credentialsEditDraft: { name: '', email: '', phone: '', hourly_rate: '' },

    /**
     * Opens the edit form for one account, with the account read from the server.
     *
     * Read rather than taken from the row on screen, because the roster payload is a
     * fixed contract that does not carry the hourly rate - which is one of the fields
     * this form exists to change. One request, on the click that needs it.
     *
     * The password panel is closed on the way in: two panels open on one tab is how a
     * password ends up being set for the account whose name the admin was reading.
     */
    async openUserEdit(userId) {
        this._credentialsTarget = null;
        this._credentialsPassword = '';
        this._credentialsRevealed = null;
        try {
            const user = await API.request(`/admin/users/${encodeURIComponent(userId)}`);
            this._credentialsEdit = user;
            this._credentialsEditDraft = {
                name: user.name || '',
                email: user.email || '',
                phone: user.phone || '',
                hourly_rate: user.hourly_rate === null || user.hourly_rate === undefined
                    ? '' : String(user.hourly_rate)
            };
        } catch (err) {
            Toast.error(err.message);
            this._credentialsEdit = null;
        }
        return this.repaintCredentialsFromCache();
    },

    closeUserEdit() {
        this._credentialsEdit = null;
        this._credentialsEditDraft = { name: '', email: '', phone: '', hourly_rate: '' };
        return this.repaintCredentialsFromCache();
    },

    /** The form's current values, read back so a repaint cannot lose a typed name. */
    readUserEditDraft() {
        if (!this._credentialsEdit) return this._credentialsEditDraft;
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value) : null;
        };
        ['name', 'email', 'phone'].forEach((key) => {
            const raw = value(`userEdit${key.charAt(0).toUpperCase()}${key.slice(1)}`);
            if (raw !== null) this._credentialsEditDraft[key] = raw.trim();
        });
        const rate = value('userEditRate');
        if (rate !== null) this._credentialsEditDraft.hourly_rate = rate.trim();
        return this._credentialsEditDraft;
    },

    credentialsEditPanelHtml() {
        const user = this._credentialsEdit;
        if (!user) return '';
        const draft = this._credentialsEditDraft;
        const box = 'ui-card is-stacked is-flat';
        const field = this.credentialsFieldClass();
        const label = 'ui-label';
        const quiet = 'ui-btn';
        return `
            <div class="${box}" data-user-edit="${this.escapeHtml(user.id)}">
                <p class="ui-card-title">${I18n.__('credentialsEditFor')} ${this.escapeHtml(user.name || user.id)} (${this.escapeHtml(user.id)})</p>
                <p class="ui-note">${I18n.__('credentialsEditIdNote')}</p>
                <div class="ui-grid two">
                    <input type="text" id="userEditName" value="${this.escapeHtml(draft.name)}"
                           placeholder="${I18n.__('name')}" class="${field}">
                    <input type="text" id="userEditEmail" value="${this.escapeHtml(draft.email)}"
                           placeholder="${I18n.__('emailOrPhone')}" class="${field}">
                    <input type="text" id="userEditPhone" value="${this.escapeHtml(draft.phone)}"
                           placeholder="${I18n.__('phone')}" class="${field}">
                    <p class="ui-note is-panel" data-user-edit-role="${this.escapeHtml(user.role || '')}">
                        ${I18n.__('role')}: <b>${this.escapeHtml(this.roleLabel(user.role))}</b>
                    </p>
                    <label class="ui-stack is-flush ui-span-all">
                        <span class="${label}">${I18n.__('credentialsHourlyRate')}</span>
                        <input type="number" id="userEditRate" step="0.5" min="0" max="1000"
                               value="${this.escapeHtml(draft.hourly_rate)}" class="${field}">
                    </label>
                </div>
                <p class="ui-note">${I18n.__('credentialsHourlyRateHint')}</p>
                <div class="ui-row">
                    <button type="button" onclick="UI_MODULES.saveUserEdit()" class="ui-btn ui-btn-primary">${I18n.__('save')}</button>
                    <button type="button" onclick="UI_MODULES.closeUserEdit()" class="${quiet}">${I18n.__('cancel')}</button>
                </div>
            </div>`;
    },

    /**
     * Saves the edit, and shows the server's refusal as the reason it gave.
     *
     * The refusals are all actionable and all different ("Name must not be empty.",
     * "Standard Admins cannot edit administrator accounts.", "Worker ID must be in range
     * 1-499 for role 'worker'."), so none of them is replaced with a generic failure: an
     * admin who fixes what the message named gets a saved account on the next click.
     */
    async saveUserEdit() {
        const user = this._credentialsEdit;
        if (!user) return;
        const draft = this.readUserEditDraft();
        // Exactly the fields the server accepts, and no id or role among them: see the
        // section comment above. Sending either would be ignored at best.
        const body = {
            user_id: user.id,
            name: draft.name,
            email: draft.email,
            phone: draft.phone
        };
        // An empty rate box means "leave the rate as it is"; a 0 means "clear it". The
        // difference matters - the server treats them differently - so a value that is
        // neither is refused here rather than sent as something the admin did not mean.
        if (String(draft.hourly_rate).trim() !== '') {
            const rate = Number(draft.hourly_rate);
            if (!Number.isFinite(rate) || rate < 0 || rate > 1000) {
                Toast.error(I18n.__('credentialsRateInvalid'));
                return;
            }
            body.hourly_rate = rate;
        }
        try {
            await API.request('/admin/users/edit', { method: 'POST', body });
            Toast.success(I18n.__('credentialsUserSaved'));
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        this._credentialsEdit = null;
        await this.loadCredentials(document.getElementById('adminContent'));
    },

    /**
     * Deletes an account, and reports the reason when the server refuses.
     *
     * The refusal is the interesting case, and it is not an error: an account with
     * attendance records is not deletable, because those shifts are what an approved
     * report is paid from. The server answers with the count and points at deactivation,
     * and that message is shown as it came - an admin told "no" without being told why
     * will simply try again.
     */
    async deleteUser(userId) {
        const user = this.credentialsById(userId) || { id: userId };
        const named = `${user.name || userId} (${userId})`;
        if (!confirm(I18n.__('credentialsDeleteConfirm').replace('{account}', named))) return;
        try {
            const answer = await API.request('/admin/users/delete', {
                method: 'POST', body: { user_id: userId }
            });
            // The confirmation is the console's own words, because the panel is read in
            // one of three languages and the server answers in one. The server's sentence
            // is still shown when it carries something the console cannot say: a face
            // template that could not be removed from disk.
            Toast.success(I18n.__('credentialsUserDeleted'));
            if ((answer.face_removal_failed || []).length > 0) Toast.error(answer.message);
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        await this.loadCredentials(document.getElementById('adminContent'));
    },

    /**
     * Deactivates an account, or brings it back.
     *
     * Deactivating is the answer for a worker who has left and has history: the account
     * can no longer sign in, every session and offline signing key it holds stops working
     * and its face reference is deleted, while the shifts it is paid for stay exactly
     * where they are. Reactivating restores the sign-in and nothing else - the template is
     * gone, so the row comes back marked "not enrolled" until somebody takes the photo
     * again. Both confirmations say what they cost before anything is sent.
     */
    async setUserStatus(userId, active) {
        const wanted = !!active;
        const key = wanted ? 'credentialsReactivateConfirm' : 'credentialsDeactivateConfirm';
        if (!confirm(I18n.__(key))) return;
        try {
            const answer = await API.request('/admin/users/status', {
                method: 'POST', body: { user_id: userId, active: wanted }
            });
            Toast.success(I18n.__(wanted ? 'credentialsReactivated' : 'credentialsDeactivated'));
            if ((answer.face_removal_failed || []).length > 0) Toast.error(answer.message);
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        await this.loadCredentials(document.getElementById('adminContent'));
    },

    // -----------------------------------------------------------------
    //  Credentials - starting an account
    //
    //  Two flows, one question. **New account** creates it here: the admin types the id,
    //  the name and the role, the password is generated, and a photo - if there is one -
    //  is turned into the face reference that lets this person clock in at all.
    //  **Registration link** hands the same job to the person it belongs to: a one-time
    //  link where they choose their own password and take their own photo. The id and
    //  the role stay the admin's choice, because whoever ends up holding the link must
    //  not be able to pick either.
    //
    //  The upload rule is the server's, restated here so a 6 MB photo is refused before
    //  it is uploaded over a phone tether: 5 MB, JPEG/PNG/WebP, nothing else. This is a
    //  courtesy and not the check - ``accept=`` and ``file.type`` are both supplied by
    //  whoever sends the request, so ``backend/uploads.py`` decides by reading the bytes.
    // -----------------------------------------------------------------

    /** The create form's fields, kept so that a repaint never loses what was typed. */
    _credentialsDraft: { id: '', name: '', role: 'worker', email: '', phone: '' },

    /** The registration-link form's fields, for the same reason. */
    _credentialsInviteDraft: { id: '', name: '', role: 'worker', email: '', phone: '' },

    openCredentialsMode(mode) {
        const wanted = String(mode || '');
        this.readCredentialsDraft();
        this.readInviteDraft();
        this._credentialsMode = this._credentialsMode === wanted ? '' : wanted;
        // A generated password, never an empty box: the admin has to read one out loud
        // either way, and the generator's output is the only kind that satisfies the
        // server's policy by construction rather than by luck.
        if (this._credentialsMode === 'create') {
            this._credentialsNewPassword = this._credentialsNewPassword || this.generatePassword();
        }
        return this.repaintCredentialsFromCache();
    },

    closeCredentialsMode() {
        this._credentialsMode = '';
        // The two readable secrets live in memory only, and closing is what forgets
        // them: a password never stored cannot leak from a later screen.
        this._credentialsCreated = null;
        this._credentialsIssued = null;
        this._credentialsNewPhoto = null;
        this._credentialsPhotoError = '';
        return this.repaintCredentialsFromCache();
    },

    /**
     * The roles this admin may hand out, and the id range each one owns.
     *
     * A standard admin cannot create administrators anywhere on the server, so the
     * option is absent rather than present-and-403 - the same reason the password button
     * is hidden for an administrator's row.
     */
    creatableRoles() {
        const actor = State.user || {};
        const roles = ['worker', 'moallem'];
        if (actor.role === 'head_admin') roles.push('admin', 'head_admin');
        return roles;
    },

    /** Roles a *link* may create - the server's narrower list, said out loud here. */
    linkRoles() {
        return ['worker', 'moallem'];
    },

    /** The id block a role owns, so a wrong id is caught before the round trip. */
    roleRange(role) {
        const ranges = {
            worker: { min: 1, max: 499 },
            moallem: { min: 500, max: 999 },
            admin: { min: 1000, max: 4999 },
            head_admin: { min: 5000, max: null }
        };
        return ranges[role] || ranges.worker;
    },

    roleRangeHint(role) {
        const range = this.roleRange(role);
        return range.max ? `${range.min}-${range.max}` : `${range.min}+`;
    },

    idFitsRole(userId, role) {
        const value = Number(String(userId).trim());
        if (!Number.isInteger(value)) return false;
        const range = this.roleRange(role);
        return value >= range.min && (range.max === null || value <= range.max);
    },

    credentialsFieldClass() {
        return 'ui-field';
    },

    photoSizeLabel(file) {
        const bytes = Number((file && file.size) || 0);
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    },

    /**
     * Why a chosen file cannot be used, or ``null`` when it can.
     *
     * Nothing chosen is not an error: an account with no face is a legitimate account,
     * it simply cannot clock in until a photo is registered for it.
     */
    checkPhotoFile(file) {
        if (!file) return null;
        const size = Number(file.size || 0);
        if (size <= 0) return I18n.__('credentialsPhotoUnreadable');
        if (size > this.PHOTO_POLICY.maxBytes) {
            return `${I18n.__('credentialsPhotoTooLarge')} (${this.photoSizeLabel(file)} / ${this.PHOTO_POLICY.maxMb} MB)`;
        }
        const type = String(file.type || '').toLowerCase();
        if (this.PHOTO_POLICY.accepted.indexOf(type) >= 0) return null;
        // Some pickers report an empty type for a perfectly good JPEG - usually the
        // camera app's own ``.jpg``. The extension is a hint only; the server still
        // decides from the bytes, so this cannot let a document through.
        const name = String(file.name || '').toLowerCase();
        if (!type && /\.(jpe?g|png|webp)$/.test(name)) return null;
        return I18n.__('credentialsPhotoWrongType');
    },

    pickCredentialsPhoto(input) {
        const chosen = input && input.files && input.files[0] ? input.files[0] : null;
        const problem = this.checkPhotoFile(chosen);
        this.readCredentialsDraft();
        this._credentialsPhotoError = problem || '';
        this._credentialsNewPhoto = problem ? null : chosen;
        if (problem) Toast.error(problem);
        return this.repaintCredentialsFromCache();
    },

    clearCredentialsPhoto() {
        this._credentialsNewPhoto = null;
        this._credentialsPhotoError = '';
        return this.repaintCredentialsFromCache();
    },

    credentialsRoleChanged(role) {
        this.readCredentialsDraft();
        if (this._credentialsMode === 'invite') {
            this._credentialsInviteDraft.role = role;
        } else {
            this._credentialsDraft.role = role;
        }
        return this.repaintCredentialsFromCache();
    },

    /** Copies the form's current values into the draft, if that form is on screen. */
    readCredentialsDraft() {
        if (this._credentialsMode !== 'create' || this._credentialsCreated) return this._credentialsDraft;
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value) : null;
        };
        ['id', 'name', 'email', 'phone'].forEach((key) => {
            const raw = value(`credentialsNew${key.charAt(0).toUpperCase()}${key.slice(1)}`);
            if (raw !== null) this._credentialsDraft[key] = raw.trim();
        });
        const role = value('credentialsNewRole');
        if (role && this.creatableRoles().indexOf(role) >= 0) this._credentialsDraft.role = role;
        return this._credentialsDraft;
    },

    readInviteDraft() {
        if (this._credentialsMode !== 'invite' || this._credentialsIssued) return this._credentialsInviteDraft;
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value) : null;
        };
        ['id', 'name', 'email', 'phone'].forEach((key) => {
            const raw = value(`credentialsLink${key.charAt(0).toUpperCase()}${key.slice(1)}`);
            if (raw !== null) this._credentialsInviteDraft[key] = raw.trim();
        });
        const role = value('credentialsLinkRole');
        if (role && this.linkRoles().indexOf(role) >= 0) this._credentialsInviteDraft.role = role;
        return this._credentialsInviteDraft;
    },

    /** The open create or link panel, or ``''`` when neither is.
     *
     * Rendered above the roster rather than in a modal, so the account being created and
     * the accounts that already exist are on one screen - the admin can see the id is
     * free while typing it.
     */
    credentialsCreatorHtml() {
        if (this._credentialsMode === 'create') return this.credentialsCreateHtml();
        if (this._credentialsMode === 'invite') return this.credentialsInviteHtml();
        return '';
    },

    credentialsCreateHtml() {
        const box = 'ui-alert is-info is-stacked';
        const field = this.credentialsFieldClass();
        const quiet = 'ui-btn';
        const created = this._credentialsCreated;
        if (created) {
            return `
                <div class="${box}" data-account-created="${this.escapeHtml(created.id)}">
                    <p class="ui-card-title">${I18n.__('credentialsCreatedTitle')}</p>
                    <p class="ui-note is-body">${I18n.__(created.face_enrolled ? 'credentialsCreatedFace' : 'credentialsCreatedNoFace')}</p>
                    <div class="ui-row">
                        <input id="credentialsCreatedPassword" readonly value="${this.escapeHtml(created.password)}" class="${field} ui-mono is-flex">
                        <button type="button" onclick="UI_MODULES.copyCredentialsPassword()" class="${quiet}">${I18n.__('credentialsCopyPassword')}</button>
                        <button type="button" onclick="UI_MODULES.closeCredentialsMode()" class="${quiet}">${I18n.__('close')}</button>
                    </div>
                    <p class="ui-note">${this.escapeHtml(created.name)} (${this.escapeHtml(created.id)})</p>
                </div>`;
        }
        const draft = this._credentialsDraft;
        const photo = this._credentialsNewPhoto;
        return `
            <div class="${box}" data-create-panel="true">
                <p class="ui-card-title">${I18n.__('credentialsNewAccount')}</p>
                <p class="ui-note is-body">${I18n.__('credentialsNewAccountHint')}</p>
                <div class="ui-grid two">
                    <input type="text" id="credentialsNewId" value="${this.escapeHtml(draft.id)}" inputmode="numeric"
                           placeholder="${I18n.__('credentialsNewId')}" class="${field}">
                    <input type="text" id="credentialsNewName" value="${this.escapeHtml(draft.name)}"
                           placeholder="${I18n.__('name')}" class="${field}">
                    <select id="credentialsNewRole" onchange="UI_MODULES.credentialsRoleChanged(this.value)" class="${field}">
                        ${this.creatableRoles().map((role) => `<option value="${role}" ${role === draft.role ? 'selected' : ''}>${this.escapeHtml(this.roleLabel(role))}</option>`).join('')}
                    </select>
                    <input type="text" id="credentialsNewEmail" value="${this.escapeHtml(draft.email)}"
                           placeholder="${I18n.__('emailOrPhone')}" class="${field}">
                    <input type="text" id="credentialsNewPhone" value="${this.escapeHtml(draft.phone)}"
                           placeholder="${I18n.__('phone')}" class="${field}">
                    <div class="ui-row">
                        <input type="text" id="credentialsNewPasswordShown" value="${this.escapeHtml(this._credentialsNewPassword)}"
                               oninput="UI_MODULES.setCredentialsNewPassword(this.value)" autocomplete="new-password"
                               placeholder="${this.escapeHtml(I18n.__('credentialsPasswordPlaceholder'))}" class="${field} ui-mono is-flex">
                        <button type="button" onclick="UI_MODULES.regenerateCredentialsNewPassword()" class="${quiet}">${I18n.__('credentialsRegenerate')}</button>
                    </div>
                    <p class="ui-note" data-password-hint>${this.escapeHtml(I18n.__('credentialsPasswordManual'))}</p>
                </div>
                <p class="ui-note" data-id-range="${this.escapeHtml(draft.role)}">
                    ${I18n.__('credentialsIdRange')}: ${this.roleRangeHint(draft.role)}
                </p>
                <div class="ui-row">
                    <input type="file" id="credentialsNewPhoto" accept="image/jpeg,image/png,image/webp"
                           onchange="UI_MODULES.pickCredentialsPhoto(this)" class="ui-field">
                    ${photo ? `<span class="ui-note" data-photo-chosen>${this.escapeHtml(photo.name || '')} · ${this.photoSizeLabel(photo)}</span>
                        <button type="button" onclick="UI_MODULES.clearCredentialsPhoto()" class="${quiet}">${I18n.__('credentialsPhotoRemove')}</button>` : ''}
                </div>
                <p class="ui-note">${I18n.__('credentialsPhotoHint')}</p>
                ${this._credentialsPhotoError ? `<p class="ui-note is-danger" data-photo-error>${this.escapeHtml(this._credentialsPhotoError)}</p>` : ''}
                <div class="ui-row">
                    <button type="button" onclick="UI_MODULES.createCredentialsAccount()" class="ui-btn ui-btn-primary">${I18n.__('credentialsCreateAccount')}</button>
                    <button type="button" onclick="UI_MODULES.closeCredentialsMode()" class="${quiet}">${I18n.__('cancel')}</button>
                </div>
            </div>`;
    },

    credentialsInviteHtml() {
        const box = 'ui-alert is-info is-stacked';
        const field = this.credentialsFieldClass();
        const quiet = 'ui-btn';
        const issued = this._credentialsIssued;
        if (issued) {
            return `
                <div class="${box}" data-link-issued="${this.escapeHtml(issued.worker_id || '')}">
                    <p class="ui-card-title">${I18n.__('credentialsLinkTitle')}</p>
                    <p class="ui-note is-body">${I18n.__('credentialsLinkOnce')}</p>
                    <div class="ui-row">
                        <input id="credentialsLinkUrl" readonly value="${this.escapeHtml(issued.url)}" class="${field} ui-mono is-flex">
                        <button type="button" onclick="UI_MODULES.copyCredentialsLink()" class="${quiet}">${I18n.__('copyLink')}</button>
                        <button type="button" onclick="UI_MODULES.shareCredentialsLink()" class="${quiet}">${I18n.__('credentialsLinkWhatsApp')}</button>
                    </div>
                    ${issued.qr_png_data_uri ? `<img src="${this.escapeHtml(issued.qr_png_data_uri)}" alt="${I18n.__('credentialsLinkQr')}" class="ui-qr">` : ''}
                    <p class="ui-note">${this.escapeHtml(issued.worker_name || '')} (${this.escapeHtml(issued.worker_id || '')}) · ${I18n.__('credentialsLinkExpires')} ${this.escapeHtml(issued.expires_at || '')}</p>
                    <button type="button" onclick="UI_MODULES.closeCredentialsMode()" class="${quiet}">${I18n.__('close')}</button>
                </div>`;
        }
        const draft = this._credentialsInviteDraft;
        return `
            <div class="${box}" data-invite-panel="true">
                <p class="ui-card-title">${I18n.__('credentialsLinkTitle')}</p>
                <p class="ui-note is-body">${I18n.__('credentialsLinkHint')}</p>
                <div class="ui-grid two">
                    <input type="text" id="credentialsLinkId" value="${this.escapeHtml(draft.id)}" inputmode="numeric"
                           placeholder="${I18n.__('credentialsNewId')}" class="${field}">
                    <input type="text" id="credentialsLinkName" value="${this.escapeHtml(draft.name)}"
                           placeholder="${I18n.__('name')}" class="${field}">
                    <select id="credentialsLinkRole" onchange="UI_MODULES.credentialsRoleChanged(this.value)" class="${field}">
                        ${this.linkRoles().map((role) => `<option value="${role}" ${role === draft.role ? 'selected' : ''}>${this.escapeHtml(this.roleLabel(role))}</option>`).join('')}
                    </select>
                    <input type="text" id="credentialsLinkEmail" value="${this.escapeHtml(draft.email)}"
                           placeholder="${I18n.__('emailOrPhone')}" class="${field}">
                    <input type="text" id="credentialsLinkPhone" value="${this.escapeHtml(draft.phone)}"
                           placeholder="${I18n.__('phone')}" class="${field}">
                </div>
                <p class="ui-note" data-id-range="${this.escapeHtml(draft.role)}">
                    ${I18n.__('credentialsIdRange')}: ${this.roleRangeHint(draft.role)}
                </p>
                <div class="ui-row">
                    <button type="button" onclick="UI_MODULES.issueCredentialsLink()" class="ui-btn ui-btn-primary">${I18n.__('credentialsLinkCreate')}</button>
                    <button type="button" onclick="UI_MODULES.closeCredentialsMode()" class="${quiet}">${I18n.__('cancel')}</button>
                </div>
            </div>`;
    },

    /**
     * Records the password typed into the new-account form.
     *
     * State rather than the DOM, because the form repaints for other reasons - picking a
     * photo or changing the role re-renders every field - and a password typed here must
     * survive that. Same rule as the reset panel: no repaint from this handler, so the
     * caret stays where the admin is typing.
     */
    setCredentialsNewPassword(value) {
        this._credentialsNewPassword = String(value === null || value === undefined ? '' : value);
    },

    regenerateCredentialsNewPassword() {
        this.readCredentialsDraft();
        this._credentialsNewPassword = this.generatePassword();
        return this.repaintCredentialsFromCache();
    },

    /**
     * Creates the account, with its face reference when a photo was chosen.
     *
     * The roster is reloaded on success, so the row that appears is the server's answer
     * rather than the form's promise - if the id turned out to be taken, or the photo
     * held no face, that is what the admin needs to be looking at.
     */
    async createCredentialsAccount() {
        const draft = this.readCredentialsDraft();
        const password = this._credentialsNewPassword;
        if (!draft.id || !draft.name) {
            Toast.error(I18n.__('credentialsCreateNeedsIdAndName'));
            return;
        }
        if (!this.idFitsRole(draft.id, draft.role)) {
            Toast.error(`${I18n.__('credentialsIdRange')}: ${this.roleRangeHint(draft.role)}`);
            return;
        }
        if (!password) {
            Toast.error(I18n.__('credentialsPasswordRequired'));
            return;
        }
        const form = new FormData();
        form.append('user_id', draft.id);
        form.append('name', draft.name);
        form.append('role', draft.role);
        form.append('password', password);
        form.append('email', draft.email);
        form.append('phone', draft.phone);
        if (this._credentialsNewPhoto) {
            form.append('photo', this._credentialsNewPhoto, this._credentialsNewPhoto.name || 'photo.jpg');
        }
        try {
            const answer = await API.request('/admin/users/create', { method: 'POST', body: form });
            this._credentialsCreated = {
                id: answer.user_id || draft.id,
                name: draft.name,
                password: password,
                face_enrolled: !!answer.face_enrolled
            };
            this._credentialsNewPassword = '';
            this._credentialsNewPhoto = null;
            this._credentialsPhotoError = '';
            this._credentialsDraft = { id: '', name: '', role: draft.role, email: '', phone: '' };
            Toast.success(I18n.__('credentialsCreatedToast'));
            await this.loadCredentials(document.getElementById('adminContent'));
        } catch (err) {
            Toast.error(err.message);
        }
    },

    /**
     * Issues the one-time registration link.
     *
     * The token comes back exactly once and is not stored in readable form anywhere - it
     * is a hash in ``enrollment_invites`` - so the panel keeps it on screen until it is
     * closed, and there is no "show it again" button to offer.
     */
    async issueCredentialsLink() {
        const draft = this.readInviteDraft();
        if (!draft.id || !draft.name) {
            Toast.error(I18n.__('credentialsLinkNeedsIdAndName'));
            return;
        }
        if (!this.idFitsRole(draft.id, draft.role)) {
            Toast.error(`${I18n.__('credentialsIdRange')}: ${this.roleRangeHint(draft.role)}`);
            return;
        }
        try {
            this._credentialsIssued = await API.request('/admin/enrollment/invites', {
                method: 'POST',
                body: {
                    worker_id: draft.id,
                    kind: 'register',
                    name: draft.name,
                    role: draft.role,
                    email: draft.email,
                    phone: draft.phone
                }
            });
            Toast.success(I18n.__('credentialsLinkReady'));
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        return this.repaintCredentialsFromCache();
    },

    copyCredentialsLink() {
        const issued = this._credentialsIssued;
        if (!issued || !issued.url) return;
        const done = () => Toast.success(I18n.__('copied'));
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(issued.url).then(done).catch(() => prompt(I18n.__('copyLink'), issued.url));
        } else {
            prompt(I18n.__('copyLink'), issued.url);
        }
    },

    /**
     * Opens WhatsApp with the link already in the message box.
     *
     * An anchor rather than ``window.open``: the app is installed to the home screen on
     * these phones, and a popup opened from a non-gesture context is the one thing a
     * mobile browser reliably blocks. If messages are not installed the link still
     * opens WhatsApp Web.
     */
    shareCredentialsLink() {
        const issued = this._credentialsIssued;
        if (!issued || !issued.url) return;
        const message = `${I18n.__('credentialsLinkMessage')} ${issued.url}`;
        const link = document.createElement('a');
        link.href = `https://wa.me/?text=${encodeURIComponent(message)}`;
        link.target = '_blank';
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        link.remove();
    },

    /**
     * A password an admin can read out over the phone.
     *
     * 16 characters from four classes, with the characters that get misheard or
     * mistyped left out on purpose: no ``O``/``0``, no ``I``/``l``/``1``. It has to
     * satisfy the server's policy (length plus not-a-common-word), which this does by
     * construction rather than by hope.
     */
    generatePassword() {
        const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
        const lower = 'abcdefghijkmnopqrstuvwxyz';
        const digits = '23456789';
        const symbols = '-_!@#$%^&*';
        const all = upper + lower + digits + symbols;
        const pick = (set) => set.charAt(this.randomInt(set.length));
        const chars = [pick(upper), pick(lower), pick(digits), pick(symbols)];
        while (chars.length < 16) chars.push(pick(all));
        // Shuffled so the four guaranteed classes are not always in the same positions.
        for (let i = chars.length - 1; i > 0; i--) {
            const j = this.randomInt(i + 1);
            const swap = chars[i];
            chars[i] = chars[j];
            chars[j] = swap;
        }
        return chars.join('');
    },

    /** Uniform in [0, limit) - crypto when the browser has it, arithmetic otherwise. */
    randomInt(limit) {
        if (typeof crypto !== 'undefined' && crypto && crypto.getRandomValues) {
            const range = 4294967296;
            const max = range - (range % limit);
            const buffer = new Uint32Array(1);
            let value = range;
            while (value >= max) {
                crypto.getRandomValues(buffer);
                value = buffer[0];
            }
            return value % limit;
        }
        return Math.floor(Math.random() * limit);
    },

    /** Role codes are the wire's, the labels are the reader's. */
    roleLabel(role) {
        const keys = {
            worker: 'roleWorker',
            moallem: 'roleMoallem',
            admin: 'roleAdmin',
            head_admin: 'roleHeadAdmin'
        };
        return keys[role] ? I18n.__(keys[role]) : String(role || '');
    },

    /**
     * The shift rules this panel was last painted from, or ``null`` before the first
     * paint. ``saveShiftRules`` falls back to them, never to a hardcoded number.
     */
    _shiftRules: null,

    /**
     * The Admin tab: the rules a shift is measured by, then the admin roster.
     *
     * The shift rules are here rather than in the API docs because they are the numbers
     * a payroll question turns on - what a day is worth, how much of it is an unpaid
     * break, and whether the day ends by itself - and until now the only way to change
     * them was a ``curl``.
     */
    async renderAdminManagement(content, knownRules) {
        // ``knownRules`` is passed back after a save, so the panel repaints from the rules
        // the server stored instead of spending a second round trip re-reading them.
        let rules = knownRules || null;
        try {
            rules = rules || await API.request('/admin/shift_rules');
        } catch (err) {
            Toast.error(err.message);
        }
        // The rules this panel was painted from. ``saveShiftRules`` reads a field back from
        // here when the box itself cannot answer (see that method for why).
        this._shiftRules = rules;
        content.innerHTML = `
            <div class="ui-page" data-admin-panel="true">
                ${this.shiftRulesHtml(rules)}
                ${this.createAdminHtml()}
            </div>`;
        const rulesForm = document.getElementById('shiftRulesForm');
        if (rulesForm) rulesForm.onsubmit = (event) => this.saveShiftRules(event);
        const adminForm = document.getElementById('addAdminForm');
        if (adminForm) adminForm.onsubmit = (event) => this.addAdmin(event);
    },

    /**
     * The second job this tab has: creating another administrator.
     *
     * It was three unlabelled boxes under an "Admin Name / Password / Admin ID
     * (1000+)" heading - placeholders doing a label's work, which vanish the moment
     * somebody types in them, and nothing at all saying what the ID range means. The
     * range is the whole permission model: 1000-4999 is an admin who cannot touch
     * another administrator, 5000 and above is a head admin who can.
     */
    createAdminHtml() {
        return `
            <section class="ui-card is-flat" aria-labelledby="createAdminTitle">
                <h3 class="ui-section-title" id="createAdminTitle">${this.escapeHtml(I18n.__('adminCreateTitle'))}</h3>
                <p class="ui-section-note" style="margin-top:4px">${this.escapeHtml(I18n.__('adminCreateHint'))}</p>
                <form id="addAdminForm" class="ui-grid three" style="margin-top:14px">
                    <div>
                        <label class="ui-label" for="adminId">${this.escapeHtml(I18n.__('adminId'))}</label>
                        <input type="text" id="adminId" class="ui-field" inputmode="numeric" required
                               placeholder="${this.escapeHtml(I18n.__('adminIdPlaceholder'))}">
                    </div>
                    <div>
                        <label class="ui-label" for="adminName">${this.escapeHtml(I18n.__('adminName'))}</label>
                        <input type="text" id="adminName" class="ui-field" autocomplete="off" required>
                    </div>
                    <div>
                        <label class="ui-label" for="adminPass">${this.escapeHtml(I18n.__('adminPassword'))}</label>
                        <input type="password" id="adminPass" class="ui-field" autocomplete="new-password" required>
                    </div>
                    <div style="grid-column:1/-1">
                        <button type="submit" class="ui-btn ui-btn-primary">${this.OPS_ICONS.shield}${this.escapeHtml(I18n.__('adminCreate'))}</button>
                    </div>
                </form>
            </section>`;
    },

    /** Create the account, or say why not. Same failure shape as the site form. */
    async addAdmin(event) {
        if (event && event.preventDefault) event.preventDefault();
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value) : '';
        };
        try {
            await API.request('/admin/admins/add', { method: 'POST', body: {
                user_id: value('adminId'),
                name: value('adminName'),
                password: value('adminPass'),
                role: 'admin',
                creator_id: State.user.id
            }});
        } catch (err) {
            // Same reason as the site form: "Admin ID must be in range 1000-4999." is the
            // only thing that tells the admin what to change, and it used to vanish.
            Toast.error(err.message);
            return;
        }
        // Was an ``alert()``, and the panel never repainted - so the account was created
        // and the form still showed the details, which is how the same admin gets made twice.
        Toast.success(I18n.__('adminCreated'));
        UI.renderAdminTab('Admin');
    },

    /**
     * The shift rules, with the arithmetic spelled out underneath them.
     *
     * The summary line is not decoration: "8 h paid, 30 min unpaid break" is only
     * meaningful next to the 8.5 h day it produces, and an administrator typing 45 into
     * the break box should be able to see what that does before saving it.
     */
    shiftRulesHtml(rules) {
        const value = (key, fallback) => (
            rules && rules[key] !== undefined && rules[key] !== null ? rules[key] : fallback
        );
        const regular = Number(value('regular_hours', 8));
        const minutes = Number(value('break_minutes', 30));
        const afterHours = Number(value('break_after_hours', 4));
        const overtimeAt = Number(value('overtime_notify_hours', 8.1));
        const autoClose = String(value('auto_close_at_regular', 1)) !== '0';
        const onSite = regular + (regular >= afterHours ? minutes / 60 : 0);
        return `
            <section class="ui-card is-flat" data-rules-panel="true" aria-labelledby="shiftRulesTitle">
                <h3 class="ui-section-title" id="shiftRulesTitle">${this.escapeHtml(I18n.__('shiftRules'))}</h3>
                <p class="ui-section-note" style="margin-top:4px">${this.escapeHtml(I18n.__('shiftRulesHint'))}</p>
                <p class="ui-section-note" data-rules-applies>${this.escapeHtml(I18n.__('adminRulesHint'))}</p>

                <!-- What the three inputs add up to, as figures rather than as a
                     sentence to be parsed. These are the numbers the board colours a
                     row by, so an administrator changing a rule sees the row change. -->
                <div class="ui-facts" style="margin-top:16px">
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('shiftRulesOnSiteDay'))}</span>
                        <span class="ui-fact-value" data-rules-fact="on-site">${this.escapeHtml(`${this.hoursLabel(onSite)} h`)}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('shiftRulesRegular'))}</span>
                        <span class="ui-fact-value" data-rules-fact="paid">${this.escapeHtml(`${this.hoursLabel(regular)} h`)}</span>
                    </div>
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('shiftRulesOvertimeAt'))}</span>
                        <span class="ui-fact-value" data-rules-fact="overtime">${this.escapeHtml(`${this.hoursLabel(overtimeAt)} h`)}</span>
                    </div>
                </div>

                <form id="shiftRulesForm" class="ui-grid three" style="margin-top:16px">
                    <div>
                        <label class="ui-label" for="rulesRegularHours">${this.escapeHtml(I18n.__('shiftRulesRegular'))}</label>
                        <input type="number" id="rulesRegularHours" class="ui-field" step="0.25" min="0.5" max="24"
                               value="${this.escapeHtml(regular)}">
                    </div>
                    <div>
                        <label class="ui-label" for="rulesBreakMinutes">${this.escapeHtml(I18n.__('shiftRulesBreak'))}</label>
                        <input type="number" id="rulesBreakMinutes" class="ui-field" step="5" min="0" max="240"
                               value="${this.escapeHtml(minutes)}">
                    </div>
                    <div>
                        <label class="ui-label" for="rulesBreakAfterHours">${this.escapeHtml(I18n.__('shiftRulesBreakAfter'))}</label>
                        <input type="number" id="rulesBreakAfterHours" class="ui-field" step="0.5" min="0" max="24"
                               value="${this.escapeHtml(afterHours)}">
                    </div>
                    <label class="ui-check" style="grid-column:1/-1">
                        <input type="checkbox" id="rulesAutoClose" ${autoClose ? 'checked' : ''}>
                        <span>${this.escapeHtml(I18n.__('shiftRulesAutoClose'))}</span>
                    </label>
                    <!-- The window a site follows when it has none of its own. Same three
                         fields as a site's, at the company scope, because "nobody here is
                         late" is a decision about this window on every site that inherits
                         it. Blank puts it back to the shipped 04:00-06:30. -->
                    <div style="grid-column:1/-1">
                        <p class="ui-section-note">${this.escapeHtml(I18n.__('shiftRulesWindowHint'))}</p>
                    </div>
                    <div>
                        <label class="ui-label" for="rulesWindowStart">${this.escapeHtml(I18n.__('sitesWindowStart'))}</label>
                        <input type="time" id="rulesWindowStart" class="ui-field" value="${this.escapeHtml(value('clock_in_window_start', ''))}">
                    </div>
                    <div>
                        <label class="ui-label" for="rulesWindowEnd">${this.escapeHtml(I18n.__('sitesWindowEnd'))}</label>
                        <input type="time" id="rulesWindowEnd" class="ui-field" value="${this.escapeHtml(value('clock_in_window_end', ''))}">
                    </div>
                    <div>
                        <label class="ui-label" for="rulesTimezone">${this.escapeHtml(I18n.__('sitesWindowTimezone'))}</label>
                        <input type="text" id="rulesTimezone" class="ui-field" list="siteTimezoneOptions"
                               placeholder="${this.escapeHtml(I18n.__('sitesWindowTimezonePlaceholder'))}"
                               value="${this.escapeHtml(value('site_timezone', ''))}">
                    </div>
                    <!-- The text-xs class is kept alongside the component class on purpose:
                         the product suite reads this line by that class, and the Tailwind
                         CDN is not always reachable on site, so the token class is the one
                         that has to survive the offline case. -->
                    <p class="ui-section-note" data-rules-summary="${onSite.toFixed(2)}" style="grid-column:1/-1">
                        ${this.escapeHtml(I18n.__('shiftRulesSummary'))}
                    </p>
                    <div style="grid-column:1/-1">
                        <button type="submit" class="ui-btn ui-btn-primary">${this.OPS_ICONS.sliders}${this.escapeHtml(I18n.__('save'))}</button>
                    </div>
                </form>
                ${this.siteTimezoneOptionsHtml()}
            </section>`;
    },

    /**
     * Saves the rules and repaints from the server's answer.
     *
     * The endpoint refuses nonsense (a negative break, a paid day of zero) and the
     * refusal is shown as it came: it names the field and the range, which is the only
     * thing that tells an administrator what to change.
     *
     * A field that does not answer falls back to the rule the panel was painted from, and
     * only then to the shipped default. Both halves of that are real: ``Number('')`` is 0,
     * so an emptied paid-hours box used to save a zero-hour paid day and a zero-minute
     * break - values the endpoint accepts, and which pay nobody for anything - and a
     * checkbox carries its state as a property rather than as the attribute the panel
     * writes, so "no answer" must not be read as "switched off", which is the difference
     * between keeping the automatic close and quietly losing it.
     */
    async saveShiftRules(event) {
        if (event && event.preventDefault) event.preventDefault();
        const loaded = this._shiftRules || {};
        const number = (id, key, fallback) => {
            const element = document.getElementById(id);
            const raw = element && element.value !== undefined && element.value !== null
                ? String(element.value).trim() : '';
            const value = raw === '' ? NaN : Number(raw);
            const remembered = Number(loaded[key]);
            if (Number.isFinite(value)) return value;
            return Number.isFinite(remembered) ? remembered : fallback;
        };
        const box = document.getElementById('rulesAutoClose');
        const rememberedClose = String(
            loaded.auto_close_at_regular === undefined || loaded.auto_close_at_regular === null
                ? 1 : loaded.auto_close_at_regular
        ) !== '0';
        const autoClose = box && typeof box.checked === 'boolean' ? box.checked : rememberedClose;
        // Trimmed text rather than a number, and ``''`` is allowed through, which is the
        // opposite of what ``number()`` does with an emptied numeric box. That asymmetry is
        // deliberate: a blank paid-hours box is somebody who cleared it by accident, and
        // saving 0 would pay nobody for anything - while a blank *time* box is the documented
        // way to say "back to the shipped 04:00-06:30", so falling back there would make the
        // company window impossible to take off again.
        const text = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined && element.value !== null
                ? String(element.value).trim() : '';
        };
        const body = {
            regular_hours: number('rulesRegularHours', 'regular_hours', 8),
            break_minutes: number('rulesBreakMinutes', 'break_minutes', 30),
            break_after_hours: number('rulesBreakAfterHours', 'break_after_hours', 4),
            auto_close_at_regular: autoClose ? 1 : 0,
            // What every site without a window of its own is judged by. Sent with the rest of
            // the form because this panel is the only place it can be changed.
            clock_in_window_start: text('rulesWindowStart'),
            clock_in_window_end: text('rulesWindowEnd'),
            site_timezone: text('rulesTimezone')
        };
        try {
            const answer = await API.request('/admin/shift_rules', { method: 'POST', body: body });
            Toast.success(I18n.__('shiftRulesSaved'));
            // Painted from the row the server echoed, not from what was typed: a value it
            // stored differently is a value the administrator has to see.
            const stored = answer && answer.rules ? answer.rules : loaded;
            await this.renderAdminManagement(document.getElementById('adminContent'), stored);
            return answer;
        } catch (err) {
            Toast.error(err.message);
            return null;
        }
    },

    // -----------------------------------------------------------------
    //  Shifts - a timesheet: one row per shift, for a period the admin picks
    //
    //  The tab used to dump the newest raw ``/admin/logs`` page and call it shifts:
    //  no period, no dates, and a 500-row cap a busy month would run past. It then
    //  became a per-worker monthly total - a payroll report with the money hidden.
    //  This is what it is now: the shifts themselves - the day, who worked it, where,
    //  for how long, whether an admin has signed it off, and whether that worker has
    //  anything outstanding with us. ``/admin/reports/shifts`` supplies the rows and
    //  the totals; the tab never adds them up itself.
    // -----------------------------------------------------------------

    // -----------------------------------------------------------------
    //  The column order - the administrator's, not the code's
    //
    //  Whoever checks "who is still awaiting approval" before they look at the hours
    //  should not have to hunt for that column every morning, so the order is editable
    //  and remembered. In localStorage, like the theme and the language: it is a
    //  preference about one person's screen, it has to survive a reload, and it must
    //  not quietly rearrange a colleague's.
    // -----------------------------------------------------------------

    /** Every column the timesheet can show, keyed by the name the stored order uses. */
    shiftsColumnDefs() {
        return {
            date: { label: 'date' },
            employee: { label: 'employee' },
            id: { label: 'userId' },
            site: { label: 'site' },
            hours: { label: 'hours' },
            awaiting: { label: 'shiftsPending' },
            notes: { label: 'shiftsOpenNotes' }
        };
    },

    /** The order this tab was asked for, and the one Reset puts back. */
    defaultShiftsColumns() {
        return ['date', 'employee', 'id', 'site', 'hours', 'awaiting', 'notes'];
    },

    /**
     * The order to render in: what the admin saved, repaired against the columns that
     * exist.
     *
     * A stored order is data written by an older version of this file, so it is treated
     * as such: an unknown key is dropped and a known column it does not mention is
     * appended, which means a release that adds a column shows it rather than hiding it
     * until somebody clears their browser data. Junk under the key is not a reason to
     * lose the table either - it falls back to the default order.
     */
    shiftsColumns() {
        let stored = null;
        try {
            stored = JSON.parse(localStorage.getItem('shiftsColumns') || 'null');
        } catch (err) {
            stored = null;
        }
        const known = this.shiftsColumnDefs();
        const order = [];
        const seen = new Set();
        (Array.isArray(stored) ? stored : []).forEach((key) => {
            if (known[key] && !seen.has(key)) {
                seen.add(key);
                order.push(key);
            }
        });
        this.defaultShiftsColumns().forEach((key) => { if (!seen.has(key)) order.push(key); });
        return order;
    },

    /** Remember an order. A browser that refuses the write still renders the new one. */
    setShiftsColumns(order) {
        try {
            localStorage.setItem('shiftsColumns', JSON.stringify(order));
        } catch (err) {
            // Storage disabled (a private window, a hardened browser) is still a usable
            // timesheet - the order just does not outlive the page.
        }
    },

    /** One step earlier/later (-1/+1), then repaint from the rows already in hand. */
    moveShiftsColumn(key, delta) {
        const order = this.shiftsColumns();
        const from = order.indexOf(key);
        const to = from + delta;
        if (from < 0 || to < 0 || to >= order.length) return undefined;
        order.splice(to, 0, order.splice(from, 1)[0]);
        this.setShiftsColumns(order);
        return this.repaintShiftsFromCache();
    },

    resetShiftsColumns() {
        try {
            localStorage.removeItem('shiftsColumns');
        } catch (err) {
            // Nothing to remove where storage is unavailable.
        }
        return this.repaintShiftsFromCache();
    },

    /** The header of one column, in the reader's language. */
    shiftsColumnLabel(key) {
        return I18n.__(this.shiftsColumnDefs()[key].label);
    },

    /**
     * The report currently on screen, together with the period it describes.
     *
     * Kept so that a search can repaint from data already in hand: refetching the same
     * period in order to filter it client-side would cost a round trip - and a spinner
     * on a phone on site - to get back identical rows.
     */
    _shiftsReport: null,

    /**
     * The period on screen, or this month so far.
     *
     * Held in ``State`` so switching admin tabs (or switching language, which
     * re-renders) does not quietly move an admin reading figures onto a different
     * shifts period than the one those figures came from.
     */
    shiftsRange() {
        return State.shiftsRange || this.defaultShiftsRange();
    },

    /**
     * First of this month -> today, which is exactly the window the server picks
     * when it is sent no range, so the first render cannot disagree with the API.
     */
    defaultShiftsRange() {
        return this.monthRange(0);
    },

    /** ``YYYY-MM-DD`` in local time - what the API and the date inputs both use. */
    isoDate(when) {
        const pad = (value) => String(value).padStart(2, '0');
        return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
    },

    /**
     * A whole calendar month: ``offset`` 0 is this month, -1 is last month.
     *
     * This month ends *today*, not on a date in the future: a shifts period has to
     * describe work that has happened, and a stray future end date would make the
     * period look like it had missed shifts.
     */
    monthRange(offset) {
        const today = new Date();
        const first = new Date(today.getFullYear(), today.getMonth() + offset, 1);
        // Day 0 of the next month is the last day of this one - no month-length table.
        const last = offset === 0 ? today : new Date(today.getFullYear(), today.getMonth() + offset + 1, 0);
        return { start: this.isoDate(first), end: this.isoDate(last) };
    },

    /**
     * Sunday -> today.
     *
     * Sunday-first on purpose: the site's default ``working_days`` runs Sunday -> Friday,
     * so a working week starts on Sunday and ends on Thursday. A Monday-based preset
     * would cut that week in half and bill Sunday to a period of its own.
     */
    weekRange() {
        const today = new Date();
        const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - today.getDay());
        return { start: this.isoDate(start), end: this.isoDate(today) };
    },

    /**
     * The one-click periods, in the order an admin pays people.
     *
     * ``active`` marks the preset whose window is on screen, so the picker says which
     * one produced the figures rather than leaving three identical-looking buttons.
     */
    shiftsPresets(range) {
        const current = range || this.shiftsRange();
        return [
            { key: 'thisMonth', label: 'shiftsThisMonth', range: this.monthRange(0) },
            { key: 'lastMonth', label: 'shiftsLastMonth', range: this.monthRange(-1) },
            { key: 'thisWeek', label: 'shiftsThisWeek', range: this.weekRange() }
        ].map((preset) => ({
            key: preset.key,
            label: preset.label,
            range: preset.range,
            active: preset.range.start === current.start && preset.range.end === current.end
        }));
    },

    /** One tap on a preset: set the period, then reload the totals for it. */
    applyShiftsPreset(key) {
        const preset = this.shiftsPresets().find((candidate) => candidate.key === key);
        if (!preset) return;
        if (!this.setShiftsRange(preset.range.start, preset.range.end)) return;
        return UI.renderAdminTab('Shifts');
    },

    // -----------------------------------------------------------------
    //  The period in the URL
    //
    //  ``#shifts=2026-08-01..2026-08-31`` is what makes the period shareable: the
    //  address bar always describes the figures on screen, so an admin can paste it
    //  to a colleague (or bookmark it) and both read the same shifts period. A
    //  fragment is used rather than a query string or a path, so no server route or
    //  tunnel rewrite has to know about it.
    // -----------------------------------------------------------------

    /**
     * The period encoded in a URL fragment, or null when there is none or it is junk.
     *
     * ``payroll=`` is still read: links with that fragment were shared with colleagues
     * before the tab was renamed, and a link that stops working is worse than a link
     * with an old word in it.
     */
    shiftsRangeFromUrl(url) {
        const source = String(url === undefined ? window.location.hash : url);
        const match = /(?:^|[#&])(?:shifts|payroll)=(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})/.exec(source);
        if (!match) return null;
        const range = { start: match[1], end: match[2] };
        // A backwards range is not a period: ignore a mistyped link rather than put
        // every reader of it in front of the same 400.
        return range.start > range.end ? null : range;
    },

    /** The search carried in a URL fragment, or '' when there is none. */
    shiftsQueryFromUrl(url) {
        const source = String(url === undefined ? window.location.hash : url);
        const match = /(?:^|[#&])q=([^&]*)/.exec(source);
        if (!match) return '';
        try {
            return decodeURIComponent(match[1]);
        } catch (err) {
            // A malformed escape ("%zz") must not take the whole page down with it.
            return match[1];
        }
    },

    /** The fragment that describes a period and the search over it. */
    shiftsUrl(range, query) {
        const base = `#shifts=${range.start}..${range.end}`;
        return query ? `${base}&q=${encodeURIComponent(query)}` : base;
    },

    /**
     * Puts the shown period and search in the URL.
     *
     * ``replaceState``, not ``pushState``: stepping through presets (or typing in the
     * search box) should not leave a trail of history entries between the admin and the
     * page they came from, and the URL still copies and bookmarks perfectly.
     */
    syncShiftsUrl(range) {
        if (typeof history === 'undefined' || !history.replaceState) return;
        const hash = this.shiftsUrl(range, this.shiftsQuery());
        if (window.location.hash === hash) return;
        try {
            history.replaceState(null, '', hash);
        } catch (err) {
            // A few contexts (file://, a sandboxed frame) refuse history writes. The
            // period still works there - it just cannot be linked from this page - and
            // the figures must not be lost because the address bar is unavailable.
        }
    },

    /**
     * Adopt a shared link: `#shifts=...&q=...` selects the period, the search and the tab.
     *
     * Returns true when the fragment carried a usable period. Selecting Shifts here
     * (rather than at the call site) is deliberate: a shifts link that opens on Live
     * Ops would hide exactly the figures it was sent to show. The search is taken from
     * the link too, so that clearing it is the only way to see *more* than the link.
     */
    adoptShiftsRangeFromUrl() {
        const range = this.shiftsRangeFromUrl();
        if (!range) return false;
        State.shiftsRange = range;
        State.shiftsQuery = this.shiftsQueryFromUrl();
        State.adminTab = 'Shifts';
        return true;
    },

    /** Copies the link to the period and search on screen, to paste to a colleague. */
    copyShiftsLink() {
        // Build the URL from the state rather than reading the address bar, so the link
        // is right even when the page was opened from a link with no fragment yet.
        const link = `${window.location.href.split('#')[0]}${this.shiftsUrl(this.shiftsRange(), this.shiftsQuery())}`;
        const done = () => Toast.success(I18n.__('copied'));
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(link).then(done).catch(() => prompt(I18n.__('copyLink'), link));
        } else {
            prompt(I18n.__('copyLink'), link);
        }
    },

    // -----------------------------------------------------------------
    //  Search inside the period
    //
    //  One box for the four things an admin actually has in hand: a name, a worker id,
    //  a site, or a date. The first three narrow the rows on screen (the request is
    //  untouched, so the period the totals describe never changes under the admin); a
    //  date cannot narrow an aggregated row, so it offers to switch the *period* to
    //  that day instead of silently filtering everything away.
    // -----------------------------------------------------------------

    shiftsQuery() {
        return State.shiftsQuery || '';
    },

    /** `2026-08-07` or `2026/08/07` typed into the box means "that day", not a filter. */
    shiftsSearchDay(query) {
        const match = /^(\d{4})[-/](\d{2})[-/](\d{2})$/.exec(String(query || '').trim());
        return match ? `${match[1]}-${match[2]}-${match[3]}` : null;
    },

    /**
     * The rows matching a search.
     *
     * Every whitespace-separated term has to match somewhere in the name, the worker id,
     * the site, the day or the approval state, so "tower khan" narrows the list instead of
     * widening it, and a day typed in finds that day's shifts without asking the server for
     * a different period. Substring and case-insensitive: an admin knows a name, not its
     * exact spelling.
     */
    shiftsMatches(rows, query) {
        const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
        if (terms.length === 0) return rows;
        return rows.filter((row) => {
            const haystack = [
                row.worker_name, row.worker_id, row.site_name, row.date, row.timestamp, row.status
            ].join(' ').toLowerCase();
            return terms.every((term) => haystack.indexOf(term) >= 0);
        });
    },

    /**
     * Totals for the rows on screen.
     *
     * Sums the server's own per-shift figures, never a re-derivation from the raw
     * clock-ins: that keeps a filtered view and the period totals the *same* arithmetic
     * over a different set of rows, instead of two number-crunchers that can disagree.
     *
     * Kept key-for-key identical to the server's ``totals``, so a card can be painted
     * from either one without knowing which it was handed.
     */
    sumShiftsRows(rows) {
        const sum = (key) => rows.reduce((total, row) => total + (Number(row[key]) || 0), 0);
        const awaitingHours = (row) => (row.awaiting_approval ? Number(row.hours) || 0 : 0);
        return {
            shifts: rows.length,
            workers: new Set(rows.map((row) => String(row.worker_id))).size,
            hours: sum('hours'),
            // A rejected row is not awaiting anything and is worth nothing, so it lands in
            // neither total - exactly as the server counts it.
            approved_hours: rows.reduce(
                (total, row) => total + (Number(row.hours) || 0) - awaitingHours(row), 0
            ),
            awaiting_approval_hours: rows.reduce((total, row) => total + awaitingHours(row), 0),
            awaiting_approval: rows.filter((row) => row.awaiting_approval).length,
            break_hours: sum('break_hours'),
            workers_with_open_notes: new Set(
                rows.filter((row) => Number(row.open_notes) > 0).map((row) => String(row.worker_id))
            ).size
        };
    },

    /** Reads the search box and repaints the rows for it. */
    async applyShiftsSearch() {
        const box = document.getElementById('shiftsQuery');
        State.shiftsQuery = box ? String(box.value || '').trim() : '';
        return this.repaintShiftsFromCache();
    },

    async clearShiftsSearch() {
        State.shiftsQuery = '';
        return this.repaintShiftsFromCache();
    },

    /**
     * Repaints the tab from the report already on screen.
     *
     * The cache is only used when it describes the period being shown: figures from one
     * period under another period's dates are exactly the kind of quiet mismatch this
     * whole tab exists to avoid. Anything else falls back to a real request.
     */
    repaintShiftsFromCache() {
        const content = document.getElementById('adminContent');
        const cached = this._shiftsReport;
        const range = this.shiftsRange();
        if (!content || !cached || cached.range.start !== range.start || cached.range.end !== range.end) {
            return UI.renderAdminTab('Shifts');
        }
        this.paintShifts(content, this.shiftsToolbarHtml(range) + this.shiftsReportHtml(cached.report));
        return Promise.resolve();
    },

    /** The box's date shortcut: move the whole period onto that one day. */
    async applyShiftsDay(day) {
        if (!this.setShiftsRange(day, day)) return;
        // The query was a request for a day, not a filter: leaving it in the box would
        // hide every row of the very day it just fetched.
        State.shiftsQuery = '';
        return UI.renderAdminTab('Shifts');
    },

    /**
     * Validate a period and remember it.
     *
     * Refused here as well as on the server: the API answers 400 for ``end <
     * start``, but the admin should hear it while still looking at the two dates
     * they typed, not after a round trip.
     */
    setShiftsRange(start, end) {
        if (!start || !end) {
            Toast.error(I18n.__('shiftsRangeRequired'));
            return false;
        }
        if (start > end) {
            Toast.error(I18n.__('shiftsRangeInvalid'));
            return false;
        }
        State.shiftsRange = { start, end };
        return true;
    },

    /** Reads the two date inputs and reloads the totals for that period. */
    async applyShiftsFilter() {
        const start = document.getElementById('shiftsStart').value;
        const end = document.getElementById('shiftsEnd').value;
        if (!this.setShiftsRange(start, end)) return;
        // Re-render the tab rather than patching the DOM in place: the toolbar's
        // loading state and the figures then always come from the same request.
        await UI.renderAdminTab('Shifts');
    },

    async renderShifts(content) {
        // Paint the picker before the request: the fetch can be slow on site, and the
        // admin should see the period they are waiting on rather than a blank tab.
        this.paintShifts(content, this.shiftsToolbarHtml(this.shiftsRange()) + UI.loadingHtml());
        await this.loadShiftsReport(content);
    },

    /** Everything above the figures: the period picker, the columns and the search box. */
    shiftsToolbarHtml(range) {
        return `${this.shiftsFilterHtml(range)}${this.shiftsColumnsHtml()}${this.shiftsSearchHtml()}`;
    },

    /**
     * The column editor: every column as a chip with a move-earlier / move-later button.
     *
     * Deliberately not drag-and-drop. This is a table an administrator rearranges once and
     * then reads for months, and a drag that needs a steady finger on a phone in gloves is
     * a worse tool for that than two buttons that always do the same thing. The order is
     * written the moment it changes, so there is no Save to forget and nothing to lose by
     * closing the panel.
     */
    shiftsColumnsHtml() {
        const order = this.shiftsColumns();
        const chip = 'ui-chip';
        const step = 'ui-btn ui-btn-sm ui-btn-quiet';
        return `
            <details id="shiftsColumns" class="ops-panel" style="margin-bottom:16px">
                <summary>${this.OPS_ICONS.table}<span>${this.escapeHtml(I18n.__('shiftsColumns'))}</span></summary>
                <div class="ops-panel-body">
                    <div class="ui-row" data-column-editor data-order="${order.join(',')}">
                        ${order.map((key, index) => `
                            <span class="${chip}" data-column="${key}">
                                ${this.shiftsColumnLabel(key)}
                                <button type="button" data-move-earlier title="${this.escapeHtml(I18n.__('shiftsColumnEarlier'))}"
                                        onclick="UI_MODULES.moveShiftsColumn('${key}', -1)" class="${step}"
                                        ${index === 0 ? 'disabled' : ''}>&#8592;</button>
                                <button type="button" data-move-later title="${this.escapeHtml(I18n.__('shiftsColumnLater'))}"
                                        onclick="UI_MODULES.moveShiftsColumn('${key}', 1)" class="${step}"
                                        ${index === order.length - 1 ? 'disabled' : ''}>&#8594;</button>
                            </span>`).join('')}
                        <button type="button" data-columns-reset onclick="UI_MODULES.resetShiftsColumns()" class="${chip}">${this.escapeHtml(I18n.__('shiftsColumnsReset'))}</button>
                    </div>
                </div>
            </details>`;
    },

    /**
     * Paints the whole tab and binds the picker.
     *
     * Every path redraws the toolbar with the figures, rather than patching a child
     * node: the dates on screen are then always the dates those figures came from,
     * including after a failed request.
     */
    paintShifts(content, html) {
        // The URL follows the figures, including when the request failed: the link an
        // admin copies next has to describe the period and the search they are looking at.
        this.syncShiftsUrl(this.shiftsRange());
        content.innerHTML = html;
        document.getElementById('shiftsFilter').onsubmit = (event) => {
            event.preventDefault();
            return this.applyShiftsFilter();
        };
        document.getElementById('shiftsSearchForm').onsubmit = (event) => {
            event.preventDefault();
            return this.applyShiftsSearch();
        };
    },

    /**
     * The search box: name, worker id, site - or a date.
     *
     * Submitted rather than filtered on every keystroke, because the tab is repainted
     * from a string: filtering as you type would rebuild the input under the caret and
     * drop it mid-word. Enter (or Search) applies, Clear removes it.
     */
    shiftsSearchHtml() {
        const query = this.shiftsQuery();
        return `
            <form id="shiftsSearchForm" class="ui-row" style="margin-bottom:16px">
                <label class="sr-only" for="shiftsQuery">${this.escapeHtml(I18n.__('shiftsSearchPlaceholder'))}</label>
                <input type="search" id="shiftsQuery" class="ui-field is-flex" value="${this.escapeHtml(query)}"
                       placeholder="${this.escapeHtml(I18n.__('shiftsSearchPlaceholder'))}">
                <button type="submit" class="ui-btn">${this.OPS_ICONS.search}${this.escapeHtml(I18n.__('search'))}</button>
                ${query ? `<button type="button" data-clear-search onclick="UI_MODULES.clearShiftsSearch()" class="ui-btn ui-btn-quiet">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('clear'))}</button>` : ''}
            </form>`;
    },

    /**
     * The date-range picker.
     *
     * Deliberately without ``min``/``max`` on the inputs: tightening the end to the
     * current start (or the reverse) makes a legitimate move - "back to August" -
     * impossible from the UI, and the range is validated properly on submit anyway.
     */
    shiftsFilterHtml(range) {
        return `
            <form id="shiftsFilter" class="ui-row" style="align-items:flex-end;margin-bottom:12px">
                <div>
                    <label class="ui-label" for="shiftsStart">${this.escapeHtml(I18n.__('shiftsFrom'))}</label>
                    <input type="date" id="shiftsStart" value="${this.escapeHtml(range.start)}" class="ui-field">
                </div>
                <div>
                    <label class="ui-label" for="shiftsEnd">${this.escapeHtml(I18n.__('shiftsTo'))}</label>
                    <input type="date" id="shiftsEnd" value="${this.escapeHtml(range.end)}" class="ui-field">
                </div>
                <button type="submit" class="ui-btn ui-btn-primary">${this.OPS_ICONS.table}${this.escapeHtml(I18n.__('viewTotals'))}</button>
                <button type="button" onclick="UI_MODULES.downloadShiftsCsv()" class="ui-btn">${this.OPS_ICONS.download}${this.escapeHtml(I18n.__('downloadCSV'))}</button>
            </form>
            <div class="ui-row" style="margin-bottom:20px">
                <!-- The data-preset and data-active attributes stay adjacent and in that
                     order: the product suite reads which preset is in effect from exactly
                     that pair. -->
                ${this.shiftsPresets(range).map(preset => `
                    <button type="button" data-preset="${preset.key}" data-active="${preset.active}"
                            onclick="UI_MODULES.applyShiftsPreset('${preset.key}')"
                            class="ui-chip"${preset.active ? ' aria-pressed="true"' : ''}>${this.escapeHtml(I18n.__(preset.label))}</button>`).join('')}
                <button type="button" data-copy-link onclick="UI_MODULES.copyShiftsLink()"
                        class="ui-chip ui-push">${this.OPS_ICONS.copy}${this.escapeHtml(I18n.__('copyLink'))}</button>
            </div>`;
    },

    async loadShiftsReport(content) {
        const range = this.shiftsRange();
        try {
            const report = await API.request(`/admin/reports/shifts?start=${range.start}&end=${range.end}`);
            this._shiftsReport = { range: { start: range.start, end: range.end }, report: report };
            this.paintShifts(content, this.shiftsToolbarHtml(range) + this.shiftsReportHtml(report));
        } catch (err) {
            // No figures for this period, so nothing may be reused from an earlier one.
            this._shiftsReport = null;
            // The picker stays on screen with the error, so a rejected range (or a dead
            // server) is something the admin can correct and retry without leaving the tab.
            this.paintShifts(content, this.shiftsToolbarHtml(range) +
                `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`);
        }
    },

    shiftsReportHtml(report) {
        const rows = report.rows || [];
        const period = report.period || {};
        const query = this.shiftsQuery();
        // A day typed in still offers the one-tap "make this the period" chip below, but it
        // is also an ordinary filter: a timesheet row has a date, so typing one narrows the
        // list rather than standing there refusing to. No search ever re-asks the server for
        // a different period, so the period keeps meaning exactly what it says.
        const day = this.shiftsSearchDay(query);
        const filtering = query !== '';
        const shown = this.shiftsVisibleRows(report);
        const totals = filtering ? this.sumShiftsRows(shown) : (report.totals || {});
        const noMatches = filtering && rows.length > 0 && shown.length === 0;
        // Hours only, and the four ways the timesheet is read: how much time the period
        // holds, how much of it has been signed off, how much is still waiting - and how
        // many shifts that is. There is no rate and no estimate: this app does not pay
        // anybody, and a money card would promise a payout screen that does not exist.
        // The fourth element is the figure's tone - ``warn`` for hours nobody has signed
        // off, ``quiet`` for the break that is not part of the paid figure beside it - and
        // it is a role the stylesheet knows, not a panel of colour names.
        const cards = [
            ['hours', 'hours', this.hoursLabel(totals.hours), ''],
            ['approved_hours', 'shiftsApproved', this.hoursLabel(totals.approved_hours), ''],
            ['awaiting_approval_hours', 'shiftsPendingHours', this.hoursLabel(totals.awaiting_approval_hours), 'warn'],
            ['awaiting_approval', 'shiftsPendingShifts', String(totals.awaiting_approval || 0), 'warn'],
            // Beside the counted hours, because the two together are what a door-to-door
            // reconciliation is about: 8.0 h counted out of 8.5 h on site.
            ['break_hours', 'shiftsBreak', this.hoursLabel(totals.break_hours), 'quiet'],
            ['shifts', 'shiftsWorked', String(totals.shifts || 0), ''],
            ['workers', 'shiftsWorkers', String(totals.workers || 0), ''],
        ];
        const dayChip = day ? `
            <div style="margin-bottom:16px">
                <button type="button" data-show-day onclick="UI_MODULES.applyShiftsDay('${day}')" class="ui-btn ui-btn-primary">
                    ${this.OPS_ICONS.table}${this.escapeHtml(I18n.__('shiftsShowDay'))} ${this.escapeHtml(day)}
                </button>
            </div>` : '';
        const filterNote = filtering ? `
            <p class="ui-section-note" data-filter-note style="margin-bottom:12px">
                ${this.escapeHtml(I18n.__('shiftsFiltered'))}: “${this.escapeHtml(query)}” · ${shown.length} / ${rows.length} ${this.escapeHtml(I18n.__('shifts'))}.<br>
                ${this.escapeHtml(I18n.__('shiftsFilteredTotals'))}
            </p>` : '';
        // With no match the cards are left out on purpose: a grid of zeros reads as "this
        // period held no work", which is the opposite of what a no-match means.
        // The same stat tiles the Live Ops board opens with, for the same reason: these
        // seven figures are what the tab is for, and a seven-cell grid of them reads as
        // one glance rather than seven. ``data-total`` / ``data-value`` stay adjacent -
        // the product suite reads the figures off that pair.
        const cardsHtml = noMatches ? '' : `
            <div class="ops-stats" style="margin-bottom:20px">
                ${cards.map(([key, label, value, tone]) => `
                    <div class="ops-stat${tone ? ' is-' + tone : ''}" data-total="${key}" data-value="${value}">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__(label))}</span>
                        <span class="ops-stat-value">${this.escapeHtml(value)}</span>
                    </div>`).join('')}
            </div>`;
        const body = shown.length === 0
            ? `<div class="ui-empty"${noMatches ? ' data-no-matches="true"' : ''}>
                    <span class="ui-empty-icon">${this.OPS_ICONS.table}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__(noMatches ? 'shiftsNoMatches' : 'shiftsEmpty'))}</p>
               </div>`
            : this.shiftsRowsHtml(shown);
        return `
            <div class="ui-section-head" style="margin-bottom:12px">
                <p class="ui-section-note">${this.escapeHtml(I18n.__('shiftsPeriod'))}:
                    <span class="ops-name">${this.escapeHtml(period.start || '')} \u2192 ${this.escapeHtml(period.end || '')}</span></p>
                <p class="ui-section-note" style="max-width:52ch">${this.escapeHtml(I18n.__('shiftsApprovedOnly'))}</p>
            </div>
            ${dayChip}${filterNote}${cardsHtml}${body}`;
    },

    shiftsRowsHtml(rows) {
        const columns = this.shiftsColumns();
        // The cells carry no class of their own: their padding, their hairline and the row
        // hover all come from ``.ui-table`` on the element, which is what a cell in this
        // table looks like wherever it is drawn - the phone card included.
        if (Device.isMobile) {
            return `<div class="ui-stack">${rows.map(row => `
                <div class="ui-card is-stacked"${this.shiftAttr(row)}>
                    <dl class="ui-stack is-tight">
                        ${columns.map(key => `
                            <div class="ui-spread" style="align-items:baseline">
                                <dt class="ui-note is-strong" data-shift-label>${this.shiftsColumnLabel(key)}</dt>
                                <dd class="ui-fact-value" style="text-align:end">${this.shiftsCellHtml(row, key)}</dd>
                            </div>`).join('')}
                    </dl>
                </div>`).join('')}</div>`;
        }
        return `
            <div class="ui-table-wrap">
                <table class="ui-table" data-shifts-table="true">
                    <caption class="sr-only">${this.escapeHtml(I18n.__('shifts'))}</caption>
                    <thead>
                        <tr>
                            ${columns.map(key => `<th>${this.shiftsColumnLabel(key)}</th>`).join('')}
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.map(row => `<tr${this.shiftAttr(row)}>
                            ${columns.map(key => `<td>${this.shiftsCellHtml(row, key)}</td>`).join('')}
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    },

    /**
     * The identity of a rendered row, as attributes.
     *
     * ``data-shift`` is the clock-out's own id - the one thing about the row that is
     * unique; the rest are what a reader (and the suite that drives this file) can group
     * and filter by without parsing the cells back out of the markup.
     */
    shiftAttr(row) {
        return ` data-shift="${this.escapeHtml(row.log_id)}"`
            + ` data-worker="${this.escapeHtml(row.worker_id)}"`
            + ` data-date="${this.escapeHtml(row.date)}"`
            + ` data-awaiting="${row.awaiting_approval ? 'true' : 'false'}"`;
    },

    /** One cell of one row. The only place a column's value is written. */
    shiftsCellHtml(row, key) {
        switch (key) {
            case 'date':
                return `<span class="ui-nowrap">${this.escapeHtml(row.date)}</span>`;
            case 'employee':
                return `<span class="ui-strong">${this.escapeHtml(row.worker_name || row.worker_id)}</span>`;
            case 'id':
                return `<span class="ui-tone-muted">${this.escapeHtml(row.worker_id)}</span>`;
            case 'site':
                // An em dash, not an empty cell: a shift whose site is not on file is a gap
                // in the record, and a blank reads as "this row has no site column".
                return row.site_name
                    ? this.escapeHtml(row.site_name)
                    : `<span class="ui-tone-faint">\u2014</span>`;
            case 'hours':
                // The number the server counted, not the raw clock: a shift somebody has
                // signed off is worth exactly the hours they signed for.
                return `<span class="ui-strong">${this.hoursLabel(row.hours)}</span>`;
            case 'awaiting':
                return this.awaitingHtml(row);
            case 'notes':
                return Number(row.open_notes) > 0
                    ? `<span class="ui-strong ui-tone-warn">${Number(row.open_notes)}</span>`
                    : `<span class="ui-tone-faint">0</span>`;
            default:
                return '';
        }
    },

    /**
     * Awaiting approval, or the decision that was made.
     *
     * A waiting shift says so in the same amber the worker's own card uses for hours past
     * the paid day. A decided one names the decision (approved, closed by the system,
     * rejected) rather than a bare tick, because "rejected" and "approved" are not the
     * same kind of nothing.
     */
    awaitingHtml(row) {
        if (row.awaiting_approval) {
            return `<span class="ui-badge is-warn">${I18n.__('shiftsPending')}</span>`;
        }
        return `<span class="ui-note">${this.escapeHtml(row.status || '')}</span>`;
    },

    /**
     * Downloads exactly the rows on screen - same set, same order, filtered or not.
     *
     * This used to hand over to ``/admin/reports/export``, which exports the whole period
     * because it has no free-text filter. An admin who searched a site and then hit
     * Download got every worker back, and a file that disagrees with the table it came
     * from is worse than no file: it gets forwarded as the period's record. The rows on
     * screen are already the whole aggregated period, so the file is built from them and
     * cannot disagree with anything.
     */
    downloadShiftsCsv() {
        const range = this.shiftsRange();
        if (!this.shiftsReportFor(range)) {
            // Nothing on screen for this period - the request failed or has not landed -
            // so there is no honest file to write.
            Toast.error(I18n.__('shiftsNothingToExport'));
            return;
        }
        API.saveFile(
            this.shiftsExportName(range, this.shiftsQuery()),
            this.shiftsCsv(this.shiftsRowsOnScreen())
        );
    },

    /** The report on screen *if* it describes this period; a stale one is not usable. */
    shiftsReportFor(range) {
        const cached = this._shiftsReport;
        if (!cached || cached.range.start !== range.start || cached.range.end !== range.end) return null;
        return cached.report;
    },

    /**
     * The rows a report shows under the current search - the one definition of "which
     * shifts does this search mean", used by the table and by the download alike.
     *
     * A day is an ordinary filter here, not a special case: a timesheet row carries its own
     * date, so typing one narrows the list and the file together. Two implementations of
     * this rule is exactly how a download starts disagreeing with the screen it came from.
     */
    shiftsVisibleRows(report) {
        const rows = (report && report.rows) || [];
        return this.shiftsMatches(rows, this.shiftsQuery());
    },

    /** The rows the table is showing right now, for the period on screen. */
    shiftsRowsOnScreen() {
        return this.shiftsVisibleRows(this.shiftsReportFor(this.shiftsRange()));
    },

    /**
     * The rows as CSV: who, their id, where, and how long.
     *
     * Four columns, and the same four in the same order as ``/admin/reports/export``
     * writes, so a file downloaded here and one pulled from the API are the same sheet.
     * The *screen's* column order deliberately does not travel into the file: a sheet whose
     * columns move from day to day cannot be compared with last month's, and the person it
     * is sent to did not rearrange anything.
     *
     * The hours are the figure on screen (an approved shift shows the approved hours); the
     * API export writes the same figure at full precision. Quoting is minimal, like
     * ``csv.writer``.
     */
    shiftsCsv(rows) {
        const columns = ['Employee', 'id', 'site', 'hours'];
        const cell = (value) => {
            const text = value === null || value === undefined ? '' : String(value);
            return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
        };
        const line = (values) => values.map(cell).join(',');
        const body = rows.map((row) => line([
            row.worker_name || row.worker_id,
            row.worker_id,
            row.site_name || '',
            this.hoursLabel(row.hours)
        ]));
        // CRLF and a final break, matching csv.writer: the last row must not be lost to a
        // parser that reads lines.
        return [line(columns)].concat(body).join('\r\n') + '\r\n';
    },

    /**
     * A filename that says which period - and which search - the file holds.
     *
     * The search is part of the name so two downloads of the same period do not silently
     * replace each other in the Downloads folder.
     */
    shiftsExportName(range, query) {
        const slug = String(query || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 24);
        return `shifts_${range.start}_${range.end}${slug ? `_${slug}` : ''}.csv`;
    },

    // (``sitesLabel()`` and ``pendingHours()`` are gone with the per-worker report: a shift
    // has one site, and a timesheet row is either signed off or it is not - the server says
    // which, and the cells are written by ``shiftsCellHtml()`` alone.)

    // -----------------------------------------------------------------
    //  Notes inbox - what workers and moallems are asking for
    //
    //  A worker can now raise something in writing (password help, missing material,
    //  a hours question) instead of hoping to catch an admin on the phone, which is
    //  the difference between a request that gets handled and one that quietly does
    //  not. This tab is the other half: one list of what is waiting, a thread to
    //  answer in, and - for a password request - the reset itself, wired to the same
    //  endpoint the Credentials tab uses.
    //
    //  What it deliberately does NOT do is show or store the worker's password. The
    //  reset generates one, reveals it once for the admin to hand over, and signs the
    //  account out; writing it into a reply would put a readable password in the
    //  database and in every backup of it.
    // -----------------------------------------------------------------

    /** The last list response, kept so a search repaints without a request. */
    _notes: null,

    /** The note currently open (with its whole thread), or null for the list. */
    _noteThread: null,

    /** A password just generated for a note's author: shown once, then forgotten. */
    _noteRevealed: null,

    /** A password the admin typed for a note's author; blank means "generate one". */
    _notePasswordDraft: '',

    /** The status a reply will move the note to; ``resolved`` after a password reset. */
    _noteStatusDraft: '',

    /** "" means every status, which is what the tab opens on. */
    notesStatus() { return State.notesStatus || ''; },
    notesQuery() { return State.notesQuery || ''; },

    noteStatusLabel(status) { return codeLabel('noteStatus', status); },
    noteCategoryLabel(category) { return codeLabel('noteCat', category); },

    /** The badge variant for a note's status, as a class the stylesheet already has. */
    noteStatusClass(status) {
        if (status === 'resolved') return ' is-ok';
        if (status === 'in_progress') return ' is-info';
        if (status === 'closed') return ' is-quiet';
        return ' is-warn';
    },

    noteStatusChip(status) {
        return `<span class="ui-badge${this.noteStatusClass(status)}">${this.noteStatusLabel(status)}</span>`;
    },

    async renderNotes(content) {
        // Toolbar first: the list can be slow from site, and an admin should see the
        // filter they are about to use rather than a blank tab.
        this.paintNotes(content, this.notesToolbarHtml() + UI.loadingHtml());
        await this.loadNotes(content);
    },

    async loadNotes(content) {
        try {
            const data = await API.request('/admin/notes');
            this._notes = data;
            this.paintNotes(content, this.notesToolbarHtml() + this.notesListHtml(data));
        } catch (err) {
            this._notes = null;
            this.paintNotes(content, this.notesToolbarHtml() +
                `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`);
        }
    },

    /** Repaints the tab and binds the search box. */
    paintNotes(content, html) {
        content.innerHTML = html;
        const form = document.getElementById('notesSearchForm');
        if (form) {
            form.onsubmit = (event) => {
                event.preventDefault();
                return this.applyNotesSearch();
            };
        }
    },

    repaintNotesFromCache() {
        const content = document.getElementById('adminContent');
        if (!content || !this._notes) return UI.renderAdminTab('Notes');
        this.paintNotes(content, this.notesToolbarHtml() + this.notesListHtml(this._notes));
        return Promise.resolve();
    },

    async applyNotesSearch() {
        const box = document.getElementById('notesQuery');
        State.notesQuery = box ? String(box.value || '').trim() : '';
        return this.repaintNotesFromCache();
    },

    async clearNotesSearch() {
        State.notesQuery = '';
        return this.repaintNotesFromCache();
    },

    async filterNotesByStatus(status) {
        State.notesStatus = status || '';
        return this.repaintNotesFromCache();
    },

    /**
     * The status chips, each carrying how many notes are in that state.
     *
     * The counts come from the server and cover every note, not the filtered list: a
     * chip that says "3" because that is what is on screen would be answering a
     * question nobody asked.
     */
    notesToolbarHtml() {
        const counts = (this._notes && this._notes.counts) || {};
        const active = this.notesStatus();
        const total = Object.keys(counts).reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
        const query = this.notesQuery();
        const chips = [['', 'notesFilterAll', total]].concat(
            ['open', 'in_progress', 'resolved', 'closed'].map((status) => [status, null, counts[status] || 0])
        );
        return `
            <div class="ui-section-head">
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('notesInbox'))}</h3>
                <p class="ui-section-note" data-notes-hint style="max-width:64ch">${this.escapeHtml(I18n.__('notesInboxHint'))}</p>
            </div>
            <div class="ui-row" style="margin-top:12px">
                <!-- The data-status and data-active attributes are adjacent on purpose: the
                     product suite reads the filter state off that pair, and one of them
                     moving to another element would make the counts untestable without
                     touching CSS. -->
                ${chips.map(([status, labelKey, count]) => `
                    <button type="button" data-status="${status || 'all'}" data-active="${status === active}"
                            onclick="UI_MODULES.filterNotesByStatus('${status}')"
                            class="ui-chip"${status === active ? ' aria-pressed="true"' : ''}>${this.escapeHtml(labelKey ? I18n.__(labelKey) : this.noteStatusLabel(status))} (${count})</button>`).join('')}
            </div>
            <form id="notesSearchForm" class="ui-row" style="margin-top:12px;margin-bottom:16px">
                <label class="sr-only" for="notesQuery">${this.escapeHtml(I18n.__('notesSearchPlaceholder'))}</label>
                <input type="search" id="notesQuery" class="ui-field is-flex" value="${this.escapeHtml(query)}"
                       placeholder="${this.escapeHtml(I18n.__('notesSearchPlaceholder'))}">
                <button type="submit" class="ui-btn">${this.OPS_ICONS.search}${this.escapeHtml(I18n.__('search'))}</button>
                ${query ? `<button type="button" onclick="UI_MODULES.clearNotesSearch()" class="ui-btn ui-btn-quiet">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('clear'))}</button>` : ''}
            </form>`;
    },

    /**
     * The notes matching the status filter and the search.
     *
     * Same rule as the shifts tab: every term has to match somewhere, so "tower khan"
     * narrows instead of widening. The body is searched too - an admin looking for
     * "cement" is looking for what the worker wrote, not for how they titled it.
     */
    notesMatches(notes, query) {
        const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
        if (terms.length === 0) return notes;
        return notes.filter((note) => {
            const haystack = [
                note.worker_name, note.worker_id, note.subject, note.body,
                this.noteCategoryLabel(note.category), this.noteStatusLabel(note.status),
                note.last_message ? note.last_message.body : ''
            ].join(' ').toLowerCase();
            return terms.every((term) => haystack.indexOf(term) >= 0);
        });
    },

    notesVisible(data) {
        const notes = (data && data.notes) || [];
        const status = this.notesStatus();
        const byStatus = status ? notes.filter((note) => note.status === status) : notes;
        return this.notesMatches(byStatus, this.notesQuery());
    },

    notesListHtml(data) {
        const notes = (data && data.notes) || [];
        const shown = this.notesVisible(data);
        const query = this.notesQuery();
        if (shown.length === 0) {
            return `<p class="ui-empty" data-no-matches>${I18n.__('notesNone')}</p>`;
        }
        const filterNote = query ? `
            <p class="ui-note" data-filter-note>
                ${I18n.__('notesFiltered')}: “${this.escapeHtml(query)}” · ${shown.length} / ${notes.length}
            </p>` : '';
        return `${filterNote}${Device.isMobile ? this.notesCardsHtml(shown) : this.notesTableHtml(shown)}`;
    },

    notesTableHtml(notes) {
        // ``ui-table`` on the element: the frame brings the hairlines, the uppercase column
        // labels and the row hover, and no cell below has to name a padding value.
        return `
            <div class="ui-table-wrap">
                <table class="ui-table" data-notes-table="true">
                    <caption class="sr-only">${this.escapeHtml(I18n.__('notesInbox'))}</caption>
                    <thead>
                        <tr>
                            <th>${I18n.__('name')}</th>
                            <th>${I18n.__('noteCategory')}</th>
                            <th>${I18n.__('noteSubject')}</th>
                            <th>${I18n.__('status')}</th>
                            <th>${I18n.__('noteLastActivity')}</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>
                        ${notes.map((note) => `<tr data-note="${note.id}">
                            <td>
                                <p class="ui-card-title">${this.escapeHtml(note.worker_name || note.worker_id)}</p>
                                <p class="ui-note">${this.escapeHtml(note.worker_id)} · ${this.escapeHtml(this.roleLabel(note.worker_role || ''))}</p>
                            </td>
                            <td>${this.escapeHtml(this.noteCategoryLabel(note.category))}</td>
                            <td class="is-clip">
                                <p class="ui-strong ui-truncate">${this.escapeHtml(note.subject)}</p>
                                <p class="ui-note ui-truncate">${this.escapeHtml(note.body)}</p>
                            </td>
                            <td>${this.noteStatusChip(note.status)}</td>
                            <td class="ui-tone-muted">${this.escapeHtml(note.last_reply_at || note.created_at || '')}</td>
                            <td class="is-end">${this.noteOpenButtonHtml(note)}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    },

    notesCardsHtml(notes) {
        return `<div class="ui-stack">${notes.map((note) => `
            <div class="ui-card is-stacked" data-note="${note.id}">
                <div class="ui-spread">
                    <p class="ui-strong ui-truncate">${this.escapeHtml(note.subject)}</p>
                    ${this.noteUnreadBadge(note)}
                </div>
                <p class="ui-note">
                    ${this.escapeHtml(note.worker_name || note.worker_id)} · ${this.escapeHtml(note.worker_id)}
                </p>
                <p class="ui-note">${this.escapeHtml(this.noteCategoryLabel(note.category))} · ${this.noteStatusChip(note.status)}</p>
                <p class="ui-note is-body">${this.escapeHtml(note.body)}</p>
                <p class="ui-note ui-tone-faint">${this.escapeHtml(note.last_reply_at || note.created_at || '')}</p>
                <div class="ui-row">${this.noteOpenButtonHtml(note)}</div>
            </div>`).join('')}</div>`;
    },

    noteUnreadBadge(note) {
        const unread = Number(note.admin_unread) || 0;
        if (unread <= 0) return '';
        return `<span class="ui-badge is-solid" data-admin-unread>${I18n.__('noteWaitingReply')}</span>`;
    },

    noteOpenButtonHtml(note) {
        return `
            ${note.priority === 'high' ? `<span data-urgent="1" class="ui-badge is-danger ui-spaced-end">${I18n.__('notePriorityHigh')}</span>` : ''}
            ${this.noteUnreadBadge(note)}
            <button type="button" data-open-note onclick="UI_MODULES.openNote(${note.id})"
                    class="ui-btn ui-btn-primary ui-btn-sm ui-spaced-start">${I18n.__('open')}</button>`;
    },

    /**
     * One note with its whole thread, internal messages included.
     *
     * Opening from the list forgets any password on screen: the panel belongs to the note
     * it was created for, and the reveal must never follow the admin into a colleague's
     * thread. Re-reading the *same* note (after a reply, or right after a reset) goes
     * through ``loadNote`` instead, which is what keeps a password readable until the
     * admin closes it or copies it.
     */
    async openNote(noteId) {
        this._noteRevealed = null;
        this._noteStatusDraft = '';
        return this.loadNote(noteId);
    },

    async loadNote(noteId) {
        const content = document.getElementById('adminContent');
        if (!content) return;
        content.innerHTML = UI.loadingHtml();
        let note;
        try {
            note = await API.request(`/admin/notes/${noteId}`);
        } catch (err) {
            content.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
            return;
        }
        this._noteThread = note;
        this.paintNotes(content, this.noteThreadHtml(note));
    },

    noteThreadHtml(note) {
        const messages = note.messages || [];
        const field = 'ui-field';
        const quiet = 'ui-btn ui-btn-quiet';
        return `
            <button type="button" onclick="UI_MODULES.backToNotes()"
                    class="ui-btn is-link" data-notes-back>
                ← ${I18n.__('noteBackAdmin')}
            </button>
            <div class="ui-spread is-top">
                <div class="ui-stack is-tight">
                    <p class="ui-title">${this.escapeHtml(note.subject)}</p>
                    <p class="ui-note">
                        ${this.escapeHtml(note.worker_name || note.worker_id)} (${this.escapeHtml(note.worker_id)})
                        · ${this.escapeHtml(this.roleLabel(note.worker_role || ''))}
                        · ${this.escapeHtml(this.noteCategoryLabel(note.category))}
                        · ${I18n.__('noteOpened')}: ${this.escapeHtml(note.created_at || '')}
                    </p>
                </div>
                <div class="ui-row">${this.noteStatusChip(note.status)}
                    ${note.priority === 'high' ? `<span data-urgent="1" class="ui-badge is-danger">${I18n.__('notePriorityHigh')}</span>` : ''}
                </div>
            </div>
            ${this.notePasswordPanelHtml(note, field, quiet)}
            <div class="ui-stack is-tight">${messages.map((message) => this.noteMessageHtml(message)).join('')}</div>
            <div class="ui-card is-tight is-stacked is-flat" data-note-composer>
                <textarea id="noteReplyBody" rows="3" maxlength="2000"
                          placeholder="${I18n.__('noteReplyToWorker')}" class="${field}"></textarea>
                <div class="ui-row">
                    <select id="noteReplyStatus" class="ui-field">
                        ${[['', 'noteKeepStatus'], ['in_progress', 'noteMarkInProgress'], ['resolved', 'noteMarkResolved'], ['open', 'noteMarkOpen']]
                            .map(([value, key]) => `<option value="${value}" ${this._noteStatusDraft === value ? 'selected' : ''}>${I18n.__(key)}</option>`).join('')}
                    </select>
                    <label class="ui-check">
                        <input type="checkbox" id="noteInternal">
                        <span>${I18n.__('noteInternal')}</span>
                    </label>
                    <button type="button" data-send-reply onclick="UI_MODULES.replyToNote(${note.id}, this)"
                            class="ui-btn ui-btn-primary ui-push">${I18n.__('noteReply')}</button>
                </div>
            </div>`;
    },

    /**
     * Your own messages on the right, the worker's on the left - the same reading as
     * every messaging app, so "who said what" needs no label.
     */
    noteMessageHtml(message) {
        const mine = !!message.from_admin;
        const who = mine ? I18n.__('noteFromAdmin') : I18n.__('noteFromWorker');
        const tag = message.internal
            ? `<span class="ui-badge is-warn ui-spaced-start">${I18n.__('noteInternalTag')}</span>`
            : '';
        return `
            <div class="hand-bubble-row${mine ? ' is-mine' : ''}" data-message="${message.id}" data-internal="${message.internal ? 1 : 0}">
                <div class="hand-bubble${mine ? ' is-mine' : ''}">
                    <p class="hand-bubble-who">${who} · ${this.escapeHtml(message.created_at || '')}${tag}</p>
                    <p class="hand-bubble-body">${this.escapeHtml(message.body)}</p>
                </div>
            </div>`;
    },

    backToNotes() {
        this._noteThread = null;
        this._noteRevealed = null;
        return UI.renderAdminTab('Notes');
    },

    async replyToNote(noteId, button) {
        const field = document.getElementById('noteReplyBody');
        const statusField = document.getElementById('noteReplyStatus');
        const internalField = document.getElementById('noteInternal');
        const body = field ? String(field.value || '').trim() : '';
        if (!body) {
            Toast.error(I18n.__('noteBodyRequired'));
            return;
        }
        const status = statusField ? String(statusField.value || '') : '';
        if (button) button.disabled = true;
        try {
            await API.request(`/admin/notes/${noteId}/replies`, {
                method: 'POST',
                body: {
                    body: body,
                    internal: !!(internalField && internalField.checked),
                    status: status || null
                }
            });
        } catch (err) {
            if (button) button.disabled = false;
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('noteReplySent'));
        // Re-read so the thread shows the message that was just sent - and so the password
        // panel, if one is on screen, survives the repaint.
        await this.loadNote(noteId);
    },

    async setNoteStatus(noteId, status) {
        try {
            await API.request(`/admin/notes/${noteId}/status`, { method: 'POST', body: { status: status } });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('noteStatusSet'));
        await this.loadNote(noteId);
    },

    // -----------------------------------------------------------------
    //  Password help, answered from the note itself
    // -----------------------------------------------------------------

    /**
     * The panel that answers the note's actual request: a new password.
     *
     * It calls the same ``/admin/users/edit_password`` the Credentials tab calls, so a
     * reset from here invalidates the worker's sessions exactly the same way - and the
     * password is shown once, here, because this is the only moment it is readable.
     * The suggested reply below is prefilled, not sent: what the worker is told is the
     * administrator's sentence, in the language the two of them share.
     */
    notePasswordPanelHtml(note, field, quiet) {
        const revealed = this._noteRevealed;
        if (revealed) {
            return `
                <div class="ui-alert is-ok is-stacked" data-note-password-reveal="${this.escapeHtml(revealed.worker_id)}">
                    <p class="ui-card-title">${I18n.__('notePasswordSetFor')} ${this.escapeHtml(note.worker_name || revealed.worker_id)}</p>
                    <p class="ui-note is-body">${I18n.__('credentialsRevealNote')}</p>
                    <div class="ui-row">
                        <input id="noteRevealedPassword" readonly value="${this.escapeHtml(revealed.password)}" class="ui-field ui-mono is-flex">
                        <button type="button" onclick="UI_MODULES.copyNotePassword()" class="${quiet}">${I18n.__('credentialsCopyPassword')}</button>
                        <button type="button" onclick="UI_MODULES.dismissNotePassword()" class="${quiet}">${I18n.__('close')}</button>
                    </div>
                    <p class="ui-note">${I18n.__('noteSetPasswordHint')}</p>
                </div>`;
        }
        const protectedTarget = note.can_reset_password === false;
        return `
            <div class="ui-card is-tight is-stacked" data-note-password-panel>
                <div class="ui-row">
                    <input type="text" id="noteSetPasswordManual" value="${this.escapeHtml(this._notePasswordDraft)}"
                           oninput="UI_MODULES.setNotePasswordDraft(this.value)" autocomplete="new-password"
                           ${protectedTarget ? 'disabled' : ''}
                           placeholder="${this.escapeHtml(I18n.__('credentialsPasswordPlaceholder'))}" class="${field} ui-mono is-flex">
                    <button type="button" data-reset-password onclick="UI_MODULES.resetPasswordFromNote()"
                            ${protectedTarget ? 'disabled' : ''}
                            class="ui-btn ui-btn-warn">${I18n.__('noteSetPassword')}</button>
                    <p class="ui-note">${protectedTarget ? I18n.__('notePasswordProtected') : I18n.__('noteSetPasswordHint')}</p>
                </div>
            </div>`;
    },

    /**
     * Records the password typed beside the note's reset button.
     *
     * Blank is meaningful here rather than an error: the button generates one, so leaving
     * the box empty keeps the old one-tap reset for an admin with no password in mind.
     * No repaint - it would wipe the reply being written in the composer beside it.
     */
    setNotePasswordDraft(value) {
        this._notePasswordDraft = String(value === null || value === undefined ? '' : value);
    },

    async resetPasswordFromNote() {
        const note = this._noteThread;
        if (!note) return;
        if (note.can_reset_password === false) {
            Toast.error(I18n.__('notePasswordProtected'));
            return;
        }
        if (!confirm(`${I18n.__('noteSetPassword')} — ${note.worker_name || note.worker_id}? ${I18n.__('credentialsSignsOut')}`)) return;

        // A typed password wins; an empty box still gets the generator, so the reset keeps
        // working as a single tap for the admin who never touches the field.
        const typed = String(this._notePasswordDraft || '');
        const password = typed || this.generatePassword();
        try {
            await API.request('/admin/users/edit_password', {
                method: 'POST',
                body: { worker_id: note.worker_id, new_password: password, admin_id: State.user.id }
            });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('credentialsSaved'));
        this._noteRevealed = { worker_id: note.worker_id, password: password };
        this._notePasswordDraft = '';
        // The reply is prefilled, not sent: the admin reads it out (or edits it) and
        // decides when to close the note. A silent reset the worker is never told about
        // is a support call waiting to happen.
        this._noteStatusDraft = 'resolved';
        // ``loadNote``, not ``openNote``: the panel just created has to survive the
        // repaint that shows it.
        await this.loadNote(note.id);
        const box = document.getElementById('noteReplyBody');
        if (box && !box.value) {
            box.value = `${I18n.__('noteSetPassword')}. ${I18n.__('credentialsSaved')}`;
        }
    },

    copyNotePassword() {
        const revealed = this._noteRevealed;
        if (!revealed) {
            Toast.error(I18n.__('credentialsPasswordRequired'));
            return;
        }
        const done = () => Toast.success(I18n.__('copied'));
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(revealed.password).then(done).catch(() => prompt(I18n.__('credentialsCopyPassword'), revealed.password));
        } else {
            prompt(I18n.__('credentialsCopyPassword'), revealed.password);
        }
    },

    /** Forgetting the password here is the whole of the cleanup - it is stored nowhere. */
    async dismissNotePassword() {
        this._noteRevealed = null;
        const content = document.getElementById('adminContent');
        if (!content || !this._noteThread) {
            return UI.renderAdminTab('Notes');
        }
        this.paintNotes(content, this.noteThreadHtml(this._noteThread));
        return Promise.resolve();
    },

    escapeHtml(value) {
        const escapes = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
        return String(value ?? '').replace(/[&<>"']/g, (char) => escapes[char]);
    },

    /** "8.1" for 8.1, "8" for 8.0 - the same shape the worker card and server use. */
    hoursLabel(value) {
        const number = Number(value);
        return Number.isFinite(number) ? String(Number(number.toFixed(2))) : '0';
    }
};
