import { useSchedules, useSchedulerStatus } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

export function SchedulesPage() {
  const schedules = useSchedules();
  const scheduler = useSchedulerStatus();

  if (schedules.isLoading || scheduler.isLoading) return <PageLoading />;
  if (schedules.error) return <ErrorAlert message={schedules.error.message} />;
  if (scheduler.error) return <ErrorAlert message={scheduler.error.message} />;

  const jobs = schedules.data ?? [];
  const tasks = scheduler.data?.tasks ?? [];

  return (
    <div className="p-6 space-y-6">
      <h2 className="text-2xl font-semibold">Schedules</h2>

      <div>
        <h3 className="text-lg font-medium mb-3">User Schedules</h3>
        {jobs.length === 0 ? (
          <EmptyState message="No user schedules found" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Trigger Type</TableHead>
                <TableHead>Team</TableHead>
                <TableHead>Fire Count</TableHead>
                <TableHead>Last Fired</TableHead>
                <TableHead>Active</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((job) => (
                <TableRow key={job.id}>
                  <TableCell className="font-mono text-xs">{job.id}</TableCell>
                  <TableCell className="max-w-[200px] truncate" title={job.task}>
                    {job.task}
                  </TableCell>
                  <TableCell>{job.description}</TableCell>
                  <TableCell>{job.trigger_type}</TableCell>
                  <TableCell>{job.team_id ?? '-'}</TableCell>
                  <TableCell>{job.fire_count}</TableCell>
                  <TableCell className="text-xs">{job.last_fired ?? '-'}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={job.active ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                    >
                      {job.active ? 'active' : 'inactive'}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <Separator />

      <div>
        <h3 className="text-lg font-medium mb-3">System Scheduler Tasks</h3>
        {tasks.length === 0 ? (
          <EmptyState message="No system scheduler tasks found" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Interval</TableHead>
                <TableHead>Last Run</TableHead>
                <TableHead>Next Run</TableHead>
                <TableHead>Enabled</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tasks.map((task) => (
                <TableRow key={task.name}>
                  <TableCell className="font-mono text-xs">{task.name}</TableCell>
                  <TableCell>{task.interval}</TableCell>
                  <TableCell className="text-xs">{task.last_run ?? '-'}</TableCell>
                  <TableCell className="text-xs">{task.next_run ?? '-'}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={task.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                    >
                      {task.enabled ? 'enabled' : 'disabled'}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
