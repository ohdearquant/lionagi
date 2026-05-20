/**
 * Node.js ESM loader that resolves extensionless TypeScript imports.
 * Used by: node --experimental-strip-types --loader ./ts-loader.mjs --test ...
 */
import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve as resolvePath } from "node:path";

export async function resolve(specifier, context, nextResolve) {
  // If the specifier is a relative path without extension, try adding .ts
  if (specifier.startsWith(".") && !specifier.match(/\.\w+$/)) {
    const parentDir = context.parentURL
      ? resolvePath(fileURLToPath(context.parentURL), "..")
      : process.cwd();
    const candidate = resolvePath(parentDir, specifier + ".ts");
    if (existsSync(candidate)) {
      return nextResolve(pathToFileURL(candidate).href, context);
    }
  }
  return nextResolve(specifier, context);
}
