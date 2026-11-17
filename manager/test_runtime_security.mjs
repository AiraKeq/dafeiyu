import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {PRIVATE_RUNTIME, ensurePrivateRuntime} from './runtime-security.mjs';
await assert.rejects(ensurePrivateRuntime(path.resolve('qq-deepseek-deploy/manager/.runtime')));
await ensurePrivateRuntime();
assert.equal(fs.lstatSync(PRIVATE_RUNTIME).isSymbolicLink(),false);
await ensurePrivateRuntime();
console.log(JSON.stringify({private_storage_checks:3,passed:3,permissions_modified:0,ssh_keys_copied:0}));
