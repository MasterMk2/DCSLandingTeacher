import { useMemo } from 'react';
import { SourceInfo } from '../types/api';

interface SourceSelectorProps {
  sources: SourceInfo[];
  currentSource: string | null;
  onChange: (sourceId: string | null) => void;
}

export const SourceSelector: React.FC<SourceSelectorProps> = ({
  sources,
  currentSource,
  onChange,
}) => {
  const allSources = useMemo(() => [
    { id: null, name: 'すべてのソース', connected: true },
    ...sources,
  ], [sources]);

  return (
    <select
      value={currentSource ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="source-selector"
    >
      {allSources.map((src) => (
        <option key={src.id ?? 'all'} value={src.id ?? ''}>
          {src.name} {src.connected ? '🟢' : '🔴'}
        </option>
      ))}
    </select>
  );
};