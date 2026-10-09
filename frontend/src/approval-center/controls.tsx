import { Alert, Button, Card, Input } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import type { AnswerInput, ApprovalInput, ControlReceipt, InteractionView, TakeoverInput, TaskView } from '../api/generated/types.gen';
import { EvidenceLink, Snapshot, Status } from '../task-center/shared';
import * as api from './api';

type Command = { kind: 'approval'; body: ApprovalInput } | { kind: 'judgment' | 'information'; body: AnswerInput }
  | { kind: 'takeover'; body: TakeoverInput };
const titles = { approved: '批准动作', rejected: '拒绝动作', judgment: '提交判断', information: '提交信息', takeover: '接管' };
const policyLabels = { allow: '已放行', need_approval: '需要审批', deny: '禁止执行' };
const environmentLabels = { local: '本地', test: '测试', staging: '预发布', production: '生产' };

export function ApprovalPlan({ value }: { value: InteractionView }) {
  const ticket = value.approval;
  if (!ticket) return null;
  return <Card title="待审批动作计划" className="approval-plan">
    <p className="control-summary">{ticket.plan.summary.statement}</p>
    <p>目标环境：{environmentLabels[ticket.plan.environment]}</p>
    <div className="claim">{ticket.plan.summary.evidence_ids.map((id) => <EvidenceLink key={id} id={id} />)}</div>
    <div className="plan-references">结论：<EvidenceLink id={ticket.plan.conclusion_evidence_id} /> · 复核：<EvidenceLink id={ticket.plan.review_evidence_id} /></div>
    {ticket.plan.actions.map(({ action, policy }, index) => <section className="planned-action" key={action.id} aria-label={`动作 ${index + 1}`}>
      <h3>{index + 1}. {action.name} · {action.service_name}</h3>
      <p><strong>{policy.risk_level}</strong> · {policyLabels[policy.decision]} · {policy.reason}</p>
      <p>{action.rationale.statement}</p>
      <div className="claim">{action.rationale.evidence_ids.map((id) => <EvidenceLink key={id} id={id} />)}</div>
      <h4>动作参数</h4><Snapshot value={action.parameters} />
      <h4>执行前提</h4><ul>{action.preconditions.map((item, i) => <li key={i}>{item}</li>)}</ul>
      <h4>回滚方案</h4><p>{action.rollback.description}</p><p>触发条件：{action.rollback.trigger}</p><Snapshot value={action.rollback.parameters} />
      <h4>独立验证</h4><ul>{action.verification.checks.map((item, i) => <li key={i}>{item}</li>)}</ul>
      <p>成功标准：{action.verification.success_criteria}</p><p>失败处置：{action.verification.failure_response}</p>
    </section>)}
    <details className="approval-identity"><summary>查看审批绑定信息</summary>
      <dl className="record-meta"><div><dt>审批 ID</dt><dd>{ticket.approval_id}</dd></div>
        <div><dt>等待版本</dt><dd>{ticket.wait_version}</dd></div>
        <div><dt>动作哈希</dt><dd>{ticket.action_hash}</dd></div>
        <div><dt>计划证据</dt><dd><EvidenceLink id={ticket.plan_evidence_id} /></dd></div>
        {value.approval_request_evidence_id && <div><dt>审批请求证据</dt><dd><EvidenceLink id={value.approval_request_evidence_id} /></dd></div>}
      </dl>
    </details>
  </Card>;
}

