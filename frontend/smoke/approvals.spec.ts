import { expect, test } from '@playwright/test';
import type { ControlReceipt, EvidenceView, InteractionView } from '../src/api/generated/types.gen';

test('真实 API/Temporal 审批哈希、判断隔离、补充信息、拒绝与接管', async ({ page, context }) => {
  test.setTimeout(120_000);
  const password = process.env.WEIPAI_FRONTEND_SMOKE_PASSWORD;
  const approvalId = process.env.WEIPAI_FRONTEND_APPROVAL_ID;
  const rejectId = process.env.WEIPAI_FRONTEND_REJECT_ID;
  const judgmentId = process.env.WEIPAI_FRONTEND_JUDGMENT_ID;
  const informationId = process.env.WEIPAI_FRONTEND_INFORMATION_ID;
  const takeoverId = process.env.WEIPAI_FRONTEND_TAKEOVER_ID;
  if (!password || !approvalId || !rejectId || !judgmentId || !informationId || !takeoverId) throw new Error('缺少本机人工处理样例');
  const remote: string[] = [];
  const errors: string[] = [];
  const operations: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', async (route) => {
    const request = route.request(); const url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') { remote.push(url.origin); await route.abort(); return; }
    if (request.method() === 'POST' && !url.pathname.startsWith('/api/auth/')) {
      operations.push(url.pathname);
      expect(request.headers()['x-csrf-token']).toBeTruthy();
      expect(request.postDataJSON()).not.toHaveProperty('actor');
    }
    await route.continue();
  });
  await page.goto(`/approvals/${approvalId}`);
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('账户', { exact: true }).fill('local-browser-owner');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/approvals/${approvalId}$`));
  await expect(page.getByRole('button', { name: '批准动作', exact: true })).toBeVisible();
  const interaction: InteractionView = await (await page.request.get(`/api/tasks/${approvalId}/interaction`)).json();
  const ticket = interaction.approval!;
  await page.getByText('查看审批绑定信息', { exact: true }).click();
  await expect(page.getByText(ticket.action_hash, { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/approval-detail.png', fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.cache/frontend-smoke/approval-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('link', { name: '返回审批中心', exact: true }).click();
  const list = page.getByRole('region', { name: '待审批列表' });
  await expect(list.locator(`a[href*="${approvalId}"]`)).toBeVisible();
  await expect(list.locator(`a[href*="${judgmentId}"]`)).toHaveCount(0);
  await page.getByRole('tab', { name: '待人工判断', exact: true }).click();
  const judgments = page.getByRole('region', { name: '待人工判断列表' });
  await expect(judgments.locator(`a[href*="${judgmentId}"]`)).toBeVisible();
  await expect(judgments.locator(`a[href*="${approvalId}"]`)).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('tab', { name: '待人工判断', exact: true })).toHaveAttribute('aria-selected', 'true');
  await page.screenshot({ path: '../.cache/frontend-smoke/approval-judgment-list.png', fullPage: true, animations: 'disabled' });
  await page.goto(`/approvals/${approvalId}`);
  await page.getByRole('button', { name: '批准动作', exact: true }).click();
  const approved = page.waitForResponse((response) => response.url().endsWith(`/api/tasks/${approvalId}/approval`));
  await page.getByRole('button', { name: '确认批准动作', exact: true }).click();
  const response = await approved;
  expect(response.status()).toBe(202);
  const body = response.request().postDataJSON();
  expect(body).toEqual({ approval_id: ticket.approval_id, wait_version: ticket.wait_version, action_hash: ticket.action_hash, decision: 'approved' });
  const receipt: ControlReceipt = await response.json();
  await expect(page.getByText('操作已记录并发送恢复信号', { exact: true })).toBeVisible();
  // 信号投递与目标 Workflow 状态提交是异步的；手动刷新验收真实状态，不在产品中加轮询。
  await expect.poll(async () => { await page.getByRole('button', { name: '刷新状态', exact: true }).click();
    return page.locator('.task-summary .ant-tag').innerText(); }, { timeout: 15_000 }).toBe('执行中');
  await expect(page.locator('.task-summary .ant-tag')).toHaveText('执行中');
  await page.getByRole('link', { name: `查看证据 ${receipt.evidence_id}`, exact: true }).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByText('local-browser-owner', { exact: true }).first()).toBeVisible();
  const evidence: EvidenceView = await (await page.request.get(`/api/evidence/${receipt.evidence_id}`)).json();
  expect(evidence.task_id).toBe(approvalId); expect(evidence.source_tool).toBe('approval.decision');
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  // 真实重投同一内容仍返回同一回执，参数变化被后端拒绝。
  const csrf = (await (await page.request.get('/api/auth/me')).json()).csrf_token;
  const repeated = await page.request.post(`/api/tasks/${approvalId}/approval`, { data: body, headers: { 'X-CSRF-Token': csrf } });
  expect(repeated.status()).toBe(202); expect(await repeated.json()).toEqual(receipt);
  const altered = await page.request.post(`/api/tasks/${approvalId}/approval`, { data: { ...body, action_hash: '0'.repeat(64) }, headers: { 'X-CSRF-Token': csrf } });
  expect(altered.status()).toBe(409);
  await page.goto(`/approvals/${rejectId}`);
  await page.getByRole('button', { name: '拒绝动作', exact: true }).click();
  const rejected = page.waitForResponse((result) => result.url().endsWith(`/api/tasks/${rejectId}/approval`));
  await page.getByRole('button', { name: '确认拒绝动作', exact: true }).click();
  expect((await rejected).status()).toBe(202);
  for (const [id, label, submit, endpoint] of [
    [judgmentId, '你的判断', '提交判断', 'judgment'],
    [informationId, '补充信息', '提交信息', 'information'],
  ] as const) {
    await page.goto(`/approvals/${id}`);
    await expect(page.getByRole('button', { name: '批准动作', exact: true })).toHaveCount(0);
    await page.getByLabel(label, { exact: true }).fill('已核对现实信息，优先保障支付业务');
    await page.getByRole('button', { name: submit, exact: true }).click();
    const delivered = page.waitForResponse((result) => result.url().endsWith(`/api/tasks/${id}/${endpoint}`));
    await page.getByRole('button', { name: `确认${submit}`, exact: true }).click();
    expect((await delivered).status()).toBe(202);
    await expect(page.getByText('操作已记录并发送恢复信号', { exact: true })).toBeVisible();
  }
  await page.goto(`/approvals/${takeoverId}`);
  await page.getByLabel('接管原因', { exact: true }).fill('由我接管，停止后续自动化');
  await page.getByRole('button', { name: '接管任务', exact: true }).click();
  const takeover = page.waitForResponse((result) => result.url().endsWith(`/api/tasks/${takeoverId}/takeover`));
  await page.getByRole('button', { name: '确认接管', exact: true }).click();
  expect((await takeover).status()).toBe(202);
  await expect(page.getByText('接管操作已接纳', { exact: true })).toBeVisible();
  await expect(page.locator('.task-summary .ant-tag')).toHaveText('已转人工');
  await page.screenshot({ path: '../.cache/frontend-smoke/approval-takeover.png', fullPage: true, animations: 'disabled' });
  expect(operations).toEqual([`/api/tasks/${approvalId}/approval`, `/api/tasks/${rejectId}/approval`,
    `/api/tasks/${judgmentId}/judgment`, `/api/tasks/${informationId}/information`, `/api/tasks/${takeoverId}/takeover`]);
  expect(remote).toEqual([]); expect(errors).toEqual([]);
});
