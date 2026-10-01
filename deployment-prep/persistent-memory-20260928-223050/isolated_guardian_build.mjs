#!/usr/bin/env node
// Offline preparation only by default. --build creates an artifact, not a live app.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fork } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SELF = fileURLToPath(import.meta.url);
const VITE_KEYS = [
  'VITE_FIREBASE_API_KEY', 'VITE_FIREBASE_AUTH_DOMAIN',
  'VITE_FIREBASE_DATABASE_URL', 'VITE_FIREBASE_PROJECT_ID',
];
const ISOLATED_AUTH_KEY = 'VITE_ISOLATED_AUTH';
const PROFILE_KEYS = [
  'schema_version', 'test_project_id', 'test_database_url',
  'production_project_id', 'production_database_url',
  'database_project_confirmed', 'elder_id', 'device_id', 'pi', 'guardian',
];
const PI_KEYS = [
  'code_dir', 'working_directory', 'original_code_dir', 'original_state_directory',
  'python', 'openai_key_file', 'firebase_credential_file', 'firebase_uid',
];
// Keep only OS process-launch essentials. Nothing else is inherited by Vite.
const OS_ENV_KEYS = new Set([
  'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'TMPDIR',
  'HOME', 'USERPROFILE', 'LANG', 'LC_ALL', 'NUMBER_OF_PROCESSORS',
]);

