#!/usr/bin/env node
/**
 * Render AwsIncidentsBench film scripts (hub/films/*.json) to MP4 with film.html in headless Chrome.
 *
 *   node tools/render_films.mjs hub/films/<script>.json [more.json ...] [--out DIR]
 *   node tools/render_films.mjs --all [--out DIR]          every script in hub/films/ (not hub/films/stages/)
 *
 * Options: --out DIR (default <tmpdir>/awsincidentsbench-films, outside the repo), --workers N (parallel pages,
 * default half the cores, at most 8), --fps 30, --crf 18, --max-mb 8 (per 45 s, scaled to the film's length),
 * --chrome PATH (default the system Google Chrome), --playwright DIR (a folder holding node_modules/playwright-core;
 * also PLAYWRIGHT_CORE), --data URL (run JSON folder, relative to film.html), --stills (default on: a PNG at each
 * scene's midpoint and each caption for review; --no-stills), --stills-only, --check N (re-draw N frames in fresh
 * ?frame= pages and compare pixels), --keep-frames.
 *
 * Writes <id>.mp4 (H.264, yuv420p, +faststart, 1280x720), <id>.poster.png (the script's `poster` second, else the
 * first decision scene's middle), <id>.vtt (the captions) and <id>-stills/. Every frame goes through the same
 * drawFrame(n) that film.html?script=...&frame=n&fps=... draws, so a frame is reproducible from its URL alone. Speed
 * comes from loading each worker page once and drawing its run of frames in place, many pages at a time.
 */
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync, createReadStream } from 'node:fs';
import { cpus, tmpdir } from 'node:os';
import { basename, dirname, extname, join, relative, resolve, sep } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const HUB = join(ROOT, 'hub');
const FILMS = join(HUB, 'films');
const PAGE = '/space/app/public/viz/film.html';
const FFMPEG = existsSync('/opt/homebrew/bin/ffmpeg') ? '/opt/homebrew/bin/ffmpeg' : 'ffmpeg';
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

function args(argv){
  const o = {scripts: [], out: join(tmpdir(), 'awsincidentsbench-films'), workers: Math.max(1, Math.min(8, Math.floor(cpus().length / 2))), fps: 30, crf: 18,
    maxMb: 8, chrome: CHROME, playwright: process.env.PLAYWRIGHT_CORE || null, data: null, stills: true, stillsOnly: false, check: 0, keep: false, all: false};
  for (let i = 0; i < argv.length; i++){
    const a = argv[i], v = () => argv[++i];
    if (a === '--all') o.all = true; else if (a === '--out') o.out = resolve(v()); else if (a === '--workers') o.workers = +v();
    else if (a === '--fps') o.fps = +v(); else if (a === '--crf') o.crf = +v(); else if (a === '--max-mb') o.maxMb = +v();
    else if (a === '--chrome') o.chrome = v(); else if (a === '--playwright') o.playwright = v(); else if (a === '--data') o.data = v();
    else if (a === '--no-stills') o.stills = false; else if (a === '--stills-only') o.stillsOnly = true; else if (a === '--check') o.check = +v(); else if (a === '--keep-frames') o.keep = true;
    else if (a.startsWith('--')) throw new Error('unknown option ' + a); else o.scripts.push(resolve(a));
  }
  if (o.all) for (const f of readdirSync(FILMS).sort()) if (f.endsWith('.json')) o.scripts.push(join(FILMS, f));
  if (!o.scripts.length) throw new Error('name a script or pass --all');
  return o;
}

function loadPlaywright(dir){
  const tries = [dir, process.cwd(), ROOT].filter(Boolean);
  for (const d of tries){
    try { return createRequire(join(resolve(d), 'noop.js'))('playwright-core'); } catch (e) {}
  }
  throw new Error('playwright-core not found: npm install playwright-core into a folder and pass --playwright DIR');
}

