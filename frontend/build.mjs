// Build the dashboard into ./dist with esbuild.
//   npm run build            production bundle
//   npm run dev              rebuild on every change (serve it with the Flask backend)
// Env: API_BASE=https://api.example.com  -> call a backend on another origin.
import * as esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(root, 'dist');
const watch = process.argv.includes('--watch');
const { version } = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));

function writeHtml() {
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8')
    .replaceAll('__BUILD__', Date.now().toString(36))
    .replaceAll('__VERSION__', version);
  fs.writeFileSync(path.join(dist, 'index.html'), html);
}

fs.rmSync(dist, { recursive: true, force: true });
fs.mkdirSync(path.join(dist, 'assets'), { recursive: true });

const options = {
  absWorkingDir: root,
  entryPoints: { app: 'src/main.jsx' },
  outdir: 'dist/assets',
  bundle: true,
  format: 'esm',
  target: ['es2020', 'chrome100', 'firefox100', 'safari15'],
  jsx: 'automatic',
  minify: !watch,
  sourcemap: true,
  logLevel: 'info',
  define: {
    'process.env.NODE_ENV': JSON.stringify(watch ? 'development' : 'production'),
    __API_BASE__: JSON.stringify(process.env.API_BASE || ''),
    __APP_VERSION__: JSON.stringify(version),
  },
  plugins: [{
    name: 'html',
    setup(build) {
      build.onEnd((result) => { if (!result.errors.length) writeHtml(); });
    },
  }],
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log('Watching src/ - reload the dashboard after each rebuild.');
} else {
  await esbuild.build(options);
}
