import { useState, type FormEvent } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { setToken } from '@/lib/api';

export function LoginPage() {
  const [token, setTokenValue] = useState('');
  const [error, setError] = useState('');
  const navigate = useNavigate();

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');

    if (!token.trim()) {
      setError('Token is required');
      return;
    }

    // Test the token against the health-adjacent endpoint
    try {
      const res = await fetch('/api/fleet/status', {
        headers: { Authorization: `Bearer ${token.trim()}` },
      });
      if (res.status === 401) {
        setError('Invalid token');
        return;
      }
      setToken(token.trim());
      navigate({ to: '/console' });
    } catch {
      setError('Cannot connect to backend');
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-muted/30">
      <Card className="w-[400px]">
        <CardHeader>
          <CardTitle>Companest Console</CardTitle>
          <CardDescription>Enter your API token to continue</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="token">API Token</Label>
              <Input
                id="token"
                type="password"
                value={token}
                onChange={(e) => setTokenValue(e.target.value)}
                placeholder="Enter COMPANEST_API_TOKEN"
              />
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="w-full">Login</Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
