# The dashboard's Arabic, Hindi and Urdu strings: what was written in the redesign, and what is wrong with it

**Reviewed:** the 18 keys this redesign introduced — the view switcher's five labels, the vital
strip's denominator, the day strip's three lines, and phase 4's nine (the two watch figures, payroll
readiness, and the export control). **54 strings** across `i18n.ar.js`, `i18n.hi.js`, `i18n.ur.js`,
plus the nine written later when the download control grew into three artifacts - see the addendum at
the end, which is the same standing as this list - and the five the credentials roster added when its
table became a list (a second addendum, below it).
**Date:** 2026-09-29. **Status: these strings shipped unreviewed by a native speaker**, which this file
exists to make explicit.

## How to read this, and how far to trust it

The review was done by reading each new string against its English source *and* against every other
use of the same concept in the same table — the check that actually catches drift, because the three
translation tables were written over months by different hands.

Two kinds of finding are in here, and they do not deserve equal confidence:

* **Definite** — verifiable from the files alone: two different concepts rendered by one word, one
  concept rendered by two words on adjacent screens, a numeral with no noun, a dropped word that
  changes what the sentence means. A native speaker would confirm these, not overturn them.
* **Awkward** — register and idiom. These are a non-native reading of naturalness, offered as a
  question rather than a correction. They are marked as such.

Every finding is a *flag*; nothing here has been rewritten. The suggested replacement is a
suggestion, and the Arabic and Urdu ones in particular want a native eye before they land.

## Verdict in one line

No string is unintelligible, and Urdu is the most internally consistent of the three. But **three
findings are misleading rather than merely clumsy** (a dormant account that reads as a deactivated
one, in both Hindi and Urdu; a payroll sentence whose verb inverts its own meaning, in Arabic), and
**the day strip's late count and the vital strip's denominator are both elliptical enough to be
wrong** in Hindi and Urdu.

---

## Arabic (`i18n.ar.js`)

| # | Key | Current | Class | Why it is wrong |
|---|---|---|---|---|
| AR-1 | `dashboardPayrollWaiting` | `{shifts} منّاوبات ({hours} ساعة) … فتشميل الرواتب الآن سيستبعدها.` | **Definite — meaning** | `تشْميل` is *inclusion* (as in "شملهم العفو"), so the clause reads "so **including** the payroll now will **exclude** them" — it contradicts itself. The intended word is `تشغيل` (running the payroll). "رواتب" appears nowhere else in the table either. |
| AR-2 | `dashboardPeriodAwaitingShifts`, `dashboardPayrollWaiting` | `منّاوبات` | **Definite — vocabulary drift** | The table has two established words for a shift and the redesign added a third: `ورديات` (18 keys, including the Shifts tab itself and `shiftsPendingShifts` = `ورديات بانتظار الموافقة` — *the same concept*) and `نوبات` (8 keys, and `dashboardReviews` in this very view says `نوبات تنتظر المراجعة`). Pick one; `ورديات بانتظار الموافقة` already exists and should be reused verbatim. |
| AR-3 | `dashboardDormant` | `متوقف — لا تسجيل حضور منذ {days} يومًا` | **Definite — misleading** | `متوقّف` is the table's word for a *stopped feature* (`shiftRulesCloseDeferred` = `الإغلاق التلقائي متوقّف`), and it sits one row from `dashboardDeactivated` = `مُعطَّل`. A dormant account must not read as a switched-off one — that is precisely the distinction phase 4 was built to draw. Suggested: `متوقّف عن الحضور` or `منقطع`. |
| AR-4 | `dashboardPeopleTitle` vs `dashboardMetricPeople` | `الأفراد` vs `الأشخاص` | **Definite — drift** | The tab and the panel heading it opens use two different words for People, one above the other on the same screen. |
| AR-5 | `dashboardPeriodAwaitingHours` vs the Shifts screen | `بانتظار الاعتماد` | **Definite — drift** | The same figure (`awaiting_approval_hours`) is `ساعات بانتظار الموافقة` on the shifts screen (`shiftsPendingHours`) and, for a worker, `بانتظار الموافقة` (`myHoursAwaiting`) — but `بانتظار الاعتماد` here and `اعتماد` on the Approvals button. `موافقة` is the dominant word (19 keys); the dashboard is the outlier. The dashboard's own link opens the screen that says `موافقة`. |
| AR-6 | `dashboardPeriodDays` | `كل يوم من أيام هذه النافذة ({days} يومًا)، مقارنةً بأكثرها ({busiest}).` | Awkward | `بأكثرها` has no noun — the scale is *present days*, and the parenthetical repeats the day count while the busiest figure arrives bare. Hindi and Urdu both name it ("the busiest day"); Arabic should too: `مقارنةً بأكثر الأيام حضورًا ({busiest})`. |
| AR-7 | `dashboardOvertimeOpen` | `تجاوزوا حدّ الوقت الإضافي` | Awkward | A perfect-tense verb ("they exceeded") used as the label of a *count* in a queue row. A state rather than a sentence fits the row: `تجاوز حدّ الوقت الإضافي`. |
| AR-8 | `dashboardRefused24h` | `رُفضت خلال آخر يوم` | Awkward | A feminine plural verb with no subject, under a numeral. Reads as a fragment; `مرفوضة خلال آخر يوم` or `حالات رفض خلال آخر يوم` says it. |

