import {
  AppstoreOutlined, AuditOutlined, BellOutlined, BranchesOutlined, CalendarOutlined,
  CheckSquareOutlined, CloudUploadOutlined, CommentOutlined, DeploymentUnitOutlined,
  ExperimentOutlined, FileTextOutlined, FlagOutlined, ReadOutlined, SafetyCertificateOutlined,
  ThunderboltOutlined, ToolOutlined, WarningOutlined,
} from '@ant-design/icons';

// 顺序和入口逐一对应权威设计第 34 节。
export const navigation = [
  { path: '/dashboard', label: '总览', icon: AppstoreOutlined, description: '任务进展与需要你关注的事项' },
  { path: '/tasks', label: 'AI 任务中心', icon: CheckSquareOutlined, description: '查看 AI 调查、决策与处理进度' },
  { path: '/incidents', label: '事故中心', icon: WarningOutlined, description: '故障记录、处置过程与复盘' },
  { path: '/services', label: '服务与上下文图', icon: DeploymentUnitOutlined, description: '理解服务、资源与依赖关系' },
  { path: '/events', label: '事件中心', icon: BellOutlined, description: '汇集告警、工单与环境变化' },
  { path: '/releases', label: '发布中心', icon: CloudUploadOutlined, description: '发布检查、变更与上线验证' },
  { path: '/inspections', label: '巡检中心', icon: CalendarOutlined, description: '日常健康检查与异常发现' },
  { path: '/tickets', label: '工单中心', icon: FileTextOutlined, description: '统一跟进业务运维请求' },
  { path: '/runbooks', label: '运行手册中心', icon: ToolOutlined, description: '可复用的诊断与处理经验' },
  { path: '/risks', label: '风险中心', icon: SafetyCertificateOutlined, description: '稳定性、容量、安全与成本风险' },
  { path: '/war-room', label: '重大保障', icon: FlagOutlined, description: '活动、高峰期与重大变更保障' },
  { path: '/architecture', label: '架构评审', icon: BranchesOutlined, description: '结合环境与经验评审技术方案' },
  { path: '/automation', label: '自动化中心', icon: ThunderboltOutlined, description: '发现重复劳动与自动化机会' },
  { path: '/approvals', label: '审批中心', icon: AuditOutlined, description: '高风险授权与需要你判断的事项' },
  { path: '/knowledge', label: '知识中心', icon: ReadOutlined, description: '业务规则、公司规范与历史经验' },
  { path: '/audit', label: '审计中心', icon: ExperimentOutlined, description: '追溯操作、证据与状态变化' },
  { path: '/chat', label: 'AI 对话', icon: CommentOutlined, description: '与运维大脑一起调查和处理问题' },
] as const;

export function safeReturnPath(value: unknown): string {
  if (typeof value !== 'string') return '/dashboard';
  const pathname = value.split(/[?#]/, 1)[0] ?? '';
  const detail = /^\/(audit|tasks|events|incidents|approvals|runbooks|knowledge|releases|tickets|inspections|risks|war-room|architecture|automation)\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const catalog = /^\/(runbooks|knowledge)\/new$/.test(pathname);
  const service = /^\/services\/[^/\\]+$/.test(pathname);
  return navigation.some((entry) => entry.path === pathname) || detail.test(pathname) || catalog || service ? value : '/dashboard';
}
