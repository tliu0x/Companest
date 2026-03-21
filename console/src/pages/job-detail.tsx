import { useParams } from '@tanstack/react-router';
import { useJob } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { StatusBadge } from '@/components/shared/status-badge';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { ArrowLeft } from 'lucide-react';

export function JobDetailPage() {
  const { jobId } = useParams({ from: '/layout/console/jobs/$jobId' as const });
  const { data: job, isLoading, error } = useJob(jobId);

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;
  if (!job) return <ErrorAlert message="Job not found" />;

  return (
    <div className="p-6 space-y-6">
      {/* Back button */}
      <a href="/console/jobs">
        <Button variant="ghost" size="sm">
          <ArrowLeft className="h-4 w-4 mr-1" />
          Back to Jobs
        </Button>
      </a>

      {/* Title and status */}
      <div className="flex items-center gap-3">
        <h2 className="text-2xl font-semibold font-mono">{job.id}</h2>
        <StatusBadge status={job.status} />
        <JsonDrawer title="Job JSON" data={job} />
      </div>

      {/* Metadata */}
      {job.company_id && (
        <p className="text-sm text-muted-foreground">Company: {job.company_id}</p>
      )}

      {/* Timestamps */}
      <Card>
        <CardHeader>
          <CardTitle>Timestamps</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
            <div>
              <dt className="text-muted-foreground">Created</dt>
              <dd>{new Date(job.created_at).toLocaleString()}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Started</dt>
              <dd>{job.started_at ? new Date(job.started_at).toLocaleString() : '-'}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Completed</dt>
              <dd>{job.completed_at ? new Date(job.completed_at).toLocaleString() : '-'}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Task */}
      <Card>
        <CardHeader>
          <CardTitle>Task</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm whitespace-pre-wrap">{job.task}</p>
        </CardContent>
      </Card>

      {/* Result */}
      {job.result && (
        <Card>
          <CardHeader>
            <CardTitle>Result</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm whitespace-pre-wrap">{job.result}</p>
          </CardContent>
        </Card>
      )}

      {/* Error */}
      {job.error && (
        <Card>
          <CardHeader>
            <CardTitle className="text-destructive">Error</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm whitespace-pre-wrap text-destructive">{job.error}</p>
          </CardContent>
        </Card>
      )}

      {/* Context */}
      {job.context && Object.keys(job.context).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Context</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
              {JSON.stringify(job.context, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}

      {/* Subtasks */}
      {job.subtasks && job.subtasks.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Subtasks</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
              {JSON.stringify(job.subtasks, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
