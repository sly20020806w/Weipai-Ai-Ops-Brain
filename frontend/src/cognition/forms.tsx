import { cloneElement, useId, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Alert, Button } from 'antd';
import { Link } from 'react-router-dom';
import type { ReactElement } from 'react';
import type { DiagnosticStep, KnowledgeInput, KnowledgeType, KnowledgeView, RiskLevel, RunbookCondition, RunbookInput, RunbookView } from '../api/generated/types.gen';
import * as api from './api';
import { knowledgeLabels } from './labels';

function Field({ label, children }: { label: string; children: ReactElement<{ id?: string }> }) {
  const id = useId();
  return <div className="catalog-field"><label htmlFor={id}>{label}</label>{cloneElement(children, { id })}</div>;
}
export function RiskSelect({ value, onChange, label = '风险等级' }: { value: RiskLevel; onChange: (value: RiskLevel) => void; label?: string }) {
  return <Field label={label}><select value={value} onChange={(event) => onChange(event.target.value as RiskLevel)}>
    {['只读查询', '低风险动作', '有限变更', '生产重要变更', '高风险生产操作', '破坏性或不可逆'].map((text, level) => <option key={level} value={`L${level}`}>L{level} · {text}</option>)}
  </select></Field>;
}
function Conditions({ title, rows, onChange }: { title: string; rows: RunbookCondition[]; onChange: (rows: RunbookCondition[]) => void }) {
  return <fieldset><legend>{title}</legend>{rows.map((row, index) => <div className="catalog-row" key={index}>
    <Field label={`${title} ${index + 1} 字段`}><select value={row.field} onChange={(event) => onChange(rows.map((item, at) => at === index ? { ...item, field: event.target.value as RunbookCondition['field'] } : item))}>
      <option value="service_name">服务名称</option><option value="title">任务标题</option><option value="task_source">任务来源</option></select></Field>
    <Field label={`${title} ${index + 1} 匹配方式`}><select value={row.operator} onChange={(event) => onChange(rows.map((item, at) => at === index ? { ...item, operator: event.target.value as RunbookCondition['operator'] } : item))}>
      <option value="equals">等于</option><option value="contains">包含</option></select></Field>
    <Field label={`${title} ${index + 1} 值`}><input required maxLength={4000} value={row.value} onChange={(event) => onChange(rows.map((item, at) => at === index ? { ...item, value: event.target.value } : item))} /></Field>
    <Button onClick={() => onChange(rows.filter((_, at) => at !== index))} disabled={title === '适用条件' && rows.length === 1}>删除{title} {index + 1}</Button>
  </div>)}<Button disabled={rows.length >= 30} onClick={() => onChange([...rows, { field: 'service_name', operator: 'equals', value: '' }])}>添加{title}</Button></fieldset>;
}
function SaveState({ error, validation, path }: { error: Error | null; validation: string; path: string }) {
  return <>{(validation || error) && <Alert role="alert" type="error" title="保存未完成" description={validation || error?.message} />}
    {error && <Link to={path}>返回列表核对保存结果</Link>}</>;
}

