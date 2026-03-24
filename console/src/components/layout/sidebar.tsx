import { Link, useRouterState } from '@tanstack/react-router';
import { LayoutDashboard, Building2, ListTodo, Users, Clock, DollarSign, Link2 } from 'lucide-react';
import { cn } from '@/lib/utils';

const navItems = [
  { to: '/console/', label: 'Overview', icon: LayoutDashboard },
  { to: '/console/companies', label: 'Companies', icon: Building2 },
  { to: '/console/jobs', label: 'Jobs', icon: ListTodo },
  { to: '/console/teams', label: 'Teams', icon: Users },
  { to: '/console/schedules', label: 'Schedules', icon: Clock },
  { to: '/console/finance', label: 'Finance', icon: DollarSign },
  { to: '/console/bindings', label: 'Bindings', icon: Link2 },
];

export function Sidebar() {
  const router = useRouterState();
  const currentPath = router.location.pathname;

  return (
    <aside className="w-56 border-r border-border bg-muted/30 min-h-screen p-4">
      <div className="mb-6">
        <h1 className="text-lg font-semibold">Companest</h1>
        <p className="text-xs text-muted-foreground">Console</p>
      </div>
      <nav className="space-y-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentPath === item.to || (item.to !== '/console/' && currentPath.startsWith(item.to));
          return (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                'flex items-center gap-2 px-3 py-2 text-sm rounded-md transition-colors',
                isActive
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
              )}
            >
              <Icon className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
