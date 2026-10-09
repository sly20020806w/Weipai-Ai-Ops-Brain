import { Alert, Button, Card, Input, Tag } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import type { ChatAnswer, ChatInput, EventReceipt, TaskStatus } from '../api/generated/types.gen';
import { EvidenceDrawer, EvidenceLink, PageHeader, QueryState } from '../task-center/shared';
import { statusLabels } from '../task-center/labels';
import { ChatError, isUuid, readAnswer, streamAnswer } from './api';

type Turn = { input: ChatInput; receipt?: EventReceipt; answer?: ChatAnswer; text: string; ids: string[]; error?: Error };

function AnswerText({ text, ids }: { text: string; ids: string[] }) {
  return <div className="chat-answer">{text.split(/(\[Evidence:[0-9a-f-]{36}\])/gi).map((part, index) => {
    const id = part.match(/^\[Evidence:([0-9a-f-]{36})\]$/i)?.[1];
    return id && ids.includes(id) ? <EvidenceLink key={index} id={id} /> : <span key={index}>{part}</span>;
  })}</div>;
}

function Answer({ answer, text, ids, taskId }: { answer?: ChatAnswer; text: string; ids: string[]; taskId?: string }) {
  const references = answer?.evidence_ids ?? ids;
  return <>
    {taskId && <div className="chat-task"><Tag>Human 任务</Tag><Link to={`/tasks/${taskId}`}>查看任务 {taskId}</Link></div>}
    {answer && <div className="chat-result"><Tag color={answer.pending ? 'blue' : ['CLOSED', 'RESOLVED'].includes(answer.status) ? 'green' : 'gold'}>
      {Object.hasOwn(statusLabels, answer.status) ? statusLabels[answer.status as TaskStatus] : answer.status}</Tag>
      {answer.pending && <span>任务仍在处理，可手动读取已保存回答。</span>}
      {answer.policy_decision && <span>策略判定：{({ need_approval: '需要审批', allow: '允许', deny: '拒绝' } as Record<string, string>)[answer.policy_decision] ?? answer.policy_decision}</span>}
    </div>}
    <AnswerText text={answer ? answer.answer ?? '' : text} ids={references} />
    {answer && !answer.answer && <p>当前没有可交付回答，请查看任务详情与待处理事项。</p>}
    {references.length > 0 && <details className="chat-references"><summary>本轮证据（{references.length}）</summary>
      {references.map((id) => <EvidenceLink key={id} id={id} />)}</details>}
    {answer && <div className="chat-references">{[
      [answer.conclusion_evidence_id, '结论'], [answer.review_evidence_id, '反证复核'], [answer.plan_evidence_id, '动作计划'],
    ].map(([id, label]) => id && <span key={id}>{label}：<EvidenceLink id={id} /></span>)}</div>}
    {taskId && answer && ['WAITING_APPROVAL', 'NEED_HUMAN_JUDGMENT', 'WAITING_INFORMATION', 'ESCALATED', 'AUTOMATION_ABORTED'].includes(answer.status)
      && <Link className="task-control-link" to={`/approvals/${taskId}`}>查看人工处理</Link>}
  </>;
}