## Hindi (`i18n.hi.js`)

| # | Key | Current | Class | Why it is wrong |
|---|---|---|---|---|
| HI-1 | `dashboardDormant` | `निष्क्रिय — {days} दिनों से कोई उपस्थिति नहीं` | **Definite — misleading** | `निष्क्रिय` is exactly `dashboardDeactivated` in this table. The people view draws both, so a deactivated account and a dormant one read identically — and the label is doing the work the figure cannot. Suggested: `सुप्त` or `लंबे समय से अनुपस्थित`. |
| HI-2 | `dashboardVitalOfExpected` | `कुल {expected} में से` | **Definite — dropped word** | Reads "out of a total of {expected}". The English note exists *only* to say what the denominator is (the days the sites were open — `dashboardPeriodExpectedDays` = `अपेक्षित दिन`), because "132" beside "of 5" is otherwise a roster figure. Fix: `अपेक्षित {expected} में से`. |
| HI-3 | `dashboardPeriodExtremeSub`, `dashboardPeriodDayAria` | `… · {late} देर से` | **Definite — grammar** | A bare numeral with an adverbial phrase is not a noun phrase: "3 by-late". The table's own good forms are `shiftsLateArrivals` = `देर से पहुँचे` and `liveOpsLateArrivals` = `देर से पहुँचे`. Fix: `{late} देर से आए`. |
| HI-4 | `dashboardPeriodAwaitingShifts` | `स्वीकृति की प्रतीक्षा में शिफ़्टें` | **Definite — spelling drift** | Across the table, 44 keys spell it `शिफ्ट` against 9 spelling `शिफ़्ट`. Within this feature it is 1–1: `dashboardReviews` — the same view, one row up — spells it `शिफ्टें`. So the screen carries both spellings of one word, which is worse than either choice alone. (Either is defensible; the split is not.) |
| HI-5 | `dashboardOnboarding` | `… , कभी उपस्थित नहीं` | **Definite — drift** | "Never clocked in" is `कभी दर्ज समय नहीं` in `dashboardNeverClockedIn`, one row above this one. Two wordings of one concept inside one card. |
| HI-6 | `dashboardPeriodPresentDays` | `उपस्थित दिन` | **Definite — noun form** | The table's noun for attendance is `उपस्थिति` (`title` = `साइट उपस्थिति`, `attendanceLogs`, `noHistory`, `attendanceOk`). `उपस्थित दिन` is an adjective bolted to a noun; `उपस्थिति के दिन` is the same concept in the table's own words. Matters more than usual here: this label is on a *vital*, and the figure beside it is the one somebody reads first. |
| HI-7 | `dashboardPeriodAwaitingHours` / `…AwaitingShifts` / `dashboardPayrollLink` vs the Shifts screen | `स्वीकृति की प्रतीक्षा` | **Definite — drift** | The Shifts screen says `मंज़ूरी बाकी घंटे` and `मंज़ूरी बाकी शिफ्ट` for the same two figures; the dashboard says `स्वीकृति`. The dashboard's own link opens that screen. Both families exist in the table (the worker's own hours use `स्वीकृति प्रतीक्षित`), so the fix is to choose one *for shifts and hours* and use it in both places. |
| HI-8 | `dashboardPeriodLate` | `देर से आना` | Awkward | An infinitive ("to arrive late") labelling a count. It copies the pre-existing `liveOpsLate`, but `shiftsLateArrivals` shows the better form (`देर से पहुँचे`). So: pre-existing bad habit, faithfully reproduced. |
| HI-9 | `dashboardPeriodMostLate` | `सबसे ज़्यादा देर` | Awkward | "Most delay" as a heading over a list of people. `सबसे ज़्यादा देर से आने वाले` says who is listed. |
| HI-10 | `dashboardWaitingHint` | `वे कतारें जो किसी के फैसले तक सब रोक देती हैं` | Awkward | `सब रोक देती हैं` ("stops everything") is loose; the panel holds specific queues. `किसी के फैसले तक काम रोक देती हैं` is tighter. |

