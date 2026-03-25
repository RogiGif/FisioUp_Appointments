(function () {
  const configNode = document.getElementById('post-designer-config');
  if (!configNode) return;

  const config = JSON.parse(configNode.textContent || '{}');
  const canvas = document.getElementById('post-designer-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const alignButtons = Array.from(document.querySelectorAll('[data-align-target][data-align-mode]'));
  const previewShell = document.querySelector('.post-designer-shell');
  const previewColumn = document.getElementById('post-designer-preview-column');
  const previewCard = document.getElementById('post-designer-preview-card');
  const PREVIEW_FIXED_TOP = 104;
  let previewPinTicking = false;

  const elements = {
    image: document.getElementById('post-designer-image'),
    format: document.getElementById('post-designer-format'),
    preset: document.getElementById('post-designer-preset'),
    logoVariant: document.getElementById('post-designer-logo-variant'),
    logoColor: document.getElementById('post-designer-logo-color'),
    accent: document.getElementById('post-designer-accent'),
    textColor: document.getElementById('post-designer-text-color'),
    overlay: document.getElementById('post-designer-overlay'),
    overlayDirection: document.getElementById('post-designer-overlay-direction'),
    title: document.getElementById('post-designer-title'),
    subtitle: document.getElementById('post-designer-subtitle'),
    kind: document.getElementById('post-designer-kind'),
    body: document.getElementById('post-designer-body'),
    logoX: document.getElementById('post-designer-logo-x'),
    logoY: document.getElementById('post-designer-logo-y'),
    logoXValue: document.getElementById('post-designer-logo-x-value'),
    logoYValue: document.getElementById('post-designer-logo-y-value'),
    titleX: document.getElementById('post-designer-title-x'),
    titleY: document.getElementById('post-designer-title-y'),
    titleXValue: document.getElementById('post-designer-title-x-value'),
    titleYValue: document.getElementById('post-designer-title-y-value'),
    subtitleX: document.getElementById('post-designer-subtitle-x'),
    subtitleY: document.getElementById('post-designer-subtitle-y'),
    subtitleXValue: document.getElementById('post-designer-subtitle-x-value'),
    subtitleYValue: document.getElementById('post-designer-subtitle-y-value'),
    titleSize: document.getElementById('post-designer-title-size'),
    subtitleSize: document.getElementById('post-designer-subtitle-size'),
    reset: document.getElementById('post-designer-reset-btn'),
    download: document.getElementById('post-designer-download-btn'),
    createHighlight: document.getElementById('post-designer-highlight-btn'),
    presetName: document.getElementById('post-designer-template-name'),
    savedPresetSelect: document.getElementById('post-designer-saved-presets'),
    savePreset: document.getElementById('post-designer-save-template-btn'),
    applyPreset: document.getElementById('post-designer-apply-template-btn'),
    deletePreset: document.getElementById('post-designer-delete-template-btn'),
    previewZoom: document.getElementById('post-designer-preview-zoom'),
    status: document.getElementById('post-designer-status'),
  };

  const formats = Object.fromEntries((config.formats || []).map((item) => [item.id, item]));
  const presets = Object.fromEntries((config.presets || []).map((item) => [item.id, item]));
  const STORAGE_KEY = 'fisioapp.postDesigner.templates';
  const AUTOSAVE_KEY = 'fisioapp.postDesigner.autosave';

  const defaults = {
    format: (config.formats && config.formats[0] && config.formats[0].id) || 'landscape',
    preset: (config.presets && config.presets[0] && config.presets[0].id) || 'hero-left',
    logoVariant: 'white',
    logoColor: '#14b8e6',
    accent: '#14b8e6',
    textColor: '#ffffff',
    overlay: 28,
    overlayDirection: 'left',
    title: 'Fisioterapia pélvica',
    subtitle: 'Preparação para o parto',
    kind: 'news',
    body: '',
    titleSize: 62,
    subtitleSize: 38,
    previewZoom: 'fit',
    positions: {},
  };

  const state = {
    backgroundImage: null,
    backgroundUrl: null,
    logos: {
      default: loadImage(config.logoDefaultUrl),
      white: loadImage(config.logoWhiteUrl),
    },
    positions: {},
    renderBoxes: {},
    drag: null,
    hoverTarget: '',
  };

  function getCookie(name) {
    const cookies = document.cookie ? document.cookie.split(';') : [];
    for (let index = 0; index < cookies.length; index += 1) {
      const cookie = cookies[index].trim();
      if (cookie.startsWith(`${name}=`)) {
        return decodeURIComponent(cookie.substring(name.length + 1));
      }
    }
    return '';
  }

  function loadImage(src) {
    const image = new Image();
    image.src = src;
    return image;
  }

  function currentSettings() {
    return {
      format: elements.format.value,
      preset: elements.preset.value,
      logoVariant: elements.logoVariant.value,
      logoColor: elements.logoColor.value,
      accent: elements.accent.value,
      textColor: elements.textColor.value,
      overlay: Number(elements.overlay.value),
      overlayDirection: elements.overlayDirection.value,
      title: elements.title.value,
      subtitle: elements.subtitle.value,
      kind: elements.kind.value,
      body: elements.body.value,
      titleSize: Number(elements.titleSize.value),
      subtitleSize: Number(elements.subtitleSize.value),
      previewZoom: elements.previewZoom ? elements.previewZoom.value : defaults.previewZoom,
      positions: state.positions,
    };
  }

  function getCurrentPreset() {
    return presets[elements.preset.value] || config.presets[0];
  }

  function syncLogoColorAvailability() {
    if (!elements.logoColor || !elements.logoVariant) return;
    const isCustom = elements.logoVariant.value === 'custom';
    elements.logoColor.disabled = !isCustom;
  }

  function syncPreviewZoom() {
    if (!canvas) return;
    if (!elements.previewZoom || window.innerWidth < 1200) {
      canvas.style.width = '100%';
      canvas.style.maxWidth = '100%';
      canvas.style.maxHeight = window.innerWidth < 1200 ? 'none' : 'calc(100vh - 270px)';
      return;
    }

    const zoomValue = elements.previewZoom.value || defaults.previewZoom;
    if (zoomValue === 'fit') {
      canvas.style.width = '100%';
      canvas.style.maxWidth = '100%';
      canvas.style.maxHeight = 'calc(100vh - 270px)';
      return;
    }

    canvas.style.maxHeight = 'none';
    canvas.style.maxWidth = 'none';
    canvas.style.width = `${zoomValue}%`;
  }

  function resetPreviewPin() {
    if (!previewColumn || !previewCard) return;
    previewColumn.style.removeProperty('height');
    previewCard.style.removeProperty('width');
    previewCard.classList.remove('is-fixed', 'is-bottom');
  }

  function syncPreviewPin() {
    if (!previewShell || !previewColumn || !previewCard) return;

    if (window.innerWidth < 1200) {
      resetPreviewPin();
      return;
    }

    previewCard.classList.remove('is-fixed', 'is-bottom');
    previewCard.style.removeProperty('width');

    const shellHeight = previewShell.offsetHeight;
    const cardHeight = previewCard.offsetHeight;
    if (!shellHeight || !cardHeight || shellHeight <= cardHeight) {
      resetPreviewPin();
      return;
    }

    previewColumn.style.height = `${shellHeight}px`;

    const scrollY = window.scrollY || window.pageYOffset || 0;
    const shellRect = previewShell.getBoundingClientRect();
    const columnRect = previewColumn.getBoundingClientRect();
    const shellTop = scrollY + shellRect.top;
    const shellBottom = shellTop + shellHeight;
    const columnTop = scrollY + columnRect.top;
    const fixedStart = columnTop - PREVIEW_FIXED_TOP;
    const fixedEnd = shellBottom - cardHeight - PREVIEW_FIXED_TOP;

    if (scrollY <= fixedStart) {
      return;
    }

    if (scrollY >= fixedEnd) {
      previewCard.classList.add('is-bottom');
      return;
    }

    previewCard.classList.add('is-fixed');
    previewCard.style.width = `${columnRect.width}px`;
  }

  function requestPreviewPinSync() {
    if (previewPinTicking) return;
    previewPinTicking = true;
    window.requestAnimationFrame(() => {
      previewPinTicking = false;
      syncPreviewPin();
    });
  }

  function getSafeZone() {
    const formatId = elements.format.value || defaults.format;
    if (formatId === 'story') {
      return { left: 0.08, right: 0.08, top: 0.07, bottom: 0.09 };
    }
    if (formatId === 'square') {
      return { left: 0.07, right: 0.07, top: 0.07, bottom: 0.07 };
    }
    return { left: 0.06, right: 0.06, top: 0.06, bottom: 0.07 };
  }

  function applySettings(settings) {
    const merged = { ...defaults, ...(settings || {}) };
    elements.format.value = merged.format;
    syncCanvasSize();
    elements.preset.value = merged.preset;
    elements.logoVariant.value = merged.logoVariant;
    elements.logoColor.value = merged.logoColor || defaults.logoColor;
    elements.accent.value = merged.accent;
    elements.textColor.value = merged.textColor;
    elements.overlay.value = merged.overlay;
    elements.overlayDirection.value = merged.overlayDirection || defaults.overlayDirection;
    elements.title.value = merged.title;
    elements.subtitle.value = merged.subtitle;
    elements.kind.value = merged.kind || defaults.kind;
    elements.body.value = merged.body || defaults.body;
    elements.titleSize.value = merged.titleSize;
    elements.subtitleSize.value = merged.subtitleSize;
    if (elements.previewZoom) {
      elements.previewZoom.value = merged.previewZoom || defaults.previewZoom;
    }
    state.positions = { ...(merged.positions || {}) };
    updatePositionControls();
    syncLogoColorAvailability();
    syncPreviewZoom();
  }

  function loadAutosave() {
    try {
      return JSON.parse(localStorage.getItem(AUTOSAVE_KEY) || 'null');
    } catch (error) {
      return null;
    }
  }

  function saveAutosave() {
    localStorage.setItem(AUTOSAVE_KEY, JSON.stringify(currentSettings()));
  }

  function loadSavedTemplates() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    } catch (error) {
      return {};
    }
  }

  function saveSavedTemplates(data) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  }

  function refreshSavedTemplateOptions(selectedKey) {
    const templates = loadSavedTemplates();
    elements.savedPresetSelect.innerHTML = '<option value="">Escolher preset guardado</option>';
    Object.keys(templates).sort().forEach((key) => {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = key;
      elements.savedPresetSelect.appendChild(option);
    });
    if (selectedKey && templates[selectedKey]) {
      elements.savedPresetSelect.value = selectedKey;
    }
  }

  function syncCanvasSize() {
    const selected = formats[elements.format.value] || formats[defaults.format];
    if (!selected) return;
    canvas.width = selected.width;
    canvas.height = selected.height;
  }

  function hexToRgba(hex, alpha) {
    const normalized = String(hex || '').replace('#', '');
    const safe = normalized.length === 3
      ? normalized.split('').map((char) => char + char).join('')
      : normalized;
    const r = parseInt(safe.substring(0, 2), 16) || 0;
    const g = parseInt(safe.substring(2, 4), 16) || 0;
    const b = parseInt(safe.substring(4, 6), 16) || 0;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function drawBackground(targetCtx) {
    targetCtx.clearRect(0, 0, canvas.width, canvas.height);
    if (state.backgroundImage) {
      const img = state.backgroundImage;
      const imageRatio = img.width / img.height;
      const canvasRatio = canvas.width / canvas.height;
      let drawWidth = canvas.width;
      let drawHeight = canvas.height;
      let offsetX = 0;
      let offsetY = 0;

      if (imageRatio > canvasRatio) {
        drawHeight = canvas.height;
        drawWidth = drawHeight * imageRatio;
        offsetX = (canvas.width - drawWidth) / 2;
      } else {
        drawWidth = canvas.width;
        drawHeight = drawWidth / imageRatio;
        offsetY = (canvas.height - drawHeight) / 2;
      }

      targetCtx.drawImage(img, offsetX, offsetY, drawWidth, drawHeight);
    } else {
      const gradient = targetCtx.createLinearGradient(0, 0, canvas.width, canvas.height);
      gradient.addColorStop(0, '#d8dde8');
      gradient.addColorStop(1, '#eef2f7');
      targetCtx.fillStyle = gradient;
      targetCtx.fillRect(0, 0, canvas.width, canvas.height);
    }

    const overlayStrength = Number(elements.overlay.value) / 100;
    if (overlayStrength > 0) {
      const overlayDirection = elements.overlayDirection.value || defaults.overlayDirection;
      const gradient = overlayDirection === 'right'
        ? targetCtx.createLinearGradient(canvas.width, 0, canvas.width * 0.28, 0)
        : targetCtx.createLinearGradient(0, 0, canvas.width * 0.72, 0);
      gradient.addColorStop(0, hexToRgba('#0f172a', overlayStrength * 0.85));
      gradient.addColorStop(0.42, hexToRgba('#0f172a', overlayStrength * 0.32));
      gradient.addColorStop(1, 'rgba(15, 23, 42, 0)');
      targetCtx.fillStyle = gradient;
      targetCtx.fillRect(0, 0, canvas.width, canvas.height);
    }
  }

  function drawLogo(targetCtx, preset) {
    const logoVariant = elements.logoVariant.value;
    const logo = logoVariant === 'default' ? state.logos.default : state.logos.white;
    if (!logo) return;
    if (!logo.complete) {
      logo.addEventListener('load', render, { once: true });
      return;
    }
    const targetWidth = canvas.width * preset.logo.width;
    const ratio = logo.height / logo.width || 0.24;
    const targetHeight = targetWidth * ratio;
    const position = getResolvedPosition('logo', preset.logo);
    const x = canvas.width * position.x;
    const y = canvas.height * position.y;
    if (logoVariant === 'default') {
      targetCtx.drawImage(logo, x, y, targetWidth, targetHeight);
    } else {
      const tintColor = logoVariant === 'custom'
        ? (elements.logoColor.value || defaults.logoColor)
        : '#ffffff';
      const offscreen = document.createElement('canvas');
      offscreen.width = Math.max(1, Math.round(targetWidth));
      offscreen.height = Math.max(1, Math.round(targetHeight));
      const offscreenCtx = offscreen.getContext('2d');
      offscreenCtx.drawImage(logo, 0, 0, offscreen.width, offscreen.height);
      offscreenCtx.globalCompositeOperation = 'source-atop';
      offscreenCtx.fillStyle = tintColor;
      offscreenCtx.fillRect(0, 0, offscreen.width, offscreen.height);
      offscreenCtx.globalCompositeOperation = 'source-over';
      targetCtx.drawImage(offscreen, x, y, targetWidth, targetHeight);
    }
    state.renderBoxes.logo = { x, y, width: targetWidth, height: targetHeight, anchorX: position.x, anchorY: position.y, align: 'left' };
  }

  function wrapText(text, maxWidth) {
    const words = String(text || '').trim().split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    const lines = [];
    let current = words[0];
    for (let index = 1; index < words.length; index += 1) {
      const testLine = `${current} ${words[index]}`;
      if (ctx.measureText(testLine).width <= maxWidth) {
        current = testLine;
      } else {
        lines.push(current);
        current = words[index];
      }
    }
    lines.push(current);
    return lines;
  }

  function drawTextBlock(targetCtx, lines, options) {
    if (!lines.length) return;
    const paddingX = options.paddingX;
    const paddingY = options.paddingY;
    const lineHeight = options.lineHeight;
    const metrics = lines.map((line) => {
      const measure = targetCtx.measureText(line);
      const left = Math.max(0, measure.actualBoundingBoxLeft || 0);
      const right = Math.max(0, measure.actualBoundingBoxRight || measure.width || 0);
      return {
        line,
        left,
        right,
        width: left + right,
      };
    });
    const maxMetricWidth = Math.max(...metrics.map((item) => item.width));
    const blockWidth = maxMetricWidth + (paddingX * 2);
    const blockHeight = (lines.length * lineHeight) + (paddingY * 2) - (lineHeight * 0.2);
    const startX = options.align === 'right' ? options.x - blockWidth : options.x;
    const startY = options.y;

    targetCtx.fillStyle = options.background;
    targetCtx.fillRect(startX, startY, blockWidth, blockHeight);

    targetCtx.fillStyle = options.color;
    targetCtx.textAlign = 'left';
    targetCtx.textBaseline = 'top';
    metrics.forEach((item, index) => {
      const textX = startX + paddingX + ((maxMetricWidth - item.width) / 2) + item.left;
      targetCtx.fillText(item.line, textX, startY + paddingY + (index * lineHeight));
    });
    targetCtx.textAlign = 'start';
    return {
      x: startX,
      y: startY,
      width: blockWidth,
      height: blockHeight,
      anchorX: options.xRel,
      anchorY: options.yRel,
      align: options.align,
    };
  }

  function drawCopy(targetCtx, preset) {
    const accent = elements.accent.value;
    const textColor = elements.textColor.value;
    const titleSize = Math.round((Number(elements.titleSize.value) / 1200) * canvas.width);
    const subtitleSize = Math.round((Number(elements.subtitleSize.value) / 1200) * canvas.width);

    targetCtx.font = `500 ${titleSize}px Montserrat, Arial, sans-serif`;
    const titleLines = wrapText(elements.title.value, canvas.width * preset.title_box.w);
    const titlePosition = getResolvedPosition('title', preset.title_box);
    state.renderBoxes.title = drawTextBlock(targetCtx, titleLines, {
      x: canvas.width * titlePosition.x,
      y: canvas.height * titlePosition.y,
      xRel: titlePosition.x,
      yRel: titlePosition.y,
      align: preset.title_box.align,
      background: accent,
      color: textColor,
      lineHeight: Math.round(titleSize * 1.12),
      paddingX: Math.max(14, Math.round(canvas.width * 0.015)),
      paddingY: Math.max(10, Math.round(canvas.height * 0.012)),
    });

    targetCtx.font = `400 ${subtitleSize}px Montserrat, Arial, sans-serif`;
    const subtitleLines = wrapText(elements.subtitle.value, canvas.width * preset.subtitle_box.w);
    const subtitlePosition = getResolvedPosition('subtitle', preset.subtitle_box);
    state.renderBoxes.subtitle = drawTextBlock(targetCtx, subtitleLines, {
      x: canvas.width * subtitlePosition.x,
      y: canvas.height * subtitlePosition.y,
      xRel: subtitlePosition.x,
      yRel: subtitlePosition.y,
      align: preset.subtitle_box.align,
      background: accent,
      color: textColor,
      lineHeight: Math.round(subtitleSize * 1.16),
      paddingX: Math.max(14, Math.round(canvas.width * 0.013)),
      paddingY: Math.max(9, Math.round(canvas.height * 0.011)),
    });
  }

  function drawSafeZoneGuides(targetCtx) {
    const zone = getSafeZone();
    const left = canvas.width * zone.left;
    const top = canvas.height * zone.top;
    const width = canvas.width * (1 - zone.left - zone.right);
    const height = canvas.height * (1 - zone.top - zone.bottom);

    targetCtx.save();
    targetCtx.strokeStyle = 'rgba(255, 255, 255, 0.7)';
    targetCtx.lineWidth = Math.max(1.5, canvas.width * 0.0016);
    targetCtx.setLineDash([12, 10]);
    targetCtx.strokeRect(left, top, width, height);
    targetCtx.fillStyle = 'rgba(15, 23, 42, 0.55)';
    targetCtx.fillRect(left + 12, top + 12, 118, 28);
    targetCtx.fillStyle = '#ffffff';
    targetCtx.font = `600 ${Math.max(12, canvas.width * 0.013)}px Inter, Arial, sans-serif`;
    targetCtx.textBaseline = 'middle';
    targetCtx.fillText('Zona segura', left + 24, top + 26);
    targetCtx.restore();
  }

  function render(options) {
    const renderOptions = {
      includeGuides: true,
      includeSelection: true,
      ...(options || {}),
    };
    const preset = presets[elements.preset.value] || config.presets[0];
    if (!preset) return;
    state.renderBoxes = {};
    drawBackground(ctx);
    if (renderOptions.includeGuides) {
      drawSafeZoneGuides(ctx);
    }
    drawLogo(ctx, preset);
    drawCopy(ctx, preset);
    if (renderOptions.includeSelection) {
      drawSelectionOutline();
    }
    updatePositionControls();
    saveAutosave();
    requestPreviewPinSync();
  }

  function drawSelectionOutline() {
    const activeKey = state.drag ? state.drag.type : state.hoverTarget;
    if (!activeKey || !state.renderBoxes[activeKey]) return;
    const box = state.renderBoxes[activeKey];
    ctx.save();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = Math.max(2, canvas.width * 0.0025);
    ctx.setLineDash([10, 8]);
    ctx.strokeRect(box.x, box.y, box.width, box.height);
    ctx.restore();
  }

  function getResolvedPosition(key, basePosition) {
    return state.positions[key] || { x: basePosition.x, y: basePosition.y };
  }

  function updatePositionControls() {
    const preset = getCurrentPreset();
    if (!preset) return;

    const controls = [
      { key: 'logo', base: preset.logo, x: elements.logoX, y: elements.logoY, xValue: elements.logoXValue, yValue: elements.logoYValue },
      { key: 'title', base: preset.title_box, x: elements.titleX, y: elements.titleY, xValue: elements.titleXValue, yValue: elements.titleYValue },
      { key: 'subtitle', base: preset.subtitle_box, x: elements.subtitleX, y: elements.subtitleY, xValue: elements.subtitleXValue, yValue: elements.subtitleYValue },
    ];

    controls.forEach((control) => {
      const position = getResolvedPosition(control.key, control.base);
      const xPercent = Math.round(position.x * 100);
      const yPercent = Math.round(position.y * 100);
      if (control.x) control.x.value = String(xPercent);
      if (control.y) control.y.value = String(yPercent);
      if (control.xValue) control.xValue.textContent = `${xPercent}%`;
      if (control.yValue) control.yValue.textContent = `${yPercent}%`;
    });
  }

  function getCanvasPoint(event) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  }

  function findHitTarget(point) {
    const order = ['subtitle', 'title', 'logo'];
    for (let index = 0; index < order.length; index += 1) {
      const key = order[index];
      const box = state.renderBoxes[key];
      if (!box) continue;
      if (
        point.x >= box.x &&
        point.x <= box.x + box.width &&
        point.y >= box.y &&
        point.y <= box.y + box.height
      ) {
        return key;
      }
    }
    return '';
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function bindPositionSlider(key, axis, input, output, min, max) {
    if (!input) return;
    const handler = () => {
      const preset = getCurrentPreset();
      if (!preset) return;
      const base = key === 'logo' ? preset.logo : key === 'title' ? preset.title_box : preset.subtitle_box;
      const current = getResolvedPosition(key, base);
      const nextValue = clamp(Number(input.value) / 100, min, max);
      state.positions[key] = {
        x: axis === 'x' ? nextValue : current.x,
        y: axis === 'y' ? nextValue : current.y,
      };
      if (output) output.textContent = `${Math.round(nextValue * 100)}%`;
      render();
    };
    input.addEventListener('input', handler);
    input.addEventListener('change', handler);
  }

  function quickAlignElement(target, mode) {
    const preset = getCurrentPreset();
    const box = state.renderBoxes[target];
    if (!preset || !box) {
      render();
      return;
    }

    const safeZone = getSafeZone();
    const leftPx = canvas.width * safeZone.left;
    const rightPx = canvas.width * (1 - safeZone.right);
    const centerPx = canvas.width / 2;

    let anchorX;
    if (target === 'logo') {
      if (mode === 'left') anchorX = leftPx / canvas.width;
      if (mode === 'center') anchorX = (centerPx - (box.width / 2)) / canvas.width;
      if (mode === 'right') anchorX = (rightPx - box.width) / canvas.width;
    } else {
      const isRightAnchored = box.align === 'right';
      if (mode === 'left') anchorX = isRightAnchored ? (leftPx + box.width) / canvas.width : leftPx / canvas.width;
      if (mode === 'center') anchorX = isRightAnchored ? (centerPx + (box.width / 2)) / canvas.width : (centerPx - (box.width / 2)) / canvas.width;
      if (mode === 'right') anchorX = isRightAnchored ? rightPx / canvas.width : (rightPx - box.width) / canvas.width;
    }

    const current = state.positions[target] || {};
    state.positions[target] = {
      x: clamp(anchorX, 0.02, 0.98),
      y: typeof current.y === 'number'
        ? current.y
        : getResolvedPosition(target, target === 'title' ? preset.title_box : target === 'subtitle' ? preset.subtitle_box : preset.logo).y,
    };
    render();
    setStatus(`Alinhamento ${mode === 'left' ? 'à esquerda' : mode === 'center' ? 'ao centro' : 'à direita'} aplicado.`, 'neutral');
  }

  function setStatus(message, tone) {
    if (!elements.status) return;
    const tones = {
      neutral: 'text-muted',
      success: 'text-success',
      danger: 'text-danger',
    };
    elements.status.className = tones[tone] || tones.neutral;
    elements.status.textContent = message || '';
  }

  function exportCanvas() {
    render({ includeGuides: false, includeSelection: false });
    const imageData = canvas.toDataURL('image/png');
    render();
    return imageData;
  }

  function slugifyFilename(text) {
    return String(text || 'post-fisioup')
      .toLowerCase()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'post-fisioup';
  }

  elements.image.addEventListener('change', (event) => {
    const file = (event.target.files || [])[0];
    if (!file) {
      state.backgroundImage = null;
      render();
      return;
    }
    if (state.backgroundUrl) {
      URL.revokeObjectURL(state.backgroundUrl);
    }
    const objectUrl = URL.createObjectURL(file);
    state.backgroundUrl = objectUrl;
    const image = new Image();
    image.onload = function () {
      state.backgroundImage = image;
      render();
      setStatus('Imagem carregada com sucesso.', 'success');
    };
    image.src = objectUrl;
  });

  [
    elements.format,
    elements.preset,
    elements.logoVariant,
    elements.logoColor,
    elements.accent,
    elements.textColor,
    elements.overlay,
    elements.overlayDirection,
    elements.title,
    elements.subtitle,
    elements.kind,
    elements.body,
    elements.titleSize,
    elements.subtitleSize,
  ].forEach((element) => {
    if (!element) return;
    element.addEventListener('input', () => {
      if (element === elements.format) syncCanvasSize();
      if (element === elements.logoVariant) syncLogoColorAvailability();
      render();
    });
    element.addEventListener('change', () => {
      if (element === elements.format) syncCanvasSize();
      if (element === elements.preset) state.positions = {};
      if (element === elements.logoVariant) syncLogoColorAvailability();
      render();
    });
  });

  if (elements.previewZoom) {
    elements.previewZoom.addEventListener('change', () => {
      syncPreviewZoom();
      requestPreviewPinSync();
      saveAutosave();
    });
  }

  elements.reset.addEventListener('click', () => {
    applySettings(defaults);
    elements.image.value = '';
    if (state.backgroundUrl) {
      URL.revokeObjectURL(state.backgroundUrl);
      state.backgroundUrl = null;
    }
    state.backgroundImage = null;
    state.positions = {};
    render();
    setStatus('Editor reposto aos valores base.', 'neutral');
  });

  elements.download.addEventListener('click', () => {
    const link = document.createElement('a');
    const selectedFormat = formats[elements.format.value] || formats[defaults.format];
    link.href = exportCanvas();
    link.download = `${slugifyFilename(elements.title.value)}-${selectedFormat.id}.png`;
    link.click();
    setStatus('PNG exportado.', 'success');
  });

  elements.savePreset.addEventListener('click', () => {
    const name = String(elements.presetName.value || '').trim();
    if (!name) {
      setStatus('Indica um nome para guardar o preset.', 'danger');
      return;
    }
    const templates = loadSavedTemplates();
    templates[name] = currentSettings();
    saveSavedTemplates(templates);
    refreshSavedTemplateOptions(name);
    elements.presetName.value = '';
    setStatus('Preset guardado neste navegador.', 'success');
  });

  elements.applyPreset.addEventListener('click', () => {
    const key = elements.savedPresetSelect.value;
    const templates = loadSavedTemplates();
    if (!key || !templates[key]) {
      setStatus('Escolhe um preset guardado.', 'danger');
      return;
    }
    applySettings(templates[key]);
    render();
    setStatus('Preset aplicado.', 'success');
  });

  elements.deletePreset.addEventListener('click', () => {
    const key = elements.savedPresetSelect.value;
    const templates = loadSavedTemplates();
    if (!key || !templates[key]) {
      setStatus('Escolhe um preset para apagar.', 'danger');
      return;
    }
    delete templates[key];
    saveSavedTemplates(templates);
    refreshSavedTemplateOptions();
    setStatus('Preset removido.', 'neutral');
  });

  if (elements.createHighlight) {
    elements.createHighlight.addEventListener('click', async () => {
      try {
        setStatus('A criar rascunho...', 'neutral');
        const formData = new FormData();
        formData.append('csrfmiddlewaretoken', getCookie('csrftoken'));
        formData.append('title', elements.title.value || defaults.title);
        formData.append('subtitle', elements.subtitle.value || '');
        formData.append('kind', elements.kind.value || defaults.kind);
        formData.append('body', elements.body.value || '');
        formData.append('format', elements.format.value || defaults.format);
        formData.append('image_data', exportCanvas());

        const response = await fetch(config.createHighlightUrl, {
          method: 'POST',
          body: formData,
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
          },
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || 'Não foi possível criar o rascunho.');
        }
        setStatus(payload.message || 'Rascunho criado com sucesso.', 'success');
        if (payload.redirect_url) {
          window.location.href = payload.redirect_url;
        }
      } catch (error) {
        setStatus(error.message || 'Erro ao criar o rascunho.', 'danger');
      }
    });
  }

  Promise.all([
    new Promise((resolve) => state.logos.default.complete ? resolve() : state.logos.default.addEventListener('load', resolve, { once: true })),
    new Promise((resolve) => state.logos.white.complete ? resolve() : state.logos.white.addEventListener('load', resolve, { once: true })),
    ...(document.fonts && typeof document.fonts.load === 'function'
      ? [
          document.fonts.load('500 62px Montserrat'),
          document.fonts.load('400 38px Montserrat'),
        ]
      : []),
  ]).finally(() => {
    applySettings(loadAutosave() || defaults);
    refreshSavedTemplateOptions();
    render();
  });

  window.addEventListener('scroll', requestPreviewPinSync, { passive: true });
  window.addEventListener('resize', () => {
    syncPreviewZoom();
    requestPreviewPinSync();
  });

  canvas.addEventListener('pointerdown', (event) => {
    const point = getCanvasPoint(event);
    const hitTarget = findHitTarget(point);
    if (!hitTarget) return;
    const box = state.renderBoxes[hitTarget];
    state.drag = {
      type: hitTarget,
      startX: point.x,
      startY: point.y,
      originX: box.anchorX,
      originY: box.anchorY,
    };
    canvas.setPointerCapture(event.pointerId);
    canvas.style.cursor = 'grabbing';
    render();
  });

  canvas.addEventListener('pointermove', (event) => {
    const point = getCanvasPoint(event);
    if (state.drag) {
      const deltaX = (point.x - state.drag.startX) / canvas.width;
      const deltaY = (point.y - state.drag.startY) / canvas.height;
      state.positions[state.drag.type] = {
        x: clamp(state.drag.originX + deltaX, 0.02, 0.98),
        y: clamp(state.drag.originY + deltaY, 0.02, 0.95),
      };
      render();
      return;
    }
    state.hoverTarget = findHitTarget(point);
    canvas.style.cursor = state.hoverTarget ? 'grab' : 'default';
    render();
  });

  function releaseDrag(event) {
    if (state.drag) {
      state.drag = null;
      if (event && typeof canvas.releasePointerCapture === 'function') {
        try {
          canvas.releasePointerCapture(event.pointerId);
        } catch (error) {
          // no-op
        }
      }
      canvas.style.cursor = state.hoverTarget ? 'grab' : 'default';
      render();
      setStatus('Posição atualizada no preview.', 'neutral');
    }
  }

  canvas.addEventListener('pointerup', releaseDrag);
  canvas.addEventListener('pointerleave', () => {
    state.hoverTarget = '';
    if (!state.drag) {
      canvas.style.cursor = 'default';
      render();
    }
  });
  canvas.addEventListener('pointercancel', releaseDrag);

  bindPositionSlider('logo', 'x', elements.logoX, elements.logoXValue, 0.02, 0.98);
  bindPositionSlider('logo', 'y', elements.logoY, elements.logoYValue, 0.02, 0.95);
  bindPositionSlider('title', 'x', elements.titleX, elements.titleXValue, 0.02, 0.98);
  bindPositionSlider('title', 'y', elements.titleY, elements.titleYValue, 0.02, 0.95);
  bindPositionSlider('subtitle', 'x', elements.subtitleX, elements.subtitleXValue, 0.02, 0.98);
  bindPositionSlider('subtitle', 'y', elements.subtitleY, elements.subtitleYValue, 0.02, 0.95);

  alignButtons.forEach((button) => {
    button.addEventListener('click', () => {
      quickAlignElement(button.dataset.alignTarget, button.dataset.alignMode);
    });
  });
}());
