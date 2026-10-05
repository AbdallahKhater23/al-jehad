// What every worker's phone reads: the English table for the worker screens, the loader, and
// the language switch.
//
// TWO SPLITS, for the same reason. Arabic, Hindi and Urdu used to be in this file, which every
// session loads - two thirds of a 127 KB first-party download was a table a worker on site
// cellular would never read; they are in ``i18n.{ar,hi,ur}.js`` now, fetched the first time a
// reader asks for one. The console's own strings were the other half of the same problem: an
// administrator's screens are the only place most of these keys are ever read, and every
// worker's phone was downloading them. They are in ``admin_i18n.js`` now, fetched beside
// ``admin_modules.js`` by the one session that can reach a console screen.
//
// What stays here is only what a worker path can reach - named directly, through
// ``codeLabel(namespace, code)``, or through ``I18n.__p(base, count)``.
// ``backend/tests/test_frontend_payload.py`` is what keeps that true, and it is also what
// proves the chunks still carry the tables that used to live here, unchanged.

const TRANSLATIONS = {
    en: {
        "title": "Site Attendance",
        "loginIntro": "Site attendance for crews on site. Sign in with the ID, the email or phone number on your account, and the password for your account - the one you chose, or the one an administrator gave you.",
        "loginHelp": "Password not working? Ask an administrator to set a new one - your sessions close everywhere when they do.",
        "companyFooter": "{brand} - stone, marble and granite since 1983.",
        "login": "Sign In",
        "userId": "User ID",
        "emailOrPhone": "Email or Phone",
        "password": "Password",
        "logout": "Logout",
        "theme": "Theme",
        "lang": "Language",
        "pending": "Pending Attendance Reviews",
        "activeShifts": "Active Shifts",
        "forceOut": "Force Out",
        "forceIn": "Force In",
        "forceOutTitle": "Force out {name}",
        "forceOutHint": "The shift is closed now. Leave hours empty to record what the clock says, or name the hours you are authorising - for example when the worker forgot to clock out and the session has run on overnight.",
        "forceOutHoursLabel": "Authorised hours (optional)",
        "forceOutHoursHint": "Empty = the clock's own figure. A named figure is recorded with your name on it; hours past the paid day still need the ordinary overtime approval.",
        "forceOutConfirm": "Force out and record",
        "pendingReviews": "Approvals",
        "sites": "Sites",
        "shifts": "Shifts",
        "shiftsPeriod": "Period",
        // The arrival column, and the count of the ones outside the window. The wording is
        // the screen's own - the punch card's "late - the window closed 12 minutes ago" is
        // written for somebody standing at a gate, not for a sheet read a month later.
        "shiftsArrival": "Arrival",
        "shiftsArrivalOnTime": "On time",
        "shiftsArrivalLate": "Late {minutes} min",
        "shiftsArrivalEarly": "Early {minutes} min",
        "shiftsArrivalUnknown": "No clock-in",
        "shiftsApprovedOnly": "One row per shift. Hours an admin has approved are counted in Approved hours; anything still waiting for a decision is left out of that figure.",
        "shiftsSheetNote": "One row per shift. The total counts every row above, including the hours of a shift that is still waiting for a decision.",
        "credentials": "Credentials",

        "notes": "Notes",
        "notesMine": "My notes",
        "notesIntro": "Ask your administrator for something, or tell them something is missing or wrong. The answer comes back here, and the record stays.",
        "notesNew": "New note",
        "notesEmpty": "No notes yet.",
        "notesEmptyHint": "Open one for a password change, a missing item or material, or a question about your hours.",
        "notesOpenHint": "These are still waiting for an answer. Wait for a reply before opening more.",
        "noteSubject": "Title",
        "noteSubjectPlaceholder": "What is this about?",
        "noteCategory": "Type",
        "noteMessage": "Message",
        "noteMessagePlaceholder": "Say what happened, and what you need.",
        "noteUrgent": "This is urgent",
        "noteSend": "Send note",
        "noteCancel": "Cancel",
        "noteReply": "Send reply",
        "noteReplyPlaceholder": "Write a message…",
        "noteClose": "Close this note",
        "noteBack": "Back to my notes",
        "noteSent": "Note sent to your administrator.",
        "noteReplySent": "Message sent.",
        "noteClosed": "Note closed.",
        "noteSubjectRequired": "Give your note a short title.",
        "noteBodyRequired": "Write a message first.",
        "noteReopened": "Sent — the note is open again.",
        "noteWaitingReply": "New reply",
        "noteFromYou": "You",
        "noteFromAdmin": "Administrator",
        "noteOpened": "Opened",
        "notePriorityHigh": "Urgent",
        "noteStatus_open": "Open",
        "noteStatus_in_progress": "Being handled",
        "noteStatus_resolved": "Resolved",
        "noteStatus_closed": "Closed",
        "noteCat_password_reset": "Password",
        "noteCat_missing_item": "Missing item or material",
        "noteCat_shift_hours": "Shift or hours",
        "noteCat_enrollment": "Face registration",
        "noteCat_working_conditions": "Working conditions",
        "noteCat_other": "Something else",
        "notesToday": "Today",
        "timeJustNow": "Just now",
        "timeMinutesAgo": "{count} min ago",
        "timeHoursAgo": "{count} h ago",
        "timeYesterday": "Yesterday",
        "timeDaysAgo": "{count} d ago",
        // The counted strings' other forms, one per CLDR cardinal category: ``I18n.__p``
        // picks by the reader's own rule, so Hindi writes घंटा for one hour and घंटे for the
        // rest, and Arabic writes the dual (بلا رقم) for two and a different noun shape for
        // three-to-ten and eleven-to-99. A language writes only the forms its rule can
        // return; the rest fall back to the plain key above, which is why the base string
        // stays the "other" form and every existing call site still reads correctly. The
        // forms are declared in all four tables even where unused: the tables are one
        // vocabulary, and a form English never selects is one Arabic can still ask for.
        "timeMinutesAgo_one": "a minute ago",
        "timeMinutesAgo_two": "two minutes ago",
        "timeMinutesAgo_few": "{count} min ago",
        "timeMinutesAgo_many": "{count} min ago",
        "timeHoursAgo_one": "an hour ago",
        "timeHoursAgo_two": "two hours ago",
        "timeHoursAgo_few": "{count} h ago",
        "timeHoursAgo_many": "{count} h ago",
        "timeDaysAgo_one": "a day ago",
        "timeDaysAgo_two": "two days ago",
        "timeDaysAgo_few": "{count} d ago",
        "timeDaysAgo_many": "{count} d ago",
        "noteResolvedHint": "Marked resolved by the administrator. Reply if it is not actually done — that reopens the note.",
        "noteClosedHint": "You closed this note.",
        // The worker's own inbox: what the system has told *them*, the other side of the
        // notes they write. The badge on the handset's clock panel and the badge on the
        // alerts tab are the same count, and the two ``alertsUnread*`` strings are the
        // banner that carries it - separate keys rather than one with a number in it,
        // because "1 new alerts" is the kind of thing a reader notices before the message.
        "alerts": "Alerts",
        "alertsMine": "My alerts",
        "alertsIntro": "What the system has told you, kept for you: a shift that ran past the paid day, or a day the system closed at the limit. The record stays here even when the notification never reached the phone.",
        "alertsEmpty": "Nothing waiting.",
        "alertsEmptyHint": "Notices about your own shift arrive here, and they stay until you have read them.",
        "alertsNew": "New",
        "alertsMarkRead": "Mark as read",
        "alertsMarkAll": "Mark all as read",
        "alertsMarkedRead": "Alert marked as read.",
        "alertsMarkedAll": "All alerts marked as read.",
        "alertsUnreadOne": "1 new alert",
        "alertsUnreadMany": "{count} new alerts",
        "alertsOpen": "See all",
        "alertsSent": "Sent",
        "alertKind_overtime_crossed": "Overtime",
        "alertKind_shift_auto_closed": "Day closed",
        "alertKind_overtime_authorised": "Overtime authorised",
        "alertKind_overtime_declined": "Overtime refused",
        // The worker's own welcome, in their own inbox: see
        // ``notifications.KIND_WORKER_ACCOUNT_APPROVED``.
        "alertKind_transit_arrived": "Arrival confirmed",
        "alertKind_account_approved": "Account approved",
        // The welcome card on the clock panel - what a worker hired through the public form
        // meets on the first screen their new account ever shows them. The instructions
        // themselves are the *server's* own notice body, printed verbatim; these are the
        // chrome around it. See ``worker_modules.welcomeHtml``.
        "welcomeTitle": "Welcome",
        "welcomeSignedIn": "You are signed in, {name}.",
        "welcomeWorkerId": "Your worker id",
        "welcomeDismiss": "Got it",
        "welcomeKept": "This stays in your alerts, so you can look it up again.",
        "pushTitle": "Push notifications",
        "pushChecking": "Checking whether this server can send notifications…",
        "pushServerOff": "This server cannot send notifications yet.",
        "pushSwUnsupported": "This browser cannot show push notifications.",
        "pushSwFailed": "Notifications could not be set up in this browser.",
        "pushOnNote": "You are notified on this device even when the app is closed.",
        "pushOffNote": "Turn on to be told here when the system has a message for you.",
        "pushEnable": "Turn on notifications",
        "pushDisable": "Turn off notifications",
        "pushEnabled": "Notifications are on for this device.",
        "pushDisabled": "Notifications are off for this device.",
        "pushPermissionDenied": "Notification permission was not granted, so nothing can be delivered.",
        "pushUnavailable": "Notifications are not available right now.",
        "pushDevices": "{count} device(s) receive your notifications",
        "corpusTitle": "Photos for face matching",
        "corpusChecking": "Checking your choice…",
        "corpusNote": "Only you can decide this. With your agreement the system keeps the photo from your punch, so face matching can be measured and made to work from further away. Nobody can agree on your behalf, your supervisor cannot see this answer, and you can stop it at any time.",
        "corpusOnNote": "You agreed. Photos from your punches are kept to measure face matching.",
        "corpusOffNote": "Not agreed. The photo from your punch is not kept for measurement.",
        "corpusEnable": "I agree",
        "corpusWithdraw": "Stop keeping my photos",
        "corpusGranted": "Thank you - your agreement is on the record.",
        "corpusWithdrawn": "Your agreement has been withdrawn.",
        "corpusUnavailable": "This could not be changed right now. Try again.",
        "roleWorker": "Worker",
        "roleMoallem": "Moallem",
        "roleOffOffice": "Off-Office Worker",
        "roleAdmin": "Administrator",
        "roleHeadAdmin": "Head administrator",
        "roleDeveloper": "Developer",
        // The root tier's own console: runtime flags, the deployment's private
        // alert hub, database diagnostics and the raw audit stream.
        "developerConsole": "Developer",
        "hintDeveloperConsole": "The root tier's own tools: runtime flags, the deployment's private alerts, database diagnostics and the raw audit stream.",
        "admin": "Admin",
        "captureSubmit": "Capture & Submit",
        "captureHint": "Fit your face in the frame, then tap the shutter.",
        // The live framing hint on the camera card: said while the shutter is still open,
        // so a bad photo is fixed before it is taken rather than reported after.
        "framingDark": "Too dark — move into the light",
        "framingBright": "Too bright — turn away from the glare",
        "framingFlat": "The camera can't see you — point it at your face",
        "framingNoFace": "No face in the frame yet",
        "framingManyFaces": "Only one person can be in the frame",
        "framingCloser": "Move a little closer",
        "framingFurther": "Move back a little",
        "framingGood": "Good — hold steady",

        // The clock-in window, on the punch card before the shutter. The server sends the
        // arithmetic (verdict + minutes) and these are what turns it into a sentence.
        "punchWindowSite": "{site} · clock-in window {window}",
        "punchWindowOnTime": "on time",
        "punchWindowEarly": "early — opens in {minutes}",
        "punchWindowLate": "late — the window closed {minutes} ago",
        "punchWindowOffSite": "You are not inside any site's radius — a clock-in from here is not recorded. Move onto the site.",
        "clockIn": "Check In",
        "clockOut": "Check Out",
        "currentlyClockedOut": "You are currently checked out.",
        "onShift": "You are on shift",
        "shiftStatus": "Shift status",
        "clockedInAt": "Checked in at",
        "overtimeNeedsApproval": "Past {hours}h paid — the overtime line is reached",
        "overtimeOpenShiftHint": "The shift stays open and keeps counting until you clock out - time past the paid day needs approval from your administrator, who has been alerted.",
        "shiftPaidDay": "Paid day",
        "shiftUnpaidBreak": "Unpaid break",
        "shiftDayComplete": "You have worked your paid day of {paid} h (about {onsite} h on site, including the unpaid break). Nothing more is recorded after it.",
        "shiftEndsNow": "Full day reached",
        "earlyClockOutTitle": "Clock out early?",
        "earlyClockOutBody": "You have worked {paid}h of the {regular}h paid day. If you clock out now, this time it will be recorded as {paid}h \u2014 not the full {regular}h day.",
        "earlyClockOutConfirm": "Confirm and clock out",
        "flaggedForReview": "A shift of yours is waiting for an admin review, so clocking out will be refused until it is cleared. Ask your supervisor to review it.",
        "transitPendingTitle": "Travel shift - waiting for a site",
        "transitPendingBody": "You are on the road. This shift stays open until you reach a site: arriving confirms it and credits the travel time. It cannot be closed from here - if you cannot reach a site, ask an administrator to close it.",
        "accountPendingTitle": "Account waiting for approval",
        "accountPendingBody": "An administrator has to approve your account before you can clock in or out. You can sign in, see your details and read your messages in the meantime.",
        "transitRequestCheckout": "Ask an administrator to close my shift",
        "transitRequestSent": "Request sent to an administrator.",
        // The arrival action on the road: the one tap that turns the "In Transit" placeholder
        // into the site the worker reached. It is the primary button while the flag is set.
        "transitArrive": "I have arrived at a site",
        // An arrival is a geofence decision the server makes, and the offline queue carries no
        // arrival - so the road state has to say why the tap cannot be saved for later.
        "transitArriveOffline": "Confirming an arrival needs a connection: the server checks the site's geofence. Move into signal and tap again.",
        // The two refusals the arrival itself can get, in the worker's language rather than the
        // server's. The endpoint answers ``{error_code, message}`` and the handset keys on the code
        // (see ``ARRIVAL_REFUSAL_KEYS``), so these are the sentences a worker actually reads.
        "transitArriveOutside": "You are not inside a site yet. An arrival is confirmed by the site's own boundary: walk inside it and tap again.",
        "transitArriveConfirmed": "This travel shift is already confirmed at a site, so there is nothing left to confirm here. Your card will show the site.",
        "hoursThisMonth": "Hours this month",
        "hours": "Hours",
        "date": "Date",
        "action": "Action",
        "time": "Time",
        "site": "Site",
        "status": "Status",
        "name": "Name",
        "role": "Role",
        "error": "Error",
        "loading": "Loading…",
        "comingSoon": "module coming soon.",
        "clock": "Clock",
        "history": "History",
        "profile": "Profile",
        "timesheetHistory": "Timesheet History",
        "noHistory": "No attendance records yet.",
        "handTimeOnShift": "Time on shift",
        //: The line under the timer: how much of the paid day is still ahead. The count is a
        //: rendered duration ("4h 46m"), not a bare number, so no language has to agree with
        //: it in number or case - which is the same ground ``shiftsCoverageDays`` stood on.
        "handPaidRemaining": "{left} left of the paid day",
        "handNoHistoryHint": "Every clock-in and clock-out you make appears here, newest first.",
        // Handset access for an account that also runs the console, and the timesheet of
        // its own that account can read and download.
        "handsetHint": "Clock in, and read your own hours",
        "backToConsole": "Back to the console",
        "myHours": "My hours",
        "myHoursDownload": "Download CSV",
        //: The paper half of the same download. Named for what the reader gets rather than
        //: for the dialog that makes it: the button opens the print dialog, and "Save as
        //: PDF" in it is what writes the file.
        "myHoursExportPdf": "PDF (print)",
        "myHoursMonth": "Month",
        "myHoursColBreak": "Break",
        "myHoursColRecorded": "Recorded hours",
        "myHoursWorked": "Worked",
        "myHoursApproved": "Approved",
        "myHoursAwaiting": "Awaiting approval",
        "myHoursOvertime": "Overtime",
        "myHoursSites": "Sites worked",
        "myHoursWhere": "Where the hours were worked",
        "myHoursNothingToExport": "Nothing to download for this period yet.",
        // The column chooser: which of the vocabulary this account's own two files carry.
        "myHoursColumns": "Columns in the file",
        "myHoursColumnsHint": "Tick what your CSV and PDF should carry. Saved to your account, so next month and any other phone agree.",
        "myHoursColumnsSave": "Save columns",
        "myHoursColumnsSaved": "Columns saved.",
        "myHoursColumnsEmpty": "Keep at least one column.",
        "handSections": "Sections",
        "handReady": "Ready",
        "handNotReady": "Needs attention",
        "handAccount": "Account",
        "handPreferences": "Preferences",
        "handThisPhone": "This phone",
        "serverUnreachable": "Cannot reach the server. Check your connection.",
        "sessionExpiredSignInAgain": "Your session expired. Sign in again.",
        "loginNoToken": "Sign-in failed: the server did not return a session token.",

        "offlineSaved": "No signal — this punch is saved on your phone.",
        "offlineQueueFailed": "Could not save this punch on the phone.",
        "offlineNoConnection": "Still offline. Queued punches stay on this phone.",
        "offlineModeHint": "You are offline. Punches are saved on this phone and sync by themselves.",
        "queuedPunches": "Punches waiting to sync",
        "queuedPunchesHint": "They upload automatically as soon as you have signal.",
        "syncNow": "Sync now",
        "nothingToSync": "Nothing to sync.",
        "syncDone": "Punches synced",
        "syncFailed": "Sync failed",
        "lastKnownStatus": "Last known shift status — offline",
        "sessionExpired": "Your session expired. Sign in again.",
        "deviceKeyLost": "This phone is registered but its signing key is missing.",
        "deviceRevoked": "This phone was revoked. An administrator must re-enable it.",
        "reRegisterDevice": "Re-register this phone",
        "deviceRegistered": "This phone can now record offline punches.",

        "deviceStatus": "Device status",
        "checkLocation": "Test location",
        "checkCamera": "Test camera",
        "location": "Location",
        "gpsReady": "Location available",
        "gpsInsecure": "Location blocked (HTTP)",
        "gpsDenied": "Location permission denied",
        "cameraReady": "Camera available",
        "cameraInsecure": "Camera blocked (HTTP)",
        "cameraUnsupported": "Camera not supported",
        "cameraOk": "Camera works ✔",
        "gettingLocation": "Getting your location…",
        "locationOk": "Location OK:",

        "insecureTitle": "This page is not secure, so GPS is blocked",
        "insecureWarning": "⚠️ Open this app over HTTPS, otherwise phones block GPS and the camera.",
        "insecureBody": "The page was opened over plain HTTP. Browsers only allow GPS and the camera on HTTPS (or on localhost). Open the HTTPS address below and accept the certificate warning once.",
        "insecureStep1": "Open the HTTPS link shown below and accept the security warning once.",
        "insecureCamera": "Open this app over HTTPS to use the camera.",
        "openThisLink": "Open this link on this device",
        "insecureTunnelHint": "If your admin gave you a public HTTPS link (for example an ngrok or Cloudflare Tunnel address), open that instead — it needs no certificate warning.",

        "gpsBlockedTitle": "Location permission is blocked",
        "gpsBlockedBody": "Your browser is refusing to share your location with this site. The steps below fix it in about 20 seconds:",
        "gpsUnavailableTitle": "Could not get a GPS fix",
        "gpsUnavailableBody": "The device could not work out where it is right now.",
        "gpsUnavailableStep1": "Make sure location/GPS is switched on in the phone settings.",
        "gpsUnavailableStep2": "Move near a window or outdoors, give it 10–20 seconds, then retry.",
        "gpsTimeoutTitle": "Location request timed out",
        "gpsTimeoutBody": "Finding your position took too long.",
        "gpsFailedTitle": "Could not read your location",
        "gpsFailedBody": "The browser did not return your coordinates.",
        "gpsUnsupported": "This browser does not support location.",

        "iosStep1": "Open Settings → Safari → Location and choose “Ask” or “Allow”.",
        "iosStep2": "In Safari tap the “aA” button in the address bar → Website Settings → Location → Allow.",
        "iosStep3": "Reload the page, then tap Retry.",
        "androidStep1": "In Chrome tap the lock / tune icon left of the address bar.",
        "androidStep2": "Choose Permissions → Location → Allow.",
        "androidStep3": "Also turn on Location in the system settings, then reload the page.",
        "desktopStep1": "Click the lock / tune icon left of the address bar.",
        "desktopStep2": "Set Location to “Allow”.",
        "desktopStep3": "Reload the page (F5), then tap Retry.",

        "cameraBlockedTitle": "Camera permission is blocked",
        "cameraMissingTitle": "No camera found",
        "cameraMissingBody": "This device has no camera available for the browser.",
        "cameraBusyTitle": "The camera is busy",
        "cameraBusyBody": "Close any other app that is using the camera, then try again.",
        "cameraFailedTitle": "Could not start the camera",
        "cameraNotReady": "The camera is not ready yet, please wait a moment.",
        "retry": "Retry location",
        "stillBlocked": "The browser is still blocking location. Follow the steps above.",
        "cancel": "Cancel",
        "close": "Close",
        "copyLink": "Copy link",
        "copied": "Copied to clipboard",
        "quickLinks": "Links",
        "forceInHint": "For a worker whose phone is dead or who forgot to clock in. The arrival is recorded as a forced clock-in, with your name on it.",
        "forceInNothing": "Every worker who can punch is already on shift.",
        "attendanceOk": "Attendance recorded",
        "attendanceError": "Attendance error",
        "liveOpsForceWorker": "Worker",
        "liveOpsForceSite": "Site",
        "liveOpsForceReady": "Available: {count}",
        "liveOpsStreamUnavailable": "The live board could not stay connected, so it is checking on a timer again.",

        "adminConsole": "Operations console",
        "navGroupOperations": "Operations",
        "navGroupPeople": "People",
        "navGroupConfig": "Configuration",
        "hintLiveOps": "Who is on site right now, and the one action that changes it.",
        "hintApprovals": "Clocked-in shifts that are waiting on your decision.",
        "hintCredentials": "Who can sign in, and with what.",
        "hintLinks": "Password-free clock links, and every punch they produced.",
        "hintNotes": "What workers, moallems and off-office workers are asking for, in writing.",
        "hintShifts": "Hours per shift over a period, ready for payroll.",
        "hintSites": "Every site the app will accept a clock-in from.",
        "hintAdmin": "The rules these figures are computed from, and administrator accounts.",

        //  The walk-up registration queue: the tab that decides who the public form just
        //  introduced us to. One block, so a reader comparing the four tables can see the
        //  whole vocabulary of the screen in one place.
        //  The console's front door - the tab every console role lands on, and the numbers
        //  behind it. One block, so a reader comparing the four tables sees the whole
        //  vocabulary of the screen in one place: the tab's own name and hint (which
        //  ``ADMIN_TABS`` reads by key), the stamp, the panel headings, every figure's label,
        //  and the sentences for the two states that are not numbers at all - a panel that
        //  could not be read, and a queue that is empty.
        "dashboard": "Dashboard",
        "hintDashboard": "What this deployment is, and what is waiting on a person.",

        "registrations": "Registrations",
        "hintRegistrations": "People who asked to join from the public form, oldest first. Approving one creates the account, and the id it was given is shown here.",
        "registrationsBadgeLabel": "applications waiting",
        "approvalsBadgeLabel": "unanswered overtime",
        "approvalsClockIn": "Clocked in",

        "adminAlerts": "Alerts",
        "hintAlerts": "What the system is telling you, and what nobody has answered yet.",
        "reload": "Reload",
        "languageUnavailable": "That language could not be downloaded. Check your connection and try again.",
        "consoleUnavailable": "The administrator console could not be loaded. Check your connection, then reload the page.",
        // The vendor's credit, which closes every page. ``{brand}`` is the wordmark in
        // BRAND - a proper noun, so it is the same string in every language and only the
        // words around it move (Hindi puts them after the name, not before).
        "poweredBy": "Powered by {brand}"
    }
};



