import React from 'react';
import { createRoot } from 'react-dom/client';
import { BasementRecallObservation } from '../src/components/BasementRecallObservation.jsx';
import '../src/styles.css';
const root = createRoot(document.getElementById('root'));
root.render(<BasementRecallObservation />);
window.unmountObservationPreview = () => root.unmount();
