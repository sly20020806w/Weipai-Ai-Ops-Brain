import { Alert, Button, Spin } from 'antd';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ApiError } from './api/client';
import { ApprovalDetailPage, ApprovalListPage } from './approval-center/pages';
import { LoginPage } from './auth/login-page';
import { useSession } from './auth/use-session';
import { CatalogCreatePage, KnowledgeDetailPage, KnowledgeListPage, RunbookDetailPage, RunbookListPage, ServiceGraphPage, ServiceListPage } from './cognition/pages';
import { navigation, safeReturnPath } from './navigation';
import { centers } from './operations/api';
import { OperationsDetailPage, OperationsListPage, RiskDetailPage, RiskListPage } from './operations/pages';
import { ChatPage } from './chat/page';
import { ConsoleShell } from './shell/console-shell';
import { EntryPage } from './shell/entry-page';
import { DashboardPage } from './overview/dashboard';
import { AuditDetailPage, AuditListPage } from './overview/audit';
import { EventDetailPage, EventListPage, IncidentDetailPage, IncidentListPage, TaskDetailPage, TaskListPage } from './task-center/pages';

export function ConsoleApp() {
  const session = useSession();
  const location = useLocation();
  if (session.isPending) return <div className="loading-page"><div><Spin size="large" /><p role="status">正在验证会话…</p></div></div>;
  if (session.isError) return <main className="loading-page"><div className="session-error">
    <Alert type="error" showIcon role="alert" title="暂时无法验证会话"
      description={session.error instanceof ApiError ? session.error.message : '暂时无法连接服务，请稍后重试。'} />
    <Button style={{ marginTop: 20 }} onClick={() => void session.refetch()} loading={session.isFetching}>重新连接</Button>
  </div></main>;
  if (!session.data) {
    return location.pathname === '/login' ? <LoginPage /> : <Navigate to="/login" replace
      state={{ from: safeReturnPath(location.pathname + location.search + location.hash) }} />;
  }
  if (location.pathname === '/login') {
    const state = location.state as { from?: unknown } | null;
    return <Navigate to={safeReturnPath(state?.from)} replace />;
  }
  return <Routes>
    <Route element={<ConsoleShell session={session.data} />}>
      <Route index element={<Navigate to="/dashboard" replace />} />
      <Route path="/chat" element={<ChatPage />} />
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route path="/audit" element={<AuditListPage />} />
      <Route path="/audit/:id" element={<AuditDetailPage />} />
      <Route path="/tasks" element={<TaskListPage />} />
      <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
      <Route path="/events" element={<EventListPage />} />
      <Route path="/events/:eventId" element={<EventDetailPage />} />
      <Route path="/incidents" element={<IncidentListPage />} />
      <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
      <Route path="/approvals" element={<ApprovalListPage />} />
      <Route path="/approvals/:taskId" element={<ApprovalDetailPage />} />
      <Route path="/services" element={<ServiceListPage />} />
      <Route path="/services/:serviceName" element={<ServiceGraphPage />} />
      <Route path="/runbooks" element={<RunbookListPage />} />
      <Route path="/runbooks/new" element={<CatalogCreatePage kind="runbooks" />} />
      <Route path="/runbooks/:id" element={<RunbookDetailPage />} />
      <Route path="/knowledge" element={<KnowledgeListPage />} />
      <Route path="/knowledge/new" element={<CatalogCreatePage kind="knowledge" />} />
      <Route path="/knowledge/:id" element={<KnowledgeDetailPage />} />
      {(Object.keys(centers) as Array<keyof typeof centers>).map((center) => <Route key={center} path={`/${center}`} element={<OperationsListPage center={center} />} />)}
      {(Object.keys(centers) as Array<keyof typeof centers>).map((center) => <Route key={`${center}-detail`} path={`/${center}/:id`} element={<OperationsDetailPage center={center} />} />)}
      <Route path="/risks" element={<RiskListPage />} />
      <Route path="/risks/:id" element={<RiskDetailPage />} />
      {navigation.filter((entry) => !['/chat', '/dashboard', '/audit', '/tasks', '/events', '/incidents', '/approvals', '/services', '/runbooks', '/knowledge', '/risks', ...Object.keys(centers).map((center) => `/${center}`)].includes(entry.path))
        .map((entry) => <Route key={entry.path} path={entry.path} element={<EntryPage entry={entry} />} />)}
      <Route path="*" element={<section className="empty-panel"><h1>页面未找到</h1><p>请从导航选择工作入口。</p><Button href="/dashboard">返回总览</Button></section>} />
    </Route>
  </Routes>;
}