const I18n = {
    lang: localStorage.getItem('lang') || 'en',

    //: The languages whose table lives in a file of its own, fetched on demand. English is
    //: the one that stays inline: it is what every fallback lands on, and a reader who has
    //: not chosen a language should not have to wait for a download to see the screen.
    CHUNKS: ['ar', 'hi', 'ur'],

    //: The languages that read right to left, so the whole layout mirrors for them. A list
    //: rather than a per-call branch: the second RTL language costs a line here, not an
    //: audit of every place ``dir`` is set.
    RTL: ['ar', 'ur'],

    //: Whether a table reads right to left (the current language by default).
    isRtl(lang) {
        return this.RTL.indexOf(lang || this.lang) >= 0;
    },

    //: Loads in flight, by language, so two switches in one session fetch one file.
    _loading: {},

    //: Whether the table for ``lang`` (the current one by default) is in memory - that is,
    //: whether ``__`` can answer in it right now.
    has(lang) {
        return !!TRANSLATIONS[lang || this.lang];
    },

    /**
     * The table for ``lang``, fetching ``i18n.<lang>.js`` the first time it is needed.
     *
     * Resolves either way, and deliberately: a language file that cannot be fetched - no
     * connection, a captive portal that answers 200 with a login page - is not a broken
     * app. The reader keeps reading what is in memory, and ``setLang`` below is what says
     * so out loud. A rejected promise here would surface as an unhandled rejection in the
     * boot panel, which names the wrong problem entirely.
     */
    load(lang) {
        const code = lang || this.lang;
        if (this.has(code) || this.CHUNKS.indexOf(code) < 0) return Promise.resolve();
        if (this._loading[code]) return this._loading[code];
        this._loading[code] = new Promise((resolve) => {
            const script = document.createElement('script');
            script.src = 'i18n.' + code + '.js';
            script.addEventListener('load', resolve);
            script.addEventListener('error', () => {
                // Only the failed attempt is forgotten, so switching back retries it
                // instead of remembering a dead end for the rest of the session.
                delete this._loading[code];
                resolve();
            });
            (document.head || document.body).appendChild(script);
        });
        return this._loading[code];
    },

    setLang(lang) {
        const previous = this.lang;
        this.lang = lang;
        localStorage.setItem('lang', lang);
        // Arabic and Urdu read right-to-left; flip the document so the layout mirrors.
        document.documentElement.setAttribute('dir', this.isRtl(lang) ? 'rtl' : 'ltr');
        document.documentElement.setAttribute('lang', lang);
        // The table may be a file this tab has never opened, and repainting before it
        // lands would flash the whole screen in the language the reader just left - the
        // one thing choosing a language is supposed to prevent. So: fetch, then repaint.
        if (this.has(lang)) return UI.renderApp();
        return this.load(lang).then(() => {
            if (!this.has(lang)) {
                // Nothing arrived. Put the reader back where they were and say so: a
                // selector reading AR over English text looks like a broken translation,
                // which is harder to act on than a sentence explaining the connection.
                this.lang = previous;
                localStorage.setItem('lang', previous);
                this.applyDirection();
                if (typeof Toast !== 'undefined') Toast.error(I18n.__('languageUnavailable'));
            }
            return UI.renderApp();
        });
    },
    __(key) {
        const table = TRANSLATIONS[this.lang] || TRANSLATIONS.en;
        return table[key] || TRANSLATIONS.en[key] || key;
    },

    /**
     * The plural category ``count`` falls in, for ``lang`` - CLDR's cardinal categories,
     * trimmed to the whole numbers this app counts in.
     *
     * Three of the four languages split at one, and differently: English and Urdu say the
     * same words for one and for the rest, Hindi says "एक घंटा" for one and "घंटे" for
     * everything else (a count of zero reads as the singular noun there, so zero shares
     * ``one``). Arabic is the one that needs the whole set - its dual has a shape of its
     * own, and its 3-10 and 11-99 ranges take different noun shapes again.
     *
     * Only the integer operands matter here: every value through ``__p`` is a count of
     * minutes, hours, days or messages, so CLDR's fractional operands are not modelled.
     */
    pluralCategory(count, lang) {
        const n = Math.abs(Math.round(Number(count) || 0));
        const code = lang || this.lang;
        if (code === 'ar') {
            if (n === 0) return 'zero';
            if (n === 1) return 'one';
            if (n === 2) return 'two';
            const rest = n % 100;
            if (rest >= 3 && rest <= 10) return 'few';
            if (rest >= 11 && rest <= 99) return 'many';
            return 'other';
        }
        if (code === 'hi') return n === 0 || n === 1 ? 'one' : 'other';
        return n === 1 ? 'one' : 'other';
    },

    /**
     * A counted string, in the form the reader's language gives it.
     *
     * ``__(key)`` renders one sentence per key, which is enough for every string without a
     * number in it - and wrong for one with: "1 h ago" and "3 h ago" are the same words
     * in English, but not in Hindi (घंटा against घंटे) and not in Arabic (a dual, a 3-10
     * noun and an 11-99 noun).
     *
     * The forms live beside the plain key as ``<key>_<category>``. A language writes only
     * the forms its own rule can return; a form it does not carry falls back to
     * ``<key>_other``, then to the plain ``<key>`` - and if the table itself has not been
     * fetched yet, to English's. So every counted key keeps its old plain string, and a
     * caller that has not been moved over to ``__p`` still reads correctly.
     */
    __p(key, count) {
        const table = TRANSLATIONS[this.lang] || TRANSLATIONS.en;
        const category = this.pluralCategory(count);
        const candidates = [key + '_' + category, key + '_other', key];
        for (const name of candidates) {
            if (table[name]) return String(table[name]).replace('{count}', count);
            if (TRANSLATIONS.en[name]) {
                return String(TRANSLATIONS.en[name]).replace('{count}', count);
            }
        }
        return key;
    },
    applyDirection() {
        document.documentElement.setAttribute('dir', this.isRtl() ? 'rtl' : 'ltr');
        document.documentElement.setAttribute('lang', this.lang);
    },

    /**
     * Fill in the table for a stored language before the first paint.
     *
     * Called from ``UI.init`` and awaited there: an Arabic reader has no English paint to
     * flash because nothing has been drawn yet. Deliberately silent on failure - the
     * screen falls back to English, and the language selector is what tells them.
     */
    loadStored() {
        if (this.has(this.lang)) return Promise.resolve();
        return this.load(this.lang);
    }
};
