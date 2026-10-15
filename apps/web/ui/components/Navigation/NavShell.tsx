"use client";

import { usePathname } from "next/navigation";
import { MainNav } from "./MainNav";

/**
 * Route-aware chrome. The deck (and the per-run deep link that re-exports it)
 * owns its own TopBar/rail layout, so the dashboard nav must NOT stack on top
 * of it (double chrome). Everywhere else the nav plus the shared <main> shell
 * wrap the page content.
 */
const CHROMELESS_PREFIXES = ["/deck", "/run"];

export function NavShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";
  const chromeless = CHROMELESS_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(prefix + "/"),
  );

  if (chromeless) return <>{children}</>;

  return (
    <>
      <MainNav />
      <main className="ds-main">{children}</main>
    </>
  );
}
