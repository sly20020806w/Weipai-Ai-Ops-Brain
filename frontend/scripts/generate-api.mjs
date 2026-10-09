import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createClient } from '@hey-api/openapi-ts';

const frontend = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const root = path.dirname(frontend);
await fs.mkdir(path.join(root, '.cache'), { recursive: true });
const scratch = await fs.mkdtemp(path.join(root, '.cache', 'api-generation-'));
const contract = path.join(scratch, 'openapi.json');
const output = path.join(scratch, 'generated');
const destination = path.join(frontend, 'src', 'api', 'generated');
const checking = process.argv.includes('--check');

async function files(directory, prefix = '') {
  const found = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const relative = path.join(prefix, entry.name);
    if (entry.isDirectory()) found.push(...await files(path.join(directory, entry.name), relative));
    else if (entry.isFile()) found.push(relative);
    else throw new Error('生成目录中不允许符号链接');
  }
  return found.sort();
}

async function digest(filename) {
  return createHash('sha256').update(await fs.readFile(filename)).digest('hex');
}

try {
  let uv = process.env.WEIPAI_UV ?? 'uv';
  if (!process.env.WEIPAI_UV) {
    const local = path.join(root, '.tools', 'uv', 'bin', 'uv.exe');
    try { await fs.access(local); uv = local; } catch { /* 使用 PATH 中的 uv。 */ }
  }
  execFileSync(uv, ['run', '--frozen', '--directory', path.join(root, 'backend'),
    'python', path.join(root, 'scripts', 'export_openapi.py'), contract], {
    cwd: root, stdio: 'pipe', windowsHide: true,
  });
  await createClient({
    input: contract,
    output: { path: output },
    plugins: ['@hey-api/typescript', '@hey-api/client-fetch', '@hey-api/sdk'],
  });
  const generated = await files(output);
  if (checking) {
    const existing = await files(destination);
    if (JSON.stringify(generated) !== JSON.stringify(existing)) throw new Error('生成文件集合已变化');
    for (const filename of generated) {
      if (await digest(path.join(output, filename)) !== await digest(path.join(destination, filename))) {
        throw new Error(`生成客户端已过期：${filename}`);
      }
    }
    if (await digest(contract) !== await digest(path.join(frontend, 'openapi.json'))) {
      throw new Error('OpenAPI 契约已过期');
    }
    console.log(`OpenAPI 契约与 ${generated.length} 个客户端文件逐字节一致。`);
  } else {
    await fs.mkdir(destination, { recursive: true });
    for (const filename of await files(destination)) {
      if (!generated.includes(filename)) await fs.unlink(path.join(destination, filename));
    }
    for (const filename of generated) {
      await fs.mkdir(path.dirname(path.join(destination, filename)), { recursive: true });
      await fs.copyFile(path.join(output, filename), path.join(destination, filename));
    }
    await fs.copyFile(contract, path.join(frontend, 'openapi.json'));
    console.log('已从后端 OpenAPI 生成类型、SDK 与 Fetch 客户端。');
  }
} catch (error) {
  console.error(checking ? '客户端一致性检查失败，请运行 pnpm api:generate。' : '客户端生成失败。');
  // 子进程输出可能包含本机路径或配置，避免直接转储。
  if (error instanceof Error && !('stderr' in error)) console.error(error.message);
  process.exitCode = 1;
} finally {
  // mkdtemp 的绝对路径必须仍位于本仓库 .cache 内。
  if (path.dirname(scratch) === path.join(root, '.cache')) {
    await fs.rm(scratch, { recursive: true, force: true });
  } else {
    console.error('临时目录越界，未执行清理。');
    process.exitCode = 1;
  }
}
