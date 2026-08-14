export const WORKER_SETTINGS_SPLIT_STORAGE_KEY = "dswarm.workerSettings.masterWidth";
export const WORKER_SETTINGS_MASTER_MIN = 500;
export const WORKER_SETTINGS_DETAIL_MIN = 460;
export const WORKER_SETTINGS_SPLITTER_WIDTH = 10;
export const WORKER_SETTINGS_MASTER_DEFAULT = 620;

export function clampWorkerSettingsMasterWidth(width: number, containerWidth: number): number {
  const safeContainer = Number.isFinite(containerWidth) && containerWidth > 0
    ? containerWidth
    : WORKER_SETTINGS_MASTER_DEFAULT + WORKER_SETTINGS_DETAIL_MIN + WORKER_SETTINGS_SPLITTER_WIDTH;
  const max = Math.max(
    WORKER_SETTINGS_MASTER_MIN,
    Math.floor(safeContainer - WORKER_SETTINGS_DETAIL_MIN - WORKER_SETTINGS_SPLITTER_WIDTH),
  );
  const safeWidth = Number.isFinite(width) ? width : WORKER_SETTINGS_MASTER_DEFAULT;
  return Math.min(max, Math.max(WORKER_SETTINGS_MASTER_MIN, Math.round(safeWidth)));
}

export function defaultWorkerSettingsMasterWidth(containerWidth: number): number {
  return clampWorkerSettingsMasterWidth(
    Math.round(containerWidth * 0.45),
    containerWidth,
  );
}
