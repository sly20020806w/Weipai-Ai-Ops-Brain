import { Button, Card, Table, Tabs } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import type { TaskStatus, TaskView } from '../api/generated/types.gen';
import * as taskApi from '../task-center/api';
import { EmptyRecords, EvidenceDrawer, PageControl, PageHeader, QueryState, Status, Time } from '../task-center/shared';
import { sourceLabels } from '../task-center/labels';
import { useListParams } from '../task-center/use-list-params';
import * as api from './api';
import { ApprovalPlan, TaskControls } from './controls';
import { pendingCategories } from './categories';

const categories = [
  ...pendingCategories,
  { key: 'takeover', label: '人工接管', status: undefined, description: '选择任务并写明接管原因，停止后续自动化。' },
] satisfies { key: string; label: string; status: TaskStatus | undefined; description: string }[];

export function ApprovalListPage() {
  const { params, page, update } = useListParams();
  const category = categories.find((item) => item.key === params.get('kind')) ?? categories[0]!;
  const status = category.status;
  const query = useQuery({ queryKey: ['tasks', 'control', category.key, page], queryFn: ({ signal }) =>
    taskApi.tasks({ status, limit: 20, offset: (page - 1) * 20 }, signal) });
  return <><PageHeader title="审批中心" description="风险授权、人工判断与补充信息各自处理。">
    <Button onClick={() => void query.refetch()} loading={query.isFetching}>刷新列表</Button></PageHeader>
    <Tabs activeKey={category.key} onChange={(value) => update('kind', value)} items={categories.map(({ key, label }) => ({ key, label }))} />
    <p className="control-description">{category.description}</p>
    <section className="records-panel" aria-label={`${category.label}列表`}>
      <QueryState pending={query.isPending} error={query.error} retry={() => void query.refetch()} />
      {query.data && !query.isError && <><Table<TaskView> rowKey="id" dataSource={query.data.items} pagination={false} scroll={{ x: 720 }}
        locale={{ emptyText: <EmptyRecords label={`暂无${category.label}任务`} /> }} columns={[
          { title: '任务', render: (_, row) => <div><Link className="record-title" to={`/approvals/${row.id}?kind=${category.key}`}>{row.title}</Link>
            <small className="record-id">{row.id}</small></div> },
          { title: '状态', render: (_, row) => <Status value={row.status} /> },
          { title: '来源', render: (_, row) => sourceLabels[row.source] },
          { title: '最近更新', render: (_, row) => <Time value={row.updated_at} /> },
        ]} /><PageControl total={query.data.total} page={page} onChange={(value) => update('page', String(value))} /></>}
    </section></>;
}

export function ApprovalDetailPage() {
  const id = useParams().taskId!;
  const client = useQueryClient();
  const task = useQuery({ queryKey: ['task', id], queryFn: ({ signal }) => taskApi.task(id, signal) });
  const interaction = useQuery({ queryKey: ['interaction', id], queryFn: ({ signal }) => api.interaction(id, signal) });
  const history = useQuery({ queryKey: ['task-history', id], queryFn: ({ signal }) => taskApi.history(id, signal) });
  const { params } = useListParams();
  function refresh() {
    for (const key of ['task', 'interaction', 'task-history']) void client.invalidateQueries({ queryKey: [key, id] });
  }
  return <><PageHeader title="人工处理详情" description="核对动作和证据后提交；任务状态由后端返回。">
    <Link to={`/approvals?kind=${categories.find((item) => item.key === params.get('kind'))?.key ?? 'approval'}`}>返回审批中心</Link></PageHeader>
    <QueryState pending={task.isPending} error={task.error} retry={() => void task.refetch()} />
    {task.data && !task.isError && <>
      <Card title={task.data.title} className="task-summary" extra={<Status value={task.data.status} />}>
        <dl className="record-meta"><div><dt>任务 ID</dt><dd>{task.data.id}</dd></div>
          <div><dt>状态版本</dt><dd>{task.data.status_version}</dd></div><div><dt>最近更新</dt><dd><Time value={task.data.updated_at} /></dd></div></dl>
        <div className="control-buttons"><Button onClick={refresh} loading={task.isFetching || interaction.isFetching}>刷新状态</Button>
          <Link to={`/tasks/${id}`}>查看调查与证据链</Link></div>
      </Card>
      <QueryState pending={interaction.isPending} error={interaction.error} retry={() => void interaction.refetch()} />
      <div className="detail-stack">{interaction.data && !interaction.isError && interaction.data.status === task.data.status
        && interaction.data.status_version === task.data.status_version && <ApprovalPlan value={interaction.data} />}</div>
      <TaskControls key={id} task={task.data} interaction={interaction.data} ready={!interaction.isError && !interaction.isFetching && !task.isFetching} refresh={refresh} />
      <Card title="最近状态历史" className="control-history">
        <QueryState pending={history.isPending} error={history.error} retry={() => void history.refetch()} />
        {history.data && !history.isError && <ol>{history.data.slice(-12).map((row) => <li key={row.id}>
          <Time value={row.changed_at} /> · <Status value={row.to_status} /><p>{row.reason}</p><small>操作人：{row.actor}</small>
        </li>)}</ol>}
      </Card>
    </>}<EvidenceDrawer /></>;
}
