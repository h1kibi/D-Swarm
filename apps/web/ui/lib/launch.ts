export function workerBackendOverride(containerMode?: boolean): Record<string, "local" | "container"> {
  if (containerMode == null) return {};
  return { worker_backend: containerMode ? "container" : "local" };
}