## Urdu (`i18n.ur.js`)

| # | Key | Current | Class | Why it is wrong |
|---|---|---|---|---|
| UR-1 | `dashboardDormant` | `غیر فعال — {days} دن سے کوئی حاضری نہیں` | **Definite — misleading** | `غیر فعال` is exactly `dashboardDeactivated`, and it is the negation of `dashboardActive` (`فعال`). Same collision as Hindi, same consequence. Suggested: `غیر حاضر` / `طویل عرصے سے غیر حاضر`. |
| UR-2 | `dashboardDormant` | `{days} دن سے` | **Definite — grammar** | With a count, Urdu takes the plural oblique: `دنوں سے`. The next string written in the same batch (`dashboardOnboarding`) says `دنوں میں`, and Hindi says `दिनों से` — this is the one that slips. |
| UR-3 | `dashboardVitalOfExpected` | `کل {expected} میں سے` | **Definite — dropped word** | Same defect as HI-2: "out of a total of {expected}", with nothing saying the total is *expected days*. Fix: `متوقع {expected} میں سے`. |
| UR-4 | `dashboardPeriodExtremeSub`, `dashboardPeriodDayAria` | `… · {late} دیر سے` | **Definite — grammar** | Same as HI-3. The table's own noun is `دیر سے آمد` (`liveOpsLate`, `dashboardPeriodLate`) and `دیر سے آمدیں` (`shiftsLateArrivals`). Fix: `{late} دیر سے آمد`. |
| UR-5 | `dashboardOnboarding` | `… , کبھی حاضر نہیں` | **Definite — drift** | `dashboardNeverClockedIn` (one row above) says `کبھی حاضری درج نہیں`. |
| UR-6 | `dashboardMetricPeople` vs `dashboardPeopleTitle` | `لوگ` vs `افراد` | **Definite — drift** | The tab and the heading it opens disagree. `افراد` is also what `dashboardPeriodWorkers` uses, so it is the majority register. |
| UR-7 | `dashboardPeriodWorkers` | `موجود افراد` | **Definite — wrong word** | `موجود` means *available/existing*, not *present*: "available individuals". Present is `حاضر` throughout this table (30 keys). Fix: `حاضر افراد`. |
| UR-8 | `dashboardPayrollWaiting` | `… اس لیے اب تنخواہ چلانے پر یہ رہ جائیں گی۔` | Awkward | `تنخواہ چلانا` is colloquial for running a payroll, and the table already carries the transliteration `پے رول` (`hintShifts`). `یہ رہ جائیں گی` ("they will remain") is also softer than the English "leave them out" — the point of the sentence is money that will not be paid. Suggested: `… اس لیے اب پے رول چلانے پر یہ حصہ رہ جائے گا۔` |

---

