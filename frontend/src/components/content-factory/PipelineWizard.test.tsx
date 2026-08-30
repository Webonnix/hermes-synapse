import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PipelineWizard } from './PipelineWizard';

describe('PipelineWizard', () => {
  it('collects production settings and submits one complete request', async () => {
    const onCreate = vi.fn(async () => undefined);
    const template = {
      id: 'ai_news_shorts', name: 'AI NEWS SHORTS', description: 'Daily sourced short', defaults: {},
    };
    render(<PipelineWizard
      language="en"
      templates={[template]}
      channels={[]}
      providers={[{
        id: 'local-llm', category: 'LLM', model: 'qwen', free: true,
        asyncJob: false, concurrency: 4, isDefault: false,
      }]}
      settings={null}
      busy={false}
      onCreate={onCreate}
    />);

    fireEvent.click(screen.getByRole('button', { name: /AI NEWS SHORTS/i }));
    fireEvent.change(screen.getByLabelText('New pipeline name'), { target: { value: 'Daily Robotics' } });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));

    fireEvent.change(screen.getByDisplayValue('artificial intelligence'), { target: { value: 'robotics' } });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));

    fireEvent.click(screen.getByText('local-llm'));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledOnce());
    expect(onCreate).toHaveBeenCalledWith(template, expect.objectContaining({
      name: 'Daily Robotics',
      templateId: 'ai_news_shorts',
      activate: false,
      options: expect.objectContaining({
        niche: 'robotics',
        fallbackEnabled: true,
        llmFallbackProviders: ['local-llm'],
        publishingMode: 'REVIEW_REQUIRED',
        maxCostPerRun: 2,
      }),
    }));
  });

  it('blocks immediate activation without a connected publishing account', () => {
    render(<PipelineWizard
      language="en"
      templates={[{ id: 'ai_news_shorts', name: 'AI NEWS SHORTS', description: 'Short', defaults: {} }]}
      channels={[]}
      providers={[]}
      settings={null}
      busy={false}
      onCreate={vi.fn(async () => undefined)}
    />);

    fireEvent.click(screen.getByRole('button', { name: /AI NEWS SHORTS/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByText('Activate immediately'));

    expect(screen.getByText('Choose a publishing account before activation')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create & activate' })).toBeDisabled();
  });
});
