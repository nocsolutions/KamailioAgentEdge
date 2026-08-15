// Headless WebRTC agent for the KamailioAgentEdge: registers over WSS, auto-answers
// an inbound INVITE with a DTLS-SRTP/PCMU answer (via werift), sends a PCMU tone and
// counts received RTP. Proves the edge's WebRTC media bridge end to end.
//
//   node agent.js <wss-url> <ext> <password> [realm]
const WebSocket = require('ws');
const crypto = require('crypto');
const { RTCPeerConnection, RTCRtpCodecParameters, MediaStreamTrack, RtpPacket } = require('werift');

const [,, URL, EXT, PASS, REALM='avatar.tech'] = process.argv;
if (!URL || !EXT || !PASS) { console.error('args: <wss-url> <ext> <pass> [realm]'); process.exit(2); }

const md5 = s => crypto.createHash('md5').update(s).digest('hex');
const rand = (n=12) => crypto.randomBytes(n).toString('hex');
const host = rand(6) + '.invalid';
const fromtag = rand(6);
const callidReg = rand(10);
let regCseq = 0;

// ---- digest ----
function digest(h, method, uri) {
  const ha1 = md5(`${EXT}:${h.realm}:${PASS}`), ha2 = md5(`${method}:${uri}`);
  if (h.qop) { const nc='00000001', cn=rand(8);
    const r = md5(`${ha1}:${h.nonce}:${nc}:${cn}:auth:${ha2}`);
    return `Digest username="${EXT}", realm="${h.realm}", nonce="${h.nonce}", uri="${uri}", response="${r}", qop=auth, nc=${nc}, cnonce="${cn}"`; }
  const r = md5(`${ha1}:${h.nonce}:${ha2}`);
  return `Digest username="${EXT}", realm="${h.realm}", nonce="${h.nonce}", uri="${uri}", response="${r}"`;
}
const parseAuth = l => { const g=k=>(l.match(new RegExp(k+'="?([^",]+)"?','i'))||[])[1];
  return {realm:g('realm'),nonce:g('nonce'),qop:g('qop')}; };
const hdr = (m,n) => (m.split('\r\n').find(l=>new RegExp('^'+n+':','i').test(l))||'').replace(new RegExp('^'+n+':\\s*','i'),'').trim();

process.on('uncaughtException', e => console.log('>> uncaught:', e.message));
const ws = new WebSocket(URL, 'sip', { rejectUnauthorized:false });
let regAuthTried = false;

function sendRegister(auth) {
  regCseq++;
  const uri = `sip:${REALM}`, branch='z9hG4bK'+rand(6);
  const L = [`REGISTER ${uri} SIP/2.0`,`Via: SIP/2.0/WS ${host};branch=${branch};rport`,
    `Max-Forwards: 70`,`From: <sip:${EXT}@${REALM}>;tag=${fromtag}`,`To: <sip:${EXT}@${REALM}>`,
    `Call-ID: ${callidReg}`,`CSeq: ${regCseq} REGISTER`,
    `Contact: <sip:${EXT}@${host};transport=ws>;expires=300`,`Supported: outbound, path`,
    `User-Agent: coregears-agent`,`Content-Length: 0`,``,``];
  if (auth) L[L.length-3] = `Authorization: ${digest(auth,'REGISTER',uri)}\r\nContent-Length: 0`;
  ws.send(L.join('\r\n'));
}

