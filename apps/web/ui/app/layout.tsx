import type { Metadata } from "next";
import "./globals.css";
import "./shell.css";
import { I18nProvider } from "@/lib/i18n";
import { LoginGate } from "@/components/LoginGate";
import { NavShell } from "@/components/Navigation/NavShell";

export const metadata: Metadata = {
  title: "D-Swarm",
  description: "D-Swarm — observe and command the autonomous CTF solver swarm.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" data-theme="dark">
      <body>
        <I18nProvider>
          <LoginGate>
            <NavShell>{children}</NavShell>
          </LoginGate>
        </I18nProvider>
      </body>
    </html>
  );
}
