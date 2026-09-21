import { describe, expect, it } from "vitest";
import { parseFrames } from "./sse";

describe("parseFrames", () => {
  it("reads complete frames and keeps the remainder", () => {
    const { events, rest } = parseFrames(
      'data: {"type":"delta","text":"Your "}\n\ndata: {"type":"delta"',
    );
    expect(events).toEqual([{ type: "delta", text: "Your " }]);
    expect(rest).toBe('data: {"type":"delta"');
  });

  it("survives a frame split across two chunks", () => {
    // The case that is hard to reproduce in a browser and breaks everything
    // when it happens: a frame crossing a packet boundary.
    const first = parseFrames('data: {"type":"delta","te');
    expect(first.events).toEqual([]);

    const second = parseFrames(first.rest + 'xt":"clicks fell."}\n\n');
    expect(second.events).toEqual([{ type: "delta", text: "clicks fell." }]);
    expect(second.rest).toBe("");
  });

  it("drops an unparseable frame rather than ending the answer", () => {
    const { events } = parseFrames(
      'data: not json\n\ndata: {"type":"done"}\n\n',
    );
    expect(events).toEqual([{ type: "done" }]);
  });

  it("ignores comments and blank frames", () => {
    const { events } = parseFrames(': keep-alive\n\ndata: {"type":"done"}\n\n');
    expect(events).toEqual([{ type: "done" }]);
  });
});
