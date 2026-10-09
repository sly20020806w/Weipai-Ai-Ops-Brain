import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Table, Tag } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import type { KnowledgeType, KnowledgeView, NodeView, RunbookMaturity, RunbookView } from '../api/generated/types.gen';
import { EmptyRecords, PageControl, QueryState, Snapshot, Time } from '../task-center/shared';
import { useListParams } from '../task-center/use-list-params';
import * as api from './api';
import { ContextGraph } from './graph';
import { KnowledgeForm, RunbookForm } from './forms';
import { knowledgeLabels, maturityLabels } from './labels';

function Header({ title, description, children }: { title: string; description: string; children?: React.ReactNode }) {
  return <div className="page-head"><div><div className="eyebrow">认知与经验</div><h1>{title}</h1><p>{description}</p></div>{children}</div>;
}
const base = (kind: 'runbooks' | 'knowledge') => `/${kind}`;

export function ServiceListPage() {
  const { page, update } = useListParams();
  const query = useQuery({ queryKey: ['services', page], queryFn: ({ signal }) => api.services((page - 1) * 20, signal) });
  return <><Header title="服务与上下文图" description="从已发现的服务查看业务、资源与上下游关系。"><Button onClick={() => void query.refetch()}>刷新列表</Button></Header>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <><div className="records-panel"><Table dataSource={query.data.items} rowKey="id" pagination={false} scroll={{ x: 640 }}
      locale={{ emptyText: <EmptyRecords label="尚未发现服务" /> }} columns={[
        { title: '服务', render: (_, item: NodeView) => <Link className="record-title" to={`/services/${encodeURIComponent(item.external_id)}`}>{item.name}</Link> },
        { title: '源系统标识', dataIndex: 'external_id' },
      ]} /></div><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
  </>;
}
export function ServiceGraphPage() {
  const { serviceName = '' } = useParams();
  const { params, update } = useListParams();
  const parsed = Number(params.get('hops') ?? 2);
  const hops = [1, 2, 3, 4].includes(parsed) ? parsed : 2;
  const raw = params.get('direction');
  const direction = raw === 'upstream' || raw === 'downstream' ? raw : 'both';
  const query = useQuery({ queryKey: ['service-graph', serviceName, hops, direction], queryFn: ({ signal }) => api.graph(serviceName, hops, direction, signal) });
  return <><Link to="/services">← 服务列表</Link><Header title="服务上下文" description={serviceName}><Button onClick={() => void query.refetch()}>刷新关系图</Button></Header>
    <div className="list-filters"><label>邻居跳数<select aria-label="邻居跳数" value={hops} onChange={(event) => update('hops', event.target.value)}>{[1, 2, 3, 4].map((value) => <option key={value} value={value}>{value} 跳</option>)}</select></label>
      <label>关系方向<select aria-label="关系方向" value={direction} onChange={(event) => update('direction', event.target.value)}><option value="both">完整上下文</option><option value="upstream">上游调用</option><option value="downstream">下游调用</option></select></label></div>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <><p className="control-description">本次读取：<Time value={query.data.as_of} />；新鲜度按此时间计算，刷新后重新计算。</p>
      {query.data.nodes.length ? <ContextGraph key={`${serviceName}-${hops}-${direction}`} graph={query.data} /> : <EmptyRecords label="暂无上下文节点" />}</>}
  </>;
}

