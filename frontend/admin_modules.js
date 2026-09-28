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
    //: The same guard for the force-in panel's roster, which is fetched on open: a panel
    //: opened, closed and reopened must not take the first answer's word for it.
    _forceInRun: 0,
    _liveOpsQuery: '',
    _liveOpsSite: '',
    _liveOpsSort: 'longest',
    //: Which way the board is read: the site a shift is at, or the *category* those sites
    //: belong to. Flipping it clears the filter, because a value that named a site names
    //: nothing once the rows are grouped by category.
    _liveOpsGroup: 'site',
    //: Whether the board is showing every open shift or only the first few.
    //:
    //: Held here rather than in the markup on purpose. ``paintLiveOps`` replaces the board's
    //: innerHTML whole - on the 45 s poll, on Refresh, and on the 1 s tick - so a fold that
    //: lived in a DOM attribute or a class name would snap shut under the operator's hands
    //: while they were reading row five, which is worse than never opening at all.
    _liveOpsExpanded: false,

    //: How often the board asks whether anything has moved. One *counted* read, and the rows
    //: behind it only when the answer is yes - polling that repaints regardless is what makes
    //: a dashboard feel broken, and polling that re-reads the rows to find out is what makes
    //: it cost the same as the list it is drawing.
    LIVE_OPS_POLL_MS: 45000,

    //: How many shifts the board shows before the fold. Two, because the board's job is the
    //: shift that needs a decision and a five-row wall of "on site, fine" is what buries it -
    //: and because two is also the number that fits above the fold on the phone this console is
    //: usually opened on. The rest are one tap away, and the control says how many.
    LIVE_OPS_FOLD: 2,

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
        table: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="M3 10h18M9 10v9"></path></svg>',
        printer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 8V4h10v4"></path><rect x="4" y="8" width="16" height="7" rx="2"></rect><path d="M7 15h10v5H7z"></path></svg>',
        eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg>'
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

    /**
     * A clock-in timestamp as a Date, or null when it cannot be read.
     *
     * Only for *display*. These digits are a zone-less wall clock, so the instant they
     * describe depends on where the reader is standing - see ``SHIFT_CLOCK`` and use
     * ``liveOpsFacts(...).origin`` for anything that counts.
     */
    liveOpsStart(clockInTime) {
        if (!clockInTime) return null;
        // One parse for both boards, in ``SHIFT_CLOCK.recordedAt``: what the worker's own
        // card reads and what this board reads have to be the same thing.
        const at = SHIFT_CLOCK.recordedAt(clockInTime);
        return at === null ? null : new Date(at);
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
     * Whether this arrival was flagged late.
     *
     * ``active_sessions.late_flag`` is either ``NULL`` (the arrival was inside the site's
     * window) or the *sentence* ``shift_windows.describe`` wrote when it was not - prose for a
     * notification body, never a boolean. This used to read ``late_flag === true || String(...) === '1'``,
     * which is right for a test's ``0``/``1`` and wrong for every value the server writes, so
     * the late badge and the late count were both quietly zero on a real deployment. The rule
     * is now the one ``live_ops._LATE_SQL`` counts with: something was written, and it is not
     * one of the two spellings of "no".
     */
    liveOpsIsLate(session) {
        const flag = session && session.late_flag;
        if (flag === null || flag === undefined || flag === false) return false;
        const text = String(flag).trim().toLowerCase();
        return text !== '' && text !== '0' && text !== 'false';
    },

    /**
     * What one open shift means *right now*.
     *
     * ``state`` is the line the shift has crossed, and it is the whole point of
     * the board: ``on`` is a normal working day, ``over`` has passed the overtime
     * line the server alerts on (``overtime_notify_hours``, a *paid* figure - so the
     * break is out of it before it is compared, exactly as the server does it), and
     * ``closing`` has reached the paid day the system will close it at. Whether an
     * ``over`` shift is then held for approval depends on the line against the paid
     * day (``shift_hours.overtime_assessment``): past the paid day needs a decision,
     * and a line set below the day only warns early.
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

        const late = this.liveOpsIsLate(session);
        const at = now === undefined ? Date.now() : Number(now);
        const start = this.liveOpsStart(session && session.clock_in_time);
        // Where the count starts. The server's own ``seconds_on_site`` is the figure the
        // shift is really at; falling back to the recorded stamp is the old arithmetic,
        // and it is right only while this console shares the server's zone.
        const origin = SHIFT_CLOCK.origin(session, at);
        if (origin === null) {
            return {
                start: start, origin: null, seconds: null, paidSeconds: null, percent: 0, closesAt: null,
                breakSeconds, daySeconds, overtimeSeconds, autoCloses, late, state: 'unknown'
            };
        }

        const seconds = SHIFT_CLOCK.elapsed(origin, at);
        // The same rule the server deducts by, so the two never disagree.
        const taken = seconds >= breakAfter * 3600 ? Math.min(breakSeconds, seconds) : 0;
        const paidSeconds = Math.max(0, seconds - taken);
        const state = paidSeconds >= daySeconds && autoCloses
            ? 'closing'
            : paidSeconds >= overtimeSeconds ? 'over' : 'on';
        return {
            start, origin, seconds, paidSeconds, daySeconds, overtimeSeconds, autoCloses, late, state,
            breakSeconds,
            // Projected off the *displayed* time, so the close time a console shows sits on
            // the same wall clock as the stamp printed beside it (see ``liveOpsStart``).
            closesAt: start && autoCloses ? new Date(start.getTime() + closingOffset * 1000) : null,
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
     * The clock-in time, the instant the count began and the late flag, on every
     * element the ticker rewrites.
     *
     * The tick runs off the DOM, not off a captured closure: it reads the start
     * time from the element it is about to change. Anything carrying
     * ``data-fact`` therefore has to carry ``data-start`` too - an element that
     * ticks without one is recomputed from `undefined` and paints "clock-in
     * unreadable" over a perfectly healthy row a second after it renders.
     *
     * ``data-origin`` rides along for the same reason and carries the *other* figure:
     * ``data-start`` is the wall clock as the server recorded it (shown, and ambiguous
     * in another zone), while ``data-origin`` is the instant the shift's count began,
     * converted once from the server's own ``seconds_on_site`` when the board was read.
     * The tick rewrites the elapsed figure and the overtime badge from the origin, so a
     * console in a different zone from the server cannot re-derive them from digits that
     * mean something else there.
     */
    liveOpsFactAttrs(facts) {
        const start = facts.start instanceof Date ? facts.start.toISOString() : '';
        const origin = Number.isFinite(facts.origin) ? new Date(facts.origin).toISOString() : '';
        return `data-start="${this.escapeHtml(start)}" data-origin="${this.escapeHtml(origin)}"` +
            ` data-late="${facts.late ? '1' : '0'}"`;
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
    /** Which way the board is read: ``'site'`` (the default) or ``'category'``. */
    liveOpsGroup() {
        return this._liveOpsGroup === 'category' ? 'category' : 'site';
    },

    /**
     * ``site_name`` -> category name, from the site list the board already read.
     *
     * Built once per read rather than looked up per row: the board polls, and a scan of the
     * site list inside the row loop is the kind of quiet cost that only shows on the
     * deployment with three hundred sites.
     */
    liveOpsCategoryMap(sites) {
        const map = new Map();
        for (const site of (sites || [])) {
            const name = String((site && site.site_name) || '');
            if (!name) continue;
            map.set(name, String((site && site.category) || ''));
        }
        return map;
    },

    /** The category of the site a session is at, or ``''`` when it has none. */
    liveOpsCategoryOf(map, session) {
        return map.get(String((session && session.site_name) || '')) || '';
    },

    liveOpsRows(data) {
        const query = String(this._liveOpsQuery || '').trim().toLowerCase();
        const site = this._liveOpsSite || '';
        const group = this.liveOpsGroup();
        const categories = this.liveOpsCategoryMap(data && data.sites);
        const elapsed = (facts) => (facts.seconds === null ? -1 : facts.seconds);
        const rows = ((data && data.sessions) || []).map((session) => ({
            session,
            category: this.liveOpsCategoryOf(categories, session),
            facts: this.liveOpsFacts(session, data && data.rules)
        })).filter(({ session, category }) => {
            // One field narrows the board either way: it holds a site in Site view and a
            // category in Category view, so the chip row is still the one control.
            if (site) {
                const value = group === 'category' ? category : String(session.site_name || '');
                if (value !== site) return false;
            }
            if (!query) return true;
            // The same contract as the Shifts search (``shiftsMatches``), so one question
            // gets one answer on both tabs: a term made only of digits is read as a worker
            // id and matched against what the row *is* - name, id, role - and never against
            // where the shift sits, whose site names carry the deployment's own digits
            // ("Salmiya Block 4") and used to answer a search for worker 4 with a second
            // row. Any other term - a word, or "block 4" inside a longer one - searches
            // everything, site and category included, because "block 4" is a place and the
            // words say so.
            const identity = [session.name, session.worker_id, session.role, this.roleLabel(session.role)]
                .map((value) => String(value === null || value === undefined ? '' : value).toLowerCase());
            const where = [session.site_name, category]
                .map((value) => String(value === null || value === undefined ? '' : value).toLowerCase());
            return query.split(/\s+/).filter(Boolean).every((term) => (/^\d+$/.test(term)
                ? identity.some((field) => field.indexOf(term) >= 0)
                : identity.concat(where).some((field) => field.indexOf(term) >= 0)));
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

    /**
     * The board's three figures, and the shift they are about.
     *
     * **Counted by the server when it can be** (``GET /admin/live_ops/count``): the same join
     * the rows use, in SQL, so these numerals no longer cost a roster-sized payload to
     * produce. The server answers the sites people are spread across - the *same figure* in
     * either grouping, mapped here through the category map the rows are grouped by - and the
     * longest open shift as a row reference, because "longest" is elapsed time and elapsed
     * time is computed from a stamp (see ``seconds_on_site``).
     *
     * **Counting the rows is the fallback, not the plan**: a counted read that cannot be
     * answered (a blip, an older server) leaves the board doing what every console did before
     * it existed, over the rows it is already showing. ``counted`` says which of the two the
     * caller got, which is what the tests hold, and the numerals are never a zero standing in
     * for a read that failed - the fallback *knows* the answer, it just worked it out here.
     */
    /**
     * Whether an answer really is the board's counted read, rather than an empty one.
     *
     * The figures are a *number* and nothing else passes: an empty object (an endpoint that
     * does not exist yet, a responder that answers everything with ``{}``) must not be read as
     * "nobody is on site", which is the one output that reads as good news. A body without the
     * count is not a count of zero, so the board falls back to counting its own rows - which it
     * can do, and which is what it did before the read existed.
     */
    liveOpsCountedRead(value) {
        return !!value && typeof value === 'object' && !Array.isArray(value)
            && typeof value.on_site === 'number' && isFinite(value.on_site);
    },

    liveOpsStats(data) {
        const group = this.liveOpsGroup();
        const categories = this.liveOpsCategoryMap(data && data.sites);
        const rows = ((data && data.sessions) || []).map((session) => ({
            session,
            category: this.liveOpsCategoryOf(categories, session),
            facts: this.liveOpsFacts(session, data && data.rules)
        }));
        // What the board says people are spread across: sites in Site view, categories in
        // Category view - the same figure over the same rows, read in the chosen grouping.
        const coveredOf = (pairs) => new Set(pairs.map(({ site_name }) => (group === 'category'
            ? this.liveOpsCategoryOf(categories, { site_name: site_name })
            : String(site_name || ''))).filter(Boolean));
        const count = data && this.liveOpsCountedRead(data.count) ? data.count : null;
        if (count) {
            const longestRow = count.longest ? Object.assign({ role: null, late_flag: null }, count.longest) : null;
            return {
                onSite: Number(count.on_site) || 0,
                sites: coveredOf(count.sites || []).size,
                group,
                longest: longestRow
                    ? { session: longestRow, facts: this.liveOpsFacts(longestRow, data.rules) }
                    : null,
                late: Number(count.late) || 0,
                counted: true
            };
        }
        let longest = null;
        for (const row of rows) {
            if (row.facts.seconds === null) continue;
            if (!longest || row.facts.seconds > longest.facts.seconds) longest = row;
        }
        return {
            onSite: rows.length,
            sites: coveredOf(rows.map((row) => row.session)).size,
            group,
            longest,
            late: rows.filter((row) => row.facts.late).length,
            counted: false
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
                <span class="ops-stat-hint">${this.escapeHtml(`${I18n.__(stats.group === 'category' ? 'liveOpsCategoriesCovered' : 'liveOpsSitesCovered')}: ${stats.sites}`)}</span>
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
            `;
        // There is no "past the paid day" figure here on purpose. The deployment does not run
        // payroll from this screen, so a count of shifts past a notional paid day was a number
        // nobody acted on, sitting where three figures somebody does act on already are. The
        // per-row state and the paid-hours line still say what a shift has run to; the board's
        // headline figures are now the ones an operator is here for.
    },

    /** The sentence the live region announces: counts, never a bare number. */
    liveOpsStatusSentence(data) {
        const stats = this.liveOpsStats(data);
        return I18n.__(stats.group === 'category' ? 'liveOpsStatusLineCategories' : 'liveOpsStatusLine')
            .replace('{workers}', String(stats.onSite))
            .replace('{sites}', String(stats.sites));
    },

    liveOpsStatusTime(data) {
        const at = new Date((data && data.at) || Date.now());
        const pad = (value) => String(value).padStart(2, '0');
        return I18n.__('liveOpsUpdated').replace('{time}', `${pad(at.getHours())}:${pad(at.getMinutes())}`);
    },

    /**
     * The site (or category) picker: one list of everything that can be chosen.
     *
     * A row of chips was the wrong instrument for this. Chips were built from the shifts that
     * happen to be running right now, so the site an operator was looking for and the site
     * nobody is at looked exactly alike - both simply absent - and the row grew with the
     * deployment instead of staying one control. The list is the whole of what can be chosen,
     * with the number of people behind each entry, and it is one field either way: a site in
     * Site view, a category in Category view.
     */
    liveOpsFilterSelectHtml(data) {
        const group = this.liveOpsGroup();
        const sessions = (data && data.sessions) || [];
        const categories = this.liveOpsCategoryMap(data && data.sites);
        const counts = new Map();
        for (const session of sessions) {
            const name = group === 'category'
                ? this.liveOpsCategoryOf(categories, session)
                : String(session.site_name || '');
            if (!name) continue;   // a shift with no site / no category has no entry of its own
            counts.set(name, (counts.get(name) || 0) + 1);
        }
        const selected = this._liveOpsSite || '';
        const option = (value, label) => `<option value="${this.escapeHtml(value)}"${selected === value ? ' selected' : ''}>${this.escapeHtml(label)}</option>`;
        const all = I18n.__(group === 'category' ? 'liveOpsAllCategories' : 'liveOpsAllSites');
        const names = Array.from(counts.keys()).sort();
        const label = I18n.__(group === 'category' ? 'liveOpsFilterByCategory' : 'liveOpsSiteFilter');
        return `<label class="sr-only" for="liveOpsFilter">${this.escapeHtml(label)}</label>
            <select class="ops-select" id="liveOpsFilter" data-live-ops-filter>${option('', `${all} (${sessions.length})`)}${names.map((name) => option(name, `${name} (${counts.get(name)})`)).join('')}</select>`;
    },

    /**
     * A change anywhere in the board's pane. The picker is the one delegated control that fires
     * on ``change`` rather than ``click`` - a ``data-`` hook, like every other control this file
     * gained, rather than an inline ``onchange`` the CSP's budget would have to grow for.
     */
    onLiveOpsChange(event) {
        const target = event && event.target;
        if (!target || typeof target.getAttribute !== 'function') return undefined;
        if (target.getAttribute('data-live-ops-filter') === null) return undefined;
        return this.setLiveOpsSite(target.value);
    },

    liveOpsSortSelectHtml() {
        const sort = this._liveOpsSort || 'longest';
        const option = (value, key) => `<option value="${value}"${sort === value ? ' selected' : ''}>${this.escapeHtml(I18n.__(key))}</option>`;
        return `<label class="sr-only" for="liveOpsSort">${this.escapeHtml(I18n.__('liveOpsSort'))}</label>
            <select class="ops-sort" id="liveOpsSort" data-sort-select onchange="UI_MODULES.setLiveOpsSort(this.value)">
                ${option('longest', 'liveOpsSortLongest')}${option('newest', 'liveOpsSortNewest')}${option('name', 'liveOpsSortName')}
            </select>`;
    },

    /**
     * The Site / Category switch: read the board by where people are, or by the kind of
     * place that is.
     *
     * A segmented pair rather than a select, because there are two views and both are worth
     * naming on the screen. Bound by a ``data-`` hook through ``onLiveOpsClick``, like the
     * fold: the toolbar is rebuilt often enough that the one inline allowance this file has
     * should keep falling rather than grow.
     */
    liveOpsGroupToggleHtml() {
        const group = this.liveOpsGroup();
        // ``data-live-ops-group`` and ``aria-pressed`` stay adjacent and in that order: the
        // suite reads which half is pressed off exactly that pair.
        const option = (value, key) => `<button type="button" class="ops-seg-btn" data-live-ops-group="${value}" aria-pressed="${group === value ? 'true' : 'false'}">${this.escapeHtml(I18n.__(key))}</button>`;
        return `<div class="ops-seg" role="group" aria-label="${this.escapeHtml(I18n.__('liveOpsViewBy'))}">${option('site', 'liveOpsGroupSite')}${option('category', 'liveOpsGroupCategory')}</div>`;
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

    /**
     * The small line under a name: the role, in the reader's words, then the rest.
     *
     * The board is where an operator decides what to do about who is on site, so the one
     * thing it must not do is make an administrator look like anybody else. They work a
     * shift here too, their hours are reviewed like anyone else's, and a row that printed
     * the wire code - or, on the card, nothing at all - left the reader to work out that
     * this shift is an administrator's from a name they had to recognise.
     *
     * Empty parts are dropped rather than joined blindly: a session with no role on file
     * used to read "\u00b7 1000", a separator with nothing in front of it.
     */
    liveOpsWhoSubHtml(parts) {
        return this.escapeHtml(parts.filter(Boolean).join(' \u00b7 '));
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
                            <span class="ops-sub">${this.liveOpsWhoSubHtml([this.roleLabel(session.role), session.worker_id])}</span>
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
                <td class="is-end">${this.mayActOnAccount(session) ? this.forceOutButtonHtml(session) : ''}</td>
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
                        <span class="ops-sub">${this.liveOpsWhoSubHtml([
                            this.roleLabel(session.role), session.site_name,
                            this.liveOpsClockTime(session.clock_in_time)
                        ])}</span>
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
                ${this.mayActOnAccount(session)
                    ? `<div class="ops-card-foot">${this.forceOutButtonHtml(session)}</div>`
                    : ''}
            </article>`;
    },

    /**
     * The control that ends a session, or nothing where this admin may not end it.
     *
     * One string rather than the same button written into the row and the card: the two
     * layouts differ in everything but this, and a second copy is where they would drift.
     */
    forceOutButtonHtml(session) {
        return `<button type="button" class="ops-btn ops-btn-danger" data-force-out="${this.escapeHtml(session.worker_id)}"
                            onclick="UI.forceOutModal('${this.liveOpsInlineString(session.worker_id)}', '${this.liveOpsInlineString(session.worker_name || session.worker_id)}', '${this.liveOpsInlineString(session.clock_in_time || '')}')">${this.escapeHtml(I18n.__('forceOut'))}</button>`;
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
                <p class="ops-empty-body">${this.escapeHtml(filtered
                    ? I18n.__(this.liveOpsGroup() === 'category' ? 'liveOpsNoMatchesHintCategory' : 'liveOpsNoMatchesHint')
                    : I18n.__('liveOpsNobodyHint'))}</p>
                ${action}
            </div>`;
    },

    /**
     * The visible rows under their headings. One heading per category, or nothing at all.
     *
     * Category view is the only grouping: a site is already the row's own column in Site
     * view, and a heading over every site would be a second copy of it. The uncategorised
     * bucket sorts last, where the reader who is not looking for it can ignore it.
     */
    liveOpsGroups(rows) {
        const group = this.liveOpsGroup();
        const buckets = new Map();
        for (const row of rows) {
            const key = group === 'category' ? row.category : String(row.session.site_name || '');
            if (!buckets.has(key)) buckets.set(key, { key, rows: [] });
            buckets.get(key).rows.push(row);
        }
        return Array.from(buckets.values()).map((bucket) => ({
            ...bucket,
            label: bucket.key || I18n.__('sitesCategoryNone'),
            count: bucket.rows.length
        })).sort((a, b) => {
            if (!a.key) return 1;
            if (!b.key) return -1;
            return a.label.localeCompare(b.label);
        });
    },

    liveOpsBoardHtml(data) {
        const rows = this.liveOpsRows(data);
        const total = ((data && data.sessions) || []).length;
        if (rows.length === 0) return this.liveOpsEmptyHtml(total);
        // Category view is grouped *whole* and never folded. The fold exists to keep a flat
        // list of forty names from being a wall; a board with a heading per kind of place is
        // read one heading at a time, and folding the list *before* grouping is worse than
        // either - the first few rows belong to whichever category the first person happens to
        // be at, so the board would show one heading and hide every other category behind a
        // "Show more" that looks like it is about people rather than about kinds of place.
        if (this.liveOpsGroup() === 'category') {
            return this.liveOpsGroupedBoardHtml(rows);
        }
        const visible = this.liveOpsVisibleRows(rows);
        const body = Device.isMobile ? this.liveOpsCardsHtml(visible) : this.liveOpsTableHtml(visible);
        return `${body}${this.liveOpsFoldHtml(rows)}`;
    },

    /**
     * The board under one heading per category.
     *
     * A single category - or none, on a board nobody has categorised - is the flat board:
     * one heading over every row is a heading that tells the reader nothing. Either way the
     * rows are drawn by the same table or cards the Site view uses, so a grouped board is
     * never a second renderer that can drift from the first.
     */
    liveOpsGroupedBoardHtml(rows) {
        const groups = this.liveOpsGroups(rows);
        if (groups.length <= 1) {
            return Device.isMobile ? this.liveOpsCardsHtml(rows) : this.liveOpsTableHtml(rows);
        }
        return groups.map((group) => `
            <section class="ops-group" data-live-ops-group-section="${this.escapeHtml(group.key)}">
                <h3 class="ops-group-head">${this.escapeHtml(group.label)}
                    <span class="ops-group-count">${this.escapeHtml(I18n.__('liveOpsGroupCount').replace('{count}', String(group.count)))}</span>
                </h3>
                ${Device.isMobile ? this.liveOpsCardsHtml(group.rows) : this.liveOpsTableHtml(group.rows)}
            </section>`).join('');
    },

    /**
     * The rows on the board: the first few, or all of them once the fold is opened.
     *
     * Outside the two list layouts on purpose, so the table and the phone's cards fold at
     * exactly the same point - a fold implemented in each of them is a fold that will differ
     * between them, and the operator on the phone is the one who would be told a different
     * number of people are on site.
     */
    liveOpsVisibleRows(rows) {
        if (this._liveOpsExpanded) return rows;
        return rows.slice(0, this.LIVE_OPS_FOLD);
    },

    /**
     * The fold's control, or nothing at all when the board already fits.
     *
     * It carries the number it is hiding - "Show more (2)" - because that is the question an
     * operator asks before tapping it, and it is a *button* with ``aria-expanded`` rather than a
     * styled link: a screen reader has to be able to say whether the list under it is open.
     *
     * The words are the fold's own, and never the filter's. "Showing 2 of 4" already means "a
     * search or a site chip is narrowing this board", and a fold reported in that same sentence
     * would leave an operator unable to tell a filter they set from a fold they forgot - the two
     * look identical and mean opposite things about the figures above.
     */
    liveOpsFoldHtml(rows) {
        if (rows.length <= this.LIVE_OPS_FOLD) return '';
        const hidden = rows.length - this.LIVE_OPS_FOLD;
        const expanded = !!this._liveOpsExpanded;
        const label = expanded
            ? I18n.__('liveOpsShowLess')
            : I18n.__('liveOpsShowMore').replace('{count}', String(hidden));
        // No handler attribute: the control is inside a board whose innerHTML is replaced on
        // every tick and poll, and the click is delegated from ``renderLiveOps`` instead
        // (``onLiveOpsClick``). A handler written into the markup is also a handler the
        // document's CSP has to allow inline for, which is the allowance an injected image
        // with a bogus source needs.
        return `
            <div class="ops-fold">
                <button type="button" class="ops-btn" data-live-ops-toggle
                        aria-expanded="${expanded ? 'true' : 'false'}">${this.escapeHtml(label)}</button>
            </div>`;
    },

    /**
     * One tap on the fold: remembered, then repainted from the read already in hand.
     *
     * No request and no full re-render of the tab - the rows being revealed are the ones the
     * server already sent, so this is a repaint of the board, exactly like the poll. It
     * returns the new state so a caller (and a test) can read it back.
     */
    toggleLiveOpsExpanded() {
        this._liveOpsExpanded = !this._liveOpsExpanded;
        if (this._liveOps) this.paintLiveOps(this._liveOps);
        return this._liveOpsExpanded;
    },

    /**
     * A click anywhere in the board's pane. The fold is the only delegated control so far;
     * anything else has its own handler, and a click that is not the fold falls through.
     */
    onLiveOpsClick(event) {
        const target = event && event.target;
        if (!target || typeof target.closest !== 'function') return undefined;
        if (target.closest('[data-live-ops-toggle]')) return this.toggleLiveOpsExpanded();
        const group = target.closest('[data-live-ops-group]');
        if (group) return this.setLiveOpsGroup((group.dataset || {}).liveOpsGroup);
        return undefined;
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
                    ${this.liveOpsFilterSelectHtml(data)}
                    ${Device.isMobile ? this.liveOpsSortSelectHtml() : ''}
                    ${this.liveOpsGroupToggleHtml()}
                </div>
                <details class="ops-panel" id="liveOpsForceIn"${this._forceInOpen ? ' open' : ''} ontoggle="UI_MODULES.liveOpsPanelToggled(this)">
                    <summary>${this.OPS_ICONS.person}<span>${this.escapeHtml(I18n.__('forceInTitle'))}</span></summary>
                    <div id="liveOpsForceInBody">${this.liveOpsForceInBodyHtml(data)}</div>
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

    /**
     * The board's own reads: the rows it draws, the figures it counts, and the two rules.
     *
     * **The roster is not one of them.** It used to be - the whole ``/admin/users`` payload,
     * whose rows each carry ``password_set`` and an audit-log join for the last password
     * change, fetched so that a board of who is *on site* could fill a picker of who is not.
     * That panel is a roster and it loads like one: when somebody opens it (see
     * ``liveOpsPanelToggled``). What is left here is what the board cannot be drawn without -
     * the open shifts, the sites (a picker and a category map, one row per *site*), the shift
     * rules (one row) and the counted figures.
     *
     * Two of the four may fail without taking the screen with them, and each for its own
     * reason: ``rules`` has documented defaults, and a counted read that cannot be answered
     * leaves ``liveOpsStats`` counting the rows instead (which is what this console did
     * before the read existed).
     */
    async fetchLiveOps() {
        const [sessions, count, sites, rules] = await Promise.all([
            API.request('/admin/active_sessions'),
            API.request('/admin/live_ops/count').catch(() => null),
            API.request('/admin/sites'),
            API.request('/admin/shift_rules').catch(() => ({}))
        ]);
        const list = (value) => (Array.isArray(value) ? value : []);
        return {
            sessions: list(sessions),
            count: this.liveOpsCountedRead(count) ? count : null,
            sites: list(sites),
            rules: rules && typeof rules === 'object' && !Array.isArray(rules) ? rules : {},
            at: Date.now()
        };
    },

    /**
     * The force-in panel's body: the roster, which is the one payload a board should not carry.
     *
     * Drawn from the count the board already has for everything else - the panel needs the
     * people who are *not* on shift, which is a list of accounts, so it is fetched when the
     * panel is opened rather than with every render, Refresh and poll of the board behind it.
     * Until then this is the same ``loadingHtml`` the links panel and the credentials panel
     * put in a panel they are about to fill, and it costs one line of markup on a screen
     * nobody has opened.
     */
    liveOpsForceInBodyHtml(data) {
        if (!data || !data.users) return UI.loadingHtml();
        return UI.forceInPanelHtml(data.sessions || [], data.users || [], data.sites || []);
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
        // The fold's control, delegated: the button is a string at paint time, so there is no
        // node to attach to - and a handler per paint would be a handler per repaint. Assigned
        // rather than added, like the Shifts tab's, so a repaint cannot leave the previous
        // paint's listener behind on the same element.
        content.onclick = (event) => this.onLiveOpsClick(event);
        // The picker fires on ``change``, not ``click``: one delegated listener beside the
        // click one, assigned rather than added for the same reason - a repaint must not leave
        // the previous paint's listener behind on the same element.
        content.onchange = (event) => this.onLiveOpsChange(event);
        this.startLiveOps();
        // A panel that was open when the tab was left is open again, and its roster is the one
        // thing this render did not fetch: a disclosure already open fires no ``toggle``, so
        // nothing else would ever ask for it.
        if (this._forceInOpen && !data.users) this.liveOpsPanelToggled(document.getElementById('liveOpsForceIn'));
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
        // A board somebody has left starts folded again. This is the only place the state is
        // cleared (``startLiveOps`` calls this method), and it is deliberately *not* the poll
        // or the tick: those repaint the board the operator is already reading, and the one
        // thing that must not happen there is the list closing by itself.
        this._liveOpsExpanded = false;
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
            // repaint of the pane it is looking at. ``data-origin`` is the whole
            // record's clock - the instant the count began - and it is what the
            // elapsed figure and the hour state are recomputed from.
            const facts = this.liveOpsFacts({
                clock_in_time: el.dataset.start,
                origin_at: el.dataset.origin,
                late_flag: el.dataset.late
            }, rules);
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

        // The headline figures move too: a shift crossing the line changes what the longest
        // row means, and which shift that is.
        const stats = this.liveOpsStats(data);
        const longest = document.getElementById('liveOpsStatLongest');
        if (longest) longest.textContent = stats.longest ? this.liveOpsDuration(stats.longest.facts.seconds) : '\u2014';
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
        let count = null;
        try {
            count = await API.request('/admin/live_ops/count');
        } catch (err) {
            count = null;   // falls back to the rows below, which is what this poll used to do
        }
        if (!this._liveOps || State.adminTab !== 'Live Ops') return;
        if (this.liveOpsCountedRead(count)) {
            const fresh = { ...this._liveOps, count, at: Date.now() };
            // Nothing the board draws moved: the figures it is showing are the ones it has,
            // and the rows that go with them are already on the page.
            if (!this.liveOpsMoved(count, this._liveOps.count)) { this._liveOps = fresh; return; }
            let sessions;
            try {
                sessions = await API.request('/admin/active_sessions');
            } catch (err) {
                return;   // a blip keeps the board up, with its last known-good rows
            }
            if (!this._liveOps || State.adminTab !== 'Live Ops') return;
            this._liveOps = { ...fresh, sessions: Array.isArray(sessions) ? sessions : [] };
            this.paintLiveOps(this._liveOps);
            return;
        }
        // No counted read: compare the rows themselves, exactly as this poll did before the
        // counted read existed.
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

    /**
     * Whether a counted read says anything the board is drawing has moved.
     *
     * The five facts the row signature covered, answered by the server instead of by a
     * downloaded list: who is on shift (the ids, in no order, hence the sort), where they are
     * (the sites and how many of them), how many arrived late, and which shift is the oldest.
     * It is deliberately not a comparison of the whole payload - ``as_of`` moves on every
     * read, and a board that repainted because the clock advanced is the board that twitches.
     *
     * A **name** is the one thing this cannot see: renaming somebody who is on shift leaves
     * every fact here unchanged, so the new name arrives with the next row read, or with the
     * operator's own Refresh (which is exact, and always was). That is the trade - a board
     * that re-reads the rows every 45 s to catch a rename is a board paying the row payload to
     * be told that nothing happened.
     */
    liveOpsMoved(count, previous) {
        if (!count || !previous) return true;
        const facts = (value) => JSON.stringify({
            on_site: Number(value.on_site) || 0,
            late: Number(value.late) || 0,
            sites: (value.sites || []).map((site) => [String(site.site_name || ''), Number(site.workers) || 0]),
            ids: (value.worker_ids || []).map(String).sort(),
            longest: value.longest
                ? [String(value.longest.worker_id), String(value.longest.clock_in_time)]
                : null
        });
        return facts(count) !== facts(previous);
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

    /** Repaint the force-in panel's body in place, if the panel is on the page at all. */
    paintForceInBody(data) {
        const body = document.getElementById('liveOpsForceInBody');
        if (body) body.innerHTML = this.liveOpsForceInBodyHtml(data);
    },

    /** The Refresh button: a full re-read, but the toolbar stays put. */
    async refreshLiveOps() {
        try {
            const fresh = await this.fetchLiveOps();
            // A roster the panel already loaded is not in ``fetchLiveOps`` any more, so a
            // Refresh must carry it across rather than empty the panel somebody is looking at.
            const users = this._liveOps && this._liveOps.users;
            this._liveOps = users ? { ...fresh, users } : fresh;
            this.paintLiveOps(this._liveOps);
            // Never redraw a panel somebody is part-way through filling in.
            if (!this._forceInOpen) this.paintForceInBody(this._liveOps);
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
        // The picker already holds the value the reader chose - it is the control that called
        // this - so only the board and the sentence under it are repainted.
        const select = document.getElementById('liveOpsFilter');
        if (select) select.value = this._liveOpsSite;
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(this._liveOps);
        const note = document.getElementById('liveOpsFilterNote');
        if (note) note.textContent = this.liveOpsFilterNoteHtml(this._liveOps);
    },

    /**
     * Flip the board between sites and site categories.
     *
     * A full re-render rather than a patch of the board: the view changes the filter chips'
     * values *and* the stats label beside them, which live in the toolbar this setter does
     * not own - and a chip row still offering sites while the rows under it are grouped by
     * category is the one thing this switch must never show. It drops any filter for the same
     * reason a filter is unusable across the flip: the value that named a site names nothing
     * once the rows are grouped by category.
     */
    setLiveOpsGroup(value) {
        const group = value === 'category' ? 'category' : 'site';
        if (group === this.liveOpsGroup()) return undefined;
        this._liveOpsGroup = group;
        State.liveOpsGroup = group;
        this._liveOpsSite = '';
        State.liveOpsSite = '';
        // The render is returned so a caller - and the suite that drives this file - can wait
        // for the board it asked for, exactly like ``renderAdminTab`` itself.
        return UI.renderAdminTab('Live Ops');
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
        const select = document.getElementById('liveOpsFilter');
        if (select) select.value = '';
        const board = document.getElementById('liveOpsBoard');
        if (board) board.innerHTML = this.liveOpsBoardHtml(this._liveOps);
        const note = document.getElementById('liveOpsFilterNote');
        if (note) note.textContent = this.liveOpsFilterNoteHtml(this._liveOps);
    },

    /**
     * The force-in panel, opened: remember that, and load the roster if it is not here yet.
     *
     * The fetch paints the *panel*, never the board: the disclosure is open, and whatever
     * somebody has already typed into its fields lives in that subtree - a repaint of the
     * board would fold the panel shut and blank the form, which is the same mistake
     * ``paintLiveOps`` exists to avoid on the board itself. A failure says so in the panel and
     * leaves the rest of the screen alone; the operator can close and reopen it to retry.
     */
    async liveOpsPanelToggled(details) {
        this._forceInOpen = !!(details && details.open);
        if (!this._forceInOpen || !this._liveOps || this._liveOps.users) return;
        const body = document.getElementById('liveOpsForceInBody');
        if (!body) return;
        const wanted = ++this._forceInRun;
        body.innerHTML = UI.loadingHtml();
        try {
            const users = await API.request('/admin/users');
            if (wanted !== this._forceInRun || !this._liveOps) return;   // the tab has moved on
            this._liveOps = { ...this._liveOps, users: Array.isArray(users) ? users : [] };
            body.innerHTML = UI.forceInPanelHtml(
                this._liveOps.sessions || [], this._liveOps.users, this._liveOps.sites || []
            );
        } catch (err) {
            if (wanted !== this._forceInRun) return;
            body.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml((err && err.message) || '')}</p>`;
        }
    },

    /** The empty board's one action: open the panel and put the caret in it. */
    openForceIn() {
        this._forceInOpen = true;
        const panel = document.getElementById('liveOpsForceIn');
        if (panel) panel.open = true;
        // Setting ``open`` does not fire ``toggle`` everywhere (and never in the test DOM), so
        // the roster is asked for here rather than waited for.
        this.liveOpsPanelToggled(panel);
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

    /**
     * The evidence block: the frame, the match verdict, the liveness verdict, and the
     * sentence that put this row in the queue.
     *
     * Deliberately not an ``<img src>`` (see ``showLinkPhoto``): the frame is a worker's face
     * served to administrators only, so the bytes are fetched with the session credential
     * and turned into an object URL this page alone can read. A missing frame is stated, not
     * hidden - "no picture" and "the picture failed to load" are different facts, and a
     * reviewer must know which one they are looking at.
     */
    approvalsEvidenceHtml(log) {
        const id = this.escapeHtml(log.id);
        const verdictKey = {
            approved: 'approvalsVerdictApproved',
            review: 'approvalsVerdictReview',
            refused: 'approvalsVerdictRefused',
        }[log.match_verdict];
        const distance = (log.score !== null && log.score !== undefined && Number(log.score) !== 0 && Number(log.score) !== 1)
            ? Number(log.score).toFixed(2)
            : null;
        const liveness = log.liveness_class && log.liveness_class !== 'unverified_offline'
            ? this.escapeHtml(this.approvalsLivenessLabel(log.liveness_class, log.liveness_score))
            : null;
        const facts = [
            distance !== null ? { label: I18n.__('approvalsMatchScore'), value: distance } : null,
            verdictKey ? { label: I18n.__('approvalsMatchVerdict'), value: I18n.__(verdictKey) } : null,
            liveness ? { label: I18n.__('approvalsLiveness'), value: liveness } : null,
        ].filter(Boolean);
        const frameButton = log.frame_url
            ? `<button type="button" class="ui-btn ui-btn-sm" data-show-frame="${id}">${this.OPS_ICONS.eye}${this.escapeHtml(I18n.__('approvalsShowFrame'))}</button>`
            : '';
        const frameNote = log.frame_url
            ? ''
            : `<p class="ui-section-note" data-frame-missing>${this.escapeHtml(I18n.__('approvalsFrameMissing'))}</p>`;
        return `
            <div class="ui-evidence" data-evidence="${id}">
                <p class="ops-stat-label">${this.escapeHtml(I18n.__('approvalsEvidence'))}</p>
                <div class="ui-evidence-row">
                    <div class="ui-evidence-media">
                        <img id="reviewFrame${id}" class="hidden ui-photo" alt="${this.escapeHtml(I18n.__('approvalsFrameAlt'))}" />
                    </div>
                    <div class="ui-evidence-facts">
                        ${frameButton}
                        ${frameNote}
                        ${facts.length ? `<div class="ui-facts">${facts.map((f) => `
                            <div class="ui-fact">
                                <span class="ops-stat-label">${this.escapeHtml(f.label)}</span>
                                <span class="ui-fact-value">${f.value}</span>
                            </div>`).join('')}</div>` : ''}
                    </div>
                </div>
                ${log.flag_reason ? `<p class="ui-note is-warn" data-flag-reason>${this.OPS_ICONS.alert}${this.escapeHtml(log.flag_reason)}</p>` : ''}
            </div>`;
    },

    /**
     * One liveness clause: the class as words, with the model's confidence when it has one.
     * The classes are the log codes ``liveness.log_fields`` writes - shown as words here,
     * never as the raw code, the same rule ``codeLabel`` follows for notification kinds.
     */
    approvalsLivenessLabel(livenessClass, livenessScore) {
        const key = {
            live: 'livenessClassLive',
            print_attack: 'livenessClassPrint',
            replay_attack: 'livenessClassReplay',
            unknown: 'livenessClassUnknown',
            unavailable: 'livenessClassUnavailable',
            error: 'livenessClassError',
        }[livenessClass];
        const label = key ? I18n.__(key) : String(livenessClass);
        const confidence = (livenessScore === null || livenessScore === undefined) ? null : Number(livenessScore);
        return confidence === null || Number.isNaN(confidence)
            ? label
            : `${label} (${confidence.toFixed(2)})`;
    },

    /**
     * One frame, fetched with this admin's token. The same pattern as ``showLinkPhoto``:
     * the endpoint answers 404 for a frame retention wiped or a punch that never had one,
     * and the note in the card says that rather than a broken image icon.
     */
    async showReviewFrame(logId) {
        const image = document.getElementById(`reviewFrame${logId}`);
        const button = document.querySelector(`[data-show-frame="${CSS.escape(String(logId))}"]`);
        const headers = {};
        if (State.token) headers['Authorization'] = `Bearer ${State.token}`;
        if (button) button.disabled = true;
        try {
            const response = await fetch(`${API.baseURL}/admin/pending_review_frame/${encodeURIComponent(logId)}`, { headers });
            if (!response.ok) throw new Error(I18n.__('approvalsScoreFailed'));
            const blob = await response.blob();
            if (image) {
                image.src = URL.createObjectURL(blob);
                image.classList.remove('hidden');
            }
            if (button) button.remove();
        } catch (err) {
            Toast.error(err.message || I18n.__('approvalsScoreFailed'));
            if (button) button.disabled = false;
        }
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
                ${this.approvalsEvidenceHtml(log)}
                ${this.approvalsDecisionHtml(log, idArg)}
            </article>`;
    },

    /**
     * The decision - the reason box and the two answers - or nothing for a reader who may not
     * make it.
     *
     * An administrator's own long shift reaches this queue like anybody else's and waits like
     * anybody else's (``test_admin_shift_visibility``). Who *decides* it is the one place the
     * role matters: authorising a peer's overtime is the escalation ``_guard_standard_admin``
     * exists to refuse, so for a standard admin reading an administrator's row this renders
     * as evidence - the face, the verdict, the hours - and no box to type a decision into.
     */
    approvalsDecisionHtml(log, idArg) {
        if (!this.mayActOnAccount(log)) return '';
        const id = this.escapeHtml(log.id);
        return `
                <label class="ui-label" for="note-${id}" style="margin-top:16px">${this.escapeHtml(I18n.__('approvalsNote'))}</label>
                <textarea id="note-${id}" class="ui-field" rows="2"
                          placeholder="${this.escapeHtml(I18n.__('approvalsNotePlaceholder'))}"></textarea>
                <p class="ui-section-note" style="margin-top:6px">${this.escapeHtml(I18n.__('approvalsNoteUsed'))}</p>
                <div class="ui-row" style="margin-top:12px">
                    <button type="button" class="ui-btn ui-btn-primary" data-approve="${id}"
                            onclick="UI_MODULES.handleApproval(${idArg}, 'approve')">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('approvalsApprove'))}</button>
                    <button type="button" class="ui-btn ui-btn-danger" data-reject="${id}"
                            onclick="UI_MODULES.handleApproval(${idArg}, 'reject')">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('approvalsReject'))}</button>
                </div>`;
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
        // Two questions, two reads, in flight *together*: a shift that is still running is a
        // different question from a clock-out waiting for review, so the tab waits for the
        // slower of the two rather than for their sum. They are separately fatal as well -
        // a crossings read that fails leaves its own section empty (the queue below is still
        // the screen's primary content), and only the queue's failure replaces the screen.
        //
        // The ``.catch`` is attached where the read starts, not where it is awaited: on the
        // error path below this promise is never awaited, and an unhandled rejection is a
        // console error nobody can act on.
        const crossingsRead = API.request('/admin/overtime/crossings').catch(() => []);
        // The refusals read is separately fatal too: it is triage evidence, not the queue's
        // primary content, and a read that fails leaves its own section empty.
        //
        // Asked for by the root tier only, and asked at the route it now lives on. The surface
        // left the administrator's queue - a refusal is a verdict about the *band*, not about a
        // site's attendance - so a console that requested it for every operator would be a
        // screenful of 403s for the audience the tab belongs to, and a section that never
        // paints. The server refuses it either way; this is the console agreeing with the
        // server rather than a second, weaker rule.
        const refusalsRead = this.isDeveloper()
            ? API.request('/developer/refused-punches?days=1').catch(() => [])
            : Promise.resolve([]);
        let logs;
        try {
            logs = await API.request('/admin/pending_reviews');
        } catch (err) {
            // A failed read used to leave the previous screen up, or throw. It says what
            // went wrong now, and offers the one control that can fix it.
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Approvals')");
            return;
        }
        const crossings = await crossingsRead;
        const refusals = await refusalsRead;
        content.innerHTML = `<div class="ui-page" data-approvals="true">${this.crossingsSectionHtml(Array.isArray(crossings) ? crossings : [])}${this.approvalsHtml(Array.isArray(logs) ? logs : [])}${this.refusalsSectionHtml(Array.isArray(refusals) ? refusals : [])}</div>`;
        // One delegated pass over every "show the frame" button on the page: a repaint
        // between paint and tap cannot orphan it, and no inline handler is added (the count
        // ``test_frontend_xss`` pins only falls). The guards are the same ones
        // ``bindCredentialsControls`` carries: the stub DOMs the frontend suites run under
        // give ``content`` as a bare element without ``querySelectorAll``.
        if (content && typeof content.querySelectorAll === 'function') {
            // One delegated pass per control, and never an assumption about what
            // ``querySelectorAll`` hands back. In a browser it is a ``NodeList``; in the stubbed
            // DOM the frontend suites run under it is a plain array, and on a guard rail built
            // for one of them the other becomes a crash on a tab that otherwise works.
            // ``Array.from`` accepts an array, an array-like and an iterable, and answers ``[]``
            // for anything else - so a stub with no tree, no ``querySelectorAll`` or a shape
            // nobody has thought of yet cannot take the screen down.
            const bindEach = (selector, handler) => {
                let nodes = [];
                try {
                    nodes = Array.from(content.querySelectorAll(selector) || []);
                } catch (err) {
                    nodes = [];
                }
                nodes.forEach((node) => {
                    if (node && typeof node.addEventListener === 'function') handler(node);
                });
            };
            bindEach('[data-show-frame]', (button) => {
                button.addEventListener('click', () => {
                    this.showReviewFrame(button.getAttribute('data-show-frame'));
                });
            });
            bindEach('[data-refusal-show-frame]', (button) => {
                button.addEventListener('click', () => {
                    this.showRefusalFrame(button.getAttribute('data-refusal-show-frame'));
                });
            });
            bindEach('[data-refusal-clear]', (button) => {
                button.addEventListener('click', () => {
                    this.handleRefusalClear(button.getAttribute('data-refusal-clear'));
                });
            });
            // The two answers to a live crossing, bound the same way and for the same reasons:
            // a repaint between paint and tap cannot orphan a listener, and no inline handler is
            // added - ``test_frontend_xss`` pins the count of those, and it only ever falls.
            bindEach('[data-crossing-accept]', (button) => {
                button.addEventListener('click', () => {
                    this.handleCrossing(button.getAttribute('data-crossing-accept'), true);
                });
            });
            bindEach('[data-crossing-decline]', (button) => {
                button.addEventListener('click', () => {
                    this.handleCrossing(button.getAttribute('data-crossing-decline'), false);
                });
            });
        }
    },

    /**
     * The live crossings: shifts that are past the overtime line *right now*.
     *
     * A separate section rather than rows in the queue below, because the two are different
     * questions with different consequences. A queue row is a shift that has ended and is
     * waiting to be priced; a crossing is a shift that has not ended, where the answer changes
     * what the rest of it is paid for. Reading a queue row is a step towards deciding it;
     * reading a crossing decided nothing at all, which is why it does not live in Alerts.
     */
    /**
     * Is the reader the root tier - the role that reads the deployment's own business?
     *
     * One line, and it is the shell's line: ``isRootTier`` decides which tabs the nav
     * offers and what this module asks ``/developer/*`` for, so both must answer the same
     * question or the console would offer a screen it then refuses to fetch.
     */
    isDeveloper() {
        return isRootTier();
    },

    /**
     * The refused punches: today's face-check refusals, the frame beside the score.
     *
     * A refusal is evidence about the system as much as the worker - a band derived from the
     * wrong corpus refuses honest workers all day at scores just past the line, and the only
     * way to see that is the scores and the faces together. That makes it a question about the
     * model rather than about a site's attendance, which is why it is the **root tier's** and
     * why an administrator's Approvals tab no longer carries it: the list, the frame and the
     * clear all live on ``/developer/refused-punches``, and the server refuses the old paths.
     *
     * Read-only: there is no approve path (a refusal was never attendance), only a clear so the
     * triaged list shrinks.
     */
    refusalsSectionHtml(items) {
        if (!Array.isArray(items) || items.length === 0) return '';
        return `
            <p class="ui-section-note" data-refusals-title="true">${this.escapeHtml(I18n.__('refusalsTitle'))}</p>
            <p class="ui-section-note" data-refusals-hint="true">${this.escapeHtml(I18n.__('refusalsHint'))}</p>
            <div class="ui-stack">${items.map((item) => this.refusalCardHtml(item)).join('')}</div>`;
    },

    refusalCardHtml(item) {
        const id = this.escapeHtml(String(item.id));
        const who = this.escapeHtml(item.name || item.worker_id || '?');
        const sub = this.escapeHtml([item.worker_id, item.site_name]
            .filter(Boolean).join(' \u00b7 '));
        const score = item.score === null || item.score === undefined
            ? '\u2014'
            : Number(item.score).toFixed(4);
        const action = item.action === 'clock_out' ? I18n.__('forceOut') : I18n.__('forceIn');
        const when = String(item.created_at || '');
        return `
            <article class="ui-card" data-refusal="${id}">
                <div class="ui-row ui-row-between">
                    <div>
                        <strong>${who}</strong>
                        <div class="ops-stat-label">${sub}</div>
                    </div>
                    <span class="ui-badge is-warn">${this.escapeHtml(action)}</span>
                </div>
                <div class="ops-stat-grid" style="margin-top:12px">
                    <div class="ops-stat">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('refusalsScore'))}</span>
                        <span class="ops-stat-value">${this.escapeHtml(score)}</span>
                    </div>
                    <div class="ops-stat">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('refusalsReason'))}</span>
                        <span class="ops-stat-value">${this.escapeHtml(String(item.error_code || ''))}</span>
                    </div>
                    <div class="ops-stat">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('refusalsWhen'))}</span>
                        <span class="ops-stat-value">${this.escapeHtml(when)}</span>
                    </div>
                </div>
                <img id="refusalFrame${id}" class="hidden" alt="" style="margin-top:12px;max-width:160px;border-radius:8px" />
                <div class="ui-row" style="margin-top:12px">
                    ${item.frame_url ? `<button type="button" class="ui-btn" data-refusal-show-frame="${id}">${this.escapeHtml(I18n.__('refusalsShowFrame'))}</button>` : ''}
                    <button type="button" class="ui-btn" data-refusal-clear="${id}">${this.escapeHtml(I18n.__('refusalsClear'))}</button>
                </div>
            </article>`;
    },

    /** The same authenticated-blob fetch the review frame uses, against the refusal route. */
    async showRefusalFrame(refusalId) {
        const id = String(refusalId);
        const image = document.getElementById(`refusalFrame${id}`);
        const button = document.querySelector(`[data-refusal-show-frame="${CSS.escape(id)}"]`);
        const headers = {};
        if (State.token) headers['Authorization'] = `Bearer ${State.token}`;
        if (button) button.disabled = true;
        try {
            const response = await fetch(`${API.baseURL}/developer/refused-punches/${encodeURIComponent(id)}/frame`, { headers });
            if (!response.ok) throw new Error(I18n.__('approvalsScoreFailed'));
            const blob = await response.blob();
            if (image) {
                image.src = URL.createObjectURL(blob);
                image.classList.remove('hidden');
            }
            if (button) button.remove();
        } catch (err) {
            Toast.error(err.message || I18n.__('approvalsScoreFailed'));
            if (button) button.disabled = false;
        }
    },

    async handleRefusalClear(refusalId) {
        const id = String(refusalId);
        try {
            await API.request(`/developer/refused-punches/${encodeURIComponent(id)}/clear`, { method: 'POST' });
            Toast.success(I18n.__('refusalsCleared'));
            this.renderAdminTab('Approvals');
        } catch (err) {
            Toast.error(err.message);
        }
    },

    crossingsSectionHtml(items) {
        if (!Array.isArray(items) || items.length === 0) return '';
        return `
            <p class="ui-section-note" data-crossings-title="true">${this.escapeHtml(I18n.__('crossingsTitle'))}</p>
            <p class="ui-section-note" data-crossings-hint="true">${this.escapeHtml(I18n.__('crossingsHint'))}</p>
            <div class="ui-stack">${items.map((item) => this.crossingCardHtml(item)).join('')}</div>`;
    },

    crossingCardHtml(item) {
        const id = this.escapeHtml(String(item.worker_id));
        const decision = item.decision || null;
        // Whether this row is a *question* right now, read from the server rather than re-derived
        // here. An authorisation covers the shift only up to the ceiling somebody named (a blank
        // acceptance covers what had been worked at that moment, and no more), a refusal stands for
        // the whole shift however long it runs, and the rule that decides between them - with the
        // tolerance it carries - is ``overtime.authorisation_covers``. A client comparing
        // ``paid_hours`` against ``authorised_hours`` itself would be a second rule, and the two
        // would disagree the moment either moved. No field at all reads as settled, so a card with
        // an answer on it never looks like it is still asking.
        const needsAnswer = !decision || item.needs_answer === true;
        // Two numbers, because the decision is about the *second* one: what the shift has worked
        // so far, and what is being held past the line. An operator who sees only "9 h" cannot
        // tell whether anything is at stake.
        const stats = [
            [I18n.__('crossingsPaid'), Number(item.paid_hours || 0).toFixed(2)],
            [I18n.__('crossingsHeld'), Number(item.overtime_hours || 0).toFixed(2)],
            [I18n.__('approvalsClockIn'), String(item.clock_in_time || '')],
            [I18n.__('approvalsSite'), String(item.site_name || '')],
        ];
        // Who answered: the name the server resolved for the id, falling back to the id itself
        // so a decision whose account has gone still says something an operator can read.
        const who = String(decision && (decision.decided_by_name || decision.decided_by) || '');
        const answer = decision
            ? `<p class="ui-section-note" data-crossing-answer="${id}">${this.escapeHtml(
                  (decision.decision === 'declined'
                      ? I18n.__('crossingsDeclinedBy')
                      : I18n.__('crossingsAuthorised')
                            .replace('{hours}', Number(decision.authorised_hours || 0).toFixed(2))
                  ).replace('{who}', who)
              )}</p>
              ${
                  decision.note
                      ? `<p class="ui-section-note" data-crossing-answer-note="${id}">${this.escapeHtml(
                            String(decision.note)
                        )}</p>`
                      : ''
              }`
            : '';
        // A ceiling the shift has worked past: the answer stays on the card *and* the form comes
        // back, with the figure the operator is being asked about. Without this the second question
        // the server is waiting on has no way to be answered from the console - the card would show
        // an authorisation, say nothing about the hours beyond it, and never offer the question.
        // The evidence travels with the answer, in the same block the review cards use, so the two
        // halves of this tab read the same way: a decision, and what it rested on.
        const evidence = decision ? this.crossingDecisionEvidenceHtml(item, decision, who) : '';
        const outgrown =
            decision && needsAnswer
                ? `<p class="ui-section-note" data-crossing-outgrown="${id}">${this.escapeHtml(
                      I18n.__('crossingsPastCeiling').replace(
                          '{hours}', Number(item.unauthorised_hours || 0).toFixed(2)
                      )
                  )}</p>`
                : '';
        // The decision, or nothing where this reader is not the one who may make it: an
        // administrator's own crossing is a head admin's answer (``mayActOnAccount``), and the
        // live queue asks the same question the Approvals list does.
        const controls = needsAnswer && this.mayActOnAccount(item)
            ? `
                <label class="ui-label" for="crossingHours-${id}" style="margin-top:16px">${this.escapeHtml(I18n.__('crossingsCeiling'))}</label>
                <input id="crossingHours-${id}" class="ui-field" type="number" min="0" step="0.25"
                       data-crossing-hours="${id}" />
                <label class="ui-label" for="crossingNote-${id}" style="margin-top:12px">${this.escapeHtml(I18n.__('approvalsNote'))}</label>
                <textarea id="crossingNote-${id}" class="ui-field" rows="2" data-crossing-note="${id}"
                          placeholder="${this.escapeHtml(I18n.__('approvalsNotePlaceholder'))}"></textarea>
                <div class="ui-row" style="margin-top:12px">
                    <button type="button" class="ui-btn ui-btn-primary" data-crossing-accept="${id}">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('crossingsAccept'))}</button>
                    <button type="button" class="ui-btn" data-crossing-decline="${id}">${this.escapeHtml(I18n.__('crossingsDecline'))}</button>
                </div>`
            : '';
        return `
            <article class="ui-card" data-crossing="${id}">
                <div class="ui-row ui-row-between">
                    <strong>${this.escapeHtml(String(item.worker_name || id))}</strong>
                    <span class="ui-badge is-warn">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('crossingsRunning'))}</span>
                </div>
                <div class="ops-stat-grid" style="margin-top:12px">
                    ${stats
                        .map(
                            ([label, value]) => `
                    <div class="ops-stat">
                        <span class="ops-stat-label">${this.escapeHtml(label)}</span>
                        <span class="ops-stat-value">${this.escapeHtml(value)}</span>
                    </div>`
                        )
                        .join('')}
                </div>
                ${item.close_defers ? `<p class="ui-section-note" data-crossing-holds-open="${id}">${this.escapeHtml(I18n.__('crossingsCloseDefers'))}</p>` : ''}
                ${answer}${evidence}${outgrown}${controls}
            </article>`;
    },

    /**
     * The evidence behind an answer: the ceiling, the hours worked when it was given, who gave it.
     *
     * The other half of this tab's items already carry a block like this for a review, and for the
     * same reason. "Authorised up to 12 h" on its own cannot tell a generous ceiling given at
     * 8.5 h from a rubber-stamp given at 11.9 h, and that difference is what the next person to
     * ask - an operator tempted to extend it, the worker asking why their week moved, an auditor
     * months later - needs from the card rather than from a query.
     *
     * A refusal shows the same block without the ceiling: it authorised nobody, and printing the
     * paid day it kept under a label reading "authorised" would read as an authorisation. The
     * hours worked when it was made are its evidence, and they are the whole of it.
     */
    crossingDecisionEvidenceHtml(item, decision, who) {
        const id = this.escapeHtml(String(item.worker_id));
        const recorded = Number(decision.recorded_hours_at_decision);
        const facts = [
            decision.decision === 'declined'
                ? null
                : {
                      label: I18n.__('crossingsDecisionCeiling'),
                      value: Number(decision.authorised_hours || 0).toFixed(2)
                  },
            isFinite(recorded)
                ? { label: I18n.__('crossingsDecisionRecorded'), value: recorded.toFixed(2) }
                : null,
            { label: I18n.__('crossingsDecisionBy'), value: String(who || '') }
        ].filter(Boolean);
        return `
            <div class="ui-evidence" data-crossing-evidence="${id}">
                <p class="ops-stat-label">${this.escapeHtml(I18n.__('approvalsEvidence'))}</p>
                <div class="ui-facts">${facts
                    .map(
                        (fact) => `
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(fact.label)}</span>
                        <span class="ui-fact-value">${this.escapeHtml(fact.value)}</span>
                    </div>`
                    )
                    .join('')}</div>
            </div>`;
    },

    /**
     * One answer to a live crossing, and the endpoint that owns it.
     *
     * The ceiling is optional and empty means "the hours worked so far": the same default the
     * server applies, so the two cannot disagree about what a blank field authorises. Hours past
     * the ceiling are not paid quietly - they come back as a second question - so a number here
     * is a deliberate act rather than a formality.
     */
    async handleCrossing(workerId, accept) {
        const id = String(workerId);
        const hoursField = document.getElementById(`crossingHours-${id}`);
        const noteField = document.getElementById(`crossingNote-${id}`);
        const raw = hoursField ? String(hoursField.value || '').trim() : '';
        const note = noteField ? String(noteField.value || '').trim() : '';
        if (!accept && !note) {
            // Same rule as refusing a review: a refusal is what the record keeps to answer
            // "why was my overtime refused".
            Toast.error(I18n.__('approvalsRejectNeedsNote'));
            return;
        }
        const body = { note: note || null };
        if (raw) {
            const hours = Number(raw);
            if (!isFinite(hours) || hours < 0) {
                // Caught here so a typo is a toast rather than a round trip that answers 400.
                Toast.error(I18n.__('crossingsCeilingInvalid'));
                return;
            }
            body.authorised_hours = hours;
        }
        const card = typeof document.querySelector === 'function' ? document.querySelector(`[data-crossing="${id}"]`) : null;
        const buttons = card && card.querySelectorAll ? Array.from(card.querySelectorAll('button')) : [];
        // The answer is a round trip on a phone tether: both buttons go down while it is in
        // flight, so one card cannot take a second answer to the same question.
        buttons.forEach((button) => { button.disabled = true; });
        try {
            await API.request(
                `/admin/overtime/crossings/${encodeURIComponent(id)}/${accept ? 'accept' : 'decline'}`,
                { method: 'POST', body }
            );
        } catch (err) {
            buttons.forEach((button) => { button.disabled = false; });
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__(accept ? 'crossingsAccepted' : 'crossingsDeclined'));
        // The answer changed the count the badge carries, so the badge is re-asked (forced,
        // past the throttle) before the tab repaints: a repaint that re-fetched and re-painted
        // would agree with it a moment later, but one repaint per answer is what the operator
        // is watching, and the badge should not lag the card it was counted from.
        if (typeof UI !== 'undefined' && UI.refreshApprovalsBadge) UI.refreshApprovalsBadge(true);
        UI.renderAdminTab('Approvals');
    },

    /**
     * The two answers to a review - and the one endpoint each.
     *
     * ``action`` was taken and thrown away here: both buttons posted to
     * ``/admin/approve_review``, so pressing *Reject* approved the shift at its full recorded
     * hours. A worker was paid the overtime their manager had just refused, and the only
     * trace of the refusal was that the card disappeared. The decision picks the endpoint
     * now, and a refusal has to carry the reason it is recorded with - the server refuses
     * one without it, so the check here is what saves a round trip, not what enforces it.
     */
    async handleApproval(logId, action) {
        const reject = action === 'reject';
        // Read defensively: a second click - or a second admin on the same review - lands
        // after the list has re-rendered, when this row's note box is already detached.
        const field = document.getElementById(`note-${logId}`);
        const note = field ? String(field.value || '').trim() : '';
        if (reject && !note) {
            // A reason, not a nicety: it is what the record keeps to answer "why was my
            // overtime refused", and the server answers 422 without one.
            Toast.error(I18n.__('approvalsRejectNeedsNote'));
            return;
        }
        const card = typeof document.querySelector === 'function' ? document.querySelector(`[data-review="${logId}"]`) : null;
        const buttons = card && card.querySelectorAll ? Array.from(card.querySelectorAll('button')) : [];
        // The decision is a round trip on a phone tether. Both buttons go down while it is
        // in flight, so the card cannot take a second answer to the same question.
        buttons.forEach((button) => { button.disabled = true; });
        try {
            if (reject) {
                // No ``admin_id``: identity is the bearer token, and a rejection is a new call
                // site that has no reason to repeat a field the server ignores.
                await API.request('/admin/reject_review', { method: 'POST', body: { log_id: logId, note } });
            } else {
                await API.request('/admin/approve_review', { method: 'POST', body: { log_id: logId, admin_id: State.user.id, note } });
            }
        } catch (err) {
            buttons.forEach((button) => { button.disabled = false; });
            Toast.error(err.message);
            return;
        }
        // Was an ``alert()``: a modal that stops the browser, cannot be read by the toast
        // queue, and announces nothing to a screen reader that was not already looking.
        Toast.success(I18n.__(reject ? 'approvalsRejected' : 'approvalsDecided'));
        UI.renderAdminTab('Approvals');
    },

    // -----------------------------------------------------------------
    //  Alerts - what the system is telling you, and the one decision it waits on
    // -----------------------------------------------------------------
    /**
     * The console's read of ``admin_notifications``: the table the server writes when it has
     * something to say - a note nobody has answered, a shift past its paid day, a start that
     * bypassed a failing self-test, a push channel that has stopped delivering.
     *
     * The tab is the root tier's, and so is the route it reads
     * (``/developer/notifications``): every row here is either the deployment's own business
     * or a decision about a site, and the tier that owns the deployment is the one that
     * answers both. It used to answer every administrator, with the deployment's events
     * withheld from them by a filter - a queue of decisions about the host, offered to
     * somebody who cannot take them.
     *
     * The tab exists for the *acknowledgement*, not for the list. Every one of these rows was
     * already stored before this screen, and reachable from an API - which for the people who
     * run the deployment is the same as not being reachable at all. What is new is that a
     * decision can be given an answer: ``POST /developer/notifications/{id}/acknowledge`` takes
     * a reason, records it against the operator's own identity in the append-only
     * ``audit_log``, and readiness stops reporting the forced start as unaccepted. Readiness is
     * what a monitor watches; this is where a person accepts what it is reporting.
     *
     * Three rules the rendering follows:
     *
     * 1. **Unacknowledged first, then severity, then newest.** The queue leads with what is
     *    waiting on somebody, so the row that needs an answer is never below the fold.
     * 2. **Text from the server is escaped like any other text.** An alert body is a sentence
     *    this backend wrote, and it can carry a worker's name or a site name; ``escapeHtml`` is
     *    what keeps a site called ``<b>`` from turning bold in the middle of an alert about a
     *    forced start.
     * 3. **A reason is required before the button does anything.** The server refuses an empty
     *    note (400, through ``textguard``), so the check here saves a round trip on a phone
     *    tether - it is not what enforces it.
     */
    ALERT_SEVERITY_ORDER: { critical: 0, warning: 1, info: 2 },

    alertSeverityRank(alert) {
        const rank = this.ALERT_SEVERITY_ORDER[String((alert && alert.severity) || '')];
        return rank === undefined ? 3 : rank;
    },

    /** ``critical`` as words, in the reader's language, with the code as the fallback.
     *
     * No arrow glyph here: ``test_frontend_print_sheet`` reads these two files for one to stop a
     * second date range being drawn, and a comment is text it cannot tell from a template.
     */
    alertSeverityLabel(alert) {
        return codeLabel('notifSeverity', String((alert && alert.severity) || 'info'));
    },

    /** Most urgent first: unanswered, then the tone, then the newest. */
    alertOrder(alerts) {
        return alerts.slice().sort((a, b) => {
            const waiting = (a.acknowledged_at ? 1 : 0) - (b.acknowledged_at ? 1 : 0);
            if (waiting !== 0) return waiting;
            const severity = this.alertSeverityRank(a) - this.alertSeverityRank(b);
            if (severity !== 0) return severity;
            return Number(b.id || 0) - Number(a.id || 0);
        });
    },

    alertCardHtml(alert) {
        const id = this.liveOpsInlineString(alert.id);
        const acknowledged = !!alert.acknowledged_at;
        const severity = String(alert.severity || 'info');
        const tone = severity === 'critical' ? ' is-danger' : (severity === 'warning' ? ' is-warn' : '');
        const chip = acknowledged
            ? `<span class="ui-badge" data-alert-acknowledged-chip="true">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('adminAlertsAcknowledged'))}</span>`
            : `<span class="ui-badge${tone}" data-alert-waiting-chip="true">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('adminAlertsWaiting'))}</span>`;
        return `
            <article class="ui-card${tone}" data-alert="${this.escapeHtml(String(alert.id))}"
                     data-alert-kind="${this.escapeHtml(String(alert.kind || ''))}"
                     data-alert-severity="${this.escapeHtml(severity)}"
                     data-unacknowledged="${acknowledged ? '0' : '1'}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        <span class="ui-badge${tone}" data-alert-severity-badge="true">${this.escapeHtml(this.alertSeverityLabel(alert))}</span>
                        <div class="ops-who">
                            <span class="ops-name">${this.escapeHtml(alert.title || '')}</span>
                            <span class="ops-sub">${this.escapeHtml(I18n.__('adminAlertsRaised').replace('{when}', String(alert.created_at || '')))}</span>
                        </div>
                    </div>
                    ${chip}
                </div>
                <p class="ui-note is-body" data-alert-body="true">${this.escapeHtml(alert.body || '')}</p>
                ${acknowledged ? this.alertAcknowledgementHtml(alert) : this.alertAcknowledgeFormHtml(alert, id)}
            </article>`;
    },

    /** What was decided before: who accepted it, when, and in their own words why. */
    alertAcknowledgementHtml(alert) {
        const acknowledged = I18n.__('adminAlertsAcknowledgedBy')
            .replace('{who}', String(alert.acknowledged_by || ''))
            .replace('{when}', String(alert.acknowledged_at || ''));
        return `
                <p class="ui-section-note" data-alert-acknowledged-by="true">${this.escapeHtml(acknowledged)}</p>
                <p class="ui-note is-body" data-alert-acknowledgement-note="true">${this.escapeHtml(I18n.__('adminAlertsReason'))}: ${this.escapeHtml(alert.acknowledgement_note || '')}</p>`;
    },

    /**
     * The answer, for an alert nobody has given one to yet.
     *
     * Both controls are bound by ``bindAlertControls`` rather than carrying an ``onclick``: an
     * inline handler is the reason the document policy still allows ``script-src-attr
     * 'unsafe-inline'``, and the alert queue - which renders text this server wrote, including
     * from a worker's note - is the last screen that should widen it.
     */
    alertAcknowledgeFormHtml(alert, id) {
        const read = alert.read_at
            ? ''
            : `
                    <button type="button" class="ui-btn" data-alert-mark-read="${id}">${this.escapeHtml(I18n.__('adminAlertsMarkRead'))}</button>`;
        return `
                <label class="ui-label" for="alertNote${id}" style="margin-top:16px">${this.escapeHtml(I18n.__('adminAlertsAcknowledgeNote'))}</label>
                <textarea id="alertNote${id}" class="ui-field" rows="2" data-alert-note="${id}"
                          placeholder="${this.escapeHtml(I18n.__('adminAlertsAcknowledgePlaceholder'))}"></textarea>
                <p class="ui-section-note" style="margin-top:6px">${this.escapeHtml(I18n.__('adminAlertsAcknowledgeKept'))}</p>
                <div class="ui-row" style="margin-top:12px">
                    <button type="button" class="ui-btn ui-btn-primary" data-alert-acknowledge="${id}">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('adminAlertsAcknowledge'))}</button>${read}
                </div>`;
    },

    alertsHtml(data) {
        const alerts = Array.isArray(data && data.notifications) ? data.notifications : [];
        if (alerts.length === 0) {
            return `
                <div class="ui-empty" data-alerts-empty="true">
                    <span class="ui-empty-icon">${this.OPS_ICONS.check}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('adminAlertsEmpty'))}</p>
                    <p class="ui-empty-body">${this.escapeHtml(I18n.__('adminAlertsEmptyHint'))}</p>
                    <button type="button" class="ui-btn" data-alerts-live-ops="true">${this.OPS_ICONS.clock}${this.escapeHtml(I18n.__('activeShifts'))}</button>
                </div>`;
        }
        const waiting = alerts.filter((alert) => !alert.acknowledged_at).length;
        const count = I18n.__('adminAlertsCount')
            .replace('{waiting}', String(waiting))
            .replace('{count}', String(alerts.length));
        return `
            <p class="ui-section-note" data-alerts-count="${waiting}">${this.escapeHtml(count)}</p>
            <p class="ui-section-note">${this.escapeHtml(I18n.__('adminAlertsHint'))}</p>
            <div class="ui-stack">${this.alertOrder(alerts).map((alert) => this.alertCardHtml(alert)).join('')}</div>`;
    },

    async renderAlerts(content) {
        if (!content) return;
        content.innerHTML = UI.consoleSkeletonHtml(I18n.__('adminAlertsTitle'));
        let data;
        try {
            data = await API.request('/developer/notifications?limit=100');
        } catch (err) {
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Alerts')");
            return;
        }
        content.innerHTML = `<div class="ui-page" data-alerts="true">${this.alertsHtml(data)}</div>`;
        this.bindAlertControls(content);
    },

    /**
     * Bind the queue's controls after it is painted.
     *
     * One pass over the card's own buttons, with the id read off the attribute the card was
     * drawn with - so a repaint between paint and tap cannot orphan a handler, and no inline
     * ``onclick`` joins the ones the document policy already tolerates. The guards are the same
     * ones ``bindCredentialsControls`` and the review cards carry: a stub DOM without
     * ``querySelectorAll`` must be able to render the tab without throwing.
     */
    bindAlertControls(content) {
        if (!content || typeof content.querySelectorAll !== 'function') return;
        const on = (selector, handler) => {
            content.querySelectorAll(selector).forEach((button) => {
                if (typeof button.addEventListener !== 'function') return;
                button.addEventListener('click', () => handler(button.getAttribute(selector.slice(1, -1))));
            });
        };
        on('[data-alert-acknowledge]', (id) => this.acknowledgeAlert(id));
        on('[data-alert-mark-read]', (id) => this.markAlertRead(id));
        content.querySelectorAll('[data-alerts-live-ops]').forEach((button) => {
            if (typeof button.addEventListener !== 'function') return;
            button.addEventListener('click', () => UI.renderAdminTab('Live Ops'));
        });
    },

    /**
     * Accept an alert, with the reason. The note is the point of the screen.
     *
     * Answers ``409`` by showing the server's sentence - it names the administrator who
     * accepted it first and when, which is what a second person needs to know and is not
     * something this screen can know by itself.
     */
    async acknowledgeAlert(notificationId) {
        const field = document.getElementById(`alertNote${notificationId}`);
        const note = field ? String(field.value || '').trim() : '';
        if (!note) {
            Toast.error(I18n.__('adminAlertsAcknowledgeNeedsNote'));
            return;
        }
        const card = typeof document.querySelector === 'function'
            ? document.querySelector(`[data-alert="${notificationId}"]`)
            : null;
        const buttons = card && card.querySelectorAll ? Array.from(card.querySelectorAll('button')) : [];
        // The decision is a round trip on a phone tether: both buttons go down while it is in
        // flight, so one alert cannot take two answers.
        buttons.forEach((button) => { button.disabled = true; });
        try {
            await API.request(`/developer/notifications/${encodeURIComponent(notificationId)}/acknowledge`, {
                method: 'POST',
                body: { note }
            });
        } catch (err) {
            buttons.forEach((button) => { button.disabled = false; });
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('adminAlertsAcknowledgedToast'));
        UI.renderAdminTab('Alerts');
    },

    /**
     * Seen, but not accepted. Kept as a separate act on purpose: an informational alert (a
     * retention sweep, a note a worker wrote) needs clearing, and nothing about clearing it
     * should look like accepting a forced start.
     */
    async markAlertRead(notificationId) {
        try {
            await API.request(`/developer/notifications/${encodeURIComponent(notificationId)}/read`, { method: 'POST' });
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        Toast.success(I18n.__('adminAlertsMarkedRead'));
        UI.renderAdminTab('Alerts');
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

    /**
     * The category picker: the categories, plus the choice of none.
     *
     * "No category" is first and selected by default, because it is what every site was
     * before this feature existed - and a picker that silently chose the first category on the
     * list would put a site into a warehouse the moment somebody moved its pin.
     */
    sitesCategoryOptionsHtml(selected) {
        const chosen = selected === null || selected === undefined ? '' : String(selected);
        const options = (this._siteCategories || []).map((category) =>
            `<option value="${this.escapeHtml(String(category.category_id))}"${chosen === String(category.category_id) ? ' selected' : ''}>${this.escapeHtml(category.name)}</option>`
        ).join('');
        return `<option value=""${chosen === '' ? ' selected' : ''}>${this.escapeHtml(I18n.__('sitesCategoryNone'))}</option>${options}`;
    },

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
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('sitesCategory'))}</span>
                        <span class="ui-fact-value" data-site-category="${this.escapeHtml(name)}">${this.escapeHtml(site.category || I18n.__('sitesCategoryNone'))}</span>
                    </div>
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
        if (keys.some((key) => source[key] === 'site')) return I18n.__('sitesWindowFromSite');
        // The category comes before the company in this sentence for the same reason it comes
        // before it at the gate: a window that a warehouse's hours decided is not the company's,
        // and telling an administrator it is would send them to the wrong screen to change it.
        if (keys.some((key) => source[key] === 'category')) {
            return I18n.__('sitesWindowFromCategory').replace('{category}', String(site.category || ''));
        }
        return I18n.__('sitesWindowFromCompany');
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
                <div style="grid-column:1/-1">
                    <label class="ui-label" for="editSiteCategory">${this.escapeHtml(I18n.__('sitesCategory'))}</label>
                    <select id="editSiteCategory" class="ui-field">${this.sitesCategoryOptionsHtml(site.category_id)}</select>
                </div>
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
                    <!-- Three cases, not two: a site with no hours of its own follows its
                         category when it has one, and telling that administrator the hours are
                         "the company's" would point them at the wrong screen to change them. -->
                    <p class="ui-section-note" id="editSiteWindowNotice">${this.escapeHtml(
                        inherits
                            ? (site.category
                                ? I18n.__('sitesWindowNoticeCategory')
                                    .replace('{category}', String(site.category))
                                    .replace('{window}', this.windowLabel(site))
                                : I18n.__('sitesWindowNoticeCompany').replace('{window}', this.windowLabel(site)))
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

    /** The company zone, offered as a suggestion - the field is not a closed list. */
    siteTimezoneOptionsHtml() {
        return `<datalist id="siteTimezoneOptions">
            ${['Asia/Kuwait']
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
                        <label class="ui-label" for="siteCategory">${this.escapeHtml(I18n.__('sitesCategory'))}</label>
                        <select id="siteCategory" class="ui-field">${this.sitesCategoryOptionsHtml(null)}</select>
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

    /**
     * The categories, and the honesty about their reach.
     *
     * Every row says how many sites follow it, because that is the figure that decides whether
     * an edit here is a small correction or a retune of a whole class of sites - and it is the
     * same number the server refuses a delete on.
     */
    sitesCategoriesHtml() {
        const categories = this._siteCategories || [];
        const editing = this._categoryEdit === null || this._categoryEdit === undefined
            ? null : Number(this._categoryEdit);
        const rows = categories.map((category) => {
            const id = Number(category.category_id);
            if (editing === id) {
                return `<li class="ui-card" data-category-row="${id}">${this.siteCategoryFormHtml(category)}</li>`;
            }
            return `
                <li class="ui-card" data-category-row="${id}">
                    <div class="ui-spread">
                        <div class="ops-who">
                            <span class="ops-name">${this.escapeHtml(category.name)}</span>
                            <span class="ops-sub">${this.escapeHtml(this.siteCategoryWindowLabel(category))} · ${this.escapeHtml(I18n.__('sitesCategorySiteCount').replace('{count}', String(category.site_count || 0)))}</span>
                        </div>
                        <div class="ui-row">
                            <button type="button" class="ui-btn ui-btn-sm" data-category-edit="${id}">${this.escapeHtml(I18n.__('sitesEdit'))}</button>
                            <button type="button" class="ui-btn ui-btn-danger ui-btn-sm" data-category-delete="${id}">${this.escapeHtml(I18n.__('sitesDelete'))}</button>
                        </div>
                    </div>
                </li>`;
        }).join('');
        return `
            <section class="ui-section" id="siteCategoriesPanel">
                <div class="ui-section-head">
                    <h3 class="ui-section-title">${this.escapeHtml(I18n.__('sitesCategories'))}</h3>
                    <p class="ui-section-note">${this.escapeHtml(I18n.__('sitesCategoriesHint'))}</p>
                </div>
                ${categories.length === 0 ? '' : `<ul class="ui-stack" style="list-style:none;margin:0 0 14px;padding:0">${rows}</ul>`}
                ${this.siteCategoryFormHtml(null)}
            </section>`;
    },

    /** ``04:00-06:30``, or the word for "follows the company hours" when nothing is set. */
    siteCategoryWindowLabel(category) {
        const start = String((category && category.clock_in_window_start) || '');
        const end = String((category && category.clock_in_window_end) || '');
        if (!start && !end) return I18n.__('sitesCategoryNoHours');
        return `${start || '00:00'}-${end || '23:59'}`;
    },

    /**
     * One form for adding a category and for editing one in place.
     *
     * The same fields either way, because they are the same question - what is this class of
     * sites called, and when does its day start - and a separate edit dialog is a second place
     * for the two to disagree about which fields a blank box clears.
     */
    siteCategoryFormHtml(category) {
        const raw = (key) => {
            const value = category ? category[key] : null;
            return value === null || value === undefined ? '' : String(value);
        };
        const id = category ? Number(category.category_id) : '';
        return `
            <form id="siteCategoryForm" class="ui-grid three" style="margin-top:10px">
                <div>
                    <label class="ui-label" for="siteCategoryName">${this.escapeHtml(I18n.__('sitesCategoryName'))}</label>
                    <input type="text" id="siteCategoryName" class="ui-field" required value="${this.escapeHtml(raw('name'))}"
                           placeholder="${this.escapeHtml(I18n.__('sitesCategoryNamePlaceholder'))}">
                </div>
                <div>
                    <label class="ui-label" for="siteCategoryStart">${this.escapeHtml(I18n.__('sitesWindowStart'))}</label>
                    <input type="time" id="siteCategoryStart" class="ui-field" value="${this.escapeHtml(raw('clock_in_window_start'))}">
                </div>
                <div>
                    <label class="ui-label" for="siteCategoryEnd">${this.escapeHtml(I18n.__('sitesWindowEnd'))}</label>
                    <input type="time" id="siteCategoryEnd" class="ui-field" value="${this.escapeHtml(raw('clock_in_window_end'))}">
                </div>
                <div>
                    <label class="ui-label" for="siteCategoryTimezone">${this.escapeHtml(I18n.__('sitesWindowTimezone'))}</label>
                    <input type="text" id="siteCategoryTimezone" class="ui-field" list="siteTimezoneOptions"
                           placeholder="${this.escapeHtml(I18n.__('sitesWindowTimezonePlaceholder'))}" value="${this.escapeHtml(raw('site_timezone'))}">
                </div>
                <div style="grid-column:1/-1" class="ui-row">
                    <button type="submit" class="ui-btn ui-btn-primary">${this.OPS_ICONS.check}${this.escapeHtml(category ? I18n.__('save') : I18n.__('sitesCategoryAdd'))}</button>
                    <button type="button" class="ui-btn ui-btn-quiet" data-category-cancel="1"${category ? '' : ' hidden'}>${this.escapeHtml(I18n.__('cancel'))}</button>
                    <p class="ui-section-note" style="margin:0">${this.escapeHtml(I18n.__('sitesCategoryFormHint'))}</p>
                </div>
            </form>`;
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
            ${this.sitesCategoriesHtml()}
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
        let categories;
        try {
            // Read together, so the category pickers can be painted with the sites: a form that
            // opened with an empty picker and filled itself a moment later is a form somebody
            // has already chosen from.
            [sites, categories] = await Promise.all([
                API.request('/admin/sites'),
                API.request('/admin/site_categories')
            ]);
        } catch (err) {
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Sites')");
            return;
        }
        this._sites = Array.isArray(sites) ? sites : [];
        this._siteCategories = Array.isArray(categories) ? categories : [];
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
        // The categories panel, bound the same way: the handler is attached to the container and
        // the buttons carry ids, never a category name - a name is operator data, and handler
        // text built from data is the sink the document CSP exists to close.
        const panel = document.getElementById('siteCategoriesPanel');
        if (panel) {
            const categoryForm = document.getElementById('siteCategoryForm');
            if (categoryForm) categoryForm.onsubmit = (event) => this.saveSiteCategory(event);
            panel.onclick = (event) => this.onCategoriesClick(event);
        }
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

    /** The categories panel's buttons: edit, cancel, delete. */
    onCategoriesClick(event) {
        const target = event && event.target;
        if (!target || typeof target.closest !== 'function') return undefined;
        const edit = target.closest('[data-category-edit]');
        if (edit) return this.openCategoryEdit(Number(edit.dataset.categoryEdit));
        if (target.closest('[data-category-cancel]')) return this.cancelCategoryEdit();
        const remove = target.closest('[data-category-delete]');
        if (remove) return this.deleteSiteCategory(Number(remove.dataset.categoryDelete));
        return undefined;
    },

    /**
     * Which category the panel's one form is editing, or ``null`` when it is adding one.
     *
     * Held here rather than read back out of the rendered form, exactly as ``_siteEdit`` is:
     * the form is a string until it is painted, so "which row is this?" is a fact about the
     * console's own state, and reading it off an attribute is a second copy of that fact.
     */
    openCategoryEdit(categoryId) {
        this._categoryEdit = Number(categoryId);
        this.paintSites();
        return undefined;
    },

    cancelCategoryEdit() {
        this._categoryEdit = null;
        this.paintSites();
        return undefined;
    },

    /**
     * Add a category, or save the one being edited.
     *
     * A blank time box is sent as ``null``, which means "follow the company hours" - so a
     * category whose hours nobody set cannot freeze whatever the company window happened to be
     * the day somebody typed its name.
     */
    async saveSiteCategory(event) {
        if (event && event.preventDefault) event.preventDefault();
        const value = (id) => {
            const element = document.getElementById(id);
            return element && element.value !== undefined ? String(element.value).trim() : '';
        };
        const id = this._categoryEdit === null || this._categoryEdit === undefined
            ? null : Number(this._categoryEdit);
        const body = {
            name: value('siteCategoryName'),
            clock_in_window_start: value('siteCategoryStart') || null,
            clock_in_window_end: value('siteCategoryEnd') || null,
            site_timezone: value('siteCategoryTimezone') || null
        };
        try {
            if (id) {
                await API.request('/admin/site_categories/edit', {
                    method: 'POST', body: { category_id: id, ...body }
                });
            } else {
                await API.request('/admin/site_categories/add', { method: 'POST', body });
            }
        } catch (err) {
            Toast.error(err.message);
            return null;
        }
        Toast.success(I18n.__(id ? 'sitesCategorySaved' : 'sitesCategoryAdded'));
        this._categoryEdit = null;
        UI.renderAdminTab('Sites');
        return true;
    },

    /**
     * Delete a category, once nothing is inside it.
     *
     * The confirmation leads with the count, because that is the fact that makes this dialog
     * different from deleting a site: it says how many sites are deliberately left where they
     * are. The server refuses while any remain, and its sentence (which names the count) is
     * shown verbatim - so the refusal and the confirmation say the same thing.
     */
    async deleteSiteCategory(categoryId) {
        const id = Number(categoryId);
        const category = (this._siteCategories || []).find(
            (row) => Number(row.category_id) === id
        );
        if (!category) return undefined;
        const question = I18n.__('sitesCategoryDeleteConfirm')
            .replace('{name}', category.name)
            .replace('{sites}', String(category.site_count || 0));
        if (!confirm(question)) return undefined;
        const fd = new FormData();
        fd.append('category_id', String(id));
        try {
            await API.request('/admin/site_categories/delete', { method: 'POST', body: fd });
        } catch (err) {
            Toast.error(err.message);
            return undefined;
        }
        Toast.success(I18n.__('sitesCategoryDeleted'));
        UI.renderAdminTab('Sites');
        return true;
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
                site_timezone: value('siteWindowTimezone') || null,
                // Sent as a number or as null; an empty option is "no category", which is what
                // the column's NULL means and what an uncategorised site has always been.
                category_id: value('siteCategory') ? Number(value('siteCategory')) : null
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
                site_timezone: value('editSiteTimezone') || null,
                // Part of what this form saves: the picker is always on screen here, so what it
                // holds is the answer - including "no category" after a move out of one.
                category_id: value('editSiteCategory') ? Number(value('editSiteCategory')) : null
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
    //  Registrations - who is asking to join, and the two answers
    //
    //  The intake has been complete on the server for as long as it has existed: a
    //  public form writes a pending row with a photograph, an approval allocates the id
    //  and creates the account, a refusal keeps the row and destroys the face. What was
    //  missing was a *person*: the queue could only be filled by hand and emptied by
    //  curl, so nobody could see who was waiting, and nothing could decide them.
    //
    //  Three rules this screen is built on, and they are the server's own:
    //
    //  * **The photograph is fetched, never embedded.** A list of forty applications
    //    would otherwise be forty faces in one response, and an ``<img src>`` cannot
    //    carry a token anyway - a URL that worked in a ``src`` would be a URL that worked
    //    for anybody. The bytes come from the per-request route with this session's
    //    credential, one application at a time, when the reviewer asks to see it.
    //  * **The order is the queue's, not this screen's.** Oldest first, straight from the
    //    server: a queue nobody reads in order is a queue that starves whoever applied
    //    first, and the first applicant is the one being phoned about.
    //  * **The account a decision was about is the thing to remember.** The id is its own - the
    //    worker was given it when they registered, and has been signing in with it while they
    //    waited - so the receipt says which account was just approved and what to hand over,
    //    rather than leaving it in a toast that scrolls away.
    // -----------------------------------------------------------------

    /** The last read of the queue, and the last decision this console made. */
    _registrations: null,
    _registrationsLast: null,

    /** The intake switch as the last read answered it, or null before that read. */
    _registrationsIntake: null,

    //: How many applications one read carries. The server's own ceiling is higher, and the
    //: count it reports is counted separately from the page it returns - so a truncated page
    //: says so rather than pretending the queue ended here.
    REGISTRATIONS_LIMIT: 200,

    /** Seconds this application has been waiting, or null when its timestamp is unreadable. */
    registrationsWaitingSeconds(request) {
        const start = this.liveOpsStart(request && request.created_at);
        return start ? Math.max(0, (Date.now() - start.getTime()) / 1000) : null;
    },

    /**
     * The receipt for the last decision, kept above the queue.
     *
     *
     * An approved application *leaves* the queue - the row is not pending any more, which is
     * the whole point - so the id it was given would go with it. This is where that id stays
     * until the next decision: the administrator who approved has to read it to the worker,
     * and the row they read it from is gone.
     *
     * A template that could not be written is stated in the same place, because it changes
     * what they do next: the account exists and cannot clock in until somebody enrolls it,
     * and the console that made it is the console that can. It is not an error to retry -
     * the account is real - so it is a warning on a receipt rather than a red failure.
     *
     * The sign-in line under it is the other half of a decision the applicant cannot look up.
     * The id is minted by the approval, the contact is whatever they typed on a form weeks ago,
     * and the password is one they chose and then never used - so the administrator who made
     * the account is the only person who can hand all three over at once. They are the same
     * three values the worker's own notice names (``notifications.KIND_WORKER_ACCOUNT_APPROVED``),
     * said in the console's voice to the person doing the handing over.
     */
    registrationsNoticeHtml() {
        const last = this._registrationsLast;
        if (!last) return '';
        const heading = `<p class="ops-stat-label">${this.escapeHtml(I18n.__('registrationsLast'))}</p>`;
        if (last.action !== 'approve') {
            // The server destroys the photograph of a refused application, and says so when it
            // could not: a refusal is not the moment to claim a face is gone when it is still
            // on disk for somebody to find.
            const sentence = I18n.__(last.photo_destroyed === false ? 'registrationsPhotoKept' : 'registrationsRejected')
                .replace('{request}', String(last.user_id));
            return `
                <div class="ui-card is-warn" data-registration-refused="${this.escapeHtml(last.user_id)}">
                    ${heading}
                    <p class="ui-note">${this.escapeHtml(sentence)}</p>
                </div>`;
        }
        // What to hand over, now that the row that held it has left the queue: the id is
        // above, and this is the rest of what a first sign-in is typed with. The contact
        // comes back in the approval's own answer (see ``handleRegistration``).
        const contact = [last.phone, last.email].filter(Boolean).join(' \u00b7 ');
        const signIn = contact
            ? I18n.__('registrationsSignIn').replace('{id}', last.user_id).replace('{contact}', contact)
            : I18n.__('registrationsSignInNoContact').replace('{id}', last.user_id);
        const warning = last.template_written
            ? ''
            : `<p class="ui-note is-warn" data-registration-template-warning="true">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('registrationsTemplateWarning').replace('{id}', last.user_id))}</p>`;
        return `
            <div class="ui-card is-ok" data-registration-minted="${this.escapeHtml(last.user_id)}">
                ${heading}
                <p class="ui-fact-value" data-registration-minted-id>${this.escapeHtml(last.user_id)}</p>
                <p class="ui-note">${this.escapeHtml(I18n.__('registrationsApproved').replace('{id}', last.user_id).replace('{name}', last.name))}</p>
                <p class="ui-note" data-registration-signin="${this.escapeHtml(last.user_id)}">${this.escapeHtml(signIn)}</p>
                ${warning}
            </div>`;
    },

    /**
     * The one control on this screen: whether the permanent link is accepting applications.
     *
     *
     * The public form is a URL the company prints once, so opening and closing it is an
     * operator's decision on the day rather than a deploy - and it is drawn on the queue
     * because that is where its consequence shows: a queue that stops growing is otherwise a
     * queue somebody believes is broken.
     *
     * TWO SWITCHES, AND BOTH ARE SAID OUT LOUD. The deployment's own
     * (``REGISTRATION_ENABLED``) is a ceiling over this one and cannot be moved from here, so
     * when *it* is what holds the link shut the button is left off the screen entirely and the
     * sentence names the reason - the rule the rail already follows for a tab the reader
     * cannot act on, because a control that looks pressable and answers nothing teaches an
     * operator that this screen is broken.
     *
     * The reason codes are mapped to keys rather than interpolated into one: the table is the
     * vocabulary, and a key built by concatenation is a string no parity check can see.
     */
    registrationsIntakeHtml() {
        const intake = this._registrationsIntake;
        if (!intake) return '';
        const WHY = {
            open: 'registrationsIntakeWhyOpen',
            closed_by_console: 'registrationsIntakeWhyClosedByConsole',
            closed_by_deployment: 'registrationsIntakeWhyClosedByDeployment'
        };
        const why = WHY[intake.reason] || 'registrationsIntakeWhyClosedByConsole';
        // Not ``!intake.accepting``: a deployment that does not run walk-up registration at
        // all is a different answer from an operator having closed it, and only the second is
        // this console's to change.
        const movable = why !== 'registrationsIntakeWhyClosedByDeployment';
        const accepting = intake.accepting === true;
        const button = movable
            ? `<button type="button" class="ui-btn ${accepting ? 'ui-btn-danger' : 'ui-btn-primary'}" data-registration-intake="${accepting ? 'close' : 'open'}">${this.escapeHtml(I18n.__(accepting ? 'registrationsIntakeClose' : 'registrationsIntakeOpen'))}</button>`
            : '';
        return `
            <div class="ui-card" data-registrations-intake="${this.escapeHtml(intake.reason)}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('registrationsIntakeTitle'))}</span>
                        <span class="ui-fact-value" data-registrations-intake-state="${this.escapeHtml(intake.reason)}">${this.escapeHtml(I18n.__(accepting ? 'registrationsIntakeAccepting' : 'registrationsIntakeRefusing'))}</span>
                    </div>
                    ${button}
                </div>
                <p class="ui-note">${this.escapeHtml(I18n.__(why))}</p>
            </div>`;
    },

    /**
     * One application, in the order a reviewer reads it: who is asking, how long they have
     * waited, what they said about themselves, the face the form captured, and the two
     * answers with the note that is recorded either way.
     *
     * An application past a day is drawn as a danger rather than as a warning. A day is where
     * "we are working through the queue" turns into "this person has been waiting on us for a
     * shift and a night", and the colour is never the only signal - the waiting figure beside
     * it is the fact, and the card is announced by its own text.
     */
    registrationsCardHtml(request) {
        const id = this.escapeHtml(request.id);
        const seconds = this.registrationsWaitingSeconds(request);
        const danger = seconds !== null && seconds >= 86400;
        const waiting = seconds === null ? '\u2014' : this.liveOpsDuration(seconds);
        const facts = [
            { label: I18n.__('registrationsRole'), text: this.roleLabel(request.requested_role) },
            { label: I18n.__('registrationsApplied'), text: String(request.created_at || '\u2014') },
            {
                label: I18n.__('registrationsContact'),
                text: [request.phone, request.email].filter(Boolean).join(' \u00b7 ') || '\u2014'
            }
        ];
        if (request.work_details) {
            facts.push({ label: I18n.__('registrationsDetails'), text: String(request.work_details) });
        }
        return `
            <article class="ui-card${danger ? ' is-danger' : ' is-warn'}" data-registration="${id}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        <div class="ops-who">
                            <span class="ops-name">${this.escapeHtml(request.full_name || '')}</span>
                            <span class="ops-sub">${this.escapeHtml(this.roleLabel(request.requested_role))} \u00b7 #${id}</span>
                        </div>
                    </div>
                    <span class="ui-badge${danger ? ' is-danger' : ' is-warn'}">${this.OPS_ICONS.alert}${this.escapeHtml(`${I18n.__('registrationsWaiting')} ${waiting}`)}</span>
                </div>
                <div class="ui-facts" style="margin-top:14px">
                    ${facts.map((fact) => `
                        <div class="ui-fact">
                            <span class="ops-stat-label">${this.escapeHtml(fact.label)}</span>
                            <span class="ui-fact-value" data-registration-fact>${this.escapeHtml(fact.text)}</span>
                        </div>`).join('')}
                </div>
                <div class="ui-evidence-row" style="margin-top:14px">
                    <div class="ui-evidence-media">
                        <img id="registrationPhoto${id}" class="hidden ui-photo" alt="${this.escapeHtml(I18n.__('registrationsPhotoAlt'))}" />
                    </div>
                    <div class="ui-evidence-facts">
                        <button type="button" class="ui-btn ui-btn-sm" data-registration-photo="${id}">${this.OPS_ICONS.eye}${this.escapeHtml(I18n.__('registrationsShowPhoto'))}</button>
                    </div>
                </div>
                <label class="ui-label" for="registrationNote-${id}" style="margin-top:16px">${this.escapeHtml(I18n.__('registrationsNote'))}</label>
                <textarea id="registrationNote-${id}" class="ui-field" rows="2"
                          placeholder="${this.escapeHtml(I18n.__('registrationsNotePlaceholder'))}"></textarea>
                <p class="ui-section-note" style="margin-top:6px">${this.escapeHtml(I18n.__('registrationsNoteUsed'))}</p>
                <div class="ui-row" style="margin-top:12px">
                    <button type="button" class="ui-btn ui-btn-primary" data-registration-approve="${id}">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('registrationsApprove'))}</button>
                    <button type="button" class="ui-btn ui-btn-danger" data-registration-reject="${id}">${this.OPS_ICONS.close}${this.escapeHtml(I18n.__('registrationsReject'))}</button>
                </div>
            </article>`;
    },

    /**
     * The queue itself: how many are waiting, then the cards oldest first.
     *
     * ``pending`` is the server's own total, counted apart from the page it sent. The two
     * agree until the page is truncated, and when they do not the screen says so instead of
     * ending at row two hundred as if the queue were empty behind it.
     */
    registrationsHtml(requests, pending) {
        if (requests.length === 0) {
            return `
                <div class="ui-empty" data-registrations-empty="true">
                    <span class="ui-empty-icon">${this.OPS_ICONS.check}</span>
                    <p class="ui-empty-title">${this.escapeHtml(I18n.__('registrationsEmpty'))}</p>
                    <p class="ui-empty-body">${this.escapeHtml(I18n.__('registrationsEmptyHint'))}</p>
                </div>`;
        }
        const total = Number(pending) || requests.length;
        const showing = total > requests.length
            ? ` ${I18n.__('liveOpsShowing').replace('{shown}', String(requests.length)).replace('{total}', String(total))}`
            : '';
        const count = I18n.__('registrationsCount').replace('{count}', String(total));
        return `
            <p class="ui-section-note" data-registrations-count="${total}">${this.escapeHtml(count + showing)}</p>
            <div class="ui-stack">${requests.map((request) => this.registrationsCardHtml(request)).join('')}</div>`;
    },

    /**
     * The tab: the intake switch, the last decision's receipt, then the rows a reviewer works
     * through.
     *
     * Two reads, and they answer different questions: the queue read reports whether the link
     * is accepting at all (which is what the closed note below it is drawn from), and the
     * intake read reports *which* of the two switches decided that - the difference between a
     * control an operator can move and a wall.
     *
     * The read is also what the tab's badge is counted from, and the count is written back
     * into ``State`` here - the badge and the screen are then one number rather than two
     * that can disagree by a decision.
     *
     * Every control is bound with a delegated listener, not an inline handler, exactly like
     * the approvals board: a repaint between paint and tap cannot orphan a listener, and the
     * document CSP's allowance for inline attributes is something only ever given up.
     */
    async renderRegistrations(content) {
        if (!content) return;
        content.innerHTML = UI.consoleSkeletonHtml(I18n.__('registrationsTitle'));
        let data;
        try {
            // The status is the server's own constant
            // (``registrations.STATUS_PENDING_APPROVAL``), not the word the console would have
            // chosen: the endpoint validates it against the three it knows and answers 400 for
            // anything else, and a queue screen that could not read its own queue would be a
            // screen of "status must be one of ...".
            data = await API.request(`/admin/registrations?status=pending_approval&limit=${this.REGISTRATIONS_LIMIT}`);
        } catch (err) {
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Registrations')");
            return;
        }
        try {
            this._registrationsIntake = await API.request('/admin/registrations/intake');
        } catch (err) {
            // A switch whose state could not be read is left off the screen rather than drawn
            // in a position that may not be the server's. The queue's own closed note still
            // says whether the link is accepting, so nothing on the screen is wrong - there is
            // just no lever on it.
            this._registrationsIntake = null;
        }
        this._registrations = data;
        const requests = Array.isArray(data && data.requests) ? data.requests : [];
        const pending = Math.max(0, Number(data && data.pending) || 0);
        State.registrationsWaiting = pending;
        if (typeof UI !== 'undefined' && UI.paintRegistrationsBadge) UI.paintRegistrationsBadge();
        // The intake switch, stated on the queue rather than left to the public form to
        // explain: the applicants already in here are still decisions to make, and a queue
        // that silently stops growing is a queue somebody believes is broken.
        const closed = data && data.enabled === false
            ? `<p class="ui-note is-warn" data-registrations-closed="true">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('registrationsClosed'))}</p>`
            : '';
        content.innerHTML = `<div class="ui-page" data-registrations="true">${this.registrationsIntakeHtml()}${this.registrationsNoticeHtml()}${closed}${this.registrationsHtml(requests, pending)}</div>`;
        if (typeof content.querySelectorAll === 'function') {
            const bindEach = (selector, handler) => {
                let nodes = [];
                try {
                    nodes = Array.from(content.querySelectorAll(selector) || []);
                } catch (err) {
                    nodes = [];
                }
                nodes.forEach((node) => {
                    if (node && typeof node.addEventListener === 'function') handler(node);
                });
            };
            bindEach('[data-registration-intake]', (button) => {
                button.addEventListener('click', () => {
                    this.toggleRegistrationsIntake(button.getAttribute('data-registration-intake'));
                });
            });
            bindEach('[data-registration-photo]', (button) => {
                button.addEventListener('click', () => {
                    this.showRegistrationPhoto(button.getAttribute('data-registration-photo'));
                });
            });
            bindEach('[data-registration-approve]', (button) => {
                button.addEventListener('click', () => {
                    this.handleRegistration(button.getAttribute('data-registration-approve'), 'approve');
                });
            });
            bindEach('[data-registration-reject]', (button) => {
                button.addEventListener('click', () => {
                    this.handleRegistration(button.getAttribute('data-registration-reject'), 'reject');
                });
            });
        }
    },

    /**
     * The submitted photograph, fetched with this session's credential.
     *
     * The endpoint is the per-account one rather than a field on the list, which is why this is
     * a button: forty faces in one response was the thing the server was built to avoid, and the
     * bytes are needed exactly once - by the reviewer looking at the application in front of
     * them.
     *
     * A 404 is not a failure and does not read like one: the route only serves an account that is
     * *still waiting* - an approved worker's face is not this screen's business, and a refused
     * one has been wiped - so "there is no photograph any more" is a fact about the record. It is
     * said in its own words.
     */
    async showRegistrationPhoto(requestId) {
        const image = document.getElementById(`registrationPhoto${requestId}`);
        const button = document.querySelector(`[data-registration-photo="${CSS.escape(String(requestId))}"]`);
        const headers = {};
        if (State.token) headers['Authorization'] = `Bearer ${State.token}`;
        if (button) button.disabled = true;
        try {
            const response = await fetch(`${API.baseURL}/admin/registrations/${encodeURIComponent(requestId)}/photo`, { headers });
            if (!response.ok) {
                throw new Error(I18n.__(response.status === 404 ? 'registrationsPhotoGone' : 'registrationsPhotoFailed'));
            }
            const blob = await response.blob();
            if (image) {
                image.src = URL.createObjectURL(blob);
                image.classList.remove('hidden');
            }
            if (button) button.remove();
        } catch (err) {
            Toast.error(err.message || I18n.__('registrationsPhotoFailed'));
            if (button) button.disabled = false;
        }
    },

    /**
     * The two answers to one application, and what each of them does.
     *
     * **Approve** lets the account record attendance, and the answer carries the id and what to
     * hand over - kept on the screen (see ``registrationsNoticeHtml``) rather than only
     * announced. **Refuse** deletes the account and wipes its face, and here the console asks for
     * a reason before it sends: the server accepts a refusal without one, and ``audit_log`` is
     * where the answer to "why was I turned down" survives the deletion.
     *
     * Both buttons go down while the request is in flight: this is a round trip on a phone
     * tether, and a card that took a second answer would be a second account or a second
     * refusal for one decision. A 409 means somebody else decided it first, and the queue is
     * re-read rather than left holding a button that can only fail again.
     */
    async handleRegistration(requestId, action) {
        const approve = action === 'approve';
        const field = document.getElementById(`registrationNote-${requestId}`);
        const note = field ? String(field.value || '').trim() : '';
        if (!approve && !note) {
            Toast.error(I18n.__('registrationsRejectNeedsNote'));
            return;
        }
        const card = typeof document.querySelector === 'function' ? document.querySelector(`[data-registration="${requestId}"]`) : null;
        const buttons = card && card.querySelectorAll ? Array.from(card.querySelectorAll('button')) : [];
        buttons.forEach((button) => { button.disabled = true; });
        let answer = null;
        try {
            answer = await API.request(
                `/admin/registrations/${encodeURIComponent(requestId)}/${approve ? 'approve' : 'reject'}`,
                { method: 'POST', body: { note } }
            );
        } catch (err) {
            buttons.forEach((button) => { button.disabled = false; });
            Toast.error(err.message);
            // Awaited, like the repaint below: a caller - or a suite - that awaited this
            // decision is entitled to read the screen it produced, and the reputation of a
            // stale row has a request in it.
            if (err.status === 409) await UI.renderAdminTab('Registrations');
            return;
        }
        const account = String((answer && answer.user_id) || '');
        const named = String((answer && answer.name) || '');
        // The two values the sign-in screen matches on. They come back with the answer because
        // the queue row that held them is gone the moment it is approved.
        const emailed = String((answer && answer.email) || '');
        const phoned = String((answer && answer.phone) || '');
        const wroteTemplate = !(answer && answer.template_written === false);
        const photoDestroyed = !(answer && answer.photo_destroyed === false);
        this._registrationsLast = approve
            ? {
                  action: 'approve', user_id: account, name: named,
                  template_written: wroteTemplate, email: emailed, phone: phoned
              }
            : { action: 'reject', user_id: account || requestId, photo_destroyed: photoDestroyed };
        if (approve) {
            Toast.success(
                wroteTemplate
                    ? I18n.__('registrationsApproved').replace('{id}', account).replace('{name}', named)
                    : I18n.__('registrationsTemplateWarning').replace('{id}', account)
            );
        } else {
            const refused = I18n.__(photoDestroyed ? 'registrationsRejected' : 'registrationsPhotoKept');
            Toast.success(refused.replace('{request}', String(requestId)));
        }
        // The answer changed the count the badge carries, so the badge is re-asked (forced,
        // past the throttle) before the tab repaints - the same order the crossings board
        // uses, and for the same reason: the badge should not lag the card it was counted
        // from by a whole repaint.
        if (typeof UI !== 'undefined' && UI.refreshRegistrationsBadge) UI.refreshRegistrationsBadge(true);
        // Awaited so that "the decision has been made" and "the screen shows it" are the same
        // moment for whoever awaited this: the id stays in ``_registrationsLast`` either way,
        // but a caller that reads the queue the instant this resolves would otherwise read the
        // skeleton.
        await UI.renderAdminTab('Registrations');
    },

    /**
     * Open or close the permanent link, from the console, with no restart.
     *
     * The two switches are read back from the server rather than assumed from what was asked
     * for: a write the deployment flag refuses stores the operator's intent and still leaves
     * the link shut, and a console that reported success there would be telling an operator
     * the form is open while the form refuses every applicant. So the answer *is* the state,
     * and the repaint draws whatever it says - including no lever, when the deployment is what
     * is holding it.
     */
    async toggleRegistrationsIntake(action) {
        const wanted = action === 'open';
        const button = typeof document.querySelector === 'function'
            ? document.querySelector(`[data-registration-intake="${wanted ? 'open' : 'close'}"]`)
            : null;
        if (button) button.disabled = true;
        try {
            const answer = await API.request('/admin/registrations/intake', {
                method: 'POST',
                body: { open: wanted }
            });
            this._registrationsIntake = answer || null;
            Toast.success(I18n.__(wanted ? 'registrationsIntakeOpened' : 'registrationsIntakeShut'));
        } catch (err) {
            if (button) button.disabled = false;
            Toast.error(err.message);
            return;
        }
        // Awaited, so that "the switch was moved" and "the screen shows it" are the same
        // moment for whoever awaited this - the same contract the two decisions above keep.
        await UI.renderAdminTab('Registrations');
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

    /** The enrollment link just issued - the plaintext token exists only in this object. */
    _credentialsIssued: null,

    /**
     * The id the server last refused for being taken, and the free number it could be.
     *
     * ``{id, free}`` while that refusal is worth showing, ``null`` otherwise. It survives a
     * repaint - changing the role or picking a photo must not lose the answer to "which
     * number, then?" - and a refusal that names its own id stays true while it is up, so it
     * is only cleared by the things that settle the question: the number being taken, an
     * account being created, or the form being opened or closed.
     */
    _credentialsIdTaken: null,

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
        accepted: ['image/jpeg', 'image/png', 'image/webp'],
        // The server's ingestion boundary (``uploads.face_frame_max_pixels`` and
        // ``face_frame_max_edge_px``), the same numbers the link pages read from
        // ``photo_policy``. Mirrored for the same reason ``maxBytes`` is - but this one is not
        // a refusal: above it the file is *resized* rather than rejected, because the photo an
        // admin has on their phone is 12 MP as a matter of course, and refusing it would break
        // the ordinary way a worker gets enrolled. ``test_frontend_photo_downscale`` fails if
        // this mirror stops matching the backend.
        face_frame_max_pixels: 4000000,
        face_frame_max_edge_px: 2048
    },

    /**
     * The sentence ``/admin/users/create`` answers a taken id with, verbatim.
     *
     * The only refusal the create form can answer itself: every other 400 it can produce
     * (a name that is blank, a password under the policy, an id that is not a number) needs
     * the admin to change something the console cannot guess, and this one needs a number
     * nobody is using - which the roster in hand already says. So it is the one refusal
     * worth recognising, and the server sends it as a plain string with no error code, which
     * leaves the text as the only thing to recognise it by. ``test_frontend_account_creation``
     * pins this string against the one ``backend/main.py`` raises, so the two cannot drift
     * into a form that quietly stops offering the fix.
     */
    TAKEN_ID_REFUSAL: 'User ID already exists.',

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
        this.bindCredentialsControls(content);
    },

    /**
     * One listener per repaint for the controls this tab renders by hand.
     *
     * Bound on the nodes that were just rendered, so a repaint cannot leave a second
     * listener behind, and one per node rather than on the container, because the question
     * each handler answers is "which account or which file" and the node already carries it.
     * Same idiom as the handset's export buttons.
     *
     * All of these are ``data-`` hooks rather than inline attributes. The document CSP has to
     * allow ``script-src-attr 'unsafe-inline'`` for the handlers the console builds as
     * strings, that allowance is pinned per file so it may only fall, and what it buys an
     * injected ``<img onerror>`` is exactly this - so a control added today binds its own
     * listener instead of widening it.
     */
    bindCredentialsControls(root) {
        const scope = root || document;
        if (typeof scope.querySelectorAll !== 'function') return;
        const buttons = scope.querySelectorAll('[data-enroll-self]');
        for (let i = 0; i < buttons.length; i += 1) {
            const button = buttons[i];
            if (typeof button.addEventListener !== 'function') continue;
            button.addEventListener('click', (event) => {
                event.preventDefault();
                this.enrollSelf();
            });
        }
        // The reference photo on the edit panel. A ``change`` event and the element itself as
        // the argument, because an ``<input type="file">`` is the one control whose value
        // cannot be re-read later: the file list is a snapshot taken when the dialog closed.
        const pickers = scope.querySelectorAll('[data-edit-photo]');
        for (let i = 0; i < pickers.length; i += 1) {
            const picker = pickers[i];
            if (typeof picker.addEventListener !== 'function') continue;
            picker.addEventListener('change', (event) => {
                this.pickEditPhoto(event && event.target ? event.target : picker);
            });
        }
        const clears = scope.querySelectorAll('[data-clear-edit-photo]');
        for (let i = 0; i < clears.length; i += 1) {
            const clear = clears[i];
            if (typeof clear.addEventListener !== 'function') continue;
            clear.addEventListener('click', (event) => {
                event.preventDefault();
                this.clearEditPhoto();
            });
        }
        // The one-tap fix a taken-id refusal leaves behind. The number it offers is read out
        // of state rather than off the node, so the handler stays the same shape as the four
        // above; the node carries it too (``data-use-free-id``) for whoever is reading the
        // panel rather than clicking it.
        const adopts = scope.querySelectorAll('[data-use-free-id]');
        for (let i = 0; i < adopts.length; i += 1) {
            const adopt = adopts[i];
            if (typeof adopt.addEventListener !== 'function') continue;
            adopt.addEventListener('click', (event) => {
                event.preventDefault();
                this.useSuggestedCredentialsId();
            });
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

    /**
     * Whether this admin may act on somebody's own record.
     *
     * The server's ``_guard_standard_admin``, said in the rail: a standard admin may not
     * reach over an administrator - not their name, not their password, not their face, and
     * not the shift or the overtime decision this rule was extended to. An action that
     * always answers 403 is not a control, it is a trap, so the ones this refuses are
     * absent rather than present-and-refused. The enforcement is on the server, because a
     * hidden button has never stopped anybody.
     *
     * ``head_admin`` is deliberately not restricted: the role owns the deployment, and a
     * rule that also bound it would leave an administrator's own record decidable by nobody.
     */
    mayActOnAccount(account) {
        const actor = State.user || {};
        const privileged = !!account && (account.role === 'admin' || account.role === 'head_admin');
        return !(actor.role === 'admin' && privileged);
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
        const isSelf = String(user.id) === String((State.user || {}).id);
        // Your own reference photo is the one thing about your own row that you *may* change,
        // and it is the whole reason an administrator who works a site can clock in at all.
        // So it is offered on the row that reports whether a face is on file - above the
        // protected-account branch below, which is what suppresses everything else an
        // administrator would do to an administrator.
        const selfEnroll = isSelf ? this.selfEnrollButtonHtml(user, compact) : '';
        if (!this.canManageAccount(user)) {
            return `${selfEnroll}<span class="ui-badge is-quiet" data-protected="true">${this.OPS_ICONS.shield}${this.escapeHtml(I18n.__('credentialsAdminProtected'))}</span>`;
        }
        if (String(user.id) === String(this._credentialsTarget)) return '';
        const id = this.escapeHtml(user.id);
        const inactive = this.accountStatus(user) !== 'active';
        // Your own row gets no way to switch yourself off: the server refuses it (the only
        // account that could be the last head admin is the one asking), so offering the
        // button would be offering a request that always fails. Editing yourself is fine -
        // a name or a rate is not a lockout.
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
        return `<span class="ui-row" style="${rowStyle}">${selfEnroll}${setPassword}${edit}${status}${remove}</span>`;
    },

    /**
     * Whether *this* account may register its own face.
     *
     * The server's answer is ``SELF_ENROLL_ROLES`` in ``main.py``, and the console reads the
     * same list it uses to decide who may step from the console onto the clock: a role that
     * cannot punch has no use for a template, and one that is not in that list would only
     * meet a 403. A test pins the two lists equal, because a button that is drawn from a
     * different list than the endpoint is checked against is a button that fails in the one
     * place nobody looks.
     */
    canSelfEnroll() {
        if (typeof UI === 'undefined' || typeof UI.canOpenHandset !== 'function') return false;
        return UI.canOpenHandset();
    },

    /**
     * Enroll or re-enroll my own face, on the row that reports what is on file.
     *
     * Two labels, one action: an account with no template is being told how to clock in, and
     * an account that has one is being offered a replacement (a template can go stale when
     * the detector changes, and the console's own readiness screen reports exactly that).
     *
     * A ``data-`` hook rather than an inline ``onclick``: the CSP's inline-attribute
     * allowance is pinned per file and may not rise, and that allowance is what an injected
     * ``<img onerror>`` needs. ``paintCredentials`` binds the listener, so a repaint cannot
     * leave a second one behind.
     */
    selfEnrollButtonHtml(user, compact) {
        if (!this.canSelfEnroll()) return '';
        const id = this.escapeHtml(user.id);
        const label = this.escapeHtml(I18n.__(user.face_enrolled ? 'credentialsFaceReplace' : 'credentialsFaceEnroll'));
        const icon = this.OPS_ICONS.camera;
        return compact
            ? `<button type="button" data-enroll-self="${id}" class="ui-btn ui-btn-sm ui-btn-primary is-icon" title="${label}" aria-label="${label}">${icon}</button>`
            : `<button type="button" data-enroll-self="${id}" class="ui-btn ui-btn-sm ui-btn-primary">${icon}${label}</button>`;
    },

    /**
     * Take my own reference photo: the camera, the shutter, and one request.
     *
     * The same camera the punch card uses (``Camera.start``, the same front-facing stream and
     * the same framing once the overlay is up), on the device that will be doing the
     * punching, which is what makes the template resemble the live captures it will be
     * compared with. The server runs the enrollment liveness policy on what comes back, so a
     * photo of a photo cannot become a permanent template - the overlay says so, because a
     * refusal the reader cannot predict is a refusal that looks like a bug.
     */
    async enrollSelf() {
        if (!this.canSelfEnroll()) return;
        // One overlay at a time: a second shutter over the first would post two templates for
        // the same account, and the loser of that race is whichever one lands second. Held
        // rather than searched for, like the print sheet: it is the node this path put in the
        // page, so it is the node this path takes back out.
        if (this._enrollBusy || this._enrollOverlay) return;
        if (typeof Camera === 'undefined' || !Camera.isSupported) {
            const info = Camera.explain({ name: 'NotAllowedError' });
            UI.showHelpModal(info.title, info.steps);
            return;
        }
        this._enrollBusy = true;
        try {
            const overlay = document.createElement('div');
            overlay.className = 'camera-overlay';
            overlay.id = 'enrollOverlay';
            overlay.innerHTML = `
                <div class="camera-head" style="padding-top:calc(16px + var(--safe-top))">
                    <p class="camera-action">${this.escapeHtml(I18n.__('credentialsFaceEnrollTitle'))}</p>
                    <button type="button" data-close-enroll="true" class="icon-button" style="background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.25);color:#fff">\u2715</button>
                </div>
                <video id="enrollVideo" autoplay playsinline muted></video>
                <div class="camera-controls">
                    <div class="camera-controls-row">
                        <div class="camera-spacer"></div>
                        <button type="button" id="enrollShutter" class="shutter-button" aria-label="${this.escapeHtml(I18n.__('credentialsFaceEnrollTake'))}"></button>
                        <div class="camera-spacer"></div>
                    </div>
                    <p class="camera-hint">${this.escapeHtml(I18n.__('credentialsFaceEnrollHint'))}</p>
                </div>`;
            document.body.appendChild(overlay);
            document.body.style.overflow = 'hidden';
            this._enrollOverlay = overlay;
            // Scoped to the overlay that was just built, not searched for across the document:
            // this camera is one of two the app can have on screen (a punch card is the other),
            // and the two controls below belong to this one.
            const close = overlay.querySelector('[data-close-enroll]');
            if (close) close.addEventListener('click', () => this.closeEnrollCamera());
            const shutter = overlay.querySelector('#enrollShutter');
            if (shutter) shutter.addEventListener('click', () => this.submitSelfEnroll());
            try {
                const stream = await Camera.start();
                const video = document.getElementById('enrollVideo');
                video.srcObject = stream;
                try { await video.play(); } catch (e) { /* autoplay policing; ignore */ }
            } catch (err) {
                // The handset's own explanation of a camera that will not open - permission
                // refused, an insecure origin, no camera on a desktop - because it is the
                // same camera and the same three reasons.
                const info = Camera.explain(err);
                this.closeEnrollCamera();
                UI.showHelpModal(info.title, info.steps);
                return;
            }
        } finally {
            this._enrollBusy = false;
        }
    },

    /**
     * Send the frame. Success is the row saying "enrolled" a moment later.
     *
     * The request carries the photo and nothing else. Whose template this is, is the token -
     * there is no account id here to be pointed at somebody else, and the endpoint refuses a
     * role that is not allowed to enroll itself at all.
     */
    async submitSelfEnroll() {
        const video = document.getElementById('enrollVideo');
        const shutter = document.getElementById('enrollShutter');
        if (!State.user) {
            // The session died while the overlay was open; without this the capture throws on
            // a user that is no longer there and the reader is left on a camera that cannot
            // send anything.
            this.closeEnrollCamera();
            Toast.error(I18n.__('sessionExpiredSignInAgain'));
            return;
        }
        if (!video || !video.videoWidth) {
            Toast.error(I18n.__('cameraNotReady'));
            return;
        }
        if (shutter) shutter.disabled = true;
        let blob = null;
        try {
            blob = await Camera.snapshot(video);
        } catch (err) {
            blob = null;
        }
        if (!blob) {
            if (shutter) shutter.disabled = false;
            Toast.error(I18n.__('cameraFailedTitle'));
            return;
        }
        const form = new FormData();
        form.append('photo', blob, 'reference.jpg');
        try {
            await API.request('/worker/me/enroll', { method: 'POST', body: form });
        } catch (err) {
            // A 401 has already cleared the session and repainted the login screen; take the
            // camera down with it. Everything else - a liveness refusal, a frame the model
            // cannot use - is about this photo, so the overlay stays open and the shutter
            // comes back, and the reader can simply try again.
            if (!State.user) {
                this.closeEnrollCamera();
                Toast.error(I18n.__('sessionExpiredSignInAgain'));
                return;
            }
            if (shutter) shutter.disabled = false;
            Toast.error(`${I18n.__('credentialsFaceEnrollFailed')}: ${err.message}`);
            return;
        }
        this.closeEnrollCamera();
        Toast.success(I18n.__('credentialsFaceEnrollDone'));
        // The roster is the answer to "did it work": repainting from the server is what turns
        // the face cell into "enrolled" and the button into "replace".
        await this.loadCredentials(document.getElementById('adminContent'));
    },

    /** Take the enrollment camera down, and the page back out of the way of nothing. */
    closeEnrollCamera() {
        State.stopCamera();
        const overlay = this._enrollOverlay;
        this._enrollOverlay = null;
        if (overlay && typeof overlay.remove === 'function') overlay.remove();
        document.body.style.overflow = '';
    },

    /** The enrollment camera this module has open, if any - one at a time, by construction. */
    _enrollOverlay: null,

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
    _credentialsEditDraft: { name: '', email: '', phone: '', hourly_rate: '', transit_enabled: false },

    /**
     * The replacement reference photo chosen on the edit panel, or ``null``.
     *
     * The selection is held as the file object itself rather than read back from the input
     * when it is time to send: a repaint rebuilds the picker, and browsers do not let a file
     * be put back into one - so the object in memory is the only thing a retry after a failed
     * upload can still send. Cleared whenever the panel opens, closes or saves.
     */
    _credentialsEditPhoto: null,

    /** Why the chosen photo cannot be used, or ``""``. Shown beside the picker. */
    _credentialsEditPhotoError: '',

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
        // Whatever was chosen for the last account, dropped before this one opens: a photo
        // left over from the previous row would be written as *this* person's template, which
        // is the same one-account-to-another bug the password panel is arranged to prevent.
        this._credentialsEditPhoto = null;
        this._credentialsEditPhotoError = '';
        try {
            const user = await API.request(`/admin/users/${encodeURIComponent(userId)}`);
            this._credentialsEdit = user;
            this._credentialsEditDraft = {
                name: user.name || '',
                email: user.email || '',
                phone: user.phone || '',
                hourly_rate: user.hourly_rate === null || user.hourly_rate === undefined
                    ? '' : String(user.hourly_rate),
                transit_enabled: user.transit_enabled === true
            };
        } catch (err) {
            Toast.error(err.message);
            this._credentialsEdit = null;
        }
        return this.repaintCredentialsFromCache();
    },

    closeUserEdit() {
        this._credentialsEdit = null;
        this._credentialsEditDraft = { name: '', email: '', phone: '', hourly_rate: '', transit_enabled: false };
        // The chosen file goes with the panel: it is a copy of somebody's face in memory, and
        // closing the form is what stops holding it.
        this._credentialsEditPhoto = null;
        this._credentialsEditPhotoError = '';
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
        // The transit grant is a checkbox, so it is read for its ``checked`` state rather than
        // its value, and only while the box actually exists - a repaint that dropped it must
        // not silently turn a granted privilege off on the next save.
        const transit = document.getElementById('userEditTransit');
        if (transit && typeof transit.checked === 'boolean') {
            this._credentialsEditDraft.transit_enabled = transit.checked;
        }
        return this._credentialsEditDraft;
    },

    credentialsEditPanelHtml() {
        const user = this._credentialsEdit;
        if (!user) return '';
        const draft = this._credentialsEditDraft;
        const photo = this._credentialsEditPhoto;
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
                    <!-- The off-geofence privilege, granted one account at a time. It is a plain
                         checkbox and not a role, because two workers can hold the same role and
                         only one of them drive - so the administrator names the person, here,
                         rather than handing the privilege to a category. -->
                    <label class="ui-stack is-flush ui-span-all">
                        <span class="${label}">
                            <input type="checkbox" id="userEditTransit" ${draft.transit_enabled ? 'checked' : ''}>
                            ${I18n.__('credentialsTransit')}
                        </span>
                    </label>
                </div>
                <p class="ui-note">${I18n.__('credentialsHourlyRateHint')}</p>
                <p class="ui-note">${I18n.__('credentialsTransitHint')}</p>
                <!-- The face, which is the one thing about this person that is not a field on
                     their row. What is on file is stated rather than implied, because the two
                     cases are different jobs: a first photo is what makes the account able to
                     clock in at all, and a replacement silently stops the previous template
                     working - see the hint under the picker, which says so. -->
                <div class="ui-facts" style="margin-top:12px">
                    <div class="ui-fact">
                        <span class="ops-stat-label">${this.escapeHtml(I18n.__('credentialsPhoto'))}</span>
                        <span data-edit-face="${user.face_enrolled ? 'enrolled' : 'missing'}">${this.credentialsFace(user)}</span>
                    </div>
                </div>
                <div class="ui-row" style="margin-top:12px">
                    <input type="file" id="userEditPhoto" accept="image/jpeg,image/png,image/webp"
                           data-edit-photo="${this.escapeHtml(user.id)}" class="${field}">
                    ${photo ? `<span class="ui-note" data-edit-photo-chosen>${this.escapeHtml(photo.name || '')} · ${this.photoSizeLabel(photo)}</span>
                        <button type="button" data-clear-edit-photo="true" class="${quiet}">${I18n.__('credentialsPhotoRemove')}</button>` : ''}
                </div>
                <p class="ui-note">${I18n.__('credentialsPhotoEditHint')}</p>
                ${this._credentialsEditPhotoError ? `<p class="ui-note is-danger" data-edit-photo-error>${this.escapeHtml(this._credentialsEditPhotoError)}</p>` : ''}
                <div class="ui-row">
                    <button type="button" onclick="UI_MODULES.saveUserEdit()" class="ui-btn ui-btn-primary">${I18n.__('save')}</button>
                    <button type="button" onclick="UI_MODULES.closeUserEdit()" class="${quiet}">${I18n.__('cancel')}</button>
                </div>
            </div>`;
    },

    /**
     * Holds the photo chosen for the account being edited, or refuses it before upload.
     *
     * The same policy check the create form makes (``checkPhotoFile``), and deliberately the
     * same one: one account's photo is not a looser kind of photo than another's. A refusal
     * holds nothing, says why beside the picker and makes no request - the alternative is a
     * 5 MB upload over a site's phone tether ending in a 413 the admin could have been told
     * about instantly.
     */
    pickEditPhoto(input) {
        const chosen = input && input.files && input.files[0] ? input.files[0] : null;
        const problem = this.checkPhotoFile(chosen);
        this.readUserEditDraft();
        this._credentialsEditPhotoError = problem || '';
        this._credentialsEditPhoto = problem ? null : chosen;
        if (problem) Toast.error(problem);
        if (problem) return this.repaintCredentialsFromCache();
        this.shrinkFacePhoto(chosen, (small) => {
            // Guarded: a resize takes a moment, and the admin may have chosen another photo -
            // or cleared this one - while it was running. The slower answer must not win.
            if (this._credentialsEditPhoto === chosen) {
                this._credentialsEditPhoto = small;
                this.repaintCredentialsFromCache();
            }
        });
        return this.repaintCredentialsFromCache();
    },

    /**
     * A chosen *face* photo, reduced to the deployment's ingestion boundary before it is sent.
     *
     * Enrolling somebody usually starts with a photograph on the admin's own phone, which its
     * camera app wrote at 12 MP - above the boundary that protects the punch path. Resizing
     * here is what keeps that boundary from refusing the ordinary way a worker gets enrolled;
     * the server still checks the bytes it receives, and ``checkPhotoFile`` has already refused
     * size and type. The logo picker deliberately does not come through here: a mark is not a
     * face frame and the boundary does not apply to it.
     */
    shrinkFacePhoto(file, done) {
        // ``UI``, not ``Capture``: this module is fetched by the app shell, which does not carry
        // ``capture.js`` (that is the link pages' camera, and the shell's eager script list is
        // pinned to what *every* session needs). One implementation per page world, with the
        // boundary's numbers pinned against the backend by ``test_frontend_photo_downscale``.
        if (!file || typeof UI === 'undefined' || !UI.shrinkPhoto) return done(file);
        return UI.shrinkPhoto(file, done);
    },

    clearEditPhoto() {
        this._credentialsEditPhoto = null;
        this._credentialsEditPhotoError = '';
        return this.repaintCredentialsFromCache();
    },

    /**
     * Writes the chosen photo as this account's template: ``POST /admin/enroll``.
     *
     * The one endpoint for the job, shared with the console's own enrollment and the bulk
     * roster import - it takes a file from disk, embeds it and overwrites both files the
     * account's punches are checked against. Returns whether it landed, because the caller
     * decides what a failure means: the account's *fields* are saved by then, so the panel
     * stays open with the file still held and the reason beside the picker rather than
     * closing on a person whose face was never written.
     */
    async uploadEditPhoto(user) {
        const photo = this._credentialsEditPhoto;
        if (!photo) return true;
        const form = new FormData();
        form.append('worker_id', user.id);
        form.append('photo', photo, photo.name || 'reference.jpg');
        try {
            await API.request('/admin/enroll', { method: 'POST', body: form });
        } catch (err) {
            this._credentialsEditPhotoError = err.message;
            Toast.error(`${I18n.__('credentialsFaceEnrollFailed')}: ${err.message}`);
            await this.repaintCredentialsFromCache();
            return false;
        }
        this._credentialsEditPhoto = null;
        this._credentialsEditPhotoError = '';
        Toast.success(I18n.__('credentialsPhotoSaved'));
        return true;
    },

    /**
     * Saves the edit, and shows the server's refusal as the reason it gave.
     *
     * The refusals are all actionable and all different ("Name must not be empty.",
     * "Standard Admins cannot edit administrator accounts.", "User ID not found."), so
     * none of them is replaced with a generic failure: an admin who fixes what the
     * message named gets a saved account on the next click.
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
            phone: draft.phone,
            // The off-geofence privilege, sent for exactly this one account. It travels on every
            // save (not only when the box changed), because the checkbox states the whole truth
            // about the grant and the server treats an absent field as "leave it alone".
            transit_enabled: !!draft.transit_enabled
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
        } catch (err) {
            Toast.error(err.message);
            return;
        }
        // A chosen photo is a second request, because it is a second content type: the
        // account's own fields travel as JSON and a file cannot. It is sent only when one was
        // chosen, so the ordinary edit - a name, a rate - is still one round trip.
        if (this._credentialsEditPhoto) {
            const uploaded = await this.uploadEditPhoto(user);
            if (!uploaded) return;
        }
        Toast.success(I18n.__('credentialsUserSaved'));
        this.closeUserEdit();
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
    //  **Enrollment link** hands the *photo* to the person it belongs to: a one-time link
    //  that registers that account's face, taken where they actually clock in. It creates
    //  nothing - an account is created here, or approved from the site's registration link
    //  after somebody has looked at the applicant's photograph - so the link carries an
    //  account id and no other field at all.
    //
    //  The upload rule is the server's, restated here so a 6 MB photo is refused before
    //  it is uploaded over a phone tether: 5 MB, JPEG/PNG/WebP, nothing else. This is a
    //  courtesy and not the check - ``accept=`` and ``file.type`` are both supplied by
    //  whoever sends the request, so ``backend/uploads.py`` decides by reading the bytes.
    // -----------------------------------------------------------------

    /** The create form's fields, kept so that a repaint never loses what was typed. */
    _credentialsDraft: { id: '', name: '', role: 'worker', email: '', phone: '' },

    /** The enrollment-link form's one field, for the same reason. */
    _credentialsInviteDraft: { id: '' },

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
            // A form the admin has just opened is not a form that was just refused.
            this._credentialsIdTaken = null;
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
        this._credentialsIdTaken = null;
        return this.repaintCredentialsFromCache();
    },

    /**
     * The roles this admin may hand out.
     *
     * A standard admin cannot create administrators anywhere on the server, so the
     * option is absent rather than present-and-403 - the same reason the password button
     * is hidden for an administrator's row. The list is the whole rule: the role chosen
     * here is what the account becomes, and no id picks it for them.
     */
    creatableRoles() {
        const actor = State.user || {};
        const roles = ['worker', 'moallem', 'off_office'];
        if (actor.role === 'head_admin') roles.push('admin', 'head_admin');
        return roles;
    },

    /**
     * Whether an id is the kind of number the server accepts.
     *
     * This mirrors ``main._validate_id_and_role`` and nothing else. The per-role id blocks
     * are gone server-side: an id no longer decides a role, and refusing one for falling
     * outside a block is how this form came to reject accounts the server would have
     * created happily. What is left of that old rule is the part every allocator in the
     * application depends on - an account id is a whole number - so it is caught here
     * rather than by a doomed round trip. Whether the id is *free* stays the server's
     * answer to give: the roster in hand can be a moment out of date, and a second
     * administrator creating the same id is exactly what the 400 is for.
     */
    idIsWholeNumber(userId) {
        const text = String(userId === null || userId === undefined ? '' : userId).trim();
        return /^[+-]?\d+$/.test(text);
    },

    /** Whether the server's answer to a create attempt was the taken-id refusal. */
    isTakenIdRefusal(message) {
        const said = String(message === null || message === undefined ? '' : message).trim();
        return said === this.TAKEN_ID_REFUSAL;
    },

    /**
     * The lowest id above the refused one that no account on the roster is using.
     *
     * A suggestion and not a rule: the server accepts any free number, this is simply the
     * one the admin most likely meant. Counting upward from the refused id rather than
     * scanning from 1 keeps it beside the number they typed, which is where a mistyped
     * neighbour is; every step either lands on a free number or skips one account, so the
     * walk cannot outlast the roster. If somebody else takes this number first, the next
     * refusal says so and offers the one after it - the server stays the authority on which
     * ids are free.
     */
    nextFreeCredentialsId(refusedId, roster) {
        const taken = new Set((roster || []).map((user) => String(user && user.id)));
        const start = Math.max(0, Math.floor(Number(refusedId)) || 0);
        let candidate = start;
        while (candidate - start <= taken.size && taken.has(String(candidate + 1))) candidate += 1;
        return String(candidate + 1);
    },

    /**
     * Takes the free number the refusal offered: the id box gets it, ready to send again.
     *
     * One tap instead of a retype on a phone. The number goes into the draft the panel paints
     * from and into the box itself, and the refusal is forgotten with it, because the id it
     * was about is no longer the one being offered.
     */
    useSuggestedCredentialsId() {
        const taken = this._credentialsIdTaken;
        if (!taken) return Promise.resolve();
        this.readCredentialsDraft();
        const free = String(taken.free);
        this._credentialsDraft.id = free;
        // Into the box as well as the draft it paints from. The draft is what survives a
        // repaint, but the field is what the admin reads back - and this method should not
        // leave the two disagreeing for as long as a repaint takes.
        const box = document.getElementById('credentialsNewId');
        if (box && box.value !== undefined) box.value = free;
        this._credentialsIdTaken = null;
        return this.repaintCredentialsFromCache();
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
    /**
     * Why the chosen file cannot be used, or ``null`` when it can.
     *
     * ``wording`` is what the refusal calls the file. One policy, three subjects: a face
     * reference, a logo, and whatever comes next all go through the same ceiling and the
     * same type allowlist, and only the sentence changes - a company uploading its mark is
     * not being told to "retake the photo".
     */
    checkPhotoFile(file, wording) {
        const say = wording || {
            unreadable: 'credentialsPhotoUnreadable',
            tooLarge: 'credentialsPhotoTooLarge',
            wrongType: 'credentialsPhotoWrongType'
        };
        if (!file) return null;
        const size = Number(file.size || 0);
        if (size <= 0) return I18n.__(say.unreadable);
        if (size > this.PHOTO_POLICY.maxBytes) {
            return `${I18n.__(say.tooLarge)} (${this.photoSizeLabel(file)} / ${this.PHOTO_POLICY.maxMb} MB)`;
        }
        const type = String(file.type || '').toLowerCase();
        if (this.PHOTO_POLICY.accepted.indexOf(type) >= 0) return null;
        // Some pickers report an empty type for a perfectly good JPEG - usually the
        // camera app's own ``.jpg``. The extension is a hint only; the server still
        // decides from the bytes, so this cannot let a document through.
        const name = String(file.name || '').toLowerCase();
        if (!type && /\.(jpe?g|png|webp)$/.test(name)) return null;
        return I18n.__(say.wrongType);
    },

    pickCredentialsPhoto(input) {
        const chosen = input && input.files && input.files[0] ? input.files[0] : null;
        const problem = this.checkPhotoFile(chosen);
        this.readCredentialsDraft();
        this._credentialsPhotoError = problem || '';
        this._credentialsNewPhoto = problem ? null : chosen;
        if (problem) Toast.error(problem);
        if (problem) return this.repaintCredentialsFromCache();
        this.shrinkFacePhoto(chosen, (small) => {
            if (this._credentialsNewPhoto === chosen) {
                this._credentialsNewPhoto = small;
                this.repaintCredentialsFromCache();
            }
        });
        return this.repaintCredentialsFromCache();
    },

    clearCredentialsPhoto() {
        this._credentialsNewPhoto = null;
        this._credentialsPhotoError = '';
        return this.repaintCredentialsFromCache();
    },

    /** The role select belongs to the create form: a link hands out no role at all. */
    credentialsRoleChanged(role) {
        this.readCredentialsDraft();
        this._credentialsDraft.role = role;
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

    /**
     * The one field the link form has: the account the link is for.
     *
     * Nothing else travels, because nothing else may be chosen here any more. The name, the
     * role and the contact details all belong to the account that already exists, and the
     * server reads them off that row - a form that could send a name would be a form that
     * could send a *different* one.
     */
    readInviteDraft() {
        if (this._credentialsMode !== 'invite' || this._credentialsIssued) return this._credentialsInviteDraft;
        const element = document.getElementById('credentialsLinkId');
        if (element && element.value !== undefined) {
            this._credentialsInviteDraft.id = String(element.value).trim();
        }
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
        const taken = this._credentialsIdTaken;
        return `
            <div class="${box}" data-create-panel="true">
                <p class="ui-card-title">${I18n.__('credentialsNewAccount')}</p>
                <p class="ui-note is-body">${I18n.__('credentialsNewAccountHint')}</p>
                <div class="ui-grid two">
                    <input type="text" id="credentialsNewId" value="${this.escapeHtml(draft.id)}" inputmode="numeric"
                           placeholder="${I18n.__('credentialsNewId')}" class="${field}${taken ? ' is-danger' : ''}">
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
                <p class="ui-note" data-id-rule="true">${this.escapeHtml(I18n.__('credentialsIdRule'))}</p>
                ${taken ? `<p class="ui-note is-danger" data-id-taken="${this.escapeHtml(taken.id)}">
                    ${this.escapeHtml(I18n.__('credentialsIdTaken').replace('{id}', taken.id))}
                    <button type="button" class="${quiet}" data-use-free-id="${this.escapeHtml(taken.free)}">${this.escapeHtml(I18n.__('credentialsIdUseFree').replace('{id}', taken.free))}</button>
                </p>` : ''}
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
                <div class="ui-row">
                    <input type="text" id="credentialsLinkId" value="${this.escapeHtml(draft.id)}" inputmode="numeric"
                           placeholder="${I18n.__('credentialsNewId')}" class="${field}">
                </div>
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
        if (!this.idIsWholeNumber(draft.id)) {
            Toast.error(I18n.__('credentialsIdWholeNumber'));
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
            this._credentialsIdTaken = null;
            this._credentialsDraft = { id: '', name: '', role: draft.role, email: '', phone: '' };
            Toast.success(I18n.__('credentialsCreatedToast'));
            await this.loadCredentials(document.getElementById('adminContent'));
        } catch (err) {
            // A taken id is the one refusal this form can answer itself, and the answer is a
            // number: the roster in hand says which ones are free. Offered rather than
            // applied - the admin typed that id for a reason, and a form that silently
            // rewrote it would be the range check again, wearing a helpful face. Without a
            // roster there is nothing to suggest from, so the toast stands alone.
            if (this._credentials && this.isTakenIdRefusal(err.message)) {
                this._credentialsIdTaken = {
                    id: draft.id,
                    free: this.nextFreeCredentialsId(draft.id, this._credentials)
                };
                Toast.error(err.message);
                await this.repaintCredentialsFromCache();
                return;
            }
            Toast.error(err.message);
        }
    },

    /**
     * Issues the one-time enrollment link for an account that already exists.
     *
     * The account id is the whole request. An id no account carries is the server's answer
     * to give (404), and deliberately not a range check here: the console does not know the
     * roster, and what a link is about is a person who is already on it.
     *
     * The token comes back exactly once and is not stored in readable form anywhere - it
     * is a hash in ``enrollment_invites`` - so the panel keeps it on screen until it is
     * closed, and there is no "show it again" button to offer.
     */
    async issueCredentialsLink() {
        const draft = this.readInviteDraft();
        if (!draft.id) {
            Toast.error(I18n.__('credentialsLinkNeedsId'));
            return;
        }
        try {
            this._credentialsIssued = await API.request('/admin/enrollment/invites', {
                method: 'POST',
                body: { worker_id: draft.id }
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
            off_office: 'roleOffOffice',
            admin: 'roleAdmin',
            head_admin: 'roleHeadAdmin',
            developer: 'roleDeveloper'
        };
        return keys[role] ? I18n.__(keys[role]) : String(role || '');
    },

    // =====================================================================
    //  Developer - the root tier's own tools, in one place
    // =====================================================================
    //
    // Four surfaces that existed only as endpoints until now: the runtime
    // store (``/developer/runtime``), the deployment's private alert hub
    // (``/developer/alerts``), database diagnostics (``/developer/diagnostics``)
    // and the raw audit stream (``/developer/audit``). They are read-only-ish
    // operator tools - a flag, a cache drop, an acknowledgement, a plan - and
    // every one of them was reachable only with curl, which for the person who
    // runs the deployment is the same as not being reachable at all.
    //
    // Three rules, the same ones the alert queue follows:
    //
    // 1. **Server text is escaped like any other text.** A summary, a runtime
    //    document, a trace id - each is interpolated through ``escapeHtml``.
    // 2. **No inline handler.** The document CSP still allows
    //    ``script-src-attr 'unsafe-inline'`` and its per-file budget may only
    //    fall, so every control is bound through ``bindDeveloperControls`` by a
    //    ``data-`` hook rather than an ``onclick``.
    // 3. **A failed read is a sentence, not a blank panel** - ``uiErrorHtml``,
    //    like every other tab.

    //: The last good read, so a repaint after an action does not re-fetch. Cleared
    //: on every tab entry, because an operator arriving here wants the truth now.
    _devConsole: null,

    async renderDeveloperConsole(content) {
        if (!content) return;
        content.innerHTML = UI.consoleSkeletonHtml(I18n.__('developerConsole'));
        this._devConsole = null;
        let runtime, alerts, pool, slow, audit, backups, sessions;
        let ml, shadow, db, engine, offline, tamper;
        try {
            [runtime, alerts, pool, slow, audit, backups, sessions,
             ml, shadow, db, engine, offline, tamper] = await Promise.all([
                API.request('/developer/runtime'),
                API.request('/developer/alerts?limit=50'),
                API.request('/developer/diagnostics/pool'),
                API.request('/developer/diagnostics/slow-queries?limit=20'),
                API.request('/developer/audit?limit=100'),
                // One read of the backup directory. The list carries the verdict each snapshot
                // recorded when it was written; re-hashing every file is a separate, deliberate
                // act (the Verify button), because it costs the disk and this is a tab paint.
                API.request('/developer/db/backups?limit=50'),
                API.request('/developer/sessions'),
                // The diagnostic domains: the models and their bands, the shadow migration's
                // paired log, the database and its journal, the child that owns the models, and
                // the offline protocol's two ledgers. All six are reads an operator waits on -
                // the two *acts* beside them (a full integrity check, a checkpoint) are buttons,
                // because neither belongs in a tab paint.
                API.request('/developer/ml/diagnostics'),
                API.request('/developer/ml/shadow-summary'),
                API.request('/developer/db/stats'),
                API.request('/developer/engine/process-stats'),
                API.request('/developer/offline/devices'),
                API.request('/developer/offline/tamper-alerts')
            ]);
        } catch (err) {
            content.innerHTML = this.uiErrorHtml(err, "UI.renderAdminTab('Developer')");
            return;
        }
        this._devConsole = {
            runtime: runtime || {}, alerts: alerts || {}, pool: pool || {},
            slow: slow || {}, audit: audit || {}, backups: backups || {},
            sessions: sessions || [], ml: ml || {}, shadow: shadow || {}, db: db || {},
            engine: engine || {}, offline: offline || {}, tamper: tamper || {}
        };
        content.innerHTML = `<div class="ui-page" data-developer-console="true">${this.devConsoleHtml()}</div>`;
        this.bindDeveloperControls(content);
    },

    devConsoleHtml() {
        return `
            <header class="ui-section-head">
                <h1 class="ui-section-title">${this.escapeHtml(I18n.__('developerConsole'))}</h1>
                <p class="ui-section-note">${this.escapeHtml(I18n.__('hintDeveloperConsole'))}</p>
            </header>
            ${this.devRuntimeHtml()}
            ${this.devAlertsHtml()}
            ${this.devDiagnosticsHtml()}
            ${this.devMlHtml()}
            ${this.devDatabaseHtml()}
            ${this.devEngineHtml()}
            ${this.devOfflineHtml()}
            ${this.devBackupsHtml()}
            ${this.devSessionsHtml()}
            ${this.devAuditHtml()}`;
    },

    /** The runtime store: a value every worker picks up without a redeploy. */
    devRuntimeHtml() {
        const data = this._devConsole.runtime || {};
        const values = data.values || {};
        const defaults = data.defaults || {};
        const docs = data.documentation || {};
        const levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
        const field = (key, value) => {
            const name = ` data-runtime-input="${this.escapeHtml(key)}"`;
            if (typeof value === 'boolean') {
                return `<select class="ui-field"${name}><option value="true"${value ? ' selected' : ''}>true</option>` +
                    `<option value="false"${value ? '' : ' selected'}>false</option></select>`;
            }
            if (key === 'log_level') {
                return `<select class="ui-field"${name}>${levels.map((level) =>
                    `<option value="${level}"${String(value) === level ? ' selected' : ''}>${level}</option>`).join('')}</select>`;
            }
            return `<input class="ui-field"${name} value="${this.escapeHtml(String(value))}">`;
        };
        const rows = Object.keys(values).sort().map((key) => `
            <div class="ui-card" data-runtime="${this.escapeHtml(key)}">
                <div class="ui-spread">
                    <div class="ops-row-main">
                        <span class="ops-name">${this.escapeHtml(key)}</span>
                        <span class="ops-sub">${this.escapeHtml(I18n.__('devRuntimeDefault').replace('{value}', String(defaults[key])))}</span>
                    </div>
                    <button type="button" class="ui-btn ui-btn-sm" data-runtime-save="${this.escapeHtml(key)}">${this.escapeHtml(I18n.__('devRuntimeSave'))}</button>
                </div>
                <p class="ui-note is-body">${this.escapeHtml(docs[key] || '')}</p>
                <div class="ui-row">
                    ${field(key, values[key])}
                    <input class="ui-field" data-runtime-note="${this.escapeHtml(key)}" placeholder="${this.escapeHtml(I18n.__('devRuntimeNote'))}">
                </div>
            </div>`).join('');
        return `
            <section class="ui-stack" data-dev-section="runtime">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devRuntime'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devRuntimeHint'))}</p>
                <p class="ops-sub">${this.escapeHtml(I18n.__('devRuntimeVersion').replace('{version}', String(data.version ?? '')))}</p>
                ${rows}
            </section>`;
    },

    /** The private hub: what the deployment has said about itself, newest first. */
    devAlertsHtml() {
        const data = this._devConsole.alerts || {};
        const alerts = data.alerts || [];
        const kinds = data.kinds || {};
        const rows = alerts.map((alert) => {
            const severity = String(alert.severity || 'info');
            const tone = severity === 'critical' ? ' is-danger' : (severity === 'warning' ? ' is-warn' : '');
            const read = !!alert.read_at;
            return `
                <article class="ui-card${tone}" data-dev-alert="${this.escapeHtml(String(alert.id))}" data-dev-alert-unread="${read ? '0' : '1'}">
                    <div class="ui-spread">
                        <div class="ops-row-main">
                            <span class="ui-badge${tone}">${this.escapeHtml(severity)}</span>
                            <div class="ops-who">
                                <span class="ops-name">${this.escapeHtml(alert.summary || '')}</span>
                                <span class="ops-sub">${this.escapeHtml(`${alert.kind || ''} \u00b7 ${alert.created_at || ''}`)}</span>
                            </div>
                        </div>
                        ${read
                            ? `<span class="ui-badge" data-dev-alert-read-chip="true">${this.OPS_ICONS.check}${this.escapeHtml(I18n.__('devAlertsRead'))}</span>`
                            : `<button type="button" class="ui-btn ui-btn-sm" data-dev-alert-read="${this.escapeHtml(String(alert.id))}">${this.escapeHtml(I18n.__('devAlertsMarkRead'))}</button>`}
                    </div>
                    <p class="ui-note is-body">${this.escapeHtml(kinds[alert.kind] || '')}</p>
                </article>`;
        }).join('');
        return `
            <section class="ui-stack" data-dev-section="alerts">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devAlerts'))}</h2>
                <p class="ops-sub">${this.escapeHtml(I18n.__('devAlertsUnread').replace('{count}', String(data.unread ?? 0)))}</p>
                ${rows || `<p class="ui-empty">${this.escapeHtml(I18n.__('devAlertsEmpty'))}</p>`}
            </section>`;
    },

    /** The write path's real counters, the slow statements, and a plan on demand. */
    devDiagnosticsHtml() {
        const pool = this._devConsole.pool || {};
        const slow = this._devConsole.slow || {};
        const recent = slow.recent || [];
        const fields = Object.keys(pool).sort().map((key) =>
            `<span class="ui-fact"><span class="ui-fact-sub">${this.escapeHtml(key)}</span>` +
            `<span class="ui-fact-value">${this.escapeHtml(String(pool[key]))}</span></span>`).join('');
        const slowRows = recent.map((row) => `<tr>
                <td>${this.escapeHtml(String(row.operation || row.verb || ''))}</td>
                <td>${this.escapeHtml(String(row.duration_ms ?? row.ms ?? ''))}</td>
                <td>${this.escapeHtml(String(row.trace_id || ''))}</td>
            </tr>`).join('');
        const explainable = (slow.explainable || []).map((name) =>
            `<button type="button" class="ui-btn ui-btn-sm" data-dev-explain="${this.escapeHtml(name)}">${this.escapeHtml(name)}</button>`).join('');
        return `
            <section class="ui-stack" data-dev-section="diagnostics">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devDiagnostics'))}</h2>
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devPool'))}</h3>
                <div class="ui-grid three">${fields}</div>
                <p class="ui-note">${this.escapeHtml(pool.saturated ? I18n.__('devPoolSaturated') : I18n.__('devPoolHealthy'))}</p>
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devSlowQueries'))}</h3>
                <p class="ops-sub">${this.escapeHtml(I18n.__('devSlowThreshold').replace('{ms}', String(slow.threshold_ms ?? '')))}</p>
                <table class="ui-table">
                    <thead><tr>
                        <th>${this.escapeHtml(I18n.__('devColOperation'))}</th>
                        <th>${this.escapeHtml(I18n.__('devColDuration'))}</th>
                        <th>${this.escapeHtml(I18n.__('devColTrace'))}</th>
                    </tr></thead>
                    <tbody>${slowRows || `<tr><td colspan="3">${this.escapeHtml(I18n.__('devSlowEmpty'))}</td></tr>`}</tbody>
                </table>
                <p class="ui-note">${this.escapeHtml(I18n.__('devExplainHint'))}</p>
                <div class="ui-row">${explainable}</div>
                <pre class="ui-note" data-dev-plan="true"></pre>
                <button type="button" class="ui-btn" data-dev-flush="true">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('devFlush'))}</button>
                <p class="ui-note">${this.escapeHtml(I18n.__('devFlushHint'))}</p>
            </section>`;
    },

    /**
     * Live sessions: who could be signed in, and the one lever that ends it.
     *
     * The server cannot list tokens - a bearer JWT is stateless, so "which sessions exist"
     * is unknowable by design - and this section does not pretend otherwise. It shows who
     * *could* act right now, when their credential was last minted (the audit ``login`` rows,
     * the best evidence there is), and what revoking would do. The row is the revocation's
     * own scope: one account per row, so the lever cannot be pulled at "everyone" by accident.
     */
    devSessionsHtml() {
        const sessions = this._devConsole.sessions || [];
        const me = (State.user || {}).id;
        const rows = sessions.map((session) => {
            const isSelf = String(session.id) === String(me);
            return `
                <tr data-dev-session="${this.escapeHtml(String(session.id))}" data-dev-session-role="${this.escapeHtml(String(session.role || ''))}">
                    <td>${this.escapeHtml(String(session.id || ''))}</td>
                    <td>${this.escapeHtml(String(session.name || ''))}</td>
                    <td>${this.escapeHtml(this.roleLabel(session.role || ''))}</td>
                    <td>${this.escapeHtml(String(session.last_login_at || '\u2014'))}</td>
                    <td>${this.escapeHtml(String(session.last_login_ip || '\u2014'))}</td>
                    <td>${this.escapeHtml(String(session.active_devices ?? 0))}</td>
                    <td><button type="button" class="ui-btn ui-btn-sm ui-btn-quiet" data-dev-session-revoke="${this.escapeHtml(String(session.id))}" ${isSelf ? 'data-dev-session-self="true"' : ''}>${this.OPS_ICONS.logout}${this.escapeHtml(I18n.__('devSessionsRevoke'))}</button></td>
                </tr>`;
        }).join('');
        return `
            <section class="ui-stack" data-dev-section="sessions">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devSessions'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devSessionsHint'))}</p>
                <table class="ui-table">
                    <thead><tr>
                        <th>${this.escapeHtml(I18n.__('devColActor'))}</th>
                        <th>${this.escapeHtml(I18n.__('devSessionsColName'))}</th>
                        <th>${this.escapeHtml(I18n.__('role'))}</th>
                        <th>${this.escapeHtml(I18n.__('devSessionsColLastLogin'))}</th>
                        <th>${this.escapeHtml(I18n.__('devSessionsColIp'))}</th>
                        <th>${this.escapeHtml(I18n.__('devSessionsColDevices'))}</th>
                        <th>${this.escapeHtml(I18n.__('devSessionsColAction'))}</th>
                    </tr></thead>
                    <tbody>${rows || `<tr><td colspan="7">${this.escapeHtml(I18n.__('devSessionsEmpty'))}</td></tr>`}</tbody>
                </table>
            </section>`;
    },

    /** The raw appended history: security events, newest first, with the trace ids. */
    devAuditHtml() {
        const events = (this._devConsole.audit || {}).events || [];
        const rows = events.map((event) => `<tr>
                <td>${this.escapeHtml(String(event.created_at || ''))}</td>
                <td>${this.escapeHtml(String(event.actor_id || ''))}</td>
                <td>${this.escapeHtml(this.roleLabel(event.actor_role || ''))}</td>
                <td>${this.escapeHtml(String(event.action || ''))}</td>
                <td>${this.escapeHtml([event.entity, event.entity_id].filter(Boolean).join(' '))}</td>
            </tr>`).join('');
        return `
            <section class="ui-stack" data-dev-section="audit">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devAudit'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devAuditHint'))}</p>
                <table class="ui-table">
                    <thead><tr>
                        <th>${this.escapeHtml(I18n.__('devColWhen'))}</th>
                        <th>${this.escapeHtml(I18n.__('devColActor'))}</th>
                        <th>${this.escapeHtml(I18n.__('role'))}</th>
                        <th>${this.escapeHtml(I18n.__('devColAction'))}</th>
                        <th>${this.escapeHtml(I18n.__('devColEntity'))}</th>
                    </tr></thead>
                    <tbody>${rows || `<tr><td colspan="5">${this.escapeHtml(I18n.__('devAuditEmpty'))}</td></tr>`}</tbody>
                </table>
                <button type="button" class="ui-btn" data-dev-audit-reload="true">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('devAuditReload'))}</button>
            </section>`;
    },

    /** Bytes as a person reads them, the way the photo picker already does it. */
    devBytes(bytes) {
        const size = Math.max(0, Number(bytes) || 0);
        if (size < 1024) return `${size} B`;
        if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
        if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
        return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`;
    },

    /**
     * The backup directory: what is in it, and the one button that adds to it.
     *
     * WHY A BUTTON, AND NOT ONLY A LIST
     * --------------------------------
     * ``tools/backup.py`` has been this project's snapshot tool all along, run by hand from a
     * shell before a migration. The person who decides at 03:00 that a change can wait no longer
     * is the person looking at this tab, and "open a terminal on the server" is where a backup
     * does not get taken. The button runs that tool: the database through ``VACUUM INTO`` (atomic,
     * and correct while the server is running), the source tree, the templates, the interpreter's
     * own freeze, and a manifest of every file written beside them.
     *
     * WRITTEN AND INTACT ARE TWO QUESTIONS
     * -----------------------------------
     * A row and a directory is one of them; the verdict is the other, and the Verify button is
     * what re-asks it. The list reports the verdict each snapshot recorded when it was *written* -
     * re-hashing every file on every paint is not something a tab is allowed to do - so Verify is
     * the deliberate act that re-reads the bytes, and its answer is printed where it cannot be
     * mistaken for the stored one. A snapshot whose verdict is FAIL is shown rather than hidden:
     * the bytes may still be recoverable by somebody who knows SQLite, and it is exactly the row
     * the operator must not have to go looking for.
     */
    devBackupsHtml() {
        const data = this._devConsole.backups || {};
        const items = data.items || [];
        const directory = String(data.directory || '');
        const newest = data.newest || null;
        const age = newest && typeof newest.age_hours === 'number' ? newest.age_hours : null;
        const rows = items.map((item) => {
            const kind = String(item.kind || '');
            const status = String(item.status || '');
            const tone = status === 'PASS' ? ' is-ok'
                : (status === 'FAIL' ? ' is-danger' : (kind === 'unverified' ? ' is-warn' : ''));
            const label = kind === 'snapshot' ? I18n.__('devBackupsKindSnapshot')
                : (kind === 'database' ? I18n.__('devBackupsKindDatabase') : I18n.__('devBackupsKindUnverified'));
            const notes = (item.notes || []).filter(Boolean).join(' ');
            const files = item.files === null || item.files === undefined ? '\u2014' : String(item.files);
            return `
                <tr data-dev-backup="${this.escapeHtml(String(item.name || ''))}" data-dev-backup-kind="${this.escapeHtml(kind)}">
                    <td>
                        <span class="ops-name">${this.escapeHtml(String(item.name || ''))}</span>
                        ${notes ? `<span class="ops-sub">${this.escapeHtml(notes)}</span>` : ''}
                    </td>
                    <td><span class="ui-badge${tone}">${this.escapeHtml(label)}</span></td>
                    <td>${this.escapeHtml(String(item.created_at || '\u2014'))}</td>
                    <td>${this.escapeHtml(this.devBytes(item.size_bytes))}</td>
                    <td>${status ? `<span class="ui-badge${tone}">${this.escapeHtml(status)}</span>` : this.escapeHtml('\u2014')}</td>
                    <td>${this.escapeHtml(files)}</td>
                    <td>${item.verifiable
                        ? `<button type="button" class="ui-btn ui-btn-sm" data-dev-backup-verify="${this.escapeHtml(String(item.name || ''))}">${this.escapeHtml(I18n.__('devBackupsVerify'))}</button>`
                        : `<span class="ops-sub">${this.escapeHtml(I18n.__('devBackupsNoManifest'))}</span>`}</td>
                </tr>`;
        }).join('');
        // A backup surface that never says "the newest one is a fortnight old" is a surface that
        // reports a habit, not a state: the failure an operator actually has is not a corrupt
        // snapshot, it is not taking one.
        const stale = age !== null && age >= 24
            ? `<p class="ui-note is-warn" data-dev-backup-stale="true">${this.OPS_ICONS.alert}${this.escapeHtml(I18n.__('devBackupsStale').replace('{hours}', String(Math.round(age))))}</p>`
            : '';
        const empty = data.exists === false
            ? `<p class="ui-empty" data-dev-backups-empty="true">${this.escapeHtml(I18n.__('devBackupsNoDirectory'))}</p>`
            : `<p class="ui-empty" data-dev-backups-empty="true">${this.escapeHtml(I18n.__('devBackupsEmpty'))}</p>`;
        return `
            <section class="ui-stack" data-dev-section="backups">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devBackups'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devBackupsHint'))}</p>
                <p class="ops-sub" data-dev-backups-dir="${this.escapeHtml(directory)}">${this.escapeHtml(I18n.__('devBackupsDirectory').replace('{dir}', directory))}</p>
                <p class="ops-sub">${this.escapeHtml(I18n.__('devBackupsCounts')
                    .replace('{snapshots}', String(data.snapshots || 0))
                    .replace('{databases}', String(data.databases || 0))
                    .replace('{unverified}', String(data.unverified || 0))
                    .replace('{bytes}', this.devBytes(data.total_bytes))) }</p>
                ${stale}
                <div class="ui-row">
                    <button type="button" class="ui-btn ui-btn-primary" data-dev-backup-now="true">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('devBackupsNow'))}</button>
                    <label class="ui-label" for="devBackupAssets"><input type="checkbox" id="devBackupAssets" data-dev-backup-assets="true" checked> ${this.escapeHtml(I18n.__('devBackupsAssets'))}</label>
                </div>
                <p class="ui-note">${this.escapeHtml(I18n.__('devBackupsNowHint'))}</p>
                <table class="ui-table">
                    <thead><tr>
                        <th>${this.escapeHtml(I18n.__('devBackupsColName'))}</th>
                        <th>${this.escapeHtml(I18n.__('devBackupsColKind'))}</th>
                        <th>${this.escapeHtml(I18n.__('devColWhen'))}</th>
                        <th>${this.escapeHtml(I18n.__('devBackupsColSize'))}</th>
                        <th>${this.escapeHtml(I18n.__('devBackupsColStatus'))}</th>
                        <th>${this.escapeHtml(I18n.__('devBackupsColFiles'))}</th>
                        <th>${this.escapeHtml(I18n.__('devSessionsColAction'))}</th>
                    </tr></thead>
                    <tbody>${rows || `<tr><td colspan="7">${empty}</td></tr>`}</tbody>
                </table>
                <p class="ui-note">${this.escapeHtml(I18n.__('devBackupsVerifyHint'))}</p>
                <pre class="ui-note" data-dev-backup-report="true"></pre>
            </section>`;
    },

    /**
     * Take a snapshot, from this tab.
     *
     * The button says so while it waits, because this is the one control here that takes seconds
     * to minutes: a full snapshot copies the source tree and every enrolled face and hashes all
     * of it, and on the one vCPU this deployment is sized for that is the heaviest thing the
     * process can be asked to do. A button that looked idle through that would be clicked again,
     * which is exactly what the server's own lock refuses.
     *
     * The verdict picks the message. "Written" is not "intact": a snapshot that failed its own
     * check raises a critical alert on the server and reads as an error here, because an operator
     * who is told "done" about a copy they cannot restore from is worse off than one who is told
     * nothing.
     */
    async takeDevSnapshot() {
        const box = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-backup-assets]') : null;
        const includeAssets = box ? !!box.checked : true;
        const button = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-backup-now]') : null;
        if (button) {
            button.disabled = true;
            button.textContent = I18n.__('devBackupsWorking');
        }
        try {
            const data = await API.request('/developer/db/snapshot', {
                method: 'POST',
                body: { include_assets: includeAssets }
            });
            const name = String((data && data.name) || '');
            const files = String((data && data.files) || 0);
            if (String((data && data.verified) || '') === 'PASS') {
                Toast.success(I18n.__('devBackupsCreated').replace('{name}', name).replace('{files}', files));
            } else {
                Toast.error(I18n.__('devBackupsUnverified').replace('{name}', name));
            }
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
            if (button) {
                button.disabled = false;
                button.textContent = I18n.__('devBackupsNow');
            }
        }
    },

    /**
     * Re-hash one snapshot and print what the check found.
     *
     * No repaint on purpose. A repaint re-reads the list, and the list reports the verdict
     * recorded when the snapshot was written - which is still the old one after a verification
     * disproves it (the server does not rewrite ``VERIFY.json``; a verification is a *read*).
     * Redrawing the row from the stored verdict would quietly undo the answer the operator just
     * asked for, so the answer goes into the report line and stays there until they reload.
     */
    async verifyDevBackup(name) {
        const target = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-backup-report]') : null;
        try {
            const report = await API.request(`/developer/db/backups/${encodeURIComponent(name)}/verify`, { method: 'POST' });
            const status = String((report && report.status) || '');
            const errors = (report && report.errors) || [];
            const line = I18n.__('devBackupsVerifyResult')
                .replace('{name}', name)
                .replace('{status}', status)
                .replace('{checked}', String((report && report.files_checked) || 0))
                .replace('{listed}', String((report && report.files_listed) || 0));
            if (target) target.textContent = [line].concat(errors).join('\n');
            if (status === 'PASS') Toast.success(line);
            else Toast.error(line);
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /**
     * Bind every control the panel drew, by its ``data-`` hook.
     *
     * One pass after the paint, the way ``bindAlertControls`` does it: a repaint
     * between paint and tap cannot orphan a handler, and no inline ``onclick``
     * joins the ones the document policy already tolerates. Guarded so a stub DOM
     * without ``querySelectorAll`` - which every frontend suite runs against -
     * can render the panel without throwing.
     */
    bindDeveloperControls(content) {
        if (!content || typeof content.querySelectorAll !== 'function') return;
        const on = (selector, attribute, handler) => {
            content.querySelectorAll(selector).forEach((node) => {
                if (typeof node.addEventListener !== 'function') return;
                node.addEventListener('click', () => handler(node.getAttribute(attribute)));
            });
        };
        on('[data-runtime-save]', 'data-runtime-save', (key) => this.saveRuntimeValue(key));
        on('[data-dev-session-revoke]', 'data-dev-session-revoke', (id) => this.revokeDevSession(id));
        on('[data-dev-alert-read]', 'data-dev-alert-read', (id) => this.markDevAlertRead(id));
        on('[data-dev-explain]', 'data-dev-explain', (name) => this.explainDevQuery(name));
        on('[data-dev-flush]', 'data-dev-flush', () => this.flushDevCaches());
        on('[data-dev-backup-now]', 'data-dev-backup-now', () => this.takeDevSnapshot());
        on('[data-dev-backup-verify]', 'data-dev-backup-verify', (name) => this.verifyDevBackup(name));
        on('[data-dev-audit-reload]', 'data-dev-audit-reload', () => UI.renderAdminTab('Developer'));
        on('[data-dev-liveness-apply]', 'data-dev-liveness-apply', () => this.moveDevLivenessMode());
        on('[data-dev-shadow-evaluate]', 'data-dev-shadow-evaluate', () => this.evaluateDevShadowGate());
        on('[data-dev-checkpoint]', 'data-dev-checkpoint', () => this.checkpointDevWal());
        on('[data-dev-integrity]', 'data-dev-integrity', () => this.runDevIntegrityCheck());
        on('[data-dev-engine-restart]', 'data-dev-engine-restart', () => this.restartDevEngine());
        on('[data-dev-tamper-filter]', 'data-dev-tamper-filter', () => this.filterDevTamperAlerts());
    },

    /** Change one runtime value. The note is optional; the audit row is not. */
    async saveRuntimeValue(key) {
        const input = typeof document.querySelector === 'function'
            ? document.querySelector(`[data-runtime-input="${key}"]`) : null;
        if (!input) return;
        const noteField = typeof document.querySelector === 'function'
            ? document.querySelector(`[data-runtime-note="${key}"]`) : null;
        let value = String(input.value);
        if (value === 'true') value = true;
        else if (value === 'false') value = false;
        try {
            await API.request(`/developer/runtime/${encodeURIComponent(key)}`, {
                method: 'PATCH',
                body: { value, note: noteField && String(noteField.value || '').trim() || null }
            });
            Toast.success(I18n.__('devRuntimeSaved')
                .replace('{key}', key).replace('{value}', String(value)));
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /**
     * Revoke one account's sessions.
     *
     * Confirm in the reader's own words, because the lever is quiet and permanent for the
     * holder: every signed-in phone and browser of that account is signed out at its next
     * request. Revoking *your own* sessions says so before the confirm, since this tab will
     * not outlive the request that re-renders it.
     */
    async revokeDevSession(userId) {
        const sessions = this._devConsole.sessions || [];
        const target = sessions.find((session) => String(session.id) === String(userId)) || {};
        const who = [target.name, userId].filter(Boolean).join(' · ');
        const selfWarning = String(userId) === String((State.user || {}).id)
            ? ` ${I18n.__('devSessionsSelfWarning')}` : '';
        if (!confirm(`${I18n.__('devSessionsConfirm').replace('{who}', who)}${selfWarning}`)) return;
        try {
            await API.request(`/developer/sessions/${encodeURIComponent(userId)}/revoke`, { method: 'POST' });
            Toast.success(I18n.__('devSessionsRevoked').replace('{who}', who));
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /** Acknowledge one alert in the private hub so the unread count falls. */
    async markDevAlertRead(alertId) {
        try {
            await API.request(`/developer/alerts/${encodeURIComponent(alertId)}/read`, { method: 'POST' });
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /** Ask SQLite for one named query's plan, and write it under the buttons. */
    async explainDevQuery(name) {
        const target = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-plan]') : null;
        try {
            const data = await API.request(`/developer/diagnostics/query-plan/${encodeURIComponent(name)}`);
            const plan = (data && data.plan) || [];
            if (target) {
                target.textContent = `${I18n.__('devExplainResult').replace('{name}', name)}\n${plan.join('\n')}`;
            }
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /** Drop the caches the deployment really has, and say how many went. */
    async flushDevCaches() {
        try {
            const data = await API.request('/developer/diagnostics/caches/flush', { method: 'POST' });
            const count = Array.isArray(data && data.flushed) ? data.flushed.length : 0;
            Toast.success(I18n.__('devFlushed').replace('{count}', String(count)));
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    // -----------------------------------------------------------------
    //  The diagnostic domains: the models, the database, the engine child
    //  and the offline protocol's own forensics
    // -----------------------------------------------------------------
    //
    // Four domains that until now were reachable only with curl. They follow the three rules
    // the panels above do, and one of them needs stating because these panels differ:
    //
    // 1. **A field name is the server's own word for the fact.** The facts grids and the
    //    forensic rows print ``Stats.as_dict()``'s keys, the pragmas' and the payload's own
    //    field names rather than a label invented here - a panel that renamed
    //    ``frames_checkpointed`` would be a second vocabulary for one number, and the first
    //    thing to drift from the payload it describes. Only the chrome an operator reads as a
    //    sentence is translated; the write-path panel above already prints its counters this
    //    way.
    // 2. **No inline handler** - ``bindDeveloperControls`` binds these controls too, by their
    //    ``data-`` hooks.
    // 3. **A failed read is a sentence, not a blank panel** - ``uiErrorHtml``, once for the
    //    whole console, which is why one dead route takes the tab rather than one panel.
    //
    // What is genuinely different is that two of these reads are *acts* rather than
    // observations: ``PRAGMA integrity_check`` walks every page of the database, seconds on a
    // large one, and a checkpoint blocks writers while it copies the live frames. Neither
    // belongs in a tab paint, so both are buttons - the judgement the backups panel's Verify
    // lever already makes.

    /** Scalars only, in the server's own order: a nested object is not a fact you can print. */
    devFactPairs(value) {
        return Object.entries(value || {}).filter(
            ([, item]) => item !== undefined && (item === null || typeof item !== 'object')
        );
    },

    /**
     * One facts grid, under the server's own field names.
     *
     * ``keys`` picks and orders them where a payload's own order is not the order an operator
     * reads in; ``value`` may also be an array of pairs, where a fact has to be shaped first (a
     * byte count as a person reads it, a salt that arrives masked).
     */
    devFactsHtml(value, columns, keys) {
        const source = value || {};
        const pairs = Array.isArray(source)
            ? source.filter((pair) => pair[1] !== undefined)
            : (keys
                ? keys.filter((key) => source[key] !== undefined).map((key) => [key, source[key]])
                : this.devFactPairs(source));
        const cells = pairs.map(([key, item]) =>
            `<span class="ui-fact"><span class="ui-fact-sub">${this.escapeHtml(String(key))}</span>` +
            `<span class="ui-fact-value">${this.escapeHtml(this.devFactValue(item))}</span></span>`
        ).join('');
        if (!cells) return '';
        return `<div class="ui-grid ${columns || 'three'}">${cells}</div>`;
    },

    /** A value as a reader takes it: a dash for nothing, a word for a flag, else the string. */
    devFactValue(value) {
        if (value === null || value === undefined) return '\u2014';
        if (typeof value === 'boolean') return value ? 'true' : 'false';
        return String(value);
    },

    /**
     * The models: what this build scores with, and the policy it scores under.
     *
     * Five answers an operator needs in one place during an incident, each from the module
     * that owns it: the queue (is the engine saturated), the decision lines (what a punch is
     * decided by, and whether a line was derived or typed), the detector and its crop (is the
     * pipeline the intended one), the liveness policy (enforce, and overridden to what), and
     * the shadow migration's paired log.
     *
     * The mode control is a select built from ``liveness_modes``, which the server computes
     * from ``liveness.MODES``: the vocabulary has one owner and this draws it rather than
     * restating three words in a fourth place. The reason is required by the endpoint, so the
     * box says so *before* the refusal rather than leaving an operator to find it in a 400.
     */
    devMlHtml() {
        const data = this._devConsole.ml || {};
        const engine = data.engine || {};
        const detector = data.detector || {};
        const liveness = data.liveness || {};
        const active = data.active_band || null;
        const activeModel = active ? String(active.model || '') : '';
        const modes = data.liveness_modes || [];
        const current = String(liveness.mode || '');
        const options = modes.map((mode) =>
            `<option value="${this.escapeHtml(mode)}"${mode === current ? ' selected' : ''}>${this.escapeHtml(mode)}</option>`
        ).join('');
        // Every band in the table, with the one the live pipeline decides by marked: a band
        // nobody is scoring with is still worth reading (it is what a flip would move to), but
        // the live one has to be findable without comparing model ids by eye.
        const bands = Object.keys(data.bands || {}).sort().map((name) => {
            const band = data.bands[name] || {};
            const isActive = !!activeModel && String(band.model || '') === activeModel;
            return `
                <div class="ui-card" data-dev-band="${this.escapeHtml(name)}" data-dev-band-active="${isActive ? '1' : '0'}">
                    <div class="ops-row-main">
                        <span class="ops-name">${this.escapeHtml(name)}</span>
                        <span class="ops-sub">${this.escapeHtml(String(band.model || ''))}</span>
                    </div>
                    ${this.devFactsHtml(band, 'three', ['approve', 'review', 'derived_approve', 'derived_review'])}
                    ${band.one_line ? '' : `<p class="ui-note is-warn">${this.escapeHtml(I18n.__('devMlTwoLines'))}</p>`}
                    ${band.basis ? `<p class="ui-note">${this.escapeHtml(String(band.basis))}</p>` : ''}
                </div>`;
        }).join('');
        return `
            <section class="ui-stack" data-dev-section="ml">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devMl'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devMlHint'))}</p>
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devMlQueue'))}</h3>
                ${this.devFactsHtml(engine)}
                <p class="ops-sub" data-dev-active-band="${this.escapeHtml(activeModel)}">${this.escapeHtml(
                    active
                        ? I18n.__('devMlActiveBand').replace('{model}', activeModel)
                        : I18n.__('devMlNoBand')
                )}</p>
                ${active ? '' : `<p class="ui-note is-warn" data-dev-band-error="true">${this.escapeHtml(String(data.active_band_error || ''))}</p>`}
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devMlBands'))}</h3>
                <p class="ui-note">${this.escapeHtml(I18n.__('devMlBandsHint'))}</p>
                ${bands}
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devMlDetector'))}</h3>
                ${this.devFactsHtml(detector, 'three', ['available', 'detector', 'pipeline', 'model_present', 'model_fingerprint'])}
                ${this.devFactsHtml(detector.crop)}
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devMlLiveness'))}</h3>
                <p class="ui-note">${this.escapeHtml(I18n.__('devMlLivenessHint'))}</p>
                ${this.devFactsHtml([
                    ['mode', liveness.mode],
                    ['configured_mode', liveness.configured_mode],
                    ['override', liveness.override],
                    ['liveness_override', data.liveness_override],
                    ['available', liveness.available],
                    ['input_size', liveness.input_size],
                    ['accept_threshold', liveness.accept_threshold],
                    ['reject_threshold', liveness.reject_threshold]
                ])}
                <div class="ui-row">
                    <select class="ui-field" data-dev-liveness-mode="true">${options}</select>
                    <input class="ui-field" data-dev-liveness-reason="true" placeholder="${this.escapeHtml(I18n.__('devMlLivenessReason'))}">
                    <button type="button" class="ui-btn" data-dev-liveness-apply="true">${this.escapeHtml(I18n.__('devMlLivenessApply'))}</button>
                </div>
                ${this.devShadowHtml()}
            </section>`;
    },

    /**
     * The paired log and the cutover gate's verdict on it.
     *
     * ``flip_ready`` is three-valued and the panel must not flatten it: ``true`` is ready,
     * ``false`` is *not* ready, and ``null`` is **not evaluated** - no gallery was named, so the
     * coverage the gate is measured against is unknowable from here. The last two call for
     * opposite next steps ("wait for the backfill" against "name the cache and ask again"), and
     * a panel that showed both as "not ready" would leave an operator waiting on a measurement
     * nobody had run. The reason the server sent is printed either way, because it is the only
     * thing that says which of the two it is.
     */
    devShadowHtml() {
        const data = this._devConsole.shadow || {};
        const summary = data.summary || {};
        const paired = data.paired || {};
        const gate = data.gate || {};
        const configured = data.configured || {};
        const ready = data.flip_ready;
        const tone = ready === true ? ' is-ok' : (ready === false ? ' is-danger' : ' is-warn');
        const word = ready === true ? I18n.__('devMlShadowReady')
            : (ready === false ? I18n.__('devMlShadowNotReady') : I18n.__('devMlShadowUnevaluated'));
        return `
            <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devMlShadow'))}</h3>
            <p class="ui-note">${this.escapeHtml(I18n.__('devMlShadowHint'))}</p>
            ${this.devFactsHtml([
                ['started', data.started],
                ['log', configured.log],
                ['log_present', configured.log_present],
                ['shadow_model_present', configured.shadow_model_present],
                ['contract', configured.contract]
            ])}
            ${data.started ? this.devFactsHtml([
                ['events', summary.events],
                ['paired_samples', summary.paired_samples],
                ['error_rate', summary.error_rate],
                ['verdict_agreement', summary.verdict_agreement],
                ['agreements', paired.agreements],
                ['disagreements', paired.disagreements],
                ['coverage', data.coverage],
                ['min_coverage', gate.min_coverage],
                ['min_paired_samples', gate.min_paired_samples],
                ['max_error_rate', gate.max_error_rate]
            ]) : `<p class="ui-note is-warn" data-dev-shadow-idle="true">${this.escapeHtml(I18n.__('devMlShadowNoLog'))}</p>`}
            <p class="ui-note"><span class="ui-badge${tone}" data-dev-shadow-gate="${String(ready)}">${this.escapeHtml(word)}</span></p>
            ${data.reason ? `<p class="ui-note" data-dev-shadow-reason="true">${this.escapeHtml(String(data.reason))}</p>` : ''}
            <div class="ui-row">
                <input class="ui-field" data-dev-shadow-gallery="true" placeholder="${this.escapeHtml(I18n.__('devMlShadowGallery'))}">
                <button type="button" class="ui-btn" data-dev-shadow-evaluate="true">${this.escapeHtml(I18n.__('devMlShadowEvaluate'))}</button>
            </div>
            <pre class="ui-note" data-dev-shadow-report="true"></pre>`;
    },

    /**
     * The database: contention, the journal, the schema, and the one check that reads it all.
     *
     * The checkpoint select is drawn from ``checkpoint_modes`` - the same tuple
     * ``db_wal_checkpoint`` validates against - so the control cannot offer a mode the endpoint
     * would answer 400 to. The integrity check is a *button* because it is a full page-level
     * read of the database, and this is a tab paint; its answer goes into the report line and is
     * never folded back into the panel, because "found nothing" and "was never run" must not
     * look the same.
     */
    devDatabaseHtml() {
        const data = this._devConsole.db || {};
        const wal = data.wal || {};
        const modes = data.checkpoint_modes || [];
        const options = modes.map((mode) =>
            `<option value="${this.escapeHtml(mode)}"${mode === 'TRUNCATE' ? ' selected' : ''}>${this.escapeHtml(mode)}</option>`
        ).join('');
        return `
            <section class="ui-stack" data-dev-section="database">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devDb'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devDbHint'))}</p>
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devDbCounters'))}</h3>
                ${this.devFactsHtml(data.counters)}
                ${data.scope ? `<p class="ops-sub" data-dev-db-scope="true">${this.escapeHtml(String(data.scope))}</p>` : ''}
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devDbWal'))}</h3>
                ${this.devFactsHtml(wal, 'three', ['pages_in_log', 'bytes_in_log', 'frames_checkpointed', 'busy', 'report_is_a_passive_checkpoint'])}
                <p class="ui-note">${this.escapeHtml(I18n.__('devDbWalNote'))}</p>
                <div class="ui-row">
                    <select class="ui-field" data-dev-checkpoint-mode="true">${options}</select>
                    <button type="button" class="ui-btn" data-dev-checkpoint="true">${this.escapeHtml(I18n.__('devDbCheckpoint'))}</button>
                </div>
                <p class="ui-note">${this.escapeHtml(I18n.__('devDbCheckpointHint'))}</p>
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devDbPragmas'))}</h3>
                ${this.devFactsHtml(data.pragmas)}
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devDbIntegrity'))}</h3>
                <p class="ui-note">${this.escapeHtml(I18n.__('devDbIntegrityHint'))}</p>
                <button type="button" class="ui-btn" data-dev-integrity="true">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('devDbIntegrityRun'))}</button>
                <pre class="ui-note" data-dev-integrity-report="true"></pre>
            </section>`;
    },

    /**
     * The model child: whether one exists, what it holds, and the lever that replaces it.
     *
     * The restart button is drawn only when the deployment actually runs a separate process:
     * with ``FACE_ENGINE_PROCESS`` off the endpoint answers 409, and a button whose only answer
     * is a refusal is a trap - the same rule the force-in roster and the snapshot's Verify
     * lever follow. When no child has been needed yet there is nothing to report *and* the
     * lever still stands: the operator's intent ("have a healthy model process") is the same
     * either way, and the server starts one rather than refusing.
     *
     * The state word is three-valued for the same reason the shadow gate's is: "no child has
     * been needed" and "the child is not running" are different states, and the second is the
     * one a gate is failing under.
     */
    devEngineHtml() {
        const data = this._devConsole.engine || {};
        const enabled = data.enabled !== false;
        const started = data.started === true;
        const alive = data.alive === true;
        const state = enabled ? (started ? (alive ? 'alive' : 'dead') : 'not_started') : 'in_process';
        const line = I18n.__({
            in_process: 'devEngineInProcess',
            not_started: 'devEngineNotStarted',
            alive: 'devEngineAlive',
            dead: 'devEngineDead'
        }[state]);
        return `
            <section class="ui-stack" data-dev-section="engine">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devEngine'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devEngineHint'))}</p>
                <p class="ops-sub" data-dev-engine-state="${state}">${this.escapeHtml(line)}</p>
                ${data.message ? `<p class="ui-note" data-dev-engine-message="true">${this.escapeHtml(String(data.message))}</p>` : ''}
                ${this.devFactsHtml([
                    ['enabled', data.enabled],
                    ['started', data.started],
                    ['alive', data.alive],
                    ['name', data.name],
                    ['handler', data.handler],
                    ['pid', data.pid],
                    ['worker_pid', data.worker_pid],
                    ['interpreter_pid', data.interpreter_pid],
                    ['starts', data.starts],
                    ['calls', data.calls],
                    ['failures', data.failures],
                    ['timeouts', data.timeouts],
                    ['in_flight', data.in_flight],
                    ['last_call_ms', data.last_call_ms],
                    ['deadline_seconds', data.deadline_seconds],
                    ['last_error', data.last_error],
                    ['ping_ms', data.ping_ms],
                    ['ping_error', data.ping_error],
                    ['rss_bytes', data.rss_bytes === null || data.rss_bytes === undefined
                        ? null : this.devBytes(data.rss_bytes)]
                ])}
                ${data.ping_note ? `<p class="ui-note is-warn" data-dev-engine-ping-note="true">${this.escapeHtml(String(data.ping_note))}</p>` : ''}
                ${enabled ? '' : this.devFactsHtml(data.engine)}
                ${enabled ? `<button type="button" class="ui-btn" data-dev-engine-restart="true">${this.OPS_ICONS.refresh}${this.escapeHtml(I18n.__('devEngineRestart'))}</button>
                <p class="ui-note">${this.escapeHtml(I18n.__('devEngineRestartHint'))}</p>` : ''}
            </section>`;
    },

    /**
     * Offline forensics: the devices that sign, and the refusals that were about a clock.
     *
     * Two ledgers answering different questions. The device ledger is the *trust* half - which
     * device, whose, signed with which key epoch, and whether it was revoked - and the tamper
     * list is the *time* half: a monotonic offset that disagrees with the anchor the device was
     * issued, a signature that does not verify, a nonce replayed twice. Neither is a queue to
     * approve: a rejected offline punch was never materialised as attendance.
     *
     * The filter is the one action, because the question an operator arrives with is almost
     * always about one worker ("what is that phone doing"), and the endpoint already takes the
     * id. It narrows the list *in place* rather than repainting the tab: a repaint would re-read
     * every other route on the tab for one question, and, worse, would redraw the box empty -
     * losing the very thing the operator just typed. The salt arrives masked and is printed
     * exactly as the server sent it: a second copy of key material, kept here for convenience,
     * would only be as safe as this panel.
     */
    devOfflineHtml() {
        const data = this._devConsole.offline || {};
        const anchors = data.anchors || {};
        const rows = (data.devices || []).map((device) => {
            const revoked = !!device.revoked_at;
            const salt = device.key_salt || {};
            return `
                <div class="ui-card${revoked ? ' is-warn' : ''}" data-dev-device="${this.escapeHtml(String(device.device_id || ''))}" data-dev-device-revoked="${revoked ? '1' : '0'}">
                    <div class="ui-spread">
                        <div class="ops-row-main">
                            <span class="ops-name">${this.escapeHtml(String(device.device_id || ''))}</span>
                            <span class="ops-sub">${this.escapeHtml([String(device.worker_id || ''), device.worker_name || '', device.worker_role || ''].filter(Boolean).join(' \u00b7 '))}</span>
                        </div>
                        ${revoked ? `<span class="ui-badge is-warn">${this.escapeHtml(I18n.__('devOfflineRevoked'))}</span>` : ''}
                    </div>
                    ${this.devFactsHtml([
                        ['key_epoch', device.key_epoch],
                        ['key_salt', salt.prefix ? `${salt.prefix}\u2026 (${salt.length})` : null],
                        ['created_at', device.created_at],
                        ['last_seen_at', device.last_seen_at],
                        ['last_anchor_at', device.last_anchor_at],
                        ['revoked_at', device.revoked_at]
                    ])}
                </div>`;
        }).join('');
        return `
            <section class="ui-stack" data-dev-section="offline">
                <h2 class="ui-section-title">${this.escapeHtml(I18n.__('devOffline'))}</h2>
                <p class="ui-note">${this.escapeHtml(I18n.__('devOfflineHint'))}</p>
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devOfflineDevices'))}</h3>
                <p class="ui-note">${this.escapeHtml(I18n.__('devOfflineDevicesHint'))}</p>
                <p class="ops-sub" data-dev-offline-anchors="true">${this.escapeHtml(I18n.__('devOfflineAnchors')
                    .replace('{issued}', String(anchors.issued ?? 0))
                    .replace('{consumed}', String(anchors.consumed ?? 0))
                    .replace('{unconsumed}', String(anchors.unconsumed ?? 0)))}
                 ${this.escapeHtml(I18n.__('devOfflineWindow').replace('{hours}', String(data.window_hours ?? '')))}</p>
                ${rows || `<p class="ui-empty">${this.escapeHtml(I18n.__('devOfflineEmpty'))}</p>`}
                <h3 class="ui-section-title">${this.escapeHtml(I18n.__('devOfflineTamper'))}</h3>
                <p class="ui-note">${this.escapeHtml(I18n.__('devOfflineTamperHint'))}</p>
                <div data-dev-tamper-block="true">${this.devTamperHtml()}</div>
                <div class="ui-row">
                    <input class="ui-field" data-dev-tamper-worker="true" placeholder="${this.escapeHtml(I18n.__('devOfflineFilter'))}">
                    <button type="button" class="ui-btn" data-dev-tamper-filter="true">${this.escapeHtml(I18n.__('devOfflineFilterApply'))}</button>
                </div>
            </section>`;
    },

    /**
     * The refusal ledger, in a block of its own so the filter can replace just this.
     *
     * The wrapper is what ``filterDevTamperAlerts`` rewrites: the counts, the rows and the empty
     * state are one answer to one question, and the filter control sits outside it - a
     * re-rendered button would be an unbound button, since the delegated binding ran once, at
     * paint time.
     */
    devTamperHtml() {
        const tamper = this._devConsole.tamper || {};
        const counts = tamper.counts || {};
        const rows = (tamper.alerts || []).map((alert) => {
            const flag = alert.tamper === true;
            const skew = typeof alert.skew_seconds === 'number' ? Math.abs(alert.skew_seconds) : null;
            const tone = flag ? ' is-danger' : '';
            return `
                <div class="ui-card${tone}" data-dev-tamper="${this.escapeHtml(String(alert.client_punch_id || ''))}" data-dev-tamper-flag="${flag ? '1' : '0'}">
                    <div class="ui-spread">
                        <div class="ops-row-main">
                            <span class="ui-badge${tone}">${this.escapeHtml(String(alert.rejection_code || alert.status || ''))}</span>
                            <div class="ops-who">
                                <span class="ops-name">${this.escapeHtml(String(alert.client_punch_id || ''))}</span>
                                <span class="ops-sub">${this.escapeHtml([String(alert.worker_id || ''), alert.worker_name || '', alert.action || ''].filter(Boolean).join(' \u00b7 '))}</span>
                            </div>
                        </div>
                        ${skew === null ? '' : `<span class="ui-badge${skew >= 3600 ? ' is-danger' : ''}" data-dev-tamper-skew="${this.escapeHtml(String(skew))}">${this.escapeHtml(`skew ${skew}s`)}</span>`}
                    </div>
                    ${this.devFactsHtml([
                        ['device_id', alert.device_id],
                        ['client_timestamp', alert.client_timestamp],
                        ['anchor_server_time', alert.anchor_server_time],
                        ['effective_time', alert.effective_time],
                        ['skew_seconds', alert.skew_seconds],
                        ['monotonic_offset_s', alert.monotonic_offset_s],
                        ['signature_version', alert.signature_version],
                        ['received_at', alert.received_at]
                    ])}
                    ${alert.flag_reason ? `<p class="ui-note">${this.escapeHtml(String(alert.flag_reason))}</p>` : ''}
                </div>`;
        }).join('');
        return `
                <p class="ops-sub" data-dev-tamper-counts="true">${this.escapeHtml(I18n.__('devOfflineTamperCounts')
                    .replace('{inWindow}', String(counts.in_window ?? 0))
                    .replace('{tamper}', String(counts.tamper ?? 0))
                    .replace('{anchored}', String(counts.with_an_anchor ?? 0)))}
                 ${this.escapeHtml(String((tamper.tamper_codes || []).join(', ')))}</p>
                ${rows || `<p class="ui-empty">${this.escapeHtml(I18n.__('devOfflineEmpty'))}</p>`}`;
    },


    /**
     * Move the liveness policy, with the reason the audit row is read for.
     *
     * The reason is not optional and is not defaulted here: ``set_liveness_mode`` refuses an
     * empty one, and inventing a sentence on the operator's behalf would put words in the audit
     * trail that nobody wrote. A refusal is a toast, not a silent no-op.
     */
    async moveDevLivenessMode() {
        const box = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-liveness-mode]') : null;
        const field = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-liveness-reason]') : null;
        if (!box) return;
        const mode = String(box.value || '');
        const reason = field ? String(field.value || '').trim() : '';
        try {
            const data = await API.request('/developer/ml/liveness-mode', {
                method: 'POST',
                body: { mode: mode, reason: reason }
            });
            Toast.success(I18n.__('devMlLivenessMoved')
                .replace('{previous}', String((data && data.previous) || '\u2014'))
                .replace('{mode}', String((data && data.mode) || mode)));
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /**
     * Ask the server to re-run the cutover gate against a named gallery.
     *
     * No repaint, for the reason ``verifyDevBackup`` gives: a repaint would replace the answer
     * the operator just asked for with the one from the first paint - the "not evaluated" they
     * stopped relying on. The line goes into its own report, where it cannot be mistaken for
     * what the paint said.
     */
    async evaluateDevShadowGate() {
        const field = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-shadow-gallery]') : null;
        const target = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-shadow-report]') : null;
        const gallery = field ? String(field.value || '').trim() : '';
        const query = gallery ? `?gallery=${encodeURIComponent(gallery)}` : '';
        try {
            const data = await API.request(`/developer/ml/shadow-summary${query}`);
            const ready = data && data.flip_ready;
            const word = ready === true ? I18n.__('devMlShadowReady')
                : (ready === false ? I18n.__('devMlShadowNotReady') : I18n.__('devMlShadowUnevaluated'));
            const paired = (data && data.paired) || {};
            const summary = (data && data.summary) || {};
            const line = [
                word,
                `coverage=${String((data && data.coverage) ?? '\u2014')}`,
                `paired_samples=${String(paired.paired_samples ?? summary.paired_samples ?? '\u2014')}`,
                `verdict_agreement=${String(summary.verdict_agreement ?? '\u2014')}`,
                `error_rate=${String(summary.error_rate ?? '\u2014')}`,
                String((data && data.reason) || '')
            ].join(' \u00b7 ');
            if (target) target.textContent = line;
            if (ready === true) Toast.success(word);
            else Toast.error(line);
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /** Checkpoint the journal by hand, in the mode the select names. */
    async checkpointDevWal() {
        const box = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-checkpoint-mode]') : null;
        const mode = box ? String(box.value || '') : '';
        try {
            const data = await API.request(
                `/developer/db/wal-checkpoint?mode=${encodeURIComponent(mode)}`, { method: 'POST' }
            );
            Toast.success(I18n.__('devDbCheckpointed')
                .replace('{mode}', String((data && data.mode) || mode))
                .replace('{before}', String((data && data.before_frames) ?? 0))
                .replace('{after}', String((data && data.log_frames) ?? 0)));
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /**
     * Run the full integrity check, and print what it found.
     *
     * The button says what it is doing while it waits, because this is the one read here that
     * takes seconds to minutes: ``PRAGMA integrity_check`` walks every page. No repaint for the
     * reason the shadow gate gives - and because a repaint would spend the read twice.
     */
    async runDevIntegrityCheck() {
        const target = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-integrity-report]') : null;
        const button = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-integrity]') : null;
        if (button) {
            button.disabled = true;
            button.textContent = I18n.__('devDbIntegrityRunning');
        }
        try {
            const data = await API.request('/developer/db/integrity');
            const schema = (data && data.schema) || {};
            const version = (data && data.schema_version) || {};
            const integrity = (data && data.integrity) || {};
            const output = (integrity.output || []).map(String);
            const lines = [
                I18n.__('devDbSchemaVersion')
                    .replace('{current}', String(version.current ?? '\u2014'))
                    .replace('{expected}', String(version.expected ?? '\u2014')),
                I18n.__('devDbSchemaPending').replace('{count}', String((version.pending || []).length)),
                `schema.ready=${String(schema.ready)} \u00b7 schema.mode=${String(schema.mode)}`,
                integrity.ok
                    ? I18n.__('devDbIntegrityOk')
                    : I18n.__('devDbIntegrityProblems').replace('{count}', String(output.length))
            ].concat(output);
            if (target) target.textContent = lines.join('\n');
            if (integrity.ok) Toast.success(I18n.__('devDbIntegrityOk'));
            else Toast.error(lines[3]);
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        } finally {
            if (button) {
                button.disabled = false;
                button.textContent = I18n.__('devDbIntegrityRun');
            }
        }
    },

    /**
     * Replace the model process.
     *
     * Confirmed, because the replacement is not free for anyone waiting at a gate: an in-flight
     * verification is lost with the child, and the next punch pays the ~200 MiB load again. The
     * answer names both pids, which is how an operator sees that a wedged child was really
     * replaced rather than merely reported on.
     */
    async restartDevEngine() {
        if (!confirm(I18n.__('devEngineConfirm'))) return;
        try {
            const data = await API.request('/developer/engine/restart-worker', { method: 'POST' });
            Toast.success(I18n.__('devEngineRestarted')
                .replace('{old}', String((data && data.old_pid) ?? '\u2014'))
                .replace('{new}', String((data && data.new_pid) ?? '\u2014')));
            return UI.renderAdminTab('Developer');
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },

    /**
     * Narrow the tamper ledger to one worker, and write the answer back in place.
     *
     * An empty box is "the whole window" rather than a refused request: clearing the filter is
     * the other thing this control is for, and a worker id that matches nothing answers with an
     * empty list and a zero count, which is the honest answer to a question about nobody.
     *
     * No repaint, for the reason the shadow gate gives and one of its own. A repaint would
     * re-read the whole console for a question about one list, and it would redraw the box
     * empty - throwing away the id that says which list this is. The block it rewrites holds no
     * controls of its own, so the binding that ran at paint time is still the binding it needs.
     */
    async filterDevTamperAlerts() {
        const field = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-tamper-worker]') : null;
        const target = typeof document.querySelector === 'function'
            ? document.querySelector('[data-dev-tamper-block]') : null;
        const worker = field ? String(field.value || '').trim() : '';
        const query = worker ? `?worker_id=${encodeURIComponent(worker)}` : '';
        try {
            const data = await API.request(`/developer/offline/tamper-alerts${query}`);
            this._devConsole.tamper = data || {};
            if (target) target.innerHTML = this.devTamperHtml();
        } catch (err) {
            Toast.error((err && err.message) || I18n.__('error'));
        }
    },


    /**
     * The shift rules this panel was last painted from, or ``null`` before the first
     * paint. ``saveShiftRules`` falls back to them, never to a hardcoded number.
     */
    _shiftRules: null,

    /**
     * The Admin tab: whose deployment this is, and the rules a shift is measured by.
     *
     * Two panels, both about the deployment rather than about one account. The **Company**
     * panel is the name and the mark every screen and every printed sheet carries (see
     * ``Brand``); the **shift rules** are the numbers a payroll question turns on - what a
     * day is worth, how much of it is an unpaid break, and whether the day ends by itself -
     * and until the panel existed the only way to change them was a ``curl``.
     *
     * Creating an administrator used to be this tab's third panel, and it was a worse
     * version of a screen that already existed: three boxes and no role choice, against a
     * Credentials form that takes the id, the name, the contact details, the password *and*
     * the face in one step. Two places to create an account is one place too many, so the
     * panel is gone and the note below says where the job actually lives - including which
     * of its two boxes decides what an administrator may do, because the id no longer
     * does: the role selected on the Credentials form is the whole of it.
     */
    async renderAdminManagement(content, knownRules, knownBranding) {
        // ``knownRules`` and ``knownBranding`` are passed back after a save, so the panel
        // repaints from what the server stored instead of spending extra round trips
        // re-reading it.
        let rules = knownRules || null;
        try {
            rules = rules || await API.request('/admin/shift_rules');
        } catch (err) {
            Toast.error(err.message);
        }
        let branding = knownBranding || null;
        try {
            branding = branding || await API.request('/branding');
            // Keep the lockup the rest of the app draws in step with what this panel is
            // showing, without a reload: the rail, the handset header and the next sheet
            // all read ``BRAND``.
            if (branding) this.adoptBranding(branding);
        } catch (err) {
            // Silence, deliberately. The read is public and the console already holds a
            // lockup (the one resolved at boot); a company name that cannot be re-read is
            // not worth an error toast over, and the panel draws what the app is using.
        }
        // The rules this panel was painted from. ``saveShiftRules`` reads a field back from
        // here when the box itself cannot answer (see that method for why).
        this._shiftRules = rules;
        content.innerHTML = `
            <div class="ui-page" data-admin-panel="true">
                ${this.companyHtml(branding)}
                ${this.shiftRulesHtml(rules)}
                <p class="ui-note is-body" data-admin-create-moved="true">${this.escapeHtml(I18n.__('adminCreateMoved'))}</p>
            </div>`;
        const rulesForm = document.getElementById('shiftRulesForm');
        if (rulesForm) rulesForm.onsubmit = (event) => this.saveShiftRules(event);
        const companyForm = document.getElementById('companyForm');
        if (companyForm) companyForm.onsubmit = (event) => this.saveCompany(event);
        const reset = document.getElementById('companyReset');
        if (reset) reset.onclick = () => this.resetCompanyLines();
        const logoInput = document.getElementById('companyLogoInput');
        if (logoInput) logoInput.onchange = () => this.pickCompanyLogo(logoInput);
        const removeLogo = document.getElementById('companyLogoRemove');
        if (removeLogo) removeLogo.onclick = () => this.removeCompanyLogo();
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
        // What the two watchers add up to, from the server: with the automatic close on and
        // the alert at or above the paid day, no shift can ever reach the alert - the day is
        // ended first, and the watcher looks exactly like one with nothing to report. The
        // figures this is decided from are on this screen, so the verdict belongs here too.
        // The sentence is built from the numbers rather than taken from the server's own
        // (English) ``detail``, because this panel ships in three languages.
        const dayEnd = (rules && rules.day_end) || null;
        const alertLost = !!(dayEnd && dayEnd.alert_reachable === false);
        // The other half of the same verdict, and the one with a behavioural change behind it:
        // with the overtime line above the paid day the automatic close stands down, so a
        // switch that still reads as on no longer closes anything. The two are mutually
        // exclusive (the alert is either below the line, on it, or above it), so one note is
        // shown, never a stack.
        const closeDeferred = !!(dayEnd && dayEnd.close_defers === true);
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

                ${alertLost
                    ? `<p class="ui-note is-body is-warn" data-rules-alert="unreachable">${this.escapeHtml(
                        I18n.__('shiftRulesAlertUnreachable')
                            .replace('{regular}', String(regular))
                            .replace('{notify}', String(overtimeAt))
                    )}</p>`
                    : ''}
                ${closeDeferred
                    ? `<p class="ui-note is-body is-warn" data-rules-alert="deferred">${this.escapeHtml(
                        I18n.__('shiftRulesCloseDeferred')
                            .replace('{regular}', String(regular))
                            .replace('{notify}', String(overtimeAt))
                    )}</p>`
                    : ''}

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
    //  Company - the name and the mark every screen and every sheet carries
    //
    //  Four lines and an image, next door to the shift rules because they are the same
    //  kind of thing: the deployment's own settings, editable where the deployment is
    //  administered. What they are *not* is decoration - the name on the login panel is
    //  how a worker on site cellular knows this is the app their administrator told them
    //  about, and the name at the top of a printed timesheet is whose payroll it is.
    // -----------------------------------------------------------------

    /**
     * The four lines of the lockup, and the box each one is typed into.
     *
     * One list rather than a template and a saver that agree by hand: the panel draws from
     * it, the save reads from it, and a fifth line would be one entry here. ``limit`` is the
     * server's own ceiling (``branding.MAX_FIELD_CHARS``), carried as ``maxlength`` so a box
     * stops before the refusal rather than after it.
     */
    COMPANY_LINES: [
        { key: 'name', input: 'companyName', label: 'companyName', limit: 60 },
        { key: 'legal', input: 'companyLegal', label: 'companyLegal', limit: 60 },
        { key: 'est', input: 'companyEst', label: 'companyEst', limit: 24 },
        { key: 'tagline', input: 'companyTagline', label: 'companyTagline', limit: 80 }
    ],

    /**
     * The Company panel: what this deployment calls itself and what mark it prints.
     *
     * The boxes are filled with what is *in force*, not with what is stored, because those
     * are the same thing to the administrator reading them: a line nobody has configured
     * shows the one this application ships with, and a line the company emptied shows an
     * empty box. That is also why the reset button exists - "stop deciding, print what you
     * shipped with" is a different act from "print nothing here", and only one of them can
     * be expressed by clearing a box.
     */
    companyHtml(data) {
        const inForce = (key) => {
            const stored = data ? data[key] : null;
            return stored === null || stored === undefined ? BRAND_DEFAULTS[key] : String(stored);
        };
        const logo = (data && data.logo) || null;
        const fields = this.COMPANY_LINES.map((line) => `
                    <div>
                        <label class="ui-label" for="${line.input}">${this.escapeHtml(I18n.__(line.label))}</label>
                        <input type="text" id="${line.input}" class="ui-field" maxlength="${line.limit}"
                               value="${this.escapeHtml(inForce(line.key))}">
                    </div>`).join('');
        const markNote = logo
            ? I18n.__('companyLogoConfigured')
                .replace('{width}', String(logo.width || 0))
                .replace('{height}', String(logo.height || 0))
                .replace('{size}', `${Math.max(1, Math.round(Number(logo.bytes || 0) / 1024))} KB`)
            : I18n.__('companyLogoShipped');
        return `
            <section class="ui-card is-flat" data-company-panel="true" aria-labelledby="companyTitle">
                <h3 class="ui-section-title" id="companyTitle">${this.escapeHtml(I18n.__('companyTitle'))}</h3>
                <p class="ui-section-note" style="margin-top:4px">${this.escapeHtml(I18n.__('companyHint'))}</p>
                <form id="companyForm" class="ui-grid two" style="margin-top:16px">
                    ${fields}
                    <div class="ui-row" style="grid-column:1/-1">
                        <button type="submit" class="ui-btn ui-btn-primary">${this.escapeHtml(I18n.__('save'))}</button>
                        <button type="button" id="companyReset" class="ui-btn ui-btn-quiet">${this.escapeHtml(I18n.__('companyReset'))}</button>
                    </div>
                    <p class="ui-section-note" style="grid-column:1/-1" data-company-blank-note="true">${this.escapeHtml(I18n.__('companyBlankNote'))}</p>
                </form>
                <div class="company-mark">
                    <img class="company-mark-preview" id="companyMarkPreview" src="${this.escapeHtml(BRAND.mark)}" alt=""
                         width="120" height="60" decoding="async">
                    <div class="company-mark-body">
                        <p class="ui-section-note" data-company-mark-note="true">${this.escapeHtml(markNote)}</p>
                        <div class="ui-row">
                            <input type="file" id="companyLogoInput" class="ui-field" accept="image/png,image/jpeg,image/webp"
                                   aria-label="${this.escapeHtml(I18n.__('companyLogoChoose'))}">
                            ${logo ? `<button type="button" id="companyLogoRemove" class="ui-btn ui-btn-quiet">${this.escapeHtml(I18n.__('companyLogoRemove'))}</button>` : ''}
                        </div>
                    </div>
                </div>
            </section>`;
    },

    /**
     * Saves the four lines: exactly what is in the boxes is what gets stored.
     *
     * Sent as strings including the empty one, so an emptied box means "this line is not
     * printed" - the server keeps that apart from ``null``, and only the reset button below
     * sends ``null``. Anything else would make "I don't want a legal suffix on the sheet"
     * and "put the shipped suffix back" the same keystroke.
     */
    async saveCompany(event) {
        if (event && event.preventDefault) event.preventDefault();
        const body = {};
        for (const line of this.COMPANY_LINES) {
            const element = document.getElementById(line.input);
            body[line.key] = element && element.value !== undefined && element.value !== null
                ? String(element.value).trim() : '';
        }
        return this.sendCompany('/admin/branding', { method: 'POST', body: body }, 'companySaved');
    },

    /** Puts every line back to the shipped lockup, and says so on the button. */
    async resetCompanyLines() {
        const body = {};
        for (const line of this.COMPANY_LINES) body[line.key] = null;
        return this.sendCompany('/admin/branding', { method: 'POST', body: body }, 'companyResetDone');
    },

    /**
     * Sets the company's mark from a chosen file.
     *
     * Checked in the browser first, against the same ceiling and the same type allowlist a
     * face reference goes through (``checkPhotoFile``, with the wording a logo deserves): a
     * 6 MB image refused here is one the administrator does not wait for over a site tether
     * before being told. The server re-checks the bytes and re-encodes them, so this is a
     * courtesy and never the check that matters.
     */
    async pickCompanyLogo(input) {
        const chosen = input && input.files && input.files[0] ? input.files[0] : null;
        const problem = this.checkPhotoFile(chosen, {
            unreadable: 'companyLogoUnreadable',
            tooLarge: 'companyLogoTooLarge',
            wrongType: 'companyLogoWrongType'
        });
        if (problem) {
            Toast.error(problem);
            if (input) input.value = '';
            return null;
        }
        if (!chosen) return null;
        const form = new FormData();
        form.append('logo', chosen, chosen.name || 'logo.png');
        return this.sendCompany('/admin/branding/logo', { method: 'POST', body: form }, 'companyLogoSaved');
    },

    /** Removes the configured mark, so the shipped one is used again. */
    async removeCompanyLogo() {
        return this.sendCompany('/admin/branding/logo', { method: 'DELETE' }, 'companyLogoRemoved');
    },

    /**
     * One request, one repaint, one message - the shape all five company actions share.
     *
     * The repaint is ``UI.paintAdminConsole`` rather than this panel alone: the name and the
     * mark are drawn on the rail, on the handset header and on the login panel, and an
     * administrator who has just renamed the company should see it everywhere at once rather
     * than in the one box they were typing in. Everything else the console was showing is
     * rebuilt from the server, and ``State.adminTab`` is what puts them back on this tab.
     */
    async sendCompany(endpoint, options, messageKey) {
        try {
            const answer = await API.request(endpoint, options);
            if (answer && answer.branding) this.adoptBranding(answer.branding);
            Toast.success(I18n.__(messageKey));
            UI.paintAdminConsole();
            return answer;
        } catch (err) {
            Toast.error(err.message);
            return null;
        }
    },

    /** Takes the server's answer as the lockup in force, here and on the next sheet. */
    adoptBranding(data) {
        Brand.apply(data);
        Brand.remember(data);
        return data;
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
            // The role is a column, not a footnote: an administrator who clocks in works a
            // shift like anybody else, and the first question about their row is which of
            // these rows is theirs. It sits beside the name because that is what it
            // describes, and it is searchable (``shiftsMatches``) so "administrator" is
            // also a filter rather than only something to read.
            role: { label: 'role' },
            id: { label: 'userId' },
            site: { label: 'site' },
            // The kind of place the site is (a warehouse, a factory). The filter row has
            // always offered it; a filter whose value cannot be read on the row it selected
            // is a filter the reader has to take on trust.
            category: { label: 'sitesCategory' },
            arrival: { label: 'shiftsArrival' },
            hours: { label: 'hours' },
            awaiting: { label: 'shiftsPending' },
            notes: { label: 'shiftsOpenNotes' }
        };
    },

    /** The order this tab was asked for, and the one Reset puts back. */
    defaultShiftsColumns() {
        return ['date', 'employee', 'role', 'id', 'site', 'category', 'arrival', 'hours', 'awaiting', 'notes'];
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

    /**
     * The category carried in a URL fragment, or '' when there is none.
     *
     * Read off the fragment rather than matched against the chips, because the chips are built
     * from the report - and the report is what this has to be set *before*: a link naming a
     * category should arrive with that category already selected, so the table the reader sees
     * is the table it was copied from.
     */
    shiftsCategoryFromUrl(url) {
        const source = String(url === undefined ? window.location.hash : url);
        const match = /(?:^|[#&])c=([^&]*)/.exec(source);
        if (!match) return '';
        try {
            return decodeURIComponent(match[1]);
        } catch (err) {
            // A malformed escape ("%zz") must not take the whole page down with it.
            return match[1];
        }
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

    /**
     * The fragment that describes a period, the search and the category over it.
     *
     * The category travels too, and that is not decoration: a link that carried the search but
     * dropped the category would show a colleague a *wider* table than the one it was copied
     * from, with the same dates at the top of it.
     */
    shiftsUrl(range, query) {
        const parts = [`#shifts=${range.start}..${range.end}`];
        if (query) parts.push(`q=${encodeURIComponent(query)}`);
        if (this.shiftsCategory()) parts.push(`c=${encodeURIComponent(this.shiftsCategory())}`);
        return parts.join('&');
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
        State.shiftsCategory = this.shiftsCategoryFromUrl();
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

    /** The category being filtered by, or '' for all of them. */
    shiftsCategory() {
        return State.shiftsCategory || '';
    },

    /**
     * One tap on a category: narrow the rows, change no figures' source.
     *
     * A repaint from the report already on screen, exactly like the search box - the categories
     * being filtered by are in that report, so a filter that re-asked the server for the period
     * would be a round trip to change nothing but which rows are drawn.
     */
    setShiftsCategory(name) {
        // A category and a search narrow *together*, which is the useful behaviour: "this
        // month's warehouse shifts by Ahmed" is a question somebody actually asks. Neither
        // clears the other, and the filter note below the cards names both.
        State.shiftsCategory = String(name === undefined || name === null ? '' : name);
        return this.repaintShiftsFromCache();
    },

    /** Whether anything is narrowing the rows, the category included. */
    shiftsFiltering() {
        return this.shiftsQuery() !== '' || this.shiftsCategory() !== '';
    },

    /**
     * Both filters as one string, for a file name.
     *
     * ``exportSlug`` keeps only ASCII, so an Arabic category contributes nothing to the name
     * rather than turning into punctuation - the same thing that already happens to an Arabic
     * search, and the period in the name still says which rows are in the file.
     */
    shiftsExportFilter() {
        return [this.shiftsQuery(), this.shiftsCategory()].filter(Boolean).join(' ');
    },

    /** The filter, in one sentence, for the note under the cards and the printed sheet. */
    shiftsFilterSentence() {
        return [
            this.shiftsQuery() ? `“${this.shiftsQuery()}”` : '',
            this.shiftsCategory()
                ? `${I18n.__('shiftsCategoryFilter')}: “${this.shiftsCategory()}”`
                : ''
        ].filter(Boolean).join(' \u00b7 ');
    },

    /**
     * The category picker, built from the report on screen.
     *
     * Not from a list fetched separately, and a list rather than a row of chips. Built from the
     * report, a category that worked nothing this period is simply not offered - a chip or an
     * option that filters to an empty table is a dead end - and the count beside each entry is
     * the number of shifts behind it, which is the first thing a reader checks a filter
     * against. A list rather than chips, so the control does not grow row by row as a company
     * adds categories and so the one that is *chosen* is legible even when the row is long.
     */
    shiftsCategorySelectHtml(report) {
        const categories = (report && report.categories) || [];
        if (categories.length === 0 && !this.shiftsCategory()) return '';
        const active = this.shiftsCategory();
        const rows = ((report && report.rows) || []).length;
        const option = (value, label) => `<option value="${this.escapeHtml(value)}"${active === value ? ' selected' : ''}>${this.escapeHtml(label)}</option>`;
        return `<div>
                <label class="ui-label" for="shiftsCategoryFilter">${this.escapeHtml(I18n.__('sitesCategory'))}</label>
                <select class="ui-field" id="shiftsCategoryFilter" data-shifts-category>${option('', `${I18n.__('shiftsAllCategories')} (${rows})`)}${categories.map((entry) => option(entry.name, `${entry.name} (${entry.shifts})`)).join('')}</select>
            </div>`;
    },

    /**
     * A change anywhere in the Shifts tab. The category picker is delegated here rather than
     * carrying an inline ``onchange``: a category name is operator data, and the CSP's
     * per-file allowance for inline handlers may only fall.
     */
    onShiftsChange(event) {
        const target = event && event.target;
        if (!target || typeof target.getAttribute !== 'function') return undefined;
        if (target.getAttribute('data-shifts-category') === null) return undefined;
        return this.setShiftsCategory(target.value);
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
     *
     * The one term that is not a substring search is a number: it is read as a worker id
     * and matched against what the row *is* rather than against the date it was worked on.
     * The rule is stated where it is applied.
     */
    shiftsMatches(rows, query) {
        const terms = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
        if (terms.length === 0) return rows;
        return rows.filter((row) => {
            // What the row *is*, with the calendar kept out of it: the two are one haystack
            // for every search except the numeric one below, and that exception is the
            // reason they are written apart.
            const identity = [
                row.worker_name, row.worker_id, row.site_name, row.status,
                // The role in both forms, like the credentials roster: the code is how it
                // arrives, the label is what is on screen - and "who was that administrator
                // again" is asked by typing the word the table shows.
                row.role, this.roleLabel(row.role),
                // The site's category, so a search for the word an operator thinks in - "مخزن" -
                // finds the shifts worked at every warehouse instead of none of them.
                row.site_category,
                this.arrivalWords(row)
            ].join(' ').toLowerCase();
            const calendar = [row.date, row.timestamp].join(' ').toLowerCase();
            return terms.every((term) => (
                // A number is somebody's id, not the fourth digit of a date. This deployment
                // hands out ids like `1`, `2` and `4`, and every row carries those digits in
                // its own date - so searching for worker 4 used to answer with the whole
                // timesheet. A *day* is still searchable: it is typed with its dashes
                // (`2026-08-07`), which is not a bare number, and matches below.
                /^\d+$/.test(term)
                    ? identity.indexOf(term) >= 0
                    : `${identity} ${calendar}`.indexOf(term) >= 0
            ));
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
        // How much of this row is still undecided. The server sends the figure on every
        // row, so the console sums the server's own arithmetic rather than assuming an
        // awaiting shift holds all of its hours - which stopped being true when a pending
        // overtime shift began crediting its standard day and holding only the extra. The
        // fallback is for a payload from an older server, where holding everything was
        // the rule; it keeps the two figures summing to ``hours`` either way.
        const awaitingHours = (row) => {
            if (!row.awaiting_approval) return 0;
            if (row.awaiting_approval_hours === undefined || row.awaiting_approval_hours === null) {
                return Number(row.hours) || 0;
            }
            return Number(row.awaiting_approval_hours) || 0;
        };
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
            ).size,
            // Counted per shift, not per worker: a count of arrivals, matching the server's
            // own figure key for key, so a filtered view and the period total are the same
            // arithmetic over a different set of rows.
            late_arrivals: rows.filter((row) => row.arrival_verdict === 'late').length
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
        this.paintShifts(content, this.shiftsToolbarHtml(range, cached.report) + this.shiftsReportHtml(cached.report));
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

    /**
     * Everything above the figures: the period picker, the categories, the columns, the search.
     *
     * ``report`` is the report on screen, and it is what lets the category chips carry a count:
     * the number of shifts behind each chip is the server's own per-period figure, so a chip can
     * never promise a table it will not fill. It is optional because the first paint happens
     * before the request comes back - the chips are simply absent until there is something to
     * count, which is more honest than a row of zeros.
     */
    shiftsToolbarHtml(range, report) {
        return `${this.shiftsFilterHtml(range, report)}${this.shiftsColumnsHtml()}${this.shiftsSearchHtml()}`;
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
        // The row actions, delegated: rows are a string at paint time, so there is no node
        // to attach to - and a handler per row would be a handler per row per repaint.
        // Assigned rather than added, like the two forms above, so repainting the tab
        // cannot leave the previous paint's listener behind on the same element.
        content.onclick = (event) => this.onShiftsClick(event);
        // The category picker fires on ``change``: one delegated listener beside the click one.
        content.onchange = (event) => this.onShiftsChange(event);
    },

    /**
     * A click anywhere in the Shifts tab. Two row controls are delegated here - the edit
     * affordance and the print action - because rows are a string at paint time, so there
     * is no node to attach a handler to; every other control in the tab carries its own
     * handler or its own inline call.
     */
    onShiftsClick(event) {
        const target = event && event.target;
        if (!target || typeof target.closest !== 'function') return undefined;
        const edit = target.closest('[data-edit-hours]');
        if (edit) {
            const dataset = edit.dataset || {};
            return this.editShiftHoursModal(dataset.editHours, dataset.editWorker, dataset.editRecorded);
        }
        const button = target.closest('[data-print-worker]');
        if (!button) return undefined;
        return this.printWorkerMonth((button.dataset || {}).printWorker);
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
    shiftsFilterHtml(range, report) {
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
                <div>
                    <!-- Which file the Download button writes. Read when the button is pressed
                         rather than remembered, so asking for the PDF once does not leave the
                         next download as a PDF nobody wanted. -->
                    <label class="ui-label" for="shiftsExportFormat">${this.escapeHtml(I18n.__('shiftsExportFormat'))}</label>
                    <select id="shiftsExportFormat" class="ui-field">
                        <option value="csv">${this.escapeHtml(I18n.__('shiftsExportExcel'))}</option>
                        <option value="pdf">${this.escapeHtml(I18n.__('shiftsExportPdf'))}</option>
                    </select>
                </div>
                <button type="button" onclick="UI_MODULES.downloadShiftsReport()" class="ui-btn">${this.OPS_ICONS.download}${this.escapeHtml(I18n.__('shiftsExportDownload'))}</button>
            </form>
            <div class="ui-row" style="margin-bottom:20px">
                <!-- The data-preset and data-active attributes stay adjacent and in that
                     order: the product suite reads which preset is in effect from exactly
                     that pair. -->
                ${this.shiftsPresets(range).map(preset => `
                    <button type="button" data-preset="${preset.key}" data-active="${preset.active}"
                            onclick="UI_MODULES.applyShiftsPreset('${preset.key}')"
                            class="ui-chip"${preset.active ? ' aria-pressed="true"' : ''}>${this.escapeHtml(I18n.__(preset.label))}</button>`).join('')}
                <!-- The category picker sits beside the periods rather than under the table,
                     where an operator is already reading rows: this row is where "what am I
                     looking at" is answered, and a warehouse filter is the same kind of choice
                     as "this month". The count on each option is the shifts behind it in *this*
                     period. A list rather than a quick search, so a long category name cannot
                     scroll out of reach and the chosen one is legible at a glance. -->
                ${this.shiftsCategorySelectHtml(report)}
                <button type="button" data-copy-link onclick="UI_MODULES.copyShiftsLink()"
                        class="ui-chip ui-push">${this.OPS_ICONS.copy}${this.escapeHtml(I18n.__('copyLink'))}</button>
            </div>`;
    },

    async loadShiftsReport(content) {
        const range = this.shiftsRange();
        try {
            const report = await API.request(`/admin/reports/shifts?start=${range.start}&end=${range.end}`);
            this._shiftsReport = { range: { start: range.start, end: range.end }, report: report };
            this.paintShifts(content, this.shiftsToolbarHtml(range, report) + this.shiftsReportHtml(report));
        } catch (err) {
            // No figures for this period, so nothing may be reused from an earlier one.
            this._shiftsReport = null;
            // The picker stays on screen with the error, so a rejected range (or a dead
            // server) is something the admin can correct and retry without leaving the tab.
            this.paintShifts(content, this.shiftsToolbarHtml(range, null) +
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
        const filtering = this.shiftsFiltering();
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
            // The one card that is not about hours: how much of the period walked in after
            // its window. Amber only when there is something to look at - a zero is the
            // good news, and a permanent amber cell stops meaning anything.
            ['late_arrivals', 'shiftsLateArrivals', String(totals.late_arrivals || 0),
                totals.late_arrivals ? 'warn' : ''],
        ];
        const dayChip = day ? `
            <div style="margin-bottom:16px">
                <button type="button" data-show-day onclick="UI_MODULES.applyShiftsDay('${day}')" class="ui-btn ui-btn-primary">
                    ${this.OPS_ICONS.table}${this.escapeHtml(I18n.__('shiftsShowDay'))} ${this.escapeHtml(day)}
                </button>
            </div>` : '';
        // The note names *what* is narrowing the rows, in the reader's words, because the
        // cards above it are then a total over fewer rows than the period holds - and a figure
        // nobody can account for is worse than no figure. Both filters are named when both are on.
        const filterNote = filtering ? `
            <p class="ui-section-note" data-filter-note style="margin-bottom:12px">
                ${this.escapeHtml(I18n.__('shiftsFiltered'))}: ${this.escapeHtml(this.shiftsFilterSentence())} · ${shown.length} / ${rows.length} ${this.escapeHtml(I18n.__('shifts'))}.<br>
                ${this.escapeHtml(I18n.__('shiftsFilteredTotals'))}
            </p>` : '';
        // With no match the cards are left out on purpose: a grid of zeros reads as "this
        // period held no work", which is the opposite of what a no-match means.
        // The same stat tiles the Live Ops board opens with, for the same reason: these
        // figures are what the tab is for, and one grid of them reads as one glance rather
        // than eight. ``data-total`` / ``data-value`` stay adjacent -
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
        //
        // The last cell is the one thing on a row that is not a column: the action that
        // prints *this person's* month. It is painted here rather than added as a column
        // because it is not a fact about the shift - the column chooser must not offer it,
        // the CSV must not carry it, and there is nothing to search it for.
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
                    <div class="ui-row" style="justify-content:flex-end">${this.shiftPrintActionHtml(row)}</div>
                </div>`).join('')}</div>`;
        }
        return `
            <div class="ui-table-wrap">
                <table class="ui-table" data-shifts-table="true">
                    <caption class="sr-only">${this.escapeHtml(I18n.__('shifts'))}</caption>
                    <thead>
                        <tr>
                            ${columns.map(key => `<th>${this.shiftsColumnLabel(key)}</th>`).join('')}
                            <th><span class="sr-only">${this.escapeHtml(I18n.__('shiftsPrintWorker'))}</span></th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.map(row => `<tr${this.shiftAttr(row)}>
                            ${columns.map(key => `<td>${this.shiftsCellHtml(row, key)}</td>`).join('')}
                            <td>${this.shiftPrintActionHtml(row)}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    },

    /**
     * The one control on a shift row that is not a column: print this person's month.
     *
     * An administrator asked for one worker's timesheet has had one way to get it: download
     * the whole period and take the other people out of the file. The row already knows
     * whose shift it is, so a single person's sheet is one tap from the shift that raised
     * the question - and the shift they clicked is inside the month it prints, which is how
     * the reader knows they clicked the right row.
     *
     * A ``data-`` hook rather than an inline ``onclick``: the CSP's inline-attribute
     * allowance is pinned per file and may only fall, and the label carries a worker's
     * *name*, which is text somebody else typed. ``paintShifts`` binds it.
     */
    shiftPrintActionHtml(row) {
        const label = I18n.__('shiftsPrintWorkerNamed')
            .replace('{name}', row.worker_name || row.worker_id);
        // The edit affordance rides beside the print action, and only on a row whose hours
        // are settled: a shift still awaiting a decision is answered in the Approvals queue,
        // and an edit affordance there would invite settling it "by the side door". The
        // server refuses those rows too - this is a courtesy, not the guard.
        const edit = this.shiftEditable(row)
            ? `<button type="button" class="ui-btn ui-btn-sm ui-btn-quiet is-icon"
                        data-edit-hours="${this.escapeHtml(String(row.log_id))}"
                        data-edit-worker="${this.escapeHtml(String(row.worker_name || row.worker_id))}"
                        data-edit-recorded="${this.escapeHtml(String(row.recorded_hours ?? row.hours ?? ''))}"
                        title="${this.escapeHtml(I18n.__('shiftHoursEditTitle'))}"
                        aria-label="${this.escapeHtml(I18n.__('shiftHoursEditTitle'))}">${this.OPS_ICONS.pencil}</button>`
            : '';
        return `${edit}<button type="button" class="ui-btn ui-btn-sm ui-btn-quiet is-icon"
                        data-print-worker="${this.escapeHtml(row.worker_id)}"
                        title="${this.escapeHtml(label)}"
                        aria-label="${this.escapeHtml(label)}">${this.OPS_ICONS.printer}</button>`;
    },

    /**
     * Whether a shift row's hours may be corrected.
     *
     * Payable or rejected rows are settled - their figures are history an administrator
     * may correct with an audit trail behind it. Rows still held for a decision (the
     * server names the same set) are not offered the form at all: editing around the
     * review would stand in for the decision the queue exists to record.
     */
    shiftEditable(row) {
        const code = String(row.status_code || '');
        return ['approved', 'auto_closed_8h', 'overtime_rejected', 'rejected', 'forced_out'].indexOf(code) >= 0;
    },

    /**
     * The hours-correction dialog for a shift that was already worked.
     *
     * One field - the figure the administrator is signing - prefilled with what the row
     * records now, and the note optional. The server re-derives the overtime from the
     * named figure and appends the before/after pair to the audit trail, so the dialog
     * has no arithmetic of its own to get wrong.
     */
    editShiftHoursModal(logId, workerName, recordedHours) {
        const name = this.escapeHtml(String(workerName || logId));
        const recorded = recordedHours !== '' && recordedHours != null && isFinite(Number(recordedHours))
            ? Number(recordedHours)
            : null;
        const backdrop = Modal.open(`
            <h3 style="margin-top:0">${this.escapeHtml(I18n.__('shiftHoursEditTitle'))}</h3>
            <p class="ops-note" style="margin-top:0">${this.escapeHtml(I18n.__('shiftHoursEditWorker'))}: <span class="ui-strong">${name}</span></p>
            ${recorded !== null ? `<p class="ops-note">${this.escapeHtml(I18n.__('shiftHoursEditRecorded'))}: ${this.escapeHtml(this.hoursLabel(recorded))}</p>` : ''}
            <label class="ops-stat-label" for="editShiftHours">${this.escapeHtml(I18n.__('shiftHoursEditLabel'))}</label>
            <input id="editShiftHours" class="ops-field" type="number" min="0" max="24" step="0.25" inputmode="decimal"
                   value="${recorded !== null ? this.escapeHtml(String(recorded)) : ''}" />
            <label class="ops-stat-label" for="editShiftHoursNote" style="margin-top:12px">${this.escapeHtml(I18n.__('shiftHoursEditNoteLabel'))}</label>
            <input id="editShiftHoursNote" class="ops-field" type="text" maxlength="300"
                   placeholder="${this.escapeHtml(I18n.__('shiftHoursEditNoteHint'))}" />
            <p class="ops-note">${this.escapeHtml(I18n.__('shiftHoursEditHint'))}</p>
            <div class="ui-row" style="margin-top:16px">
                <button type="button" class="ops-btn ops-btn-primary" id="editShiftHoursGo">${this.escapeHtml(I18n.__('shiftHoursEditConfirm'))}</button>
                <button type="button" class="ops-btn" id="editShiftHoursCancel">${this.escapeHtml(I18n.__('cancel'))}</button>
            </div>`, { dismissible: true });
        if (!backdrop) return;
        // Both buttons bound, not inline: the inline-handler budget in ``test_frontend_xss``
        // only falls, and a dialog that binds one button by id can bind the other the same way.
        const cancel = backdrop.querySelector('#editShiftHoursCancel');
        if (cancel && typeof cancel.addEventListener === 'function') {
            cancel.addEventListener('click', () => Modal.close());
        }
        backdrop.querySelector('#editShiftHoursGo').addEventListener('click', async () => {
            const field = backdrop.querySelector('#editShiftHours');
            const noteField = backdrop.querySelector('#editShiftHoursNote');
            const hours = Number(String((field && field.value) || '').trim());
            if (!isFinite(hours) || hours < 0 || hours > 24) {
                Toast.error(I18n.__('shiftHoursEditInvalid'));
                return;
            }
            const go = backdrop.querySelector('#editShiftHoursGo');
            if (go) go.disabled = true;
            try {
                const res = await API.request(`/admin/shifts/${encodeURIComponent(String(logId))}/hours`, {
                    method: 'POST',
                    body: {
                        hours,
                        note: noteField && String(noteField.value || '').trim() ? String(noteField.value).trim() : null
                    }
                });
                Modal.close();
                Toast.success(res.message || I18n.__('shiftHoursEditConfirm'));
                const pane = document.getElementById('adminContent');
                if (pane) this.loadShiftsReport(pane);
            } catch (err) {
                if (go) go.disabled = false;
                Toast.error(err.message);
            }
        });
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

    /**
     * Whether this shift's arrival was inside the window, and by how much it was not.
     *
     * The verdict and the minutes come from the server, which judges the arrival with the
     * same function the gate did (``shift_windows``), so this cell cannot disagree with the
     * flag the worker's own card showed. "No clock-in" is a fourth state on purpose: a
     * force-clock-out, or a shift closed with no arrival on file, is *unknown*, and a cell
     * that read "on time" there would be inventing punctuality out of missing data.
     */
    arrivalCellHtml(row) {
        const minutes = String(Number(row.arrival_minutes) || 0);
        const clocked = row.arrival_time ? ` title="${this.escapeHtml(row.arrival_time)}"` : '';
        if (row.arrival_verdict === 'late') {
            return `<span class="ui-badge is-warn"${clocked}>${this.escapeHtml(
                I18n.__('shiftsArrivalLate').replace('{minutes}', minutes))}</span>`;
        }
        if (row.arrival_verdict === 'early') {
            return `<span class="ui-badge"${clocked}>${this.escapeHtml(
                I18n.__('shiftsArrivalEarly').replace('{minutes}', minutes))}</span>`;
        }
        if (row.arrival_verdict === 'on_time') {
            return `<span class="ui-note"${clocked}>${this.escapeHtml(I18n.__('shiftsArrivalOnTime'))}</span>`;
        }
        return `<span class="ui-tone-faint">${this.escapeHtml(I18n.__('shiftsArrivalUnknown'))}</span>`;
    },

    /**
     * The arrival in the words a search can match: "On time", "Late 12 min", "Early 5 min".
     *
     * The wording that is on screen *and* the verdict code, so "late" finds the late ones
     * while "late 12" finds the ones that far off - and an administrator reading the console
     * in Arabic can type the Arabic word and get the same rows. The number is in the haystack
     * too, because "who was more than an hour late" is asked by typing what is on the screen:
     * every term has to match, so "late 60" is the answer to it.
     */
    arrivalWords(row) {
        const word = {
            late: 'shiftsArrivalLate',
            early: 'shiftsArrivalEarly',
            on_time: 'shiftsArrivalOnTime'
        }[row.arrival_verdict];
        if (!word) return '';
        const minutes = String(Number(row.arrival_minutes) || 0);
        // The verdict's own wording and nobody else's: a haystack that carried all three
        // sentences would make "late" match every row on the tab, which is the opposite of
        // a search.
        return [row.arrival_verdict, minutes, I18n.__(word).replace('{minutes}', minutes)].join(' ');
    },

    /** One cell of one row. The only place a column's value is written. */
    shiftsCellHtml(row, key) {
        switch (key) {
            case 'date':
                return `<span class="ui-nowrap">${this.escapeHtml(row.date)}</span>`;
            case 'employee':
                return `<span class="ui-strong">${this.escapeHtml(row.worker_name || row.worker_id)}</span>`;
            case 'role':
                // In the reader's words, like every other role on this console. An account
                // that no longer exists still has shifts here - deleting a login does not
                // delete the days somebody worked - so the cell says the role is unknown
                // rather than painting an empty cell the reader would take for "worker".
                return row.role
                    ? this.escapeHtml(this.roleLabel(row.role))
                    : `<span class="ui-tone-faint">\u2014</span>`;
            case 'id':
                return `<span class="ui-tone-muted">${this.escapeHtml(row.worker_id)}</span>`;
            case 'site':
                // An em dash, not an empty cell: a shift whose site is not on file is a gap
                // in the record, and a blank reads as "this row has no site column".
                return row.site_name
                    ? this.escapeHtml(row.site_name)
                    : `<span class="ui-tone-faint">\u2014</span>`;
            case 'category':
                // The site's category as the server resolved it *on this row*, not a second
                // lookup in the site list: the timesheet already decided which category each
                // shift belongs to, and a lookup here could disagree with the chip that
                // selected it. A site with no category reads as a gap, like a row with no site.
                return row.site_category
                    ? this.escapeHtml(row.site_category)
                    : `<span class="ui-tone-faint">\u2014</span>`;
            case 'arrival':
                return this.arrivalCellHtml(row);
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

    /** Which file the Download button writes: the spreadsheet, or the report on paper. */
    shiftsExportFormat() {
        const field = document.getElementById('shiftsExportFormat');
        const chosen = field && field.value ? String(field.value) : '';
        // Anything that is not a deliberate "pdf" is the spreadsheet. The field is read
        // from the DOM, so a value this build does not know - a stale page, a browser that
        // gave the select no value - must not silently turn a download into a print dialog
        // nobody asked for.
        return chosen === 'pdf' ? 'pdf' : 'csv';
    },

    /**
     * The Download button: one control, two files, and exactly the rows on screen either
     * way - which is the whole point of building both from the report already in hand.
     */
    downloadShiftsReport() {
        if (this.shiftsExportFormat() === 'pdf') {
            this.printShiftsReport();
            return;
        }
        this.downloadShiftsCsv();
    },

    /**
     * The report as a printable sheet - the PDF half of the download.
     *
     * Printed by the browser rather than generated on the server, because the PDF *is* the
     * print dialog's job here: no PDF library is pinned, no Arabic-capable font ships with
     * one, and a sheet the browser draws has the reader's own fonts and direction already
     * right - in Cairo, in Muscat, on a machine whose fonts nobody configured. "Save as
     * PDF" in that dialog is what writes the file.
     *
     * What goes *on* the sheet is this screen's own - the administrator's columns, the
     * period, the total. How the paper is taken out of the page, named in the dialog and
     * put back is ``PrintReport``'s, shared with the worker's own timesheet: those three
     * rules are the same for both readers, and two copies of them is how one of the two
     * screens ends up leaving its sheet behind.
     *
     * The sheet is built from the same rows as the CSV, for the same reason the CSV is
     * built on the client: a file that has to be re-derived from the server can disagree
     * with the table it was taken from.
     */
    printShiftsReport() {
        const range = this.shiftsRange();
        const report = this.shiftsReportFor(range);
        if (!report) {
            // Nothing on screen for this period, so there is no honest sheet to print.
            Toast.error(I18n.__('shiftsNothingToExport'));
            return;
        }
        PrintReport.sheet(
            this.shiftsPrintHtml(report, range),
            this.shiftsExportName(range, this.shiftsExportFilter(), '')
        );
    },

    /**
     * The sheet: what period it covers, what it was filtered to, the rows, and the total.
     *
     * The columns are the administrator's own - the same order, the same cells as the table
     * on screen - because this is the timesheet they are looking at, on paper. The four
     * fixed columns of the CSV exist so two months of files can be compared; paper is read
     * by the person who set the columns up.
     *
     * What this builds is the *content*: which cells, what the total says, and what an empty
     * period reads. The frame around it - the title, the period line, the table, the note
     * about approved hours - is ``PrintReport.sheetHtml``'s, shared with the worker's own
     * timesheet, so the two sheets cannot drift apart in what they claim about the same
     * figures.
     */
    shiftsPrintHtml(report, range) {
        const columns = this.shiftsColumns();
        const shown = this.shiftsVisibleRows(report);
        const filtering = this.shiftsFiltering();
        const totals = filtering ? this.sumShiftsRows(shown) : (report.totals || {});
        const period = report.period || range;
        // The sheet carries the same filter sentence as the screen: it is printed to be read
        // away from the tab, and a total over part of a period with nothing saying so is a
        // number that will be trusted wrongly.
        const filterNote = filtering ? `${I18n.__('shiftsFiltered')}: ${this.shiftsFilterSentence()}` : '';
        // The total, on paper only: a printed timesheet without one is a list, and the
        // figure is already computed for the cards above the table on screen.
        const totalsLine = `${this.escapeHtml(I18n.__('shiftsTotal'))}: <b>${this.escapeHtml(this.hoursLabel(totals.hours))} h</b>`
            + ` · ${Number(totals.shifts || 0)} ${this.escapeHtml(I18n.__('shiftsWorked'))}`
            + ` · ${Number(totals.workers || 0)} ${this.escapeHtml(I18n.__('shiftsWorkers'))}`;
        return PrintReport.sheetHtml({
            title: I18n.__('shiftsPrintTitle'),
            meta: [PrintReport.periodLine(period, filterNote)],
            columns: columns.map((key) => this.shiftsColumnLabel(key)),
            rows: shown.map((row) => columns.map((key) => this.shiftsCellHtml(row, key))),
            totals: totalsLine,
            empty: I18n.__(filtering ? 'shiftsNoMatches' : 'shiftsEmpty')
        });
    },

    /**
     * The month a per-worker sheet covers: the calendar month the period on screen starts
     * in, clipped to today while that month is still running.
     *
     * Read from the period rather than from the clock, so "this person's month" means the
     * month the administrator is looking at - the one the tab opened on, or the one a preset
     * or a shared link put them on - and the button that says which month it prints is
     * telling the truth. The clip is the presets' own rule: a shifts period describes work
     * that has happened, and a sheet ending in the future reads as a month that lost its
     * last shifts. Only a month already under way is clipped; a past month keeps its last
     * day.
     */
    shiftsWorkerMonth(range) {
        const onScreen = range || this.shiftsRange();
        const month = String((onScreen && onScreen.start) || '').slice(0, 7);
        if (!/^\d{4}-\d{2}$/.test(month)) return onScreen;
        // Day zero of the *next* month is the last day of this one - no month-length table.
        const last = this.isoDate(new Date(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0));
        const today = this.isoDate(new Date());
        return {
            start: `${month}-01`,
            end: `${month}-01` <= today && last > today ? today : last
        };
    },

    /**
     * One person's month, printed: whose it is, the days, and what they add up to.
     *
     * Two things this sheet says that the period sheet on the rest of this tab cannot: it
     * holds one person (the shift's own worker) and it covers that person's whole month,
     * not the period on screen. Neither is done by filtering the rows this screen happens to
     * be holding - the rows are asked of the server for one worker and the month's bounds,
     * through the same endpoint the tab reads, so the figures on paper are the server's own
     * and a month longer than the period on screen is a month, not a guess.
     */
    async printWorkerMonth(workerId) {
        const id = String(workerId === null || workerId === undefined ? '' : workerId);
        if (!id) return undefined;
        const month = this.shiftsWorkerMonth(this.shiftsRange());
        let report = null;
        try {
            report = await API.request(
                `/admin/reports/shifts?start=${month.start}&end=${month.end}&worker_id=${encodeURIComponent(id)}`
            );
        } catch (err) {
            Toast.error(`${I18n.__('error')}: ${err.message}`);
            return undefined;
        }
        const rows = (report && report.rows) || [];
        if (rows.length === 0) {
            // A month nobody worked is not a document: a sheet with a title and no rows is
            // read as a lost timesheet rather than as somebody who did not work, so this is
            // refused in words instead of printed.
            Toast.error(I18n.__('shiftsPrintWorkerEmpty'));
            return undefined;
        }
        PrintReport.sheet(
            this.workerMonthSheetHtml(report, id),
            this.workerMonthExportName(month, rows[0].worker_name || id)
        );
        return undefined;
    },

    /**
     * The sheet: one worker's month, under the same frame as every other report here.
     *
     * The columns are the administrator's own, minus the three that identify *whose* rows
     * these are - the employee, the role and the id. On a sheet about one person they would
     * repeat the same name down twenty rows, and the line above the table says them once, in
     * the header where a reader looks for them. What is left keeps the tab's order and the
     * tab's own cells, so the paper and the screen it came from read the same way.
     */
    workerMonthSheetHtml(report, fallbackId) {
        const totals = report.totals || {};
        const rows = report.rows || [];
        const hours = (value) => `${this.hoursLabel(value)} h`;
        const facts = [
            `${I18n.__('shiftsTotal')}: <b>${this.escapeHtml(hours(totals.hours))}</b>`,
            `${this.escapeHtml(I18n.__('shiftsApproved'))}: ${this.escapeHtml(hours(totals.approved_hours))}`
        ];
        if (Number(totals.awaiting_approval_hours || 0) > 0) {
            // Only when something is waiting: a zero on the sheet would read as a decision
            // that is owed when there is none.
            facts.push(
                `${this.escapeHtml(I18n.__('shiftsPendingHours'))}: `
                + `${this.escapeHtml(hours(totals.awaiting_approval_hours))}`
            );
        }
        facts.push(
            `${Number(totals.shifts || 0)} ${this.escapeHtml(I18n.__('shiftsWorked'))}`
            + ` · ${this.escapeHtml(hours(totals.break_hours))} ${this.escapeHtml(I18n.__('shiftsBreak'))}`
        );
        const columns = this.workerMonthColumns();
        return PrintReport.sheetHtml({
            title: I18n.__('shiftsWorkerSheet'),
            meta: [this.workerMonthWho(rows[0] || {}, fallbackId), PrintReport.periodLine(report.period || {})],
            columns: columns.map((key) => this.shiftsColumnLabel(key)),
            rows: rows.map((row) => columns.map((key) => this.shiftsCellHtml(row, key))),
            totals: facts.join(' · '),
            empty: I18n.__('shiftsEmpty')
        });
    },

    /**
     * Who the sheet is about: name, role, id - once, above the table.
     *
     * The role is left out when the account is gone (the row carries no role then, and the
     * timesheet keeps its rows after its login is deleted), rather than printed as a blank.
     */
    workerMonthWho(row, fallbackId) {
        const id = row.worker_id || fallbackId;
        return [
            row.worker_name || id,
            row.role ? this.roleLabel(row.role) : '',
            id ? `id ${id}` : ''
        ].filter(Boolean).join(' · ');
    },

    /** The columns one person's sheet carries: the tab's own order, without the identity. */
    workerMonthColumns() {
        // The three that answer "whose row is this" - a question a one-worker sheet answers
        // in its header instead of on every line.
        const identity = ['employee', 'role', 'id'];
        return this.shiftsColumns().filter((key) => identity.indexOf(key) < 0);
    },

    /**
     * The name the print dialog offers for one person's month: who, then which month.
     *
     * The worker's name is in it on purpose. "Save as PDF" writes into a folder where two of
     * these sheets may already be, and the other one is a different person.
     */
    workerMonthExportName(month, name) {
        const slug = this.exportSlug(name);
        return `timesheet${slug ? `_${slug}` : ''}_${month.start}_${month.end}`;
    },

    /** A text value as a file name's own word: lower case, dashes, nothing exotic. */
    exportSlug(value) {
        return String(value || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 24);
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
            this.shiftsExportName(range, this.shiftsExportFilter()),
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
        return this.shiftsMatches(this.shiftsRowsInCategory(rows), this.shiftsQuery());
    },

    /**
     * The rows at sites in the chosen category, or all of them when none is chosen.
     *
     * Matched on the name the server published on the row rather than by looking the site up:
     * the timesheet already decided which category each shift belongs to, and a second lookup
     * here could disagree with the column the reader is looking at.
     */
    shiftsRowsInCategory(rows) {
        const category = this.shiftsCategory();
        if (!category) return rows;
        return rows.filter((row) => String(row.site_category || '') === category);
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
     *
     * ``extension`` is ``.csv`` for the sheet the page writes itself, and empty for the
     * printed one: the print dialog names the file after the *document title* and appends
     * its own extension, so a title that already ended in ``.pdf`` would produce
     * ``shifts_....pdf.pdf``.
     */
    shiftsExportName(range, query, extension = 'csv') {
        const slug = this.exportSlug(query);
        const suffix = extension ? `.${extension}` : '';
        return `shifts_${range.start}_${range.end}${slug ? `_${slug}` : ''}${suffix}`;
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
        // Two questions on one screen, and the reader's own comes first. An administrator
        // works a shift of their own (``handsetRoles``, and ``SELF_ENROLL_ROLES`` beside it),
        // and the note a worker writes to ask for something is a note they have to be able to
        // write too - but this tab used to offer only the mailbox side of it: every note as a
        // reviewer, and no way to open one of their own. It is the *worker's* card, painted
        // here by ``WORKER_MODULES.renderNotes``, because two copies of "my notes" would
        // disagree the first time one of them changed.
        content.innerHTML = this.notesMineHtml() + `<div id="notesInbox"></div>`;
        this.paintMyNotes();
        // The inbox is painted into its own host so the card above survives a search, a
        // repaint and an error line: the two are separate questions and the second must not
        // be able to take the first off the screen.
        const inbox = this.notesRegion() || content;
        // Toolbar first: the list can be slow from site, and an admin should see the
        // filter they are about to use rather than a blank tab.
        this.paintNotes(inbox, this.notesToolbarHtml() + UI.loadingHtml());
        await this.loadNotes(inbox);
    },

    /** The host the administrator's own notes paint into, above the mailbox. */
    notesMineHtml() {
        return `<section class="ui-card" data-my-notes="true"><div id="adminMyNotes"></div></section>`;
    },

    /**
     * Where the mailbox paints: its own region, or the tab when nothing has painted yet.
     *
     * One function because four call sites have to agree - the queue, the thread it opens
     * into, the repaint that keeps a revealed password readable, and the dismissal that
     * forgets it. A reader who reaches this screen from somewhere other than the tab itself
     * (a notification, a reload) still has a target.
     */
    notesRegion() {
        return document.getElementById('notesInbox') || document.getElementById('adminContent');
    },

    /**
     * The signed-in administrator's own notes, in the worker's own words.
     *
     * The same request and the same cards a worker gets on the handset, because an
     * administrator asking for a password reset or reporting a missing delivery is the same
     * person making the same request. A failure is left to that module's own error line
     * rather than raised here - the mailbox below is a different question.
     */
    paintMyNotes() {
        const host = document.getElementById('adminMyNotes');
        if (!host || typeof WORKER_MODULES === 'undefined' || !WORKER_MODULES.renderNotes) return;
        Promise.resolve(WORKER_MODULES.renderNotes(host)).catch(() => {});
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
        // The inbox, not the tab: repainting the whole screen would take "my notes" down
        // with it, and the reader who is searching the mailbox is not done with their own.
        const content = this.notesRegion();
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
        // The mailbox region, beside the caller's own notes rather than over them: opening a
        // note is a move *within* the queue, and a reader who came here to file something of
        // their own has not finished with the card above.
        const content = this.notesRegion();
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
        const content = this.notesRegion();
        if (!content || !this._noteThread) {
            return UI.renderAdminTab('Notes');
        }
        this.paintNotes(content, this.noteThreadHtml(this._noteThread));
        return Promise.resolve();
    },

    // =====================================================================
    //  Dashboard - the console's front door
    // =====================================================================
    //
    // The screen an administrator lands on, and the reason it is a landing screen
    // rather than a fifth board: **every number on it is counted by the server.**
    // ``GET /admin/dashboard`` runs aggregate queries and answers with the totals,
    // where every other screen in this console *downloads a list and counts it in
    // the browser* - ``fetchLiveOps`` alone reads four full payloads on a poll loop
    // so that a board can say "12 on site". This tab reads one, once.
    //
    // Three rules it follows, all of them the plan's (``docs/DASHBOARD_PLAN.md``):
    //
    // 1. **A snapshot, not a board.** It does not poll, on purpose: a polled aggregate
    //    is a query every few seconds against the same SQLite writer that serves the
    //    gate. ``as_of`` is drawn beside the figures and the panel says in words that
    //    it refreshes when asked, because silently drifting totals are worse than
    //    totals that admit they are a minute old.
    // 2. **A count, a state, or a link.** Anything that needs a list or a decision
    //    lives in the tab that owns it, and each panel links there.
    // 3. **Unknown is not zero.** A panel the server could not read arrives as ``null``
    //    and is drawn as "could not be read" - never as a zero standing in for it,
    //    which is the one output that reads as good news.
    //
    // Every control is bound by a ``data-`` hook through ``bindDashboardControls``
    // rather than an ``onclick``: the CSP's inline-attribute allowance may only fall.

    //: The last read, so each view helper can ask for its own slice of it. ``null``
    //: is "not read", and it is cleared on every entry to the tab: a reader arriving
    //: at the front door wants the truth now, not the figure from five minutes ago.
    _dashboard: null,

    //: The element the last paint was drawn into, so switching a view can redraw in place
    //: without reaching for the document. Written only by ``renderDashboard``, which is the
    //: only thing that ever paints this tab - so it is the same age as ``_dashboard`` above.
    _dashboardHost: null,

    //: The days the period panel asks for when nothing has been chosen. It has to be the
    //: server's own default (``dashboard.DEFAULT_PERIOD_DAYS``), because the label on the
    //: button says "last 7 days" and the figures under it come from whatever the request
    //: asked for - one number, in two places, held together by the frontend suite.
    DASHBOARD_PERIOD_DAYS: 7,

    /**
     * The period panel's window as the query parameter takes it: a day count, or ``month``.
     *
     * ``7`` rather than ``"7days"``: the server refuses anything but a number or ``month``
     * (see ``dashboard.PERIOD_PATTERN``), and a window the server would 422 is a panel that
     * reads as broken rather than as the default it meant.
     */
    dashboardPeriodToken() {
        const token = String(State.dashboardDays || '').trim();
        return token || String(this.DASHBOARD_PERIOD_DAYS);
    },

    /** The front door: one read, then the five panels. */
    async renderDashboard(content) {
        if (!content) return;
        this._dashboard = null;
        this._dashboardHost = content;
        content.innerHTML = UI.consoleSkeletonHtml(I18n.__('dashboard'));
        let data = null;
        try {
            // The period's window is the one thing this screen asks for: every other panel is
            // about now, and a second parameter would be a second window to explain.
            data = await API.request(`/admin/dashboard?days=${encodeURIComponent(this.dashboardPeriodToken())}`);
        } catch (err) {
            // The shell is drawn anyway, so the refresh control is on the screen that
            // says the read failed - which is the one moment somebody wants it.
            content.innerHTML = this.dashboardShellHtml(null) + this.uiErrorHtml(err, null);
            this.bindDashboardControls(content);
            return;
        }
        this._dashboard = data;
        content.innerHTML = this.dashboardShellHtml(data);
        this.bindDashboardControls(content);
    },

    /**
     * The shell: the stamp, the vital strip, the view switcher, and the one view in effect.
     *
     * WHY IT IS NOT FIVE STACKED PANELS ANY MORE. Five cards down a page is five screens of
     * scrolling, and the figure that decides whether anybody is needed at all is the one that
     * ends up below the fold - a dashboard whose answer is four scrolls down is a dashboard read
     * by habit rather than by need. So the screen is two things now:
     *
     * 1. **A vital strip** - the numbers somebody needs *before* deciding to read anything, each
     *    one the server's own field and each one a link into the tab that owns that queue. It
     *    never switches away, so the state of the deployment is always on the screen.
     * 2. **One view at a time**, chosen by a segmented control built from the console's own
     *    ``ops-seg`` vocabulary, with the panel it draws wired as a real ``tabpanel``.
     *
     * Nothing is counted, summed or averaged in the browser to make any of this work: a strip tile
     * prints one field, and a tab is a name. Where a figure *is* derived - "waiting on a person"
     * across four queues - the server already answered it (``waiting``), which is why the strip
     * carries the queues separately rather than adding them up here.
     */
    dashboardShellHtml(data) {
        return `
            <div class="ui-page" data-dashboard="true">
                <header class="ui-section-head">
                    <h1 class="ui-section-title">${this.escapeHtml(I18n.__('dashboard'))}</h1>
                    <p class="ui-section-note dashboard-lead">${this.escapeHtml(I18n.__('hintDashboard'))}</p>
                </header>
                <div class="ui-spread dashboard-stamp">
                    <p class="ops-sub" data-dashboard-asof="true">${this.escapeHtml(this.dashboardStampText(data))}</p>
                    <button type="button" class="ui-btn" data-dashboard-refresh="true">${this.OPS_ICONS.refresh}<span>${this.escapeHtml(I18n.__('dashboardRefresh'))}</span></button>
                </div>
                ${this.dashboardVitalsHtml(data)}
                ${this.dashboardSwitchHtml(data)}
                ${this.dashboardViewHtml(data)}
            </div>`;
    },

    /**
     * The vital strip: five figures, each one field of the payload and each one a link.
     *
     * Chosen to be the five that change what somebody does in the next ten minutes: the two
     * queues a person has to work, who is on site right now, how the week is going, and the hours
     * nobody has signed for yet. The order is that order. A queue figure that is not zero is
     * marked as *waiting* (the warn tone), and "on shift" takes the live tone only when somebody
     * is actually on site - the same two tones the board next door uses for the same facts.
     */
    dashboardVitalsHtml(data) {
        const waiting = data ? data.waiting : null;
        const moment = data ? data.now : null;
        const period = data ? data.period : null;
        // ``null`` when the panel behind the figure could not be read. It is drawn as an em dash
        // and never as a zero: on this strip a zero is good news, and inventing good news is the
        // one output this page must not produce.
        const field = (source, name) =>
            (source === null || source === undefined ? null : source[name]);
        const tiles = [
            { id: 'reviews', labelKey: 'dashboardReviews', tab: 'Approvals', tone: 'is-warn', value: field(waiting, 'reviews') },
            { id: 'registrations', labelKey: 'dashboardRegistrations', tab: 'Registrations', tone: 'is-warn', value: field(waiting, 'registrations') },
            { id: 'on_shift', labelKey: 'dashboardOnShift', tab: 'Live Ops', tone: 'is-live', value: field(moment, 'on_shift') },
            {
                id: 'present_days', labelKey: 'dashboardPeriodPresentDays', tab: 'Shifts', value: field(period, 'present_days'),
                note: period
                    ? I18n.__('dashboardVitalOfExpected').replace('{expected}', String(Number(period.expected_days || 0)))
                    : ''
            },
            { id: 'awaiting_approval_hours', labelKey: 'dashboardPeriodAwaitingHours', tab: 'Approvals', tone: 'is-warn', value: field(period, 'awaiting_approval_hours') }
        ];
        return `<section class="dashboard-vitals" data-dashboard-vitals="true">${tiles
            .map((tile) => this.dashboardVitalHtml(tile))
            .join('')}</section>`;
    },

    /**
     * One vital: the figure, what it is, and where it is worked.
     *
     * The whole tile is the button, so the tap target is the tile and not a word inside it (a
     * phone in gloves is the reader this console was written for). The tab's own name is printed
     * under the label - "Approvals", "Registrations" - so a tile says where it goes without
     * repeating a sentence five times, and the accessible name of the button is all of it.
     */
    dashboardVitalHtml(tile) {
        const unknown = tile.value === null || tile.value === undefined;
        const figure = unknown ? '—' : String(tile.value);
        // The tone is a *state*, not decoration: a queue with something in it is the reason this
        // screen exists, and a stopped queue is not marked at all.
        const flagged = !unknown && Number(tile.value) > 0;
        const classes = ['dashboard-vital'];
        if (unknown) classes.push('is-unknown');
        else if (flagged && tile.tone) classes.push(tile.tone);
        const offered = this.dashboardTabOffered(tile.tab);
        const link = offered ? ` data-dashboard-go="${this.escapeHtml(tile.tab)}"` : '';
        return `
            <button type="button" class="${classes.join(' ')}" data-dashboard-vital="${this.escapeHtml(tile.id)}"${link}>
                <span class="dashboard-vital-value" data-dashboard-vital-fact="${this.escapeHtml(tile.id)}">${this.escapeHtml(figure)}</span>
                <span class="dashboard-vital-label">${this.escapeHtml(I18n.__(tile.labelKey))}</span>
                ${tile.note ? `<span class="dashboard-vital-note">${this.escapeHtml(tile.note)}</span>` : ''}
                ${offered ? `<span class="dashboard-vital-link">${this.escapeHtml(I18n.__(adminTabRecord(tile.tab).key))}</span>` : ''}
            </button>`;
    },

    /**
     * The views this screen can show, in the order they are offered, oldest question first.
     *
     * One list, so the tab strip, the drawn panel, the keyboard walk and the tests cannot
     * disagree about what a view is called or which one is first. The short ``label`` is for the
     * control and the ``title`` is the panel's own heading, because a segmented control that read
     * "Waiting on a person / Right now / The period" would not fit a phone - and a label is not a
     * heading.
     */
    dashboardViews() {
        return [
            { id: 'waiting', label: 'dashboardMetricWaiting', title: 'dashboardWaitingTitle', hint: 'dashboardWaitingHint' },
            { id: 'now', label: 'dashboardMetricNow', title: 'dashboardNowTitle', hint: 'dashboardNowHint' },
            { id: 'people', label: 'dashboardMetricPeople', title: 'dashboardPeopleTitle', hint: 'dashboardPeopleHint' },
            { id: 'places', label: 'dashboardMetricPlaces', title: 'dashboardPlacesTitle', hint: 'dashboardPlacesHint' },
            { id: 'period', label: 'dashboardMetricPeriod', title: 'dashboardPeriodTitle', hint: 'dashboardPeriodHint' }
        ];
    },

    /** The view in effect, or the first one when the remembered id is not one of them. */
    dashboardMetric() {
        const views = this.dashboardViews();
        const wanted = String(State.dashboardMetric || '');
        return views.some((view) => view.id === wanted) ? wanted : views[0].id;
    },

    /**
     * The switcher: a tablist, wired end to end - ``role``, ``aria-selected``, ``aria-controls``,
     * and the roving ``tabindex`` a tab walk needs (only the selected tab is in the tab order, and
     * the arrow keys in ``bindDashboardControls`` are what move between them).
     *
     * No counts on the tabs, deliberately: a tab that carried a figure would need the *sum* of a
     * panel's queues, and adding numbers up in the browser is the one thing this screen refuses -
     * the strip above it already carries each queue as the server counted it.
     */
    dashboardSwitchHtml(data) {
        const active = this.dashboardMetric();
        const buttons = this.dashboardViews().map((view) => {
            const selected = view.id === active;
            return `
                <button type="button" role="tab" class="dashboard-switch-btn${selected ? ' is-active' : ''}"
                        id="dashboard-tab-${view.id}" aria-selected="${selected ? 'true' : 'false'}"
                        aria-controls="dashboard-view-${view.id}" tabindex="${selected ? '0' : '-1'}"
                        data-dashboard-metric="${view.id}">${this.escapeHtml(I18n.__(view.label))}</button>`;
        }).join('');
        return `<div class="dashboard-switch ops-seg" role="tablist" data-dashboard-switch="true"
                     aria-label="${this.escapeHtml(I18n.__('dashboard'))}">${buttons}</div>`;
    },

    /** The one view in effect, wrapped as the tabpanel its tab points at. */
    dashboardViewHtml(data) {
        const views = this.dashboardViews();
        const view = views.find((candidate) => candidate.id === this.dashboardMetric()) || views[0];
        const panel = data ? data[view.id] : null;
        const readable = panel !== null && panel !== undefined;
        return `
            <section class="ui-card dashboard-panel" data-dashboard-panel="${view.id}" role="tabpanel"
                     id="dashboard-view-${view.id}" aria-labelledby="dashboard-tab-${view.id}">
                <div class="ui-section-head">
                    <h2 class="ui-section-title">${this.escapeHtml(I18n.__(view.title))}</h2>
                    <p class="ui-section-note dashboard-lead">${this.escapeHtml(I18n.__(view.hint))}</p>
                </div>
                ${readable
                    ? this.dashboardViewBodyHtml(view.id, panel)
                    : `<p class="ui-note is-warn" data-dashboard-unreadable="${view.id}">${this.OPS_ICONS.alert}<span>${this.escapeHtml(I18n.__('dashboardUnreadable'))}</span></p>`}
            </section>`;
    },

    /** The body of one view, by id. The renderers themselves are unchanged - one panel, one read. */
    dashboardViewBodyHtml(id, panel) {
        if (id === 'now') return this.dashboardNowBodyHtml(panel);
        if (id === 'people') return this.dashboardPeopleBodyHtml(panel);
        if (id === 'places') return this.dashboardPlacesBodyHtml(panel);
        if (id === 'period') return this.dashboardPeriodBodyHtml(panel);
        return this.dashboardWaitingBodyHtml(panel);
    },

    /** "Counted at ..." - and what this screen will *not* do unless it is asked. */
    dashboardStampText(data) {
        const stamp = data && data.as_of ? String(data.as_of) : '';
        return stamp
            ? I18n.__('dashboardAsOf').replace('{stamp}', stamp)
            : I18n.__('dashboardUnreadable');
    },

    /**
     * Whether this session is offered a tab, before a link to it is drawn.
     *
     * ``adminVisibleTabs`` is the nav's own answer to that question, so a panel cannot
     * offer a link the rail does not have. The alert queue is the case that matters: the
     * server answers ``null`` for its count to anybody but the root tier, and the tab is
     * ``rootOnly``, so a link there would be a dead end for the person holding the phone.
     */
    dashboardTabOffered(tab) {
        try {
            return adminVisibleTabs().some((entry) => entry.id === tab);
        } catch (err) {
            return false;
        }
    },

    /** A link into the tab that owns a queue. Empty where this session is not offered it. */
    dashboardLinkHtml(tab, labelKey) {
        if (!this.dashboardTabOffered(tab)) return '';
        const label = I18n.__(labelKey).replace('{tab}', I18n.__(adminTabRecord(tab).key));
        return `<p class="dashboard-link"><button type="button" class="ui-btn ui-btn-sm" data-dashboard-go="${this.escapeHtml(tab)}">${this.escapeHtml(label)}</button></p>`;
    },

    /**
     * One facts grid, with the labels translated rather than the payload's field names.
     *
     * ``devFactsHtml`` prints the server's own keys, which is right on the Developer tab -
     * an operator reading a diagnostic wants the name the database uses. This screen is read
     * by an administrator, so each field is drawn under the words the four tables give it;
     * the field name survives as the ``data-`` hook, so the markup still says which number
     * it is for anybody reading the page source or a failing test.
     */
    dashboardFactsHtml(fields, source) {
        const cells = fields
            .filter(([field]) => source[field] !== undefined && source[field] !== null)
            .map(([field, labelKey]) =>
                `<span class="ui-fact"><span class="ui-fact-sub">${this.escapeHtml(I18n.__(labelKey))}</span>` +
                `<span class="ui-fact-value" data-dashboard-fact="${this.escapeHtml(field)}">${this.escapeHtml(String(source[field]))}</span></span>`)
            .join('');
        return `<div class="ui-facts dashboard-facts">${cells}</div>`;
    },

    /**
     * "26h 10m" / "3d 4h" - how long the oldest thing in the queues has been waiting.
     *
     * Below a day it is the board's own short duration, which exists and already reads well
     * ("{hours}h {minutes}m", "{minutes}m"); above a day, the same shape in days, because
     * "73h 12m" is a figure a reader has to divide before it means anything.
     */
    dashboardWaitLabel(seconds) {
        const total = Math.max(0, Number(seconds) || 0);
        const days = Math.floor(total / 86400);
        if (days >= 1) {
            const hours = Math.floor((total % 86400) / 3600);
            return I18n.__('dashboardDaysShort')
                .replace('{days}', String(days))
                .replace('{hours}', String(hours));
        }
        return this.liveOpsDuration(total);
    },

    /**
     * Bind the tab's controls after it is painted.
     *
     * One pass over the ``data-`` hooks the markup carries, so no inline ``onclick`` joins
     * the ones the document policy already tolerates, and a repaint between paint and tap
     * cannot orphan a handler. The guard is the one every binder here carries: a stub DOM
     * without ``querySelectorAll`` must be able to render the tab without throwing.
     */
    bindDashboardControls(content) {
        if (!content || typeof content.querySelectorAll !== 'function') return;
        content.querySelectorAll('[data-dashboard-go]').forEach((button) => {
            if (typeof button.addEventListener !== 'function') return;
            button.addEventListener('click', () => UI.renderAdminTab(String(button.getAttribute('data-dashboard-go') || '')));
        });
        content.querySelectorAll('[data-dashboard-refresh]').forEach((button) => {
            if (typeof button.addEventListener !== 'function') return;
            // A repaint *is* the refresh: the tab re-reads on entry and drops its cached
            // copy first, so one tap is one request and one honest stamp.
            button.addEventListener('click', () => UI.renderAdminTab('Dashboard'));
        });
        // The view switcher. A tap redraws from the snapshot already in hand rather than
        // re-reading: which card is on top changes not one figure, so a request behind this tap
        // would be a request nobody asked for.
        content.querySelectorAll('[data-dashboard-metric]').forEach((button) => {
            if (typeof button.addEventListener !== 'function') return;
            button.addEventListener('click', () => UI_MODULES.dashboardSetMetric(
                button.getAttribute('data-dashboard-metric')
            ));
            // A ``tablist`` promises the arrow keys, and the roving ``tabindex`` puts only the
            // selected one in the tab order - so without this the views behind the first would be
            // unreachable from a keyboard, which is a control that only exists for a mouse.
            button.addEventListener('keydown', (event) => UI_MODULES.dashboardMetricKey(event));
        });
        // The period panel's window. Choosing one re-reads the tab: the figures are the
        // server's, so a window changed here has to be a window re-counted there.
        content.querySelectorAll('[data-dashboard-preset]').forEach((button) => {
            if (typeof button.addEventListener !== 'function') return;
            button.addEventListener('click', () => UI_MODULES.dashboardSetPeriod(
                String(button.getAttribute('data-dashboard-preset') || '')
            ));
        });
        // One person's own attendance, over the period the figure beside them came from. The
        // window travels in the markup rather than being read back off the payload, so the link
        // cannot describe a different period than the number it sits under.
        content.querySelectorAll('[data-dashboard-worker]').forEach((button) => {
            if (typeof button.addEventListener !== 'function') return;
            button.addEventListener('click', () => UI_MODULES.dashboardOpenWorker(
                button.getAttribute('data-dashboard-worker'),
                button.getAttribute('data-dashboard-start'),
                button.getAttribute('data-dashboard-end')
            ));
        });
    },

    /**
     * One tap on a view: remember it, then redraw it from the snapshot already in hand.
     *
     * A switch is not a refresh. The payload on this screen is one counted read, and which card
     * is on top changes none of its figures - so re-asking the server here would be a request per
     * tap for an answer this page already holds. The stamp stays where it was for the same
     * reason: nothing was re-counted, so nothing may claim it was.
     */
    dashboardSetMetric(id) {
        const views = this.dashboardViews();
        const wanted = String(id === null || id === undefined ? '' : id);
        if (!views.some((view) => view.id === wanted)) return undefined;
        State.dashboardMetric = wanted;
        // No host means no shell on the screen to redraw - the tab was left behind, or the read
        // failed before there was one. The remembered choice still stands for the next paint.
        const host = this._dashboardHost;
        if (!host || typeof host !== 'object') return undefined;
        host.innerHTML = this.dashboardShellHtml(this._dashboard);
        this.bindDashboardControls(host);
        // Focus follows the selection, which is what a keyboard reader just asked for: the redraw
        // replaced the element that had focus, so it has to be placed by hand.
        if (typeof host.querySelector === 'function') {
            const tab = host.querySelector(`#dashboard-tab-${wanted}`);
            if (tab && typeof tab.focus === 'function') tab.focus();
        }
        return undefined;
    },

    /**
     * The arrow keys a ``tablist`` promises: the arrows walk and wrap, Home and End jump.
     *
     * Left and right follow the order the tabs are *drawn* in rather than a physical direction,
     * which is the behaviour that survives the RTL layout: the browser reverses the strip, and the
     * key that meant "the next one" still means "the next one".
     */
    dashboardMetricKey(event) {
        const key = event ? String(event.key || '') : '';
        const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 };
        const views = this.dashboardViews();
        let index = null;
        if (steps[key]) {
            const current = views.findIndex((view) => view.id === this.dashboardMetric());
            index = (Math.max(0, current) + steps[key] + views.length) % views.length;
        } else if (key === 'Home') {
            index = 0;
        } else if (key === 'End') {
            index = views.length - 1;
        }
        if (index === null) return undefined;
        // Only once the key is known to be ours: a page that swallowed every keystroke to move a
        // tab would break the scroll keys on the panel underneath.
        if (typeof event.preventDefault === 'function') event.preventDefault();
        return this.dashboardSetMetric(views[index].id);
    },

    /** One tap on a period window: remember it, then re-count the tab for it. */
    dashboardSetPeriod(token) {
        State.dashboardDays = String(token || '').trim();
        return UI.renderAdminTab('Dashboard');
    },

    /**
     * Open one person's own attendance, over the window the figure came from.
     *
     * The plan asks for the extremes as a *link into the context* rather than as a leaderboard, and
     * the context lives in the Shifts tab: its rows for one worker, over one period, which is the
     * same window this panel counted. So the tap carries the person and the two dates - the same
     * two fields the tab's own period picker writes - and clears any category filter left over
     * from an earlier visit, because a stale filter would hide the very rows the link promised.
     */
    dashboardOpenWorker(workerId, start, end) {
        const id = String(workerId === null || workerId === undefined ? '' : workerId).trim();
        if (!id) return undefined;
        const from = String(start || '').trim();
        const to = String(end || '').trim();
        // A window the tab would refuse is worse than the tab's own: only a usable pair is
        // adopted, and the dates are compared as ISO text, which is what they are.
        if (from && to && from <= to) State.shiftsRange = { start: from, end: to };
        State.shiftsQuery = id;
        State.shiftsCategory = '';
        return UI.renderAdminTab('Shifts');
    },

    /**
     * Waiting on a person - the first view, because it is the only one that changes behaviour.
     *
     * Each count is the predicate its own screen uses, so the dashboard and the queue it
     * names cannot disagree: the review codes are ``reports.PENDING_CODES``, the
     * applications are ``registrations.STATUS_PENDING_APPROVAL`` (the quarantine a submission
     * files and an approval lifts), the notes are ``notes.OPEN_STATUSES``
     * - all decided server-side, in the query, and this screen draws what it is handed.
     */
    dashboardWaitingBodyHtml(waiting) {
        const queues = [
            ['reviews', 'dashboardReviews', 'Approvals'],
            ['registrations', 'dashboardRegistrations', 'Registrations'],
            ['notes', 'dashboardNotes', 'Notes']
        ];
        // The alert queue is drawn only where its count is *readable*: the server answers
        // ``null`` to every role but the root tier, and ``null`` here means "not yours to
        // read" rather than zero - so a row of 0 would say "nothing is waiting" about a
        // surface this reader cannot open at all. The tab check is the other half of the
        // same question, and both are asked because they are answered by different things.
        if (waiting.alerts !== null && waiting.alerts !== undefined && this.dashboardTabOffered('Alerts')) {
            queues.push(['alerts', 'dashboardAlerts', 'Alerts']);
        }
        const rows = queues.map(([field, labelKey, tab]) => `
            <div class="dashboard-queue" data-dashboard-queue="${field}">
                <span class="dashboard-queue-value" data-dashboard-fact="${field}">${this.escapeHtml(String(waiting[field] ?? 0))}</span>
                <span class="dashboard-queue-label">${this.escapeHtml(I18n.__(labelKey))}</span>
                ${this.dashboardTabOffered(tab)
                    ? `<button type="button" class="ui-btn ui-btn-sm" data-dashboard-go="${tab}">${this.escapeHtml(I18n.__('dashboardOpenTab').replace('{tab}', I18n.__(adminTabRecord(tab).key)))}</button>`
                    : ''}
            </div>`).join('');
        return `
            <div class="dashboard-queues">${rows}</div>
            ${this.dashboardOldestHtml(waiting)}`;
    },

    /**
     * The one figure that decides whether somebody works the queues now or after lunch.
     *
     * It is the *maximum* the server computed rather than a sum or an average: an average
     * across four queues describes none of them, and the question this answers is "has
     * anything been sitting here too long".
     */
    dashboardOldestHtml(waiting) {
        const seconds = waiting.oldest_seconds;
        if (seconds === null || seconds === undefined) {
            // Not "0": nothing waiting has no oldest item at all, and an age of zero would
            // say something was filed a second ago.
            return `<p class="ui-note" data-dashboard-oldest="none">${this.escapeHtml(I18n.__('dashboardNothingWaiting'))}</p>`;
        }
        const label = I18n.__('dashboardOldest').replace('{waiting}', this.dashboardWaitLabel(seconds));
        return `<p class="ui-note" data-dashboard-oldest="${this.escapeHtml(String(seconds))}">${this.OPS_ICONS.clock}<span>${this.escapeHtml(label)}</span></p>`;
    },

    /**
     * Right now - the numbers that say whether the board next door is normal.
     *
     * Live Ops answers *who* is on site, in a list that polls. This answers *how many, and is
     * that normal*: the same session figure the board holds, plus the three ways a punch can be
     * waiting - a phone handed it over and it is not a record yet, the deployment refused it, or
     * the shift has run past the overtime line with nobody having answered for it.
     *
     * The distinction is drawn in the words rather than left to the reader: ``refused_24h`` is a
     * *rate* to watch, because a refused punch has no triage state anybody can clear
     * (``refused_punches`` owns that decision), and the note under the rows says so.
     */
    dashboardNowBodyHtml(moment) {
        // The tab each figure belongs to, where one exists. ``on_shift`` is the board's own
        // reading, so it links to the board; an open crossing is an Approvals decision; the
        // refusals are the root tier's to read. ``offline_waiting`` deliberately has no tab: the
        // punch queue is triaged at ``/admin/punch_queue``, which no screen in this console
        // lists, and a button into a tab that does not show the rows is worse than no button.
        const rows = [
            ['on_shift', 'dashboardOnShift', 'Live Ops'],
            ['overtime_open', 'dashboardOvertimeOpen', 'Approvals'],
            ['offline_waiting', 'dashboardOfflineWaiting', null],
            ['refused_24h', 'dashboardRefused24h', 'Developer']
        ].map(([field, labelKey, tab]) => `
            <div class="dashboard-queue" data-dashboard-now="${field}">
                <span class="dashboard-queue-value" data-dashboard-fact="${field}">${this.escapeHtml(String(moment[field] ?? 0))}</span>
                <span class="dashboard-queue-label">${this.escapeHtml(I18n.__(labelKey))}</span>
                ${tab && this.dashboardTabOffered(tab)
                    ? `<button type="button" class="ui-btn ui-btn-sm" data-dashboard-go="${tab}">${this.escapeHtml(I18n.__('dashboardOpenTab').replace('{tab}', I18n.__(adminTabRecord(tab).key)))}</button>`
                    : ''}
            </div>`).join('');
        // Who, by site: the same rows the total counts, grouped by the server rather than here,
        // so the split and the total cannot disagree. A site name is text somebody typed, so it
        // is escaped like every other value off the wire.
        const sites = (Array.isArray(moment.by_site) ? moment.by_site : []).map((entry) => `
            <span class="ui-fact"><span class="ui-fact-sub">${this.escapeHtml(entry.site_name)}</span><span class="ui-fact-value" data-dashboard-now-site="${this.escapeHtml(entry.site_name)}">${this.escapeHtml(String(entry.workers))}</span></span>`).join('');
        return `
            <div class="dashboard-queues">${rows}</div>
            <p class="ui-section-note dashboard-sub">${this.escapeHtml(I18n.__('dashboardNowBySite'))}</p>
            ${sites
                ? `<div class="ui-facts dashboard-facts" data-dashboard-onsite="true">${sites}</div>`
                : `<p class="ui-note" data-dashboard-nobody="true">${this.escapeHtml(I18n.__('dashboardNowNobody'))}</p>`}
            <p class="ui-note" data-dashboard-refused-note="true">${this.OPS_ICONS.alert}<span>${this.escapeHtml(I18n.__('dashboardNowRefusedNote'))}</span></p>`;
    },

    /**
     * People - the roster as this sign-in can see it.
     *
     * The root account is not checked for here, and must not be: ``visibility_clause``
     * excludes it *inside* the query the server ran, because a filter applied on this side
     * still leaves the count of what it removed on the wire - and a count is an enumeration.
     */
    dashboardPeopleBodyHtml(people) {
        // A fixed order rather than the payload's own: worker first, then the two lead
        // roles, then the administrative ones - the order an administrator reads a roster
        // in, and the one the Credentials form offers roles in. A role with no accounts is
        // left out, because a row of zeroes is not what anybody came to this panel for.
        const order = ['worker', 'moallem', 'off_office', 'admin', 'head_admin'];
        const counts = people.by_role || {};
        const roles = order
            .filter((role) => counts[role] !== undefined && counts[role] !== null)
            .map((role) =>
                `<span class="ui-fact"><span class="ui-fact-sub">${this.escapeHtml(this.roleLabel(role))}</span>` +
                `<span class="ui-fact-value" data-dashboard-role="${this.escapeHtml(role)}">${this.escapeHtml(String(counts[role]))}</span></span>`)
            .join('');
        return `
            ${this.dashboardFactsHtml([
                ['accounts', 'dashboardAccounts'],
                ['active', 'dashboardActive'],
                // The quarantine, named: an account the public form created and nobody has
                // decided. Drawn beside "deactivated" and never inside it - they are opposite
                // facts, and the roster of a deployment with a queue is not a roster that has
                // been switched off.
                ['pending_approval', 'dashboardPendingApproval'],
                ['deactivated', 'dashboardDeactivated'],
                ['enrolled', 'dashboardEnrolled'],
                ['no_face', 'dashboardNoFace'],
                ['no_password', 'dashboardNoPassword'],
                ['new_this_week', 'dashboardNewThisWeek'],
                ['never_clocked_in', 'dashboardNeverClockedIn']
            ], people)}
            ${roles ? `<p class="ui-section-note dashboard-sub">${this.escapeHtml(I18n.__('dashboardByRole'))}</p>
            <div class="ui-facts dashboard-facts" data-dashboard-roles="true">${roles}</div>` : ''}
            ${this.dashboardLinkHtml('Credentials', 'dashboardPeopleLink')}`;
    },

    /** Places - where a clock-in is allowed from, and who has reached none of them today. */
    dashboardPlacesBodyHtml(places) {
        // A category name and a site name are both text somebody typed, and the site list
        // below is the one *list* of server values this page draws - so both are escaped
        // like every other value off the wire. (The server refuses markup at the boundary
        // now; these rows predate that, and a read path does not get to assume.)
        const categories = (places.by_category || []).map((entry) =>
            `<span class="ui-fact"><span class="ui-fact-sub">${this.escapeHtml(entry.category)}</span>` +
            `<span class="ui-fact-value" data-dashboard-category="${this.escapeHtml(entry.category)}">${this.escapeHtml(String(entry.sites))}</span></span>`)
            .join('');
        const unmanned = places.unmanned_today || [];
        return `
            ${this.dashboardFactsHtml([
                ['sites', 'dashboardSites'],
                ['categories', 'dashboardCategories'],
                ['no_category', 'dashboardNoCategory'],
                ['overriding_window', 'dashboardOverridingWindow']
            ], places)}
            ${categories ? `<p class="ui-section-note dashboard-sub">${this.escapeHtml(I18n.__('dashboardByCategory'))}</p>
            <div class="ui-facts dashboard-facts" data-dashboard-categories="true">${categories}</div>` : ''}
            <p class="ui-section-note dashboard-sub" data-dashboard-unmanned-count="${unmanned.length}">${this.escapeHtml(I18n.__('dashboardUnmanned'))}</p>
            ${unmanned.length === 0
                ? `<p class="ui-note" data-dashboard-all-manned="true">${this.escapeHtml(I18n.__('dashboardUnmannedNone'))}</p>`
                : `<div class="dashboard-sites" data-dashboard-unmanned-list="true">${unmanned.map((site) =>
                    `<span class="ui-badge is-warn" data-dashboard-unmanned-site="${this.escapeHtml(site)}">${this.OPS_ICONS.alert}<span>${this.escapeHtml(site)}</span></span>`).join('')}</div>`}
            ${this.dashboardLinkHtml('Sites', 'dashboardPlacesLink')}`;
    },

    /**
     * The period - the one view that is about a window rather than a moment.
     *
     * Everything in it is the server's (``reports.attendance_period``, the counted twin of the
     * attendance report): the days people were present, the days the site was open, the arrivals
     * that landed late, the hours an administrator has approved and the hours still waiting on
     * one. Nothing is added up here - a total computed in the browser would be a fourth copy of
     * arithmetic that already exists on the server twice.
     *
     * The window itself is drawn with the figures (``start``, ``end``, ``preset``) for the reason
     * the stamp is drawn: a summary has to say what it is a summary *of*. It is also what makes
     * the two extremes links to a period rather than to a person's whole history.
     */
    dashboardPeriodBodyHtml(period) {
        return `
            ${this.dashboardPeriodRangeHtml(period)}
            ${this.dashboardFactsHtml([
                ['workers', 'dashboardPeriodWorkers'],
                ['present_days', 'dashboardPeriodPresentDays'],
                ['expected_days', 'dashboardPeriodExpectedDays'],
                ['late_arrivals', 'dashboardPeriodLate'],
                ['average_attendance_rate', 'dashboardPeriodRate'],
                ['approved_hours', 'dashboardPeriodApprovedHours'],
                // Beside the approved figure and named for what it is: the part of those hours
                // that needed somebody's decision, never hours worked twice.
                ['overtime_hours', 'dashboardPeriodOvertimeHours'],
                ['awaiting_approval_hours', 'dashboardPeriodAwaitingHours']
            ], period)}
            ${this.dashboardDaysHtml(period)}
            ${this.dashboardExtremesHtml(period)}`;
    },

    /**
     * The window control: two presets, the one in effect marked, and the dates it resolved to.
     *
     * Buttons rather than the two date inputs the Shifts tab carries, because this panel offers
     * *two questions* - "how is this week going" and "how did this month go" - and a date picker
     * would answer neither in one tap. The server counts the window either way.
     */
    dashboardPeriodRangeHtml(period) {
        const active = String(period.preset || '');
        const presets = [
            {
                key: 'days',
                // The label names the *window the button asks for*, never the one on screen:
                // under the month preset, a button reading "last 30 days" would be describing
                // the figures rather than what tapping it does.
                label: I18n.__('dashboardPeriodPresetDays')
                    .replace('{days}', String(this.DASHBOARD_PERIOD_DAYS))
            },
            { key: 'month', label: I18n.__('dashboardPeriodPresetMonth') }
        ];
        const window = I18n.__('dashboardPeriodWindow')
            .replace('{start}', String(period.start || ''))
            .replace('{end}', String(period.end || ''));
        return `
            <div class="ui-row dashboard-period-range" data-dashboard-period-range="true">
                ${presets.map((preset) => `
                    <button type="button" class="ui-chip" data-dashboard-preset="${this.escapeHtml(preset.key)}"
                            ${preset.key === active ? 'aria-pressed="true"' : ''}>${this.escapeHtml(preset.label)}</button>`).join('')}
                <span class="ui-note" data-dashboard-period-window="true">${this.escapeHtml(window)}</span>
            </div>`;
    },

    /**
     * The shape of the window: one column per day, the bar its present days, the numeral its late
     * arrivals.
     *
     * This is the one question on this screen that a single number cannot answer. "132 present
     * days" and "4 late arrivals" say how the window went; they do not say *which* days it came
     * apart on, and "the whole week was short-staffed" and "Tuesday was short-staffed" are
     * different problems with different fixes. The counted twin answers it in the same read
     * (``by_day``), so drawing it costs no request at all.
     *
     * The bars are scaled against the busiest day *in the window* rather than against the roster:
     * an absolute scale would make a quiet deployment look like a dead one, and the question here
     * is the shape of this window. The busiest day is named in words above the strip, because a
     * chart whose scale is only implied is a chart nobody can read - and colour never carries the
     * meaning alone: a late arrival is printed as a numeral on the day it happened, and every
     * column states its own date and both figures, so the strip is readable without the picture.
     */
    dashboardDaysHtml(period) {
        const days = Array.isArray(period.by_day) ? period.by_day : [];
        const caption = I18n.__('dashboardPeriodDays')
            .replace('{days}', String(days.length))
            .replace('{busiest}', String(days.reduce((most, entry) => Math.max(most, Number(entry.present) || 0), 0)));
        if (days.length === 0) {
            return `<p class="ui-note" data-dashboard-days-none="true">${this.escapeHtml(I18n.__('dashboardPeriodNoDays'))}</p>`;
        }
        // The floor is the divisor, never the answer: a window nobody worked draws its columns
        // empty rather than dividing by zero.
        const scale = Math.max(1, days.reduce((most, entry) => Math.max(most, Number(entry.present) || 0), 0));
        const columns = days.map((entry) => {
            const present = Number(entry.present) || 0;
            const late = Number(entry.late) || 0;
            const day = String(entry.day || '');
            // A day with nobody on site draws no bar at all rather than a two-pixel stub: "nobody
            // came in" and "one person came in" have to be different pictures.
            const bar = present > 0
                ? `<span class="dashboard-day-bar" style="height: ${this.dashboardDayHeight(present, scale)}%"></span>`
                : '<span class="dashboard-day-bar is-empty"></span>';
            return `
                <li class="dashboard-day${late > 0 ? ' is-late' : ''}" data-dashboard-day="${this.escapeHtml(day)}"
                    data-dashboard-day-present="${this.escapeHtml(String(present))}"
                    aria-label="${this.escapeHtml(this.dashboardDayLabel(entry, present, late))}">${late > 0
                        ? `<span class="dashboard-day-late" data-dashboard-day-late="${this.escapeHtml(day)}">${this.escapeHtml(String(late))}</span>`
                        : ''}<span class="dashboard-day-track">${bar}</span><span class="dashboard-day-tick">${this.escapeHtml(this.dashboardDayTick(day))}</span></li>`;
        }).join('');
        // ``role="list"`` is not decoration: a markerless list loses its list semantics in Safari
        // and VoiceOver, which would leave the columns as unlabelled spans.
        return `
            <p class="ui-section-note dashboard-sub" data-dashboard-days-caption="true">${this.escapeHtml(caption)}</p>
            <ol class="dashboard-days" role="list" data-dashboard-days="true" aria-label="${this.escapeHtml(caption)}">${columns}</ol>`;
    },

    /** One day's height, as a share of the busiest day in the window. */
    dashboardDayHeight(present, scale) {
        const share = Math.round((Number(present) || 0) / scale * 100);
        // A day with somebody in it always draws something: a bar rounded down to 0% would read as
        // a day nobody came in, which is the one thing this strip must not say by accident.
        return Math.min(100, Math.max(8, share));
    },

    /** What one column *is*, in words - the strip for a reader who cannot see a bar. */
    dashboardDayLabel(entry, present, late) {
        return I18n.__('dashboardPeriodDayAria')
            .replace('{day}', String(entry.day || ''))
            .replace('{present}', String(present))
            .replace('{late}', String(late));
    },

    /** The last two digits of the date, under the column - enough to find a bar on a 31-day strip. */
    dashboardDayTick(day) {
        return String(day || '').slice(-2);
    },

    /**
     * The two people worth opening: the least present, and the most often late.
     *
     * Each one is one tap from the context it came from - the person's own rows in the Shifts
     * tab, over this same window - which is the whole difference between this and a leaderboard:
     * a ranking tells a reader that somebody is at the bottom, and a link lets them find out why.
     * Two groups rather than one merged list because "never here" and "always late" are different
     * facts about a person, and the second is the one worth answering first.
     */
    dashboardExtremesHtml(period) {
        const expected = Number(period.expected_days || 0);
        const groups = [
            { name: 'quietest', labelKey: 'dashboardPeriodQuietest', figure: (row) => row.rate },
            { name: 'most_late', labelKey: 'dashboardPeriodMostLate', figure: (row) => row.late }
        ];
        return groups.map((group) => {
            const rows = Array.isArray(period[group.name]) ? period[group.name] : [];
            const body = rows.length === 0
                ? `<p class="ui-note" data-dashboard-extreme-none="${group.name}">${this.escapeHtml(I18n.__('dashboardPeriodNone'))}</p>`
                : `<div class="dashboard-queues" data-dashboard-extreme="${group.name}">${rows
                    .map((row) => this.dashboardExtremeRowHtml(row, group, period, expected))
                    .join('')}</div>`;
            return `
                <p class="ui-section-note dashboard-sub">${this.escapeHtml(I18n.__(group.labelKey))}</p>
                ${body}`;
        }).join('');
    },

    dashboardExtremeRowHtml(row, group, period, expected) {
        const id = String(row.worker_id || '');
        // The name if the account still has one, the id if it does not: the days were worked
        // either way, and a row that printed a blank over a deleted account would read as a
        // rendering bug rather than as somebody whose login is gone.
        const who = row.worker_name ? String(row.worker_name) : id;
        const sub = I18n.__('dashboardPeriodExtremeSub')
            .replace('{days}', String(Number(row.present_days || 0)))
            .replace('{expected}', String(expected))
            .replace('{late}', String(Number(row.late || 0)));
        // The link exists only where the tab that owns those rows is offered - the same guard
        // every other link on this screen carries, so a control can never lead to a tab the
        // rail does not have.
        const link = this.dashboardTabOffered('Shifts')
            ? `<button type="button" class="ui-btn ui-btn-sm" data-dashboard-worker="${this.escapeHtml(id)}"`
                + ` data-dashboard-start="${this.escapeHtml(String(period.start || ''))}"`
                + ` data-dashboard-end="${this.escapeHtml(String(period.end || ''))}">`
                + `${this.escapeHtml(I18n.__('dashboardPeriodOpenWorker').replace('{tab}', I18n.__(adminTabRecord('Shifts').key)))}</button>`
            : '';
        return `
            <div class="dashboard-queue" data-dashboard-extreme-row="${this.escapeHtml(id)}">
                <span class="dashboard-queue-value" data-dashboard-extreme-figure="${this.escapeHtml(id)}">${this.escapeHtml(String(group.figure(row) ?? 0))}</span>
                <span class="dashboard-queue-label">${this.escapeHtml(who)}</span>
                <span class="ui-note" data-dashboard-extreme-sub="${this.escapeHtml(id)}">${this.escapeHtml(sub)}</span>
                ${link}
            </div>`;
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
