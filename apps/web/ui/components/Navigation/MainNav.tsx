"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useUser, logout } from "@/lib/useUser";
import { useT, useLang } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";

const NAV: { href: string; key: string; admin?: boolean }[] = [
  { href: "/", key: "nav.dashboard" },
  { href: "/deck", key: "nav.deck" },
  { href: "/events", key: "nav.events" },
  { href: "/targets", key: "nav.targets" },
  { href: "/runs", key: "nav.runs" },
  { href: "/analytics", key: "nav.analytics" },
  { href: "/admin/accounts", key: "nav.accounts", admin: true },
  { href: "/admin/audit", key: "nav.audit", admin: true },
  { href: "/settings", key: "nav.settings" },
];

export function MainNav() {
  const pathname = usePathname();
  const t = useT();
  const { lang, setLang } = useLang();
  const { theme, toggleTheme } = useTheme();
  const { user, isAdmin } = useUser();

  const handleLogout = async () => {
    await logout();
    window.location.href = "/";
  };

  const isActive = (path: string) =>
    path === "/" ? pathname === "/" : pathname === path || pathname?.startsWith(path + "/");

  return (
    <nav className="ds-nav">
      <Link href="/" className="ds-nav-brand">
        <span className="ds-led ds-led--cyan" aria-hidden />
        D-SWARM
      </Link>

      {NAV.filter((item) => !item.admin || isAdmin).map((item) => (
        <Link
          key={item.href}
          href={item.href}
          className={"ds-nav-link" + (isActive(item.href) ? " ds-nav-link--on" : "")}
        >
          {t(item.key)}
        </Link>
      ))}

      <span className="ds-nav-spacer" />

      <span className="ds-nav-side">
        <button
          type="button"
          className="ds-nav-btn"
          onClick={toggleTheme}
          title={t("nav.theme")}
          aria-label={t("nav.theme")}
        >
          {theme === "dark" ? "◐ light" : "◑ dark"}
        </button>
        <button
          type="button"
          className="ds-nav-btn"
          onClick={() => setLang(lang === "zh" ? "en" : "zh")}
          title={t("nav.lang")}
          aria-label={t("nav.lang")}
        >
          {lang === "zh" ? "EN" : "中"}
        </button>
        {user && (
          <>
            <span className="ds-nav-user">{user.display_name || user.username}</span>
            <span className={"ds-badge " + (isAdmin ? "ds-badge--red" : "ds-badge--accent")}>
              {isAdmin ? t("role.admin") : t("role.member")}
            </span>
            <button type="button" className="ds-nav-btn" onClick={handleLogout}>
              {t("nav.logout")}
            </button>
          </>
        )}
      </span>
    </nav>
  );
}
