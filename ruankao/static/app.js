/* 渐进增强。
   页面在禁用 JS 时依然完全可用：表单走普通 POST，地区选择就是原生勾选框，
   提交动作不依赖任何脚本。这类工具的访问者可能来自各种奇怪的浏览器环境，
   不该因为没开 JS 就用不了。 */

(function () {
  "use strict";

  /* ---------- 全选 / 清空 / 反选 ---------- */
  Array.prototype.forEach.call(document.querySelectorAll("[data-bulk]"), function (btn) {
    btn.addEventListener("click", function () {
      var scope = btn.closest("form") || document;
      var mode = btn.getAttribute("data-bulk");
      Array.prototype.forEach.call(
        scope.querySelectorAll('input[name="region"]'),
        function (box) {
          if (mode === "all") box.checked = true;
          else if (mode === "none") box.checked = false;
          else box.checked = !box.checked;
        }
      );
    });
  });

  /* ---------- 订阅表单：提交前的本地校验 ---------- */
  var form = document.getElementById("subscribe-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      var chosen = form.querySelectorAll('input[name="region"]:checked');
      if (chosen.length === 0) {
        event.preventDefault();
        alert("请至少选择一个报考地区。");
        return;
      }
      var submit = form.querySelector('button[type="submit"]');
      if (submit) {
        submit.disabled = true;
        submit.textContent = "正在提交…";
      }
    });
  }

  /* ---------- 管理页：改动未保存就离开时提醒一下 ---------- */
  var manageForm = document.getElementById("manage-regions");
  if (manageForm) {
    var dirty = false;
    manageForm.addEventListener("change", function () { dirty = true; });
    manageForm.addEventListener("submit", function () { dirty = false; });
    window.addEventListener("beforeunload", function (event) {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }
})();
