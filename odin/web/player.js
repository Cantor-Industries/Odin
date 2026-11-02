/**
 * Native go2rtc WebRTC player for the drone HUD.
 *
 * Why not the <video-stream> element from go2rtc?
 *   It lives on :1984 and would have to be embedded in an iframe. An iframe
 *   blocks access to the <video> element, so the HUD could not report real
 *   frame rate, decode timing or jitter buffer depth - and could not tune the
 *   browser's playout delay, which is the single biggest source of avoidable
 *   latency on this link.
 *
 * This implements only the signalling go2rtc needs (WebSocket + one offer),
 * keeps the media element local, and exposes getStats() to the HUD.
 */

const GO2RTC_PORT = 1984;

export class DronePlayer {
  constructor(videoEl, opts = {}) {
    this.video = videoEl;
    this.host = opts.host || location.hostname;
    this.src = opts.src || 'drone';
    this.port = opts.port || GO2RTC_PORT;

    // Playout delay requested from the browser, in seconds. WebRTC otherwise
    // buffers ~0.3-0.5 s to smooth jitter; on a LAN we can run far tighter.
    //
    // The correct lever for an INBOUND stream is RTCRtpReceiver
    // .jitterBufferTarget (Chrome 111+). `sender.playoutDelayHint` only affects
    // media we send, which is why it did nothing here.
    this.jitterBufferTarget = opts.jitterBufferTarget ?? 0.03;

    this.ws = null;
    this.pc = null;
    this.state = 'idle';
    this.retry = 0;
    this.stats = null;
    this.onstate = opts.onstate || (() => {});

    this._last = { t: 0, frames: 0, bytes: 0, decode: 0, packets: 0, lost: 0 };
    this._fps = 0;
    this._bitrate = 0;
    this._decodeMs = 0;
    this._jitterMs = 0;
    this._rttMs = 0;
    this._lost = 0;
    this._pps = 0;
    this._maxGapPkts = 0;
    this._maxGapMs = 0;

    // poll stats continuously so the HUD can show them
    this._statsTimer = setInterval(() => this._sample(), 1000);
  }

  url() {
    return `ws://${this.host}:${this.port}/api/ws?src=${encodeURIComponent(this.src)}`;
  }

  connect() {
    this._teardown();
    this._setState('connecting');
    let ws;
    try {
      ws = new WebSocket(this.url());
    } catch (e) {
      this._scheduleRetry();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.retry = 0;
      this._setState('signalling');
      this._offer();
    };
    ws.onmessage = (ev) => this._onMessage(ev);
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this._setState('closed');
      this._scheduleRetry();
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  _setState(s) {
    if (this.state !== s) {
      this.state = s;
      this.onstate(s);
    }
  }

  _scheduleRetry() {
    this.retry = Math.min(this.retry + 1, 6);
    const delay = Math.min(400 * Math.pow(1.7, this.retry), 6000);
    clearTimeout(this._retryTimer);
    this._retryTimer = setTimeout(() => this.connect(), delay);
  }

  _onMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }

