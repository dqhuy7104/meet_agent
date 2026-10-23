import type { SpeakerIdentity, VoiceIdentificationStatus } from '../types/voice';
export function speakerDisplay(speaker: string, identities: SpeakerIdentity[]): {name: string; label: string; similarity: number | null} {
  const identity = identities.find(item => item.speaker === speaker);
  return { name: identity?.display_name ?? 'Chưa xác định', label: speaker, similarity: identity?.origin === 'automatic' ? identity.similarity : null };
}
export function voiceStatusMessage(status: VoiceIdentificationStatus): string {
  const messages: Record<VoiceIdentificationStatus, string> = {
    completed: 'Đã nhận diện. Người chưa khớp hồ sơ được hiển thị là Chưa xác định.',
    failed: 'Nhận diện giọng nói thất bại. Transcript vẫn có sẵn; bạn có thể gán tên thủ công.',
    disabled: 'Nhận diện tự động đang tắt. Bạn có thể gán tên thủ công.',
    not_run: 'Chưa chạy nhận diện cho cuộc họp này. Bạn có thể gán tên thủ công.',
  };
  return messages[status];
}
export function fifoWarning(sampleCount: number): string | null {
  return sampleCount >= 5 ? 'Đã có 5 mẫu. Thêm mẫu thành công sẽ thay thế mẫu cũ nhất.' : null;
}
