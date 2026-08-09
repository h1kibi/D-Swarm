import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "D-Swarm",
  description: "D-Swarm — observe and command the autonomous CTF solver swarm.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
