// @ts-check
const esbuild = require("esbuild");

const watch = process.argv.includes("--watch");
const minify = process.argv.includes("--minify");

const ctx = esbuild.context({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  format: "cjs",
  platform: "node",
  external: ["vscode"],
  outfile: "out/extension.js",
  sourcemap: !minify,
  minify,
});

ctx.then(async (c) => {
  if (watch) {
    await c.watch();
    console.log("[esbuild] watching...");
  } else {
    await c.rebuild();
    await c.dispose();
    console.log("[esbuild] done");
  }
}).catch((e) => {
  console.error(e);
  process.exit(1);
});
