import type { TaskSource, TaskStatus } from '../api/generated/types.gen';

export const statusLabels = {
  NEW: '新建', CONTEXT_BUILDING: '构建上下文', RUNBOOK_MATCHING: '匹配运行手册', INVESTIGATING: '调查中',
  RCA: '根因分析', PLANNING: '规划动作', NEED_HUMAN_JUDGMENT: '等待人工判断', WAITING_INFORMATION: '等待补充信息',
  WAITING_APPROVAL: '等待审批', EXECUTING: '执行中', VERIFYING: '验证中', RESOLVED: '已验证解决',
  FAILED: '失败', AUTOMATION_ABORTED: '自动化已熔断', ESCALATED: '已转人工', LEARNING: '复盘学习', CLOSED: '已关闭',
} satisfies Record<TaskStatus, string>;
export const sourceLabels = {
  Alert: '告警', Ticket: '工单', Schedule: '定时', State: '状态', Prediction: '预测', Release: '发布', Human: '人工', AI: 'AI',
} satisfies Record<TaskSource, string>;
