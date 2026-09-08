/*
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 阻抗地形图渲染（10-20 电极位置 SVG + 按阈值着色 + 选中高亮）。
作者: Spoon
*/

const DEFAULT_ELECTRODE_POS = {
  Fp1: { x: 42, y: 14 },
  Fpz: { x: 50, y: 14 },
  Fp2: { x: 58, y: 14 },
  AF7: { x: 30, y: 20 },
  AF3: { x: 42, y: 22 },
  AF4: { x: 58, y: 22 },
  AF8: { x: 70, y: 20 },
  F7: { x: 24, y: 28 },
  F5: { x: 30, y: 30 },
  F3: { x: 36, y: 32 },
  F1: { x: 42, y: 32 },
  Fz: { x: 50, y: 32 },
  F2: { x: 58, y: 32 },
  F4: { x: 64, y: 32 },
  F6: { x: 70, y: 30 },
  F8: { x: 76, y: 28 },
  FT9: { x: 12, y: 36 },
  FT7: { x: 20, y: 40 },
  FC5: { x: 28, y: 42 },
  FC3: { x: 35, y: 42 },
  FC1: { x: 42, y: 42 },
  FCz: { x: 50, y: 42 },
  FC2: { x: 58, y: 42 },
  FC4: { x: 65, y: 42 },
  FC6: { x: 72, y: 42 },
  FT8: { x: 80, y: 40 },
  FT10: { x: 88, y: 36 },
  T7: { x: 18, y: 50 },
  C5: { x: 28, y: 50 },
  C3: { x: 35, y: 50 },
  C1: { x: 42, y: 50 },
  Cz: { x: 50, y: 50 },
  C2: { x: 58, y: 50 },
  C4: { x: 65, y: 50 },
  C6: { x: 72, y: 50 },
  T8: { x: 82, y: 50 },
  TP9: { x: 12, y: 60 },
  TP7: { x: 20, y: 60 },
  CP5: { x: 28, y: 58 },
  CP3: { x: 35, y: 58 },
  CP1: { x: 42, y: 58 },
  CPz: { x: 50, y: 58 },
  CP2: { x: 58, y: 58 },
  CP4: { x: 65, y: 58 },
  CP6: { x: 72, y: 58 },
  TP8: { x: 80, y: 60 },
  TP10: { x: 88, y: 60 },
  P7: { x: 24, y: 70 },
  P5: { x: 30, y: 68 },
  P3: { x: 36, y: 68 },
  P1: { x: 42, y: 68 },
  Pz: { x: 50, y: 68 },
  P2: { x: 58, y: 68 },
  P4: { x: 64, y: 68 },
  P6: { x: 70, y: 68 },
  P8: { x: 76, y: 70 },
  PO7: { x: 31, y: 80 },
  PO5: { x: 36, y: 78 },
  PO3: { x: 42, y: 78 },
  POz: { x: 50, y: 78 },
  PO4: { x: 58, y: 78 },
  PO6: { x: 64, y: 78 },
  PO8: { x: 69, y: 80 },
  O1: { x: 42, y: 87 },
  Oz: { x: 50, y: 87 },
  O2: { x: 58, y: 87 },
  CB1: { x: 46, y: 92 },
  CB2: { x: 54, y: 92 },
  BIAS: { x: 24, y: 95.5 },
  tDCS: { x: 76, y: 95.5 },
  A1: { x: 4, y: 50 },
  A2: { x: 96, y: 50 },
};

let headGradientSequence = 0;

function getElectrodePos(name, positions, aliases) {
  const n = String(name || '').trim();
  if (!n) return null;
  if (positions && positions[n]) return positions[n];
  const a = aliases && aliases[n];
  if (a && positions && positions[a]) return positions[a];
  return DEFAULT_ELECTRODE_POS[n] || null;
}

function createSvgRoot() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 100 100');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.style.maxHeight = '100%';
  svg.style.display = 'block';
  return svg;
}

