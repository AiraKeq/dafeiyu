import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';

export const PRIVATE_ROOT = path.join(os.homedir(), '.ssh');
export const PRIVATE_RUNTIME = path.join(PRIVATE_ROOT, 'whale-manager-runtime');
const message = '管理器运行目录的访问权限不够私密，已拒绝启动或连接；请保留原有SSH资料，通过安全维护核验用户目录。';

// Inspect permissions only. Never edit DACLs, move/copy SSH keys, or weaken a
// workspace sandbox grant. New files inherit the already private SSH folder.
async function checkWindows(targets) {
  const script = String.raw`
$ErrorActionPreference = 'Stop'
try {
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $allowed = @($me, 'S-1-5-18', 'S-1-5-32-544')
  $targets = ConvertFrom-Json $env:WHALE_PRIVATE_TARGETS
  foreach ($target in $targets) {
    $item = Get-Item -LiteralPath $target -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'unsafe path' }
    $acl = Get-Acl -LiteralPath $target
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $allowed) { throw 'unsafe owner' }
    $mine = $false
    foreach ($rule in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
      if ($rule.AccessControlType -eq 'Allow') {
        if ($rule.IdentityReference.Value -notin $allowed) { throw 'shared access' }
        if ($rule.IdentityReference.Value -eq $me -and
            (($rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl)) {
          $mine = $true
          if ($item.PSIsContainer -and (($rule.InheritanceFlags -band 3) -ne 3)) { throw 'unsafe inheritance' }
        }
      }
    }
    if (-not $mine) { throw 'missing private user access' }
  }
  exit 0
} catch {
  [Console]::Error.WriteLine('Private manager storage permission check failed; no permissions changed.')
  exit 1
}
`;
  const code = await new Promise((resolve, reject) => {
    const child = spawn('C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
      stdio: ['ignore', 'ignore', 'inherit'], windowsHide: true,
      env: {...process.env, PSModulePath: 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules', WHALE_PRIVATE_TARGETS: JSON.stringify(targets)},
    });
    child.once('error', reject); child.once('close', resolve);
  });
  if (code !== 0) throw new Error(message);
}

export async function ensurePrivateRuntime(runtimeDir = PRIVATE_RUNTIME) {
  // Production storage cannot be redirected into a shared workspace.
  if (path.resolve(runtimeDir) !== path.resolve(PRIVATE_RUNTIME)) throw new Error(message);
  const base = fs.lstatSync(PRIVATE_ROOT);
  if (!base.isDirectory() || base.isSymbolicLink()) throw new Error(message);
  if (process.platform === 'win32') await checkWindows([PRIVATE_ROOT]);
  else if (base.uid !== process.getuid() || (base.mode & 0o077)) throw new Error(message);
  if (!fs.existsSync(runtimeDir)) fs.mkdirSync(runtimeDir, {recursive: false, mode: 0o700});
  const stat = fs.lstatSync(runtimeDir);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(message);
  const knownFiles = ['access.json', 'server.log'].map(name => path.join(runtimeDir, name)).filter(file => fs.existsSync(file));
  if (process.platform === 'win32') await checkWindows([runtimeDir, ...knownFiles]);
  else {
    for (const file of [runtimeDir, ...knownFiles]) {
      const item = fs.lstatSync(file);
      if (item.isSymbolicLink() || item.uid !== process.getuid() || (item.mode & 0o077)) throw new Error(message);
    }
  }
  return runtimeDir;
}
