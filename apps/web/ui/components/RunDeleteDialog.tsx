"use client";

import { useEffect, useRef, useState } from "react";
import type { RunSummary } from "@/lib/useRun";
import { Icon } from "@/components/Icon";
import { useT } from "@/lib/i18n";


export function RunDeleteDialog({
  run,
  onClose,
  onConfirm,
}: {
  run: RunSummary | null;
  onClose: () => void;
  onConfirm: (run: RunSummary) => Promise<boolean> | boolean;
}) {
  const t = useT();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const keepButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    setDeleting(false);
    setError("");
  }, [run?.run_id]);

  useEffect(() => {
    if (run) keepButtonRef.current?.focus();
  }, [run?.run_id]);

  useEffect(() => {
    if (!run || deleting) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [run?.run_id, deleting, onClose]);

  const confirm = async () => {
    if (!run || deleting) return;
    const targetRun = run;
    setDeleting(true);
    setError("");
    try {
      const ok = await onConfirm(targetRun);
      if (ok) onClose();
      else setError(t("run.delete.failed"));
    } catch {
      setError(t("run.delete.failed"));
    } finally {
      setDeleting(false);
    }
  };

  const targetRun = run;
  const name = targetRun?.name || targetRun?.run_id || "";
  const live = targetRun?.status === "running" || targetRun?.status === "queued" || targetRun?.status === "paused";
  if (!targetRun) return null;
  return (
    <div
      className="run-delete-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !deleting) onClose();
      }}
    >
      <section
        className="run-delete-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-delete-title"
        aria-describedby="run-delete-warning"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="run-delete-head">
          <div className="run-delete-icon"><Icon name="alert" size={19} /></div>
          <div>
            <span className="eyebrow">RUN / DELETE</span>
            <h2 id="run-delete-title">{t("run.delete.title")}</h2>
            <p className="run-delete-name" title={name}>{name} <code>{targetRun.run_id}</code></p>
          </div>
          <button className="target-details-close" type="button" onClick={onClose} disabled={deleting} aria-label={t("run.delete.cancel")}>
            <Icon name="x" size={17} />
          </button>
        </header>
        <div className="run-delete-body">
          {live && <p className="run-delete-live-warning"><Icon name="radio" size={14} />{t("run.delete.liveWarning")}</p>}
          <p id="run-delete-warning" className="run-delete-warning">{t("run.delete.warning")}</p>
          <div className="run-delete-impact">
            <span><Icon name="list" size={13} />{t("run.delete.history")}</span>
            <span><Icon name="folder" size={13} />{t("run.delete.workspace")}</span>
            <span><Icon name="target" size={13} />{t("run.delete.relationship")}</span>
          </div>
          {error && <p className="run-delete-error" role="alert">{error}</p>}
        </div>
        <footer className="run-delete-foot">
          <button ref={keepButtonRef} className="target-ghost-button" type="button" onClick={onClose} disabled={deleting}>{t("run.delete.cancel")}</button>
          <button className="run-delete-confirm" type="button" onClick={() => void confirm()} disabled={deleting}>
            <Icon name="xCircle" size={14} />{deleting ? t("run.delete.deleting") : t("run.delete.confirm")}
          </button>
        </footer>
      </section>
    </div>
  );
}
