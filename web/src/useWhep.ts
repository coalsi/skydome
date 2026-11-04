import { useEffect, useState, type RefObject } from "react";
import { playWhep, type WhepSession } from "./whep";

export type Conn = "connecting" | "live" | "waiting" | "off";

// Plays a mediamtx path into a video element while `enabled`, reconnecting
// whenever the stream drops (mode switch, restart, a stream that's still starting)
export function useWhep(path: string, video: RefObject<HTMLVideoElement | null>, enabled = true): Conn {
  const [conn, setConn] = useState<Conn>(enabled ? "connecting" : "off");
  useEffect(() => {
    if (!enabled) {
      setConn("off");
      return;
    }
    let session: WhepSession | null = null;
    let timer: number | undefined;
    let stopped = false;
    const url = `/api/webrtc/${path}/whep`; // the server passes it on to mediamtx
    const retry = (ms: number) => {
      session?.close();
      session = null;
      if (!stopped) timer = window.setTimeout(connect, ms);
    };
    const connect = async () => {
      setConn((c) => (c === "live" ? "connecting" : c));
      try {
        session = await playWhep(url, video.current!, (state) => {
          if (state === "connected") setConn("live");
          if (state === "failed" || state === "disconnected" || state === "closed") {
            setConn("connecting");
            retry(1500);
          }
        });
        if (stopped) session.close();
      } catch {
        setConn("waiting");
        retry(2000);
      }
    };
    setConn("connecting");
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      session?.close();
    };
  }, [path, enabled, video]);
  return conn;
}
