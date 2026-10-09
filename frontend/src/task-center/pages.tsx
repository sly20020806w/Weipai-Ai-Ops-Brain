import { Button, Card, Table, Timeline } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import type { PropsWithChildren } from 'react';
import type { EventView, EvidenceView, IncidentHit, TaskSource, TaskStatus, TaskView, ToolCallView } from '../api/generated/types.gen';
import * as api from './api';
import { EmptyRecords, EvidenceDrawer, EvidenceLink, PageControl, PageHeader, QueryState, Snapshot, Status, Time } from './shared';
import { sourceLabels, statusLabels } from './labels';
import { useListParams } from './use-list-params';

const outcomeLabels: Record<string, string> = { succeeded: '成功', success: '成功', replayed: '历史回放',
  rejected: '已拒绝', denied: '已拒绝', failed: '失败' };

function SourceFilter({ value, change }: { value: string; change: (value: string) => void }) {
  return <label>任务来源<select aria-label="任务来源" value={value} onChange={(event) => change(event.target.value)}>
    <option value="">全部来源</option>{Object.entries(sourceLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
  </select></label>;
}

function ServiceFilter({ value, change }: { value: string; change: (value: string) => void }) {
  return <form className="service-filter" key={value} onSubmit={(event) => {
    event.preventDefault(); const data = new FormData(event.currentTarget); change(String(data.get('service') ?? '').trim());
  }}><label>服务名称<input name="service" defaultValue={value} maxLength={256} placeholder="例如 payment-service" /></label>
    <Button htmlType="submit">查询服务</Button></form>;
}

function ListPanel({ children }: PropsWithChildren) { return <div className="records-panel">{children}</div>; }

export function TaskListPage() {
  const { params, page, update } = useListParams();
  const status = params.get('status') as TaskStatus | null;
  const source = params.get('source') as TaskSource | null;
  const validStatus = status && Object.hasOwn(statusLabels, status) ? status : undefined;
  const validSource = source && Object.hasOwn(sourceLabels, source) ? source : undefined;
  const query = useQuery({ queryKey: ['tasks', validStatus, validSource, page], queryFn: ({ signal }) =>
    api.tasks({ status: validStatus, source: validSource, limit: 20, offset: (page - 1) * 20 }, signal) });
  return <>
    <PageHeader title="AI 任务中心" description="跟进调查、决策、执行与独立验证；时间统一显示为 UTC。">
      <Button aria-label="刷新列表" icon={<ReloadOutlined aria-hidden="true" />} onClick={() => void query.refetch()} loading={query.isFetching}>刷新列表</Button></PageHeader>
    <div className="list-filters"><label>任务状态<select aria-label="任务状态" value={validStatus ?? ''} onChange={(event) => update('status', event.target.value)}>
      <option value="">全部状态</option>{Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select></label><SourceFilter value={validSource ?? ''} change={(value) => update('source', value)} /></div>
    <ListPanel><QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
      {query.data && !query.isError && <><Table<TaskView> rowKey="id" dataSource={query.data.items} pagination={false} scroll={{ x: 760 }}
        locale={{ emptyText: <EmptyRecords label="没有符合筛选条件的任务" /> }} columns={[
          { title: '任务', dataIndex: 'title', render: (_, row) => <div><Link className="record-title" to={`/tasks/${row.id}`}>{row.title}</Link><small className="record-id">{row.id}</small></div> },
          { title: '状态', dataIndex: 'status', render: (value: TaskStatus) => <Status value={value} /> },
          { title: '来源', dataIndex: 'source', render: (value: TaskSource) => sourceLabels[value] },
          { title: '最近更新', dataIndex: 'updated_at', render: (value: string) => <Time value={value} /> },
        ]} /><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
    </ListPanel>
  </>;
}

export function EventListPage() {
  const { params, page, update } = useListParams();
  const source = params.get('source') as TaskSource | null;
  const validSource = source && Object.hasOwn(sourceLabels, source) ? source : undefined;
  const service = params.get('service_name') ?? '';
  const query = useQuery({ queryKey: ['events', validSource, service, page], queryFn: ({ signal }) =>
    api.events({ source: validSource, service_name: service || undefined, limit: 20, offset: (page - 1) * 20 }, signal) });
  return <>
    <PageHeader title="事件中心" description="查看归一化事件及其关联任务。"><Button onClick={() => void query.refetch()} loading={query.isFetching}>刷新列表</Button></PageHeader>
    <div className="list-filters"><SourceFilter value={validSource ?? ''} change={(value) => update('source', value)} />
      <ServiceFilter value={service} change={(value) => update('service_name', value)} /></div>
    <ListPanel><QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
      {query.data && !query.isError && <><Table<EventView> rowKey="id" dataSource={query.data.items} pagination={false} scroll={{ x: 850 }}
        locale={{ emptyText: <EmptyRecords label="没有符合筛选条件的事件" /> }} columns={[
          { title: '事件', dataIndex: 'title', render: (_, row) => <Link className="record-title" to={`/events/${row.id}`}>{row.title}</Link> },
          { title: '服务', dataIndex: 'service_name' }, { title: '来源', dataIndex: 'source', render: (value: TaskSource) => sourceLabels[value] },
          { title: '发生时间', dataIndex: 'occurred_at', render: (value: string) => <Time value={value} /> },
          { title: '关联任务', render: (_, row) => <Link to={`/tasks/${row.task_id}`}>查看任务</Link> },
        ]} /><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
    </ListPanel>
  </>;
}

export function IncidentListPage() {
  const { params, page, update } = useListParams();
  const service = params.get('service_name') ?? '';
  const query = useQuery({ queryKey: ['incidents', service, page], queryFn: ({ signal }) =>
    api.incidents({ service_name: service || undefined, limit: 20, offset: (page - 1) * 20 }, signal) });
  return <>
    <PageHeader title="事故中心" description="按证据追溯故障处置、独立验证和复盘。"><Button onClick={() => void query.refetch()} loading={query.isFetching}>刷新列表</Button></PageHeader>
    <div className="list-filters"><ServiceFilter value={service} change={(value) => update('service_name', value)} /></div>
    <ListPanel><QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
      {query.data && !query.isError && <><Table<IncidentHit> rowKey="evidence_id" dataSource={query.data.items} pagination={false} scroll={{ x: 720 }}
        locale={{ emptyText: <EmptyRecords label="暂无事故复盘" /> }} columns={[
          { title: '事故', render: (_, row) => <Link className="record-title" to={`/incidents/${row.evidence_id}`}>{row.report.title}</Link> },
          { title: '服务', render: (_, row) => row.report.service_name },
          { title: '复盘证据', render: (_, row) => <EvidenceLink id={row.evidence_id} /> },
          { title: '关联任务', render: (_, row) => <Link to={`/tasks/${row.report.task_id}`}>查看任务</Link> },
        ]} /><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
    </ListPanel><EvidenceDrawer />
  </>;
}

function TaskRecords({ id }: { id: string }) {
  const { params, update } = useListParams();
  const callPageNumber = Number(params.get('call_page') ?? 1);
  const callPage = Number.isSafeInteger(callPageNumber) && callPageNumber > 0 && callPageNumber < 1000000 ? callPageNumber : 1;
  const evidence = useQuery({ queryKey: ['task-evidence', id], queryFn: ({ signal }) => api.taskEvidence(id, signal) });
  const history = useQuery({ queryKey: ['task-history', id], queryFn: ({ signal }) => api.history(id, signal) });
  const calls = useQuery({ queryKey: ['task-tool-calls', id, callPage], queryFn: ({ signal }) => api.toolCalls(id, (callPage - 1) * 20, signal) });
  const conclusions = evidence.data?.filter((item) => item.source_tool === 'agent.conclusion').reverse() ?? [];
  return <div className="detail-stack">
    <Card title="调查结论">
      <QueryState pending={evidence.isPending} error={evidence.error} retry={() => void evidence.refetch()} />
      {evidence.data && !evidence.isError && (conclusions.length ? conclusions.map((item, index) => <details className="conclusion" key={item.id} open={index === 0}>
        <summary>{index === 0 ? '最近一次结论' : '历史调查结论'} · <Time value={item.collected_at} /></summary>
        <p>结论证据：<EvidenceLink id={item.id} /></p><Snapshot value={item.result_snapshot} /></details>)
        : <EmptyRecords label="尚无主 Agent 调查结论" />)}
    </Card>
    <Card title="状态时间线">
      <QueryState pending={history.isPending} error={history.error} retry={() => void history.refetch()} />
      {history.data && !history.isError && (history.data.length ? <Timeline items={history.data.map((row) => ({
        title: <><Time value={row.changed_at} /> · 版本 {row.sequence}</>,
        content: <div>{row.from_status ? <><Status value={row.from_status} /> → </> : null}<Status value={row.to_status} />
          <p>{row.reason}</p><small>操作人：{row.actor}</small></div>,
      }))} /> : <EmptyRecords label="暂无状态历史" />)}
    </Card>
    <Card title="Tool 调用">
      <QueryState pending={calls.isPending} error={calls.error} retry={() => void calls.refetch()} />
      {calls.data && !calls.isError && <><Table<ToolCallView> rowKey="id" dataSource={calls.data.items} pagination={false} scroll={{ x: 760 }}
        locale={{ emptyText: <EmptyRecords label="暂无 Tool 调用" /> }} expandable={{ expandedRowRender: (row) => <Snapshot value={row.details} /> }} columns={[
          { title: 'Tool / 操作', dataIndex: 'operation' }, { title: '结果', dataIndex: 'outcome', render: (value: string) => outcomeLabels[value] ?? value },
          { title: '调用时间', dataIndex: 'occurred_at', render: (value: string) => <Time value={value} /> },
          { title: '证据', render: (_, row) => row.evidence_id ? <EvidenceLink id={row.evidence_id} /> : '无证据（查看调用详情）' },
        ]} /><PageControl total={calls.data.total} page={callPage} onChange={(value) => update('call_page', String(value))} /></>}
    </Card>
    <Card title="证据链">
      <QueryState pending={evidence.isPending} error={evidence.error} retry={() => void evidence.refetch()} />
      {evidence.data && !evidence.isError && <Table<EvidenceView> rowKey="id" dataSource={evidence.data} pagination={{ pageSize: 20, showSizeChanger: false }} scroll={{ x: 760 }}
        locale={{ emptyText: <EmptyRecords label="暂无证据" /> }} columns={[
          { title: 'Evidence ID', render: (_, row) => <EvidenceLink id={row.id} /> },
          { title: '来源 Tool', dataIndex: 'source_tool' },
          { title: '采集时间', dataIndex: 'collected_at', render: (value: string) => <Time value={value} /> },
        ]} />}
    </Card>
  </div>;
}

export function TaskDetailPage() {
  const id = useParams().taskId!;
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['task', id], queryFn: ({ signal }) => api.task(id, signal) });
  return <>
    <PageHeader title="任务详情" description="查看真实状态、调查结论和完整证据链。"><Link to="/tasks">返回任务列表</Link></PageHeader>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <>
      <Card className="task-summary" title={query.data.title} extra={<Status value={query.data.status} />}>
        <dl className="record-meta"><div><dt>任务 ID</dt><dd>{query.data.id}</dd></div><div><dt>来源</dt><dd>{sourceLabels[query.data.source]}</dd></div>
          <div><dt>状态版本</dt><dd>{query.data.status_version}</dd></div>
          <div><dt>创建时间</dt><dd><Time value={query.data.created_at} /></dd></div>
          <div><dt>最近更新</dt><dd><Time value={query.data.updated_at} /></dd></div></dl>
        <Button aria-label="刷新任务" icon={<ReloadOutlined aria-hidden="true" />} loading={query.isFetching} onClick={() => {
          for (const key of ['task', 'task-evidence', 'task-history', 'task-tool-calls']) void client.invalidateQueries({ queryKey: [key, id] });
        }}>刷新任务</Button>
        <Link className="task-control-link" to={`/approvals/${id}`}>处理审批、判断或接管</Link>
      </Card><TaskRecords key={id} id={id} />
    </>}<EvidenceDrawer />
  </>;
}

export function EventDetailPage() {
  const id = useParams().eventId!;
  const query = useQuery({ queryKey: ['event', id], queryFn: ({ signal }) => api.event(id, signal) });
  return <><PageHeader title="事件详情" description="事件的来源、去重身份与关联任务。"><Link to="/events">返回事件列表</Link></PageHeader>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <Card title={query.data.title}><dl className="record-meta">
      <div><dt>事件 ID</dt><dd>{query.data.id}</dd></div><div><dt>来源</dt><dd>{sourceLabels[query.data.source]} / {query.data.origin}</dd></div>
      <div><dt>服务</dt><dd>{query.data.service_name}</dd></div><div><dt>外部 ID</dt><dd>{query.data.external_id}</dd></div>
      <div><dt>事件指纹</dt><dd>{query.data.fingerprint}</dd></div><div><dt>发生时间</dt><dd><Time value={query.data.occurred_at} /></dd></div>
      <div><dt>接纳时间</dt><dd><Time value={query.data.created_at} /></dd></div>
      <div><dt>关联任务</dt><dd><Link to={`/tasks/${query.data.task_id}`}>{query.data.task_id}</Link></dd></div>
    </dl></Card>}</>;
}

export function IncidentDetailPage() {
  const id = useParams().incidentId!;
  const query = useQuery({ queryKey: ['incident', id], queryFn: ({ signal }) => api.incident(id, signal) });
  return <><PageHeader title="事故复盘" description="所有结论保留原有 Evidence 引用。"><Link to="/incidents">返回事故列表</Link></PageHeader>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <div className="detail-stack">
      <Card title={query.data.report.title}><p>服务：{query.data.report.service_name}</p>
        <p>关联任务：<Link to={`/tasks/${query.data.report.task_id}`}>{query.data.report.task_id}</Link></p>
        <p>复盘证据：<EvidenceLink id={query.data.evidence_id} /></p>
        <p>运行手册草稿 ID：{query.data.report.runbook_id}</p>
      </Card>
      <Card title="事故时间线"><Timeline items={query.data.report.timeline.map((row) => ({
        title: <Time value={row.occurred_at} />, content: <><p>{row.description}</p><EvidenceLink id={row.evidence_id} /></>,
      }))} /></Card>
      {query.data.report.sections.map((section) => <Card title={section.title} key={section.title}>
        {section.conclusions.map((claim, index) => <div className="claim" key={index}><p>{claim.statement}</p>
          {claim.evidence_ids.map((identity) => <EvidenceLink key={identity} id={identity} />)}</div>)}
      </Card>)}
      <Card title="改进建议">{query.data.report.improvements.map((claim, index) => <div className="claim" key={index}>
        <p>{claim.statement}</p>{claim.evidence_ids.map((identity) => <EvidenceLink key={identity} id={identity} />)}</div>)}
        <h3>改进任务</h3>{query.data.report.improvement_task_ids.map((identity) => <p key={identity}><Link to={`/tasks/${identity}`}>{identity}</Link></p>)}
      </Card>
    </div>}<EvidenceDrawer />
  </>;
}
