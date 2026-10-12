// 使用前端既有 ESLint 的 js-yaml；不新增运行依赖，不执行 YAML 自定义类型。
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const projectRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const eslintRequire = createRequire(projectRequire.resolve('eslint/package.json'));
const yaml = eslintRequire('js-yaml');
const documents = yaml.loadAll(readFileSync(0, 'utf8'), null, { schema: yaml.JSON_SCHEMA });
process.stdout.write(JSON.stringify(documents.filter(Boolean)));