function reject(message) { throw new Error(message); }
function assert(condition, message) { if (!condition) reject(message); }
function exactKeys(value, keys, label) {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value), `${label}: object required`);
  const actual = Object.keys(value).sort();
  assert(actual.length === keys.length && actual.every((key, i) => key === [...keys].sort()[i]),
    `${label}: missing or unrecognized fields`);
}
function text(value, label) {
  assert(typeof value === 'string' && value.length > 0 && value.length <= 4096
    && value === value.trim() && !/[\x00-\x1f\x7f<>]/.test(value)
    && !/^(?:CHANGE_ME|REPLACE_ME|TODO|your_api_key_here|your_project_id)$/i.test(value),
  `${label}: nonempty, non-placeholder text required`);
  return value;
}
function projectId(value, label) {
  text(value, label);
  assert(/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(value), `${label}: invalid Firebase project ID`);
  return value;
}
function databaseUrl(value, label) {
  text(value, label);
  let url;
  try { url = new URL(value); } catch { reject(`${label}: invalid URL`); }
  assert(url.protocol === 'https:' && !url.username && !url.password && !url.port
    && !url.search && !url.hash && url.pathname === '/' && !value.includes('\\')
    && /^https:\/\/[^/?#:@]+\/?$/.test(value), `${label}: HTTPS root URL without credentials/port/query required`);
  assert(/^[a-z0-9][a-z0-9-]*\.firebaseio\.com$/.test(url.hostname)
    || /^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*\.firebasedatabase\.app$/.test(url.hostname),
  `${label}: Firebase RTDB hostname required`);
  return url.origin;
}
function validateProfile(profile) {
  exactKeys(profile, PROFILE_KEYS, 'profile');
  assert(profile.schema_version === 2, 'profile: schema_version must be 2');
  const testProject = projectId(profile.test_project_id, 'test_project_id');
  const productionProject = projectId(profile.production_project_id, 'production_project_id');
  const testDatabase = databaseUrl(profile.test_database_url, 'test_database_url');
  const productionDatabase = databaseUrl(profile.production_database_url, 'production_database_url');
  assert(testProject !== productionProject, 'Test and production project IDs must differ');
  assert(testDatabase !== productionDatabase, 'Test and production database hosts must differ');
  assert(profile.database_project_confirmed === true, 'Confirm the database/project mapping in the console first');
  assert(profile.elder_id === 'elder_001' && profile.device_id === 'device_001', 'Guardian currently requires elder_001/device_001');
  exactKeys(profile.pi, PI_KEYS, 'pi');
  for (const key of PI_KEYS) {
    const value = text(profile.pi[key], `pi.${key}`);
    if (key === 'firebase_uid') {
      assert(value.length <= 128 && !/[.#$\[\]/]/.test(value), 'pi.firebase_uid: explicit dedicated device UID required');
    } else assert(path.posix.isAbsolute(value) && !value.includes('\\'), `pi.${key}: absolute Pi path required`);
  }
  exactKeys(profile.guardian, VITE_KEYS, 'guardian');
  for (const key of VITE_KEYS) text(profile.guardian[key], `guardian.${key}`);
  assert(/^AIza[0-9A-Za-z_-]{35}$/.test(profile.guardian.VITE_FIREBASE_API_KEY),
    'Guardian API key must have Firebase public web identifier form; this is not authentication verification');
  assert(profile.guardian.VITE_FIREBASE_PROJECT_ID === testProject, 'Guardian project differs from test project');
  assert(databaseUrl(profile.guardian.VITE_FIREBASE_DATABASE_URL, 'guardian database URL') === testDatabase,
    'Guardian database differs from test database');
  assert(profile.guardian.VITE_FIREBASE_AUTH_DOMAIN === `${testProject}.firebaseapp.com`,
    'Guardian auth domain differs from test project');
  return { testProject, testDatabase };
}
function within(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}
function absolute(value, label) {
  text(value, label);
  assert(path.isAbsolute(value), `${label}: absolute path required`);
  return path.resolve(value);
}
function validatePaths(repoArg, outArg) {
  const repo = absolute(repoArg, '--repo');
  assert(fs.statSync(repo).isDirectory(), '--repo: existing directory required');
  const realRepo = fs.realpathSync(repo);
  const app = path.join(realRepo, 'apps', 'guardian-dashboard');
  assert(fs.statSync(app).isDirectory() && within(realRepo, fs.realpathSync(app)), 'Guardian source is missing or redirected');
  const out = absolute(outArg, '--out');
  assert(!within(repo, out) && !within(out, repo), '--out must be outside the repository');
  assert(!fs.lstatSync(out, { throwIfNoEntry: false }), '--out must be a new, nonexistent directory; existing output is never overwritten');
  const parent = fs.realpathSync(path.dirname(out));
  const resolvedOut = path.join(parent, path.basename(out));
  assert(fs.statSync(parent).isDirectory(), '--out parent must already exist');
  assert(!within(realRepo, resolvedOut) && !within(resolvedOut, realRepo), '--out must be outside the repository');
  assert(!fs.lstatSync(resolvedOut, { throwIfNoEntry: false }), '--out resolves to an existing path');
  assert(!fs.lstatSync(parent).isSymbolicLink(), '--out parent must not be a symlink');
  const vite = path.join(app, 'node_modules', 'vite', 'dist', 'node', 'index.js');
  const react = path.join(app, 'node_modules', '@vitejs', 'plugin-react', 'dist', 'index.js');
  for (const filename of [path.join(app, 'index.html'), path.join(app, 'package.json'), vite, react]) {
    assert(fs.statSync(filename).isFile(), 'Required Guardian source or existing build dependency is missing');
  }
  return { repo: realRepo, app, out: resolvedOut, vite, react };
}
function validateLauncherEnvironment() {
  const banned = Object.keys(process.env).filter(key =>
    /^(?:VITE_|FIREBASE(?:_|$)|OPENAI(?:_|$)|NODE_|GOOGLE_APPLICATION_CREDENTIALS$|GOOGLE_API_KEY$|GOOGLE_CLOUD_PROJECT$|GCLOUD_PROJECT$)/i.test(key));
  assert(banned.length === 0, 'Ambient VITE/Firebase/OpenAI/Node or Google credential/project settings are forbidden; use a clean launcher environment');
  // NODE_OPTIONS or --require/--import can execute before this file. This check
  // refuses contaminated launches; it cannot undo a preload already executed.
  assert(process.execArgv.length === 0, 'Node runtime options are forbidden; launch this file directly without preloads/env-file flags');
}
function loadProfile(filename) {
  const profilePath = absolute(filename, '--profile');
  const base = path.basename(profilePath).toLowerCase();
  assert(base.endsWith('.json') && !base.startsWith('.')
    && !/credential|service[-_]?account|private[-_]?key/.test(base), '--profile must name a public configuration JSON, not a secret file');
  const stat = fs.lstatSync(profilePath);
  assert(stat.isFile() && !stat.isSymbolicLink() && stat.size <= 65536, 'Public profile must be a regular JSON file of at most 64 KiB');
  let profile;
  try { profile = JSON.parse(fs.readFileSync(profilePath, 'utf8').replace(/^\uFEFF/, '')); }
  catch { reject('Public profile is not valid JSON'); }
  validateProfile(profile);
  return profile;
}
function options(args) {
  const result = { build: false };
  let mode;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === '--build' || arg === '--dry-run') {
      assert(mode === undefined, 'Specify at most one of --build/--dry-run');
      mode = arg;
      result.build = arg === '--build';
    } else if (['--profile', '--repo', '--out'].includes(arg)) {
      const key = arg.slice(2);
      assert(result[key] === undefined && i + 1 < args.length && !args[i + 1].startsWith('--'), 'Missing or duplicate option');
      result[key] = args[++i];
    } else reject('Unknown option; expected --profile ABS_JSON --repo ABS --out ABS [--dry-run|--build]');
  }
  assert(result.profile && result.repo && result.out, 'Required: --profile ABS_JSON --repo ABS --out ABS; default is --dry-run');
  return result;
}
function childEnvironment(publicValues) {
  const env = {};
  for (const key of Object.keys(process.env)) {
    if (OS_ENV_KEYS.has(key.toUpperCase())) env[key] = process.env[key];
  }
  for (const key of VITE_KEYS) env[key] = publicValues[key];
  env[ISOLATED_AUTH_KEY] = 'true';
  return env;
}
async function buildWorker() {
  assert(typeof process.send === 'function' && process.connected, 'Internal worker requires its parent IPC channel');
  let used = false;
  process.on('message', async message => {
    if (used) return;
    used = true;
    let envDir;
    try {
      exactKeys(message, ['profile', 'repo', 'out'], 'worker payload');
      validateProfile(message.profile);
      const paths = validatePaths(message.repo, message.out);
      for (const [key, value] of Object.entries(process.env)) {
        if (VITE_KEYS.includes(key)) assert(value === message.profile.guardian[key], 'Worker public environment mismatch');
        else if (key === ISOLATED_AUTH_KEY) assert(value === 'true', 'Isolated auth must be enabled');
        else assert(OS_ENV_KEYS.has(key.toUpperCase()), 'Worker received an unexpected inherited variable');
      }
      for (const key of VITE_KEYS) assert(process.env[key] === message.profile.guardian[key], 'Worker is missing a public Firebase setting');
      assert(process.env[ISOLATED_AUTH_KEY] === 'true', 'Worker is missing isolated auth mode');
      // Create only a fresh external output. No existing directory is emptied.
      fs.mkdirSync(paths.out, { mode: 0o700 });
      envDir = fs.mkdtempSync(path.join(os.tmpdir(), 'doran-guardian-empty-env-'));
      const [{ build }, { default: react }] = await Promise.all([
        import(pathToFileURL(paths.vite).href), import(pathToFileURL(paths.react).href),
      ]);
      await build({
        root: paths.app,
        configFile: false,
        envFile: false,
        envDir,
        mode: 'production',
        publicDir: false,
        cacheDir: path.join(envDir, 'vite-cache'),
        plugins: [react({ babel: { babelrc: false, configFile: false } })],
        css: { postcss: { plugins: [] } },
        define: { ...Object.fromEntries(VITE_KEYS.map(key => [
          `import.meta.env.${key}`, JSON.stringify(message.profile.guardian[key]),
        ])), 'import.meta.env.VITE_ISOLATED_AUTH': JSON.stringify('true') },
        logLevel: 'silent',
        clearScreen: false,
        build: { outDir: paths.out, emptyOutDir: false, sourcemap: false },
      });
      process.send({ ok: true });
      process.disconnect();
    } catch {
      // Do not echo bundler errors: expanded source may contain configuration.
      process.send({ ok: false });
      process.disconnect();
      process.exitCode = 1;
    } finally {
      // Remove only the newly created, empty env directory. Keep any build
      // cache/output for inspection rather than recursively deleting files.
      if (envDir) {
        try { fs.rmdirSync(envDir); } catch { /* owned nonempty directory retained */ }
      }
    }
  });
}
async function main() {
  validateLauncherEnvironment();
  const args = options(process.argv.slice(2));
  const profile = loadProfile(args.profile);
  const identity = validateProfile(profile);
  const paths = validatePaths(args.repo, args.out);
  console.log(`Test project: ${identity.testProject}`);
  console.log(`Test database host: ${new URL(identity.testDatabase).hostname}`);
  console.log(`Production project and host differ: true`);
  console.log(`Guardian source: ${paths.app}`);
  console.log(`New external output: ${paths.out}`);
  console.log('Console mapping confirmation is a user assertion; this offline tool cannot prove ownership, empty data, authorization or Firebase access.');
  console.log('Firebase web API key format identifies a public configuration field; it does not verify credentials or authorization.');
  console.log('Isolated email/password sign-in is required. A build artifact is not an authentication, Rules or connection PASS.');
  if (!args.build) {
    console.log('DRY RUN PASS: no Vite import, build, child process, network request or file write performed.');
    return;
  }
  await new Promise((resolve, rejectPromise) => {
    const child = fork(SELF, ['--internal-build-worker'], {
      execArgv: [], cwd: paths.app, env: childEnvironment(profile.guardian),
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'], windowsHide: true,
    });
    // Drain without echoing plugin output or expanded Firebase configuration.
    child.stdout.resume();
    child.stderr.resume();
    let accepted = false;
    child.on('message', result => { accepted = result?.ok === true; });
    child.on('error', () => rejectPromise(new Error('Unable to start isolated build worker')));
    child.on('exit', code => {
      if (code === 0 && accepted) resolve();
      else rejectPromise(new Error('Isolated build failed; any new output is retained. No Firebase connection was tested.'));
    });
    child.send({ profile, repo: paths.repo, out: paths.out });
  });
  console.log('BUILD ARTIFACT CREATED: no deployment, browser launch, sign-in or Firebase verification performed.');
}

try {
  if (process.argv.length === 3 && process.argv[2] === '--internal-build-worker') await buildWorker();
  else await main();
} catch (error) {
  // Filesystem errors may include paths but never echo a profile value.
  const message = error?.code ? 'Required local path or file is unavailable' : error.message;
  console.error(`REFUSED: ${message}`);
  process.exitCode = 1;
}
