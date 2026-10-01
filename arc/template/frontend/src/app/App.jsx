import React from 'react';
import { DataProvider } from './store.jsx';

// ASSEMBLY LAYER — the only file that wires features together: add ONE
// import + mount per requirement family. Business logic lives in
// frontend/src/features/<name>.jsx (self-contained).
export default function App() {
  return (
    <DataProvider>
      <div className="app">
        {/* mount feature components here */}
      </div>
    </DataProvider>
  );
}
