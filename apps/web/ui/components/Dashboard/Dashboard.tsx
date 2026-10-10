"use client";

import { useUser } from "@/lib/useUser";
import { useEvents } from "@/lib/useEventsApi";
import { useTargets } from "@/lib/useTargetsApi";
import { useRuns } from "@/lib/useRunsApi";
import Link from "next/link";
import styles from "./Dashboard.module.css";

function MemberDashboard() {
  const { events, loading: eventsLoading, error: eventsError } = useEvents();
  const { targets, loading: targetsLoading, error: targetsError } = useTargets();
  const { runs, loading: runsLoading, error: runsError } = useRuns();

  const unsolvedTargets = targets.filter((t) => !t.solved);
  const solvedTargets = targets.filter((t) => t.solved);
  const activeRuns = runs.filter((r) => r.status === "running" || r.status === "pending");
  const completedRuns = runs.filter((r) => r.status === "completed");

  return (
    <div className={styles.dashboard}>
      <div className={styles.header}>
        <h1>Welcome to D-Swarm</h1>
        <p>Your CTF solving platform</p>
      </div>

      <div className={styles.grid}>
        <div className={styles.card}>
          <div className={styles.cardHeader}>
            <h2>📌 Active Events</h2>
            {eventsError && <span className={styles.error}>{eventsError}</span>}
          </div>
          {eventsLoading ? (
            <p className={styles.loading}>Loading...</p>
          ) : events.length === 0 ? (
            <p className={styles.empty}>No events available</p>
          ) : (
            <ul className={styles.list}>
              {events.filter((e) => e.status === "active").map((event) => (
                <li key={event.id}>
                  <Link href={`/events/${event.id}`}>{event.name}</Link>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className={styles.card}>
          <div className={styles.cardHeader}>
            <h2>🎯 Targets</h2>
          </div>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{unsolvedTargets.length}</div>
              <div className={styles.statLabel}>Unsolved</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{solvedTargets.length}</div>
              <div className={styles.statLabel}>Solved</div>
            </div>
          </div>
          <Link href="/targets" className={styles.button}>
            View All
          </Link>
        </div>

        <div className={styles.card}>
          <div className={styles.cardHeader}>
            <h2>⚙️ Runs</h2>
          </div>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{activeRuns.length}</div>
              <div className={styles.statLabel}>Active</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{completedRuns.length}</div>
              <div className={styles.statLabel}>Completed</div>
            </div>
          </div>
          <Link href="/runs" className={styles.button}>
            View All
          </Link>
        </div>

        <div className={styles.card}>
          <div className={styles.cardHeader}>
            <h2>🚀 Quick Actions</h2>
          </div>
          <div className={styles.actions}>
            <Link href="/runs?mode=launch" className={styles.actionButton}>
              Start New Run
            </Link>
            <Link href="/targets?mode=create" className={styles.actionButton}>
              Create Target
            </Link>
            <Link href="/events?mode=create" className={styles.actionButton}>
              Create Event
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function AdminDashboard() {
  const { events, loading: eventsLoading } = useEvents();
  const { runs, loading: runsLoading } = useRuns();

  const totalEvents = events.length;
  const activeEvents = events.filter((e) => e.status === "active").length;
  const totalRuns = runs.length;
  const runningRuns = runs.filter((r) => r.status === "running").length;

  return (
    <div className={styles.dashboard}>
      <div className={styles.header}>
        <h1>Admin Dashboard</h1>
        <p>Manage events, accounts, and system resources</p>
      </div>

      <div className={styles.grid}>
        <div className={styles.card}>
          <h2>📅 Events Overview</h2>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{totalEvents}</div>
              <div className={styles.statLabel}>Total</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{activeEvents}</div>
              <div className={styles.statLabel}>Active</div>
            </div>
          </div>
          <Link href="/admin/events" className={styles.button}>
            Manage Events
          </Link>
        </div>

        <div className={styles.card}>
          <h2>⚙️ Runs Overview</h2>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{totalRuns}</div>
              <div className={styles.statLabel}>Total</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{runningRuns}</div>
              <div className={styles.statLabel}>Running</div>
            </div>
          </div>
          <Link href="/runs" className={styles.button}>
            View All Runs
          </Link>
        </div>

        <div className={styles.card}>
          <h2>👥 Account Management</h2>
          <p className={styles.description}>Manage team members and permissions</p>
          <Link href="/admin/accounts" className={styles.button}>
            Manage Accounts
          </Link>
        </div>

        <div className={styles.card}>
          <h2>📋 Audit Log</h2>
          <p className={styles.description}>Track system activity and changes</p>
          <Link href="/admin/audit" className={styles.button}>
            View Audit Log
          </Link>
        </div>

        <div className={styles.card}>
          <h2>🎯 Event Management</h2>
          <p className={styles.description}>Create and manage CTF events</p>
          <Link href="/events?mode=create" className={styles.button}>
            New Event
          </Link>
        </div>

        <div className={styles.card}>
          <h2>⚡ System Status</h2>
          <div className={styles.status}>
            <div className={styles.statusItem}>
              <span className={styles.statusDot + " " + styles.online}></span>
              <span>Backend: Online</span>
            </div>
            <div className={styles.statusItem}>
              <span className={styles.statusDot + " " + styles.online}></span>
              <span>Database: Online</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Dashboard() {
  const { user, loading, isAdmin } = useUser();

  if (loading) {
    return <div className={styles.loading}>Loading dashboard...</div>;
  }

  if (!user) {
    return <div className={styles.error}>Please log in to continue</div>;
  }

  return isAdmin ? <AdminDashboard /> : <MemberDashboard />;
}
