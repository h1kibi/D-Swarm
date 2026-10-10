"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useUser, logout } from "@/lib/useUser";
import styles from "./MainNav.module.css";

export function MainNav() {
  const pathname = usePathname();
  const { user, isAdmin } = useUser();

  const handleLogout = async () => {
    await logout();
    window.location.href = "/";
  };

  const isActive = (path: string) => pathname === path || pathname?.startsWith(path + "/");

  return (
    <nav className={styles.navbar}>
      <div className={styles.container}>
        <div className={styles.brand}>
          <Link href="/">D-Swarm</Link>
        </div>

        <ul className={styles.menu}>
          <li>
            <Link href="/" className={isActive("/") ? styles.active : ""}>
              Dashboard
            </Link>
          </li>
          <li>
            <Link href="/events" className={isActive("/events") ? styles.active : ""}>
              Events
            </Link>
          </li>
          <li>
            <Link href="/targets" className={isActive("/targets") ? styles.active : ""}>
              Targets
            </Link>
          </li>
          <li>
            <Link href="/runs" className={isActive("/runs") ? styles.active : ""}>
              Runs
            </Link>
          </li>
          {isAdmin && (
            <>
              <li>
                <Link href="/admin/accounts" className={isActive("/admin/accounts") ? styles.active : ""}>
                  Accounts
                </Link>
              </li>
              <li>
                <Link href="/admin/audit" className={isActive("/admin/audit") ? styles.active : ""}>
                  Audit
                </Link>
              </li>
            </>
          )}
        </ul>

        <div className={styles.userMenu}>
          {user && (
            <>
              <span className={styles.username}>{user.display_name}</span>
              <span className={styles.badge}>{user.role === "admin" ? "Admin" : "Member"}</span>
              <Link href="/settings" className={styles.settings}>
                Settings
              </Link>
              <button onClick={handleLogout} className={styles.logout}>
                Logout
              </button>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}
