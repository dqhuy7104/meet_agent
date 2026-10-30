import { useEffect, useState, type FormEvent } from 'react';
import { promptApi, type SystemPrompts } from '../lib/promptApi';

const defaults: SystemPrompts = {summary_system_prompt: '', rag_system_prompt: ''};

export function SystemPromptSettings() {
  const [prompts, setPrompts] = useState<SystemPrompts | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    setPrompts(null);
    void promptApi.get(controller.signal).then(data => {
      if (!controller.signal.aborted) setPrompts(data);
    }).catch(() => {
      if (!controller.signal.aborted) setError('Không tải được system prompt. Vui lòng thử lại.');
    });
    return () => controller.abort();
  }, [attempt]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!prompts || isSaving) return;
    setIsSaving(true);
    setError('');
    setNotice('');
    try {
      setPrompts(await promptApi.save(prompts));
      setNotice('Đã lưu system prompt.');
    } catch {
      setError('Không lưu được system prompt. Vui lòng thử lại.');
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="access-panel settings-prompts" aria-labelledby="system-prompts-title">
      <h2 id="system-prompts-title">System prompt</h2>
      <p id="system-prompts-help" className="access-note">
        Áp dụng cho toàn workspace ở các lần gọi mới. Bản tóm tắt đã lưu được giữ nguyên.
        Để trống để dùng mặc định. Quy tắc JSON, trích dẫn và chỉ dùng bằng chứng vẫn được áp dụng.
      </p>
      {error && <p role="alert" className="access-error">{error}</p>}
      {!prompts ? error ? (
        <button onClick={() => setAttempt(value => value + 1)}>Tải lại system prompt</button>
      ) : <p role="status">Đang tải system prompt…</p> : (
        <form onSubmit={event => void save(event)} aria-describedby="system-prompts-help">
          <fieldset disabled={isSaving}>
            <legend className="sr-only">Cấu hình system prompt</legend>
            <label htmlFor="summary-system-prompt">System prompt tóm tắt</label>
            <textarea id="summary-system-prompt" rows={7} maxLength={8000}
              placeholder="Ví dụ: Tóm tắt bằng tiếng Việt, tập trung vào quyết định và việc cần làm."
              value={prompts.summary_system_prompt ?? ''}
              onChange={event => { setPrompts({...prompts, summary_system_prompt: event.target.value}); setNotice(''); }} />
            <label htmlFor="rag-system-prompt">System prompt RAG</label>
            <textarea id="rag-system-prompt" rows={7} maxLength={8000}
              placeholder="Ví dụ: Trả lời ngắn gọn bằng tiếng Việt, nêu rõ những điểm chưa đủ bằng chứng."
              value={prompts.rag_system_prompt ?? ''}
              onChange={event => { setPrompts({...prompts, rag_system_prompt: event.target.value}); setNotice(''); }} />
            <p className="access-note">Tối đa 8.000 ký tự cho mỗi prompt. Khôi phục mặc định rồi bấm Lưu để áp dụng.</p>
            <div className="system-prompt-actions">
              <button type="submit">{isSaving ? 'Đang lưu…' : 'Lưu system prompt'}</button>
              <button type="button" onClick={() => { setPrompts({...defaults}); setNotice(''); }}>Khôi phục mặc định</button>
            </div>
          </fieldset>
        </form>
      )}
      <p role="status" aria-live="polite">{notice}</p>
    </section>
  );
}
