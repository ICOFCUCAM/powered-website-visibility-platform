/**
 * Reading a server-sent event stream.
 *
 * `EventSource` only does GET, and asking a question is a POST, so the stream
 * is read from `fetch` by hand. The parsing is split out here because the
 * interesting case is the one that is hard to reproduce in a browser: a frame
 * arriving in two chunks, which happens whenever an answer crosses a network
 * packet boundary and produces a JSON parse error if you assume otherwise.
 */

export interface Frame {
  type: string;
  [key: string]: unknown;
}

export interface ParseResult {
  events: Frame[];
  rest: string;
}

/**
 * Pulls every complete frame out of the buffer, and hands back whatever is
 * left so the caller can prepend the next chunk to it.
 */
export function parseFrames(buffer: string): ParseResult {
  const events: Frame[] = [];
  let rest = buffer;

  for (;;) {
    const boundary = rest.indexOf("\n\n");
    if (boundary === -1) break;

    const frame = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);

    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;

    try {
      events.push(JSON.parse(data) as Frame);
    } catch {
      // A frame we cannot parse is dropped rather than thrown: one bad frame
      // must not end an answer the customer is halfway through reading.
    }
  }

  return { events, rest };
}
