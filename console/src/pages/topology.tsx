import { Link } from '@tanstack/react-router';
import { useCompanies, useFleetStatus } from '@/lib/queries';
import { TOPOLOGY_ENABLED } from '@/lib/features';
import { getErrorMessage } from '@/lib/api';
import { useRealtime } from '@/lib/realtime';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function TopologyPage() {
  const companies = useCompanies();
  const fleetStatus = useFleetStatus();
  const { getOverviewCompanyActivityCount } = useRealtime();

  if (!TOPOLOGY_ENABLED) {
    return (
      <div className="p-6">
        <EmptyState message="Topology is disabled by feature flag." />
      </div>
    );
  }

  if (companies.isLoading || fleetStatus.isLoading) return <PageLoading />;
  if (companies.error) return <ErrorAlert message={getErrorMessage(companies.error)} />;
  if (fleetStatus.error) return <ErrorAlert message={getErrorMessage(fleetStatus.error)} />;

  const companyList = companies.data?.companies ?? [];
  const companyStatus = fleetStatus.data?.companies ?? {};

  if (companyList.length === 0) {
    return (
      <div className="p-6">
        <EmptyState message="No companies available for topology." />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-8">
      <div className="space-y-2">
        <h2 className="text-2xl font-semibold">Topology</h2>
        <p className="text-sm text-muted-foreground">
          Static fleet map. Use it to spot company health and jump into workspaces quickly.
        </p>
      </div>

      <div className="flex justify-center">
        <Card className="w-full max-w-md border-2 border-primary/30 bg-primary/5">
          <CardHeader className="pb-3">
            <CardTitle className="text-center">Companest Fleet</CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-center gap-6 text-sm text-muted-foreground">
            <div>
              <span className="block text-2xl font-semibold text-foreground">{companyList.length}</span>
              companies
            </div>
            <div>
              <span className="block text-2xl font-semibold text-foreground">{fleetStatus.data?.jobs.total ?? 0}</span>
              jobs
            </div>
            <div>
              <span className="block text-2xl font-semibold text-foreground">{fleetStatus.data?.teams?.active?.length ?? 0}</span>
              active teams
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="mx-auto h-10 w-px bg-border" />
      <div className="mx-auto h-px w-full max-w-5xl bg-border" />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {companyList.map((company) => {
          const activityCount = getOverviewCompanyActivityCount(company.id);
          const runtime = companyStatus[company.id];
          return (
            <Link
              key={company.id}
              to="/console/companies/$companyId"
              params={{ companyId: company.id }}
              className="block"
            >
              <Card className="h-full transition-colors hover:border-primary/40 hover:bg-accent/40">
                <CardHeader className="space-y-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <CardTitle className="text-lg">{company.name}</CardTitle>
                      <p className="mt-1 font-mono text-xs text-muted-foreground">{company.id}</p>
                    </div>
                    <Badge
                      variant="outline"
                      className={company.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                    >
                      {company.enabled ? 'enabled' : 'disabled'}
                    </Badge>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="outline">teams {runtime?.active_teams ?? company.active_team_count}</Badge>
                    <Badge variant="outline">jobs {runtime?.total_jobs ?? company.recent_job_count}</Badge>
                    {activityCount > 0 && (
                      <Badge variant="outline" className="bg-amber-100 text-amber-900">
                        {activityCount} new events
                      </Badge>
                    )}
                  </div>
                </CardHeader>
                <CardContent className="space-y-3 text-sm text-muted-foreground">
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <span className="block text-xs uppercase tracking-wide">Recent Jobs</span>
                      <span className="text-base font-semibold text-foreground">{company.recent_job_count}</span>
                    </div>
                    <div>
                      <span className="block text-xs uppercase tracking-wide">Bindings</span>
                      <span className="text-base font-semibold text-foreground">{company.bindings_count}</span>
                    </div>
                  </div>
                  <p>
                    Last activity:{' '}
                    <span className="text-foreground">
                      {company.last_activity_timestamp ? new Date(company.last_activity_timestamp).toLocaleString() : '-'}
                    </span>
                  </p>
                </CardContent>
              </Card>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