function drawHead(svg) {
  headGradientSequence += 1;
  const gradientId = `bhb_head_bg_${headGradientSequence}`;
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const grad = document.createElementNS('http://www.w3.org/2000/svg', 'radialGradient');
  grad.setAttribute('id', gradientId);
  grad.setAttribute('cx', '50%');
  grad.setAttribute('cy', '45%');
  grad.setAttribute('r', '54%');
  const stops = [
    { o: '0%', c: 'rgb(56, 189, 248)', a: '0.075' },
    { o: '72%', c: 'rgb(56, 189, 248)', a: '0.028' },
    { o: '100%', c: 'rgb(56, 189, 248)', a: '0' },
  ];
  for (const one of stops) {
    const s = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
    s.setAttribute('offset', one.o);
    s.setAttribute('stop-color', one.c);
    s.setAttribute('stop-opacity', one.a);
    grad.appendChild(s);
  }
  defs.appendChild(grad);
  svg.appendChild(defs);

  const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  bg.setAttribute('x', '0');
  bg.setAttribute('y', '0');
  bg.setAttribute('width', '100');
  bg.setAttribute('height', '100');
  bg.setAttribute('fill', `url(#${gradientId})`);
  svg.appendChild(bg);

  const outline = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  outline.setAttribute('d', 'M50 7 C23 7 8 27 8 52 C8 78 25 94 50 94 C75 94 92 78 92 52 C92 27 77 7 50 7 Z');
  outline.setAttribute('fill', 'rgba(0,0,0,0)');
  outline.setAttribute('stroke', 'rgba(120,170,210,0.42)');
  outline.setAttribute('stroke-width', '0.86');
  svg.appendChild(outline);

  const nose = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  nose.setAttribute('d', 'M43.5 8 C45.5 2.4 48 0.8 50 0.8 C52 0.8 54.5 2.4 56.5 8');
  nose.setAttribute('fill', 'none');
  nose.setAttribute('stroke', 'rgba(120,170,210,0.42)');
  nose.setAttribute('stroke-width', '0.64');
  svg.appendChild(nose);

  const earL = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  earL.setAttribute('d', 'M7 57 C3 55 3 47 7 45 C9 44 10 46 10 51 C10 55 9 58 7 57 Z');
  earL.setAttribute('fill', 'rgba(120,170,210,0.08)');
  earL.setAttribute('stroke', 'rgba(120,170,210,0.38)');
  earL.setAttribute('stroke-width', '0.64');
  svg.appendChild(earL);

  const earR = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  earR.setAttribute('d', 'M93 57 C97 55 97 47 93 45 C91 44 90 46 90 51 C90 55 91 58 93 57 Z');
  earR.setAttribute('fill', 'rgba(120,170,210,0.08)');
  earR.setAttribute('stroke', 'rgba(120,170,210,0.38)');
  earR.setAttribute('stroke-width', '0.64');
  svg.appendChild(earR);

  const guide = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  guide.setAttribute('d', 'M50 14 L50 87 M18 50 L82 50');
  guide.setAttribute('fill', 'none');
  guide.setAttribute('stroke', 'rgba(120,170,210,0.10)');
  guide.setAttribute('stroke-width', '0.42');
  svg.appendChild(guide);
}

function classifyOhm(value, goodMax, warnMax) {
  const v = Number(value);
  if (!Number.isFinite(v) || v <= 0) return 'unknown';
  if (v <= goodMax) return 'good';
  if (v <= warnMax) return 'warn';
  return 'bad';
}

function createElectrodeMap(hostEl, channelNames, positions, aliases, groupClasses = []) {
  const svg = createSvgRoot();
  drawHead(svg);

  const circles = new Map();
  const groups = new Map();
  const texts = new Map();
  const names = Array.isArray(channelNames) ? channelNames : [];
  const total = names.length;
  const r = total >= 60 ? 2.7 : (total >= 40 ? 3.2 : 4.4);
  const fontSize = total >= 60 ? 2.1 : (total >= 40 ? 2.6 : 3.55);
  const dy = total >= 60 ? 0.7 : 0.9;

  for (const name of names) {
    const pos = getElectrodePos(name, positions, aliases);
    if (!pos) continue;
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.classList.add('electrode', ...groupClasses);
    g.dataset.name = name;

    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.classList.add('electrode-circle');
    circle.setAttribute('cx', String(pos.x));
    circle.setAttribute('cy', String(pos.y));
    circle.setAttribute('r', String(r));
    g.appendChild(circle);

    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.classList.add('electrode-text');
    text.setAttribute('x', String(pos.x));
    text.setAttribute('y', String(pos.y + dy));
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('dominant-baseline', 'middle');
    text.setAttribute('font-size', String(fontSize));
    text.setAttribute('font-weight', '650');
    text.textContent = name;
    g.appendChild(text);

    svg.appendChild(g);
    circles.set(name, circle);
    groups.set(name, g);
    texts.set(name, text);
  }

  hostEl.innerHTML = '';
  hostEl.appendChild(svg);
  return { circles, groups, texts };
}

