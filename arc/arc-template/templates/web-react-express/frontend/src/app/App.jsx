import React from 'react';
import { DataProvider } from './store.jsx';

// ASSEMBLY LAYER — the only file that wires features together.
// Per requirement: add ONE import of its feature component and mount it.
// The FIRST requirement usually lays out the page structure here; after
// that, keep edits to imports + mounts. Business logic NEVER lives here —
// it goes in frontend/src/features/<name>.jsx (self-contained).
export default function App() {
  return (
    <DataProvider>
      <div className="app">
        {/* mount feature components here */}
      </div>
    </DataProvider>
  );
}