const TYPES = {'.html':'text/html; charset=utf-8', '.json':'application/json', '.js':'text/javascript', '.mjs':'text/javascript',
  '.css':'text/css', '.png':'image/png', '.svg':'image/svg+xml', '.woff2':'font/woff2'};
const EXT = new Map();   // /__ext/<k>/ -> a script folder outside hub/
function serve(root){
  return new Promise(ok => {
    const srv = createServer((req, res) => {
      let p = decodeURIComponent(new URL(req.url, 'http://x').pathname), base = root;
      const m = /^\/__ext\/(\d+)(\/.*)$/.exec(p); if (m && EXT.has(m[1])){ base = EXT.get(m[1]); p = m[2]; }
      const f = resolve(join(base, p));
      if (!(f + sep).startsWith(resolve(base) + sep)) { res.writeHead(403).end(); return; }
      if (!existsSync(f) || !statSync(f).isFile()) { res.writeHead(404).end(); return; }
      res.writeHead(200, {'content-type': TYPES[extname(f)] || 'application/octet-stream', 'cache-control': 'no-store'});
      createReadStream(f).pipe(res);
    }).listen(0, '127.0.0.1', () => ok(srv));
  });
}

const b64 = u => Buffer.from(u.slice(u.indexOf(',') + 1), 'base64');

async function openFilm(browser, base, scriptRel, frame, o){
  const page = await browser.newPage({viewport: {width: 1280, height: 720}, deviceScaleFactor: 1});
  const q = new URLSearchParams({script: '/' + scriptRel, frame: String(frame), fps: String(o.fps)});
  if (o.data) q.set('data', o.data);
  const errs = []; page.on('pageerror', e => errs.push(String(e))); page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
  await page.goto(base + PAGE + '?' + q, {waitUntil: 'load'});
  await page.waitForFunction(() => window.__film && (window.__film.error || document.documentElement.hasAttribute('data-frame')), null, {timeout: 60000});
  const err = await page.evaluate(() => window.__film.error || null);
  if (err || errs.length) throw new Error('film page: ' + (err || errs.join('; ')));
  return page;
}
const grab = (page, n, type) => page.evaluate(([n, type]) => { window.__film.drawFrame(n); return document.getElementById('cv').toDataURL(type, 0.95); }, [n, type]);

