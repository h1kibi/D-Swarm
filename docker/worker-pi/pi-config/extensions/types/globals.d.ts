/**
 * Minimal Node/console globals for offline `tsc --noEmit` of the pi
 * extensions (the host-side checker has no @types/node installed).
 */
declare const process: { env: Record<string, string | undefined> };
declare const console: {
  log(...args: unknown[]): void;
  error(...args: unknown[]): void;
  warn(...args: unknown[]): void;
};

declare module "node:fs" {
  export function mkdirSync(path: string, options?: { recursive?: boolean }): void;
  export function appendFileSync(path: string, data: string, encoding?: "utf8"): void;
}

declare module "node:path" {
  export function join(...parts: string[]): string;
}
