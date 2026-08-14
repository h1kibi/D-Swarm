"use client";

import { LoginGate } from "@/components/LoginGate";
import { WorkerSettingsWorkspace } from "@/components/WorkerSettingsWorkspace";
import { I18nProvider } from "@/lib/i18n";

export default function WorkerSettingsPage() {
  return <I18nProvider><LoginGate><WorkerSettingsWorkspace /></LoginGate></I18nProvider>;
}
