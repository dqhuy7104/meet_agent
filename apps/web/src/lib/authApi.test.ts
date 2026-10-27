import { afterEach, expect, it, vi } from 'vitest';
import { ApiError, request, setCsrfToken } from './api';
afterEach(()=>{vi.unstubAllGlobals();setCsrfToken(null);});
it('sends cookies and CSRF for unsafe methods including direct API attempts',async()=>{
 const fetch=vi.fn().mockResolvedValue({ok:false,status:403,json:async()=>({detail:'Action not permitted'})});vi.stubGlobal('fetch',fetch);setCsrfToken('token');
 await expect(request('/api/projects/p/members/a',{method:'PUT'})).rejects.toBeInstanceOf(ApiError);
 expect(fetch.mock.calls[0][1].credentials).toBe('same-origin');expect(fetch.mock.calls[0][1].headers.get('X-CSRF-Token')).toBe('token');
});
it('does not send CSRF for reads',async()=>{const fetch=vi.fn().mockResolvedValue({ok:true,status:200,json:async()=>[]});vi.stubGlobal('fetch',fetch);setCsrfToken('token');await request('/api/projects');expect(fetch.mock.calls[0][1].headers.has('X-CSRF-Token')).toBe(false);});
