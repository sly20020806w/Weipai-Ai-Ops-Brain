import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AppProviders } from './providers';
import { ConsoleApp } from './app';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders><BrowserRouter><ConsoleApp /></BrowserRouter></AppProviders>
  </StrictMode>,
);
