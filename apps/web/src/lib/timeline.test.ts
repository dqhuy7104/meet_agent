import { describe, expect, it } from "vitest";
import { findAudioPartAtTime } from "./timeline";

describe("findAudioPartAtTime", () => {
  const parts = [
    { start_seconds: 0, duration_seconds: 1000 },
    { start_seconds: 1200, duration_seconds: 1000 },
  ];

  it("does not select a neighboring file inside a recording gap", () => {
    expect(findAudioPartAtTime(parts, 1000)).toBeNull();
    expect(findAudioPartAtTime(parts, 1100)).toBeNull();
  });

  it("selects the next file at its actual start", () => {
    expect(findAudioPartAtTime(parts, 1200)).toBe(1);
  });
});