export function KnowledgeForm({ entry, saved, cancel }: { entry?: KnowledgeView; saved: (id: string) => Promise<void>; cancel: () => void }) {
  const [kind, setKind] = useState<KnowledgeType>(entry?.kind ?? 'business_rule');
  const [content, setContent] = useState(entry?.content ?? '');
  const [source, setSource] = useState(entry?.source ?? '');
  const [start, setStart] = useState(() => (entry?.valid_from ?? new Date().toISOString()).replace(/Z$/, ''));
  const [end, setEnd] = useState(entry?.expires_at?.replace(/Z$/, '') ?? '');
  const [validation, setValidation] = useState('');
  const lock = useRef(false);
  const mutation = useMutation({ mutationFn: (body: KnowledgeInput) => api.saveKnowledge(body, entry?.id), onSuccess: async (result) => saved(result.id) });
  async function submit() {
    if (lock.current) return;
    if (!content.trim() || !source.trim()) { setValidation('知识内容与来源不可为空白。'); return; }
    const from = new Date(start + 'Z'), until = end ? new Date(end + 'Z') : null;
    if (!Number.isFinite(from.getTime()) || (until && (!Number.isFinite(until.getTime()) || until <= from))) { setValidation('有效期结束必须晚于开始，时间使用 UTC。'); return; }
    setValidation(''); lock.current = true;
    try { await mutation.mutateAsync({ kind, content, source, valid_from: from.toISOString(), expires_at: until?.toISOString() ?? null }); } catch { /* 由 mutation 呈现错误，保留原输入。 */ } finally { lock.current = false; }
  }
  return <form className="catalog-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <Field label="知识类型"><select value={kind} onChange={(event) => setKind(event.target.value as KnowledgeType)}>{Object.entries(knowledgeLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
    <Field label="知识内容"><textarea rows={8} required maxLength={20000} value={content} onChange={(event) => setContent(event.target.value)} /></Field>
    <Field label="知识来源"><input required maxLength={512} value={source} onChange={(event) => setSource(event.target.value)} placeholder="业务约定、文档或经验来源" /></Field>
    <div className="catalog-row"><Field label="生效时间（UTC）"><input type="datetime-local" step="0.001" required value={start} onChange={(event) => setStart(event.target.value)} /></Field>
      <Field label="失效时间（UTC，可留空）"><input type="datetime-local" step="0.001" value={end} onChange={(event) => setEnd(event.target.value)} /></Field></div>
    <SaveState error={mutation.error} validation={validation} path="/knowledge" />
    <div className="control-buttons"><Button htmlType="submit" type="primary" loading={mutation.isPending}>保存知识</Button><Button aria-label="取消" disabled={mutation.isPending} onClick={cancel}>取消</Button></div>
  </form>;
}

