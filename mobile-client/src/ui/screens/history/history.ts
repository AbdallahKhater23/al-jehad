/**
 * History — the worker's own timesheet.
 *
 * Two views of the same data, because there are two questions: "what happened on Tuesday?"
 * (the log, grouped by day) and "how much am I owed?" (the period totals and the per-site
 * breakdown). The list is the default because it is the one a worker checks when a punch
 * looks wrong.
 *
 * The figures come from ``/worker/me/report`` rather than being summed on the phone: hours
 * nobody has approved are reported in ``awaiting_approval_hours`` and never added into
 * ``approved_hours``, and that rule has to be the server's or the phone will disagree with
 * payroll.
 */

import { request } from '../../../core/http.js';
import type { Screen, ScreenContext } from '../../shell.js';
import {
  esc,
  on,
  fmtTime,
  fmtDay,
  fmtHours,
  fmtHoursDecimal,
  dayKey,
  todayIso,
  daysAgoIso,
} from '../../dom.js';
import { icon } from '../../icons.js';
import { toastError } from '../../components/toast.js';

interface LogRow {
  id: number;
  site: string;
  action: string;
  timestamp: string;
  hours: number | null;
  status: string;
  status_code: string;
  approved_hours: number | null;
  overtime_hours: number | null;
  flag_reason: string | null;
}

interface ReportRow {
  site_name: string;
  hours: number;
  approved_hours: number;
  awaiting_approval_hours: number;
  shifts: number;
}

interface ReportResponse {
  period: { start: string; end: string };
  rows: Array<Record<string, unknown>>;
  by_site: ReportRow[];
  totals: {
    shifts: number;
    sites: number;
    hours: number;
    approved_hours: number;
    awaiting_approval_hours: number;
    break_hours: number;
    late_arrivals: number;
    overtime_hours: number;
  };
}

/** Which status codes are worth a badge, and what the badge says. */
const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  pending_review: { label: 'Awaiting review', cls: 'is-warn' },
  pending_overtime: { label: 'Overtime review', cls: 'is-warn' },
  unverified_offline: { label: 'Offline', cls: 'is-info' },
  flagged: { label: 'Flagged', cls: 'is-warn' },
  rejected: { label: 'Rejected', cls: 'is-danger' },
};