## One canonical word per concept, per language

The reason the drift above exists is that there was no list. This is that list, taken from the
majority usage already in each table — the terms the redesign should have used, and the ones the next
translator should start from.

| Concept | Arabic | Hindi | Urdu |
|---|---|---|---|
| Attendance / clock-in | `تسجيل الحضور` | `उपस्थिति` (admin) / `हाज़िरी` (worker-facing) | `حاضری` |
| Present (a person) | `حاضر` | `उपस्थित` | `حاضر` |
| Days present | `أيام الحضور` | `उपस्थिति के दिन` | `حاضری کے دن` |
| Days expected | `الأيام المتوقعة` | `अपेक्षित दिन` | `متوقع دن` |
| Late arrival | `وصول متأخر` | `देर से पहुँचे` | `دیر سے آمد` |
| Shift | `وردية` / `ورديات` | `शिफ्ट` | `شفٹ` |
| Approval (of hours/shifts) | `موافقة` (the shifts screen's word) — or `اعتماد`, but one of them | `मंज़ूरी` (shifts screen) — or `स्वीकृति`, but one of them | `منظوری` |
| Approved hours | `الساعات المعتمدة` | `स्वीकृत घंटे` | `منظور شدہ گھنٹے` |
| Awaiting approval | `بانتظار الموافقة` | `मंज़ूरी बाकी` | `منظوری کے منتظر` |
| Payroll | `الرواتب` | `वेतन` | `پے رول` / `تنخواہ` |
| Deactivated (account) | `مُعطَّل` | `निष्क्रिय` | `غیر فعال` |
| Dormant (account) | *needs a word — not* `متوقف` | *needs a word* | *needs a word* |

The last row is the real gap: **all three languages currently have no distinct word for "dormant",
and two of them reused the word for "deactivated"**. This came from the redesign, and it is the one
finding here that is a product problem rather than a wording problem.

## What is clean

Worth saying, because it is most of it: the payroll vocabulary follows each table's own register
(`वेतन` in Hindi, `تنخواہ`/`پے رول` in Urdu, `الرواتب` in Arabic with the approved-hours wording
matching `الساعات المعتمدة`); Urdu uses `منظوری` for every approval in this feature, which is exactly
what the table does everywhere else; the watch heading (`يستحق النظر` / `ध्यान देने योग्य` /
`قابل توجہ`), the three day-strip sentences in Hindi and Urdu, the export control and its failure
sentence read naturally in all three; and every placeholder survived the translation — including the
reordered ones, where Hindi and Urdu correctly turned "of {expected} days present" into
"{expected} में से {days} दिन".

## Added after this review: the three artifacts

Nine keys were written after the audit below, when the period card's single download grew into three
artifacts - CSV, Excel and a printable sheet. They are **as unaudited as everything else here**
(`dashboardPeriodExportExcel`, `dashboardPeriodPrint`, `dashboardPeriodPrintTitle`,
`dashboardPeriodPrintFigure`, `dashboardPeriodPrintValue`, `dashboardPeriodPrintEachDay`,
`dashboardPeriodPrintDay`, `dashboardPeriodPrintNote`, `dashboardPeriodPrintFailed`).

They were checked against each table's own vocabulary rather than against the English, and the two
places most likely to drift came out right: the late-arrival noun (`متأخر` matches `shiftsArrivalLate`;
`देर से आए` and `دیر سے آمد` match the day strip and `dashboardPeriodLate`) and the approval word in the
sheet's note (`معتمدة` / `स्वीकृत` / `منظور شدہ` are the words each table already uses for approved
hours). `تنزيل Excel` / `Excel डाउनलोड करें` / `Excel ڈاؤن لوڈ کریں` leave the product name alone, which
is the right call for a brand.

Two findings, both in the *awkward* class rather than the misleading one:

