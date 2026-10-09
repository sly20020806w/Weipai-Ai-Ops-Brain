import { Alert, Button } from 'antd';
import type { useTimeWindow } from './use-time-window';

export function TimeWindowFilter({ window }: { window: ReturnType<typeof useTimeWindow> }) {
  return <><form className="list-filters time-window" key={`${window.start}/${window.end}/${window.valid}`} onSubmit={(event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    const start = String(form.get('start')), end = String(form.get('end'));
    if (Date.parse(`${start}Z`) >= Date.parse(`${end}Z`)) {
      const input = event.currentTarget.elements.namedItem('end') as HTMLInputElement;
      input.setCustomValidity('结束时间必须晚于开始时间'); input.reportValidity(); return;
    }
    window.apply(start, end);
  }}><label>开始时间（UTC）<input name="start" type="datetime-local" step="1" required defaultValue={window.start.slice(0, 19)} /></label>
    <label>结束时间（UTC，不含）<input name="end" type="datetime-local" step="1" required defaultValue={window.end.slice(0, 19)} onChange={(event) => event.currentTarget.setCustomValidity('')} /></label>
    <Button htmlType="submit">应用时间窗</Button><Button onClick={() => window.apply('', '')}>最近 30 天</Button></form>
    {!window.valid && <Alert role="alert" showIcon type="error" title="时间窗无效" description="开始和结束时间必须同时提供、带时区，且开始早于结束。请选择有效时间窗。" />}</>;
}
