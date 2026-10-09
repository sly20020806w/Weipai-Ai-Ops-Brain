import { useState } from 'react';
import { Alert, Avatar, Button, Drawer } from 'antd';
import { LogoutOutlined, MenuOutlined, UserOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { ApiError } from '../api/client';
import type { SessionResponse } from '../api/generated/types.gen';
import { signOut } from '../auth/api';
import { sessionKey } from '../auth/use-session';
import { navigation } from '../navigation';
import { Brand } from './brand';

function Navigation({ onSelect }: { onSelect?: () => void }) {
  return <aside className="rail">
    <Brand />
    <div className="rail-caption">个人控制台</div>
    <nav aria-label="主导航">
      {navigation.map(({ path, label, icon: Icon }) =>
        <NavLink key={path} to={path} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`} onClick={onSelect}>
          <Icon aria-hidden="true" /><span>{label}</span>
        </NavLink>)}
    </nav>
    <div className="rail-foot">理解环境 · 以证据为依据</div>
  </aside>;
}

export function ConsoleShell({ session }: { session: SessionResponse }) {
  const client = useQueryClient();
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  const current = navigation.find((entry) => entry.path === location.pathname || location.pathname.startsWith(entry.path + '/'));
  const mutation = useMutation({
    mutationFn: () => signOut(session),
    onSuccess: async () => {
      await client.cancelQueries();
      client.removeQueries({ predicate: (entry) => entry.queryKey[0] !== 'session' });
      client.getMutationCache().clear();
      client.setQueryData(sessionKey, null);
    },
  });

  return <div className="console">
    <div className="rail-desktop"><Navigation /></div>
    <Drawer open={menuOpen} onClose={() => setMenuOpen(false)} placement="left" size={260}
      className="mobile-navigation" title="导航" styles={{ header: { color: '#d3daf2' } }}>
      <Navigation onSelect={() => setMenuOpen(false)} />
    </Drawer>
    <div className="workspace">
      <header className="topbar">
        <div className="topbar-path">
          <Button className="mobile-toggle" type="text" icon={<MenuOutlined />} aria-label="打开导航" onClick={() => setMenuOpen(true)} />
          <span>个人控制台 /</span><b>{current?.label ?? '页面未找到'}</b>
        </div>
        <div className="topbar-user">
          <span className="session-label"><i className="session-dot" />会话已验证</span>
          <Avatar size={30} icon={<UserOutlined />} style={{ background: '#edf0fa', color: '#7283b0' }} />
          <span className="actor" title={session.actor}>{session.actor}</span>
          <Button type="text" icon={<LogoutOutlined />} loading={mutation.isPending} onClick={() => mutation.mutate()} aria-label="退出登录">退出</Button>
        </div>
      </header>
      <main className="content">
        {mutation.isError && <Alert type="error" role="alert" showIcon style={{ marginBottom: 24 }}
          title={mutation.error instanceof ApiError ? mutation.error.message : '退出失败，请重试。'} />}
        <Outlet />
        <footer className="footer"><span>微派 AI 运维 · 个人工作台</span><span>调查有证据，行动有授权，结果有验证。</span></footer>
      </main>
    </div>
  </div>;
}
