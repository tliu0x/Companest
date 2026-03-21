import { useState } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { useCreateCompany } from '@/lib/mutations';
import { ErrorAlert } from '@/components/shared/error-alert';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';

export function CompanyCreatePage() {
  const navigate = useNavigate();
  const createCompany = useCreateCompany();

  const [id, setId] = useState('');
  const [name, setName] = useState('');
  const [domain, setDomain] = useState('');
  const [enabled, setEnabled] = useState(true);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const data: Record<string, unknown> = { id, name, enabled };
    if (domain) data.domain = domain;

    createCompany.mutate(data, {
      onSuccess: () => {
        navigate({ to: '/console/companies' });
      },
    });
  }

  return (
    <div className="p-6 max-w-lg">
      <Card>
        <CardHeader>
          <CardTitle>Create Company</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {createCompany.error && (
              <ErrorAlert message={(createCompany.error as Error).message} />
            )}

            <div className="space-y-2">
              <Label htmlFor="id">ID</Label>
              <Input
                id="id"
                value={id}
                onChange={(e) => setId(e.target.value)}
                required
                placeholder="company-id"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                placeholder="Company Name"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="domain">Domain (optional)</Label>
              <Input
                id="domain"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                placeholder="example.com"
              />
            </div>

            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="enabled"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="h-4 w-4"
              />
              <Label htmlFor="enabled">Enabled</Label>
            </div>

            <div className="flex gap-2">
              <Button type="submit" disabled={createCompany.isPending}>
                {createCompany.isPending ? 'Creating...' : 'Create'}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => navigate({ to: '/console/companies' })}
              >
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
