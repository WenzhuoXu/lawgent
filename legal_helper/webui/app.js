// Aviation Legal Helper — single-page chat client.
//
// Uses fetch + ReadableStream to consume the SSE stream from /api/chat.

const $skills = document.getElementById('skill-list');
const $messages = document.getElementById('messages');
const $form = document.getElementById('composer');
const $input = document.getElementById('input');
const $sendBtn = $form.querySelector('button');
const $artifacts = document.getElementById('artifact-list');
const $activity = document.getElementById('activity-list');
const $upload = document.getElementById('upload-list');
const $drop = document.getElementById('dropzone');
const $file = document.getElementById('file-input');

let activeSkill = null;
let attachments = []; // list of absolute paths returned by /api/upload

const eventLabels = {
  orchestrator_started: 'Orchestrator started',
  provider_turn_started: 'Model working',
  provider_turn_finished: 'Model finished',
  local_tool_call_started: 'Local tool started',
  local_tool_call_finished: 'Local tool finished',
  run_skill_called: 'Skill requested',
  run_skill_returned: 'Skill returned',
  skill_started: 'Sub-agent started',
  skill_finished: 'Sub-agent finished',
  agent_task_started: 'Agent task started',
  agent_task_finished: 'Agent task finished',
  hosted_tool_call: 'Online search / hosted tool',
  workflow_plan: 'Plan ready',
  workflow_plan_routed: 'Route selected',
  workflow_plan_decomposed: 'Plan decomposed',
  workflow_plan_created: 'Plan created',
  workflow_plan_normalized: 'Plan optimized',
  workflow_direct_output: 'Direct output',
  integration_started: 'Final synthesis started',
  citation_audit_finished: 'Citation audit finished',
};

async function loadSkills() {
  const resp = await fetch('/api/skills');
  const list = await resp.json();
  $skills.innerHTML = '';
  list.forEach((s) => {
    const li = document.createElement('li');
    li.dataset.skill = s.name;
    li.innerHTML = `<strong>${s.name}</strong><span class="desc">${(s.description || '').slice(0, 110)}…</span>`;
    li.onclick = () => {
      activeSkill = activeSkill === s.name ? null : s.name;
      [...$skills.children].forEach((c) =>
        c.classList.toggle('active', c.dataset.skill === activeSkill),
      );
    };
    $skills.appendChild(li);
  });
}

function appendMsg(role, text = '') {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  $messages.appendChild(div);
  $messages.scrollTop = $messages.scrollHeight;
  return div;
}

function appendArtifact(a) {
  if ([...$artifacts.children].some((c) => c.dataset.name === a.filename)) return;
  const li = document.createElement('li');
  li.dataset.name = a.filename;
  const link = document.createElement('a');
  link.href = a.url;
  link.textContent = a.filename;
  link.download = a.filename;
  li.appendChild(link);
  $artifacts.appendChild(li);
}

function compactValue(value) {
  if (value === null || value === undefined || value === '') return '';
  if (Array.isArray(value)) return value.filter(Boolean).join(', ');
  if (typeof value === 'object') {
    if (value.skill_name) return value.skill_name;
    if (value.tool_name) return value.tool_name;
    if (value.model && value.provider) return `${value.provider} / ${value.model}`;
    return JSON.stringify(value);
  }
  return String(value);
}

function describeWorkflowEvent(record) {
  const type = record?.event_type || 'workflow_event';
  const data = record?.data || {};
  const label = eventLabels[type] || type;
  const facts = [];

  if (data.skill_name) facts.push(data.skill_name);
  if (data.tool_name) facts.push(data.tool_name);
  if (data.provider && data.model) facts.push(`${data.provider} / ${data.model}`);
  if (data.execution_mode === 'clarify') facts.push('Clarification needed');
  else if (data.execution_mode === 'direct_answer') facts.push('Direct answer');
  else if (data.execution_mode) facts.push(data.execution_mode);
  if (data.routing_reason) facts.push(data.routing_reason);
  if (data.hosted_tool_call_count !== undefined) {
    facts.push(`${data.hosted_tool_call_count} hosted calls`);
  }
  if (data.local_tool_call_count !== undefined) {
    facts.push(`${data.local_tool_call_count} local calls`);
  }
  if (data.has_evidence_verification_log !== undefined) {
    facts.push(data.has_evidence_verification_log ? 'verification log present' : 'verification log missing');
  }
  if (data.has_claim_level_sanity_check !== undefined) {
    facts.push(data.has_claim_level_sanity_check ? 'claim check present' : 'claim check missing');
  }
  if (data.has_sources !== undefined) {
    facts.push(data.has_sources ? 'sources present' : 'sources missing');
  }
  if (type === 'citation_audit' || type === 'citation_audit_finished') {
    if (data.skipped) {
      facts.push(`skipped — ${data.skip_reason || 'generic enquiry'}`);
    } else if (data.do_not_file) {
      facts.push('⚠ DO NOT FILE — pre-filing escalation triggered');
    } else if (data.coverage_line) {
      facts.push(data.coverage_line);
    } else if (data.warnings && data.warnings.length > 0) {
      facts.push(`${data.warnings.length} item${data.warnings.length === 1 ? '' : 's'} need review`);
    } else if (data.ok !== false) {
      facts.push('✓ no issues');
    }
  }
  if (data.task_preview) facts.push(data.task_preview.slice(0, 160));
  if (data.output_preview && type !== 'local_tool_call_finished') facts.push(data.output_preview.slice(0, 160));

  return { label, detail: facts.map(compactValue).filter(Boolean).join(' · ') };
}

