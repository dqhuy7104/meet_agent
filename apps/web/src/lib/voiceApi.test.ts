import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';
import type { VoiceSample, VoiceProfile, EnrollmentJob, SpeakerIdentity } from '../types/voice';
import { voiceApi } from './voiceApi';
afterEach(() => { vi.unstubAllGlobals(); });
function respond(body: unknown, status = 200) {
  const fetch = vi.fn().mockResolvedValue({ok:status < 400,status,json:async()=>body});
  vi.stubGlobal('fetch', fetch);
  return fetch;
}
describe('voice API', () => {
  it('exposes metadata contracts without vector fields', () => {
    expectTypeOf<Extract<keyof VoiceSample | keyof VoiceProfile | keyof EnrollmentJob | keyof SpeakerIdentity, 'vector' | 'embedding' | 'embeddings'>>().toEqualTypeOf<never>();
  });
  it('sends profile IDs and null clearing with original label', async () => {
    const fetch = respond({});
    await voiceApi.correct('m/1','SPEAKER_00','p');
    expect(fetch.mock.calls[0][0]).toBe('/api/meetings/m%2F1/speaker-identity');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({speaker:'SPEAKER_00',profile_id:'p'});
    await voiceApi.correct('m/1','SPEAKER_00',null);
    expect(JSON.parse(fetch.mock.calls[1][1].body).profile_id).toBeNull();
  });
  it('uploads multipart audio without manual content type', async () => {
    const fetch = respond({id:'job',status:'queued',failure_code:null});
    await voiceApi.upload('p', new Blob(['audio'], {type:'audio/wav'}));
    const options = fetch.mock.calls[0][1];
    expect(options.body).toBeInstanceOf(FormData);
    expect(options.body.get('file')).toBeInstanceOf(Blob);
    expect(options.headers.get("Content-Type")).toBeNull();
  });
  it('reads job states with abort support', async () => {
    const job = {id:'j',status:'failed',failure_code:'insufficient_speech'};
    const fetch = respond(job);
    const controller = new AbortController();
    expect(await voiceApi.job('j',controller.signal)).toEqual(job);
    expect(fetch.mock.calls[0][0]).toBe('/api/voice-enrollment-jobs/j');
    expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
    expect(fetch.mock.calls[0][1].credentials).toBe('same-origin');
  });
  it('preserves FIFO order returned by API', async () => {
    respond([{id:9,fifo_position:1},{id:12,fifo_position:2}]);
    expect((await voiceApi.samples('p')).map(sample=>sample.id)).toEqual([9,12]);
  });
  it('sends meeting enrollment and named profile mutations', async () => {
    const fetch=respond({});
    await voiceApi.fromMeeting('p','m','PART_0_SPEAKER_00');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({meeting_id:'m',speaker:'PART_0_SPEAKER_00'});
    await voiceApi.create('An');
    await voiceApi.rename('p','Bình');
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({display_name:'An'});
    expect(fetch.mock.calls[2][1].method).toBe('PATCH');
  });
  it('handles empty deletion and actionable API errors', async () => {
    respond(undefined,204);
    await expect(voiceApi.remove('p')).resolves.toBeUndefined();
    respond({detail:'Voice profile not found'},404);
    await expect(voiceApi.samples('missing')).rejects.toThrow('Voice profile not found');
  });
});
