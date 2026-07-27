#!/usr/bin/env python3
"""Build the MULTILINGUAL use-case player.
Outputs:
  player-dashboard.html  (AUDIO_BASE=/intro/audio/  — served by the admin Lambda, audio via S3 302)
  player-desktop.html    (AUDIO_BASE=audio/         — offline copy next to the audio/ folder)
Audio: Polly mp3s for en/zh/yue/ja/ko; browser speechSynthesis fallback for vi/ms/id/th/fil.
"""
import base64, json, os, re

ROOT = os.path.dirname(os.path.abspath(__file__))

def b64(path, mime):
    with open(os.path.join(ROOT, path), 'rb') as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()

script = json.load(open(os.path.join(ROOT, 'script.json')))
tr = json.load(open(os.path.join(ROOT, 'translations.json')))
durs = json.load(open(os.path.join(ROOT, 'durations.json')))
order = ['s1-problem','s2-solution','s3-architecture','s4-build','s4-design','s5-results','s6-console']

# narration: en from script.json, others from translations.json
narr = {'en': {s['id']: s['narration'] for s in script['scenes']}}
for lang in tr['scenes'][order[0]]:
    narr[lang] = {sid: tr['scenes'][sid][lang] for sid in order}

titles = {s['id']: s['title'] for s in script['scenes']}
IMG = {k: b64(f'assets/{v}', 'image/jpeg') for k, v in {
    'dash': 'dashboard-live.jpg', 'obs': 'observability-panel.jpg',
    'eval': 'evaluations-panel-batch-scores.jpg', 'opt': 'optimizations-panel.jpg',
    'arch': 'architecture-tab-v2.jpg'}.items()}

LANGS = tr['languages']

# reuse the scene markup from the existing v1 build (single source of truth for visuals)
v1 = open(os.path.join(ROOT, 'agentcore-usecase-video.html')).read()
scenes_html = v1[v1.index('<!-- S1 PROBLEM -->'):v1.index('</div>\n\n<div id="bar">')]

html = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Introduction — Autonomous UI QA in CI/CD</title>
<style>__CSS__</style></head><body>

<div id="stage">
  <div id="hint">
    <div class="big">Autonomous UI QA in CI/CD</div>
    <div>An Amazon Bedrock AgentCore use case &nbsp;·&nbsp; ~5 min</div>
    <div style="display:flex;gap:10px;align-items:center">
      <label class="sub" for="langsel0">Narration:</label>
      <select id="langsel0" class="langsel"></select>
    </div>
    <div class="go" onclick="startShow(event)">▶&nbsp; Play</div>
    <div class="sub" id="ttsnote" style="max-width:520px;text-align:center"></div>
  </div>
__SCENES__
</div>

<div id="bar">
  <button id="play">▶ Play</button>
  <button id="prev">⏮</button><button id="next">⏭</button>
  <div id="prog"></div>
  <select id="langsel1" class="langsel"></select>
  <button id="mute">🔊</button>
</div>
<div id="cap"><span class="cc" id="cctext">Choose a narration language, then press play.</span></div>

<script>
const AUDIO_BASE = "__AUDIO_BASE__";
const SCENES = __SCENES_META__;
const NARR = __NARR__;
const DUR = __DUR__;
const LANGS = __LANGS__;
let LANG = localStorage.getItem('introLang') || 'en';
if (!LANGS[LANG]) LANG = 'en';

// language selectors (hint screen + control bar, kept in sync)
for (const selId of ['langsel0','langsel1']){
  const sel = document.getElementById(selId);
  for (const [code, cfg] of Object.entries(LANGS)){
    const o = document.createElement('option'); o.value = code;
    o.textContent = cfg.name + (cfg.polly ? '' : ' (browser voice)');
    sel.appendChild(o);
  }
  sel.value = LANG;
  sel.onchange = e => setLang(e.target.value);
}
function setLang(code){
  LANG = code; localStorage.setItem('introLang', code);
  document.querySelectorAll('.langsel').forEach(s=>s.value=code);
  const note = document.getElementById('ttsnote');
  note.textContent = LANGS[code].polly ? '' :
    'This language uses your browser\\'s built-in voice — quality varies by device.';
  if (idx >= 0){ const i = idx; hardStop(); go(i, playing || wasPlaying); }
}

