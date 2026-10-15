"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useUser, logout } from "@/lib/useUser";
import { useT, useLang } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { Icon } from "@/components/Icon";

/**
 * Top navigation for the management pages. Spec-matched to the deck's TopBar
 * (44px, --bg2 ground, --border hairline, 800-weight "D-Swarm" lockup, the
 * deck's own icon-btn/lang-btn toggles and theme/lang i18n keys) so navigating
 * between the console and the deck reads as one bar with swapped content.
 */

const NAV: { href: string; key: string; admin?: boolean }[] = [
  { href: "/", key: "nav.dashboard" },
  { href: "/events", key: "nav.events" },
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

  const online = Boolean(user?.id);

  return (
    <nav className="ds-nav">
      <Link href="/" className="ds-nav-brand">
        <span>D-Swarm</span>
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
        <span className="ds-nav-status">
          <span className={"ds-led " + (online ? "ds-led--green" : "ds-led--red")} aria-hidden />
          {online ? t("dash.online") : t("dash.offline")}
        </span>
        <button
          type="button"
          className="icon-btn"
          onClick={toggleTheme}
          title={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}
          aria-label={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} />
        </button>
        <button
          type="button"
          className="lang-btn"
          onClick={() => setLang(lang === "zh" ? "en" : "zh")}
          title={t("lang.toggleTitle")}
          aria-label={t("lang.toggleTitle")}
        >
          {t("lang.toggle")}
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
