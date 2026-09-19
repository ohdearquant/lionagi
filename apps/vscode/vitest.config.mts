import { defineConfig, type Plugin } from "vitest/config";
import * as fs from "node:fs";
import * as path from "node:path";

/**
 * Inline Vite plugin: rewrite relative *.js specifiers → *.ts when a .ts file
 * actually exists on disk. Required because the source uses Node16-style
 * `import { x } from "./foo.js"` specifiers that TypeScript resolves to `.ts`
 * at compile time, but Vite/Vitest's module resolver looks for a real `.js`
 * file that doesn't exist in the source tree.
 */
function rewriteJsToTs(): Plugin {
  return {
    name: "rewrite-js-to-ts",
    enforce: "pre",
    resolveId(source, importer) {
      if (!importer) return null;
      if (!source.startsWith(".")) return null;
      if (!source.endsWith(".js")) return null;
      const tsPath = path.resolve(path.dirname(importer), source.replace(/\.js$/, ".ts"));
      if (fs.existsSync(tsPath)) {
        return tsPath;
      }
      return null;
    },
  };
}

export default defineConfig({
  plugins: [rewriteJsToTs()],
  resolve: {
    alias: {
      // Alias the bare `vscode` module to our test mock.
      vscode: new URL("./test/mocks/vscode.ts", import.meta.url).pathname,
    },
  },
  test: {
    environment: "node",
    globals: false,
  },
});
