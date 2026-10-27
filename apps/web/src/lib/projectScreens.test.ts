import {afterEach,expect,it,vi} from 'vitest';
import {botApi} from './botApi';
import {setCsrfToken,request} from './api';
afterEach(()=>{vi.unstubAllGlobals();setCsrfToken(null);});
it('sends project and shared session protection when creating a bot',async()=>{
 vi.stubGlobal('crypto',{randomUUID:()=> 'key'});
 const fetcher=vi.fn().mockResolvedValue({ok:true,status:200,json:async()=>({})});vi.stubGlobal('fetch',fetcher);setCsrfToken('csrf');
 await botApi.create('https://meet.google.com/abc-defg-hij','Title','project-a');
 const options=fetcher.mock.calls[0][1];expect(JSON.parse(options.body).project_id).toBe('project-a');expect(options.headers.get('X-CSRF-Token')).toBe('csrf');expect(options.credentials).toBe('same-origin');
});
it.each([401,403])('explains access error %s',async(status)=>{vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status,json:async()=>({detail:'Action not permitted'})}));await expect(request('/api/meetings/private')).rejects.toThrow(status===401?'đăng nhập':'quyền');});
