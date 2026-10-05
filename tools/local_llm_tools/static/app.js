/* All untrusted agent text uses textContent. The access token stays in memory:
 * refreshing or closing this page clears it instead of persisting credentials. */
let token = '', after = 0, polling = false;
const $ = id => document.getElementById(id);
const showError = error => { $('error').textContent = error.message || String(error); };
async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Request failed');
  return value;
}
function button(label, action) {
  const node = document.createElement('button'); node.textContent = label;
  node.onclick = async () => { node.disabled = true; try { await action(); await refresh(); } catch (e) { showError(e); } finally { node.disabled = false; } };
  return node;
}
function card(value) {
  const node = document.createElement('article'), pre = document.createElement('pre');
  pre.textContent = JSON.stringify(value, null, 2); node.append(pre); return node;
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const [tasks, approvals, events] = await Promise.all([api('tasks'), api('approvals'), api('events?after=' + after)]);
    $('tasks').replaceChildren(...tasks.map(task => {
      const node = card(task);
      for (const action of ['pause','resume','cancel']) node.append(button(action, () => api('control', {id:task.id, action})));
      if (['paused','failed'].includes(task.status)) node.append(button('Resolve interrupted action', async () => {
        const note = prompt('Inspect the actual outcome first. Describe what happened; the interrupted action will be skipped, not replayed. Resume the task afterward.');
        if (note) await api('recover', {id:task.id,note});
      }));
      if (['completed','failed'].includes(task.status)) node.append(button('Compact context', () => api('compact',{id:task.id})));
      return node;
    }));
    $('approvals').replaceChildren(...approvals.map(approval => {
      const node = card(approval);
      node.append(button('Approve once', () => api('approval', {id:approval.id,approved:true})), button('Deny', () => api('approval', {id:approval.id,approved:false})));
      return node;
    }));
    for (const event of events) { after = event.id; $('events').textContent += JSON.stringify(event, null, 2) + '\n'; }
    // The complete history remains in SQLite; cap only this live DOM view.
    $('events').textContent = $('events').textContent.slice(-100000);
  } finally { polling = false; }
}
$('login-form').onsubmit = async event => {
  event.preventDefault(); token = $('token').value;
  try { await refresh(); $('token').value = ''; $('login').hidden = true; $('workspace').hidden = false; }
  catch(e) { showError(e); token = ''; }
};
$('task-form').onsubmit = async event => {
  event.preventDefault();
  const data = {prompt:$('prompt').value};
  if ($('at').value) data.at = new Date($('at').value).getTime()/1000;
  if ($('interval').value) data.interval = Number($('interval').value);
  if ($('watch').value) data.watch = $('watch').value;
  try { await api('tasks',data); await refresh(); } catch(e) { showError(e); }
};
$('memory-form').onsubmit = async event => {
  event.preventDefault();
  try {
    const owner = $('owner').value;
    const memories = await api('memories?owner=' + encodeURIComponent(owner) + '&q=' + encodeURIComponent($('query').value));
    $('memories').replaceChildren(...memories.map(memory => {
      const node = card(memory), editor = document.createElement('textarea'); editor.value = memory.content;
      node.append(editor, button('Save correction', () => api('memory/update',{owner:memory.owner,id:memory.id,content:editor.value})), button('Forget', async () => {
        if (confirm('Permanently forget this memory? Original conversation archives are retained.')) { await api('memory/forget',{owner:memory.owner,id:memory.id}); node.remove(); }
      }));
      return node;
    }));
  } catch(e) { showError(e); }
};
// A failed poll remains visible and subsequent polls reconnect automatically.
setInterval(() => { if(token) refresh().catch(showError); }, 2000);
