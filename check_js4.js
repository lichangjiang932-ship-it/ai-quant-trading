const fs = require('fs');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
try { new Function(scripts.join('\n;\n')); console.log('JS OK'); }
catch (e) { console.log('JS ERR:', e.message); process.exit(1); }
