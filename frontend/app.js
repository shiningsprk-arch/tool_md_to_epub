/**
 * md_to_epub —— 工具前端逻辑（原生 JS，无构建）。
 *
 * 与宿主的两条通道：
 *  - `MyBooksToolBridge`：toolId / theme / locale / fetch / notify（由宿主在
 *    iframe 里以 /static/toolbox-bridge.js 提供，本地单独打开时允许缺失）；
 *  - `MyBooksToolI18n`：读本地 locales/*.json，自动跟随 bridge.locale 切换语言。
 *
 * 上传走 multipart：`bridge.fetch` 是 fetch 的透传封装，理论上能带 FormData，但官方
 * 文档没有覆盖这个用法，所以失败时回退到裸 fetch（iframe 与宿主同源，cookie 自带）。
 */
(function () {
  'use strict';

  var bridge = window.MyBooksToolBridge || null;
  var i18n = window.MyBooksToolI18n.create();

  function el(id) {
    return document.getElementById(id);
  }

  var dom = {
    fileBtn: el('file-btn'),
    fileInput: el('file-input'),
    pickedFiles: el('picked-files'),
    dirBtn: el('dir-btn'),
    dirInput: el('dir-input'),
    pickedDir: el('picked-dir'),
    ignoreImages: el('ignore-images'),
    titleInput: el('title-input'),
    authorInput: el('author-input'),
    convertBtn: el('convert-btn'),
    convertText: el('convert-text'),
    spinner: el('spinner'),
    progress: el('progress'),
    error: el('error'),
    result: el('result'),
    statChapters: el('stat-chapters'),
    statImages: el('stat-images'),
    warnings: el('warnings'),
    downloadLink: el('download-link'),
  };

  var state = {
    dirFiles: [],
    plainFiles: [],
    converting: false,
    result: null,
    lastError: null,
  };

  // ------------------------------------------------------------------ 主题

  function applyTheme(theme) {
    document.body.setAttribute('data-theme', theme);
  }

  applyTheme((bridge && bridge.theme) || 'light');
  if (bridge && bridge.onThemeChange) {
    bridge.onThemeChange(applyTheme);
  }

  // ------------------------------------------------------------ 文件选择

  function hasFiles() {
    return state.dirFiles.length > 0 || state.plainFiles.length > 0;
  }

  function topFolder() {
    var first = state.dirFiles[0];
    return String((first && first.webkitRelativePath) || '').split('/')[0] || '';
  }

  function renderButton() {
    dom.convertBtn.disabled = !hasFiles() || state.converting;
    dom.convertBtn.title = hasFiles() ? '' : i18n.t('needFiles');
    dom.convertText.textContent = i18n.t(state.converting ? 'converting' : 'convertBtn');
    dom.spinner.hidden = !state.converting;
    dom.progress.hidden = !state.converting;
  }

  function renderSelection() {
    dom.pickedDir.textContent = state.dirFiles.length
      ? i18n.t('pickedDir', { name: topFolder(), n: state.dirFiles.length })
      : '';
    dom.pickedFiles.textContent = state.plainFiles.length
      ? i18n.t('pickedFiles', { n: state.plainFiles.length })
      : '';
    renderButton();
  }

  dom.dirBtn.addEventListener('click', function () {
    dom.dirInput.click();
  });

  dom.fileBtn.addEventListener('click', function () {
    dom.fileInput.click();
  });

  dom.dirInput.addEventListener('change', function (event) {
    state.dirFiles = Array.prototype.slice.call(event.target.files || []);
    if (state.dirFiles.length) {
      state.plainFiles = [];
      dom.fileInput.value = '';   // 目录与普通文件二选一
    }
    resetResult();
    renderSelection();
  });

  dom.fileInput.addEventListener('change', function (event) {
    state.plainFiles = Array.prototype.slice.call(event.target.files || []);
    if (state.plainFiles.length) {
      state.dirFiles = [];
      dom.dirInput.value = '';
    }
    resetResult();
    renderSelection();
  });

  // ---------------------------------------------------------------- 结果区

  function resetResult() {
    state.result = null;
    state.lastError = null;
    dom.result.hidden = true;
    dom.error.hidden = true;
    dom.warnings.hidden = true;
    dom.downloadLink.removeAttribute('href');
  }

  function renderResult(data) {
    state.result = data;
    dom.statChapters.textContent = String(data.chapter_count || 0);
    dom.statImages.textContent = String(data.image_count || 0);

    var warnings = data.warnings || [];
    if (warnings.length) {
      dom.warnings.textContent = i18n.t('remoteRemoved', { n: warnings.length });
      dom.warnings.hidden = false;
    } else {
      dom.warnings.hidden = true;
    }

    if (data.download_url) {
      dom.downloadLink.setAttribute('href', data.download_url);
    }
    dom.downloadLink.setAttribute('download', data.filename || 'book.epub');
    dom.downloadLink.textContent = i18n.t('downloadBtn');
    dom.result.hidden = false;
  }

  function errorText(rsp) {
    var code = (rsp && rsp.err) || 'unknown';
    var key = 'error.' + code;
    var text = i18n.t(key);
    var msg = (rsp && rsp.msg) || '';
    if (text === key) {
      return msg || code;   // 没有对应文案：用后端消息兜底（可能是中文）
    }
    // deps.missing 的 msg 是"宿主缺少依赖：<发行包名>"，包名语言无关，附上更便于排查
    return code === 'deps.missing' && msg ? text + ' — ' + msg : text;
  }

  function renderError(rsp) {
    state.lastError = rsp;
    dom.error.textContent = errorText(rsp);
    dom.error.hidden = false;
  }

  // ---------------------------------------------------------------- 转换

  function buildFormData() {
    var useDir = state.dirFiles.length > 0;
    var files = useDir ? state.dirFiles : state.plainFiles;
    var formData = new FormData();
    files.forEach(function (file) {
      formData.append('files', file, file.name);
      if (useDir) {
        // 与 files 保序对齐：后端按下标取 relative_paths
        formData.append('relative_paths', file.webkitRelativePath || file.name);
      }
    });
    formData.append('ignore_images', dom.ignoreImages.checked ? '1' : '0');
    if (dom.titleInput.value.trim()) {
      formData.append('title', dom.titleInput.value.trim());
    }
    if (dom.authorInput.value.trim()) {
      formData.append('author', dom.authorInput.value.trim());
    }
    return formData;
  }

  function rawConvert(formData) {
    var toolId = (bridge && bridge.toolId) || 'md_to_epub';
    return fetch('/api/toolbox/tool/' + toolId + '/convert', {
      method: 'POST',
      body: formData,
      credentials: 'include',
    }).then(function (resp) {
      return resp.json();
    });
  }

  function postConvert(formData) {
    if (bridge && typeof bridge.fetch === 'function') {
      // 不手动设 Content-Type，multipart 的 boundary 交给浏览器
      return bridge.fetch('convert', { method: 'POST', body: formData })
        .catch(function () {
          return rawConvert(formData);
        });
    }
    return rawConvert(formData);
  }

  function convert() {
    if (!hasFiles() || state.converting) {
      return;
    }
    resetResult();
    state.converting = true;
    renderButton();

    postConvert(buildFormData())
      .then(function (rsp) {
        if (rsp && rsp.err === 'ok') {
          renderResult(rsp.data || {});
          if (bridge && bridge.notify) {
            bridge.notify(i18n.t('success'), 'success');
          }
        } else {
          renderError(rsp || { err: 'unknown' });
        }
      })
      .catch(function (err) {
        renderError({ err: 'request_failed', msg: String((err && err.message) || err) });
      })
      .then(function () {
        state.converting = false;
        renderButton();
      });
  }

  dom.convertBtn.addEventListener('click', convert);

  // ------------------------------------------------------------ 语言切换

  // data-i18n 节点由 i18n.applyDom() 自动刷新；这里补 JS 生成的那部分文本。
  i18n.onChange(function () {
    renderSelection();
    if (state.result) {
      renderResult(state.result);
    }
    if (state.lastError) {
      renderError(state.lastError);
    }
  });

  i18n.ready.then(renderSelection);
})();
