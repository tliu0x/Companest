import { useCompanies } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

export function CompaniesPage() {
  const { data, isLoading, error } = useCompanies();

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;

  const companies = data ?? [];

  return (
    <div className="p-6 space-y-6">
      <h2 className="text-2xl font-semibold">Companies</h2>

      {companies.length === 0 ? (
        <EmptyState message="No companies found" />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Domain</TableHead>
              <TableHead>Enabled</TableHead>
              <TableHead>Bindings</TableHead>
              <TableHead>CEO Enabled</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {companies.map((company) => (
              <TableRow key={company.id}>
                <TableCell className="font-mono text-xs">{company.id}</TableCell>
                <TableCell>{company.name}</TableCell>
                <TableCell>{company.domain}</TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={company.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                  >
                    {company.enabled ? 'enabled' : 'disabled'}
                  </Badge>
                </TableCell>
                <TableCell>{company.bindings_count}</TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={company.ceo_enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                  >
                    {company.ceo_enabled ? 'enabled' : 'disabled'}
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
