#!/usr/bin/env node
/**
 * 电话会议组网（Mesh）逻辑验证 —— 不需要浏览器、不需要麦克风。
 *
 * 做法：把真实的 static/js/voice-room.js 放进 node:vm 沙箱跑，注入一套
 *   「假 DOM + 假 WebSocket + 假 RTCPeerConnection」，再按 deploy/pm-signal.py 的
 *   协议实现一个假信令服务器；然后让 N 个虚拟客户端几乎同时「加入会议」，
 *   断言最终 N 人两两之间都建成了一条连接。
 *
 * 关键断言：
 *   1) GLARE = 0      —— 任意一对不会双方同时发 offer（12 人 = 66 对，靠 uid 大小定唯一发起方）
 *   2) ICE_BEFORE_REMOTE = 0 —— ICE 候选先于 SDP 到达时，必须被缓冲后补灌，不能直接丢
 *   3) 全部 PC 变成 connected —— N×(N-1) 条，全互联
 *
 * 用法：
 *   node deploy/verify-mesh.js                 # 验证当前代码（应全过）
 *   node deploy/verify-mesh.js --legacy        # 把「谁发起」还原成旧逻辑，复现 glare（应失败）
 *   node deploy/verify-mesh.js --n=30          # 指定人数（默认 12）
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const argv = process.argv.slice(2);
const LEGACY = argv.includes('--legacy');
const nArg = argv.find((a) => a.startsWith('--n='));
const N = nArg ? parseInt(nArg.split('=')[1], 10) : 12;
const SRC = argv.find((a) => !a.startsWith('--')) || path.join(__dirname, '..', 'static', 'js', 'voice-room.js');

let src = fs.readFileSync(SRC, 'utf8');
if (LEGACY) {
    const before = src;
    // 复现修改前的逻辑：所有人都以「发起方」身份建连 → 双方同时 offer
    src = src.replace(/createPeer\(uid, shouldInitiate\(uid\)\)/g, 'createPeer(uid, true)');
    if (src === before) {
        console.error('无法注入 legacy 逻辑（找不到 createPeer(uid, shouldInitiate(uid))），请检查源码');
        process.exit(2);
    }
}

const MEMBERS = [];
for (let i = 1; i <= N; i++) MEMBERS.push({ id: i, name: '成员' + i, is_host: i === 1 });

const STATS = { pcCreated: 0, connected: 0, glare: 0, iceBeforeRemote: 0, iceArrivedFirst: 0, answerWithoutOffer: 0 };
const ALERTS = [];

// ----------------------------------------------------------------- 假信令服务器
const server = {
    members: new Map(),   // uid -> fake ws
    seen: new Map(),      // "from->to" -> 是否已出过 SDP
    join(uid, ws) {
        this.members.set(uid, ws);
        const list = [...this.members.keys()].map((u) => ({ uid: u, name: '成员' + u }));
        ws.deliver({ type: 'room_state', members: list });
        for (const [u, w] of this.members) if (u !== uid) w.deliver({ type: 'room_state', members: list });
    },
    relay(fromUid, msg) {
        const t = msg.type;
        if (t !== 'webrtc_offer' && t !== 'webrtc_answer' && t !== 'webrtc_ice') return;
        const to = Number(msg.to_uid);
        const w = this.members.get(to);
        if (!w) return;
        const key = fromUid + '->' + to;
        if (t === 'webrtc_ice') {
            if (!this.seen.get(key)) STATS.iceArrivedFirst++;
        } else {
            this.seen.set(key, true);
        }
        const out = Object.assign({}, msg, { from_uid: fromUid });
        delete out.to_uid;
        w.deliver(out);
    },
};

// ----------------------------------------------------- 假 RTCPeerConnection
const ALL_PCS = [];
class FakePC {
    constructor() {
        this.pcState = 'new';       // new | have-local-offer | have-remote-offer | stable | closed
        this.localSet = false;
        this.remoteSet = false;
        this.remoteDesc = null;
        // 真实 RTCPeerConnection 的这两个属性：setLocal/RemoteDescription 之后才会有值，
        // 前端 handleIce 就是靠 pc.remoteDescription 判断「远端描述是否就位」的，必须照实模拟。
        this.localDescription = null;
        this.remoteDescription = null;
        this.connectionState = 'new';
        this.onicecandidate = null;
        this.ontrack = null;
        this.onconnectionstatechange = null;
        this.iceSent = 0;
        this.iceRecv = 0;
        ALL_PCS.push(this);
        STATS.pcCreated++;
    }
    async createOffer() { return { type: 'offer', sdp: 'v=0 offer' }; }
    async createAnswer() { return { type: 'answer', sdp: 'v=0 answer' }; }
    async setLocalDescription(desc) {
        if (desc.type === 'offer') { this.pcState = 'have-local-offer'; this.localSet = true; }
        else if (desc.type === 'answer') { this.pcState = 'stable'; this.localSet = true; }
        else if (desc.type === 'rollback') { this.pcState = 'stable'; this.localSet = false; }
        this.localDescription = desc.type === 'rollback' ? null : desc;
        this._maybeConnected();
        // 真实浏览器是 setLocalDescription 之后异步抛 candidate。这里故意「同步」抛，
        // 制造「候选先于 SDP 发出」的最坏时序，用来验证接收端的缓冲逻辑真的生效。
        if (this.onicecandidate) {
            this.iceSent++;
            this.onicecandidate({ candidate: { candidate: 'cand-' + this.iceSent, sdpMid: '0', sdpMLineIndex: 0 } });
        }
    }
    async setRemoteDescription(desc) {
        if (desc.type === 'offer') {
            if (this.pcState === 'have-local-offer') { STATS.glare++; throw new Error('glare: 已有本地 offer 又收到 offer'); }
            this.pcState = 'have-remote-offer';
            this.remoteSet = true;
            this.remoteDesc = desc;
        } else if (desc.type === 'answer') {
            if (this.pcState !== 'have-local-offer') { STATS.answerWithoutOffer++; throw new Error('answer 找不到对应的本地 offer'); }
            this.pcState = 'stable';
            this.remoteSet = true;
            this.remoteDesc = desc;
        }
        this.remoteDescription = desc;
        this._maybeConnected();
    }
    async addIceCandidate() {
        // 真实浏览器：远端描述没设就 addIceCandidate → 抛错
        if (!this.remoteSet || !this.remoteDesc) { STATS.iceBeforeRemote++; throw new Error('远端描述尚未设置'); }
        this.iceRecv++;
    }
    addTrack() { return { setParameters: async () => {} }; }
    close() { this.pcState = 'closed'; this.connectionState = 'closed'; if (this.onconnectionstatechange) this.onconnectionstatechange(); }
    _maybeConnected() {
        if (this.localSet && this.remoteSet && this.pcState === 'stable' && this.connectionState !== 'connected') {
            this.connectionState = 'connected';
            STATS.connected++;
            if (this.onconnectionstatechange) this.onconnectionstatechange();
        }
    }
}
class FakeRTCSessionDescription { constructor(d) { Object.assign(this, d); } }
class FakeRTCIceCandidate { constructor(c) { Object.assign(this, c); } }

// ------------------------------------------------------------------ 假 DOM
const STATIC_IDS = new Set([
    'voice-member-list', 'remote-audios', 'btn-call', 'btn-mic', 'btn-follow', 'btn-download',
    'btn-revoke-presenter', 'btn-apply-presenter', 'edit-mode', 'doc-frame', 'doc-empty-tip',
    'doc-current-name', 'current-presenter-name', 'apply-banner', 'doc-panel', 'btn-doc-panel',
    'btn-doc-panel-close', 'doc-new-name', 'doc-new-ext', 'btn-doc-create', 'file-upload',
    'btn-upload', 'doc-panel-msg', 'doc-panel-table', 'doc-frame-wrap',
]);

// 每个客户端一套独立的 DOM（元素注册表不能跨客户端共享，否则 addEventListener 会串台）
function makeDom() {
    const registry = {};
    function makeEl(id) {
        return {
            id, dataset: {}, style: {}, children: [], value: '', disabled: false, checked: false,
            innerHTML: '', textContent: '',
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            addEventListener() {}, removeEventListener() {},
            appendChild(c) { this.children.push(c); if (c && c.id) registry[c.id] = c; return c; },
            remove() {}, play() { return Promise.resolve(); },
            querySelector() { return null; }, querySelectorAll() { return []; },
            focus() {}, click() {}, setAttribute() {}, getAttribute() { return null; },
        };
    }
    return {
        makeEl,
        getElementById(id) {
            if (registry[id]) return registry[id];
            if (STATIC_IDS.has(id)) { registry[id] = makeEl(id); return registry[id]; }
            return null;
        },
    };
}

function createClient(uid) {
    const dom = makeDom();
    const handlers = [];
    const sandbox = {
        console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
        setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
        alert: (m) => { ALERTS.push('uid' + uid + ': ' + m); },
        // 假麦克风：getUserMedia 只在安全上下文可用，这里直接给一条音轨
        navigator: {
            mediaDevices: {
                getUserMedia: async () => {
                    const track = { kind: 'audio', enabled: true, stop() {} };
                    return { getTracks: () => [track], getAudioTracks: () => [track] };
                },
            },
        },
        VOICE_STATE: { projectId: 1, roomId: 1, isHost: uid === 1, currentUserId: uid, currentPresenterId: null },
        SIGNAL_WS: 'ws://fake',
        WebSocket: class {
            constructor(url) {
                this.url = url; this.readyState = 1;
                const self = this;
                setTimeout(() => { if (self.onopen) self.onopen(); server.join(uid, self); }, 0);
            }
            send(str) { server.relay(uid, JSON.parse(str)); }
            close() { this.readyState = 3; if (this.onclose) this.onclose(); }
            deliver(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
        },
        RTCPeerConnection: FakePC,
        RTCSessionDescription: FakeRTCSessionDescription,
        RTCIceCandidate: FakeRTCIceCandidate,
        fetch: (url) => {
            if (url.includes('/voice/token')) {
                return Promise.resolve({ json: () => Promise.resolve({ token: 'tok-' + uid, signal_ws: 'ws://fake', room: {} }) });
            }
            if (url.includes('/voice/members')) {
                return Promise.resolve({ json: () => Promise.resolve({ members: MEMBERS }) });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') });
        },
        DOMParser: class { parseFromString() { return { querySelector: () => null }; } },
        document: {
            getElementById(id) {
                const el = dom.getElementById(id);
                if (el && !el.__patched) {
                    el.__patched = true;
                    el.addEventListener = (ev, fn) => handlers.push({ id, ev, fn });
                }
                return el;
            },
            createElement: (tag) => dom.makeEl(tag),
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener() {},
            body: dom.makeEl('body'),
        },
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    return { sandbox, handlers };
}

const clients = [];
for (let uid = 1; uid <= N; uid++) {
    const c = createClient(uid);
    vm.runInContext(src, c.sandbox, { filename: 'voice-room.js' });
    clients.push(c);
}

// 几乎同一时刻「加入会议」
let missing = 0;
for (const c of clients) {
    const call = c.handlers.find((h) => h.id === 'btn-call' && h.ev === 'click');
    if (!call) missing++;
    else call.fn();
}
if (missing) { console.error(`有 ${missing} 个客户端没抓到 btn-call 的 click 回调，脚本加载可能失败`); process.exit(2); }

setTimeout(() => {
    const expectedPcs = N * (N - 1);            // 每一对 2 条 PC
    let fail = 0;
    const check = (cond, msg) => {
        console.log((cond ? '  [OK]   ' : '  [FAIL] ') + msg);
        if (!cond) fail++;
    };
    console.log(`== Mesh 组网验证（${N} 人并发入会${LEGACY ? '，已注入 legacy 旧逻辑' : ''}）==`);
    console.log('');
    check(STATS.pcCreated === expectedPcs, `RTCPeerConnection 数 = ${STATS.pcCreated}（期望 ${expectedPcs} = ${N}×${N - 1}）`);
    check(STATS.connected === expectedPcs, `connected 的 PC 数 = ${STATS.connected}（期望 ${expectedPcs}，全互联）`);
    check(STATS.glare === 0, `GLARE（一对里双方同时 offer）= ${STATS.glare}（期望 0）`);
    check(STATS.iceBeforeRemote === 0, `未缓冲就直接 addIceCandidate 被拒 = ${STATS.iceBeforeRemote}（期望 0，说明 ICE 缓冲生效）`);
    check(STATS.answerWithoutOffer === 0, `找不到对应 offer 的 answer = ${STATS.answerWithoutOffer}（期望 0）`);
    console.log('');
    console.log(`  参考：ICE 先于 SDP 到达的次数 = ${STATS.iceArrivedFirst}（本用例故意制造的乱序，用来确认缓冲路径确实被走到）`);
    if (ALERTS.length) { console.log('  ⚠ 页面弹过 alert：'); ALERTS.forEach((a) => console.log('      ' + a)); }
    console.log('');
    if (fail === 0) console.log('==== Mesh 组网验证通过（FAIL=0）====');
    else console.log(`==== 存在 ${fail} 项 FAIL ====`);
    process.exit(fail === 0 ? 0 : 1);
}, 500);
