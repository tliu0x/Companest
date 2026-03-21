import { useState } from 'react';
import { useJobs } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

const PAGE_SIZE = 20;
const STATUS_FILTERS = ['all', 'pending', 'running', 'completed', 'failed'] as const;

export function JobsPage() {
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [offset, setOffset] = useState(0);

  const { data, isLoading, error } = useJobs({
    status: statusFilter === 'all' ? undefined : statusFilter,
    limit: PAGE_SIZE,
    offset,
  });

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;
  if (!data) return <ErrorAlert message="No jobs data available" />;

  const { jobs, total } = data;
  const hasNext = offset + PAGE_SIZE < total;
  const hasPrev = offset > 0;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">
          Jobs <span className="text-muted-foreground text-lg font-normal">({total})</span>
        </h2>
      </div>

      {/* Status filter */}
      <div className="flex gap-2">
        {STATUS_FILTERS.map((status) => (
          <Button
            key={status}
            variant={statusFilter === status ? 'default' : 'outline'}
            size="sm"
            onClick={() => {
              setStatusFilter(status);
              setOffset(0);
            }}
          >
            {status.charAt(0).toUpperCase() + status.slice(1)}
          </Button>
        ))}
      </div>

      {jobs.length === 0 ? (
        <EmptyState message="No jobs found" />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Company</TableHead>
                <TableHead>Created At</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((job) => (
                <TableRow key={job.id}>
                  <TableCell className="font-mono text-xs">
                    <a href={`/console/jobs/${job.id}`} className="text-primary hover:underline">
                      {job.id.slice(0, 8)}
                    </a>
                  </TableCell>
                  <TableCell className="max-w-md truncate" title={job.task}>
                    {job.task.length > 60 ? job.task.slice(0, 60) + '...' : job.task}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={job.status} />
                  </TableCell>
                  <TableCell>{job.company_id ?? '-'}</TableCell>
                  <TableCell>{new Date(job.created_at).toLocaleString()}</TableCell>
                  <TableCell>
                    <a href={`/console/jobs/${job.id}`} className="text-primary hover:underline text-sm">
                      View
                    </a>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {/* Pagination */}
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              Showing {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of {total}
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!hasPrev}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!hasNext}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