| # | Key | Class | Note |
|---|---|---|---|
| P-1 | `dashboardPeriodPrintFailed` | **Awkward - all three** | The refusal is telegraphic in every language: Urdu `یہ مدت پرنٹ کرنے کے لیے اسکرین پر نہیں۔` drops the copula (`... پر موجود نہیں ہے` is the sentence), Hindi `यह अवधि प्रिंट करने के लिए स्क्रीन पर नहीं है।` reads the same way, and Arabic `هذه الفترة ليست على الشاشة لطباعتها` wants `غير معروضة على الشاشة`. It is a toast nobody will quote, but it is the only sentence in the feature that is not a full sentence. |
| P-2 | `dashboardPeriodDayAria` vs `dashboardPeriodPrintDay` (Urdu) | **Awkward - drift** | The feature now spells *late arrivals* two ways in Urdu: `دیر سے آمد` here and in the period fact, `تاخیر سے` in the day strip's `aria-label`. Both are Urdu; together they are the same drift this review exists to catch, so one of them should win - `دیر سے آمد` is what the board uses everywhere else. |
| P-3 | `dashboardPeriodPrintFigure` | Awkward - judgement | `البند` / `मद` / `مد` are "item/clause"; a column heading on paper may read better as `البيان` / `विवरण` / `تفصیل` ("particulars"). Not an error - the matched `القيمة` / `मान` / `مقدار` for *Value* is plainly right. |

## Added after this review: the credentials roster

Five keys were written when the Credentials tab's nine-column table became a roster with role chips
and a page button - `credentialsEveryone`, `credentialsRoleFilter`, `credentialsShowing`,
`credentialsShowMore`, `credentialsNoRole`. They are **as unaudited as everything else here**, and
one of them is the only string in either batch that is missing its verb.

The same check as before: each string against its English source, and against every other use of the
same concept in the same table. One thing came out clean and belongs on the record, because it is the
likeliest place for a filter to drift: the six **role names on the chips are not new strings at all**
- they are the existing `roleLabel` keys (`worker`, `moallem`, `off_office`, `admin`, `head_admin`),
so a chip says whatever the rest of the console says about that role, and the chips cannot drift from
the tables that already name them. Every placeholder survived, and the reordered ones are correct:
Hindi and Urdu turned "Showing {shown} of {total}" into "out of {total}, {shown}" rather than
keeping English word order.