// estimated duration for TTS langs: CJK/Thai ~7 chars/s, latin ~15 chars/s
function estDur(sid){
  const txt = NARR[LANG][sid];
  const dense = /[\\u3000-\\u9fff\\uac00-\\ud7af\\u0e00-\\u0e7f]/.test(txt);
  return Math.max(15, txt.length / (dense ? 7 : 15));
}
function sceneDur(sid){
  return (DUR[LANG] && DUR[LANG][sid]) ? DUR[LANG][sid] : estDur(sid);
}
function beatScale(sid){ return sceneDur(sid) / DUR.en[sid]; }

const stage = document.getElementById('stage');
const capEl = document.getElementById('cctext');
let idx = -1, playing = false, wasPlaying = false, audio = null, utter = null, raf = null;
let ttsT0 = 0, ttsElapsed = 0;

const prog = document.getElementById('prog');
SCENES.forEach((s,i)=>{ const seg=document.createElement('div'); seg.className='seg';
  seg.innerHTML=`<i></i><span>${i+1}. ${s.title}</span>`;
  seg.onclick=()=>go(i,true); prog.appendChild(seg); });
const segs=[...prog.children].map(x=>x.firstChild);

function curTime(){
  if (audio) return audio.currentTime;
  return ttsElapsed + (playing ? (performance.now()-ttsT0)/1000 : 0);
}
function setCaption(sid, t){
  const txt = NARR[LANG][sid]; const frac = Math.min(1, t / sceneDur(sid));
  const center = Math.floor(txt.length * frac);
  const start = Math.max(0, Math.min(center - 60, txt.length - 150));
  let seg = txt.slice(start, start + 150);
  if (start > 0) seg = '…' + seg;
  if (start + 150 < txt.length) seg = seg + '…';
  capEl.textContent = seg;
}
function beats(sceneEl, t, sid){
  const k = beatScale(sid);
  sceneEl.querySelectorAll('.beat[data-t]').forEach(el=>{
    if (t >= parseFloat(el.dataset.t) * k) el.classList.add('in');
  });
}
function tick(){
  if (!playing || idx<0) return;
  const t = curTime(); const sid = SCENES[idx].id;
  beats(document.getElementById(sid), t, sid);
  segs[idx].style.width = (100 * Math.min(1, t/sceneDur(sid))) + '%';
  setCaption(sid, t);
  if (sid === 's5-results') convCounter(t);
  if (!audio && t >= sceneDur(sid) + 0.5) { go(idx+1, true); return; }  // TTS safety net
  raf = requestAnimationFrame(tick);
}
const CONVSTEPS = [[0,'9'],[9,'5'],[11,'4'],[13,'1'],[15,'0']];
function convCounter(t){
  const el = document.getElementById('conv'); const k = beatScale('s5-results');
  let v = '9'; for (const [tt,val] of CONVSTEPS) if (t >= (tt+8)*k) v = val;
  if (el.textContent !== v){ el.textContent = v; el.style.color = v==='0' ? 'var(--green)' : 'var(--ink)'; }
}
function hardStop(){
  wasPlaying = playing; playing = false;
  cancelAnimationFrame(raf);
  if (audio){ audio.onended=null; audio.pause(); audio=null; }
  if (utter){ utter.onend=null; speechSynthesis.cancel(); utter=null; }
}
function pickVoice(){
  const want = LANGS[LANG].bcp47 || 'en-US';
  const vs = speechSynthesis.getVoices();
  return vs.find(v=>v.lang===want) || vs.find(v=>v.lang && v.lang.startsWith(want.split('-')[0])) || null;
}
function go(i, autoplay){
  if (i<0 || i>=SCENES.length){ finished(); return; }
  hardStop();
  if (idx>=0) document.getElementById(SCENES[idx].id).classList.remove('on');
  segs.forEach((s,k)=>{ s.style.width = k<i ? '100%' : '0'; });
  idx = i;
  const sid = SCENES[idx].id;
  const sc = document.getElementById(sid);
  sc.classList.add('on');
  sc.querySelectorAll('.beat[data-t]').forEach(el=>el.classList.remove('in'));
  if (sid==='s5-results'){ const c=document.getElementById('conv'); c.textContent='9'; c.style.color='var(--ink)'; }
  ttsElapsed = 0;
  if (autoplay || wasPlaying){
    playing = true; document.getElementById('play').textContent='⏸ Pause';
    if (LANGS[LANG].polly){
      audio = new Audio(AUDIO_BASE + LANG + '/' + sid + '.mp3');
      audio.muted = muted;
      audio.onended = ()=> go(idx+1, true);
      audio.onerror = ()=> { console.warn('audio failed, TTS fallback'); audio=null; speakTts(sid); };
      audio.play().catch(()=>{ audio=null; speakTts(sid); });
    } else speakTts(sid);
    tick();
  }
}
function speakTts(sid){
  if (muted){ ttsT0 = performance.now(); return; }   // silent timing-only mode
  utter = new SpeechSynthesisUtterance(NARR[LANG][sid]);
  const v = pickVoice(); if (v) utter.voice = v;
  utter.lang = LANGS[LANG].bcp47 || 'en-US';
  utter.rate = 1.0;
  utter.onend = ()=> { if (playing) go(idx+1, true); };
  ttsT0 = performance.now();
  speechSynthesis.speak(utter);
}
function finished(){ hardStop(); idx = SCENES.length-1;
  document.getElementById('play').textContent='▶ Replay';
  capEl.textContent='Finished — click Replay or any chapter above.'; }