export function TaskControls({ task, interaction, ready, refresh }: {
  task: TaskView; interaction: InteractionView | undefined; ready: boolean; refresh: () => void;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<{ id: string; value: string } | null>(null);
  const [reason, setReason] = useState('');
  const [command, setCommand] = useState<Command | null>(null);
  const [receipt, setReceipt] = useState<ControlReceipt | null>(null);
  const [completedVersion, setCompletedVersion] = useState<number | null>(null);
  const submitting = useRef(false);
  const mutation = useMutation({ retry: false, mutationFn: (request: Command) => request.kind === 'approval'
    ? api.approve(task.id, request.body) : request.kind === 'takeover' ? api.takeover(task.id, request.body)
      : api.answer(task.id, request.kind, request.body),
    onSuccess: (result, request) => {
      setCompletedVersion(request.kind === 'takeover' ? request.body.expected_version : request.body.wait_version);
      setReceipt(result); setCommand(null); setDraft(null); setReason('');
      for (const key of ['tasks', 'task', 'interaction', 'task-history', 'task-evidence', 'task-tool-calls']) {
        void client.invalidateQueries({ queryKey: [key] });
      }
    },
  });
  const error = mutation.error;
  const conflict = error instanceof api.ControlError && [404, 409].includes(error.status);
  const uncertain = error instanceof api.ControlError && (error.status === 0 || error.status >= 500);
  const locked = Boolean(command) || mutation.isPending || completedVersion === task.status_version;
  const current = ready && interaction?.task_id === task.id && interaction.status === task.status
    && interaction.status_version === task.status_version;
  const ticket = current ? interaction.approval : null;
  const approvalReady = task.status === 'WAITING_APPROVAL' && ticket?.task_id === task.id
    && ticket.plan.task_id === task.id && ticket.wait_version === task.status_version;
  const prompt = current ? interaction.question ?? interaction.recovery : null;
  const questionId = current ? interaction.question?.question_id ?? interaction.recovery_question_id : null;
  const text = draft && draft.id === questionId ? draft.value : '';
  const kind = task.status === 'NEED_HUMAN_JUDGMENT' ? 'judgment' : task.status === 'WAITING_INFORMATION' ? 'information' : null;
  const questionReady = kind && prompt?.task.task_id === task.id && prompt.task.status === task.status
    && prompt.task.version === task.status_version && questionId;
  function prepare(next: Command) { mutation.reset(); setReceipt(null); setCommand(next); }
  const title = command ? command.kind === 'approval' ? titles[command.body.decision] : titles[command.kind] : '';
  return <div className="detail-stack control-panel">
    {receipt && <Alert role="status" type="success" showIcon title={receipt.outcome === 'taken_over' ? '接管操作已接纳' : '操作已记录并发送恢复信号'}
      description={<><p>可点击“刷新状态”查看 Workflow 后续进展。批准后仍须经过执行和独立验证。</p><p>操作证据：<EvidenceLink id={receipt.evidence_id} /></p></>} />}
    {task.status === 'WAITING_APPROVAL' && <Card title="风险授权">
      {approvalReady ? <><p>批准当前完整动作计划，或拒绝并转人工处理。</p><div className="control-buttons">
        <Button type="primary" disabled={locked} onClick={() => prepare({ kind: 'approval', body: { approval_id: ticket.approval_id,
          wait_version: ticket.wait_version, action_hash: ticket.action_hash, decision: 'approved' } })}>批准动作</Button>
        <Button danger disabled={locked} onClick={() => prepare({ kind: 'approval', body: { approval_id: ticket.approval_id,
          wait_version: ticket.wait_version, action_hash: ticket.action_hash, decision: 'rejected' } })}>拒绝动作</Button>
      </div></> : <p>当前审批单尚未就绪或已变化，请刷新后核对。</p>}
    </Card>}
    {kind && <Card title={kind === 'judgment' ? '回答人工判断' : '补充任务信息'}>
      {questionReady ? <><p className="human-question">{prompt.question}</p>
        {interaction?.question && <p>问题证据：<EvidenceLink id={interaction.question.question_evidence_id} /></p>}
        <p>回答后恢复至：<Status value={prompt.resume_status} /></p>
        <form onSubmit={(event) => { event.preventDefault(); if (text.trim() && text.trim().length <= 8000) prepare({ kind,
          body: { question_id: questionId, wait_version: prompt.task.version, answer: text.trim() } }); }}>
          <label htmlFor="human-answer">{kind === 'judgment' ? '你的判断' : '补充信息'}</label>
          <Input.TextArea id="human-answer" value={text} onChange={(event) => setDraft({ id: questionId, value: event.target.value })} rows={5}
            maxLength={8000} disabled={locked} required />
          <Button type="primary" htmlType="submit" disabled={locked || !text.trim()}>{titles[kind]}</Button>
        </form>
      </> : <p>当前问题尚未就绪或已变化，请刷新后核对。</p>}
    </Card>}
    <Card title="人工接管">
      {['CLOSED', 'RESOLVED'].includes(task.status) ? <p>当前任务已完成，无法接管。</p> : <>
        <p>接管会持久化停止自动化并请求取消 Workflow。请写明原因。</p>
        <form onSubmit={(event) => { event.preventDefault(); if (reason.trim() && reason.trim().length <= 3000) prepare({ kind: 'takeover',
          body: { expected_version: task.status_version, reason: reason.trim() } }); }}>
          <label htmlFor="takeover-reason">接管原因</label>
          <Input.TextArea id="takeover-reason" value={reason} onChange={(event) => setReason(event.target.value)} rows={3} maxLength={3000} disabled={locked} required />
          <Button danger htmlType="submit" disabled={locked || !reason.trim()}>接管任务</Button>
        </form>
      </>}
    </Card>
    {command && <Card title="核对本次操作" className="control-confirm" role="region" aria-label="核对本次操作">
      <h3>{title}</h3><p>任务：{task.title}</p>
      {command.kind === 'approval' ? <dl className="record-meta"><div><dt>审批 ID</dt><dd>{command.body.approval_id}</dd></div>
        <div><dt>等待版本</dt><dd>{command.body.wait_version}</dd></div><div><dt>动作哈希</dt><dd>{command.body.action_hash}</dd></div></dl>
        : <p className="human-question">{command.kind === 'takeover' ? command.body.reason : command.body.answer}</p>}
      {error && <Alert role="alert" type="error" showIcon title="操作未确认" description={error.message} />}
      <div className="control-buttons">
        <Button type="primary" danger={command.kind === 'takeover' || (command.kind === 'approval' && command.body.decision === 'rejected')}
          disabled={conflict || mutation.isPending} loading={mutation.isPending} onClick={() => {
            if (submitting.current) return;
            submitting.current = true;
            mutation.mutate(command, { onSettled: () => { submitting.current = false; } });
          }}>{uncertain ? '以相同内容重试' : `确认${title}`}</Button>
        {conflict ? <Button onClick={() => { setCommand(null); mutation.reset(); refresh(); }}>刷新并重新核对</Button>
          : <Button aria-label="取消" disabled={mutation.isPending || uncertain} onClick={() => { setCommand(null); mutation.reset(); }}>取消</Button>}
      </div>
    </Card>}
  </div>;
}
