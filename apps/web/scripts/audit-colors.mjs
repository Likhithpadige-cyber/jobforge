/** Audit authored and built UI paint. Transparent means no paint, not a seventh color. */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import path from "node:path";
const root = process.cwd();
const approved = new Set(["#0a0f14", "#0f3d3e", "#2e6f73", "#7fb3b8", "#eadcc8", "#c6601f"]);
const failures = [];
let count = 0;
function files(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() ? files(path.join(dir, e.name)) : [path.join(dir, e.name)]);
}
function audit(file, built = false) {
  const s = readFileSync(file, "utf8"); count++;
  const fail = value => failures.push(`${path.relative(root, file)}: ${value}`);
  for (const [literal] of s.matchAll(/#[\da-f]{3,8}\b/gi)) {
    const h = literal.toLowerCase();
    if (!approved.has(h) && !(built && (h === "#0000" || h === "#00000000"))) fail(literal);
  }
  for (const [literal] of s.matchAll(/(?:rgba?|hsla?|oklch|color)\([^)]*\)/gi)) {
    if (built && /^rgba?\(0[ ,]+0[ ,]+0(?:[ ,/]+0)\)$/.test(literal)) continue;
    fail(literal);
  }
  const keywords = new Set(["transparent", "currentcolor", "inherit", "initial", "none"]);
  for (const match of s.matchAll(/\b(?:color|background(?:-color)?|border(?:-\w+)?-color|outline-color|fill|stroke)\s*:\s*([a-z]+)\b(?!\s*\()/gi)) {
    if (!keywords.has(match[1].toLowerCase())) fail(`named paint: ${match[1]}`);
  }
  for (const match of s.matchAll(/(?:fill|stroke)=["']([^"']+)["']/gi)) {
    if (!keywords.has(match[1].toLowerCase()) && !approved.has(match[1].toLowerCase())) fail(`SVG paint: ${match[1]}`);
  }
  if (/gradient\(/i.test(s) || /bg-gradient-/.test(s)) fail("gradient paint");
  if (!built && /(?:bg|text|border|ring|fill|stroke|from|via|to)-(?:white|black|slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:[-/\s"`]|$)/.test(s)) fail("stock/legacy color utility");
  if (!built && /(?:bg|text|border|ring|fill|stroke)-[\w-]+\/(?:\d|\[)/.test(s)) fail("alpha-derived paint");
}
for (const dir of ["app", "components", "lib"]) for (const file of files(path.join(root, dir))) if (/\.(tsx?|css|svg)$/.test(file)) audit(file);
audit(path.join(root, "tailwind.config.ts"));
for (const file of files(path.join(root, "public"))) if (/\.(png|webp|jpe?g|gif|avif)$/.test(file)) failures.push(`${file}: unreviewed raster UI asset`); else if (/\.svg$/.test(file)) audit(file);
if (process.argv.includes("--built")) {
  const css = path.join(root, ".next/static/css");
  if (!existsSync(css)) failures.push("Missing production CSS; run npm run build first");
  else for (const file of files(css).filter(f => f.endsWith(".css"))) audit(file, true);
}
if (failures.length) { process.stderr.write(failures.join("\n") + "\n"); process.exitCode = 1; }
else process.stdout.write(`PASS: ${count} UI files; only six approved paint colors (plus no-paint transparency).\n`);
