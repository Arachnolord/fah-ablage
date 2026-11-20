/* Erdacht und programmiert von Fabian Hammer.
   (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. */
(function () {
  "use strict";

  // ── Upload (Ziehen & Ablegen oder Auswahl) ─────────────────────────────
  var zone = document.getElementById("ablagezone");
  var wahl = document.getElementById("dateiwahl");
  var status = document.getElementById("uploadstatus");

  function hochladen(dateien) {
    if (!dateien || !dateien.length) return;
    var fd = new FormData();
    for (var i = 0; i < dateien.length; i++) fd.append("dateien", dateien[i]);
    status.textContent = dateien.length === 1 ? "Lade 1 Datei hoch …" : "Lade " + dateien.length + " Dateien hoch …";
    fetch("/api/upload", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) {
        var t = j.angenommen === 1 ? "1 Datei angenommen" : j.angenommen + " Dateien angenommen";
        if (j.abgelehnt) t += ", " + j.abgelehnt + " abgelehnt (leer oder über 200 MB)";
        status.textContent = t + " – wird jetzt gelesen und abgelegt.";
        aktualisieren();
      })
      .catch(function () { status.textContent = "Hochladen fehlgeschlagen. Bitte noch einmal versuchen."; });
    if (wahl) wahl.value = "";
  }

  if (zone && wahl) {
    wahl.addEventListener("change", function () { hochladen(wahl.files); });
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("drueber"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("drueber"); });
    });
    zone.addEventListener("drop", function (e) { hochladen(e.dataTransfer.files); });
  }

  // ── Kacheln live halten ───────────────────────────────────────────────
  var kacheln = document.getElementById("kacheln");
  function setze(feld, wert) {
    var el = kacheln && kacheln.querySelector('[data-feld="' + feld + '"]');
    if (el) el.textContent = wert;
  }
  function aktualisieren() {
    if (!kacheln) return;
    fetch("/api/status", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j) return;
        setze("warteschlange", j.warteschlange);
        setze("abgelegt", j.abgelegt);
        setze("unsicher", j.unsicher);
        setze("dubletten", j.dubletten);
        setze("import_fertig", j.import_fertig);
        setze("blick_geklaert", j.blick_geklaert);
        setze("blick_offen", j.blick_offen);
        setze("aktuell", j.ki_pause ? "KI startet gerade – wartet …" :
          j.aktuell ? (j.aktuell.quelle === "blick" ? "schaut ein unsicheres Dokument genauer an …" : "liest gerade ein Dokument …") :
          (j.warteschlange ? "wartet …" : "nichts zu tun"));
      })
      .catch(function () {});
  }
  if (kacheln) setInterval(function () { if (!document.hidden) aktualisieren(); }, 5000);

  // ── Filter sofort anwenden ─────────────────────────────────────────────
  document.querySelectorAll("[data-sofort]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });

  // ── Vorschaubilder: fehlt eins, Platzhalter zeigen ─────────────────────
  document.addEventListener("error", function (e) {
    var t = e.target;
    if (t && t.tagName === "IMG" && t.parentNode && t.parentNode.classList.contains("dokkachel-bild")) {
      t.parentNode.classList.add("ohne-bild");
    }
  }, true);

  // ── Sortierung in der Kachelansicht ────────────────────────────────────
  document.querySelectorAll("select[data-navigiere]").forEach(function (s) {
    s.addEventListener("change", function () { if (s.value) location.href = s.value; });
  });

  // ── Auswahlliste mit Eingabe: beim Anklicken immer ALLE Einträge zeigen ──
  // Browser filtern eine datalist nach dem eingetippten Text. Darum beim
  // Anklicken den Wert kurz als Platzhalter parken; wird nichts gewählt
  // oder getippt, kommt er beim Verlassen zurück.
  document.querySelectorAll("input[data-ganze-liste]").forEach(function (el) {
    function parken() {
      if (el.value && !el.dataset.geparkt) {
        el.dataset.geparkt = el.value;
        el.placeholder = el.value;
        el.value = "";
      }
    }
    el.addEventListener("mousedown", parken);
    el.addEventListener("focus", parken);
    el.addEventListener("input", function () { delete el.dataset.geparkt; });
    el.addEventListener("blur", function () {
      if (!el.value && el.dataset.geparkt) el.value = el.dataset.geparkt;
      delete el.dataset.geparkt;
      el.placeholder = "";
    });
    if (el.form) el.form.addEventListener("submit", function () {
      if (!el.value && el.dataset.geparkt) el.value = el.dataset.geparkt;
    });
  });

  // ── Mehrfachauswahl: Leiste zeigen, alle wählen, Umschalt-Klick für Bereiche ──
  var sammel = document.getElementById("sammel");
  if (sammel) {
    var boxen = Array.prototype.slice.call(document.querySelectorAll('input[name="ids"][form="sammel"]'));
    var alle = document.querySelector("[data-alle-waehlen]");
    var zahl = sammel.querySelector("[data-sammel-zahl]");
    var zuletzt = -1;
    var aktualisieren = function () {
      var n = boxen.filter(function (b) { return b.checked; }).length;
      sammel.hidden = n === 0;
      zahl.textContent = n + (n === 1 ? " Dokument" : " Dokumente") + " ausgewählt";
      if (alle) { alle.checked = n > 0 && n === boxen.length; alle.indeterminate = n > 0 && n < boxen.length; }
      boxen.forEach(function (b) {
        var huelle = b.closest(".dokkachel, tr");
        if (huelle) huelle.classList.toggle("gewaehlt", b.checked);
      });
    };
    boxen.forEach(function (b, i) {
      b.addEventListener("click", function (e) {
        if (e.shiftKey && zuletzt >= 0 && zuletzt !== i) {
          var von = Math.min(zuletzt, i), bis = Math.max(zuletzt, i);
          for (var k = von; k <= bis; k++) boxen[k].checked = b.checked;
        }
        zuletzt = i;
        aktualisieren();
      });
    });
    if (alle) alle.addEventListener("change", function () {
      boxen.forEach(function (b) { b.checked = alle.checked; });
      aktualisieren();
    });
    sammel.querySelector("[data-auswahl-aufheben]").addEventListener("click", function () {
      boxen.forEach(function (b) { b.checked = false; });
      aktualisieren();
    });
    sammel.querySelectorAll("button[data-frage]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        if (!window.confirm(b.getAttribute("data-frage"))) e.preventDefault();
      });
    });
    aktualisieren();   // nach „Zurück“ im Browser bleiben Häkchen stehen
  }

  // ── Großer Upload (Sicherung): Hinweis zeigen, Knopf sperren ──────────
  document.querySelectorAll("form[data-hochladen]").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button");
      if (b) { b.disabled = true; b.textContent = "Lädt hoch … bitte warten"; }
    });
  });

  // ── Rückfrage vor Papierkorb/Import ────────────────────────────────────
  document.querySelectorAll("form[data-bestaetigen]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-bestaetigen"))) e.preventDefault();
    });
  });

  // ── Ordnerwahl für den Import (nur Ordnernamen, gebaut per textContent) ─
  var wahl = document.querySelector("[data-ordnerwahl]");
  if (wahl) {
    var teile = wahl.querySelector("[data-teile]"), liste = wahl.querySelector("[data-liste]");
    var info = wahl.querySelector("[data-info]"), feld = wahl.querySelector("[data-wahl]");
    var nehmen = wahl.querySelector("[data-nehmen]"), geladen = false;
    var knopf = function (text, pfad, klasse) {
      var b = document.createElement("button");
      b.type = "button"; b.className = klasse; b.textContent = text;
      b.addEventListener("click", function () { laden(pfad); });
      return b;
    };
    var laden = function (pfad) {
      info.textContent = "Lädt …";
      fetch("/api/ordner?pfad=" + encodeURIComponent(pfad), { credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (a) {
          if (!a.ok) { info.textContent = a.j.fehler || "Ordner nicht lesbar."; if (pfad) laden(""); return; }
          var j = a.j;
          feld.value = j.pfad;
          teile.textContent = "";
          teile.appendChild(knopf("Persönlicher Ordner", "", "pfad-teil"));
          j.teile.forEach(function (t) {
            teile.appendChild(document.createTextNode(" › "));
            teile.appendChild(knopf(t.name, t.pfad, "pfad-teil"));
          });
          info.textContent = j.anzeige + " – " + j.dateien.toLocaleString("de-DE") + (j.ganz ? "" : "+") +
            (j.dateien === 1 && j.ganz ? " Datei" : " Dateien");
          liste.textContent = "";
          j.ordner.forEach(function (o) {
            var li = document.createElement("li");
            li.appendChild(knopf("📁 " + o.name, o.pfad, "ordner-eintrag"));
            liste.appendChild(li);
          });
          if (!j.ordner.length) {
            var leer = document.createElement("li");
            leer.className = "leer"; leer.textContent = "Keine Unterordner.";
            liste.appendChild(leer);
          }
          nehmen.disabled = j.dateien === 0;
        })
        .catch(function () { info.textContent = "Ordner konnte nicht geladen werden."; });
    };
    wahl.addEventListener("toggle", function () {
      if (wahl.open && !geladen) { geladen = true; laden(wahl.getAttribute("data-start") || ""); }
    });
  }

  // ── Schieberegler mit Stellungstext ───────────────────────────────────
  var regler = document.getElementById("schwelle");
  var reglerText = document.getElementById("schwelle-text");
  if (regler && reglerText) {
    var stellungen = JSON.parse(regler.getAttribute("data-stellungen"));
    var zeigen = function () {
      var v = parseFloat(regler.value), t = stellungen[0][1];
      stellungen.forEach(function (s) { if (v >= s[0]) t = s[1]; });
      reglerText.textContent = "ⓘ " + Math.round(v * 100) + " %: " + t;
    };
    regler.addEventListener("input", zeigen);
    zeigen();
  }
})();
