import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

// 自定义窗口始终显式 UTC；默认窗口随手动刷新更新，同一查询使用固定起止时间。
export function useTimeWindow() {
  const [params, setParams] = useSearchParams();
  const [now, setNow] = useState(() => new Date());
  const rawStart = params.get('start'), rawEnd = params.get('end');
  const explicit = rawStart !== null || rawEnd !== null;
  const valid = !explicit || Boolean(rawStart && rawEnd && /(Z|[+-]\d{2}:\d{2})$/i.test(rawStart)
    && /(Z|[+-]\d{2}:\d{2})$/i.test(rawEnd) && Number.isFinite(Date.parse(rawStart)) && Number.isFinite(Date.parse(rawEnd))
    && Date.parse(rawStart) < Date.parse(rawEnd));
  const start = explicit && valid ? new Date(rawStart!).toISOString() : new Date(now.getTime() - 30 * 86400000).toISOString();
  const end = explicit && valid ? new Date(rawEnd!).toISOString() : now.toISOString();
  function apply(startValue: string, endValue: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous); next.delete('page'); next.delete('evidence');
      if (startValue && endValue) { next.set('start', new Date(`${startValue}Z`).toISOString()); next.set('end', new Date(`${endValue}Z`).toISOString()); }
      else { next.delete('start'); next.delete('end'); setNow(new Date()); }
      return next;
    });
  }
  return { start, end, valid, explicit, apply, refresh: () => setNow(new Date()) };
}