| # | Key | Class | Note |
|---|---|---|---|
| CR-1 | `credentialsShowMore` | **Awkward - dropped verb** | Arabic is `المزيد ({count})` - "More (4)". Hindi (`{count} और दिखाएँ`) and Urdu (`{count} مزید دکھائیں`) both kept the imperative the English has ("Show 4 more"), and the count moved into parentheses, so the Arabic button reads as a label *on* the list rather than as the action that adds to it. On a control whose whole job is to say what tapping it does, the verb is the string. Suggested: `إظهار {count} أخرى`. |
| CR-2 | `credentialsShowing` | Awkward - elliptical | Arabic `عرض {shown} من {total}` has no verb at all ("Display 3 of 14"), and Hindi/Urdu are full passive clauses with no subject (`{total} में से {shown} दिखाए जा रहे हैं`, "of {total}, {shown} are being shown" - shown *what*?). The English is elliptical in the same way, and the line sits directly under the roster beside the page button, so the ellipsis is anchored; but of these five this is the sentence a native reader is most likely to rewrite. Suggested: `يُعرض {shown} من {total}` / `{total} में से {shown} खाते` / `{total} میں سے {shown} اکاؤنٹس`. |
| CR-3 | `credentialsRoleFilter` | Awkward - drift | An `aria-label`, so only a screen reader meets it, but the *roster* is a word these tables already have: Arabic `السجل` (`dashboardPeopleHint`), Hindi `रोस्टर`, Urdu `فہرست`. All three render it as accounts instead (`قائمة الحسابات` / `खाते` / `اکاؤنٹس`), which is a different noun and, in Hindi, the only place the loan word is dropped. |
| CR-4 | `role`, `credentialsSearchPlaceholder` vs the three new strings | **Definite - two words for one concept, on one screen** | The Arabic table carries `الدور` (5 keys, including two older credential-screen strings) and `الصلاحية` (2: the plain `role` label and this tab's own search placeholder). The new strings took `الدور`, so the Credentials tab now filters by `الدور` while the search box directly above it invites a search by `الصلاحية`. The split is pre-existing and this change widened it rather than causing it; `الدور` now has the majority, but which word wins is a native decision rather than a mechanical one, so it is left standing. |
| CR-5 | `credentialsEveryone` | Clean, worth naming | `الكل` / `सभी` / `سب` is the same word the same shape of chip already carries on the notes queue (`notesFilterAll`), which is exactly the case where a translator reaches for something grander and loses a filter's plainness. |

## Added after this review: the retention panel

Forty keys were written when the Admin tab gained *Data & retention* - the store table, the policy
table, the four figures, the last sweep, and the words the Check button draws its plan with. They
are **as unaudited as everything else here**, and this is the first batch in the file where the
vocabulary was already partly in the tables: the panel deliberately reuses the names other screens
use for the same data (`devDb`, `devBackups`, `refusalsTitle`, `corpusTitle`), so those strings
cannot drift by construction.

The same check as before, plus the mechanical one: all forty keys are present in all four tables
and the placeholder *multisets* are identical (two `{bytes}`, three `{count}`, one `{days}`, one
`{hours}`) - the parity suite proves that much, and proves nothing about whether the words are
right.

What came out clean is worth naming, because this feature is one long sentence in three places and
the temptation to shorten was real. Hindi and Urdu name the sweeper `सफ़ाई कार्य` / `صفائی کا کام`,
which is the same word the alerts screen already uses for old-data cleaning (`adminAlertsHint`), so
the console says one thing about it rather than two. `retentionPlanUntouched` - *the* sentence in
this feature, the one that stops a list of deletions reading as a log of them - is a full sentence
in all three, and every one keeps the contrast (`فحص لا حذف`, `यह जाँच है, हटाना नहीं`,
`یہ جانچ ہے، ہٹانا نہیں`) rather than the terser "nothing was deleted".

| # | Key | Class | Note |
|---|---|---|---|
| RT-1 | `retentionSweeper` | **Definite - the word names the wrong job** | Arabic `المُحدِّث` is "the updater/refresher", which is the one thing this is not: the job is deleting. The rest of the new Arabic text calls it `عملية التنظيف` (the cleaning run), and the alerts screen calls it `دورة حذف بيانات قديمة` (a cycle of deleting old data), so Arabic now has three words for one job and the *label* is the wrong one. Hindi and Urdu agree with each other and with the alerts screen; Arabic is the outlier. Suggested: `أداة التنظيف`, matching the two long strings in the same panel. |
| RT-2 | `retentionStore` | **Definite - a word narrower than the English** | The header is deliberately loose in English because a row is not always a directory: the database row is a file plus its `-wal` and `-shm`. All three translations committed to *folder* (`المجلد` / `फ़ोल्डर` / `فولڈر`), so on the database row the table's own heading is false. Suggested: a word for "place" rather than "folder" - `الموقع` / `स्थान` / `مقام`. |
| RT-3 | `retentionLeftBehind` | Drift - the fault reads as neutral | English *Left behind* means "the policy says this should be gone and it is not" - the only figure on the panel that is a fault. Arabic `متبقّي`, Hindi `बाक़ी` and Urdu `باقی` all mean "remaining/left over", which is a measurement, not a fault, and a reader who does not already know the policy reads it as a total. The badge's colour carries the warning visually, and a screen reader gets nothing but the word. Suggested: `لم يُحذف` / `छूटी हुई फ़ाइलें` / `رہ جانے والی فائلیں`. |
| RT-4 | `retentionTargetAnchors` | Drift - two nouns for the same rows | The Developer console already names these `devOfflineAnchors` (`المرتكزات` / `एंकर` / `اینکر`), and the handset's own message speaks of a signing key (`deviceKeyLost`). This panel says "Device keys" (`مفاتيح الأجهزة` / `फ़ोन की ऑफ़लाइन कुंजियाँ` / `فون کی آفلائن کیز`) - a third noun, and the only place the row is called a *key* rather than an anchor. The English is already split this way, so the fix is a decision rather than a translation: pick one noun, or keep "keys" for whatever the sweep deletes and leave "anchors" to the protocol. |

## Added after this review: the Shifts tab

Sixteen strings, written with that tab's redesign - the per-day coverage strip, the two
attention filters, the sortable headers and the paged table. Two of the findings below were
**fixed while reviewing**: they were this author's own errors rather than decisions that want a
third opinion, and shipping a known grammar fault to be flagged would be a strange use of the
flag.

**Fixed - Arabic numeral agreement (SM-1).** `shiftsCoverageDays` and its three siblings were
written as `{count} يوماً`, `{count} أسبوعاً` and so on: the accusative *singular*, which is right
for eleven and above and wrong for three to ten. The counts these keys actually carry are 4
(weeks, quarters), 12 (months) and 28-31 (days), so three of the four were being used in the
range where Arabic wants the plural - "4 أسبوعاً" for what should be "4 أسابيع". No single numeral
form is correct across 4 and 31, so the phrasing now steps around the choice: `{count} من الأسابيع`
(*n of the weeks*), which is grammatical for every count. The general fix is a plural rule in
`I18n`; this phrase works around its absence.

**Fixed - the break hint repeated its own label (SM-2).** The tile is labelled *Unpaid break* and
its hint opened "unpaid, already out of the hours" (Arabic `غير مدفوعة، ...`). All four now carry
only the half the label does not already say.

| # | Key | Class | Note |
|---|---|---|---|
| SM-1 | `shiftsCoverageDays/Weeks/Months/Quarters` | **Fixed - grammar** | See above: the singular accusative was wrong for the counts these keys carry. |
| SM-2 | `shiftsBreakHint` | **Fixed - redundancy** | See above: the hint repeated the word its own label is made of. |
| SM-3 | `shiftsShowMore` | Drift - two words for one control | The credentials roster (reworked in the same session) added `credentialsShowMore`, *Show {count} more*, and this tab added *Show more*: one control, one screen family, two phrasings - Arabic `المزيد ({count})` against `عرض المزيد`, Hindi `{count} और दिखाएँ` against `और दिखाएँ`, Urdu `{count} مزید دکھائیں` against `مزید دکھائیں`. One should win, and the roster's is arguably the better of the two: the count on the button is the only place a reader is told how much is left if the line above it has scrolled away. |
| SM-4 | `shiftsCoverageTap` | Drift - the only device verb in the tables | *Tap a column to narrow the period to it.* Every other interactive hint in the console is device-neutral (*Show shifts for this day*, *Pick both a start and an end date*), and this console is read on a phone and on a desktop, where the same gesture is not called tapping. Suggested: name the outcome - *Choose a column to narrow the period to it* - or let the button-ness of the column speak for itself. |
| SM-5 | `shiftsCustomPeriod` | Watch - new vocabulary, three shapes | Nothing else in the tables says "custom", so all four were written here: Arabic `فترة مخصصة` (an adjective), Hindi `कस्टम अवधि` (a transliteration) and Urdu `اپنی مرضی کی مدت` (a phrase, *a period of one's own choosing*). The Urdu is close to twice the length of the other three and sits in the `summary` of the period fold, where the width is not free. Not wrong; worth a native eye at the width it is used in. |
| SM-6 | `shiftsCoverageDayAria`, `shiftsShowing` | Watch - phrasing that is heard rather than seen | Both are read aloud. `shiftsCoverageDayAria`'s `{from}` is an ISO date (`2026-08-05`), which a screen reader will read digit by digit - the same choice the console makes for dates on screen, worth knowing it is also heard. Hindi's `shiftsShowing` joins its two figures with a slash (*पहले {shown} / {total}*) where the other three use a word. |

Two keys were deliberately **not** written: the two attention chips reuse `shiftsPending` (*Awaiting
approval*) and `shiftsLateArrivals` (*Late arrivals*) - the same words as the amber card and the
column header above them - because a filter that names its figure differently from the figure is a
filter the reader has to translate.

## Added after this review: the Sites tab

Seven strings, written with that tab's redesign - the search that appears over a long list, and
the sentences its two empty states need. One was **fixed while reviewing**, for the same reason
SM-1 was: it was this author's own error rather than a decision that wants a third opinion.

The tab's other words were deliberately **not** written. The band's two buttons reuse `sitesAdd`
(*Add a site*) and `sitesCategories` (*Site categories*); the figures on a row reuse `sitesCategory`,
`sitesCategoryNone`, `sitesWindowStart`/`sitesWindowEnd`, `sitesWindowTimezone`, `sitesRadius` and
the three *where this came from* sentences; both folds' hints reuse `sitesLocationHint`,
`sitesWindowHint` and `sitesCategoriesHint`. Renaming the same facts on the way past a redesign
would have made the tab read as a different feature rather than a clearer view of the same one -
and it is exactly the sort of drift the parity suite cannot see, because it counts keys and
placeholder names and never reads either.

**Fixed - the empty state made a claim that was not true (ST-1).** `sitesNoMatch` was first
written as *No sites*, which is the same sentence as `sitesEmpty` - the one the tab shows on a
deployment that has no sites *at all*, where the answer is the add form sitting above it. A search
that matched nothing and a company with nothing are two states with two different ways out, so the
sentence now names what was typed: *No site matches "{query}"*, with `sitesNoMatchHint` and
`sitesClearSearch` beside it.

| # | Key | Class | Note |
|---|---|---|---|
| ST-1 | `sitesNoMatch` | **Fixed - a claim that was untrue** | See above: it said the same thing as `sitesEmpty` about a state that is not the same state. |
| ST-2 | `sitesShowing` | Drift - the second phrasing of `shiftsShowing` | Both are *{shown} of {total} {things}* and both exist because each tab filters its own list; the Arabic, Hindi and Urdu copies were written to match `shiftsShowing` where that was natural. Two tabs, one sentence - worth confirming the three translations really are as alike as their English is. |
| ST-3 | `sitesSearchLabel` | Watch - a label only a screen reader hears | *Search sites* (Arabic `ابحث في المواقع`). It names the box for a reader who cannot see the placeholder, which makes it the one string here that nobody can proofread by looking at the screen and the one with no width to argue about. |
| ST-4 | `sitesCategoriesEmpty` | Watch - terse by design | *No categories yet.* The fold it sits in already carries `sitesCategoriesHint`, so this is a full stop rather than a second explanation; the Arabic `لا توجد فئات بعد.` and the Urdu `ابھی کوئی زمرہ نہیں۔` mirror that. Not wrong, but it is the shortest sentence in the group and the one whose *tone* a native reader would settle. |
| ST-5 | `sitesSearchPlaceholder` | Checked against the code, not by a reader | *Name or category* (Arabic `الاسم أو الفئة`), which is what `sitesMatches` actually filters on - a name or a category, never an id. Recorded because a placeholder that promises more than the filter delivers is the commonest lie a search box tells. |

## What this review cannot conclude

Register and idiom for a *native* reader: whether `متوقف`, `सुप्त` or `غیر حاضر` is the word a
supervisor would actually use for a worker who has stopped turning up, whether `बाकी` or
`प्रतीक्षित` reads better on a vital tile at 13px, and whether the Arabic `مقارنةً بأكثر الأيام`
phrasing is the natural way to caption a bar chart. Those want a native speaker's five minutes, and
the two that matter most are the dormant label (a word that does not exist yet in any of the three
tables) and the payroll sentence's verb in Arabic.

## Re-running this audit

The comparison this file is built on is reproducible: parse `"key": "value"` out of the four tables,
filter to the `dashboard` namespace, and print the four columns side by side. The concept check is
the same script with a term list per language — which is what caught `निष्क्रिय`, `غیر فعال`,
`منّاوبات` and the `शिफ्ट`/`शिफ़्ट` split. Worth running again whenever a batch of dashboard strings
is added, because none of these were caught by the parity suite: it counts keys and placeholders, so
it proves a translation *exists* and says nothing about whether it is right.
