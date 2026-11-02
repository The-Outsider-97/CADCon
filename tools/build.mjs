// Delegate compilation and asset publication to SLAI's authoritative app builder.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const app = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const roots = [process.env.SLAI_ROOT, ...Array.from({ length: 4 }, (_, i) => resolve(app, ...Array(i + 1).fill('..')))];
const root = roots.filter(Boolean).find(x => existsSync(join(x, 'applications', 'tools', 'build', 'build-static.mjs')));
if (!root) {
  console.error('SLAI shared build tools missing. Set SLAI_ROOT to the SLAI v2.4 checkout.');
  process.exit(2);
}
execFileSync(process.execPath, [join(root, 'applications', 'tools', 'build', 'build-static.mjs'), app], { stdio: 'inherit' });
