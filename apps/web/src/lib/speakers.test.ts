import { describe, expect, it } from "vitest";
import { speakerLabels } from "./speakers";

describe("speakerLabels", () => {
  it("counts distinct diarization labels instead of turns", () => {
    expect(speakerLabels([
      { start: 0, end: 4, speaker: "SPEAKER_00" },
      { start: 2, end: 5, speaker: "SPEAKER_01" },
      { start: 6, end: 8, speaker: "SPEAKER_00" },
      { start: 8, end: 9, speaker: "unknown" },
      { start: 9, end: 10, speaker: "PART_2_unknown" },
    ])).toEqual(["SPEAKER_00", "SPEAKER_01"]);
  });
});
