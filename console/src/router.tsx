import { createRouter, createRoute, createRootRoute } from '@tanstack/react-router';
import { AppLayout } from '@/components/layout/app-layout';
import { LoginPage } from '@/pages/login';
import { OverviewPage } from '@/pages/overview';
import { CompaniesPage } from '@/pages/companies';
import { JobsPage } from '@/pages/jobs';
import { TeamsPage } from '@/pages/teams';
import { SchedulesPage } from '@/pages/schedules';
import { FinancePage } from '@/pages/finance';
import { BindingsPage } from '@/pages/bindings';
import { JobDetailPage } from '@/pages/job-detail';
import { CompanyCreatePage } from '@/pages/company-create';
import { CompanyDetailPage } from '@/pages/company-detail';
import { EventsPage } from '@/pages/events';
import { TopologyPage } from '@/pages/topology';

const rootRoute = createRootRoute();

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/console/login',
  component: LoginPage,
});

const layoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'layout',
  component: AppLayout,
});

const overviewRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/',
  component: OverviewPage,
});

const companiesRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/companies',
  component: CompaniesPage,
});

const jobsRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/jobs',
  component: JobsPage,
});

const jobDetailRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/jobs/$jobId',
  component: JobDetailPage,
});

const teamsRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/teams',
  component: TeamsPage,
});

const schedulesRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/schedules',
  component: SchedulesPage,
});

const financeRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/finance',
  component: FinancePage,
});

const bindingsRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/bindings',
  component: BindingsPage,
});

const eventsRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/events',
  component: EventsPage,
});

const topologyRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/topology',
  component: TopologyPage,
});

const companyCreateRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/companies/create',
  component: CompanyCreatePage,
});

const companyDetailRoute = createRoute({
  getParentRoute: () => layoutRoute,
  path: '/console/companies/$companyId',
  component: CompanyDetailPage,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  layoutRoute.addChildren([
    overviewRoute,
    companiesRoute,
    companyCreateRoute,
    companyDetailRoute,
    jobsRoute,
    jobDetailRoute,
    teamsRoute,
    schedulesRoute,
    financeRoute,
    bindingsRoute,
    eventsRoute,
    topologyRoute,
  ]),
]);

export const router = createRouter({ routeTree, basepath: '/' });

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}
