import { Button, Card, Table } from 'antd';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import type { TaskView } from '../api/generated/types.gen';
import { pendingCategories } from '../approval-center/categories';
import { tasks } from '../task-center/api';
import { sourceLabels, statusLabels } from '../task-center/labels';
import { EmptyRecords, PageHeader, QueryState, Status, Time } from '../task-center/shared';
import * as api from './api';
import { metricText } from './format';
import { TimeWindowFilter } from './window';
import { useTimeWindow } from './use-time-window';

export function DashboardPage() {
  const window = useTimeWindow();
  const active = useQuery({ queryKey: ['dashboard', 'active'], queryFn: ({ signal }) => api.activeTasks(signal) });
  const pending = useQueries({ queries: pendingCategories.map((category) => ({ queryKey: ['tasks', 'control', category.key, 1],
    queryFn: ({ signal }: { signal: AbortSignal }) => tasks({ status: category.status, limit: 20, offset: 0 }, signal) })) });
  const metrics = useQuery({ queryKey: ['metrics', window.start, window.end], enabled: window.valid,
    queryFn: ({ signal }) => api.metrics({ start: window.start, end: window.end }, signal) });
  function refresh() {
    void active.refetch(); for (const query of pending) void query.refetch();
    if (window.explicit) { if (window.valid) void metrics.refetch(); } else window.refresh();
  }
  return <><PageHeader title="总览" description="查看正在推进的工作、需要你处理的事项与 AI 能力表现。">
    <Button aria-label="刷新总览" onClick={refresh} loading={active.isFetching || pending.some((query) => query.isFetching) || metrics.isFetching}>刷新总览</Button></PageHeader>
    <h2 className="overview-heading">待我处理</h2><div className="pending-grid">
      {pendingCategories.map((category, index) => { const query = pending[index]!; return <Card key={category.key} title={category.label}>
        <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
        {query.data && !query.isError && <div className="overview-count" aria-label={`${category.label}数量`}>{query.data.total}</div>}
        <p className="control-description">{category.description}</p><Link to={`/approvals?kind=${category.key}`}>处理{category.label}事项</Link>
      </Card>; })}</div>
    <Card className="overview-section" title="运行中的任务" extra={<Link to="/tasks">查看全部任务</Link>}>
      <p className="control-description">包括新建、调查、执行、验证与复盘阶段；人工等待事项单独显示。下方列出最近创建的十个任务。</p>
      <QueryState pending={active.isPending} error={active.error} retry={() => void active.refetch()} />
      {active.data && !active.isError && <><p>当前共 <strong aria-label="运行中任务数量">{active.data.total}</strong> 个</p>
        <div className="stage-links">{active.data.stages.map(({ status, total }) => <Link key={status} to={`/tasks?status=${status}`}>{statusLabels[status]}：{total}</Link>)}</div>
        <Table<TaskView> rowKey="id" pagination={false} dataSource={active.data.items} scroll={{ x: 680 }}
          locale={{ emptyText: <EmptyRecords label="暂无运行中任务" /> }} columns={[
            { title: '任务', render: (_, row) => <Link className="record-title" to={`/tasks/${row.id}`}>{row.title}</Link> },
            { title: '状态', render: (_, row) => <Status value={row.status} /> },
            { title: '来源', render: (_, row) => sourceLabels[row.source] },
            { title: '最近更新', render: (_, row) => <Time value={row.updated_at} /> },
          ]} /></>}
    </Card>
    <section className="overview-section" aria-label="能力指标"><h2 className="overview-heading">能力指标</h2>
      <p className="control-description">统计所选时间窗内创建且在窗口结束前已结束的任务；未标注或零样本保留为未知。</p>
      <TimeWindowFilter window={window} />
      {window.valid && <><p className="control-description"><Time value={window.start} /> 至 <Time value={window.end} />（不含结束时间）</p>
        <QueryState pending={metrics.isPending} error={metrics.error} retry={() => void metrics.refetch()} />
        {metrics.data && !metrics.isError && <><p>已评价任务样本：{metrics.data.samples.length}</p><div className="metrics-grid">{metrics.data.metrics.map((metric) =>
          <Card key={metric.name} title={metric.label} data-metric={metric.name}><div className="metric-value">{metricText(metric)}</div>
            <details><summary>查看统计依据</summary><dl className="metric-facts"><div><dt>接口原值</dt><dd>{metric.value === null ? '未知' : String(metric.value)}</dd></div>
              <div><dt>分子</dt><dd>{metric.numerator}</dd></div><div><dt>分母</dt><dd>{metric.denominator}</dd></div><div><dt>单位</dt><dd>{metric.unit}</dd></div></dl></details>
          </Card>)}</div></>}
      </>}
    </section></>;
}
