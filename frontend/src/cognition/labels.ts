import type { KnowledgeType, RunbookMaturity } from '../api/generated/types.gen';

export const knowledgeLabels: Record<KnowledgeType, string> = {
  business_rule: '业务规则', standard: '公司规范', sop: '操作规程', experience: '历史经验',
  constraint: '特殊限制', team_convention: '团队约定', business_priority: '业务优先级',
};
export const maturityLabels: Record<RunbookMaturity, string> = {
  draft: '草稿', reviewed: '已审核', verified: '已验证', semi_automated: '半自动',
  approval_automated: '审批自动化', self_healing: '自愈',
};
export const nodeLabels: Record<string, string> = {
  service: '服务', business: '业务', repository: '仓库', version: '版本', cluster: '集群',
  namespace: '命名空间', deployment: '工作负载', pod: '容器组', rds: '数据库', redis: '缓存',
  mq: '消息队列', topic: '主题', ecs: '云主机', cloud_resource: '云资源', database: '数据库', cache: '缓存',
  owner: '负责人', image: '镜像', cloud_ecs: '云主机', cloud_mq: '消息队列', cloud_slb: '负载均衡',
  cloud_vpc: '私有网络', cloud_dns: '域名解析', cloud_cdn: '内容分发',
};
export function age(seconds: number) {
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
  return `${Math.floor(seconds / 86400)} 天`;
}