type DiagnosticForm = Omit<DiagnosticStep, 'parameters'> & { parameters_text: string };
export function RunbookForm({ entry, saved, cancel }: { entry?: RunbookView; saved: (id: string) => Promise<void>; cancel: () => void }) {
  const [content, setContent] = useState<RunbookInput>(() => entry ? {
    name: entry.name, description: entry.description, source: entry.source,
    applicability_conditions: entry.applicability_conditions, exclusion_conditions: entry.exclusion_conditions,
    diagnostic_steps: entry.diagnostic_steps, handling_steps: entry.handling_steps, risk_level: entry.risk_level,
    rollback_plan: entry.rollback_plan, verification_steps: entry.verification_steps,
  } : { name: '', description: '', source: '', applicability_conditions: [{ field: 'service_name', operator: 'equals', value: '' }], exclusion_conditions: [],
    diagnostic_steps: [], handling_steps: [{ description: '', risk_level: 'L3' }], risk_level: 'L3', rollback_plan: '', verification_steps: [] });
  const [diagnostics, setDiagnostics] = useState<DiagnosticForm[]>(() => entry?.diagnostic_steps.map((row) => ({ ...row, parameters_text: JSON.stringify(row.parameters, null, 2) }))
    ?? [{ description: '', tool_name: '', risk_level: 'L0', parameters_text: '{}' }]);
  const [verification, setVerification] = useState(entry?.verification_steps.join('\n') ?? '');
  const [validation, setValidation] = useState('');
  const lock = useRef(false);
  const mutation = useMutation({ mutationFn: (body: RunbookInput) => api.saveRunbook(body, entry?.id), onSuccess: async (result) => saved(result.id) });
  const change = <K extends keyof RunbookInput>(key: K, value: RunbookInput[K]) => setContent((previous) => ({ ...previous, [key]: value }));
  async function submit() {
    if (lock.current) return;
    try {
      const steps = diagnostics.map(({ parameters_text, ...step }) => {
        const parameters: unknown = JSON.parse(parameters_text);
        if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) throw new Error('诊断参数必须是 JSON 对象。');
        return { ...step, parameters: parameters as DiagnosticStep['parameters'] };
      });
      const checks = verification.split('\n').map((line) => line.trim()).filter(Boolean);
      if (!checks.length || checks.length > 30 || checks.some((line) => line.length > 4000)) throw new Error('验证方式需要 1–30 条，每条最多 4000 字，每行一条。');
      if (content.handling_steps.some((step) => step.risk_level > content.risk_level)) throw new Error('总体风险等级不能低于任何处理步骤。');
      if ([content.description, content.source, content.rollback_plan, ...content.handling_steps.map((step) => step.description), ...steps.map((step) => step.description),
        ...content.applicability_conditions.map((row) => row.value), ...content.exclusion_conditions.map((row) => row.value)].some((value) => !value.trim())) throw new Error('必填内容与条件不可为空白。');
      setValidation(''); lock.current = true;
      try { await mutation.mutateAsync({ ...content, diagnostic_steps: steps, verification_steps: checks }); } catch { /* 保留输入，不自动重试。 */ } finally { lock.current = false; }
    } catch (error) { setValidation(error instanceof SyntaxError ? '诊断参数不是有效的 JSON 对象，请检查语法。' : (error as Error).message); }
  }
  return <form className="catalog-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    {entry && <Alert role="note" type="info" title="修改内容后将增加版本，并退回草稿等待审核。" />}
    <Field label="手册标识"><input required pattern="[a-z][a-z0-9_-]{0,127}" maxLength={128} value={content.name} onChange={(event) => change('name', event.target.value)} placeholder="例如 payment-connection-check" /></Field>
    <Field label="手册说明"><textarea rows={3} required maxLength={4000} value={content.description} onChange={(event) => change('description', event.target.value)} /></Field>
    <Field label="手册来源"><input required maxLength={4000} value={content.source} onChange={(event) => change('source', event.target.value)} /></Field>
    <Conditions title="适用条件" rows={content.applicability_conditions} onChange={(rows) => change('applicability_conditions', rows)} />
    <Conditions title="排除条件" rows={content.exclusion_conditions} onChange={(rows) => change('exclusion_conditions', rows)} />
    <fieldset><legend>诊断步骤（只读 L0）</legend>{diagnostics.map((row, index) => <div className="catalog-step" key={index}>
      <Field label={`诊断 ${index + 1} 说明`}><input required maxLength={4000} value={row.description} onChange={(event) => setDiagnostics(diagnostics.map((item, at) => at === index ? { ...item, description: event.target.value } : item))} /></Field>
      <Field label={`诊断 ${index + 1} Tool 名称`}><input required pattern="[a-z][a-z0-9_]{0,127}" value={row.tool_name} onChange={(event) => setDiagnostics(diagnostics.map((item, at) => at === index ? { ...item, tool_name: event.target.value } : item))} /></Field>
      <Field label={`诊断 ${index + 1} 参数（JSON 对象）`}><textarea rows={4} required value={row.parameters_text} onChange={(event) => setDiagnostics(diagnostics.map((item, at) => at === index ? { ...item, parameters_text: event.target.value } : item))} /></Field>
      <Button disabled={diagnostics.length === 1} onClick={() => setDiagnostics(diagnostics.filter((_, at) => at !== index))}>删除诊断 {index + 1}</Button>
    </div>)}<Button disabled={diagnostics.length >= 30} onClick={() => setDiagnostics([...diagnostics, { description: '', tool_name: '', parameters_text: '{}', risk_level: 'L0' }])}>添加诊断步骤</Button></fieldset>
    <fieldset><legend>处理步骤</legend>{content.handling_steps.map((row, index) => <div className="catalog-step" key={index}>
      <Field label={`处理 ${index + 1} 说明`}><textarea rows={2} required maxLength={4000} value={row.description} onChange={(event) => change('handling_steps', content.handling_steps.map((item, at) => at === index ? { ...item, description: event.target.value } : item))} /></Field>
      <RiskSelect label={`处理 ${index + 1} 风险`} value={row.risk_level} onChange={(value) => change('handling_steps', content.handling_steps.map((item, at) => at === index ? { ...item, risk_level: value } : item))} />
      <Button disabled={content.handling_steps.length === 1} onClick={() => change('handling_steps', content.handling_steps.filter((_, at) => at !== index))}>删除处理 {index + 1}</Button>
    </div>)}<Button disabled={content.handling_steps.length >= 30} onClick={() => change('handling_steps', [...content.handling_steps, { description: '', risk_level: content.risk_level }])}>添加处理步骤</Button></fieldset>
    <RiskSelect value={content.risk_level} onChange={(value) => change('risk_level', value)} />
    <Field label="回滚方案"><textarea rows={3} required maxLength={4000} value={content.rollback_plan} onChange={(event) => change('rollback_plan', event.target.value)} /></Field>
    <Field label="验证方式（每行一条）"><textarea rows={4} required value={verification} onChange={(event) => setVerification(event.target.value)} /></Field>
    <SaveState error={mutation.error} validation={validation} path="/runbooks" />
    <div className="control-buttons"><Button htmlType="submit" type="primary" loading={mutation.isPending}>保存手册</Button><Button aria-label="取消" disabled={mutation.isPending} onClick={cancel}>取消</Button></div>
  </form>;
}
