import type { TaskStatus } from '../api/generated/types.gen';

export const pendingCategories = [
  { key: 'approval', label: '待审批', status: 'WAITING_APPROVAL', description: 'AI 已有动作方案，需要你授权高风险操作。' },
  { key: 'judgment', label: '待人工判断', status: 'NEED_HUMAN_JUDGMENT', description: 'AI 需要你提供业务取舍或现实判断。' },
  { key: 'information', label: '待补充信息', status: 'WAITING_INFORMATION', description: '补充当前调查缺失的信息，帮助 AI 继续工作。' },
] satisfies { key: string; label: string; status: TaskStatus; description: string }[];
