import { Button, Card, Table, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import type { RiskView, ScenarioView, TaskStatus, RisksApiRisksGetData } from '../api/generated/types.gen';
import { sourceLabels, statusLabels } from '../task-center/labels';
import { EmptyRecords, EvidenceDrawer, EvidenceLink, PageControl, QueryState, Status, Time } from '../task-center/shared';
import { useListParams } from '../task-center/use-list-params';
import * as api from './api';
import { ReportSnapshot } from './report';
import { evidenceTitles } from './labels';

const categories = { stability: '稳定性', capacity: '容量', security: '安全', cost: '成本' } as const;
const checkLabels: Record<string, string> = {
  service_health: '服务健康', k8s_health: 'K8s 健康', cloud_health: '云资源健康', database_health: '数据库健康',
  redis_health: 'Redis 健康', mq_health: 'MQ 健康', disk_usage: '磁盘使用率', network_health: '网络健康',
  monitoring_present: '监控覆盖', alerts_healthy: '告警健康', logs_healthy: '日志异常检查', traces_healthy: 'Trace 异常检查',
  certificate_days: '证书有效期', dns_healthy: 'DNS 健康', capacity_usage: '资源容量使用率', cost_daily_growth: '日成本异常增长',
  security_healthy: '安全基线', single_point: '单点风险', pdb_present: '缺少 PDB', hpa_present: '缺少 HPA',
  replicas: '副本不足', runbook_present: 'Runbook 覆盖', capacity_growth: '容量增长趋势', capacity_exhaustion_days: '预计容量耗尽',
  permissions_excessive: '权限过大', credential_risk: '凭证风险', public_exposure: '公网暴露', security_group_risk: '安全组风险',
  ecs_idle: '闲置 ECS', low_utilization: '资源低利用率', overprovisioned: '资源过度配置', temporary_unreclaimed: '临时资源未回收',
};
function Header({ title, description, refresh }: { title: string; description: string; refresh: () => void }) {
  return <div className="page-head"><div><div className="eyebrow">业务运维</div><h1>{title}</h1><p>{description}</p></div><Button onClick={refresh}>刷新{title.endsWith('详情') ? '详情' : '列表'}</Button></div>;
}
function ServiceFilter({ value, change }: { value: string; change: (value: string) => void }) {
  return <form className="service-filter" key={value} onSubmit={(event) => {
    event.preventDefault(); change(String(new FormData(event.currentTarget).get('service') ?? '').trim());
  }}><label>服务名称<input name="service" defaultValue={value} maxLength={63} placeholder="例如 payment-service" /></label><Button htmlType="submit">查询服务</Button></form>;
}
function listSearch(params: URLSearchParams) { const next = new URLSearchParams(params); next.delete('evidence'); return next.size ? `?${next}` : ''; }

export function OperationsListPage({ center }: { center: api.Center }) {
  const { params, page, update } = useListParams();
  const rawStatus = params.get('status') ?? '';
  const status = Object.hasOwn(statusLabels, rawStatus) ? rawStatus as TaskStatus : undefined;
  const service = params.get('service_name') ?? '';
  const definition = api.centers[center];
  const query = useQuery({ queryKey: ['operations', center, 'list', status, service, page], queryFn: ({ signal }) =>
    api.scenarios(center, { limit: 20, offset: (page - 1) * 20, status, service_name: service || undefined }, signal) });
  return <><Header title={definition.title} description={definition.description} refresh={() => void query.refetch()} />
    <div className="list-filters"><label>任务状态<select aria-label="任务状态" value={status ?? ''} onChange={(event) => update('status', event.target.value)}>
      <option value="">全部状态</option>{Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <ServiceFilter value={service} change={(value) => update('service_name', value)} /></div>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <><div className="records-panel"><Table<ScenarioView> rowKey={(row) => row.task.id} dataSource={query.data.items} pagination={false} scroll={{ x: 780 }}
      locale={{ emptyText: <EmptyRecords label="没有符合筛选条件的记录" /> }} columns={[
        { title: '任务', render: (_, row) => <><Link className="record-title" to={`/${center}/${row.task.id}${listSearch(params)}`}>{row.task.title}</Link><small className="record-id">{row.task.id}</small></> },
        { title: '服务', render: (_, row) => row.event.service_name ?? '未关联服务' },
        { title: '状态', render: (_, row) => <Status value={row.task.status} /> },
        { title: '来源', render: (_, row) => sourceLabels[row.task.source] },
        { title: '事件时间', render: (_, row) => <Time value={row.event.occurred_at} /> },
      ]} /></div><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
  </>;
}

export function OperationsDetailPage({ center }: { center: api.Center }) {
  const { id = '' } = useParams();
  const { params } = useListParams();
  const definition = api.centers[center];
  const query = useQuery({ queryKey: ['operations', center, 'detail', id], queryFn: ({ signal }) => api.scenario(center, id, signal) });
  const data = query.data;
  return <><Link to={`/${center}${listSearch(params)}`}>← 返回{definition.title}</Link>
    <Header title={`${definition.title}详情`} description="查看当前任务状态、事件与已保存的报告；各次观察保留原始时间。" refresh={() => void query.refetch()} />
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {data && !query.isError && <div className="detail-stack operations-detail"><Card title={data.task.title}>
      <dl className="record-meta"><div><dt>状态</dt><dd><Status value={data.task.status} /></dd></div><div><dt>服务</dt><dd>{data.event.service_name ?? '未关联服务'}</dd></div>
        <div><dt>任务</dt><dd><Link to={`/tasks/${data.task.id}`}>查看任务详情与状态时间线</Link></dd></div><div><dt>事件</dt><dd><Link to={`/events/${data.event.id}`}>{data.event.title}</Link></dd></div>
        <div><dt>任务来源</dt><dd>{sourceLabels[data.task.source]}</dd></div><div><dt>最近更新</dt><dd><Time value={data.task.updated_at} /></dd></div>
        <div><dt>源系统标识</dt><dd>{data.event.external_id}</dd></div><div><dt>事件发生时间</dt><dd><Time value={data.event.occurred_at} /></dd></div></dl>
      <Link to={`/approvals/${data.task.id}`}>审批、人工判断、补充信息与接管</Link>
      {center === 'inspections' && <p><Link to={`/risks?service_name=${encodeURIComponent(data.event.service_name ?? '')}`}>查看关联服务风险</Link></p>}
    </Card><Card title="报告与证据">
      <p className="control-description">每条记录均可按 Evidence ID 读回。待核实项保留在报告中，任务结束后仍可跟进未恢复风险。</p>
      {data.evidence.length ? data.evidence.map((record) => <details className="operation-record" key={record.id} open={record.source_tool.endsWith('.report') || ['automation.suggestion', 'ticket.conclusion', 'postmortem'].includes(record.source_tool)}>
        <summary>{evidenceTitles[record.source_tool] ?? record.source_tool} · <Time value={record.collected_at} /></summary>
        <p><EvidenceLink id={record.id} /></p><ReportSnapshot value={record.result_snapshot} />
      </details>) : <EmptyRecords label="尚未产生报告或证据" />}
    </Card></div>}<EvidenceDrawer />
  </>;
}

function Outcome({ value }: { value: string }) { return <Tag color={value === 'abnormal' ? 'red' : 'gold'}>{value === 'abnormal' ? '异常' : value === 'unknown' ? '待核实' : value}</Tag>; }
function Category({ value }: { value: string }) { return <>{categories[value as keyof typeof categories] ?? value}</>; }

export function RiskListPage() {
  const { params, page, update } = useListParams();
  const service = params.get('service_name') ?? '';
  const rawCategory = params.get('category') ?? '';
  const category = Object.hasOwn(categories, rawCategory) ? rawCategory as NonNullable<RisksApiRisksGetData['query']>['category'] : undefined;
  const rawActive = params.get('active');
  const active = rawActive === 'true' ? true : rawActive === 'false' ? false : undefined;
  const query = useQuery({ queryKey: ['operations', 'risks', 'list', service, category, active, page], queryFn: ({ signal }) =>
    api.risks({ limit: 20, offset: (page - 1) * 20, service_name: service || undefined, category, active }, signal) });
  return <><Header title="风险中心" description="跟进稳定性、容量、安全与成本风险；恢复状态以后端重新检查为准。" refresh={() => void query.refetch()} />
    <div className="list-filters"><label>风险类别<select aria-label="风险类别" value={category ?? ''} onChange={(event) => update('category', event.target.value)}><option value="">全部类别</option>{Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>恢复状态<select aria-label="恢复状态" value={active === undefined ? '' : String(active)} onChange={(event) => update('active', event.target.value)}><option value="">全部状态</option><option value="true">未恢复</option><option value="false">已恢复</option></select></label>
      <ServiceFilter value={service} change={(value) => update('service_name', value)} /></div>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <><div className="records-panel"><Table<RiskView> rowKey="id" dataSource={query.data.items} pagination={false} scroll={{ x: 850 }} locale={{ emptyText: <EmptyRecords label="没有符合筛选条件的风险" /> }} columns={[
      { title: '检查项 / 资源', render: (_, row) => <><Link className="record-title" to={`/risks/${row.id}${listSearch(params)}`}>{checkLabels[row.check_id] ?? row.check_id}</Link><p className="catalog-excerpt">{row.resource}</p></> },
      { title: '服务', dataIndex: 'service_name' }, { title: '类别', dataIndex: 'category', render: (value: string) => <Category value={value} /> },
      { title: '检查结果', dataIndex: 'outcome', render: (value: string) => <Outcome value={value} /> },
      { title: '恢复状态', dataIndex: 'active', render: (value: boolean) => <Tag color={value ? 'gold' : 'green'}>{value ? '未恢复' : '已恢复'}</Tag> },
      { title: '最近观察', dataIndex: 'last_seen', render: (value: string) => <Time value={value} /> },
    ]} /></div><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
  </>;
}

export function RiskDetailPage() {
  const { id = '' } = useParams(); const { params } = useListParams();
  const query = useQuery({ queryKey: ['operations', 'risks', 'detail', id], queryFn: ({ signal }) => api.risk(id, signal) });
  const data = query.data;
  return <><Link to={`/risks${listSearch(params)}`}>← 返回风险中心</Link><Header title="风险详情" description="核对风险观察、恢复状态与来源证据。" refresh={() => void query.refetch()} />
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {data && !query.isError && <div className="detail-stack"><Card title={checkLabels[data.check_id] ?? data.check_id}><dl className="record-meta">
      <div><dt>风险标识</dt><dd>{data.id}</dd></div><div><dt>服务</dt><dd>{data.service_name}</dd></div><div><dt>检查标识</dt><dd>{data.check_id}</dd></div><div><dt>资源</dt><dd>{data.resource}</dd></div>
      <div><dt>类别</dt><dd><Category value={data.category} /></dd></div><div><dt>最近检查结果</dt><dd><Outcome value={data.outcome} /></dd></div>
      <div><dt>恢复状态</dt><dd>{data.active ? '未恢复' : '已恢复'}</dd></div><div><dt>异常次数</dt><dd>{data.episode}</dd></div>
      <div><dt>首次观察</dt><dd><Time value={data.first_seen} /></dd></div><div><dt>最近观察</dt><dd><Time value={data.last_seen} /></dd></div>
      <div><dt>恢复时间</dt><dd>{data.cleared_at ? <Time value={data.cleared_at} /> : '尚未记录恢复'}</dd></div></dl>
      <p><Link to={`/inspections?service_name=${encodeURIComponent(data.service_name)}`}>查看服务巡检记录</Link></p></Card>
      <Card title="风险证据"><dl className="record-meta"><div><dt>首次发现证据</dt><dd><EvidenceLink id={data.opening_evidence_id} /></dd></div><div><dt>最新观察证据</dt><dd><EvidenceLink id={data.latest_evidence_id} /></dd></div>
        <div><dt>通知证据</dt><dd>{data.notification_evidence_id ? <EvidenceLink id={data.notification_evidence_id} /> : '尚无通知证据'}</dd></div></dl></Card></div>}<EvidenceDrawer />
  </>;
}
