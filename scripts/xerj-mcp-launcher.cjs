'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
const xerjHome = path.join(localAppData, 'Programs', 'xerj');
const keyFile = path.join(xerjHome, 'data', 'admin.key');
const xerjExe = path.join(xerjHome, 'xerj.exe');

let key;
try { key = fs.readFileSync(keyFile, 'utf8').trim(); }
catch { process.stderr.write('XERJ MCP launcher: unable to read configured key file.\\n'); process.exit(1); }
if (!key) { process.stderr.write('XERJ MCP launcher: key file is empty.\\n'); process.exit(1); }

// Whitelist the child environment: only what xerj needs, never the host agent's full env.
const childEnv = {
  XERJ_URL: process.env.XERJ_URL || 'http://127.0.0.1:9200',
  XERJ_API_KEY: key,
  XERJ_AUTH: 'ApiKey ' + key
};
// Windows requires these to resolve DLLs and temp files; they carry no secrets.
for (const name of ['SystemRoot', 'windir', 'TEMP', 'TMP', 'PATH']) {
  if (process.env[name]) childEnv[name] = process.env[name];
}

const child = spawn(xerjExe, ['mcp'], {
  stdio: 'inherit',
  env: childEnv,
  windowsHide: true
});
child.on('error', () => { process.stderr.write('XERJ MCP launcher: failed to start xerj.exe.\\n'); process.exit(1); });
child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
