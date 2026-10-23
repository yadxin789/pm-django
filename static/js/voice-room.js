// 电话会议房间前端：信令 + Mesh P2P WebRTC + OnlyOffice 演示（对齐参考 Laravel voice.blade）
(function () {
    const $ = (id) => document.getElementById(id);
    let ws = null;
    let allMembers = [];
    let onlineUids = new Set();
    let docEditor = null;
    let followingPresenter = true;
    let localStream = null;
    let currentDocId = null;
    let currentDownloadUrl = null;
    let lastSentScroll = 0;
    const peers = {};
    const pendingIce = {};    // uid -> 还没能 addIceCandidate 的候选（等远端描述就位后再补灌）
    const retryCount = {};    // uid -> 已重试次数
    const MAX_RETRY = 3;

    // 纯语音 + 同网段：host candidate 直连即可，不需要 STUN/TURN。
    // 12 人时每人 11 条 RTCPeerConnection，Opus 上行约 11×40kbps ≈ 0.4Mbps，局域网完全够用。
    const rtcConfig = { iceServers: [] };

    // 「谁发起」必须每对唯一，否则双方同时 offer（glare）会互相打断 —— 12 人 = 66 对，必然踩到；
    // 规则：uid 大的一方发起，uid 小的一方只等 offer。两边都按 room_state 里的 uid 判断，结论一致。
    function shouldInitiate(uid) {
        return Number(VOICE_STATE.currentUserId) > Number(uid);
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function sendSignal(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

    async function connectSignal() {
        try {
            const r = await fetch('/project/' + VOICE_STATE.projectId + '/voice/token').then(r => r.json());
            if (r.error) { alert(r.error); return; }
            window._voiceConf = r;
            const url = (r.signal_ws || SIGNAL_WS) + '?token=' + encodeURIComponent(r.token);
            ws = new WebSocket(url);
            ws.onopen = () => console.log('[voice] signal ws open');
            ws.onclose = () => console.log('[voice] signal ws closed');
            ws.onerror = (e) => console.error('[voice] ws error', e);
            ws.onmessage = (e) => { try { handleSignal(JSON.parse(e.data)); } catch (err) { console.error(err); } };
        } catch (e) { alert('连接信令失败: ' + e.message); }
    }

    function handleSignal(msg) {
        switch (msg.type) {
            case 'room_state':
                onlineUids = new Set((msg.members || []).map(m => Number(m.uid)));
                renderMembers();
                if (localStream) {
                    (msg.members || []).forEach(m => {
                        const uid = Number(m.uid);
                        if (uid === Number(VOICE_STATE.currentUserId) || peers[uid]) return;
                        retryCount[uid] = 0;
                        createPeer(uid, shouldInitiate(uid));
                    });
                    // 已离线的人：关掉连接、清掉远端 audio，别留一堆僵尸节点
                    Object.keys(peers).forEach(k => { if (!onlineUids.has(Number(k))) closePeer(Number(k)); });
                }
                break;
            case 'presenter_changed':
                VOICE_STATE.currentPresenterId = msg.presenter_uid;
                updatePresenterLabel(); renderMembers(); break;
            case 'presenter_apply':
                if (VOICE_STATE.isHost) showApplyBanner(msg.from_uid, msg.from_name); break;
            case 'doc_selected': loadDoc(msg.doc_id); break;
            case 'slide_sync': if (followingPresenter) applySlideSync(msg); break;
            case 'webrtc_offer': handleOffer(msg); break;
            case 'webrtc_answer': handleAnswer(msg); break;
            case 'webrtc_ice': handleIce(msg); break;
            case 'error': console.error('[voice]', msg.reason); break;
        }
    }

    function closePeer(uid) {
        const pc = peers[uid];
        if (pc) { try { pc.close(); } catch (e) {} delete peers[uid]; }
        delete pendingIce[uid];
        const audio = $('audio-' + uid);
        if (audio) { try { audio.srcObject = null; } catch (e) {} audio.remove(); }
    }

    // 连不上就重试；只有发起方重试，避免双方同时重试又撞车
    function scheduleRetry(uid) {
        const tries = retryCount[uid] || 0;
        if (tries >= MAX_RETRY) { console.warn('[voice] peer ' + uid + ' 重试次数用尽，放弃'); closePeer(uid); return; }
        retryCount[uid] = tries + 1;
        console.warn('[voice] peer ' + uid + ' 连接失败，第 ' + (tries + 1) + ' 次重试');
        closePeer(uid);
        setTimeout(() => {
            if (onlineUids.has(uid) && localStream && !peers[uid]) createPeer(uid, shouldInitiate(uid));
        }, 1000 * (tries + 1));
    }

    async function createPeer(uid, isInitiator) {
        if (peers[uid]) return peers[uid];
        const pc = new RTCPeerConnection(rtcConfig);
        peers[uid] = pc;
        if (!pendingIce[uid]) pendingIce[uid] = [];
        localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
        pc.ontrack = (e) => {
            let audio = $('audio-' + uid);
            if (!audio) { audio = document.createElement('audio'); audio.id = 'audio-' + uid; audio.autoplay = true; $('remote-audios').appendChild(audio); }
            audio.srcObject = e.streams[0];
            const p = audio.play(); if (p && p.catch) p.catch(() => {});
        };
        pc.onicecandidate = (e) => { if (e.candidate) sendSignal({ type: 'webrtc_ice', to_uid: uid, candidate: e.candidate }); };
        pc.onconnectionstatechange = () => {
            if (pc.connectionState === 'connected') { retryCount[uid] = 0; }
            else if (pc.connectionState === 'failed') { scheduleRetry(uid); }
            else if (pc.connectionState === 'closed') { closePeer(uid); }
        };
        if (isInitiator) {
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            sendSignal({ type: 'webrtc_offer', to_uid: uid, sdp: pc.localDescription });
        }
        return pc;
    }

    // 远端描述就位前到达的 ICE 候选会被 addIceCandidate 拒掉，先存着，就位后补灌
    async function flushIce(uid) {
        const pc = peers[uid];
        const queued = pendingIce[uid] || [];
        pendingIce[uid] = [];
        if (!pc) return;
        for (const candidate of queued) {
            try { await pc.addIceCandidate(new RTCIceCandidate(candidate)); }
            catch (e) { console.warn('[voice] 补灌 ICE 失败', e); }
        }
    }

    async function handleOffer(msg) {
        const uid = Number(msg.from_uid);
        const pc = await createPeer(uid, false);
        try {
            await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
        } catch (e) {
            // 兜底：万一双方还是同时 offer 了，回滚本地描述再收一次，别让这一对彻底废掉
            console.warn('[voice] setRemoteDescription(offer) 失败，回滚重试', e);
            try { await pc.setLocalDescription({ type: 'rollback' }); await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp)); }
            catch (e2) { console.error('[voice] 回滚仍失败，放弃这一对', e2); return; }
        }
        await flushIce(uid);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        sendSignal({ type: 'webrtc_answer', to_uid: uid, sdp: pc.localDescription });
    }

    async function handleAnswer(msg) {
        const uid = Number(msg.from_uid);
        const pc = peers[uid];
        if (!pc) return;
        try { await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp)); }
        catch (e) { console.warn('[voice] setRemoteDescription(answer) 失败', e); return; }
        await flushIce(uid);
    }

    async function handleIce(msg) {
        const uid = Number(msg.from_uid);
        const pc = peers[uid];
        // peer 还没建 or 远端描述还没就位 → 先缓冲，等 handleOffer/handleAnswer 里补灌
        if (!pc || !pc.remoteDescription || !pc.remoteDescription.type) {
            (pendingIce[uid] = pendingIce[uid] || []).push(msg.candidate);
            return;
        }
        try { await pc.addIceCandidate(new RTCIceCandidate(msg.candidate)); }
        catch (e) { console.warn('[voice] addIceCandidate 失败', e); }
    }

    async function loadAllMembers() {
        try {
            const r = await fetch('/project/' + VOICE_STATE.projectId + '/voice/members').then(r => r.json());
            allMembers = r.members || [];
            renderMembers();
        } catch (e) { console.error(e); }
    }
    function renderMembers() {
        const wrap = $('voice-member-list');
        if (!allMembers.length) { wrap.innerHTML = '<div class="text-center py-3 text-muted">暂无成员</div>'; return; }
        wrap.innerHTML = allMembers.map(m => {
            const online = onlineUids.has(m.id);
            const dotColor = online ? '#4caf50' : '#bdbdbd';
            const opacity = online ? 1 : 0.6;
            let assignBtn = '';
            if (VOICE_STATE.isHost && online && m.id !== VOICE_STATE.currentUserId && m.id !== VOICE_STATE.currentPresenterId) {
                assignBtn = '<button class="btn btn-sm btn-outline-primary" onclick="assignPresenter(' + m.id + ')" title="设为演示者"><i class="fa fa-arrow-right"></i></button>';
            }
            return '<div class="member-item p-2 border-bottom d-flex align-items-center" style="opacity:' + opacity + ';">' +
                '<div style="position:relative;"><div class="rounded-circle bg-secondary d-inline-flex align-items-center justify-content-center text-white" style="width:36px;height:36px;font-size:14px;">' + escapeHtml((m.name || '?')[0]) + '</div>' +
                '<span style="position:absolute;bottom:0;right:0;width:10px;height:10px;border-radius:50%;border:2px solid #fff;background:' + dotColor + ';"></span></div>' +
                '<div class="ms-2 flex-grow-1" style="font-size:13px;"><div class="fw-bold">' + escapeHtml(m.name) +
                (m.is_host ? ' <span class="badge bg-success" style="font-size:10px;">主持</span>' : '') +
                (m.id === VOICE_STATE.currentPresenterId ? ' <span class="badge bg-danger" style="font-size:10px;">演示权</span>' : '') + '</div></div>' +
                assignBtn + '</div>';
        }).join('');
    }
    function updatePresenterLabel() {
        const badge = $('current-presenter-name');
        if (!VOICE_STATE.currentPresenterId) { badge.textContent = '未开始'; return; }
        const m = allMembers.find(x => x.id === VOICE_STATE.currentPresenterId);
        badge.textContent = m ? m.name : ('用户' + VOICE_STATE.currentPresenterId);
    }
    window.assignPresenter = function (uid) { sendSignal({ type: 'presenter_assign', to_uid: uid }); };
    function showApplyBanner(uid, name) {
        const b = $('apply-banner');
        b.innerHTML = escapeHtml(name) + ' 申请演示<button class="btn btn-success btn-sm ms-2" onclick="approveApply(' + uid + ')">批准</button><button class="btn btn-secondary btn-sm ms-1" onclick="dismissApply()">忽略</button>';
        b.classList.remove('d-none');
    }
    window.approveApply = function (uid) { dismissApply(); window.assignPresenter(uid); };
    window.dismissApply = function () { $('apply-banner').classList.add('d-none'); };

    async function loadDoc(docId) {
        if (!docId) {
            $('doc-frame').style.display = 'none';
            $('doc-empty-tip').style.display = '';
            $('btn-download').disabled = true;
            currentDocId = null;
            currentDownloadUrl = null;
            const label = $('doc-current-name');
            if (label) label.textContent = '未选择';
            return;
        }
        currentDocId = docId;
        const editMode = $('edit-mode').checked ? 1 : 0;
        try {
            const r = await fetch('/project/' + VOICE_STATE.projectId + '/voice/doc-config/' + docId + '?edit=' + editMode).then(r => r.json());
            $('doc-empty-tip').style.display = 'none';
            $('doc-frame').style.display = '';
            if (docEditor) { try { docEditor.destroy(); } catch (e) {} docEditor = null; }
            if (window.DocsAPI) {
                docEditor = new DocsAPI.DocEditor('doc-frame', Object.assign({}, r.config, { width: '100%', height: '100%' }));
            } else {
                $('doc-frame').src = r.download_url;
            }
            currentDownloadUrl = r.download_url;
            $('btn-download').disabled = !currentDownloadUrl;
            const label = $('doc-current-name');
            if (label && r.doc) label.textContent = r.doc.name + '.' + r.doc.extension;
            startSlideWatcher();
        } catch (e) { console.error(e); }
    }
    function startSlideWatcher() {
        setInterval(() => {
            if (VOICE_STATE.currentUserId !== VOICE_STATE.currentPresenterId) return;
            const frame = $('doc-frame');
            try {
                const inner = frame.contentDocument || frame.contentWindow.document;
                const el = inner.scrollingElement;
                if (!el) return;
                const scroll = el.scrollTop / (el.scrollHeight || 1);
                if (Math.abs(scroll - lastSentScroll) > 0.02) { lastSentScroll = scroll; sendSignal({ type: 'slide_sync', scroll: scroll }); }
            } catch (e) {}
        }, 800);
    }
    function applySlideSync(msg) {
        const frame = $('doc-frame');
        try {
            const inner = frame.contentDocument || frame.contentWindow.document;
            const el = inner.scrollingElement;
            if (el && msg.scroll != null) el.scrollTop = msg.scroll * el.scrollHeight;
        } catch (e) {}
    }

    // ---- 会议文档面板：与「协同办公」同一个文档库、同一套操作 ----
    function docPanelMsg(text, isError) {
        const el = $('doc-panel-msg');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'small w-100 ' + (isError ? 'text-danger' : 'text-success');
    }
    function csrfHeaders() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? { 'X-CSRF-TOKEN': meta.content } : {};
    }
    function bindDocTable() {
        document.querySelectorAll('#doc-panel .js-doc-present').forEach((el) => {
            el.onclick = (ev) => {
                ev.preventDefault();
                const docId = parseInt(el.dataset.docId, 10);
                sendSignal({ type: 'doc_select', doc_id: docId });
                loadDoc(docId);
                $('doc-panel').classList.add('d-none');
            };
        });
        document.querySelectorAll('#doc-panel .js-doc-delete').forEach((el) => {
            el.onclick = async () => {
                const docId = parseInt(el.dataset.docId, 10);
                if (!confirm('确定删除文档《' + el.dataset.docName + '》吗？删除后不可恢复。')) return;
                const r = await fetch('/project/' + VOICE_STATE.projectId + '/voice/doc-delete/' + docId,
                    { method: 'POST', headers: csrfHeaders() }).then(r => r.json());
                if (r.error) { docPanelMsg(r.error, true); return; }
                if (currentDocId === docId) loadDoc(null);
                await refreshDocPanel();
            };
        });
    }
    // 新建/删除后用服务端渲染结果刷新面板（不整页刷新，避免掉出会议）
    async function refreshDocPanel() {
        try {
            const html = await fetch(window.location.href).then(r => r.text());
            const fresh = new DOMParser().parseFromString(html, 'text/html').querySelector('#doc-panel-table');
            if (fresh) { $('doc-panel-table').innerHTML = fresh.innerHTML; bindDocTable(); }
        } catch (e) { console.error(e); }
    }
    async function createDoc() {
        const name = ($('doc-new-name').value || '').trim();
        if (!name) { docPanelMsg('请先填写文档名称', true); return; }
        const fd = new FormData();
        fd.append('mode', 'template');
        fd.append('name', name);
        fd.append('extension', $('doc-new-ext').value);
        const r = await fetch('/project/' + VOICE_STATE.projectId + '/voice/doc-store',
            { method: 'POST', body: fd, headers: csrfHeaders() }).then(r => r.json());
        if (r.error) { docPanelMsg(r.error, true); return; }
        $('doc-new-name').value = '';
        docPanelMsg('已创建《' + r.doc.name + '》，点「演示」即可共享给与会人', false);
        await refreshDocPanel();
    }
    async function uploadDoc(file) {
        const fd = new FormData();
        fd.append('mode', 'upload');
        fd.append('file', file);
        const r = await fetch('/project/' + VOICE_STATE.projectId + '/voice/doc-store',
            { method: 'POST', body: fd, headers: csrfHeaders() }).then(r => r.json());
        if (r.error) { docPanelMsg(r.error, true); return; }
        docPanelMsg('已上传《' + r.doc.name + '》到项目文档库', false);
        await refreshDocPanel();
    }
    const panelToggle = $('btn-doc-panel');
    if (panelToggle) panelToggle.addEventListener('click', () => $('doc-panel').classList.toggle('d-none'));
    const panelClose = $('btn-doc-panel-close');
    if (panelClose) panelClose.addEventListener('click', () => $('doc-panel').classList.add('d-none'));
    const createBtn = $('btn-doc-create');
    if (createBtn) createBtn.addEventListener('click', createDoc);
    if ($('btn-upload')) $('btn-upload').addEventListener('click', async () => {
        const input = $('file-upload');
        const file = input.files[0];
        if (!file) { docPanelMsg('请先选择要上传的文件（docx/xlsx/pptx）', true); input.click(); return; }
        try { await uploadDoc(file); } catch (err) { docPanelMsg('上传失败: ' + err.message, true); }
        input.value = '';
    });
    $('edit-mode').addEventListener('change', () => { if (currentDocId) loadDoc(currentDocId); });
    $('btn-download').addEventListener('click', () => { if (currentDownloadUrl) window.open(currentDownloadUrl, '_blank'); });
    $('btn-follow').addEventListener('click', () => { followingPresenter = true; $('btn-follow').disabled = true; });
    $('btn-call').addEventListener('click', async () => {
        try { localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false }); }
        catch (e) { alert('无法访问麦克风：' + e.message); return; }
        await connectSignal();
        $('btn-call').disabled = true;
        $('btn-call').innerHTML = '<i class="fa fa-phone me-1"></i>会议中';
        $('btn-mic').disabled = false;
        $('btn-follow').disabled = false;
    });
    $('btn-mic').addEventListener('click', () => {
        const btn = $('btn-mic');
        const muted = btn.dataset.muted !== '1';
        btn.dataset.muted = muted ? '1' : '0';
        if (localStream) localStream.getAudioTracks().forEach(t => t.enabled = !muted);
        btn.innerHTML = muted ? '<i class="fa fa-microphone-slash"></i> 取消静音' : '<i class="fa fa-microphone"></i> 静音';
    });
    const revoke = $('btn-revoke-presenter');
    if (revoke) revoke.addEventListener('click', () => sendSignal({ type: 'presenter_revoke' }));
    const applyBtn = $('btn-apply-presenter');
    if (applyBtn) applyBtn.addEventListener('click', () => sendSignal({ type: 'presenter_apply' }));

    bindDocTable();
    loadAllMembers();
})();
