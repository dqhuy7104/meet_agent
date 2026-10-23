import { useEffect, useRef, useState } from 'react';
import { voiceApi, voiceError } from '../lib/voiceApi';
import type { EnrollmentJob } from '../types/voice';
const failureMessages: Record<string, string> = {
  insufficient_speech: 'Cần ít nhất 10 giây lời nói riêng của một người.',
  multiple_speakers: 'Audio có nhiều người nói. Hãy chọn mẫu chỉ có một người.',
  source_changed: 'Nguồn giọng nói đã thay đổi. Hãy chọn bản ghi khác.',
  model_unavailable: 'Model chưa sẵn sàng. Kiểm tra cấu hình model local.',
};
export function VoiceEnrollmentJob({ job, onCompleted, onFinished }: {job: EnrollmentJob; onCompleted: () => void; onFinished?: () => void}) {
  const [current, setCurrent] = useState(job);
  const [message, setMessage] = useState('');
  const [attempt, setAttempt] = useState(0);
  const completed = useRef(onCompleted);
  completed.current = onCompleted;
  const finished = useRef(onFinished);
  finished.current = onFinished;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let count = 0;
    setCurrent(job);
    setMessage('');
    const poll = async () => {
      try {
        const next = await voiceApi.job(job.id, controller.signal);
        if (controller.signal.aborted) return;
        setCurrent(next);
        if (next.status === 'completed') { finished.current?.(); completed.current(); return; }
        if (next.status === 'failed') { finished.current?.(); return; }
        if (++count >= 60) { setMessage('Đã dừng tự cập nhật sau 2 phút. Công việc vẫn chạy; kiểm tra lại trạng thái.'); return; }
        timer = setTimeout(() => void poll(), 2000);
      } catch (cause) {
        if (!controller.signal.aborted) setMessage(voiceError(cause));
      }
    };
    if (job.status === 'queued' || job.status === 'processing') void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job, attempt]);
  return <div className="voice-job" role="status" aria-live="polite">
    <p>{current.status === 'completed' ? 'Đã thêm mẫu giọng nói.' : current.status === 'failed' ? `Thêm mẫu thất bại. ${failureMessages[current.failure_code ?? ''] ?? 'Kiểm tra audio (tối đa 2 phút) và thử lại.'}` : current.status === 'queued' ? 'Mẫu đang chờ worker.' : 'Đang xử lý mẫu giọng nói…'}</p>
    {message && <><p>{message}</p><button type="button" onClick={() => setAttempt(value => value + 1)}>Kiểm tra lại trạng thái</button></>}
  </div>;
}
