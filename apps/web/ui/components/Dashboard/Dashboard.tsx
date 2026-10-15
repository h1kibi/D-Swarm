"use client";

import { useUser } from "@/lib/useUser";
import { useEvents } from "@/lib/useEventsApi";
import { useTargets } from "@/lib/useTargets";
import { useRuns } from "@/lib/useRunsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

/**
 * The console dashboard is an overview + operator/admin surface. Target and Run
 * CREATION/MANAGEMENT live in the Command Deck (its Target Library, run rail,
 * and launch wizard) — nothing here duplicates those actions; cards summarize
 * the same data sources and hand off to the deck.
 */

function MemberDashboard() {
  const t = useT();
  const { events, loading: eventsLoading } = useEvents();
  const targets = useTargets();
  const { runs } = useRuns();

  const activeEvents = events.filter((event) => event.status === "active");
  const solvedTargets = targets.filter((target) => target.status === "solved");
  const unsolvedTargets = targets.filter((target) => target.status !== "solved");
  const activeRuns = runs.filter((run) => run.status === "running");
  const completedRuns = runs.filter((run) => run.solved === true || run.finished === true);

  return (
    <div className="ds-page">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("dash.title")}</h1>
          <p className="ds-sub">{t("dash.sub")}</p>
        </div>
        <div className="ds-actions">
          <Link href="/deck" className="ds-btn ds-btn--primary">{t("dash.openDeck")}</Link>
        </div>
      </div>

      <div className="ds-grid">
        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.activeEvents")}</h2>
          {eventsLoading ? (
            <div className="ds-loading">{t("common.loading")}</div>
          ) : activeEvents.length === 0 ? (
            <p className="ds-muted" style={{ margin: 0 }}>{t("dash.noEvents")}</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {activeEvents.map((event) => (
                <Link key={event.id} href={`/events/${event.id}`} className="ds-link">
                  {event.name}
                </Link>
              ))}
            </div>
          )}
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.targets")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{unsolvedTargets.length}</div>
              <span className="ds-stat-label">{t("dash.unsolved")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{solvedTargets.length}</div>
              <span className="ds-stat-label">{t("dash.solved")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/deck" className="ds-btn ds-btn--sm">{t("dash.manageInDeck")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.runs")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{activeRuns.length}</div>
              <span className="ds-stat-label">{t("dash.activeRuns")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{completedRuns.length}</div>
              <span className="ds-stat-label">{t("dash.completedRuns")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/runs" className="ds-btn ds-btn--sm">{t("dash.viewAll")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.quick")}</h2>
          <div className="ds-actions" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <Link href="/deck" className="ds-btn">{t("dash.openDeck")}</Link>
            <Link href="/events" className="ds-btn">{t("dash.createEvent")}</Link>
            <Link href="/analytics" className="ds-btn">{t("nav.analytics")}</Link>
          </div>
        </section>
      </div>
    </div>
  );
}

function AdminDashboard() {
  const t = useT();
  const { user } = useUser();
  const { events } = useEvents();
  const { runs } = useRuns();

  const totalEvents = events.length;
  const activeEvents = events.filter((event) => event.status === "active").length;
  const totalRuns = runs.length;
  const runningRuns = runs.filter((run) => run.status === "running").length;
  const online = Boolean(user?.id);

  return (
    <div className="ds-page">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("dash.adminTitle")}</h1>
          <p className="ds-sub">{t("dash.adminSub")}</p>
        </div>
        <div className="ds-actions">
          <Link href="/deck" className="ds-btn ds-btn--primary">{t("dash.openDeck")}</Link>
        </div>
      </div>

      <div className="ds-grid">
        <section className="ds-card">
          <h2 className="ds-card-title">{t("events.title")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{totalEvents}</div>
              <span className="ds-stat-label">{t("dash.eventsTotal")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{activeEvents}</div>
              <span className="ds-stat-label">{t("dash.eventsActive")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/events" className="ds-btn ds-btn--sm">{t("dash.manageEvents")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("runs.title")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{totalRuns}</div>
              <span className="ds-stat-label">{t("dash.runsTotal")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{runningRuns}</div>
              <span className="ds-stat-label">{t("dash.runsRunning")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/runs" className="ds-btn ds-btn--sm">{t("dash.viewAll")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.accounts")}</h2>
          <p className="ds-sub" style={{ margin: "0 0 14px" }}>{t("dash.accountsHint")}</p>
          <div className="ds-actions" style={{ justifyContent: "flex-start" }}>
            <Link href="/admin/accounts" className="ds-btn ds-btn--sm">{t("dash.accounts")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.audit")}</h2>
          <p className="ds-sub" style={{ margin: "0 0 14px" }}>{t("dash.auditHint")}</p>
          <div className="ds-actions" style={{ justifyContent: "flex-start" }}>
            <Link href="/admin/audit" className="ds-btn ds-btn--sm">{t("dash.audit")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("dash.systemStatus")}</h2>
          <div className="ds-kv">
            <span className="ds-muted">{t("dash.backend")}</span>
            <span>
              <span
                className={"ds-led " + (online ? "ds-led--green" : "ds-led--red")}
                aria-hidden
              />{" "}
              {online ? t("dash.online") : t("dash.offline")}
            </span>
          </div>
        </section>
      </div>
    </div>
  );
}

export function Dashboard() {
  const t = useT();
  const { user, loading, isAdmin } = useUser();

  if (loading) {
    return (
      <div className="ds-page">
        <div className="ds-loading">{t("common.loading")}</div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="ds-page">
        <div className="ds-empty">{t("dash.notLoggedIn")}</div>
      </div>
    );
  }

  return isAdmin ? <AdminDashboard /> : <MemberDashboard />;
}
