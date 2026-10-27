import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '../lib/AuthProvider';
import { projectApi } from '../lib/projectApi';
import { NavLink } from 'react-router-dom';
import { meetingApi, type BackendMeeting } from '../lib/api';
import { voiceApi, voiceError } from '../lib/voiceApi';
import { fifoWarning } from '../lib/voiceDisplay';
import type { ApiMeeting } from '../types/meeting';
import type { EnrollmentJob, VoiceProfile, VoiceSample } from '../types/voice';
import { VoiceEnrollmentJob } from './VoiceEnrollmentJob';

function ProfileEditor({ profile, onChanged, canDelete }: {profile: VoiceProfile; onChanged: () => void; canDelete: boolean}) {
  const [name, setName] = useState(profile.display_name);
  const [samples, setSamples] = useState<VoiceSample[] | null>(null);
  const [meetings, setMeetings] = useState<ApiMeeting[]>([]);
  const [meeting, setMeeting] = useState<BackendMeeting | null>(null);
  const [meetingId, setMeetingId] = useState('');
  const [speaker, setSpeaker] = useState('');
  const [file, setFile] = useState<File>();
  const [job, setJob] = useState<EnrollmentJob>();
  const [error, setError] = useState('');
  const [isBusy, setIsBusy] = useState(false);
  const [isEnrolling, setIsEnrolling] = useState(false);
  const refreshSamples = useCallback(async (signal?: AbortSignal) => {
    try { const result = await voiceApi.samples(profile.id, signal); if (!signal?.aborted) setSamples(result); }
    catch (cause) { if (!signal?.aborted) setError(voiceError(cause)); }
  }, [profile.id]);
  useEffect(() => {
    const controller = new AbortController();
    void refreshSamples(controller.signal);
    void meetingApi.list(controller.signal).then(page => setMeetings(page.items.filter(item => item.status === 'completed'))).catch(cause => { if (!controller.signal.aborted) setError(voiceError(cause)); });
    return () => controller.abort();
  }, [refreshSamples]);
  useEffect(() => {
    setMeeting(null); setSpeaker('');
    if (!meetingId) return;
    const controller = new AbortController();
    void meetingApi.get(meetingId, controller.signal).then(detail => { if (!controller.signal.aborted) setMeeting(detail); }).catch(cause => { if (!controller.signal.aborted) setError(voiceError(cause)); });
    return () => controller.abort();
  }, [meetingId]);
  const run = async (action: () => Promise<void>) => {
    setIsBusy(true); setError('');
    try { await action(); } catch (cause) { setError(voiceError(cause)); } finally { setIsBusy(false); }
  };
  const start = async (action: () => Promise<EnrollmentJob>) => {
    await run(async () => { const next = await action(); setJob(next); setIsEnrolling(next.status === 'queued' || next.status === 'processing'); });
  };
  const speakers = [...new Set(meeting?.diarization_segments.map(turn => turn.speaker) ?? [])];
  const warning = fifoWarning(samples?.length ?? profile.sample_count);
  return <section className="panel voice-editor" aria-labelledby="profile-editor-title">
    <h3 id="profile-editor-title">{profile.display_name}</h3>
    {error && <p role="alert" className="message">{error}</p>}
    <form onSubmit={event => { event.preventDefault(); void run(async () => { await voiceApi.rename(profile.id, name.trim()); onChanged(); }); }}>
      <label>Tên người tham gia<input required maxLength={100} value={name} onChange={event => setName(event.target.value)} /></label>
      <button disabled={isBusy || !name.trim()}>Lưu tên</button>
      {canDelete && <button type="button" disabled={isBusy} onClick={() => {
        if (window.confirm(`Xóa hồ sơ “${profile.display_name}” và tất cả mẫu giọng nói?`)) void run(async () => { await voiceApi.remove(profile.id); onChanged(); });
      }}>Xóa hồ sơ và mẫu</button>}
    </form>
    <h4>Mẫu giọng nói · {samples?.length ?? profile.sample_count}/5</h4>
    <p>Mẫu được xếp từ cũ nhất đến mới nhất. Xóa mẫu không thay đổi tên đã gán trong cuộc họp trước.</p>
    {warning && <p className="message">{warning}</p>}
    {samples === null ? <p>Đang tải mẫu…</p> : samples.length === 0 ? <p>Chưa có mẫu giọng nói.</p> : <ol className="voice-samples">{samples.map(sample => <li key={sample.id}>
      <div><b>#{sample.id} · {sample.source === 'upload' ? 'Audio tải lên' : `Cuộc họp · ${sample.source_speaker}`}</b>
      <small>{sample.source_meeting_id && <NavLink to={`/workspace/${encodeURIComponent(sample.source_meeting_id)}`}>Xem cuộc họp nguồn · </NavLink>}FIFO {sample.fifo_position} · {new Date(sample.created_at).toLocaleString('vi-VN')}</small>
      <small>Phiên bản: {sample.fingerprint}</small></div>
      {canDelete && <button type="button" disabled={isBusy || isEnrolling} aria-label={`Xóa mẫu ${sample.id}`} onClick={() => void run(async () => { await voiceApi.removeSample(profile.id, sample.id); await refreshSamples(); onChanged(); })}>Xóa mẫu</button>}
    </li>)}</ol>}
    <h4>Thêm mẫu từ audio</h4>
    <p>Một người nói, ít nhất 10 giây lời nói riêng, audio tối đa 2 phút và 25 MiB. Audio mẫu được xóa sau xử lý.</p>
    <form onSubmit={event => { event.preventDefault(); if (!file) return; if (!file.size || file.size > 25 * 1024 * 1024) { setError('Chọn file audio không rỗng, tối đa 25 MiB.'); return; } void start(() => voiceApi.upload(profile.id, file)); }}>
      <label>File audio một người nói<input type="file" accept="audio/*" required onChange={event => setFile(event.target.files?.[0])} /></label>
      <button disabled={isBusy || isEnrolling || !file}>Thêm mẫu từ audio</button>
    </form>
    <h4>Thêm mẫu từ cuộc họp</h4>
    <form onSubmit={event => { event.preventDefault(); void start(() => voiceApi.fromMeeting(profile.id, meetingId, speaker)); }}>
      <label>Cuộc họp đã hoàn tất<select value={meetingId} onChange={event => setMeetingId(event.target.value)} required><option value="">Chọn cuộc họp</option>{meetings.map(item => <option key={item.id} value={item.id}>{item.title ?? item.original_filename}</option>)}</select></label>
      <label>Người nói trong bản ghi<select value={speaker} onChange={event => setSpeaker(event.target.value)} required disabled={!meeting}><option value="">Chọn nhãn người nói</option>{speakers.map(label => <option key={label}>{label}</option>)}</select></label>
      <button disabled={isBusy || isEnrolling || !speaker}>Thêm mẫu từ cuộc họp</button>
    </form>
    {job && <VoiceEnrollmentJob job={job} onCompleted={() => { void refreshSamples(); onChanged(); }} onFinished={() => setIsEnrolling(false)} />}
  </section>;
}
export function VoiceProfilesPage() {
  const {account}=useAuth();
  const [canManage,setCanManage]=useState(false);
  useEffect(()=>{let active=true;void projectApi.list().then(projects=>{if(active)setCanManage(projects.some(project=>project.role === "project_manager"));}).catch(()=>{});return()=>{active=false;};},[]);
  const canEdit=!!account?.is_system_admin || canManage;
  const [profiles, setProfiles] = useState<VoiceProfile[] | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [isBusy, setIsBusy] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    try { const items = await voiceApi.list(signal); if (!signal?.aborted) { setProfiles(items); setSelectedId(current => items.some(item => item.id === current) ? current : items[0]?.id ?? ''); setError(''); } }
    catch (cause) { if (!signal?.aborted) setError(voiceError(cause)); }
  }, []);
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [load]);
  const selected = profiles?.find(item => item.id === selectedId);
  return <div className="page voice-page">
    <section className="title"><div><h2>Hồ sơ giọng nói</h2><p>Thêm mẫu để nhận diện trong các cuộc họp mới. Cuộc họp cũ không tự nhận diện lại.</p></div></section>
    {error && <p className="message" role="alert">{error} <button onClick={() => void load()}>Thử lại</button></p>}
    {canEdit && <form className="panel voice-create" onSubmit={event => { event.preventDefault(); setIsBusy(true); setError(''); void voiceApi.create(name.trim()).then(async profile => { setName(''); await load(); setSelectedId(profile.id); }).catch(cause => setError(voiceError(cause))).finally(() => setIsBusy(false)); }}>
      <label>Tên người tham gia mới<input required maxLength={100} value={name} onChange={event => setName(event.target.value)} /></label><button className="primary" disabled={isBusy || !name.trim()}>Tạo hồ sơ</button>
    </form>}
    {profiles === null ? <p>Đang tải hồ sơ…</p> : !profiles.length ? <p>Chưa có hồ sơ. Tạo người tham gia để bắt đầu.</p> : <div className="voice-layout">
      <section className="panel"><label>Chọn hồ sơ<select value={selectedId} onChange={event => setSelectedId(event.target.value)}>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.display_name} ({profile.sample_count}/5 mẫu)</option>)}</select></label></section>
      {selected && canEdit && <ProfileEditor canDelete={!!account?.is_system_admin} key={selected.id} profile={selected} onChanged={() => void load()} />}
    </div>}
  </div>;
}
