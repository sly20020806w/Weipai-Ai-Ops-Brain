import { Button, Tag } from 'antd';
import { ArrowRightOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { navigation } from '../navigation';

export function EntryPage({ entry }: { entry: (typeof navigation)[number] }) {
  const Icon = entry.icon;
  return <>
    <div className="page-head"><div><div className="eyebrow">个人工作台</div><h1>{entry.label}</h1><p>{entry.description}</p></div>
      <Tag variant="filled" color="geekblue">入口已就绪</Tag></div>
    {entry.path === '/dashboard' ? <>
      <section className="hero">
        <div className="hero-orbit" aria-hidden="true" />
        <div className="eyebrow">从上下文到行动</div>
        <h2>运维工作，从这里开始。</h2>
        <p>连接真实环境，理解每一次变化。<br />在同一个工作台中查看、对话、审批与接管。</p>
      </section>
      <div className="section-caption"><h2>工作入口</h2><span>让每个环节都有迹可循</span></div>
      <section className="shortcuts" aria-label="快捷入口">
        {navigation.filter((item) => ['/tasks', '/approvals', '/services', '/chat'].includes(item.path)).map(({ path, label, icon: EntryIcon, description }) =>
          <Link key={path} to={path} className="shortcut"><div className="shortcut-icon"><EntryIcon aria-hidden="true" /></div>
            <h3>{label} <ArrowRightOutlined /></h3><p>{description}</p></Link>)}
      </section>
      <div className="scope-note"><InfoCircleOutlined /><span>导航与账户功能已开放，业务内容将在后续版本开放。</span></div>
    </> : <section className="empty-panel">
      <div className="empty-icon"><Icon aria-hidden="true" /></div>
      <h2>工作入口已就绪</h2><p>此入口的业务内容将在后续版本开放。<br />你可以通过左侧导航查看其他工作入口。</p>
      <Link to="/dashboard"><Button>返回总览</Button></Link>
    </section>}
  </>;
}
