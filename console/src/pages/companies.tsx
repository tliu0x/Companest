import { useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useCompanies } from '@/lib/queries';
import { useToggleCompany, useDeleteCompany } from '@/lib/mutations';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

function DeleteConfirm({ companyId, onClose }: { companyId: string; onClose: () => void }) {
  const [confirmText, setConfirmText] = useState('');
  const deleteCompany = useDeleteCompany();

  function handleDelete() {
    deleteCompany.mutate(companyId, { onSuccess: onClose });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-background border rounded-lg p-6 space-y-4 max-w-sm w-full mx-4">
        <h3 className="text-lg font-semibold">Delete Company</h3>
        <p className="text-sm text-muted-foreground">
          Type <span className="font-mono font-bold">{companyId}</span> to confirm deletion.
        </p>
        {deleteCompany.error && (
          <ErrorAlert message={(deleteCompany.error as Error).message} />
        )}
        <input
          className="w-full border rounded px-3 py-2 text-sm"
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          placeholder={companyId}
        />
        <div className="flex gap-2 justify-end">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button
            variant="destructive"
            disabled={confirmText !== companyId || deleteCompany.isPending}
            onClick={handleDelete}
          >
            {deleteCompany.isPending ? 'Deleting...' : 'Delete'}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function CompaniesPage() {
  const { data, isLoading, error } = useCompanies();
  const toggleCompany = useToggleCompany();
  const [deletingId, setDeletingId] = useState<string | null>(null);

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;

  const companies = data?.companies ?? [];

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Companies</h2>
        <Link to="/console/companies/create">
          <Button>Create Company</Button>
        </Link>
      </div>

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
              <TableHead>Actions</TableHead>
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
                <TableCell>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={toggleCompany.isPending}
                      onClick={() =>
                        toggleCompany.mutate({
                          id: company.id as string,
                          enabled: !company.enabled,
                        })
                      }
                    >
                      {company.enabled ? 'Disable' : 'Enable'}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      onClick={() => setDeletingId(company.id as string)}
                    >
                      Delete
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {deletingId && (
        <DeleteConfirm companyId={deletingId} onClose={() => setDeletingId(null)} />
      )}
    </div>
  );
}
