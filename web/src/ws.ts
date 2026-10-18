import { useEffect, useRef, useState } from "react";

export interface ServerEvent {
  type: string;
  data: any;
  ts?: number;
}

/** Subscribe to the backend event stream with automatic reconnect (exponential backoff). */
export function useEventStream(onEvent: (e: ServerEvent) => void): "connecting" | "open" | "closed" {
  const [status, setStatus] = useState<"connecting" | "open" | "closed">("connecting");
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let stopped = false;

    const connect = () => {
      setStatus("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}/ws`);
      socket.onopen = () => {
        retry = 0;
        setStatus("open");
      };
      socket.onmessage = (m) => {
        try {
          handler.current(JSON.parse(m.data));
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setStatus("closed");
        if (!stopped) timer = window.setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return status;
}
