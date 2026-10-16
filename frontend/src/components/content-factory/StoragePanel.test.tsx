/**
 * OneDrive on the Storage screen.
 *
 * The sign-in is a small state machine, not a field, and the two things that
 * break it are invisible in a screenshot: polling that never stops, and a
 * sign-in started against a client id that is typed but not yet saved (the
 * backend reads the stored one, so it would fail with a confusing message).
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { StoragePanel } from './StoragePanel';
import type { CFStorageConfig } from './types';

const config = (overrides: Partial<CFStorageConfig> = {}): CFStorageConfig => ({
  backend: 'onedrive',
  nextcloud: { url: '', username: '', root: '', publicUrl: '', verifyTls: true, shareDays: 7 },
  s3: { bucket: '', endpoint: '', region: '', publicUrl: '' },
  onedrive: { clientId: 'app-id', tenant: 'consumers', root: '' },
  local: { root: '' },
  secrets: { onedriveRefreshToken: 'none' },
  envBackend: 'local',
  backends: ['env', 'local', 'nextcloud', 's3', 'onedrive'],
  requireRemote: false,
  ...overrides,
});

const noop = async () => {};
const base = {
  usage: null,
  language: 'ru' as const,
  onSave: noop,
  onTest: async () => ({}),
  onClearSecret: noop,
};

describe('StoragePanel — OneDrive', () => {
  it('offers OneDrive as a storage backend', () => {
    render(<StoragePanel {...base} config={config({ backend: 'local' })}
      onOneDriveLogin={async () => ({})} onOneDrivePoll={async () => ({})} onOneDriveSignOut={noop} />);
    expect(screen.getByRole('button', { name: 'OneDrive' })).toBeInTheDocument();
  });

  it('shows the code and the URL the owner has to open', async () => {
    const onOneDriveLogin = vi.fn(async () => ({
      userCode: 'ABCD-EFGH', verificationUri: 'https://microsoft.com/devicelogin',
    }));
    render(<StoragePanel {...base} config={config()}
      onOneDriveLogin={onOneDriveLogin} onOneDrivePoll={async () => ({ status: 'pending' })}
      onOneDriveSignOut={noop} />);

    fireEvent.click(screen.getByRole('button', { name: 'Войти' }));
    await waitFor(() => expect(screen.getByText('ABCD-EFGH')).toBeInTheDocument());
    expect(screen.getByRole('link', { name: 'https://microsoft.com/devicelogin' })).toBeInTheDocument();
  });

  it('reports a signed-in account and offers to sign out', () => {
    render(<StoragePanel {...base} config={config({ secrets: { onedriveRefreshToken: 'stored' } })}
      onOneDriveLogin={async () => ({})} onOneDrivePoll={async () => ({})} onOneDriveSignOut={noop} />);
    expect(screen.getByText('выполнен')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Выйти' })).toBeInTheDocument();
  });

  it('surfaces a failed sign-in instead of polling forever', async () => {
    render(<StoragePanel {...base} config={config()}
      onOneDriveLogin={async () => { throw new Error('Microsoft отклонил Client ID'); }}
      onOneDrivePoll={async () => ({})} onOneDriveSignOut={noop} />);

    fireEvent.click(screen.getByRole('button', { name: 'Войти' }));
    await waitFor(() => expect(screen.getByText('Microsoft отклонил Client ID')).toBeInTheDocument());
  });

  it('warns that an unsaved client id is not what the sign-in will use', async () => {
    render(<StoragePanel {...base} config={config()}
      onOneDriveLogin={async () => ({})} onOneDrivePoll={async () => ({})} onOneDriveSignOut={noop} />);

    fireEvent.change(screen.getByDisplayValue('app-id'), { target: { value: 'other-id' } });
    await waitFor(() => expect(screen.getByText(/Сначала сохраните Client ID/)).toBeInTheDocument());
  });
});
