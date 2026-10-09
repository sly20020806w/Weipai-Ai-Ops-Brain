# 微派 AI 运维个人控制台

Step 47 建立 React、TypeScript strict、Vite、Ant Design、TanStack Query 前端工程。17 个入口的顺序对应权威设计第 34 节，登录、刷新会话、退出与移动导航可使用；各入口的业务内容按 Step 48–53 实现。

安装使用 Node.js 22.18+（推荐 24 LTS）与 pnpm 11。依赖版本与 pnpm 版本固定，安装不执行 MSW 的浏览器安装脚本。配置从进程环境读取，不加载 `.env`。

```powershell
pnpm --dir frontend install --frozen-lockfile
.\check-frontend.ps1
```

单独运行：

```powershell
pnpm --dir frontend lint
pnpm --dir frontend typecheck
pnpm --dir frontend test
pnpm --dir frontend build
pnpm --dir frontend api:generate
pnpm --dir frontend api:check
```

API 契约、类型、SDK 与 Fetch 客户端都从后端生成，禁止手工修改 `openapi.json` 和 `src/api/generated/`。生成器离线调用 `scripts/export_openapi.py`，清除进程部署配置后创建无数据库连接的测试 API，只导出 OpenAPI。`api:check` 在临时目录重新生成并逐字节比较所有 16 个文件及契约，退出时清理临时目录；与 Git 是否已有提交无关。

会话 Cookie 为 HttpOnly，由浏览器管理；CSRF 从登录或 `/api/auth/me` 取得，只存内存。所有生成的调用经同源 Fetch 客户端，写请求携带 CSRF，业务请求 401 清除身份和业务缓存。登录失败与 503 区分显示，不自动重复发送密码；没有访客账户或绕过后端鉴权的模式。

开发与 preview 同源代理 `/api` 至 `http://127.0.0.1:8000`；可用进程环境 `WEIPAI_API_TARGET` 更改为其他本机 HTTP Origin。Vite 仅绑定 `127.0.0.1:5173`，端口占用会直接报错；保留浏览器 Origin，由后端校验。登录配置的 `public_origin` 必须匹配浏览器的 `http://127.0.0.1:5173`。生产镜像与托管按 Step 56/57 实现。

Step 48 已实现任务、事故、事件列表与详情，支持状态/来源筛选、UTC 时间线、Tool 调用、结论和 Evidence 原快照抽屉。

完整手动与真实浏览器验收见 [任务页面验收](../docs/task-pages.md) 和 [前端外壳验收](../docs/frontend-shell.md)。需要自动准备 Fake 数据并亲自浏览时，在根目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-task-pages.ps1 -Interactive`。

Step 49 已实现审批中心的风险授权、人工判断、补充信息和人工接管。三类等待状态分列表查询，批准绑定动作哈希，回答绑定问题身份，接管绑定状态版本，均复用已有 API 与会话操作人。真实 API/Temporal/Edge 验收和亲自处理样例使用根目录 `demo-approval-pages.ps1 -Interactive`，详见 [审批页面验收](../docs/approval-pages.md)。
