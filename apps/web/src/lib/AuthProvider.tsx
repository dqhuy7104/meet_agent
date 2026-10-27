import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { authApi, type Session } from './authApi';
import { setCsrfToken } from './api';
function useSession() {
  const [session,setSession] = useState<Session | null>(null);
  const [loading,setLoading] = useState(true);
  const accept = useCallback((value:Session|null) => { setCsrfToken(value?.csrf_token ?? null); setSession(value); },[]);
  const refresh = useCallback(async () => { try { accept(await authApi.session()); } catch { accept(null); } finally { setLoading(false); } },[accept]);
  useEffect(() => { void refresh(); const expired = () => accept(null); window.addEventListener('session-expired',expired); return () => window.removeEventListener('session-expired',expired); },[refresh,accept]);
  return {account:session?.account ?? null,csrfToken:session?.csrf_token ?? null,loading,refresh,
    login:async(email:string,password:string)=>accept(await authApi.login(email,password)),
    register:async(email:string,password:string)=>accept(await authApi.register(email,password)),
    logout:async()=>{await authApi.logout();accept(null);}};
}
const AuthContext = createContext<ReturnType<typeof useSession> | null>(null);
export function AuthProvider({children}:{children:ReactNode}) { return <AuthContext.Provider value={useSession()}>{children}</AuthContext.Provider>; }
export function useAuth() { const value = useContext(AuthContext); if (!value) throw new Error('AuthProvider required'); return value; }
