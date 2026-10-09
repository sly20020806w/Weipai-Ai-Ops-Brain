import { Alert, Button, Form, Input } from 'antd';
import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';
import type { LoginApiAuthLoginPostData } from '../api/generated/types.gen';
import { safeReturnPath } from '../navigation';
import { Brand } from '../shell/brand';
import { signIn } from './api';
import { sessionKey } from './use-session';

type LoginValues = LoginApiAuthLoginPostData['body'];

export function LoginPage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [form] = Form.useForm<LoginValues>();
  const mutation = useMutation({
    mutationFn: signIn,
    onSuccess: async (session) => {
      // 取消旧身份查询，避免迟到的 401 覆盖新登录会话。
      await client.cancelQueries();
      client.removeQueries({ predicate: (entry) => entry.queryKey[0] !== 'session' });
      client.getMutationCache().clear();
      client.setQueryData(sessionKey, session);
      form.resetFields();
      const state = location.state as { from?: unknown } | null;
      navigate(safeReturnPath(state?.from), { replace: true });
    },
    onError: () => form.setFieldValue('password', ''),
  });

  return (
    <div className="login-page">
      <section className="login-story" aria-label="微派 AI 运维">
        <Brand />
        <div className="login-copy">
          <div className="eyebrow">WEIPAI · AI OPS BRAIN</div>
          <div className="login-copy-line" />
          <h1>连接环境，<br />让运维更从容。</h1>
          <p>从每一个事件开始，理解上下文、追寻证据，让经验成为下一次行动的依据。</p>
        </div>
        <div className="hero-orbit" aria-hidden="true" />
        <div className="login-bottom">你的个人运维工作台</div>
      </section>
      <main className="login-form-wrap">
        <div className="login-form">
          <div className="eyebrow">个人控制台</div>
          <h2>欢迎回来</h2>
          <p>登录账户，继续你的运维工作。</p>
          {mutation.isError && <Alert type="error" showIcon role="alert"
            title={mutation.error instanceof ApiError ? mutation.error.message : '暂时无法连接服务，请稍后重试。'} />}
          <Form form={form} layout="vertical" onFinish={(values) => mutation.mutate(values)} requiredMark={false}>
            <Form.Item name="username" label="账户" rules={[{ required: true, message: '请输入账户。' }]}>
              <Input prefix={<UserOutlined />} placeholder="输入账户" autoComplete="username" maxLength={200} size="large" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码。' }]}>
              <Input.Password prefix={<LockOutlined />} placeholder="输入密码" autoComplete="current-password" maxLength={256} size="large" />
            </Form.Item>
            <Button type="primary" htmlType="submit" size="large" block aria-label="登录" loading={mutation.isPending}>登录</Button>
          </Form>
          <div className="login-note">仅供本人使用 · 所有操作均可追溯</div>
        </div>
      </main>
    </div>
  );
}
