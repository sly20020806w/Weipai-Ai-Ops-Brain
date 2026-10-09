import { Alert, Button, Drawer, Empty, Pagination, Spin, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import type { PropsWithChildren } from 'react';
import type { TaskStatus } from '../api/generated/types.gen';
import * as api from './api';
import { statusLabels } from './labels';

const snapshotLabels: Record<string, string> = { root_cause: '根因', findings: '调查发现', confidence: '置信度',
  uncertainties: '待核实事项', statement: '结论', evidence_ids: '证据引用' };

export function Status({ value }: { value: TaskStatus }) {
  const color = ['RESOLVED', 'CLOSED'].includes(value) ? 'green'
    : ['FAILED', 'AUTOMATION_ABORTED'].includes(value) ? 'red'
    : ['WAITING_APPROVAL', 'NEED_HUMAN_JUDGMENT', 'WAITING_INFORMATION', 'ESCALATED'].includes(value) ? 'gold' : 'blue';
  return <Tag color={color}>{statusLabels[value]}</Tag>;
}

export function Time({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={value} title={value}>{Number.isNaN(date.getTime()) ? value
    : date.toISOString().replace('T', ' ').replace('Z', ' UTC')}</time>;
}

export function PageHeader({ title, description, children }: PropsWithChildren<{ title: string; description: string }>) {
  return <div className="page-head"><div><div className="eyebrow">任务与证据</div><h1>{title}</h1><p>{description}</p></div>{children}</div>;
}

export function QueryState({ pending, error, retry }: { pending: boolean; error: Error | null; retry: () => void }) {
  if (pending) return <div className="query-state" role="status"><Spin /><p>正在读取…</p></div>;
  if (error) return <div className="query-state"><Alert role="alert" type="error" showIcon title="读取失败"
    description={error.message} /><Button onClick={retry}>重试查询</Button></div>;
  return null;
}

export function EmptyRecords({ label = '暂无记录' }: { label?: string }) { return <Empty description={label} />; }

export function PageControl({ total, page, onChange }: { total: number; page: number; onChange: (page: number) => void }) {
  return <div className="page-control"><Pagination current={page} total={total} pageSize={20} showSizeChanger={false}
    showTotal={(count) => `共 ${count} 条`} onChange={onChange} /></div>;
}

export function EvidenceLink({ id }: { id: string }) {
  const [params] = useSearchParams();
  const next = new URLSearchParams(params); next.set('evidence', id);
  return <Link className="evidence-link" to={{ search: `?${next}` }} aria-label={`查看证据 ${id}`}>{id}</Link>;
}

// Evidence 快照是后端生成的 JsonValue（unknown）；保留原值，按引用字段识别可打开的 Evidence ID。
export function Snapshot({ value, field = '' }: { value: unknown; field?: string }) {
  if (typeof value === 'string') {
    const reference = /(^evidence_ids?$|_evidence_ids?$|^observed_ids$)/.test(field);
    return reference && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
      ? <EvidenceLink id={value} /> : <span className="snapshot-text">{value}</span>;
  }
  if (Array.isArray(value)) return value.length ? <ul className="snapshot-list">{value.map((item, index) =>
    <li key={index}><Snapshot value={item} field={field} /></li>)}</ul> : <span>[]</span>;
  if (value && typeof value === 'object') return <dl className="snapshot-object">{Object.entries(value).map(([key, item]) =>
    <div key={key}><dt>{snapshotLabels[key] ?? key}</dt><dd><Snapshot value={item} field={key} /></dd></div>)}</dl>;
  return <span>{value === null ? 'null' : String(value)}</span>;
}

export function EvidenceDrawer() {
  const [params, setParams] = useSearchParams();
  const id = params.get('evidence');
  const query = useQuery({ queryKey: ['evidence', id], enabled: Boolean(id),
    queryFn: ({ signal }) => api.evidence(id!, signal) });
  const close = () => setParams((previous) => { const next = new URLSearchParams(previous); next.delete('evidence'); return next; });
  return <Drawer title="证据详情" open={Boolean(id)} onClose={close} size="min(640px, 100vw)" destroyOnHidden>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <div className="evidence-detail">
      <dl className="record-meta"><div><dt>Evidence ID</dt><dd>{query.data.id}</dd></div>
        <div><dt>所属任务</dt><dd><Link to={`/tasks/${query.data.task_id}`}>{query.data.task_id}</Link></dd></div>
        <div><dt>来源 Tool</dt><dd>{query.data.source_tool}</dd></div>
        <div><dt>采集时间</dt><dd><Time value={query.data.collected_at} /></dd></div>
        <div><dt>入库时间</dt><dd><Time value={query.data.created_at} /></dd></div>
        <div><dt>源系统引用</dt><dd>{query.data.source_reference ?? '无源系统引用'}</dd></div></dl>
      <h3>调用参数</h3><Snapshot value={query.data.parameters} />
      <h3>结果快照</h3><Snapshot value={query.data.result_snapshot} />
    </div>}
  </Drawer>;
}
