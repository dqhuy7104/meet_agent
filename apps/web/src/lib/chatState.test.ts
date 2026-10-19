import { describe, expect, it } from 'vitest';
import { citationLocation, selectChatProvider, validateChatInput } from './chatState';
const catalog = {max_k:3, providers:[{id:'local',name:'Local',is_external:false,chat_models:['a']},{id:'other',name:'Other',is_external:true,chat_models:['b']}], embedding_profiles:[{id:'p',provider_id:'local',model:'e',revision:'v1'}]};
const input={question:' Ai? ',k:2,chat_provider_id:'local',chat_model_id:'a',embedding_profile_id:'p'};
describe('chat input',()=>{
  it('uses server limits and enabled profiles',()=>{
    expect(validateChatInput(input,catalog)).toBe('');
    for(const k of [0,4,1.5,NaN]) expect(validateChatInput({...input,k},catalog)).not.toBe('');
    expect(validateChatInput(input,{...catalog,embedding_profiles:[]})).not.toBe('');
    expect(validateChatInput({...input,question:'\0'},catalog)).not.toBe('');
  });
  it('resets an incompatible model and encodes source time',()=>{
    expect(selectChatProvider(input,'other',catalog).chat_model_id).toBe('b');
    expect(citationLocation({meeting_id:'a/b ?',start_seconds:12.5})).toBe('/workspace/a%2Fb%20%3F?t=12.5');
  });
});
