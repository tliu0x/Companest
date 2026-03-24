import { useFinanceSummary, useFinanceReport } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { AlertCircle } from 'lucide-react';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

function formatDollars(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

export function FinancePage() {
  const summary = useFinanceSummary();
  const report = useFinanceReport(24);

  if (summary.isLoading || report.isLoading) return <PageLoading />;
  if (summary.error) return <ErrorAlert message={summary.error.message} />;
  if (report.error) return <ErrorAlert message={report.error.message} />;

  const s = summary.data;
  const r = report.data;
  const tripped = s?.circuit_breaker?.tripped ?? false;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Finance</h2>
        <JsonDrawer title="Finance Data" data={{ summary: s, report: r }} />
      </div>

      {s && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Total Spent</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{formatDollars(s.total_spent)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Budget Remaining</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">
                {s.budget_remaining != null ? formatDollars(s.budget_remaining) : '-'}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Daily Limit</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">
                {s.daily_limit != null ? formatDollars(s.daily_limit) : '-'}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Circuit Breaker</CardTitle>
            </CardHeader>
            <CardContent>
              <Badge
                variant="outline"
                className={tripped ? 'bg-red-100 text-red-800' : 'bg-green-100 text-green-800'}
              >
                {tripped ? 'TRIPPED' : 'OK'}
              </Badge>
            </CardContent>
          </Card>
        </div>
      )}

      {tripped && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Circuit Breaker Tripped</AlertTitle>
          <AlertDescription>
            The circuit breaker has been tripped ({s?.circuit_breaker?.trip_count ?? 0} times). Spending is halted.
          </AlertDescription>
        </Alert>
      )}

      <div>
        <h3 className="text-lg font-medium mb-3">Finance Report (24h)</h3>
        {!r?.entries || r.entries.length === 0 ? (
          <EmptyState message="No report entries available" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                {Object.keys(r.entries[0]).map((key) => (
                  <TableHead key={key}>{key}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {r.entries.map((entry, i) => (
                <TableRow key={i}>
                  {Object.values(entry).map((val, j) => (
                    <TableCell key={j} className="text-xs">
                      {typeof val === 'object' ? JSON.stringify(val) : String(val ?? '-')}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
