export function speakerLabels(turns: { start: number; end: number; speaker: string }[]): string[] {
  return [...new Set(turns.map((turn) => turn.speaker).filter((label) => label && !/(^|_)unknown$/i.test(label)))];
}
