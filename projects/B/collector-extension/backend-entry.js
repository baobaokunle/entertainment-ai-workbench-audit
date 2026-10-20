// New entry uses the installed backend implementation, leaving active pages intact.
document.getElementById('collectChat').checked=false;
await import('./dashboard.js');