/**
 * 只负责通道选择的通用 10-20 头皮图。
 * 阻抗和频带能量可共享空间布局，但各自保留独立的数值与颜色语义。
 *
 * options（可选，保持向后兼容）：
 * - maxCount: 最多可选通道数，默认 1（为 1 时保留旧的"直接替换"行为）
 * - minCount: 最少保留通道数，缺省跟随 maxCount（单选默认不会取消到空），再 clamp 到 [0, maxCount]
 * - onReject: 已选满 maxCount 时再点击未选电极的回调，参数为被拒绝的通道名
 * onSelect 回调传出当前已选通道名的有序数组。
 */
export function createSelectableTopomap(hostEl, channelNames, positions, aliases, options = {}) {
  if (!hostEl) return null;
  const opts = options && typeof options === 'object' ? options : {};
  const maxCountRaw = Math.floor(Number(opts.maxCount));
  const maxCount = Number.isFinite(maxCountRaw) && maxCountRaw >= 1 ? maxCountRaw : 1;
  const minCountSource = opts.minCount !== undefined && opts.minCount !== null
    ? Math.floor(Number(opts.minCount))
    : maxCount;
  const minCount = Number.isFinite(minCountSource)
    ? Math.max(0, Math.min(maxCount, minCountSource))
    : maxCount;
  const onReject = typeof opts.onReject === 'function' ? opts.onReject : null;

  const { groups, texts } = createElectrodeMap(
    hostEl,
    channelNames,
    positions,
    aliases,
    ['channel-electrode'],
  );

  let selected = [];
  let quality = {};
  let qualityVisible = false;
  let interactionMode = 'select';
  let onSelect = null;
  let onManualToggle = null;

  function paint() {
    for (const [n, g] of groups.entries()) {
      const index = selected.indexOf(n);
      const item = quality[n] && typeof quality[n] === 'object' ? quality[n] : {};
      const state = String(item.state || 'unknown');
      const manuallyExcluded = qualityVisible && (
        state === 'manual_bad' || item.manual_enabled === false
      );
      const selectionVisible = interactionMode !== 'manual-quality';
      const qualityClass = state === 'manual_bad'
        ? 'quality-manual-bad'
        : ['good', 'suspect', 'recovering', 'bad'].includes(state)
          ? `quality-${state}`
          : 'quality-unknown';
      g.classList.remove(
        'quality-good',
        'quality-suspect',
        'quality-recovering',
        'quality-bad',
        'quality-manual-bad',
        'quality-unknown',
      );
      if (qualityVisible) g.classList.add(qualityClass);
      g.classList.toggle('selected', selectionVisible && index >= 0);
      g.classList.toggle('selected-1', selectionVisible && index === 0);
      g.classList.toggle('selected-2', selectionVisible && index === 1);
      g.classList.toggle('manual-quality-mode', interactionMode === 'manual-quality');
      const text = texts.get(n);
      if (text) text.textContent = manuallyExcluded ? '×' : n;
      g.setAttribute('aria-pressed', interactionMode === 'manual-quality'
        ? (item.manual_enabled === false ? 'true' : 'false')
        : (index >= 0 ? 'true' : 'false'));
      g.setAttribute('aria-label', interactionMode === 'manual-quality'
        ? `${item.manual_enabled === false ? '启用' : '停用'}通道 ${n}`
        : `选择通道 ${n}`);
    }
  }

  function setSelected(value) {
    const list = Array.isArray(value) ? value : [value];
    selected = list
      .map((name) => String(name || ''))
      .filter((name, index, arr) => name && arr.indexOf(name) === index)
      .slice(0, maxCount);
    paint();
  }

  function setQuality(snapshot) {
    qualityVisible = !!(snapshot && snapshot.channels && typeof snapshot.channels === 'object');
    quality = qualityVisible ? snapshot.channels : {};
    paint();
  }

  function toggle(name) {
    if (interactionMode === 'manual-quality') {
      if (onManualToggle) onManualToggle(name);
      return;
    }
    const index = selected.indexOf(name);
    if (index >= 0) {
      if (selected.length > minCount) selected.splice(index, 1);
      else return;
    } else if (selected.length < maxCount) {
      selected.push(name);
    } else if (maxCount === 1) {
      selected = [name];
    } else {
      if (onReject) onReject(name);
      return;
    }
    paint();
    if (onSelect) onSelect(selected.slice());
  }

  function bindInteraction() {
    const enabled = !!onSelect || !!onManualToggle;
    for (const [n, g] of groups.entries()) {
      g.setAttribute('role', 'button');
      g.setAttribute('tabindex', enabled ? '0' : '-1');
      g.onclick = enabled ? () => toggle(n) : null;
      g.onkeydown = enabled ? (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        toggle(n);
      } : null;
    }
    paint();
  }

  function setOnSelect(fn) {
    onSelect = typeof fn === 'function' ? fn : null;
    bindInteraction();
  }

  function setOnManualToggle(fn) {
    onManualToggle = typeof fn === 'function' ? fn : null;
    bindInteraction();
  }

  function setInteractionMode(mode) {
    interactionMode = mode === 'manual-quality' ? 'manual-quality' : 'select';
    paint();
  }

  setSelected([]);
  return {
    setSelected,
    setQuality,
    setInteractionMode,
    setOnSelect,
    setOnManualToggle,
  };
}

