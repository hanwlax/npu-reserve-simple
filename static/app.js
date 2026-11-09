(() => {
  const root = document.getElementById('npu-reserve');
  const workspace = root.querySelector('#npu-workspace');
  const identity = root.querySelector('#npu-identity');
  let machines = {};
  let records = [];
  let upcoming = [];
  let pastEnd = 0;
  let cutoff = 0;
  let windowStart = 0;
  let revision = 0;
  const activeMachines = () => Object.keys(machines).map(Number);
  let view = 'login';
  let user = null;
  let history = null;
  let draft = null;
  let drag = null;
  let notice = null;
  let releaseEditor = null;
  let busy = false;
  let polling = false;

  const escape = value => String(value).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
  const time = slot => {
    const mins = ((480 + 30 * slot) % 1440 + 1440) % 1440;
    return String(Math.floor(mins / 60)).padStart(2,'0') + ':' + String(mins % 60).padStart(2,'0');
  };
  const localDate = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit'});
  const localWeekday = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',weekday:'short'});
  const dateAt = slot => new Date((windowStart + slot*1800)*1000);
  const stamp = slot => localDate.format(dateAt(slot))+' '+time(slot);
  const dayLabel = slot => localDate.format(dateAt(slot))+' '+localWeekday.format(dateAt(slot));
  const windowLabel = () => range({s:0,e:48})+' · 北京时间';
  const boardNote = '单行点击或拖选 · 点击自己的预约可释放，也可在其中拖选一段 · 30 分钟一格';
  const range = ({s,e}) => stamp(s) + ' – ' + stamp(e);
  function ownRange(candidate) {
    if (candidate.s < pastEnd) return null;
    return upcoming.find(r => r.m === candidate.m && r.s <= candidate.s && candidate.e <= r.e);
  }

  const overlaps = (record, {m,s,e}) => record.m === m && record.s < e && s < record.e;
  const hit = candidate => records.find(r => overlaps(r,candidate));
  const valid = candidate => candidate && candidate.s >= pastEnd && candidate.s < candidate.e &&
    candidate.e <= 48 && !hit(candidate);
  const intersection = (record, candidate) => ({s:Math.max(record.s,candidate.s),e:Math.min(record.e,candidate.e)});
  const status = record => record.owner === user.username ? '我的预约' : '已预约';
  const say = (message, error = false) => { notice = {message,error}; };
  const noticeHTML = () => notice ? '<div class="notice' + (notice.error ? ' error" role="alert"' : '" role="status"') + '>' + escape(notice.message) + '</div>' : '';
  const button = (action,label,style='') => '<button type="button" class="button '+style+'" data-action="'+action+'">'+label+'</button>';

  function chrome() {
    identity.innerHTML = !user ? '' : '<span class="pill small">'+escape(user.name)+'</span>' +
      '<button class="link-button small" type="button" data-action="logout">退出</button>';
  }

  function intervalDetail(record) {
    return status(record)+' · '+range(record)+' · 预约人 '+record.operator;
  }

  function block(record, kind, label, detail) {
    const tooltip = detail ? ' title="'+escape(detail)+'"' : '';
    return '<div class="block '+kind+'" style="left:'+record.s/48*100+'%;width:'+(record.e-record.s)/48*100+'%"'+tooltip+' aria-hidden="true">' +
      (record.e-record.s >= 4 ? label : '') + '</div>';
  }

  function draftBlock(candidate) {
    return block(candidate,'draft','待提交');
  }

  function track(m) {
    const {name,ip,spec} = machines[m];
    const releaseRange = releaseEditor?.record.m === m ? releaseCandidate() : null;
    const row = records.filter(r => r.m === m);
    const detail = row.map(intervalDetail).join('，');
    return '<div class="board-row"><div class="machine"><button type="button" data-machine="'+m+'" data-action="select-machine" aria-label="选择 '+escape(name)+' 的时段">'+
      escape(name)+'</button><span class="quiet small">'+escape(ip)+'</span>'+
      (spec?'<span class="quiet small">'+escape(spec)+'</span>':'')+'</div><div class="track" data-track="'+m+'" role="group" aria-label="'+escape(detail || '无占用时段')+'">'+
      block({s:0,e:pastEnd},'past','')+row.filter(r => r.e > pastEnd).map(r =>
        block({...r,s:Math.max(r.s,pastEnd)},status(r)==='我的预约' ? 'mine' : 'booked',status(r),intervalDetail(r))).join('')+
      (draft?.m === m && valid(draft) ? draftBlock(draft) : '')+
      (releaseRange ? block(releaseRange,'release-draft','') : '')+'</div></div>';
  }

  function timeline() {
    const ids = activeMachines();
    if (!ids.length) return '<div class="panel"><h2>暂无可预约机器</h2><p class="quiet">请联系服务维护人员添加机器。</p></div>';
    const ticks = [0,4,8,12,16,20,24,28,32,36,40,44,48].map(i => {
      const cls = i===0?' first':i===48?' last':i===32?' midnight':'';
      return '<span class="tick'+cls+'" '+(i===48?'':'style="left:'+i/48*100+'%"')+'>'+time(i)+'</span>';
    }).join('');
    return '<div class="board" aria-label="机器预约时间轴"><div class="board-grid"><div class="board-row"><div class="machine"><strong>机器</strong><span class="quiet small">IP 地址 · 配置</span></div>'+
      '<div class="axis"><span class="day">今天 · '+dayLabel(0)+'</span><span class="day next">次日 · '+dayLabel(48)+'</span>'+ticks+'</div></div>'+ids.map(track).join('')+'</div></div>';
  }

  function options(selected, min, max) {
    return Array.from({length:Math.max(0,max-min+1)},(_,i) => i+min).map(i =>
      '<option value="'+i+'"'+(i===selected?' selected':'')+'>'+stamp(i)+'</option>').join('');
  }

  function draftDescription() {
    return valid(draft) ? range(draft)+' · '+((draft.e-draft.s)/2)+' 小时' : '请选择连续的空闲时段。';
  }

  function myBookings() {
    const current = [...upcoming].sort((a,b) => a.s-b.s);
    return '<section class="panel" aria-label="我的预约"><h2>我的预约</h2>' +
      (current.length ? '<div class="listing">'+current.map(r => '<div class="list-row"><div><strong>'+escape(machines[r.m].name)+
        '</strong> <span class="pill small">'+(r.s <= cutoff?'使用中':'待开始')+'</span><div class="quiet small">'+range(r)+
        '</div></div><button type="button" class="button" data-release="'+r.id+'" aria-label="释放 '+escape(machines[r.m].name)+
        ' 的预约 '+range(r)+'">释放机时</button></div>').join('')+'</div>' :
        '<p class="quiet">当前没有待开始或使用中的预约。</p>')+'</section>';
  }

  function beginRelease(record, partial = null) {
    if (!upcoming.includes(record) || !machines[record.m]) return;
    reset();
    const start = Math.max(record.s,cutoff);
    releaseEditor = {record,mode:partial?'partial':'whole',s:partial?.s ?? start,e:partial?.e ?? record.e};
    patch();
    root.querySelector('#npu-release [name="mode"]:checked').focus();
  }

  function releaseCandidate() {
    const {record,mode,s,e} = releaseEditor;
    const first = Math.max(record.s,cutoff);
    if (mode === 'whole') return {m:record.m,s:first,e:record.e};
    if (s >= first && s < e && e <= record.e) return {m:record.m,s,e};
    return null;
  }

  function releasePreview() {
    const candidate = releaseCandidate();
    if (!candidate) return '请选择预约范围内的起止时间，结束须晚于开始。';
    const record = releaseEditor.record;
    const remaining = remainingRanges({...record,s:Math.max(record.s,cutoff)},candidate);
    return '将释放 '+range(candidate)+'。'+
      (remaining.length?'保留 '+remaining.map(range).join('、')+'。':'')+
      (record.s < cutoff?'已使用的时段保留。':'')+'释放后，该时段可再次预约。';
  }

  function releaseForm() {
    const {record,mode,s,e} = releaseEditor;
    const first = Math.max(record.s,cutoff);
    const wholeLabel = record.s < cutoff ? '释放全部剩余机时' : '释放整段预约';
    const radio = (value,label) => '<label><input type="radio" name="mode" value="'+value+'" '+(mode===value?'checked':'')+'> '+label+'</label>';
    return '<form class="panel" id="npu-release" aria-label="确认释放"><h2>释放预约 · '+escape(machines[record.m].name)+'</h2>'+
      '<p class="quiet small">原预约 · '+range(record)+'</p>'+
      '<fieldset class="checks release-modes"><legend>释放范围</legend>'+radio('whole',wholeLabel)+radio('partial','仅释放一段')+'</fieldset>'+
      '<div class="release-fields" '+(mode==='partial'?'':'hidden')+'>'+
      '<label class="field">释放开始<select name="start">'+options(s,first,record.e-1)+'</select></label>'+
      '<label class="field">释放结束<select name="end">'+options(e,first+1,record.e)+'</select></label></div>'+
      '<p class="quiet" id="npu-release-preview" aria-live="polite">'+releasePreview()+'</p>'+
      '<div class="controls"><button type="submit" class="button danger" '+(releaseCandidate()?'':'disabled')+'>确认释放</button>'+
      button('cancel-release','取消')+'</div></form>';
  }

  function readRelease(data) {
    releaseEditor.mode = data.get('mode') === 'partial' ? 'partial' : 'whole';
    releaseEditor.s = Number(data.get('start'));
    releaseEditor.e = Number(data.get('end'));
  }

  function selection() {
    if (releaseEditor) return releaseForm();
    if (!draft) return '<div class="panel"><h2>选择预约时段</h2><p class="quiet">在空闲时段点击或拖选，也可点击机器名后调整起止时间。</p></div>';
    return '<div class="panel"><div><h2>预约确认 · '+escape(machines[draft.m].name)+
      '</h2><div class="quiet small" id="npu-draft-time" aria-live="polite">'+draftDescription()+'</div></div>'+
      '<form id="npu-reservation" class="controls"><label class="field">开始<select name="start">'+options(draft.s,pastEnd,47)+'</select></label>'+
      '<label class="field">结束<select name="end">'+options(draft.e,pastEnd+1,48)+'</select></label>'+
      saveButton('提交预约')+button('clear','清除选择')+'</form></div>';
  }

  function schedule() {
    const labels = [['','可预约'],['booked','已预约'],['mine','我的预约'],['past','已过去'],['draft','待提交']];
    return '<div class="intro"><div><h1>机器预约</h1><p class="quiet" id="npu-window">'+windowLabel()+
      '</p></div><div class="legend" aria-label="时段图例">'+
      labels.map(([key,label])=>'<span><b class="swatch '+key+'"></b>'+label+'</span>').join('')+'</div></div>'+
      '<section aria-label="预约时间轴"><div id="npu-board-slot">'+timeline()+'</div>'+
      '<p class="board-note">'+boardNote+'</p></section>'+
      '<div id="npu-notice" aria-live="polite">'+noticeHTML()+'</div><div id="npu-selection">'+(activeMachines().length ? selection() : '')+'</div>'+
      '<div id="npu-my-bookings">'+myBookings()+'</div>';
  }

  function historyPage() {
    const format = new Intl.DateTimeFormat('zh-CN', {
      timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'
    });
    const stamp = time => format.format(time*1000);
    const header = '<div class="intro"><div><h1>最近 7 天预约</h1>'+
      (history?'<p class="quiet">'+stamp(history.from)+' – '+stamp(history.to)+' · 北京时间</p>':'')+
      '</div>'+button('refresh-history','刷新')+'</div><div aria-live="polite">'+noticeHTML()+'</div>';
    if (!history) return header+'<p class="quiet">正在加载预约记录…</p>';
    if (!history.records.length) return header+'<section class="panel"><h2>没有预约记录</h2>'+
      (history.page>1?'<div>'+button('previous-history','上一页')+'</div>':'')+'</section>';
    const rows = history.records.map(record => '<tr><th scope="row">'+escape(record.machine)+
      '<br><span class="quiet small">'+escape(record.ip)+'</span></th><td>'+escape(record.name)+'</td>'+
      '<td>'+stamp(record.start)+'<br>'+stamp(record.end)+'</td><td>'+
      (record.end>history.to?'进行中':'已结束')+'</td></tr>').join('');
    return header+'<section class="panel"><div class="history-table"><table aria-label="历史预约">'+
      '<thead><tr><th scope="col">机器</th><th scope="col">预约人</th><th scope="col">开始与结束</th><th scope="col">状态</th></tr></thead>'+
      '<tbody>'+rows+'</tbody></table></div><div class="summary"><span class="quiet small">第 '+history.page+' 页</span>'+
      '<div class="controls">'+(history.page>1?button('previous-history','上一页'):'')+
      (history.hasMore?button('next-history','下一页'):'')+'</div></div></section>';
  }

  function loadHistory(page) {
    return run(async () => {
      history = await request('history?page='+page);
      notice = null;
      render();
    });
  }

  function saveButton(label) {
    return '<button type="submit" class="button primary">'+label+'</button>';
  }

  function login() {
    return '<form class="panel login" id="npu-login"><h1>工号登录</h1>'+
      '<p class="quiet">请输入你本人的工号。</p>'+
      '<div data-login-notice aria-live="polite">'+noticeHTML()+'</div>'+
      '<label class="field">工号<input name="username" required maxlength="9" pattern="[A-Za-z][0-9]{8}" '+
      'autocomplete="username" placeholder="例如 h00893113"></label>'+saveButton('登录')+'</form>';
  }

  function render() {
    chrome();
    const pages = {login,booking:schedule,history:historyPage};
    const nav = user ? '<nav class="nav" aria-label="预约菜单">'+
      [['booking','机器预约'],['history','历史预约']].map(([key,label]) =>
        '<button type="button" data-page="'+key+'" aria-pressed="'+(view===key)+'">'+label+'</button>'
      ).join('')+'</nav>' : '';
    workspace.innerHTML = nav+pages[view]();
  }

  function patch({board = true, form = true} = {}) {
    if (board) {
      const slot = root.querySelector('#npu-board-slot');
      const scroll = slot.querySelector('.board')?.scrollLeft || 0;
      root.querySelector('#npu-window').textContent = windowLabel();
      slot.innerHTML = timeline();
      const updated = slot.querySelector('.board');
      if (updated) updated.scrollLeft = scroll;
    }
    if (form) root.querySelector('#npu-selection').innerHTML = activeMachines().length ? selection() : '';
    const bookings = root.querySelector('#npu-my-bookings');
    if (board && bookings) bookings.innerHTML = myBookings();
    root.querySelector('#npu-notice').innerHTML = noticeHTML();
  }

  function select(candidate) {
    if (valid(candidate)) {
      releaseEditor = null;
      draft = candidate;
      notice = null;
      patch();
      return;
    }
    const occupied = hit(candidate);
    say(occupied ? '所选范围包含'+status(occupied)+'时段（'+range(occupied)+'）。请选择连续的空闲时段。' :
      '请选择尚未过去且结束时间晚于开始时间的时段。',true);
    patch({board:false,form:false});
  }

  function reset() {
    draft = null;
    notice = null;
    releaseEditor = null;
  }

  root.addEventListener('click', event => {
    const control = event.target.closest('button');
    if (!control || busy) return;
    const data = control.dataset;
    if (data.page && data.page !== view) {
      view = data.page;
      reset();
      render();
      if (view==='history') loadHistory(1);
      else run(async () => { applyState(await request('state')); patch(); });
      return;
    }
    if (data.release !== undefined) {
      beginRelease(upcoming.find(r=>r.id===Number(data.release)));
      return;
    }
    switch (data.action) {
      case 'refresh-history': loadHistory(1); break;
      case 'previous-history': loadHistory(history.page-1); break;
      case 'next-history': loadHistory(history.page+1); break;
      case 'cancel-release':
      case 'clear': reset(); patch(); break;
      case 'logout': run(async () => { await request('logout',{}); signedOut(); }); break;
      case 'select-machine': {
        const m = Number(data.machine);
        const s = Array.from({length:48-pastEnd},(_,i)=>pastEnd+i).find(i=>!hit({m,s:i,e:i+1}));
        if (s === undefined) {
          say(machines[m].name+' 在当前窗口内没有空闲时段。',true);
          patch({board:false,form:false});
        } else {
          select({m,s,e:s+1});
          root.querySelector('#npu-reservation select').focus();
        }
        break;
      }
    }
  });

  root.addEventListener('change', event => {
    const form = event.target.form;
    if (form?.id === 'npu-release') {
      readRelease(new FormData(form));
      form.querySelector('.release-fields').hidden = releaseEditor.mode !== 'partial';
      form.querySelector('#npu-release-preview').textContent = releasePreview();
      form.querySelector('button[type="submit"]').disabled = !releaseCandidate();
      patch({form:false});
    }
    if (form?.id === 'npu-reservation' && event.target.matches('select')) {
      draft = readDraft(new FormData(form));
      notice = null;
      root.querySelector('#npu-draft-time').textContent = draftDescription();
      patch({form:false});
    }
  });

  function readDraft(data) {
    return {m:draft.m,s:Number(data.get('start')),e:Number(data.get('end'))};
  }

  function remainingRanges(record, candidate) {
    const cut = intersection(record,candidate);
    const remainder = [];
    if (record.s < cut.s) remainder.push({...record,e:cut.s});
    if (cut.e < record.e) remainder.push({...record,s:cut.e});
    return remainder;
  }

  async function request(action, body) {
    const response = await fetch('/api/'+action, body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json','X-NPU-Request':'1'}, body:JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || '服务暂时不可用，请稍后重试。');
      error.status = response.status;
      error.code = data.error;
      throw error;
    }
    return data;
  }

  function applyState(state) {
    ({user,records,upcoming,pastEnd,cutoff,revision,window:windowStart} = state);
    machines = Object.fromEntries(state.machines.map(machine => [machine.id,machine]));
    if (view==='login') view = 'booking';
  }

  function signedOut(message) {
    reset();
    user = null;
    view = 'login';
    history = null;
    if (message) say(message,true);
    render();
  }

  function onBoard() {
    return view==='booking';
  }

  async function run(action, form) {
    if (busy) return;
    busy = true;
    root.setAttribute('aria-busy','true');
    const submit = form?.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      await action();
    } catch (error) {
      if (error.code==='login_required') {
        signedOut('会话已过期，请重新登录。');
      } else if (['occupied','state_changed'].includes(error.code)) {
        try {
          applyState(await request('state'));
          reset();
          say(error.code==='occupied' ? error.message+' 时间轴已更新。' : error.message,true);
          if (onBoard()) patch();
          else render();
        } catch (refreshError) {
          if (refreshError.code==='login_required') signedOut('会话已过期，请重新登录。');
          else showError('操作未完成，暂时无法刷新数据。请稍后重试。',form);
        }
      } else {
        showError(error.status ? error.message : '无法连接服务，请稍后重试。',form);
      }
    } finally {
      busy = false;
      root.removeAttribute('aria-busy');
      if (submit?.isConnected) submit.disabled = form.id==='npu-release' && !releaseCandidate();
    }
  }

  function showError(message, form) {
    say(message,true);
    const target = form?.querySelector('[data-login-notice]');
    if (target) target.innerHTML = noticeHTML();
    else if (onBoard()) patch({board:false,form:false});
    else workspace.querySelector('[aria-live]').innerHTML = noticeHTML();
  }

  function save(action, payload, form) {
    return run(async () => {
      const {state,message} = await request(action,payload);
      applyState(state);
      reset();
      say(message);
      if (onBoard()) patch();
      else render();
    },form);
  }

  root.addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    const form = event.target;
    const data = new FormData(form);
    if (form.id==='npu-login') {
      run(async () => {
        applyState(await request('login',Object.fromEntries(data)));
        reset();
        render();
      },form);
    } else if (form.id==='npu-release') {
      const record = releaseEditor?.record;
      if (!upcoming.includes(record) || !machines[record.m]) return;
      readRelease(data);
      const candidate = releaseCandidate();
      if (candidate) save('release',{...candidate,id:record.id,window:windowStart},form);
    } else if (form.id==='npu-reservation') {
      const candidate = readDraft(data);
      if (!valid(candidate)) {
        showError('所选范围无效或包含占用时段，请调整起止时间。',form);
        return;
      }
      save('book',{...candidate,window:windowStart},form);
    }
  });

  async function start() {
    busy = true;
    try {
      applyState(await request('state'));
      render();
    } catch (error) {
      signedOut(error.status===401 ? '' : '无法加载服务，请登录重试。');
    } finally {
      busy = false;
    }
  }

  async function pollBoard() {
    if (busy || polling || !onBoard() || draft || releaseEditor || drag) return;
    const before = user;
    polling = true;
    try {
      const state = await request('state');
      if (before===user && onBoard() && !busy && !draft && !releaseEditor && !drag &&
          (state.revision!==revision || state.window!==windowStart || state.pastEnd!==pastEnd || state.cutoff!==cutoff)) {
        applyState(state);
        patch();
      }
    } catch (error) {
      if (before===user && !busy && error.code==='login_required') signedOut('会话已过期，请重新登录。');
    } finally {
      polling = false;
    }
  }

  function slotAt(event, track) {
    const rect = track.getBoundingClientRect();
    return Math.max(0,Math.min(47,Math.floor((event.clientX-rect.left)/rect.width*48)));
  }

  root.addEventListener('pointerdown', event => {
    const track = event.target.closest('[data-track]');
    if (busy || !track || event.button !== 0 || !event.isPrimary) return;
    drag = {track, id:event.pointerId, s:slotAt(event,track), preview:null};
    track.setPointerCapture(event.pointerId);
  });
  root.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId || event.pointerType === 'touch') return;
    const end = slotAt(event,drag.track);
    const candidate = {m:Number(drag.track.dataset.track),s:Math.min(drag.s,end),e:Math.max(drag.s,end)+1};
    drag.preview?.remove();
    drag.preview = null;
    const own = ownRange(candidate);
    if (!own && !valid(candidate)) return;
    drag.track.insertAdjacentHTML('beforeend',own ? block(candidate,'release-draft','') : draftBlock(candidate));
    drag.preview = drag.track.lastElementChild;
  });
  root.addEventListener('pointerup', event => {
    if (!drag || drag.id !== event.pointerId) return;
    const {track,s,preview} = drag;
    const end = slotAt(event,track);
    drag = null;
    preview?.remove();
    const candidate = {m:Number(track.dataset.track),s:Math.min(s,end),e:Math.max(s,end)+1};
    const own = ownRange(candidate);
    if (own) beginRelease(own,s===end ? null : candidate);
    else select(candidate);
  });
  root.addEventListener('pointercancel', event => {
    if (drag?.id !== event.pointerId) return;
    drag.preview?.remove();
    drag = null;
  });
  start();
  setInterval(pollBoard,30000);
})();
