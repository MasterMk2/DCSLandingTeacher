import { readFileSync } from 'node:fs';

const expectedNode = readFileSync(new URL('../.nvmrc', import.meta.url), 'utf8').trim();
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
const npm = process.env.npm_config_user_agent?.match(/^npm\/(\S+)/)?.[1] ?? 'unavailable';

// Only tool versions and OS/CPU are public diagnostics; never dump the environment.
console.log(JSON.stringify({
  node: process.versions.node,
  npm,
  platform: process.platform,
  arch: process.arch,
  expectedNode,
  expectedNpm: pkg.engines.npm,
  packageManager: pkg.packageManager,
}, null, 2));
if (process.versions.node !== expectedNode || npm !== pkg.engines.npm) {
  console.error('Use the pinned Node/npm versions described in docs/development-foundation.md.');
  process.exitCode = 1;
}
