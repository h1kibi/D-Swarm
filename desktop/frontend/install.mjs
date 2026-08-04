// Wails runs `node install.mjs` in frontend/ before building — make sure
// dist/ exists so the //go:embed directive in main.go always compiles.
// Paths resolve against THIS file, never the cwd (the shell may run us from
// anywhere).
import { mkdirSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const here = dirname(fileURLToPath(import.meta.url));
mkdirSync(join(here, "dist"), { recursive: true });
console.log("frontend install ok");
