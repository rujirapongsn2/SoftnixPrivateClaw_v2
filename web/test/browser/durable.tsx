import React from 'react';
import { createRoot } from 'react-dom/client';
import { DurableJobs } from '../../src/DurableJobs';
import '../../src/styles.css';
createRoot(document.getElementById('root')!).render(
  <main style={{maxWidth: 760, margin: '16px auto', padding: 12}}>
    <DurableJobs sessionId="fixture-session" onDelivery={async () => {
      document.documentElement.dataset.deliveries = String(Number(document.documentElement.dataset.deliveries || 0) + 1);
      return true;
    }} />
  </main>,
);
