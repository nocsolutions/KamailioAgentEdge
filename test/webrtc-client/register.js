// Minimal SIP-over-WebSocket (RFC 7118) REGISTER client with digest+qop auth.
// Proves the edge's ksr_xhttp_event WS handshake + WS registrar path.
//
//   node register.js <wss-url> <ext> <password> <realm>
const WebSocket = require('ws');
const crypto = require('crypto');

const [,, URL, EXT, PASS, REALM='avatar.tech'] = process.argv;
if (!URL || !EXT || !PASS) { console.error('args: <wss-url> <ext> <pass> [realm]'); process.exit(2); }

const md5 = s => crypto.createHash('md5').update(s).digest('hex');
const rand = (n=12) => crypto.randomBytes(n).toString('hex');

const host = rand(6) + '.invalid';           // WS clients use a random sent-by
const callid = rand(10);
const fromtag = rand(6);
let cseq = 1;

function digest(h, method, uri) {
  const ha1 = md5(`${EXT}:${h.realm}:${PASS}`);
  const ha2 = md5(`${method}:${uri}`);
  let resp, extra = '';
  if (h.qop) {
    const nc = '00000001', cnonce = rand(8);
    resp = md5(`${ha1}:${h.nonce}:${nc}:${cnonce}:auth:${ha2}`);
    extra = `, qop=auth, nc=${nc}, cnonce="${cnonce}"`;
  } else {
    resp = md5(`${ha1}:${h.nonce}:${ha2}`);
  }
  return `Digest username="${EXT}", realm="${h.realm}", nonce="${h.nonce}", `
       + `uri="${uri}", response="${resp}"${h.opaque?`, opaque="${h.opaque}"`:''}${extra}`;
}

function parseAuth(line) {
  const g = k => (line.match(new RegExp(k+'="?([^",]+)"?','i'))||[])[1];
  return { realm: g('realm'), nonce: g('nonce'), qop: g('qop'), opaque: g('opaque') };
}

function register(ws, auth) {
  const uri = `sip:${REALM}`;
  const branch = 'z9hG4bK' + rand(6);
  const lines = [
    `REGISTER ${uri} SIP/2.0`,
    `Via: SIP/2.0/WS ${host};branch=${branch};rport`,
    `Max-Forwards: 70`,
    `From: <sip:${EXT}@${REALM}>;tag=${fromtag}`,
    `To: <sip:${EXT}@${REALM}>`,
    `Call-ID: ${callid}`,
    `CSeq: ${cseq} REGISTER`,
    `Contact: <sip:${EXT}@${host};transport=ws>;expires=300`,
    `Supported: outbound, path`,
    `User-Agent: coregears-test`,
  ];
  if (auth) lines.push(`Authorization: ${digest(auth, 'REGISTER', uri)}`);
  lines.push('Content-Length: 0', '', '');
  ws.send(lines.join('\r\n'));
}

const ws = new WebSocket(URL, 'sip', { rejectUnauthorized: false });
let challenged = false;
const timer = setTimeout(() => { console.error('TIMEOUT (no final response)'); process.exit(1); }, 12000);

ws.on('open', () => { console.log('WS connected, sending REGISTER'); cseq=1; register(ws, null); });
ws.on('error', e => { console.error('WS error:', e.message); process.exit(1); });
ws.on('message', data => {
  const msg = data.toString();
  const status = (msg.match(/^SIP\/2\.0 (\d{3}) (.*)/)||[])[0];
  console.log('<<', status || msg.split('\r\n')[0]);
  if (/^SIP\/2\.0 401/.test(msg) && !challenged) {
    challenged = true;
    const wl = msg.split('\r\n').find(l => /^WWW-Authenticate:/i.test(l)) || '';
    cseq = 2;
    register(ws, parseAuth(wl));
  } else if (/^SIP\/2\.0 200/.test(msg)) {
    console.log('REGISTERED OK over WSS');
    clearTimeout(timer);
    setTimeout(() => process.exit(0), 500);
  } else if (/^SIP\/2\.0 [45]/.test(msg)) {
    console.error('REGISTER failed'); clearTimeout(timer); process.exit(1);
  }
});
