import { Outlet, useNavigate } from '@tanstack/react-router';
import { useEffect } from 'react';
import { getToken } from '@/lib/api';
import { Sidebar } from './sidebar';

export function AppLayout() {
  const navigate = useNavigate();

  useEffect(() => {
    if (!getToken()) {
      navigate({ to: '/console/login' });
    }
  }, [navigate]);

  if (!getToken()) {
    return null;
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}