// ---- media: answer an inbound INVITE with werift ----
let rxCount = 0, txCount = 0;
async function handleInvite(msg) {
  let offerSdp = msg.split('\r\n\r\n').slice(1).join('\r\n\r\n');
  // werift's SDP parser throws on a bare "a=rtcp:<port>" (RFC 3605 without the
  // optional address); rtcp-mux makes it redundant, so drop it.
  offerSdp = offerSdp.split(/\r?\n/).filter(l => !/^a=rtcp:\d+\s*$/.test(l)).join('\r\n');
  const via = hdr(msg,'Via'), from = hdr(msg,'From'), to = hdr(msg,'To');
  const callid = hdr(msg,'Call-ID'), cseq = hdr(msg,'CSeq');
  // RFC 3261: a UAS MUST copy every Record-Route header, in order, into the 2xx.
  // Without it the caller has no route set and must send its ACK straight to our
  // Contact (a ws "<rand>.invalid" host it cannot resolve) - Asterisk then never
  // ACKs, the channel stays Down and no media is generated.
  const recordRoutes = msg.split('\r\n').filter(l => /^Record-Route:/i.test(l));

  // TURN_URL/TURN_USER/TURN_PASS + RELAY=1 reproduce the browser's ICE config
  // (iceTransportPolicy:"relay" against the fleet coturn). Without them we use
  // direct candidates and let rtpengine learn the peer.
  const iceServers = process.env.TURN_URL
    ? [{ urls: process.env.TURN_URL, username: process.env.TURN_USER, credential: process.env.TURN_PASS }]
    : [];
  const pcCfg = {
    codecs: { audio: [ new RTCRtpCodecParameters({ mimeType:'audio/PCMU', clockRate:8000, payloadType:0 }) ] },
    iceServers,
  };
  if (process.env.RELAY === '1') pcCfg.iceTransportPolicy = 'relay';
  console.log('>> ice config:', JSON.stringify({ iceServers: iceServers.map(s => s.urls), policy: pcCfg.iceTransportPolicy || 'all' }));
  const pc = new RTCPeerConnection(pcCfg);

  const track = new MediaStreamTrack({ kind:'audio' });
  const tr = pc.addTransceiver(track, { direction:'sendrecv' });

  // werift uses .subscribe events (not DOM on<event>=). Count inbound RTP.
  pc.onTrack.subscribe((track) => {
    track.onReceiveRtp.subscribe((rtp) => { rxCount++; if (rxCount===1) console.log('>> first RTP received from edge'); });
  });

  pc.iceConnectionStateChange.subscribe(s => console.log('>> ice:', s));
  pc.connectionStateChange.subscribe(s => console.log('>> pc:', s));
  await pc.setRemoteDescription({ type:"offer", sdp: offerSdp });
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  // wait for ICE gathering to finish so the answer carries candidates
  await new Promise(res => {
    if (pc.iceGatheringState==='complete') return res();
    pc.iceGatheringStateChange.subscribe(s => { if (s==='complete') res(); });
    setTimeout(res, 5000); });

  const ans = pc.localDescription.sdp;
  const L = [`SIP/2.0 200 OK`,`Via: ${via}`, ...recordRoutes,
    `From: ${from}`,`To: ${to};tag=${rand(6)}`,
    `Call-ID: ${callid}`,`CSeq: ${cseq}`,`Contact: <sip:${EXT}@${host};transport=ws>`,
    `Content-Type: application/sdp`,`Content-Length: ${Buffer.byteLength(ans)}`,``,ans];
  ws.send(L.join('\r\n'));
  console.log('>> sent 200 OK with DTLS-SRTP answer, connection state:', pc.connectionState);

  pc.onconnectionstatechange = () => console.log('>> pc state:', pc.connectionState);

  // send a PCMU tone (silence 0xFF) ~50pps once media path is up
  let seq = crypto.randomBytes(2).readUInt16BE(0), ts = 0;
  const ssrc = crypto.randomBytes(4).readUInt32BE(0);
  let sendErr = null;
  const iv = setInterval(() => {
    if (pc.connectionState !== 'connected') return;
    // build a raw PCMU RTP packet (12-byte header + 160B payload); werift's
    // writeRtp deSerializes a Buffer into a proper RtpPacket.
    const h = Buffer.alloc(12);
    h[0] = 0x80; h[1] = 0x00;                 // V=2, PT=0 (PCMU)
    h.writeUInt16BE(seq++ & 0xffff, 2);
    h.writeUInt32BE(ts >>> 0, 4);
    h.writeUInt32BE(ssrc >>> 0, 8);
    const pkt = Buffer.concat([h, Buffer.alloc(160, 0xFF)]);
    try { track.writeRtp(pkt); txCount++; } catch(e){ if(!sendErr){sendErr=e.message;console.log('>> writeRtp error:', e.message);} }
    ts = (ts + 160) >>> 0;
  }, 20);

  const rep = setInterval(() => console.log(`MEDIA: sent=${txCount} received=${rxCount} pc=${pc.connectionState}`), 2000);
  global.__report = () => console.log(`MEDIA FINAL: sent=${txCount} received=${rxCount} pc=${pc.connectionState} sendErr=${sendErr}`);
  setTimeout(() => { global.__report(); clearInterval(iv); clearInterval(rep); }, 20000);
}

ws.on('open', () => { console.log('WS connected'); sendRegister(null); });
ws.on('error', e => { console.error('WS error:', e.message); process.exit(1); });
ws.on('message', async (data) => {
  const msg = data.toString();
  const first = msg.split('\r\n')[0];
  if (/^SIP\/2\.0 401/.test(msg) && !regAuthTried) { regAuthTried=true; sendRegister(parseAuth(hdr(msg,'WWW-Authenticate'))); return; }
  if (/^SIP\/2\.0 200/.test(msg) && /REGISTER/.test(msg)) { console.log('REGISTERED (WSS). READY - waiting for INVITE'); return; }
  if (/^INVITE /.test(first)) {
    console.log('<< INVITE received');
    // 100 Trying then answer
    const via=hdr(msg,'Via'),from=hdr(msg,'From'),to=hdr(msg,'To'),callid=hdr(msg,'Call-ID'),cseq=hdr(msg,'CSeq');
    ws.send([`SIP/2.0 100 Trying`,`Via: ${via}`,`From: ${from}`,`To: ${to}`,`Call-ID: ${callid}`,`CSeq: ${cseq}`,`Content-Length: 0`,``,``].join('\r\n'));
    try { await handleInvite(msg); } catch(e){ console.error('answer error:', e.stack || e); }
    return;
  }
  if (/^ACK /.test(first)) { console.log('<< ACK - call established'); return; }
  if (/^BYE /.test(first)) { const via=hdr(msg,'Via'),from=hdr(msg,'From'),to=hdr(msg,'To'),callid=hdr(msg,'Call-ID'),cseq=hdr(msg,'CSeq');
    ws.send([`SIP/2.0 200 OK`,`Via: ${via}`,`From: ${from}`,`To: ${to}`,`Call-ID: ${callid}`,`CSeq: ${cseq}`,`Content-Length: 0`,``,``].join('\r\n')); console.log('<< BYE'); return; }
});