export function RunbookListPage() {
  const { page, params, update } = useListParams();
  const value = params.get('maturity') ?? '';
  const maturity = Object.hasOwn(maturityLabels, value) ? value as RunbookMaturity : undefined;
  const query = useQuery({ queryKey: ['catalog', 'runbooks', 'list', page, maturity], queryFn: ({ signal }) => api.runbooks((page - 1) * 20, maturity, signal) });
  return <><Header title="运行手册中心" description="管理可复用的诊断、处理、回滚与验证经验。"><div className="control-buttons"><Button onClick={() => void query.refetch()}>刷新列表</Button><Link className="catalog-create" to="/runbooks/new">新建手册</Link></div></Header>
    <div className="list-filters"><label>手册成熟度<select aria-label="手册成熟度" value={maturity ?? ''} onChange={(event) => update('maturity', event.target.value)}><option value="">全部成熟度</option>{Object.entries(maturityLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <Button onClick={() => update('maturity', '')}>重置筛选</Button></div>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <><div className="records-panel"><Table dataSource={query.data.items} rowKey="id" pagination={false} scroll={{ x: 780 }} locale={{ emptyText: <EmptyRecords label="暂无运行手册" /> }} columns={[
      { title: '手册', render: (_, item: RunbookView) => <><Link className="record-title" to={`/runbooks/${item.id}`}>{item.name}</Link><p className="catalog-excerpt">{item.description}</p></> },
      { title: '成熟度', dataIndex: 'maturity', render: (value: RunbookMaturity) => <Tag>{maturityLabels[value]}</Tag> },
      { title: '风险', dataIndex: 'risk_level' },
      { title: '成功 / 失败', render: (_, item: RunbookView) => `${item.success_count} / ${item.failure_count}` },
      { title: '可信度', dataIndex: 'confidence', render: (value: number) => `${(value * 100).toFixed(1)}%` },
    ]} /></div><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
  </>;
}
export function KnowledgeListPage() {
  const { page, params, update } = useListParams();
  const value = params.get('kind') ?? '';
  const kind = Object.hasOwn(knowledgeLabels, value) ? value as KnowledgeType : undefined;
  const query = useQuery({ queryKey: ['catalog', 'knowledge', 'list', page, kind], queryFn: ({ signal }) => api.knowledge((page - 1) * 20, kind, signal) });
  return <><Header title="知识中心" description="保存业务规则、规范与机器无法自动获得的经验。"><div className="control-buttons"><Button onClick={() => void query.refetch()}>刷新列表</Button><Link className="catalog-create" to="/knowledge/new">新建知识</Link></div></Header>
    <div className="list-filters"><label>知识类型<select aria-label="知识类型" value={kind ?? ''} onChange={(event) => update('kind', event.target.value)}><option value="">全部类型</option>{Object.entries(knowledgeLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><Button onClick={() => update('kind', '')}>重置筛选</Button></div>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <><div className="records-panel"><Table dataSource={query.data.items} rowKey="id" pagination={false} scroll={{ x: 720 }} locale={{ emptyText: <EmptyRecords label="暂无知识条目" /> }} columns={[
      { title: '内容', render: (_, item: KnowledgeView) => <Link className="record-title catalog-excerpt" to={`/knowledge/${item.id}`}>{item.content}</Link> },
      { title: '类型', dataIndex: 'kind', render: (value: KnowledgeType) => knowledgeLabels[value] },
      { title: '来源', dataIndex: 'source' }, { title: '失效时间', dataIndex: 'expires_at', render: (value: string | null) => value ? <Time value={value} /> : '长期有效' },
    ]} /></div><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
  </>;
}

function DeleteControl({ kind, id, name, deleted }: { kind: 'runbooks' | 'knowledge'; id: string; name: string; deleted: () => Promise<void> }) {
  const [confirm, setConfirm] = useState(false);
  const lock = useRef(false);
  const mutation = useMutation({ mutationFn: () => kind === 'runbooks' ? api.deleteRunbook(id) : api.deleteKnowledge(id), onSuccess: deleted });
  async function remove() { if (lock.current) return; lock.current = true; try { await mutation.mutateAsync(); } catch { /* 保留失败内容。 */ } finally { lock.current = false; } }
  return <div className="catalog-delete">{confirm ? <><Alert type="warning" title={`确认删除「${name}」？`} description="删除后该条目不再参与检索，已有证据快照仍可追溯。" />
    <div className="control-buttons"><Button danger loading={mutation.isPending} onClick={() => void remove()}>确认删除</Button><Button disabled={mutation.isPending} onClick={() => setConfirm(false)}>取消删除</Button></div></>
    : <Button danger onClick={() => setConfirm(true)}>删除{kind === 'runbooks' ? '手册' : '知识'}</Button>}
    {mutation.error && <Alert role="alert" type="error" title="删除未确认" description={mutation.error.message} />}</div>;
}

export function CatalogCreatePage({ kind }: { kind: 'runbooks' | 'knowledge' }) {
  const client = useQueryClient(), navigate = useNavigate();
  const saved = async (id: string) => { await client.invalidateQueries({ queryKey: ['catalog', kind] }); navigate(`${base(kind)}/${id}`, { replace: true }); };
  return <><Link to={base(kind)}>← 返回列表</Link><Header title={kind === 'runbooks' ? '新建运行手册' : '新建知识'} description={kind === 'runbooks' ? '新内容以草稿保存，成熟度由审核与独立验证推进。' : '请注明知识来源与有效期。'} />
    <Card>{kind === 'runbooks' ? <RunbookForm saved={saved} cancel={() => navigate(base(kind))} /> : <KnowledgeForm saved={saved} cancel={() => navigate(base(kind))} />}</Card></>;
}

function RunbookContent({ entry }: { entry: RunbookView }) {
  return <><dl className="record-meta"><div><dt>成熟度 / 自动化等级</dt><dd>{maturityLabels[entry.maturity]} / {entry.automation_level === 'manual' ? '人工' : maturityLabels[entry.automation_level]}</dd></div>
    <div><dt>风险等级</dt><dd>{entry.risk_level}</dd></div><div><dt>成功 / 失败</dt><dd>{entry.success_count} / {entry.failure_count}</dd></div><div><dt>可信度</dt><dd>{(entry.confidence * 100).toFixed(1)}%</dd></div><div><dt>内容版本</dt><dd>{entry.content_version ?? 1}</dd></div><div><dt>来源</dt><dd>{entry.source}</dd></div></dl>
    <h3>手册说明</h3><p className="snapshot-text">{entry.description}</p>
    {(['applicability_conditions', 'exclusion_conditions'] as const).map((field) => <section key={field}><h3>{field === 'applicability_conditions' ? '适用条件' : '排除条件'}</h3>
      {entry[field].length ? <ul>{entry[field].map((row, index) => <li key={index}>{({ service_name: '服务名称', title: '任务标题', task_source: '任务来源' })[row.field]} {row.operator === 'equals' ? '等于' : '包含'} {row.value}</li>)}</ul> : <p>无排除条件</p>}</section>)}
    <h3>诊断步骤</h3><ol>{entry.diagnostic_steps.map((step, index) => <li key={index}><p>{step.description} · {step.tool_name} · L0</p><Snapshot value={step.parameters} /></li>)}</ol>
    <h3>处理步骤</h3><ol>{entry.handling_steps.map((step, index) => <li key={index}>{step.description} · {step.risk_level}</li>)}</ol>
    <h3>回滚方案</h3><p className="snapshot-text">{entry.rollback_plan}</p><h3>验证方式</h3><ol>{entry.verification_steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
  </>;
}
export function RunbookDetailPage() {
  const { id = '' } = useParams();
  const query = useQuery({ queryKey: ['catalog', 'runbooks', 'detail', id], queryFn: ({ signal }) => api.runbook(id, signal) });
  return <><Link to="/runbooks">← 手册列表</Link><Header title="运行手册详情" description="查看适用条件、诊断步骤与处理经验。"><Button onClick={() => void query.refetch()}>刷新详情</Button></Header>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <RunbookDetail key={id} entry={query.data} />}</>;
}
function RunbookDetail({ entry }: { entry: RunbookView }) {
  const [editing, edit] = useState(false);
  const client = useQueryClient(), navigate = useNavigate();
  const saved = async () => { await client.invalidateQueries({ queryKey: ['catalog', 'runbooks'] }); edit(false); };
  return <Card title={entry.name} className="catalog-detail">{editing ? <RunbookForm entry={entry} saved={saved} cancel={() => edit(false)} /> : <><RunbookContent entry={entry} /><p>更新于 <Time value={entry.updated_at} /></p>
    <Button onClick={() => edit(true)}>编辑手册</Button><DeleteControl kind="runbooks" id={entry.id} name={entry.name} deleted={async () => { client.removeQueries({ queryKey: ['catalog', 'runbooks', 'detail', entry.id] }); await client.invalidateQueries({ queryKey: ['catalog', 'runbooks', 'list'] }); navigate('/runbooks', { replace: true }); }} /></>}</Card>;
}
export function KnowledgeDetailPage() {
  const { id = '' } = useParams();
  const query = useQuery({ queryKey: ['catalog', 'knowledge', 'detail', id], queryFn: ({ signal }) => api.entry(id, signal) });
  return <><Link to="/knowledge">← 知识列表</Link><Header title="知识详情" description="查看知识内容、来源与有效期。"><Button onClick={() => void query.refetch()}>刷新详情</Button></Header>
    <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
    {query.data && !query.isError && <KnowledgeDetail key={id} entry={query.data} />}</>;
}
function KnowledgeDetail({ entry }: { entry: KnowledgeView }) {
  const [editing, edit] = useState(false);
  const client = useQueryClient(), navigate = useNavigate();
  const saved = async () => { await client.invalidateQueries({ queryKey: ['catalog', 'knowledge'] }); edit(false); };
  return <Card title={knowledgeLabels[entry.kind]} className="catalog-detail">{editing ? <KnowledgeForm entry={entry} saved={saved} cancel={() => edit(false)} /> : <><p className="human-question">{entry.content}</p><dl className="record-meta">
    <div><dt>来源</dt><dd>{entry.source}</dd></div><div><dt>生效时间</dt><dd>{entry.valid_from ? <Time value={entry.valid_from} /> : '未提供'}</dd></div><div><dt>失效时间</dt><dd>{entry.expires_at ? <Time value={entry.expires_at} /> : '长期有效'}</dd></div>
    <div><dt>更新时间</dt><dd><Time value={entry.updated_at} /></dd></div></dl><Button onClick={() => edit(true)}>编辑知识</Button><DeleteControl kind="knowledge" id={entry.id} name={entry.content.slice(0, 60)} deleted={async () => { client.removeQueries({ queryKey: ['catalog', 'knowledge', 'detail', entry.id] }); await client.invalidateQueries({ queryKey: ['catalog', 'knowledge', 'list'] }); navigate('/knowledge', { replace: true }); }} /></>}</Card>;
}
