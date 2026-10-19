/* KALYPSO — небольшое прогрессивное улучшение интерфейса.
   Без этого файла всё работает: фильтры и формы — обычные GET/POST, графики — SVG с сервера.
   Здесь только: переключатель темы, стадии по выбранной нозологии, единица по исследованию,
   отметка «график в поле зрения» для анимации. Данных пациентов скрипт не читает и не отправляет. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "kalypso-theme";
  var THEMES = ["auto", "light", "dark"];
  var NAMES = { auto: "как в системе", light: "светлая", dark: "тёмная" };
  var ICONS = { auto: "◐", light: "☀", dark: "☾" };

  function readTheme() {
    try { var t = localStorage.getItem(KEY); return THEMES.indexOf(t) > 0 ? t : "auto"; } catch (e) { return "auto"; }
  }
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
  }
  var theme = readTheme();
  applyTheme(theme);          // до отрисовки страницы: скрипт подключён в <head> без defer
  root.classList.add("js");

  function initTheme() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    function show() {
      btn.textContent = ICONS[theme] + " Тема";
      btn.title = "Тема: " + NAMES[theme] + ". Нажмите, чтобы сменить";
      btn.setAttribute("aria-label", "Тема оформления: " + NAMES[theme] + ". Сменить");
    }
    btn.hidden = false;
    show();
    btn.addEventListener("click", function () {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      try { if (theme === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, theme); } catch (e) { /* без хранилища — до перезагрузки */ }
      applyTheme(theme);
      show();
    });
  }

  function option(sel, value, text) {
    var o = document.createElement("option");
    o.value = value; o.textContent = text; sel.appendChild(o);
    return o;
  }

  /* Фильтры: при выборе нозологии список стадий сразу становится её списком (без «Показать»). */
  function initStages() {
    var forms = document.querySelectorAll("form[data-stages]");
    Array.prototype.forEach.call(forms, function (form) {
      var map;
      try { map = JSON.parse(form.getAttribute("data-stages")); } catch (e) { return; }
      var nos = form.querySelector('select[name="nosology"]');
      var pick = form.querySelector(".stage-pick");
      var sel = pick && pick.querySelector("select");
      if (!nos || !sel) return;
      nos.addEventListener("change", function () {
        /* «Исследователь»: группировки, анализы и подгруппы — свои у нозологии, их строит сервер */
        if (form.hasAttribute("data-resubmit")) { form.submit(); return; }
        var stages = map[nos.value] || {};
        var systems = Object.keys(stages);
        sel.innerHTML = "";
        if (!nos.value) {
          option(sel, "", "— выберите нозологию");
          sel.disabled = true; pick.hidden = false;
          return;
        }
        if (!systems.length) { sel.disabled = true; pick.hidden = true; return; }
        sel.disabled = false; pick.hidden = false; sel.name = "stage";
        option(sel, "", "все");
        systems.forEach(function (system) {
          var g = document.createElement("optgroup");
          g.label = system;
          stages[system].forEach(function (v) { option(g, system + ":" + v, system + " " + v); });
          sel.appendChild(g);
        });
      });
    });
  }

  /* Исследования: подсказка единицы — из справочника выбранного исследования. */
  function initUnits() {
    var inputs = document.querySelectorAll("input[name=unit][data-units]");
    Array.prototype.forEach.call(inputs, function (unit) {
      var map;
      try { map = JSON.parse(unit.getAttribute("data-units")); } catch (e) { return; }
      var test = unit.form && unit.form.querySelector('select[name="test_code"]');
      if (!test) return;
      function update() {
        var u = map[test.value];
        unit.placeholder = u ? u + " (из справочника)" : "единица";
      }
      test.addEventListener("change", update);
      update();
    });
  }

  /* Графики: класс .in — когда SVG попал в поле зрения; CSS тогда «прорисовывает» кривые и полосы. */
  function initReveal() {
    var charts = document.querySelectorAll("svg.viz");
    var mark = function (el) { el.classList.add("in"); };
    if (!("IntersectionObserver" in window)) { Array.prototype.forEach.call(charts, mark); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { mark(e.target); io.unobserve(e.target); } });
    }, { threshold: 0.15 });
    Array.prototype.forEach.call(charts, function (el) { io.observe(el); });
  }

  function ready() {
    [initTheme, initStages, initUnits, initReveal].forEach(function (fn) {
      try { fn(); } catch (e) { if (fn === initReveal) root.classList.remove("js"); }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
