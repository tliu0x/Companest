import { useTeams } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

export function TeamsPage() {
  const { data, isLoading, error } = useTeams();

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;

  const teamEntries = data ? Object.entries(data.configs) : [];
  const activeSet = new Set(data?.active ?? []);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Teams ({teamEntries.length})</h2>
        {data && <JsonDrawer title="Teams Response" data={data} />}
      </div>

      {teamEntries.length === 0 ? (
        <EmptyState message="No teams found" />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Lead Pi</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead>Always On</TableHead>
              <TableHead>Active</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {teamEntries.map(([id, config]) => (
              <TableRow key={id}>
                <TableCell className="font-mono text-xs">{id}</TableCell>
                <TableCell>{config.role}</TableCell>
                <TableCell>{config.lead_pi}</TableCell>
                <TableCell>{config.mode}</TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={config.always_on ? 'bg-blue-100 text-blue-800' : 'bg-gray-100 text-gray-800'}
                  >
                    {config.always_on ? 'yes' : 'no'}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={activeSet.has(id) ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                  >
                    {activeSet.has(id) ? 'active' : 'inactive'}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
