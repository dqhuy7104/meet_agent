export type AudioPartTiming = { start_seconds: number; duration_seconds: number };

export function findAudioPartAtTime(parts: AudioPartTiming[], seconds: number): number | null {
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (seconds >= part.start_seconds && seconds < part.start_seconds + part.duration_seconds) {
      return index;
    }
  }
  return null;
}
