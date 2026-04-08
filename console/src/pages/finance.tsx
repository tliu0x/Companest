import { useState } from 'react';
import { useFinanceSummary, useFinanceReport } from '@/lib/queries';
import { useResetCircuitBreaker, useResolveApproval } from '@/lib/mutations';
import { getErrorMessage, getResponseNote } from '@/lib/api';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
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

function formatDollars(dollars: number): string {
  return `$${dollars.toFixed(2)}`;
}

export function FinancePage() {
  const summary = useFinanceSummary();
  const report = useFinanceReport(24);
  const resetCb = useResetCircuitBreaker();
  const resolveApproval = useResolveApproval();
  const [resetMsg, setResetMsg] = useState<string | null>(null);
  const [approvalMsg, setApprovalMsg] = useState<string | null>(null);

  if (summary.isLoading || report.isLoading) return <PageLoading />;
  if (summary.error) return <ErrorAlert message={getErrorMessage(summary.error)} />;
  if (report.error) return <ErrorAlert message={getErrorMessage(report.error)} />;

  const s = summary.data;
  const r = report.data;
  const note = getResponseNote(s) ?? getResponseNote(r);
  const tripped = s?.circuit_breaker?.tripped ?? false;
  const pendingApprovals = s?.pending_approvals ?? [];

  if (note && !s?.total && !r?.window_spend) {
    return (
      <div className="p-6">
        <EmptyState message={note} />
      </div>
    );
  }

  function handleResetCircuitBreaker() {
    if (!window.confirm('Reset the circuit breaker? This will re-enable spending.')) return;
    setResetMsg(null);
    resetCb.mutate(undefined, {
      onSuccess: () => setResetMsg('Circuit breaker reset.'),
      onError: (err) => setResetMsg(`Failed to reset: ${getErrorMessage(err)}`),
    });
  }

  function handleApproval(approvalId: string, choice: 'approve' | 'downgrade' | 'reject') {
    setApprovalMsg(null);
    resolveApproval.mutate(
      { approvalId, choice },
      {
        onSuccess: () => setApprovalMsg(`Approval ${approvalId} resolved as ${choice}.`),
        onError: (err) => setApprovalMsg(`Approval failed: ${getErrorMessage(err)}`),
      },
    );
  }

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
              <p className="text-2xl font-bold">{formatDollars(s.total)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Today</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{formatDollars(s.today)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">Daily Limit</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">
                {s.budget?.daily_limit != null ? formatDollars(s.budget.daily_limit) : '-'}
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
          <AlertDescription className="flex items-center justify-between">
            <span>
              The circuit breaker has been tripped ({s?.circuit_breaker?.events_in_window ?? 0} events in window). Spending is halted.
            </span>
            <Button
              variant="outline"
              size="sm"
              className="ml-4 shrink-0"
              disabled={resetCb.isPending}
              onClick={handleResetCircuitBreaker}
            >
              {resetCb.isPending ? 'Resetting...' : 'Reset Circuit Breaker'}
            </Button>
          </AlertDescription>
          {resetMsg && (
            <p className={`mt-2 text-sm ${resetCb.isError ? 'text-red-300' : 'text-green-300'}`}>
              {resetMsg}
            </p>
          )}
        </Alert>
      )}

      <div>
        <h3 className="text-lg font-medium mb-3">Pending Approvals</h3>
        {approvalMsg && (
          <p className={`mb-3 text-sm ${approvalMsg.includes('failed') ? 'text-destructive' : 'text-green-600'}`}>
            {approvalMsg}
          </p>
        )}
        {pendingApprovals.length === 0 ? (
          <EmptyState message="No pending approvals" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Team</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Estimate</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pendingApprovals.map((approval) => (
                <TableRow key={approval.approval_id}>
                  <TableCell className="font-mono text-xs">{approval.approval_id}</TableCell>
                  <TableCell>{approval.target_team}</TableCell>
                  <TableCell>{approval.target_model}</TableCell>
                  <TableCell>{formatDollars(approval.estimated_cost_usd)}</TableCell>
                  <TableCell>{new Date(approval.created_at).toLocaleString()}</TableCell>
                  <TableCell className="max-w-[340px] truncate" title={approval.task}>
                    {approval.task}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={resolveApproval.isPending}
                        onClick={() => handleApproval(approval.approval_id, 'approve')}
                      >
                        Approve
                      </Button>
                      {approval.suggested_downgrade && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={resolveApproval.isPending}
                          onClick={() => handleApproval(approval.approval_id, 'downgrade')}
                        >
                          Downgrade
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={resolveApproval.isPending}
                        onClick={() => handleApproval(approval.approval_id, 'reject')}
                      >
                        Reject
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <div>
        <h3 className="text-lg font-medium mb-3">Finance Report (24h)</h3>
        {(() => {
          const byTeamEntries = Object.entries(r?.by_team ?? {});
          const utilizationEntries = Object.entries(r?.team_utilization ?? {});
          if (byTeamEntries.length === 0 && utilizationEntries.length === 0) {
            return <EmptyState message="No report data available" />;
          }
          return (
            <div className="space-y-6">
              {byTeamEntries.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium mb-2">Spend by Team</h4>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Team</TableHead>
                        <TableHead>Spent</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {byTeamEntries.map(([teamId, spent]) => (
                        <TableRow key={teamId}>
                          <TableCell className="font-mono text-xs">{teamId}</TableCell>
                          <TableCell>{formatDollars(spent)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}

              {utilizationEntries.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium mb-2">Team Utilization</h4>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Team</TableHead>
                        <TableHead>Spent</TableHead>
                        <TableHead>Budget</TableHead>
                        <TableHead>Utilization</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {utilizationEntries.map(([teamId, info]) => (
                        <TableRow key={teamId}>
                          <TableCell className="font-mono text-xs">{teamId}</TableCell>
                          <TableCell>{formatDollars(info.spent)}</TableCell>
                          <TableCell>{formatDollars(info.budget)}</TableCell>
                          <TableCell>{info.utilization_pct.toFixed(1)}%</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </div>
          );
        })()}
      </div>
    </div>
  );
}
