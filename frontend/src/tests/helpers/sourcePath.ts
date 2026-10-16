// Phase 7C.2E — robust source-file path resolver for tests.
//
// Layout-invariant tests read React component source via
// ``fs.promises.readFile`` to enforce invariants that are
// inconvenient to express through the rendered DOM (e.g.
// "TopNavigation must not embed <ProviderSelector> directly").
//
// ``process.cwd()`` is fragile — vitest may run from the project
// root or the frontend directory depending on how the suite is
// invoked.  This helper anchors on the helper file's OWN
// ``import.meta.url`` (which IS populated at module load time by
// Vite's transform) and walks up the parent directory chain
// looking for the frontend project's ``package.json``.  Once
// the project root is known, the caller-supplied relative path
// is resolved from there.  This is robust across cwd and across
// vitest versions — no ``process.cwd()`` or test-state
// introspection is involved.
//
// Usage:
//
//     const source = await readSourceFile("../components/TopNavigation.tsx")
//
// The path is resolved relative to the frontend project root.

import { readFile } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HELPER_PATH = fileURLToPath(import.meta.url);
let _projectRoot: string | null = null;

function findProjectRoot(): string {
  if (_projectRoot) return _projectRoot;
  let dir = dirname(HELPER_PATH);
  for (let i = 0; i < 12; i += 1) {
    try {
      // The frontend project's package.json is the unambiguous
      // anchor.  We use ``require``-style synchronous existence
      // check via ``stat``-equivalent to keep this dependency-free.
      // Node's ``existsSync`` would work but adds a top-level
      // import; we use the lighter ``access`` via fs.statSync.
      // Avoid pulling fs at module top — only here, lazily.
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const fs = require("node:fs") as typeof import("node:fs");
      const candidate = resolve(dir, "package.json");
      if (fs.existsSync(candidate)) {
        _projectRoot = dir;
        return dir;
      }
    } catch {
      // ignore and walk up
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(
    "readSourceFile: could not locate frontend/package.json by walking up from " +
      HELPER_PATH,
  );
}

export async function readSourceFile(relativePath: string): Promise<string> {
  const base = findProjectRoot();
  const path = isAbsolute(relativePath) ? relativePath : resolve(base, relativePath);
  return readFile(path, "utf-8");
}
