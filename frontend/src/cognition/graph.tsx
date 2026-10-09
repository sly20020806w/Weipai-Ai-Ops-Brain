import { useId, useLayoutEffect, useRef, useState } from 'react';
import { Card, Tooltip } from 'antd';
import type { EdgeView, GraphContext } from '../api/generated/types.gen';
import { Time } from '../task-center/shared';
import { age, nodeLabels } from './labels';

function EdgeFacts({ edge }: { edge: EdgeView }) {
  return <dl className="graph-facts"><div><dt>来源</dt><dd>{edge.source}</dd></div>
    <div><dt>置信度</dt><dd>{(edge.confidence * 100).toFixed(1)}%</dd></div>
    <div><dt>新鲜度</dt><dd>距上次观察 {age(edge.freshness_seconds)}（{edge.freshness_seconds} 秒）</dd></div>
    <div><dt>首次观察</dt><dd><Time value={edge.first_seen} /></dd></div>
    <div><dt>上次观察</dt><dd><Time value={edge.last_seen} /></dd></div></dl>;
}

export function ContextGraph({ graph }: { graph: GraphContext }) {
  const marker = useId().replace(/:/g, '');
  const viewport = useRef<HTMLDivElement>(null);
  const [selected, select] = useState<string | null>(null);
  const root = graph.nodes.find((node) => node.kind === 'service' && node.external_id === graph.service_name);
  const others = graph.nodes.filter((node) => node.id !== root?.id).sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name));
  const width = 1100;
  const height = Math.max(260, Math.ceil(others.length / 2) * 76 + 80);
  const positions = new Map(graph.nodes.map((node) => [node.id, { x: width / 2, y: height / 2 }]));
  others.forEach((node, index) => positions.set(node.id, { x: index % 2 ? 900 : 200, y: 56 + Math.floor(index / 2) * 76 }));
  const names = new Map(graph.nodes.map((node) => [node.id, node.name]));
  const edge = graph.edges.find((item) => item.id === selected);
  const node = graph.nodes.find((item) => item.id === selected);
  useLayoutEffect(() => {
    const area = viewport.current;
    if (!area || !root) return;
    const center = () => { area.scrollLeft = Math.max(0, width / 2 - area.clientWidth / 2); area.scrollTop = Math.max(0, height / 2 - area.clientHeight / 2); };
    center(); const observer = new ResizeObserver(center); observer.observe(area);
    return () => observer.disconnect();
  }, [width, height, root]);
  return <div className="graph-layout">
    <Card title="服务关系图" extra={<span>{graph.nodes.length} 个节点 · {graph.edges.length} 条关系</span>}>
      <p className="control-description">箭头指向关系目标。悬停或聚焦关系查看来源与新鲜度；点击关系或节点查看详情。可在图内横向滚动。</p>
      <div ref={viewport} className="graph-scroll" tabIndex={0} aria-label="服务关系图滚动区域">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-label={`${graph.service_name} 上下文关系图`}>
          <defs><marker id={marker} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#8796bc" /></marker></defs>
          {graph.edges.map((item, index) => {
            const from = positions.get(item.from_node_id), to = positions.get(item.to_node_id);
            if (!from || !to) return null;
            const label = `${names.get(item.from_node_id)} → ${names.get(item.to_node_id)} · ${item.relation} · ${item.source}`;
            const dx = to.x - from.x, dy = to.y - from.y, extent = Math.max(Math.abs(dx) / 122, Math.abs(dy) / 34) || 1;
            const x1 = from.x + dx / extent, y1 = from.y + dy / extent;
            const x2 = to.x - dx / extent, y2 = to.y - dy / extent;
            const curve = (index % 5 - 2) * 18;
            const path = from === to ? `M${from.x},${from.y - 28} c90,-85 90,85 0,56`
              : `M${x1},${y1} Q${(x1 + x2) / 2 + curve},${(y1 + y2) / 2 + curve} ${x2},${y2}`;
            return <Tooltip key={item.id} trigger={['hover', 'focus']} title={<><strong>{label}</strong><EdgeFacts edge={item} /></>}>
              <g role="button" tabIndex={0} aria-label={`关系 ${label}`} className="graph-edge"
                onClick={() => select(item.id)} onKeyDown={(event) => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); select(item.id); } }}>
                <path d={path} fill="none" stroke="transparent" strokeWidth="18" />
                <path d={path} fill="none" stroke={selected === item.id ? '#5668e8' : '#b4bfd8'} strokeWidth={selected === item.id ? 3 : 1.5} markerEnd={`url(#${marker})`} />
              </g>
            </Tooltip>;
          })}
          {graph.nodes.map((item) => {
            const point = positions.get(item.id)!;
            const isRoot = item.id === root?.id;
            return <g key={item.id} role="button" tabIndex={0} aria-label={`节点 ${item.name}`} className="graph-node"
              transform={`translate(${point.x},${point.y})`} onClick={() => select(item.id)}
              onKeyDown={(event) => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); select(item.id); } }}>
              <title>{item.name} · {nodeLabels[item.kind] ?? item.kind}</title>
              <rect x="-116" y="-28" width="232" height="56" rx="12" fill={isRoot ? '#5668e8' : '#f6f8fd'} stroke={selected === item.id ? '#5668e8' : '#d9e0ee'} />
              <text textAnchor="middle" y="-2" fill={isRoot ? '#fff' : '#243047'}>{item.name.length > 26 ? item.name.slice(0, 23) + '…' : item.name}</text>
              <text textAnchor="middle" y="17" fontSize="11" fill={isRoot ? '#dfe4ff' : '#8592ab'}>{nodeLabels[item.kind] ?? item.kind}</text>
            </g>;
          })}
        </svg>
      </div>
    </Card>
    <Card title={edge ? '关系详情' : node ? '节点详情' : '关系与节点'}>
      {edge ? <><p>{names.get(edge.from_node_id)} → {names.get(edge.to_node_id)}</p><p>{edge.relation}</p><EdgeFacts edge={edge} /><small>{edge.id}</small></>
        : node ? <dl className="record-meta"><div><dt>名称</dt><dd>{node.name}</dd></div><div><dt>类型</dt><dd>{nodeLabels[node.kind] ?? node.kind}</dd></div>
          <div><dt>源系统标识</dt><dd>{node.external_id}</dd></div><div><dt>节点 ID</dt><dd>{node.id}</dd></div></dl>
          : <p className="control-description">选择图中的关系或节点，或从下方关系清单选择。</p>}
    </Card>
    <Card title="关系清单" className="graph-relations"><ul>{graph.edges.map((item) => <li key={item.id}>
      <button type="button" onClick={() => select(item.id)}>{names.get(item.from_node_id)} → {names.get(item.to_node_id)} · {item.relation}</button>
      <span>{item.source} · {(item.confidence * 100).toFixed(1)}% · 距上次观察 {age(item.freshness_seconds)}</span>
    </li>)}</ul>{!graph.edges.length && <p>暂无关系</p>}</Card>
  </div>;
}