    if (msg.type === 'error') {
      // go2rtc reports stream errors here (e.g. camera busy)
      this._setState('error');
      this.lastError = msg.value;
      return;
    }
    if (msg.type === 'webrtc/answer') {
      if (this.pc) {
        this.pc.setRemoteDescription({ type: 'answer', sdp: msg.value })
          .catch(() => {});
      }
      return;
    }
    if (msg.type === 'webrtc/candidate') {
      if (this.pc && msg.value) {
        this.pc.addIceCandidate({ candidate: msg.value, sdpMid: '0' })
          .catch(() => {});
      }
      return;
    }
  }

  async _offer() {
    const pc = new RTCPeerConnection({ iceServers: [] });
    this.pc = pc;

    pc.addTransceiver('video', { direction: 'recvonly' });
    // Audio is not used, but the transceiver keeps go2rtc's SDP shape simple
    // if a stream ever carries it.

    pc.ontrack = (ev) => {
      const stream = ev.streams[0] || new MediaStream([ev.track]);
      if (this.video.srcObject !== stream) {
        this.video.srcObject = stream;
        this.video.play().catch(() => {
          // Autoplay can be blocked before any user gesture; muting is the
          // standard workaround for a video-only stream.
          this.video.muted = true;
          this.video.play().catch(() => {});
        });
      }
      this._applyJitterTarget(pc);
      this._setState('playing');
    };

    pc.onicecandidate = (ev) => {
      const candidate = ev.candidate ? ev.candidate.toJSON().candidate : '';
      this._send({ type: 'webrtc/candidate', value: candidate });
    };

    pc.onconnectionstatechange = () => {
      if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
        this._setState('closed');
        this._scheduleRetry();
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    this._send({ type: 'webrtc/offer', value: pc.localDescription.sdp });
  }

  /**
   * Ask the browser to hold as little jitter buffer as possible.
   *
   * `RTCRtpReceiver.jitterBufferTarget` is the spec'd control for inbound
   * media (Chrome 111+); it is what actually removes the ~0.3-0.5 s of
   * default buffering. The older `playoutDelayHint` only applies to senders,
   * so it is useless for a receive-only stream and is not used.
   *
   * A tiny target (30 ms) is safe on a wired/Wi-Fi LAN where packet jitter is
   * a few ms; it would be reckless over the internet or a weak radio link.
   */
  _applyJitterTarget(pc) {
    const seconds = this.jitterBufferTarget;
    if (seconds === null || seconds === undefined) return;
    for (const rx of pc.getReceivers()) {
      try {
        if ('jitterBufferTarget' in rx) {
          rx.jitterBufferTarget = seconds * 1000; // milliseconds
        }
      } catch (e) {
        /* non-fatal: leave the browser default */
      }
    }
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  /** Pull inbound-rtp stats once a second and derive the numbers we show. */
  async _sample() {
    const pc = this.pc;
    if (!pc || !this.video || !this.video.videoWidth) { return; }
    let report;
    try { report = await pc.getStats(); } catch (e) { return; }

    let inbound = null, pair = null, remoteIn = null;
    report.forEach((s) => {
      if (s.type === 'inbound-rtp' && s.kind === 'video') inbound = s;
      if (s.type === 'candidate-pair' && s.state === 'succeeded') pair = s;
      if (s.type === 'remote-inbound-rtp' && s.kind === 'video') remoteIn = s;
    });
    if (!inbound) return;

    const now = performance.now() / 1000;
    const prev = this._last;
    if (prev.t && now > prev.t) {
      const dt = now - prev.t;
      const df = (inbound.framesDecoded || 0) - prev.frames;
      const db = (inbound.bytesReceived || 0) - prev.bytes;
      if (df > 0) this._fps = df / dt;
      if (db >= 0) this._bitrate = (db * 8) / dt / 1e6;
      const dd = (inbound.totalDecodeTime || 0) - prev.decode;
      if (df > 0) this._decodeMs = (dd / df) * 1000;

      // Packets per second, for the decoder panel's "Packets/sec out".
      const dp = (inbound.packetsReceived || 0) - (prev.packets || 0);
      if (dp >= 0) this._pps = dp / dt;

      // Longest run of consecutive lost packets since the last sample. WebRTC
      // reports only a running total, so this is the delta, which is the
      // closest thing to a "gap" that is actually available.
      const dl = (inbound.packetsLost || 0) - (prev.lost || 0);
      if (dl > 0) {
        this._maxGapPkts = Math.max(this._maxGapPkts || 0, dl);
        this._maxGapMs = Math.max(this._maxGapMs || 0, (dl / Math.max(1, this._pps || 1)) * 1000);
      }
    }
    prev.t = now;
    prev.frames = inbound.framesDecoded || 0;
    prev.bytes = inbound.bytesReceived || 0;
    prev.decode = inbound.totalDecodeTime || 0;
    prev.packets = inbound.packetsReceived || 0;
    prev.lost = inbound.packetsLost || 0;

    // Average jitter-buffer occupancy: this IS the browser-side latency.
    if (inbound.jitterBufferEmittedCount) {
      this._jitterMs =
        (inbound.jitterBufferDelay / inbound.jitterBufferEmittedCount) * 1000;
    }
    this._lost = inbound.packetsLost || 0;
    if (pair && pair.currentRoundTripTime !== undefined) {
      this._rttMs = pair.currentRoundTripTime * 1000;
    }

    this.stats = {
      fps: this._fps,
      bitrate: this._bitrate,
      decodeMs: this._decodeMs,
      jitterMs: this._jitterMs,
      rttMs: this._rttMs,
      lost: this._lost,
      width: inbound.frameWidth || 0,
      height: inbound.frameHeight || 0,
      frames: inbound.framesDecoded || 0,
      // Total glass-to-glass estimate: jitter buffer + decode.
      // (Camera + software encode are measured separately on the Pi.)
      clientMs: this._jitterMs + this._decodeMs,

      // ---- extra fields for the decoder panel ----
      packetsPerSec: this._pps || null,
      nack: inbound.nackCount ?? null,
      fir: inbound.firCount ?? null,
      pli: inbound.pliCount ?? null,
      dropped: inbound.framesDropped ?? null,
      decoded: inbound.framesDecoded ?? null,
      received: inbound.framesReceived ?? null,
      jitter: inbound.jitter !== undefined ? inbound.jitter * 1000 : null,
      packetsReceived: inbound.packetsReceived ?? null,
      retransmitted: inbound.retransmittedPacketsReceived ?? null,
      // Reported by the *sender* (the Pi) via RTCP, so often unavailable.
      remoteLost: remoteIn ? (remoteIn.packetsLost ?? null) : null,
      maxLostGapPkts: this._maxGapPkts || 0,
      maxLostGapMs: this._maxGapMs || 0,
    };
  }

  _teardown() {
    clearTimeout(this._retryTimer);
    if (this.pc) {
      try { this.pc.close(); } catch (e) {}
      this.pc = null;
    }
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onclose = null;
      try { ws.close(); } catch (e) {}
    }
  }

  destroy() {
    this._teardown();
    clearInterval(this._statsTimer);
  }
}