export function createHistoryScreen(): Screen {
  return {
    title: () => 'History',
    subtitle: () => 'Your timesheet',
    mount(host: HTMLElement, ctx: ScreenContext): () => void {
      let logs: LogRow[] = [];
      let report: ReportResponse | null = null;
      let rangeDays = 7;
      let view: 'log' | 'summary' = 'log';
      let loading = true;
      const disposers: Array<() => void> = [];

      host.innerHTML = `<div id="history-root"></div>`;
      const root = host.querySelector('#history-root') as HTMLElement;

      function renderShell(body: string): void {
        root.innerHTML = `
          <div class="hand-screen">
            <div class="ui-stack is-tight">
              <div class="ops-chips" role="group" aria-label="Period">
                ${[1, 7, 14, 30]
                  .map(
                    (d) => `
                  <button class="ops-chip" type="button" data-days="${d}"
                          aria-pressed="${d === rangeDays}">${d === 1 ? 'Today' : `Last ${d} days`}</button>
                `,
                  )
                  .join('')}
              </div>
              <div class="ops-chips" role="group" aria-label="View">
                <button class="ops-chip" type="button" data-view="log" aria-pressed="${view === 'log'}">Punches</button>
                <button class="ops-chip" type="button" data-view="summary" aria-pressed="${view === 'summary'}">Totals</button>
              </div>
            </div>
            ${body}
          </div>
        `;
      }

      function renderLoading(): void {
        renderShell(`
          <div class="hand-card">
            <div class="hand-skeleton hand-skeleton--line" style="width:60%"></div>
            <div class="hand-skeleton hand-skeleton--block"></div>
            <div class="hand-skeleton hand-skeleton--line" style="width:80%"></div>
          </div>
        `);
      }

      function renderError(message: string): void {
        renderShell(`
          <div class="hand-alert is-danger">
            ${icon('wifiOff', 16)}
            <p><strong>Could not load your timesheet</strong> ${esc(message)}</p>
          </div>
          <button class="ui-btn ui-btn-quiet is-block" type="button" data-action="reload">Try again</button>
        `);
      }

      function renderTotals(): string {
        if (!report) return '';
        const t = report.totals;
        const cell = (label: string, value: string): string => `
          <span class="hand-policy-cell">
            <span class="hand-policy-label">${esc(label)}</span>
            <span class="hand-policy-value"><b>${esc(value)}</b> h</span>
          </span>
        `;
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">${esc(report.period.start)} → ${esc(report.period.end)}</h2>
            </div>
            <div class="hand-policy" style="margin-top:0;padding-top:0;border-top:0">
              ${cell('Recorded', fmtHoursDecimal(t.hours))}
              ${cell('Approved', fmtHoursDecimal(t.approved_hours))}
              ${cell('Awaiting approval', fmtHoursDecimal(t.awaiting_approval_hours))}
              <span class="hand-policy-cell">
                <span class="hand-policy-label">Shifts</span>
                <span class="hand-policy-value"><b>${esc(String(t.shifts))}</b></span>
              </span>
            </div>
            ${
              t.awaiting_approval_hours > 0
                ? `<div class="hand-alert is-info" style="margin-top:12px">
                     ${icon('info', 16)}
                     <p>Hours awaiting approval are not payable until an administrator signs them off.</p>
                   </div>`
                : ''
            }
            ${
              t.overtime_hours > 0
                ? `<div class="hand-alert" style="margin-top:12px">
                     ${icon('alert', 16)}
                     <p><strong>${esc(fmtHoursDecimal(t.overtime_hours))} of overtime</strong> is waiting for a decision.</p>
                   </div>`
                : ''
            }
          </div>
        `;
      }

      function renderBySite(): string {
        if (!report || report.by_site.length === 0) return '';
        // A definition list rather than a table: two facts per site, and on a phone a
        // label/value pair is what the eye walks down. The web's own rows on the desk.
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">By site</h2>
            </div>
            <dl class="hand-dl">
              ${report.by_site
                .map(
                  (site) => `
                <div class="hand-dl-row">
                  <dt>${esc(site.site_name)}</dt>
                  <dd class="is-mono">${esc(fmtHoursDecimal(site.hours))} h ·
                    ${esc(String(site.shifts))} shift${site.shifts === 1 ? '' : 's'}<br />
                    <span class="hand-faint">${esc(fmtHoursDecimal(site.approved_hours))} approved</span>
                  </dd>
                </div>
              `,
                )
                .join('')}
            </dl>
          </div>
        `;
      }

      function renderLog(): string {
        if (logs.length === 0) return renderEmptyShell();

        // Grouped by the day the punch belongs to, so the summary line is a *day's* hours
        // and not a running total the reader has to subtract from.
        const groups = new Map<string, LogRow[]>();
        for (const row of logs) {
          const key = dayKey(row.timestamp);
          const bucket = groups.get(key);
          if (bucket) bucket.push(row);
          else groups.set(key, [row]);
        }

        const days = [...groups.entries()].map(([key, rows]) => {
          // The day's hours are the sum of what was *paid* that day -- the clock-outs -- and
          // not a running total the reader has to subtract from.
          const dayHours = rows
            .filter((r) => r.action === 'Clock Out')
            .reduce((sum, r) => sum + Number(r.hours ?? 0), 0);
          return `
            <div class="hand-card">
              <div class="hand-section-head">
                <h2 class="hand-section-title">${esc(fmtDay(key))}</h2>
                ${dayHours > 0 ? `<span class="ui-badge is-live">${esc(fmtHours(dayHours))}</span>` : ''}
              </div>
              <ul class="hand-rail">${rows.map(renderLogRow).join('')}</ul>
            </div>
          `;
        });
        return days.join('');
      }

      function renderEmptyShell(): string {
        return `
          <div class="ui-empty">
            <span class="ui-empty-icon">${icon('history', 22)}</span>
            <p class="ui-empty-title">No punches in this period</p>
            <p class="ui-empty-body">Try a longer range.</p>
          </div>
        `;
      }

      /**
       * One punch, as a stop on the web's rail: a filled dot for a clock-in, a hollow one
       * for a clock-out, the moment in tabular figures, and the hours it is worth on the
       * right where a thumb scrolling the column can compare them.
       */
      function renderLogRow(row: LogRow): string {
        const badge = STATUS_BADGE[row.status_code] ?? null;
        const isTransit = /transit/i.test(row.action);
        // A stop is "in" or "out" by what it did, not by what it is called: the transit
        // arrival closes nothing, so it reads as neither.
        const kind = /out/i.test(row.action) ? 'is-out' : isTransit ? '' : 'is-in';
        const hours =
          row.action === 'Clock Out' && row.hours !== null
            ? `<p class="hand-stop-hours">${esc(fmtHours(row.hours))}</p>`
            : '';
        const approved =
          row.approved_hours !== null && row.approved_hours !== undefined
            ? `<p class="hand-stop-status">${esc(fmtHours(row.approved_hours))} approved</p>`
            : '';
        return `
          <li class="hand-stop ${kind}">
            <div class="hand-stop-what">
              <span class="hand-stop-action">${esc(row.action)}</span>
              <span class="hand-stop-when">${esc(fmtTime(row.timestamp))}</span>
              <span class="hand-stop-where">${esc(row.site)}</span>
              ${
                badge || row.flag_reason
                  ? `<span class="hand-note-tags">
                       ${badge ? `<span class="ui-badge ${badge.cls}">${esc(badge.label)}</span>` : ''}
                       ${row.flag_reason ? `<span class="hand-note-stamp">${esc(row.flag_reason)}</span>` : ''}
                     </span>`
                  : ''
              }
            </div>
            <div class="hand-stop-tally">${hours}${approved}</div>
          </li>
        `;
      }

      function paint(): void {
        if (loading) {
          renderLoading();
          return;
        }
        // The log view shows the punches; the summary view shows the period totals and the
        // per-site breakdown. Each is rendered once — a totals card above a totals card is
        // how a figure gets read twice and believed twice.
        renderShell(view === 'log' ? renderLog() : `${renderTotals()}${renderBySite()}`);
      }

      async function load(): Promise<void> {
        loading = true;
        paint();
        const start = daysAgoIso(rangeDays);
        const end = todayIso();
        try {
          const [logRows, reportBody] = await Promise.all([
            request<LogRow[]>({ path: '/worker/me/logs', query: { limit: 200 } }),
            request<ReportResponse>({ path: '/worker/me/report', query: { start, end } }),
          ]);
          logs = Array.isArray(logRows) ? logRows : [];
          report = reportBody;
        } catch (e) {
          const err = e as { offline?: boolean; message?: string };
          loading = false;
          renderError(
            err.offline
              ? 'No connection to the server. Your timesheet is not cached on this phone.'
              : (err.message ?? 'The request failed.'),
          );
          return;
        }
        loading = false;
        paint();
      }

      disposers.push(
        on(root, 'click', (event) => {
          const target = (event.target as HTMLElement).closest('[data-days],[data-view],[data-action]') as HTMLElement | null;
          if (!target) return;
          if (target.hasAttribute('data-days')) {
            rangeDays = Number(target.getAttribute('data-days'));
            void load();
            return;
          }
          const nextView = target.getAttribute('data-view');
          if (nextView === 'log' || nextView === 'summary') {
            view = nextView;
            paint();
            return;
          }
          if (target.getAttribute('data-action') === 'reload') void load();
        }),
      );

      void load();
      ctx.main.scrollTop = 0;
      return () => disposers.forEach((off) => off());
    },
  };
}

void toastError;