function appendActivity(record) {
  const recordType = record?.event_type || record?.kind;
  // Token-level streaming events flood the feed (one card per chunk).
  // The citation-audit phase already emits citation_audit_started +
  // citation_audit lifecycle events, which carry the meaningful state.
  if (recordType === 'agent_delta' || recordType === 'cite_check_delta') return;
  const { label, detail } = describeWorkflowEvent(record);
  const li = document.createElement('li');
  li.className = `activity ${record?.event_type || 'event'}`;
  const title = document.createElement('strong');
  title.textContent = label;
  li.appendChild(title);
  if (detail) {
    const span = document.createElement('span');
    span.textContent = detail;
    li.appendChild(span);
  }
  ['evidence_verification_preview', 'sources_preview'].forEach((key) => {
    if (!record?.data?.[key]) return;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = key === 'sources_preview' ? 'Sources checked' : 'Evidence verification';
    const pre = document.createElement('pre');
    pre.textContent = record.data[key];
    details.appendChild(summary);
    details.appendChild(pre);
    li.appendChild(details);
  });
  const type = record?.event_type || 'workflow_event';
  if (type === 'citation_audit' || type === 'citation_audit_finished') {
    const data = record?.data || {};
    if (data.do_not_file) {
      const banner = document.createElement('div');
      banner.textContent = '⚠ DO NOT FILE — pre-filing escalation triggered';
      banner.style.color = '#8a1c13';
      banner.style.fontWeight = '600';
      banner.style.marginTop = '4px';
      banner.style.fontSize = '12px';
      li.appendChild(banner);
    }
    if (data.warnings && data.warnings.length > 0) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `Findings (${data.warnings.length})`;
      summary.style.cursor = 'pointer';
      const ul = document.createElement('ul');
      ul.style.paddingLeft = '20px';
      ul.style.marginTop = '6px';
      ul.style.marginBottom = '0';
      data.warnings.forEach((warning) => {
        const wli = document.createElement('li');
        wli.style.fontSize = '11px';
        wli.style.lineHeight = '1.4';
        wli.style.marginBottom = '4px';
        wli.textContent = warning;
        ul.appendChild(wli);
      });
      details.appendChild(summary);
      details.appendChild(ul);
      li.appendChild(details);
    }
    if (data.report_markdown) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Full verification report';
      summary.style.cursor = 'pointer';
      const pre = document.createElement('pre');
      pre.textContent = data.report_markdown;
      pre.style.whiteSpace = 'pre-wrap';
      pre.style.fontSize = '11px';
      pre.style.lineHeight = '1.5';
      pre.style.background = 'rgba(0,0,0,0.04)';
      pre.style.padding = '8px';
      pre.style.borderRadius = '4px';
      pre.style.marginTop = '6px';
      pre.style.maxHeight = '420px';
      pre.style.overflowY = 'auto';
      details.appendChild(summary);
      details.appendChild(pre);
      li.appendChild(details);
    }
  }
  $activity.appendChild(li);
  $activity.scrollTop = $activity.scrollHeight;
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append('file', file);
  const resp = await fetch('/api/upload', { method: 'POST', body: fd });
  const data = await resp.json();
  attachments.push(data.path);
  const li = document.createElement('li');
  li.textContent = data.filename;
  $upload.appendChild(li);
}

$drop.onclick = () => $file.click();
$file.onchange = (e) => Array.from(e.target.files).forEach(uploadFile);
['dragover', 'dragenter'].forEach((ev) =>
  $drop.addEventListener(ev, (e) => {
    e.preventDefault();
    $drop.classList.add('dragover');
  }),
);
['dragleave', 'drop'].forEach((ev) =>
  $drop.addEventListener(ev, (e) => {
    e.preventDefault();
    $drop.classList.remove('dragover');
  }),
);
$drop.addEventListener('drop', (e) => {
  Array.from(e.dataTransfer.files).forEach(uploadFile);
});

function parseSSEChunk(buffer, onEvent) {
  const events = buffer.split('\n\n');
  const remainder = events.pop();
  events.forEach((evtBlock) => {
    let event = 'message';
    let data = '';
    evtBlock.split('\n').forEach((line) => {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data += line.slice(5).trim();
    });
    if (!data) return;
    let payload;
    try {
      payload = JSON.parse(data);
    } catch {
      payload = data;
    }
    onEvent(event, payload);
  });
  return remainder;
}

async function sendMessage(message) {
  appendMsg('user', message);
  const $assistant = appendMsg('assistant', '');
  $activity.innerHTML = '';
  $sendBtn.disabled = true;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        attachments,
        skill_hint: activeSkill,
      }),
    });
    if (!resp.ok || !resp.body) throw new Error('Stream failed');
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSSEChunk(buffer, (event, data) => {
        if (event === 'delta' && data?.text) {
          $assistant.textContent += data.text;
          $messages.scrollTop = $messages.scrollHeight;
        } else if (event === 'skill_started') {
          appendMsg('system', `↳ skill: ${data?.skill_name || '(unknown)'}`);
        } else if (event === 'hosted_tool_call') {
          appendActivity({ event_type: 'hosted_tool_call', data });
        } else if (event === 'workflow_event') {
          appendActivity(data);
        } else if (event === 'artifact') {
          appendArtifact(data);
        } else if (event === 'error') {
          appendMsg('system', `error: ${data?.message}`);
        }
      });
    }
  } catch (e) {
    appendMsg('system', `error: ${e.message}`);
  } finally {
    $sendBtn.disabled = false;
  }
}

$form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = $input.value.trim();
  if (!text) return;
  $input.value = '';
  sendMessage(text);
});

loadSkills();
