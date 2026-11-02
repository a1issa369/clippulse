// Bundles the app with esbuild and writes a static site to dist/.
//   npm run build   production build, hashed file names, minified
//   npm run watch   rebuild on change (use with: python ../backend/dev_server.py)
import * as esbuild from "esbuild";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { basename } from "node:path";

const watch = process.argv.includes("--watch");

async function writeHtml(result) {
  const outputs = Object.keys(result.metafile.outputs).map((p) => basename(p));
  const css = outputs.find((f) => f.endsWith(".css"));
  const js = outputs.find((f) => f.endsWith(".js"));
  const template = await readFile("index.html", "utf8");
  const tags = `<link rel="stylesheet" href="/assets/${css}" />\n    <script type="module" src="/assets/${js}"></script>`;
  await writeFile("dist/index.html", template.replace("<!--ASSETS-->", tags));
}

await rm("dist", { recursive: true, force: true });
await mkdir("dist/assets", { recursive: true });
await cp("public", "dist", { recursive: true });

const options = {
  entryPoints: ["src/main.jsx"],
  bundle: true,
  outdir: "dist/assets",
  entryNames: watch ? "[name]" : "[name]-[hash]",
  format: "esm",
  target: ["es2020"],
  jsx: "automatic",
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  metafile: true,
  logLevel: "info",
  define: { "process.env.NODE_ENV": JSON.stringify(watch ? "development" : "production") },
  plugins: [{
    name: "html",
    setup(build) { build.onEnd(async (result) => { if (!result.errors.length) await writeHtml(result); }); },
  }],
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching src/ ...");
} else {
  await esbuild.build(options);
}
