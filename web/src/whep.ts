// Plays a mediamtx path over WebRTC (WHEP: one HTTP POST of the SDP offer)

export interface WhepSession {
  close(): void;
}

export async function playWhep(
  url: string,
  video: HTMLVideoElement,
  onState: (state: RTCPeerConnectionState) => void,
): Promise<WhepSession> {
  const pc = new RTCPeerConnection();
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (e) => {
    video.srcObject = e.streams[0] ?? new MediaStream([e.track]);
  };
  pc.onconnectionstatechange = () => onState(pc.connectionState);

  let resource: string | null = null;
  const close = () => {
    pc.close();
    if (resource) fetch(resource, { method: "DELETE" }).catch(() => {});
  };

  try {
    await pc.setLocalDescription(await pc.createOffer());
    await iceGathered(pc, 1500); // LAN host candidates arrive at once
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription!.sdp,
    });
    if (!r.ok) throw new Error(r.status === 404 ? "no stream" : `WHEP ${r.status}`);
    const loc = r.headers.get("Location");
    resource = loc ? new URL(loc, location.href).toString() : null;
    await pc.setRemoteDescription({ type: "answer", sdp: await r.text() });
  } catch (e) {
    close();
    throw e;
  }
  return { close };
}

function iceGathered(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => pc.iceGatheringState === "complete" && done();
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, timeoutMs);
  });
}
