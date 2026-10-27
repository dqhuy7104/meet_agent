import { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { voiceApi, voiceError } from '../lib/voiceApi';
import { fifoWarning, speakerDisplay, voiceStatusMessage } from '../lib/voiceDisplay';
import type { EnrollmentJob, SpeakerIdentity, VoiceIdentificationStatus, VoiceProfile } from '../types/voice';
import { VoiceEnrollmentJob } from './VoiceEnrollmentJob';
function SpeakerEditor({meetingId, speaker, identities, profiles, onCorrected, onEnrolled}: {
  meetingId: string; speaker: string; identities: SpeakerIdentity[]; profiles: VoiceProfile[];
  onCorrected: (identity: SpeakerIdentity) => void; onEnrolled: () => void;
}) {
  const identity = identities.find(item => item.speaker === speaker);
  const display = speakerDisplay(speaker, identities);
  const [profileId, setProfileId] = useState(identity?.profile_id ?? '');
  const [isBusy, setIsBusy] = useState(false);
  const [isEnrolling, setIsEnrolling] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [job, setJob] = useState<EnrollmentJob>();
  useEffect(() => { setProfileId(identity?.profile_id ?? ''); }, [identity?.profile_id]);
  const run = async (action: () => Promise<void>) => {
    setIsBusy(true); setError(''); setNotice('');
    try { await action(); } catch (cause) { setError(voiceError(cause)); } finally { setIsBusy(false); }
  };
  const correct = (id: string | null) => run(async () => { onCorrected(await voiceApi.correct(meetingId, speaker, id)); setNotice('Đã lưu tên cho cuộc họp này. Chưa tạo mẫu giọng nói.'); });
  const profile = profiles.find(item => item.id === profileId);
  const warning = profile ? fifoWarning(profile.sample_count) : null;
  return <section className="voice-speaker" aria-label={`Nhận diện ${speaker}`}>
    <h4>{display.name}</h4><p><code>{display.label}</code>{display.similarity !== null && <> · Cosine: {display.similarity.toFixed(3)}</>}</p>
    <label>Hồ sơ cho {speaker}<select value={profileId} onChange={event => setProfileId(event.target.value)}><option value="">Chọn hồ sơ</option>{profiles.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>
    <div className="voice-actions"><button type="button" disabled={isBusy || !profileId} onClick={() => void correct(profileId)}>Lưu tên</button><button type="button" disabled={isBusy} onClick={() => void correct(null)}>Xóa tên đã gán</button></div>
    {warning && <p className="message">{warning}</p>}
    <button type="button" disabled={isBusy || isEnrolling || !profileId} onClick={() => void run(async () => { const next = await voiceApi.fromMeeting(profileId, meetingId, speaker); setJob(next); setIsEnrolling(next.status === 'queued' || next.status === 'processing'); })}>Dùng giọng này làm mẫu{profile ? ` cho ${profile.display_name}` : ''}</button>
    {error && <p role="alert" className="message">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {job && <VoiceEnrollmentJob job={job} onCompleted={onEnrolled} onFinished={() => setIsEnrolling(false)} />}
  </section>;
}
export function MeetingSpeakerIdentities({meetingId, speakers, identities, status, onCorrected, canEdit = false}: {
  canEdit?: boolean; meetingId: string; speakers: string[]; identities: SpeakerIdentity[]; status: VoiceIdentificationStatus;
  onCorrected: (identity: SpeakerIdentity) => void;
}) {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [error, setError] = useState('');
  const load = async (signal?: AbortSignal) => {
    try { const items = await voiceApi.list(signal); if (!signal?.aborted) { setProfiles(items); setError(''); } }
    catch (cause) { if (!signal?.aborted) setError(voiceError(cause)); }
  };
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [meetingId]);
  return <section className="voice-identities" aria-label="Nhận diện người nói">
    <h4>Danh tính người nói</h4><p role="status">{voiceStatusMessage(status)}</p>
    <p>Gán tên chỉ áp dụng cho cuộc họp này. Thêm mẫu là thao tác riêng để dùng trong cuộc họp mới.</p>
    <NavLink to="/voice-profiles">Quản lý hồ sơ giọng nói</NavLink>
    {error && <p role="alert">{error} <button onClick={() => void load()}>Tải lại hồ sơ</button></p>}
    {!profiles.length && !error && <p>Tạo hồ sơ trước khi gán tên hoặc thêm mẫu.</p>}
    {!canEdit && speakers.map(speaker => <p key={speaker}>{speakerDisplay(speaker, identities).name} · {speaker}</p>)}
    {canEdit && speakers.map(speaker => <SpeakerEditor key={`${meetingId}:${speaker}`} meetingId={meetingId} speaker={speaker} identities={identities} profiles={profiles} onCorrected={onCorrected} onEnrolled={() => void load()} />)}
  </section>;
}
