import { Alert, Button, Card, Table } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import type { AuditView } from '../api/generated/types.gen';
import { EmptyRecords, EvidenceDrawer, EvidenceLink, PageControl, PageHeader, QueryState, Snapshot, Time } from '../task-center/shared';
import { useListParams } from '../task-center/use-list-params';
import * as api from './api';
import { auditLabels } from './format';
import { TimeWindowFilter } from './window';
import { useTimeWindow } from './use-time-window';

function listSearch(params: URLSearchParams) { const next = new URLSearchParams(params); next.delete('evidence'); return next.size ? `?${next}` : ''; }

export function AuditListPage() {
  const { params, page, update } = useListParams();
  const [, setParams] = useSearchParams();
  const window = useTimeWindow();
  const actor = params.get('actor') ?? '', rawType = params.get('event_type') ?? '';
  const validType = !rawType || Object.hasOwn(auditLabels, rawType);
  const type = rawType && validType ? rawType as AuditView['event_type'] : undefined;
  const validActor = !actor || (actor.trim().length > 0 && actor.length <= 200);
  const valid = validType && validActor && window.valid;
  const query = useQuery({ queryKey: ['audits', actor, type, window.start, window.end, page], enabled: valid,
    queryFn: ({ signal }) => api.audits({ limit: 20, offset: (page - 1) * 20, actor: actor || undefined,
      event_type: type, start: window.start, end: window.end }, signal) });
  return <><PageHeader title="审计中心" description="按 UTC 时间、操作类型和操作人精确检索，追溯原始记录与证据。">
    <Button onClick={() => { if (window.explicit) { if (valid) void query.refetch(); } else window.refresh(); }} loading={query.isFetching}>刷新列表</Button></PageHeader>
    <form className="list-filters" key={`${actor}/${rawType}`} onSubmit={(event) => {
      event.preventDefault(); const data = new FormData(event.currentTarget);
      setParams((previous) => { const next = new URLSearchParams(previous); next.delete('page'); next.delete('evidence');
        for (const field of ['actor', 'event_type']) { const value = String(data.get(field) ?? '').trim(); if (value) next.set(field, value); else next.delete(field); }
        return next;
      });
    }}><label>操作人<input name="actor" maxLength={200} defaultValue={actor} placeholder="精确匹配，例如 workflow" /></label>
      <label>操作类型<select aria-label="操作类型" name="event_type" defaultValue={validType ? rawType : ''}><option value="">全部类型</option>
        {Object.entries(auditLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <Button htmlType="submit">查询审计</Button><Button onClick={() => setParams(new URLSearchParams())}>重置筛选</Button></form>
    <TimeWindowFilter window={window} />
    {(!validType || !validActor) && <Alert role="alert" showIcon type="error" title="查询条件无效" description="请选择有效操作类型，操作人须为 1–200 个非空字符。" />}
    {valid && <><p className="control-description"><Time value={window.start} /> 至 <Time value={window.end} />（不含结束时间）</p>
      <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
      {query.data && !query.isError && <><div className="records-panel"><Table<AuditView> rowKey="id" pagination={false} scroll={{ x: 1000 }} dataSource={query.data.items}
        locale={{ emptyText: <EmptyRecords label="没有符合筛选条件的审计记录" /> }} columns={[
          { title: '发生时间', render: (_, row) => <Time value={row.occurred_at} /> },
          { title: '类型', render: (_, row) => auditLabels[row.event_type] },
          { title: '操作人', dataIndex: 'actor' },
          { title: '操作', render: (_, row) => <Link className="record-title" to={`/audit/${row.id}${listSearch(params)}`}>{row.operation}</Link> },
          { title: '结果', dataIndex: 'outcome' },
          { title: '任务', render: (_, row) => row.task_id ? <Link to={`/tasks/${row.task_id}`}>查看关联任务</Link> : '无关联任务' },
          { title: '证据', render: (_, row) => row.evidence_id ? <EvidenceLink id={row.evidence_id} /> : '无证据引用' },
        ]} /></div><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
    </>}<EvidenceDrawer /></>;
}

export function AuditDetailPage() {
  const { id = '' } = useParams();
  const { params } = useListParams();
  const query = useQuery({ queryKey: ['audit', id], queryFn: ({ signal }) => api.audit(id, signal) });
  const data = query.data;
  return <><Link to={`/audit${listSearch(params)}`}>← 返回审计中心</Link>
    <PageHeader title="审计详情" description="记录保留原始操作人、发生时间与结果。"><Button onClick={() => void query.refetch()}>刷新详情</Button></PageHeader>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {data && !query.isError && <Card className="audit-detail" title={data.operation}><dl className="record-meta">
      <div><dt>审计 ID</dt><dd>{data.id}</dd></div><div><dt>操作类型</dt><dd>{auditLabels[data.event_type]}</dd></div>
      <div><dt>操作人</dt><dd>{data.actor}</dd></div><div><dt>发生时间</dt><dd><Time value={data.occurred_at} /></dd></div>
      <div><dt>结果</dt><dd>{data.outcome}</dd></div><div><dt>关联任务</dt><dd>{data.task_id ? <Link to={`/tasks/${data.task_id}`}>{data.task_id}</Link> : '无关联任务'}</dd></div>
      <div><dt>证据引用</dt><dd>{data.evidence_id ? <EvidenceLink id={data.evidence_id} /> : '无证据引用'}</dd></div>
    </dl><h2>操作详情</h2><Snapshot value={data.details} /></Card>}<EvidenceDrawer /></>;
}