export function createImpedanceTopomap(hostEl, channelNames, ui, positions, aliases) {
  if (!hostEl) return null;

  const goodMax = ui && typeof ui.good_max_ohm === 'number' ? ui.good_max_ohm : (ui && typeof ui.goodMaxOhm === 'number' ? ui.goodMaxOhm : 10000);
  const warnMax = ui && typeof ui.warn_max_ohm === 'number' ? ui.warn_max_ohm : (ui && typeof ui.warnMaxOhm === 'number' ? ui.warnMaxOhm : 30000);

  const { circles, groups } = createElectrodeMap(
    hostEl,
    channelNames,
    positions,
    aliases,
    ['imp-electrode', 'imp-unknown'],
  );

  let selected = '';
  let thresholds = { goodMaxOhm: goodMax, warnMaxOhm: warnMax };
  let onSelect = null;

  function setSelected(name) {
    selected = String(name || '');
    for (const [n, g] of groups.entries()) {
      if (n === selected) g.classList.add('imp-selected');
      else g.classList.remove('imp-selected');
    }
  }

  function setThresholds(next) {
    if (!next || typeof next !== 'object') return;
    const g = Number(next.goodMaxOhm);
    const w = Number(next.warnMaxOhm);
    if (Number.isFinite(g) && g > 0) thresholds.goodMaxOhm = g;
    if (Number.isFinite(w) && w > thresholds.goodMaxOhm) thresholds.warnMaxOhm = w;
  }

  function setOnSelect(fn) {
    onSelect = typeof fn === 'function' ? fn : null;
    for (const [n, g] of groups.entries()) {
      g.onclick = onSelect ? () => { setSelected(n); onSelect(n); } : null;
    }
  }

  function update(valuesByName) {
    const vm = valuesByName && typeof valuesByName === 'object' ? valuesByName : {};
    for (const [n, g] of groups.entries()) {
      const v = vm[n];
      const kind = classifyOhm(v, thresholds.goodMaxOhm, thresholds.warnMaxOhm);
      g.classList.remove('imp-good', 'imp-warn', 'imp-bad', 'imp-unknown');
      g.classList.add(`imp-${kind}`);
      const c = circles.get(n);
      if (c) {
        const title = `${n}: ${Number.isFinite(Number(v)) ? Math.round(Number(v)) : '--'} *10 Ω`;
        c.setAttribute('data-title', title);
      }
    }
  }

  return { update, setSelected, setThresholds, setOnSelect };
}
