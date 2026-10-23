import { describe, expect, it } from 'vitest';
import { fifoWarning, speakerDisplay, voiceStatusMessage } from './voiceDisplay';
describe('voice display', () => {
  it('preserves original label alongside a known name and score', () => {
    expect(speakerDisplay('SPEAKER_00', [{speaker:'SPEAKER_00',profile_id:'p',display_name:'An',similarity:0.9,origin:'automatic'}])).toEqual({name:'An',label:'SPEAKER_00',similarity:0.9});
  });
  it('shows unknown for missing or cleared identities', () => {
    expect(speakerDisplay('SPEAKER_00', []).name).toBe('Chưa xác định');
    expect(speakerDisplay('SPEAKER_00', [{speaker:'SPEAKER_00',profile_id:null,display_name:null,similarity:null,origin:'manual'}]).name).toBe('Chưa xác định');
  });
  it('distinguishes failure from unknown and other states', () => {
    expect(voiceStatusMessage('failed')).not.toBe(voiceStatusMessage('completed'));
    expect(voiceStatusMessage('failed')).toContain('thất bại');
    expect(voiceStatusMessage('disabled')).not.toBe(voiceStatusMessage('not_run'));
  });
  it('warns about oldest replacement after successful sixth enrollment', () => {
    expect(fifoWarning(4)).toBeNull();
    expect(fifoWarning(5)).toContain('cũ nhất');
    expect(fifoWarning(5)).toContain('thành công');
  });
});
