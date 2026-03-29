/* One language in the sidebar; language switch link in the menu bar. */
(function () {
  var path = window.location.pathname || '';
  var isCn = path.indexOf('/cn/') !== -1;

  var sidebar = document.querySelector('#sidebar .sidebar-scrollbox ol.chapter');
  if (sidebar) {
    var titles = sidebar.querySelectorAll(':scope > li.part-title');
    if (titles.length >= 2) {
      var cnPartTitle = titles[1];
      var el = sidebar.firstElementChild;
      var next;
      if (isCn) {
        while (el && el !== cnPartTitle) {
          next = el.nextElementSibling;
          el.style.display = 'none';
          el = next;
        }
      } else {
        el = cnPartTitle;
        while (el) {
          next = el.nextElementSibling;
          el.style.display = 'none';
          el = next;
        }
      }
    }
  }

  function otherLangPath(p) {
    if (p.indexOf('/cn/') !== -1) return p.replace('/cn/', '/en/');
    if (p.indexOf('/en/') !== -1) return p.replace('/en/', '/cn/');
    return new URL('cn/2026/index.html', window.location.href).pathname;
  }

  var target = otherLangPath(path);
  if (target === path) return;
  if (path.indexOf('print.html') !== -1) return;

  var right = document.querySelector('#menu-bar .right-buttons');
  if (!right) return;

  var a = document.createElement('a');
  a.className = 'pgw-menu-lang';
  a.href = target;
  var icon = document.createElement('i');
  icon.className = 'fa fa-language';
  icon.setAttribute('aria-hidden', 'true');
  var label = document.createElement('span');
  label.className = 'pgw-menu-lang-label';
  if (target.indexOf('/cn/') !== -1) {
    label.textContent = '中文';
    a.title = '切换到中文版';
    a.setAttribute('aria-label', '切换到中文版');
  } else {
    label.textContent = 'English';
    a.title = 'English version';
    a.setAttribute('aria-label', 'English version');
  }
  a.appendChild(icon);
  a.appendChild(label);
  right.insertBefore(a, right.firstChild);
})();