async function render(browser, base, script, o){
  let rel = relative(HUB, script).split(sep).join('/');
  if (rel.startsWith('..')){ const k = String(EXT.size); EXT.set(k, dirname(script)); rel = '__ext/' + k + '/' + basename(script); }
  const S = JSON.parse(readFileSync(script, 'utf8'));
  const id = S.id || basename(script, '.json');
  mkdirSync(o.out, {recursive: true});
  const work = join(tmpdir(), 'films-' + id + '-' + process.pid); rmSync(work, {recursive: true, force: true}); mkdirSync(work, {recursive: true});
  const t0 = Date.now();
  const probe = await openFilm(browser, base, rel, 0, o);
  const N = await probe.evaluate(() => window.__film.frames());
  const dur = await probe.evaluate(() => window.__film.duration());
  writeFileSync(join(o.out, id + '.vtt'), await probe.evaluate(() => window.__film.captionsVTT()));
  const decision = (S.scenes || []).find(s => s.view === 'decision');
  const posterT = S.poster != null ? S.poster : decision ? (decision.start + decision.end) / 2 : dur / 2;
  writeFileSync(join(o.out, id + '.poster.png'), b64(await grab(probe, Math.round(posterT * o.fps), 'image/png')));
  const stills = [];
  if (o.stills){
    const sd = join(o.out, id + '-stills'); mkdirSync(sd, {recursive: true});
    for (const s of S.scenes){
      for (const [tag, t] of [['mid', (s.start + s.end) / 2], ...(s.captions || []).map((c, i) => ['cap' + (i + 1), (c.start + c.end) / 2])]){
        const n = Math.round(t * o.fps), f = join(sd, `${String(n).padStart(5, '0')}-${s.id}-${tag}.png`);
        writeFileSync(f, b64(await grab(probe, n, 'image/png'))); stills.push(f);
      }
    }
  }
  await probe.close();
  if (o.stillsOnly) return {id, dur, frames: N, stills, poster: join(o.out, id + '.poster.png'), vtt: join(o.out, id + '.vtt')};
  // frames: contiguous chunks, one page per worker, each loaded once
  const W = Math.max(1, Math.min(o.workers, N)), chunk = Math.ceil(N / W);
  await Promise.all(Array.from({length: W}, async (_, w) => {
    const a = w * chunk, b = Math.min(N, a + chunk); if (a >= b) return;
    const page = await openFilm(browser, base, rel, a, o);
    for (let n = a; n < b; n++) writeFileSync(join(work, String(n).padStart(5, '0') + '.jpg'), b64(await grab(page, n, 'image/jpeg')));
    await page.close();
  }));
  const tFrames = (Date.now() - t0) / 1000;
  // determinism: a fresh ?frame=n page must draw the same pixels as the worker that drew n in place
  let checked = 0;
  for (let k = 0; k < o.check; k++){
    const n = Math.floor((k + .5) * N / o.check);
    const page = await openFilm(browser, base, rel, n, o);
    const fresh = await page.evaluate(() => document.getElementById('cv').toDataURL('image/png'));
    const again = await grab(page, n, 'image/png');
    const other = await openFilm(browser, base, rel, Math.max(0, n - 37), o);
    const seeked = await grab(other, n, 'image/png');
    await page.close(); await other.close();
    if (fresh !== again || fresh !== seeked) throw new Error(`frame ${n} differs between a fresh ?frame= page and an in-place draw`);
    checked++;
  }
  // encode, tightening the rate until the file fits the budget
  const out = join(o.out, id + '.mp4'), budget = o.maxMb * 1024 * 1024 * Math.max(1, dur / 45);
  let crf = o.crf, size = 0;
  for (let attempt = 0; attempt < 5; attempt++){
    const kbps = Math.floor(budget * 8 / dur / 1000 * 0.92);
    const r = spawnSync(FFMPEG, ['-y', '-loglevel', 'error', '-framerate', String(o.fps), '-i', join(work, '%05d.jpg'),
      '-c:v', 'libx264', '-preset', 'slow', '-crf', String(crf), '-maxrate', kbps + 'k', '-bufsize', (2 * kbps) + 'k',
      '-vf', 'scale=in_range=pc:out_range=tv,format=yuv420p', '-color_range', 'tv', '-pix_fmt', 'yuv420p', '-r', String(o.fps), '-movflags', '+faststart', out], {encoding: 'utf8'});
    if (r.status !== 0) throw new Error('ffmpeg: ' + r.stderr);
    size = statSync(out).size;
    if (size <= budget) break;
    crf += 3;
  }
  if (!o.keep) rmSync(work, {recursive: true, force: true});
  return {id, out, size, dur, frames: N, crf, secs: (Date.now() - t0) / 1000, frameSecs: tFrames, workers: W, checked,
    poster: join(o.out, id + '.poster.png'), vtt: join(o.out, id + '.vtt'), stills, frames_dir: o.keep ? work : null};
}

const o = args(process.argv.slice(2));
const {chromium} = loadPlaywright(o.playwright);
const srv = await serve(HUB);
const base = 'http://127.0.0.1:' + srv.address().port;
const browser = await chromium.launch({executablePath: existsSync(o.chrome) ? o.chrome : undefined, headless: true,
  args: ['--force-color-profile=srgb', '--font-render-hinting=none', '--hide-scrollbars', '--disable-lcd-text']});
let code = 0;
try {
  for (const s of o.scripts){
    const r = await render(browser, base, s, o);
    console.log(JSON.stringify(Object.assign(r, r.size ? {mb: +(r.size / 1048576).toFixed(2)} : {}), null, 1));
  }
} catch (e) { console.error(String(e && e.stack || e)); code = 1; }
await browser.close(); srv.close();
process.exit(code);
