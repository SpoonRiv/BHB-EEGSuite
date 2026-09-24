/* Experiment pages are served only after the backend confirms installation. */
import { getModules, refreshModules, getModuleStatus, installModule, uninstallModule } from './api.js';
import { navigate, registerRoute } from './router.js';

const $ = id => document.getElementById(id);
const assetRoot = '/api/modules/music/assets/';
let moduleInfo = null, moduleScript = null, loading = null;
let changing = '', actionError = '', focusReturn = null;
let installStatus = null;

function renderCatalog() {
  const installed = !!moduleInfo?.installed;
  const installing = changing === 'install';
  const uninstalling = changing === 'uninstall';
  $('module-installed-state').textContent = actionError || (installed ? `已安装 ${moduleInfo?.version || '旧版'}` :
    changing === 'install' ? '正在安装' : moduleInfo?.error || (moduleInfo?.version ? `可安装 ${moduleInfo.version}` : '检查模块源中'));
  $('module-source-error').hidden = !installed || !moduleInfo?.error;
  $('module-source-error').textContent = installed ? moduleInfo?.error || '' : '';
  $('module-install').hidden = installed && !moduleInfo?.update_available;
  $('module-install').disabled = changing || !moduleInfo?.available || !!moduleInfo?.busy;
  $('module-install').classList.toggle('module-action--busy', installing);
  $('module-install').textContent = installing ? '安装中' : installed ? `更新至 ${moduleInfo.latest_version}` : '下载并安装';
  const downloading = installing && installStatus?.phase === 'downloading';
  $('module-progress').hidden = !downloading;
  if (downloading) {
    const total = installStatus.total || 1;
    $('module-progress').value = Math.round(installStatus.downloaded / total * 100);
  }
  $('module-uninstall').hidden = !installed;
  $('module-uninstall').disabled = changing || !!moduleInfo?.busy;
  $('module-uninstall').classList.toggle('module-action--busy', uninstalling);
  $('module-uninstall').textContent = uninstalling ? '卸载中' : '卸载';
  const card = $('mode-music');
  card.classList.toggle('mode-card--uninstalled', !installed);
  card.setAttribute('aria-label', `${installed ? '进入' : '打开'}文本-音频多模态范式${installed ? '' : '模块管理'}`);
  $('music-module-state').textContent = installed ? 'READY' : '未安装';
}

async function readAsset(name) {
  const response = await fetch(assetRoot + name, { cache: 'no-store' });
  if (!response.ok) throw new Error('模块资源加载失败，请重新安装后重试');
  return response.text();
}

async function loadStyle() {
  if ($('music-module-css')) return;
  const link = document.createElement('link');
  link.id = 'music-module-css'; link.rel = 'stylesheet'; link.href = assetRoot + 'music.css';
  await new Promise((resolve, reject) => {
    link.onload = resolve;
    link.onerror = () => { link.remove(); reject(new Error('模块样式加载失败')); };
    document.head.append(link);
  });
}

async function refreshCatalog(remote = false) {
  const catalog = await (remote ? refreshModules() : getModules());
  moduleInfo = (catalog?.modules || []).find(item => item.id === 'music') || null;
  actionError = '';
  renderCatalog();
  window.dispatchEvent(new Event('app:modules-changed'));
}

function closeManager() {
  if (changing) return;
  $('module-manager-modal').hidden = true;
  focusReturn?.focus();
}

async function openManager() {
  if ($('module-manager-modal').hidden) focusReturn = document.activeElement;
  $('module-manager-modal').hidden = false;
  $('module-manager-close').focus();
  try {
    await refreshCatalog(true);
  } catch (error) {
    console.error('module catalog failed', error);
    moduleInfo = null;
    actionError = '状态读取失败';
    renderCatalog();
  }
}

async function loadPages() {
  if (moduleScript) return;
  if (loading) return loading;
  loading = (async () => {
    await loadStyle();
    if (!$('page-music')) {
      const template = document.createElement('template');
      template.innerHTML = await readAsset('pages.html');
      document.querySelector('.app-body').append(template.content);
    }
    moduleScript = await import(assetRoot + 'music.js');
  })().finally(() => { loading = null; });
  return loading;
}

async function beforeMusicPage() {
  try {
    await refreshCatalog();
    if (!moduleInfo?.installed) {
      await navigate('#mode'); await openManager(); return false;
    }
    await loadPages(); return true;
  } catch (error) {
    await navigate('#mode'); await openManager();
    console.error('module page failed', error); return false;
  }
}

async function installMusic() {
  if (changing) return;
  if (!moduleInfo?.available) return;
  const updating = !!moduleInfo.installed;
  changing = 'install'; installStatus = null; renderCatalog();
  const progressTimer = setInterval(async () => {
    try { installStatus = await getModuleStatus('music'); renderCatalog(); } catch (_) {}
  }, 350);
  try {
    await installModule('music');
    await refreshCatalog();
    if (updating) {
      history.replaceState(null, '', window.location.pathname + '#mode'); window.location.reload();
      return;
    }
  } catch (error) {
    console.error('module installation failed', error);
    actionError = error.message || '安装失败，请重试';
  } finally {
    clearInterval(progressTimer);
    changing = ''; installStatus = null; renderCatalog();
    if (!$('module-manager-modal').hidden) (moduleInfo?.installed ? $('module-uninstall') : $('module-install')).focus();
  }
}

export async function initOptionalModules() {
  registerRoute('#music', {
    pageId: 'page-music', beforeEnter: beforeMusicPage,
    onEnter: () => moduleScript.enterMusicPage(), onLeave: () => moduleScript?.leaveMusicPage(),
  });
  registerRoute('#music-export', {
    pageId: 'page-music-export', beforeEnter: beforeMusicPage,
    onEnter: () => moduleScript.enterMusicExportPage(), onLeave: () => moduleScript?.leaveMusicExportPage(),
  });
  $('btn-module-manager').onclick = openManager;
  $('mode-music').onclick = () => {
    if (changing) return;
    if (moduleInfo?.installed) void navigate('#music');
    else void openManager();
  };
  $('module-manager-close').onclick = closeManager;
  $('module-manager-modal').onclick = event => { if (event.target === $('module-manager-modal')) closeManager(); };
  document.addEventListener('keydown', event => {
    if ($('module-manager-modal').hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); closeManager(); }
    if (event.key === 'Tab') {
      const buttons = [...$('module-manager-modal').querySelectorAll('button')].filter(el => !el.hidden && !el.disabled);
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  $('module-install').onclick = installMusic;
  $('module-uninstall').onclick = async () => {
    if (changing) return;
    changing = 'uninstall'; renderCatalog();
    try {
      await uninstallModule('music');
      // Reload clears cached module event handlers before any later reinstall.
      history.replaceState(null, '', window.location.pathname + '#mode'); window.location.reload();
    } catch (error) {
      console.error('module uninstall failed', error);
      actionError = '卸载失败，请重试';
      changing = ''; renderCatalog();
    }
  };
  renderCatalog();
  try { await refreshCatalog(); } catch (error) { console.error('module catalog failed', error); }
}