function startShow(e){ e && e.stopPropagation(); document.getElementById('hint').style.display='none'; go(0,true); }
document.getElementById('hint').addEventListener('click', function(e){
  if (e.target.closest('.langsel') || e.target.tagName==='SELECT' || e.target.tagName==='OPTION' || e.target.tagName==='LABEL') return;
  startShow(e);
});
document.getElementById('play').onclick = ()=>{
  if (idx<0){ startShow(); return; }
  if (playing){
    playing=false; document.getElementById('play').textContent='▶ Play'; cancelAnimationFrame(raf);
    if (audio) audio.pause();
    if (utter){ ttsElapsed += (performance.now()-ttsT0)/1000; speechSynthesis.pause(); }
  } else {
    playing=true; document.getElementById('play').textContent='⏸ Pause';
    if (audio) audio.play();
    else if (utter){ speechSynthesis.resume(); ttsT0 = performance.now(); }
    else go(idx, true);
    tick();
  }
};
document.getElementById('prev').onclick = ()=> go(Math.max(0,idx-1), true);
document.getElementById('next').onclick = ()=> go(Math.min(SCENES.length-1,idx+1), true);
let muted=false;
document.getElementById('mute').onclick = (e)=>{ muted=!muted; if(audio) audio.muted=muted;
  if (muted) speechSynthesis.cancel();
  e.target.textContent=muted?'🔇':'🔊'; };
document.addEventListener('keydown',e=>{ if(e.key===' '){ e.preventDefault(); document.getElementById('play').click(); }
  if(e.key==='ArrowRight') document.getElementById('next').click();
  if(e.key==='ArrowLeft') document.getElementById('prev').click(); });
if ('speechSynthesis' in window) speechSynthesis.getVoices();  // warm voice list
setLang(LANG);
</script>
</body></html>'''

# lift CSS from v1 (between <style> and </style>) and add langsel styling
css = v1[v1.index('<style>')+7 : v1.index('</style>')]
css += """
  .langsel{background:var(--card);border:1px solid var(--line);color:var(--ink);border-radius:10px;
           padding:8px 12px;font-size:13.5px;font-weight:650;cursor:pointer}
  .langsel:hover{border-color:var(--vio)}
"""

scene_meta = json.dumps([{"id": sid, "title": titles[sid]} for sid in order])
out = html.replace('__CSS__', css).replace('__SCENES__', scenes_html, 1)
out = out.replace('__SCENES_META__', scene_meta).replace('__NARR__', json.dumps(narr, ensure_ascii=False))
out = out.replace('__DUR__', json.dumps(durs)).replace('__LANGS__', json.dumps(LANGS, ensure_ascii=False))
out = out.replace('__IMG_DASH__', IMG['dash']).replace('__IMG_OBS__', IMG['obs'])
out = out.replace('__IMG_EVAL__', IMG['eval']).replace('__IMG_OPT__', IMG['opt']).replace('__IMG_ARCH__', IMG['arch'])

for name, base in [('player-dashboard.html', '/intro/audio/'), ('player-desktop.html', 'audio/')]:
    open(os.path.join(ROOT, name), 'w').write(out.replace('__AUDIO_BASE__', base))
    print(f"wrote {name} ({os.path.getsize(os.path.join(ROOT,name))/1e6:.1f} MB)")
