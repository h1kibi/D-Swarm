import type { Metadata } from "next";
import { MainNav } from "@/components/Navigation/MainNav";
import { LoginGate } from "@/components/LoginGate";
import "./globals.css";

export const metadata: Metadata = {
  title: "D-Swarm",
  description: "D-Swarm — observe and command the autonomous CTF solver swarm.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" data-theme="dark">
      <body>
        <LoginGate>
          <MainNav />
          <main>{children}</main>
        </LoginGate>
      </body>
    </html>
  );
}