export function ChatPage() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const selected = params.get('task');
  const [service, setService] = useState(() => params.get('service') ?? 'payment-service');
  const [start, setStart] = useState(() => params.get('start') ?? '');
  const [end, setEnd] = useState(() => params.get('end') ?? '');
  const [mode, setMode] = useState<NonNullable<ChatInput['mode']>>('question');
  const [message, setMessage] = useState('');
  const [follow, setFollow] = useState(true);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [validation, setValidation] = useState('');
  const controller = useRef<AbortController | null>(null);
  const loaded = useQuery({ queryKey: ['chat-answer', selected],
    enabled: Boolean(selected && isUuid(selected) && !turns.some((turn) => turn.receipt?.task_id === selected)),
    queryFn: ({ signal }) => readAnswer(selected!, signal), refetchOnWindowFocus: false });
  useEffect(() => () => controller.current?.abort(), []);
  const last = turns.at(-1);
  const previous = last?.answer && !last.answer.pending && last.answer.answer ? last.answer.task_id
    : !last && loaded.data && !loaded.data.pending && loaded.data.answer ? loaded.data.task_id : undefined;
  const mayFollow = previous && (!last || last.input.service_name === service.trim());

  function update(id: string, values: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) {
    setTurns((old) => old.map((turn) => turn.input.request_id === id ? { ...turn, ...(typeof values === 'function' ? values(turn) : values) } : turn));
  }
  async function send(input: ChatInput, retry = false) {
    if (controller.current) return;
    const current = new AbortController(); controller.current = current; setBusy(true); setValidation('');
    if (retry) update(input.request_id, { text: '', ids: [], error: undefined, answer: undefined });
    else setTurns((old) => [...old, { input, text: '', ids: [] }]);
    try {
      await streamAnswer(input, current.signal, {
        task: (receipt) => {
          update(input.request_id, { receipt });
          setParams((old) => { const next = new URLSearchParams(old); next.set('task', receipt.task_id);
            next.set('service', input.service_name); next.delete('evidence');
            for (const field of ['start', 'end'] as const) { const value = input[field];
              if (value) next.set(field, value); else next.delete(field); }
            return next; }, { replace: true });
          void queryClient.invalidateQueries({ queryKey: ['tasks'] });
        },
        evidence: (ids) => update(input.request_id, { ids }),
        delta: (text) => update(input.request_id, (turn) => ({ text: turn.text + text })),
        done: (answer) => {
          update(input.request_id, { answer }); queryClient.setQueryData(['chat-answer', answer.task_id], answer);
          void queryClient.invalidateQueries({ queryKey: ['tasks'] });
          void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
          for (const key of ['task', 'task-evidence', 'task-history', 'task-tool-calls', 'interaction'])
            void queryClient.invalidateQueries({ queryKey: [key, answer.task_id] });
        },
      });
      setMessage('');
    } catch (error) {
      update(input.request_id, { error: current.signal.aborted ? new Error('回答连接已停止，已接纳任务继续处理。可读取已保存回答或按原内容重试。')
        : error instanceof Error ? error : new ChatError() });
    } finally {
      if (controller.current === current) { controller.current = null; setBusy(false); }
    }
  }

  function submit() {
    if (controller.current) return;
    const serviceName = service.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(serviceName) || !message.trim() || message.trim().length > 4000) {
      setValidation('请填写有效服务标识和 1–4000 字的问题。'); return;
    }
    if (Boolean(start) !== Boolean(end) || (start && end && (![start, end].every((value) => /(Z|[+-]\d{2}:\d{2})$/i.test(value)
      && Number.isFinite(Date.parse(value))) || Date.parse(end) <= Date.parse(start) || Date.parse(end) - Date.parse(start) > 86400000))) {
      setValidation('UTC 时间窗须同时填写，带时区，结束晚于开始且最长 24 小时。'); return;
    }
    void send({ request_id: crypto.randomUUID(), service_name: serviceName, message: message.trim(), mode,
      previous_task_id: follow && mayFollow ? previous : null,
      start: start ? new Date(start).toISOString() : null, end: end ? new Date(end).toISOString() : null });
  }

  async function recover(turn: Turn) {
    if (!turn.receipt || controller.current) return;
    const current = new AbortController(); controller.current = current; setBusy(true);
    try { const answer = await readAnswer(turn.receipt.task_id, current.signal);
      update(turn.input.request_id, { answer, error: undefined }); }
    catch (error) { update(turn.input.request_id, { error: error instanceof Error ? error : new ChatError() }); }
    finally { if (controller.current === current) { controller.current = null; setBusy(false); } }
  }

  return <section>
    <PageHeader title="AI 对话" description="围绕服务调查问题、核对证据，并跟进统一任务的处理结果。">
      <Button disabled={busy} onClick={() => { setTurns([]); setMessage(''); setValidation('');
        setParams((old) => { const next = new URLSearchParams(old); next.delete('task'); next.delete('evidence'); return next; }); }}>新对话</Button>
    </PageHeader>
    <div className="chat-layout">
      <div className="chat-thread" aria-label="对话记录" aria-busy={busy}>
        {!turns.length && !selected && <Card><h2>从一个问题开始</h2><p>填写服务和问题，回答会逐段显示。引用的证据可直接打开，任务进展可在 AI 任务中心查看。</p></Card>}
        {!turns.length && selected && <>
          {!isUuid(selected) ? <Alert type="error" role="alert" title="对话链接无效" />
            : <><QueryState pending={loaded.isPending} error={loaded.error} retry={() => void loaded.refetch()} />
              {loaded.data && !loaded.isError && <Card title="已保存回答">
                <Answer answer={loaded.data} text="" ids={[]} taskId={selected} />
                <Button onClick={() => void loaded.refetch()} loading={loaded.isFetching}>读取已保存回答</Button>
              </Card>}</>}
        </>}
        {turns.map((turn, index) => <Card key={turn.input.request_id} title={`第 ${index + 1} 轮 · ${turn.input.service_name}`}>
          <div className="chat-question"><span>本人</span><p>{turn.input.message}</p></div>
          <div className="chat-assistant"><span>运维大脑</span>
            <Answer answer={turn.answer} text={turn.text} ids={turn.ids} taskId={turn.receipt?.task_id} />
            {busy && index === turns.length - 1 && <p role="status">{turn.text ? '正在接收回答…' : '正在调查并核验证据…'}</p>}
            {turn.error && <Alert type="error" role="alert" title="回答暂未完成" description={turn.error.message} />}
            {!busy && (turn.error || turn.answer?.pending) && <div className="control-buttons">
              {turn.receipt && <Button onClick={() => void recover(turn)}>读取已保存回答</Button>}
              {turn.error && !(turn.error instanceof ChatError && [404, 409, 422].includes(turn.error.status))
                && <Button onClick={() => void send(turn.input, true)}>按原内容重试</Button>}
            </div>}
          </div>
        </Card>)}
      </div>
      <Card className="chat-composer" title="发起问题">
        <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
          <label className="catalog-field" htmlFor="chat-service">服务标识<Input id="chat-service" value={service} onChange={(event) => setService(event.target.value)} disabled={busy} maxLength={128} /></label>
          <div className="catalog-field"><label htmlFor="chat-mode">请求方式</label><select id="chat-mode" value={mode} disabled={busy}
            onChange={(event) => setMode(event.target.value as NonNullable<ChatInput['mode']>)}>
            <option value="question">只读问答</option><option value="task">发起处置任务</option></select></div>
          <p className="chat-mode-note">{mode === 'question' ? '只读调查与回答。' : '进入统一处置流程；需要审批的动作请到审批中心明确授权。'}</p>
          <div className="catalog-field"><label htmlFor="chat-message">问题</label><Input.TextArea id="chat-message" rows={5} maxLength={4000} showCount disabled={busy} value={message} onChange={(event) => setMessage(event.target.value)} /></div>
          <details className="chat-window"><summary>查询时间窗（可选）</summary>
            <label className="catalog-field" htmlFor="chat-start">开始时间（UTC）<Input id="chat-start" placeholder="2026-10-01T01:00:00Z" value={start} disabled={busy} onChange={(event) => setStart(event.target.value)} /></label>
            <label className="catalog-field" htmlFor="chat-end">结束时间（UTC）<Input id="chat-end" placeholder="2026-10-01T02:00:00Z" value={end} disabled={busy} onChange={(event) => setEnd(event.target.value)} /></label>
            <p>留空查询接纳时刻前一小时；最长 24 小时。</p>
          </details>
          {mayFollow && <label className="chat-follow"><input type="checkbox" checked={follow} disabled={busy} onChange={(event) => setFollow(event.target.checked)} />基于上一轮追问</label>}
          {validation && <Alert type="error" role="alert" title={validation} />}
          <div className="control-buttons"><Button type="primary" htmlType="submit" disabled={busy}>发送问题</Button>
            {busy && <Button onClick={() => controller.current?.abort()}>停止接收</Button>}</div>
        </form>
      </Card>
    </div>
    <EvidenceDrawer />
  </section>;
}
