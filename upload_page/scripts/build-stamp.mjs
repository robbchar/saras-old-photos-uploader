// Cross-language twin of build_stamp.py -- see that module's docstring and
// STAMP_ALGORITHM for the full spec. Both implementations must produce a
// byte-identical stamp for the same source tree without ever talking to
// each other; scripts/build-stamp.test.ts pins both sides to the same
// constant over a shared fixture tree.
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const TOP_LEVEL_INPUT_FILES = [
  "index.html",
  "package.json",
  "yarn.lock",
  "tsconfig.json",
  "vite.config.ts",
  "vitest.config.ts",
];

/**
 * True when relPosixPath lives under a dist/, node_modules/, or dotfile
 * directory. Mirrors build_stamp.py's _is_excluded: only the directory
 * components are checked, never the file's own final name component.
 */
function isExcludedPath(relPosixPath) {
  const dirParts = relPosixPath.split("/").slice(0, -1);
  if (dirParts.includes("dist") || dirParts.includes("node_modules")) {
    return true;
  }
  return dirParts.some((part) => part.startsWith("."));
}

function toPosixRelative(pageDir, absPath) {
  return path.relative(pageDir, absPath).split(path.sep).join("/");
}

/** Collects every file under `dir`, recursively, as page-relative POSIX paths. */
function collectSrcFiles(dir, pageDir, out) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const absPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      collectSrcFiles(absPath, pageDir, out);
    } else if (entry.isFile()) {
      const relPath = toPosixRelative(pageDir, absPath);
      if (!isExcludedPath(relPath)) {
        out.push(relPath);
      }
    }
  }
}

/** Every build-input file under pageDir, as page-relative POSIX paths. */
function inputFiles(pageDir) {
  const files = [];
  for (const name of TOP_LEVEL_INPUT_FILES) {
    const candidate = path.join(pageDir, name);
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      files.push(name);
    }
  }

  const srcDir = path.join(pageDir, "src");
  if (existsSync(srcDir) && statSync(srcDir).isDirectory()) {
    collectSrcFiles(srcDir, pageDir, files);
  }

  return files;
}

/** sha256 hex of a file's bytes, with CRLF normalized to LF first. */
function fileHash(pageDir, relPosixPath) {
  const raw = readFileSync(path.join(pageDir, relPosixPath));
  // Byte-level CRLF -> LF, matching Python's raw.replace(b"\r\n", b"\n").
  // Round-tripping through the "binary" (latin1) encoding is lossless for
  // any byte value, so this stays a byte-for-byte replace, not a text one.
  const normalized = Buffer.from(
    raw.toString("binary").split("\r\n").join("\n"),
    "binary"
  );
  return createHash("sha256").update(normalized).digest("hex");
}

/**
 * sha256 hex stamp over the front-end build inputs under pageDir. Must
 * match build_stamp.py's compute_build_stamp byte-for-byte.
 */
export function computeBuildStamp(pageDir) {
  const relPaths = inputFiles(pageDir).sort();
  const manifest = relPaths
    .map((relPath) => `${relPath}\n${fileHash(pageDir, relPath)}\n`)
    .join("");
  return createHash("sha256").update(manifest, "utf-8").digest("hex");
}

function main() {
  const pageDir = path.resolve(fileURLToPath(import.meta.url), "..", "..");
  const stamp = computeBuildStamp(pageDir);
  const distDir = path.join(pageDir, "dist");
  mkdirSync(distDir, { recursive: true });
  writeFileSync(
    path.join(distDir, "build-stamp.json"),
    JSON.stringify({ stamp }) + "\n"
  );
  process.stdout.write(
    "build-stamp: wrote dist/build-stamp.json (" + stamp + ")\n"
  );
}

// ESM equivalent of Python's `if __name__ == "__main__":` -- only run main()
// when this file is the process entry point, not when a test imports it.
const isMainModule =
  process.argv[1] !== undefined &&
  import.meta.url === pathToFileURL(process.argv[1]).href;

if (isMainModule) {
  main();
}
