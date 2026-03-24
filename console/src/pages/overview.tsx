import { Link } from '@tanstack/react-router';
import { useFleetStatus } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { StatusBadge } from '@/components/shared/status-badge';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

export function OverviewPage() {
  const { data, isLoading, error } = useFleetStatus();

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;
  if (!data) return <ErrorAlert message="No fleet data available" />;

  const companiesEntries = Object.entries(data.companies ?? {});
  const jobStatusEntries = Object.entries(data.jobs).filter(
    ([key]) => key !== 'total' && key !== 'queue_size'
  );

  return (
    <div className="p-6 space-y-6">
      <h2 className="text-2xl font-semibold">Overview</h2>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Total Companies</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold">{Object.keys(data.companies ?? {}).length}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Active Teams</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold">{data.teams?.active?.length ?? 0}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Total Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold">{data.jobs.total}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Running Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold">{data.jobs.running}</p>
          </CardContent>
        </Card>
      </div>

      {/* Jobs by status */}
      <Card>
        <CardHeader>
          <CardTitle>Jobs by Status</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-3">
            {jobStatusEntries.map(([status, count]) => (
              <div key={status} className="flex items-center gap-2">
                <StatusBadge status={status} />
                <span className="text-sm font-medium">{count}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Companies table */}
      <Card>
        <CardHeader>
          <CardTitle>Companies</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Enabled</TableHead>
                <TableHead>Active Teams</TableHead>
                <TableHead>Total Jobs</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {companiesEntries.map(([id, company]) => (
                <TableRow key={id}>
                  <TableCell>
                    <Link to="/console/companies" className="text-primary hover:underline">
                      {company.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={company.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                    >
                      {company.enabled ? 'enabled' : 'disabled'}
                    </Badge>
                  </TableCell>
                  <TableCell>{company.active_teams}</TableCell>
                  <TableCell>{company.total_jobs}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
