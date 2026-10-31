import {readFileSync} from 'node:fs';
import {expect,it} from 'vitest';

it('limits collapsed navigation styles to the application sidebar',()=>{
 const css=readFileSync(new URL('../geometry.css',import.meta.url),'utf8');
 expect(css).not.toMatch(/\.app\.sidebar-hidden\s+aside\s*\{/);
 expect(css).not.toMatch(/\.app\.sidebar-hidden\s+header\s*\{/);
 expect(css).toContain('.app.sidebar-hidden>aside');
 const base=readFileSync(new URL('../styles.css',import.meta.url),'utf8');
 expect(base).not.toMatch(/\.app\s+aside\s*\{/);
});
